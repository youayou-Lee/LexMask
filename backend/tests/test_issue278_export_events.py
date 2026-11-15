"""Issue#278 · D：四个 export 事件的白名单与全链流（#279 契约）。

判据：export_started / export_page_done×N / export_packaged 在场；未声明字段整事件
拒写并计数（白名单口径唯一出处 design §2.5）；失败路径 export_failed 必在场且
error_type 非空。全部 fail-open：日志失败不阻断导出。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import fitz
import pytest

from app.core.config import settings
from app.services import page_event_log
from app.services.export_service import ExportTaskManager
from app.services.job_store import JobItemStatus, JobStore, JobType
from app.services.partial_export_service import config_from_job_config, submit_item_export


def _make_pdf(path: Path, pages: int = 3) -> str:
    doc = fitz.open()
    for i in range(pages):
        page = doc.new_page(width=595, height=842)
        page.insert_text((72, 100), f"MARKER-P{i + 1}", fontsize=14)
        page.draw_rect(fitz.Rect(100, 200, 300, 260), color=(1, 0, 0), fill=(1, 0, 0))
    doc.save(str(path))
    doc.close()
    return str(path)


@pytest.fixture()
def log_env(tmp_path, monkeypatch):
    root = tmp_path / "logs"
    root.mkdir()
    page_event_log.reset_for_tests()
    page_event_log.set_log_root(str(root))
    monkeypatch.setattr(settings, "PAGE_EVENT_LOG_ENABLED", True)
    yield root
    page_event_log.set_log_root("")
    page_event_log.reset_for_tests()


def _seed(store: JobStore) -> tuple[str, str]:
    job_id = store.create_job(
        job_type=JobType.TEXT_BATCH, title="t", config={"watermark_delivery": "keep"}, owner_id="admin"
    )
    item_id = store.add_item(job_id, "file-1")
    store.update_item_status(item_id, JobItemStatus.AWAITING_REVIEW)
    pages = {str(p): {"status": "completed", "duration_ms": 100} for p in range(1, 4)}
    store.update_item_performance(item_id, {"recognition": {"page_count": 3, "pages": pages, "failed_pages": {}}})
    return job_id, item_id


def _events(log_root: Path, job_id: str, item_id: str) -> list[dict]:
    path = log_root / job_id / f"{item_id}.jsonl"
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


def _run(store, item_id, file_info, job_id):
    manager = ExportTaskManager()
    item_row = store.get_item(item_id)

    async def _flow():
        task = await submit_item_export(
            owner_id="admin", store=store, job_row=store.get_job(job_id), item_row=item_row,
            file_info=file_info, config=config_from_job_config({"watermark_delivery": "keep"}),
            kind="partial", pages=[1, 2], manager=manager,
        )
        for _ in range(600):
            if task.status in {"completed", "failed"}:
                break
            await asyncio.sleep(0.02)
        return task

    return asyncio.run(_flow())


def _info(src: Path) -> dict:
    return {
        "file_id": "file-1",
        "file_type": "pdf_scanned",
        "page_count": 3,
        "original_filename": "测试卷.pdf",
        "file_path": str(src),
        "bounding_boxes": {
            p: [{"id": f"b{p}", "x": 0.17, "y": 0.24, "width": 0.34, "height": 0.072,
                 "type": "PERSON", "page": p, "source": "manual"}]
            for p in range(1, 4)
        },
    }


def test_export_events_schema_and_flow(log_env, tmp_path):
    out_root = tmp_path / "out"
    out_root.mkdir()
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(settings, "OUTPUT_DIR", str(out_root))
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path))
    try:
        store = JobStore(str(tmp_path / "jobs.sqlite3"))
        job_id, item_id = _seed(store)
        src = Path(_make_pdf(tmp_path / "src.pdf", 3))

        task = _run(store, item_id, _info(src), job_id)
        assert task.status == "completed", task.error

        events = _events(log_env, job_id, item_id)
        kinds = [e["event"] for e in events]
        assert "export_started" in kinds
        page_dones = [e for e in events if e["event"] == "export_page_done"]
        assert len(page_dones) == 2, "逐页事件 N=选中页数"
        assert {e["page"] for e in page_dones} == {1, 2}
        assert "export_packaged" in kinds
        packaged = next(e for e in events if e["event"] == "export_packaged")
        assert packaged.get("bytes_total", 0) > 0

        started = next(e for e in events if e["event"] == "export_started")
        assert started["kind"] == "partial"
        assert started["pages_count"] == 2
        assert started["include_summary"] is False
        assert started["export_id"] == task.export_id
    finally:
        monkeypatch.undo()


def test_export_event_unknown_field_rejected(log_env):
    """白名单制：未声明字段整事件拒写 + 计数（不是静默丢弃）。"""
    before = page_event_log.stats()
    page_event_log.set_event_context("job-x", "item-y", "file-z")
    ok = page_event_log.log_event("export_page_done", export_id="e1", operator="mallory")
    assert ok is False
    after = page_event_log.stats()
    assert after["dropped_unknown_field"] == before["dropped_unknown_field"] + 1
    assert after["written"] == before["written"]


def test_export_failed_event_on_runner_error(log_env, tmp_path, monkeypatch):
    out_root = tmp_path / "out"
    out_root.mkdir()
    monkeypatch.setattr(settings, "OUTPUT_DIR", str(out_root))
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path))

    from app.services import partial_export_service as pes

    def _boom(**kwargs):
        raise RuntimeError("render exploded")

    monkeypatch.setattr(pes, "build_item_package", _boom)

    store = JobStore(str(tmp_path / "jobs.sqlite3"))
    job_id, item_id = _seed(store)
    src = Path(_make_pdf(tmp_path / "src.pdf", 3))

    task = _run(store, item_id, _info(src), job_id)
    assert task.status == "failed"

    events = _events(log_env, job_id, item_id)
    failed = [e for e in events if e["event"] == "export_failed"]
    assert failed, "失败路径必须写 export_failed"
    assert failed[-1]["error_type"], "error_type 非空"
    monkeypatch.undo()
