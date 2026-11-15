# Copyright 2026 LexMask Contributors

"""Issue#280 · C1/C2：整卷重跑不覆盖人工框（r2 冷审裁定：人工框永远保留并存）。

注入纪律（r2 F-C1）：页级失败若发生在 ``detect_vision`` 写回点**之前**，写回根本
不执行，删守卫也红不了（变异假绿）。因此 monkeypatch
``app.services.file_operations.vision_detect`` 为**包裹**：先调真实链（真实
``detect_vision`` 含写回点与守卫完整执行）再抛异常 ⇒ 页随后按失败记账，守卫死活可辨。

* C1：写回点之后失败 ⇒ 人工框存活、ledger 保留、投影回 failed（failed_pages 优先）；
* C2（主判据，兼 M2 真杀手）：自动成功 ⇒ 人工框与自动框**并存**（人工 N + 自动 M）、
  ledger 条目原样（零清理写）、投影 manual_verified 且 auto_completed=True、
  无任何取代事件/审计。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import pytest

import app.services.task_queue as tq
import app.services.task_queue_pipelines as tqp
from app.services.file_store_db import FileStoreDB
from app.services.job_store import JobItemStatus, JobStore, JobType
from app.services.page_status_service import build_page_status

_FILE_ID = "file-1"
_ORIGINAL_REASON = "视觉识别 page timeout after 300s"
_LEDGER_ENTRY = {
    "verified_at": "2026-11-15T08:00:00+00:00",
    "operator": "apitester",
    "box_count": 2,
    "original_reason": _ORIGINAL_REASON,
}


def _manual_boxes() -> list[dict[str, Any]]:
    return [
        {
            "id": f"manual-{i}",
            "page": 3,
            "x": 0.1 + i * 0.4,
            "y": 0.2,
            "width": 0.3,
            "height": 0.05,
            "type": "PERSON",
            "text": None,
            "selected": True,
            "confidence": None,
            "source": "manual",
            "evidence_source": "manual",
        }
        for i in range(2)
    ]


class _Harness:
    """真 JobStore + 真 file_store 替身 + 真 ``detect_vision``（内部识别栈打桩）。"""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, pages: int = 5):
        self.pages = pages
        self.store = JobStore(str(tmp_path / "jobs.sqlite3"))
        self.jid = self.store.create_job(job_type=JobType.TEXT_BATCH, title="t", owner_id="apitester")
        self.iid = self.store.add_item(self.jid, _FILE_ID)
        self.store.update_item_status(self.iid, JobItemStatus.AWAITING_REVIEW)
        self.store.update_item_performance(
            self.iid,
            {
                "recognition": {
                    "mode": "vision",
                    "page_count": pages,
                    "pages": {
                        "3": {"status": "failed", "error": _ORIGINAL_REASON},
                        **{str(p): {"status": "completed"} for p in range(1, pages + 1) if p != 3},
                    },
                    "failed_pages": {"3": _ORIGINAL_REASON},
                    "manual_verified_pages": {"3": dict(_LEDGER_ENTRY)},
                }
            },
        )
        self.file_store = FileStoreDB(str(tmp_path / "file_store.sqlite3"))
        self.file_store.set(
            _FILE_ID,
            {
                "file_id": _FILE_ID,
                "file_type": "image",
                "page_count": pages,
                "file_path": str(tmp_path / "page.png"),
                "bounding_boxes": {3: _manual_boxes()},
            },
        )

        import app.services.file_management_service as fms
        import app.services.redaction_orchestrator as orch
        from app.services.file_management_service import _file_store_lock

        monkeypatch.setattr(fms, "file_store", self.file_store)
        monkeypatch.setattr(orch, "_get_file_store", lambda: self.file_store)
        monkeypatch.setattr(orch, "_get_file_store_lock", lambda: _file_store_lock)

        class _StubVisionService:
            def __init__(self) -> None:
                self.last_warnings: list[str] = []
                self.last_pipeline_status: dict = {"ocr_has": {"ran": True}}
                self.last_duration_ms: dict = {"ocr_has": 5}

            async def detect_with_dual_pipeline(self, **kwargs):
                page = int(kwargs.get("page") or 1)
                return [
                    {"id": f"auto-{page}", "page": page, "x": 0.2, "y": 0.3, "width": 0.2,
                     "height": 0.05, "type": "PERSON", "source": "ocr_has", "selected": True}
                ], None

            def _deduplicate_boxes(self, boxes):
                return list(boxes)

        monkeypatch.setattr(orch, "VisionService", _StubVisionService)

        async def _stub_docline(**kwargs):
            return None, None

        monkeypatch.setattr(orch, "text_layer_doc_boxes_for_page", _stub_docline)

        self.queue = tq.SimpleTaskQueue(concurrency=1)
        self.queue._get_store = lambda: self.store  # type: ignore[method-assign]

        self.detect_calls: list[int] = []

    def item_row(self) -> dict[str, Any]:
        return self.store.get_item(self.iid) or {}

    def recognition(self) -> dict[str, Any]:
        return (self.store.get_item_performance(self.iid) or {}).get("recognition") or {}

    def boxes_on_page3(self) -> list[dict[str, Any]]:
        return tqp._boxes_by_page(self.file_store.get(_FILE_ID).get("bounding_boxes")).get(3, [])

    def patch_detect(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        fail_after_write_pages: set[int] = frozenset(),
    ) -> None:
        """包裹注入（r2 F-C1）：真实链（写回点+守卫）完整执行后再抛异常。"""
        import app.services.file_operations as fo

        real_detect = fo.vision_detect
        fail_pages = set(fail_after_write_pages)

        async def wrapped(file_id: str, page: int, **kwargs: Any):
            self.detect_calls.append(page)
            result = await real_detect(file_id, page, **kwargs)
            if page in fail_pages:
                raise RuntimeError("injected failure after write-back")
            return result

        monkeypatch.setattr(fo, "vision_detect", wrapped)

    def task(self) -> tq.TaskItem:
        return tq.TaskItem(
            job_id=self.jid, item_id=self.iid, file_id=_FILE_ID, task_type="recognition",
        )

    def run_vision(self, *, ocr_has_types: list[str] | None = None) -> None:
        cfg = {"visual_feature_types": [], "ocr_has_types": ocr_has_types or []}
        asyncio.run(self.queue._run_vision(self.task(), dict(cfg)))


@pytest.fixture()
def audit_spy(tmp_path, monkeypatch):
    """审计面监听：C2 断言「无取代审计」用。"""
    from app.core import audit as audit_mod

    path = tmp_path / "audit.log"
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(message)s"))
    monkeypatch.setattr(audit_mod._audit_logger, "handlers", [handler])
    yield path
    handler.close()


def test_full_rerun_preserves_manual_boxes_on_still_failed_page(tmp_path, monkeypatch):
    """C1：页在写回点之后失败 → 人工框存活、ledger 保留、投影回 failed。"""
    harness = _Harness(tmp_path, monkeypatch)
    harness.patch_detect(monkeypatch, fail_after_write_pages={3})

    harness.run_vision(ocr_has_types=["姓名"])

    # 人工框存活（条数不变），且与本次识别的自动框并存（写回点已执行）
    boxes = harness.boxes_on_page3()
    manual = [box for box in boxes if box.get("source") == "manual"]
    assert len(manual) == 2
    assert any(box.get("source") == "ocr_has" for box in boxes)

    # ledger 条目保留（保留语义：零清理写）
    assert harness.recognition()["manual_verified_pages"].get("3") == _LEDGER_ENTRY
    # 重跑再失败：failed_pages 重新出现页 3 ⇒ 投影以 failed_pages 为准回红叉
    assert harness.recognition()["failed_pages"].get("3")
    projection = build_page_status(harness.item_row(), {"file_type": "image", "page_count": 5})
    cell = next(cell for cell in projection["pages"] if cell["page"] == 3)
    assert cell["state"] == "failed"


def test_full_rerun_keeps_manual_boxes_on_auto_success(tmp_path, monkeypatch, audit_spy):
    """C2（M2 真杀手）：自动成功 → 人工框与自动框并存、ledger 原样、无取代痕迹。"""
    harness = _Harness(tmp_path, monkeypatch)
    harness.patch_detect(monkeypatch)  # 全部页自动成功

    harness.run_vision(ocr_has_types=["姓名"])

    # 页 3 自动 completed
    assert harness.recognition()["pages"]["3"]["status"] == "completed"
    # ledger 条目原样（无任何清理写）
    assert harness.recognition()["manual_verified_pages"].get("3") == _LEDGER_ENTRY
    # 并存：人工 N=2 + 自动 M≥1
    boxes = harness.boxes_on_page3()
    manual = [box for box in boxes if box.get("source") == "manual"]
    auto = [box for box in boxes if box.get("source") == "ocr_has"]
    assert len(manual) == 2
    assert len(auto) >= 1

    # 投影：manual_verified 维持 + auto_completed=True（琥珀徽标的共存提示输入）
    projection = build_page_status(harness.item_row(), {"file_type": "image", "page_count": 5})
    cell = next(cell for cell in projection["pages"] if cell["page"] == 3)
    assert cell["state"] == "manual_verified"
    assert cell["manual"]["auto_completed"] is True

    # 无取代事件 schema、无取代审计
    from app.services import page_event_log as page_events

    assert "manual_superseded" not in page_events.EVENT_SCHEMAS
    assert audit_spy.read_text(encoding="utf-8").strip() == ""
