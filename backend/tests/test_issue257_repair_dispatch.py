# Copyright 2026 LexMask Contributors

"""Issue#257 一期：重启补偿要认得出「单页重试」这个任务类型（AC2.3 重启补偿条）。

桩点：``repair_stale_processing_items`` 只按 job_type 判 structured/recognition，
单页重试中断后会当成整卷重识别 ⇒ 十几分钟的活白烧。修法=看
``performance_json.recognition.page_retry_pages`` 是否非空，非空则派 ``page_retry``。
"""

from __future__ import annotations

import sqlite3

from app.services.job_models import JobItemStatus, JobType
from app.services.job_store import JobStore

_STALE_ISO = "2020-01-01T00:00:00Z"


def _make_stale_item(store: JobStore, db_path: str, *, page_retry_pages: list[int] | None) -> str:
    jid = store.create_job(job_type=JobType.TEXT_BATCH, title="t", owner_id="admin")
    iid = store.add_item(jid, "file-1")
    store.update_item_status(iid, JobItemStatus.PROCESSING)
    recognition: dict = {"page_count": 10, "failed_pages": {"5": "ocr boom"}}
    if page_retry_pages is not None:
        recognition["page_retry_pages"] = page_retry_pages
    store.update_item_performance(iid, {"recognition": recognition})
    # 最后再把心跳拨到过去：repair 的 1s 下限会把刚写过的行判成「还活着」。
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            "UPDATE job_items SET progress_updated_at = ?, updated_at = ? WHERE id = ?",
            (_STALE_ISO, _STALE_ISO, iid),
        )
        connection.commit()
    finally:
        connection.close()
    return iid


def test_stale_page_retry_is_redispatched_as_page_retry(tmp_path) -> None:
    db_path = str(tmp_path / "jobs.sqlite3")
    store = JobStore(db_path)
    iid = _make_stale_item(store, db_path, page_retry_pages=[5])

    rows = store.repair_stale_processing_items(max_age_seconds=1)

    dispatched = [row for row in rows if row["item_id"] == iid]
    assert len(dispatched) == 1
    assert dispatched[0]["task"] == "page_retry"
    assert store.get_item(iid)["status"] == JobItemStatus.PENDING.value


def test_stale_full_recognition_still_redispatched_as_recognition(tmp_path) -> None:
    db_path = str(tmp_path / "jobs.sqlite3")
    store = JobStore(db_path)
    iid = _make_stale_item(store, db_path, page_retry_pages=None)

    rows = store.repair_stale_processing_items(max_age_seconds=1)

    dispatched = [row for row in rows if row["item_id"] == iid]
    assert len(dispatched) == 1
    assert dispatched[0]["task"] == "recognition"


def test_cleared_marker_does_not_leave_page_retry_task(tmp_path) -> None:
    db_path = str(tmp_path / "jobs.sqlite3")
    store = JobStore(db_path)
    iid = _make_stale_item(store, db_path, page_retry_pages=[])

    rows = store.repair_stale_processing_items(max_age_seconds=1)

    dispatched = [row for row in rows if row["item_id"] == iid]
    assert len(dispatched) == 1
    assert dispatched[0]["task"] == "recognition"
