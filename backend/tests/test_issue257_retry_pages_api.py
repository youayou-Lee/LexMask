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


def test_submit_writes_state_before_marker_and_delivers_last(tmp_path):
    """写入顺序（Min-4，变异 f 曾 SURVIVED）：状态 → 标记 → 投递。

    理由：先落标记再改状态，进程在两者之间崩溃会留下「标记在、状态未变」的
    死锁（准入认为忙、补偿看不到 PROCESSING）；先改状态则最多回落整卷重识别，
    慢但正确。投递必须在两者之后，否则 worker 可能读到半成品状态。

    顺序本身只能靠调用序列钉住（崩溃窗口在进程内不可观测），所以用记录代理
    store 捕获调用顺序，而不是只断言最终状态。
    """
    store, jid, iid = _make_item(tmp_path)
    calls: list[str] = []

    class _RecordingStore:
        def __getattr__(self, name):  # 其余方法直接转给真 store
            return getattr(store, name)

        def update_item_status(self, *args, **kwargs):
            calls.append("update_item_status")
            return store.update_item_status(*args, **kwargs)

        def update_item_performance(self, *args, **kwargs):
            calls.append("update_item_performance")
            return store.update_item_performance(*args, **kwargs)

        def touch_job_updated(self, *args, **kwargs):
            calls.append("touch_job_updated")
            return store.touch_job_updated(*args, **kwargs)

    def _enqueue(task):
        calls.append("enqueue")

    asyncio.run(
        page_retry_service.submit_page_retry(
            store=_RecordingStore(),
            job=store.get_job(jid),
            item=store.get_item(iid),
            pages=[5],
            enqueue=_enqueue,
        )
    )

    assert calls == [
        "update_item_status",
        "update_item_performance",
        "touch_job_updated",
        "enqueue",
    ], f"写入顺序不对：{calls}"
