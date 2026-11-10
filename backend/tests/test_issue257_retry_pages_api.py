# Copyright 2026 LexMask Contributors

"""Issue#257 一期能力 2：单页重试的前置校验与任务提交（AC2.3）。

钉的是「谁能重试、重试什么、并发怎么挡」——失败账消账与翻绿在
``test_issue257_page_retry_task.py`` 里测，这里只测拒绝面与提交面。
"""

from __future__ import annotations

import asyncio

import pytest

from app.services import page_retry_service
from app.services.job_models import JobItemStatus, JobType
from app.services.job_store import JobStore
from app.services.page_retry_service import PageRetryError


def _make_item(tmp_path, *, status=JobItemStatus.AWAITING_REVIEW):
    store = JobStore(str(tmp_path / "jobs.sqlite3"))
    jid = store.create_job(job_type=JobType.TEXT_BATCH, title="t", owner_id="admin")
    iid = store.add_item(jid, "file-1")
    # 走状态机的合法路径到目标态（PENDING→PROCESSING→…），避免 InvalidStatusTransition
    if status != JobItemStatus.PENDING and status != JobItemStatus.CANCELLED:
        store.update_item_status(iid, JobItemStatus.PROCESSING)
    store.update_item_status(iid, status)
    store.update_item_performance(
        iid,
        {
            "recognition": {
                "page_count": 10,
                "failed_pages": {"5": "ocr boom"},
                "pages": {
                    "5": {"status": "failed", "error": "ocr boom"},
                    "6": {"status": "completed", "duration_ms": 10},
                },
            }
        },
    )
    return store, jid, iid


def _validate(store, iid, pages, *, file_type="pdf"):
    perf = store.get_item_performance(iid) or {}
    recognition = perf.get("recognition") or {}
    return page_retry_service.validate_page_retry(
        store.get_item(iid),
        pages,
        page_count=int(recognition.get("page_count") or 0),
        failed_pages=recognition.get("failed_pages") or {},
        file_type=file_type,
    )


def _code_of(fn):
    with pytest.raises(PageRetryError) as info:
        fn()
    return info.value.code


class TestValidation:
    def test_empty_pages_rejected(self, tmp_path):
        store, _jid, iid = _make_item(tmp_path)
        assert _code_of(lambda: _validate(store, iid, [])) == "pages_empty"

    def test_out_of_range_rejected(self, tmp_path):
        store, _jid, iid = _make_item(tmp_path)
        assert _code_of(lambda: _validate(store, iid, [0])) == "pages_out_of_range"
        assert _code_of(lambda: _validate(store, iid, [11])) == "pages_out_of_range"

    def test_page_not_in_failed_list_rejected(self, tmp_path):
        store, _jid, iid = _make_item(tmp_path)
        # 页 6 是成功页——重试它没有语义（正常路径是 reject 整卷重跑）。
        assert _code_of(lambda: _validate(store, iid, [6])) == "page_not_failed"

    def test_non_visual_type_rejected(self, tmp_path):
        store, _jid, iid = _make_item(tmp_path)
        assert (
            _code_of(lambda: _validate(store, iid, [5], file_type="docx"))
            == "partial_retry_unsupported_type"
        )

    def test_duplicate_pages_are_deduped_and_sorted(self, tmp_path):
        store, _jid, iid = _make_item(tmp_path)
        store.update_item_performance(
            iid,
            {"recognition": {"failed_pages": {"5": "ocr boom", "7": "ner boom"}}},
        )
        assert _validate(store, iid, [7, 5, 7]) == [5, 7]

    def test_processing_item_is_busy(self, tmp_path):
        store, _jid, iid = _make_item(tmp_path, status=JobItemStatus.PROCESSING)
        assert _code_of(lambda: _validate(store, iid, [5])) == "item_busy"

    @pytest.mark.parametrize(
        "status",
        [JobItemStatus.COMPLETED, JobItemStatus.FAILED, JobItemStatus.PENDING],
    )
    def test_only_awaiting_review_may_retry(self, tmp_path, status):
        store, _jid, iid = _make_item(tmp_path, status=status)
        assert (
            _code_of(lambda: _validate(store, iid, [5])) == "retry_requires_awaiting_review"
        )

    def test_in_flight_retry_is_busy(self, tmp_path):
        store, _jid, iid = _make_item(tmp_path)
        store.update_item_performance(iid, {"recognition": {"page_retry_pages": [5]}})
        assert _code_of(lambda: _validate(store, iid, [5])) == "item_busy"


class TestSubmit:
    def test_submit_marks_marker_enqueues_and_moves_item_to_processing(self, tmp_path):
        store, jid, iid = _make_item(tmp_path)
        enqueued = []

        result = asyncio.run(
            page_retry_service.submit_page_retry(
                store=store,
                job=store.get_job(jid),
                item=store.get_item(iid),
                pages=[5],
                enqueue=lambda task: enqueued.append(task),
            )
        )

        assert result["queued_pages"] == [5]
        assert store.get_item(iid)["status"] == JobItemStatus.PROCESSING.value
        perf = store.get_item_performance(iid)
        assert perf["recognition"]["page_retry_pages"] == [5]
        assert len(enqueued) == 1
        assert enqueued[0].task_type == "page_retry"
        assert enqueued[0].meta["pages"] == [5]
        assert enqueued[0].job_id == jid
        assert enqueued[0].item_id == iid
