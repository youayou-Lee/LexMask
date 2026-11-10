"""Issue#257 PR2 完整评审返修的回归判据（C1/C2/C3/I1/I6）。

每条都对应评审里的具体失败模式；判据都钉在可观测行为上（账本内容 / 状态机 /
草稿框集合 / 返回值），不看实现细节。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from app.services import page_retry_service, task_queue as tq
from app.services.job_store import JobItemStatus, JobStore, JobType
from app.services.single_task_service import redispatch_task_type_for


def _make_store(tmp_path):
    return JobStore(str(tmp_path / "jobs.sqlite3"))


def _make_item(store: JobStore, *, status=JobItemStatus.AWAITING_REVIEW, performance=None):
    jid = store.create_job(job_type=JobType.TEXT_BATCH, title="t", owner_id="admin")
    iid = store.add_item(jid, "file-1")
    if status != JobItemStatus.PENDING:
        store.update_item_status(iid, status)
    if performance:
        store.update_item_performance(iid, performance)
    return jid, iid


# ---------------------------------------------------------------------------
# C1：重启/看门狗回落必须带着页清单重派 page_retry，且整卷识别要清标记
# ---------------------------------------------------------------------------

def test_redispatch_keeps_page_retry_scope_from_marker(tmp_path):
    store = _make_store(tmp_path)
    # 崩溃/重启时留下的形态：状态还是处理中，页清单只在标记里。
    jid, iid = _make_item(
        store,
        status=JobItemStatus.PROCESSING,
        performance={"recognition": {"page_retry_pages": [5, 7]}},
    )
    job, item = store.get_job(jid), store.get_item(iid)

    assert redispatch_task_type_for(job, item) == "page_retry"
    assert page_retry_service.in_flight_pages(item) == [5, 7]

    store.update_item_performance(iid, {"recognition": {"page_retry_pages": []}})
    assert redispatch_task_type_for(job, store.get_item(iid)) == "recognition"


def test_recovery_after_retry_crash_does_not_stay_busy(tmp_path):
    """标记非空但状态已回审阅态时，必须允许重新提交（否则永久 409）。"""
    store = _make_store(tmp_path)
    _jid, iid = _make_item(
        store,
        performance={
            "recognition": {
                "page_count": 10,
                "failed_pages": {"5": "ocr boom"},
                "page_retry_pages": [5],
            }
        },
    )
    pages = page_retry_service.validate_page_retry(
        store.get_item(iid), [5], page_count=10, failed_pages={5: "ocr boom"}, file_type="pdf"
    )
    assert pages == [5]


# ---------------------------------------------------------------------------
# C2：重试页里的手工框必须保留，自动框才换新
# ---------------------------------------------------------------------------

class _Harness:
    def __init__(self, tmp_path):
        self.store = _make_store(tmp_path)
        self.jid, self.iid = _make_item(self.store)
        self.queue = tq.SimpleTaskQueue(concurrency=1)
        self.queue._get_store = lambda: self.store


def test_manual_boxes_on_retried_page_survive(tmp_path):
    from app.services.file_management_service import file_store

    h = _Harness(tmp_path)
    file_store.set(
        "file-1",
        {
            "bounding_boxes": {
                5: [
                    {
                        "id": "new5",
                        "x": 0.1,
                        "y": 0.1,
                        "width": 0.1,
                        "height": 0.1,
                        "type": "PERSON",
                        "page": 5,
                        "source": "has",
                    }
                ]
            },
            "entities": [],
        },
    )
    h.store.save_item_review_draft(
        h.iid,
        {
            "bounding_boxes": [
                {"id": "auto5", "x": 0.2, "y": 0.2, "width": 0.1, "height": 0.1, "type": "PERSON", "page": 5, "source": "has"},
                {"id": "manual5", "x": 0.3, "y": 0.3, "width": 0.1, "height": 0.1, "type": "PERSON", "page": 5, "source": "manual"},
                {"id": "keep4", "x": 0.4, "y": 0.4, "width": 0.1, "height": 0.1, "type": "PERSON", "page": 4, "source": "has"},
            ],
            "entities": [],
        },
    )

    h.queue._merge_retried_boxes_into_review_draft(h.store, h.iid, [5])

    draft = h.store.get_item_review_draft(h.iid)
    ids = {box["id"] for box in draft["bounding_boxes"]}
    assert ids == {"manual5", "keep4", "new5"}  # 自动框换新、人工框与其它页原样保留


# ---------------------------------------------------------------------------
# C3：整卷重跑成功后失败清单必须整体清掉（深合并会留下旧键）
# ---------------------------------------------------------------------------

def test_full_rerun_clears_stale_failed_pages(tmp_path):
    h = _Harness(tmp_path)
    h.store.update_item_performance(
        h.iid, {"recognition": {"failed_pages": {"3": "old error", "4": "old error"}}}
    )

    h.queue._write_failed_pages(h.store, h.iid, {})

    perf = h.store.get_item_performance(h.iid)
    assert perf["recognition"]["failed_pages"] == {}


def test_full_rerun_replaces_page_record_keys(tmp_path):
    h = _Harness(tmp_path)
    h.store.update_item_performance(
        h.iid,
        {
            "recognition": {
                "pages": {
                    "3": {
                        "page": 3,
                        "status": "failed",
                        "error": "old boom",
                        "stage_duration_ms": {"视觉识别": 5},
                    }
                }
            }
        },
    )

    # 走与 run_page 成功分支同一条写入（replace_paths 由 _record_item_performance 透传）
    h.queue._record_item_performance(
        h.store,
        h.iid,
        {"recognition": {"pages": {"3": {"page": 3, "status": "completed", "duration_ms": 9}}}},
        replace_paths=(("recognition", "pages", "3"),),
    )

    record = h.store.get_item_performance(h.iid)["recognition"]["pages"]["3"]
    assert record == {"page": 3, "status": "completed", "duration_ms": 9}


# ---------------------------------------------------------------------------
# I1：并发提交必须只有一笔成功（第二笔看到 processing → item_busy）
# ---------------------------------------------------------------------------

def test_second_submit_is_rejected_while_first_in_flight(tmp_path):
    store = _make_store(tmp_path)
    jid, iid = _make_item(
        store,
        performance={"recognition": {"page_count": 10, "failed_pages": {"5": "ocr boom", "6": "x"}}},
    )
    job, item = store.get_job(jid), store.get_item(iid)
    enqueued: list = []

    asyncio.run(
        page_retry_service.submit_page_retry(
            store=store, job=job, item=item, pages=[5], enqueue=enqueued.append
        )
    )
    assert len(enqueued) == 1

    with pytest.raises(page_retry_service.PageRetryError) as info:
        asyncio.run(
            page_retry_service.submit_page_retry(
                store=store, job=job, item=store.get_item(iid), pages=[6], enqueue=enqueued.append
            )
        )
    assert info.value.code == "item_busy"
    assert len(enqueued) == 1
    assert page_retry_service.in_flight_pages(store.get_item(iid)) == [5]


# ---------------------------------------------------------------------------
# I6：空页清单的空转任务必须把状态落回来，不能挂到 watchdog
# ---------------------------------------------------------------------------

def test_empty_scope_task_restores_review_status(tmp_path):
    h = _Harness(tmp_path)
    h.store.update_item_status(h.iid, JobItemStatus.PROCESSING)
    h.store.set_item_error_message(h.iid, "stale")

    task = tq.TaskItem(job_id=h.jid, item_id=h.iid, file_id="file-1", task_type="page_retry", meta={})
    asyncio.run(h.queue._run_page_retry(task))

    item = h.store.get_item(h.iid)
    assert item["status"] == JobItemStatus.AWAITING_REVIEW.value
    assert page_retry_service.in_flight_pages(item) == []
