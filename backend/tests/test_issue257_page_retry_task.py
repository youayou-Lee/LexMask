# Copyright 2026 LexMask Contributors

"""Issue#257 一期能力 2：单页重试任务本体（AC2.1 / AC2.2 / AC2.4）。

钉住四件事：
1. 只重试被点名的失败页，**其余页逐字节不变**（AC2.1 第 2 条）；
2. 成功后失败账消账、页记录**整体替换**（旧 ``error``/``stage_duration_ms`` 不残留 —— F1 在真库上的红性，AC2.2）；
3. 再失败保持红，原因换成新的，item 仍回 ``awaiting_review``（AC2.4）；
4. 重试成功的新框必须并入既有 review draft 的该页（否则交付时旧草稿覆盖新结果）。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

import app.services.task_queue as tq
from app.services.job_models import JobItemStatus, JobType
from app.services.job_store import JobStore

_FILE_ID = "file-1"


class _Harness:
    def __init__(self, tmp_path: Any, *, page_count: int = 10) -> None:
        self.store = JobStore(str(tmp_path / "jobs.sqlite3"))
        self.jid = self.store.create_job(job_type=JobType.TEXT_BATCH, title="t", owner_id="admin")
        self.iid = self.store.add_item(self.jid, _FILE_ID)
        self.store.update_item_status(self.iid, JobItemStatus.AWAITING_REVIEW)
        self.store.update_item_performance(
            self.iid,
            {
                "recognition": {
                    "page_count": page_count,
                    "failed_pages": {"5": "ocr boom"},
                    "pages": {
                        "5": {
                            "page": 5,
                            "started_at": "2026-11-11T00:00:00Z",
                            "finished_at": "2026-11-11T00:00:03Z",
                            "duration_ms": 3000,
                            "status": "failed",
                            "error": "ocr boom",
                            "stage_duration_ms": {"ocr": 3000},
                        },
                        "6": {
                            "page": 6,
                            "started_at": "2026-11-11T00:00:04Z",
                            "finished_at": "2026-11-11T00:00:06Z",
                            "duration_ms": 2000,
                            "status": "completed",
                        },
                    },
                }
            },
        )
        self.queue = tq.SimpleTaskQueue(concurrency=1)
        self.queue._get_store = lambda: self.store  # type: ignore[method-assign]
        self.calls: list[tuple[str, int, dict[str, Any]]] = []

    def perf(self) -> dict[str, Any]:
        return self.store.get_item_performance(self.iid) or {}

    def task(self, pages: list[int]) -> tq.TaskItem:
        return tq.TaskItem(
            job_id=self.jid,
            item_id=self.iid,
            file_id=_FILE_ID,
            task_type="page_retry",
            meta={"pages": pages},
        )


def _patch_detect(monkeypatch: pytest.MonkeyPatch, harness: _Harness, *, behaviour: str) -> None:
    import app.services.file_operations as fo

    async def _detect(file_id: str, page: int, **kwargs: Any) -> dict[str, Any]:
        harness.calls.append((file_id, page, kwargs))
        if behaviour == "fail":
            raise ValueError("ner boom again")
        return {"bounding_boxes": [], "entities": []}

    monkeypatch.setattr(fo, "vision_detect", _detect)


def _boxes_by_page(harness: _Harness, boxes: dict[str, list[dict[str, Any]]]) -> None:
    from app.services.file_management_service import file_store

    file_store.set(_FILE_ID, {"file_id": _FILE_ID, "bounding_boxes": boxes})


def test_retry_success_turns_page_green_and_clears_failed_account(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _Harness(tmp_path)
    _boxes_by_page(harness, {"5": [{"id": "b5", "page": 5, "x": 0.1, "y": 0.1, "width": 0.2, "height": 0.05, "type": "PERSON"}]})
    _patch_detect(monkeypatch, harness, behaviour="ok")
    before = json.dumps(harness.perf()["recognition"]["pages"]["6"], sort_keys=True)

    asyncio.run(harness.queue._run_page_retry(harness.task([5])))

    perf = harness.perf()
    recognition = perf["recognition"]
    assert "5" not in (recognition.get("failed_pages") or {})
    page5 = recognition["pages"]["5"]
    assert page5["status"] == "completed"
    assert "error" not in page5
    assert "stage_duration_ms" not in page5
    assert page5["duration_ms"] >= 0
    # 其余页逐字节不变
    assert json.dumps(recognition["pages"]["6"], sort_keys=True) == before
    # 重试标记清空、item 回到审阅态、错误摘要随之清空
    assert recognition["page_retry_pages"] == []
    item = harness.store.get_item(harness.iid)
    assert item["status"] == JobItemStatus.AWAITING_REVIEW.value
    assert not item.get("error_message")
    # 调用面：仅该页、merge_existing + force 都必须带
    assert len(harness.calls) == 1
    _file_id, page, kwargs = harness.calls[0]
    assert page == 5
    assert kwargs.get("merge_existing") is True
    assert kwargs.get("force") is True


def test_retry_failure_keeps_page_red_with_new_reason(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    harness = _Harness(tmp_path)
    _patch_detect(monkeypatch, harness, behaviour="fail")

    asyncio.run(harness.queue._run_page_retry(harness.task([5])))

    recognition = harness.perf()["recognition"]
    assert "5" in recognition["failed_pages"]
    assert "ValueError" in recognition["failed_pages"]["5"]
    page5 = recognition["pages"]["5"]
    assert page5["status"] == "failed"
    assert "ValueError" in page5["error"]
    assert "ocr boom" not in page5["error"]  # 旧 error 被新 error 覆盖，不留两条
    assert recognition["page_retry_pages"] == []
    item = harness.store.get_item(harness.iid)
    assert item["status"] == JobItemStatus.AWAITING_REVIEW.value
    assert item.get("error_message")  # 仍是失败摘要
    # 仍可再次发起重试（标记已清）
    assert recognition["pages"]["6"]["status"] == "completed"


def test_retry_success_merges_review_draft_boxes_for_that_page(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _Harness(tmp_path)
    old5 = {"id": "old5", "page": 5, "x": 0.5, "y": 0.5, "width": 0.1, "height": 0.1, "type": "PERSON"}
    keep4 = {"id": "keep4", "page": 4, "x": 0.2, "y": 0.2, "width": 0.1, "height": 0.1, "type": "PERSON"}
    keep6 = {"id": "keep6", "page": 6, "x": 0.3, "y": 0.3, "width": 0.1, "height": 0.1, "type": "PERSON"}
    harness.store.save_item_review_draft(
        harness.iid, {"entities": [], "bounding_boxes": [old5, keep4, keep6]}
    )
    new5 = {"id": "new5", "page": 5, "x": 0.7, "y": 0.7, "width": 0.1, "height": 0.1, "type": "PERSON"}
    _boxes_by_page(harness, {"5": [new5]})
    _patch_detect(monkeypatch, harness, behaviour="ok")

    asyncio.run(harness.queue._run_page_retry(harness.task([5])))

    draft = harness.store.get_item_review_draft(harness.iid) or {}
    ids = [box["id"] for box in draft.get("bounding_boxes") or []]
    assert "old5" not in ids  # 该页旧框被替换
    assert "new5" in ids
    assert set(ids) == {"keep4", "keep6", "new5"}
