"""Issue#257 一期 · 端点契约（页状态 / 部分导出 / 导出记录）。

判据来源（方案 v2）：
* AC1.1——页状态投影可读、轻量、三态正确；
* AC1.4——失败页必须 409 知情确认；任何参数路径都塞不进失败页；越权一律 404；
* AC1.5——选页边界与错误码；
* AC1.7——导出记录从磁盘重建（重启后仍在）。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.api.jobs import export_item_pages, get_item_exports, get_item_page_status
from app.core.errors import AppError
from app.services import file_operations, partial_export_service
from app.services.export_service import ExportTask
from app.services.job_store import JobItemStatus, JobStore, JobType


def _make_job(store: JobStore, *, config: dict | None = None):
    job_id = store.create_job(
        job_type=JobType.TEXT_BATCH, title="t", config=config or {}, owner_id="admin"
    )
    item_id = store.add_item(job_id, "file-1")
    store.update_item_status(item_id, JobItemStatus.AWAITING_REVIEW)
    return job_id, item_id


def _six_pages():
    pages = {}
    for page in (1, 2, 3, 5):
        pages[str(page)] = {"status": "completed", "duration_ms": 100}
    pages["4"] = {"status": "failed", "error": "ocr boom"}
    return {
        "recognition": {
            "page_count": 6,
            "pages": pages,
            "failed_pages": {"4": "ocr boom"},
        }
    }


def _file_info(tmp_path: Path) -> dict:
    src = tmp_path / "卷.pdf"
    src.write_bytes(b"%PDF-1.4\n%fake\n")
    return {
        "file_id": "file-1",
        "file_type": "pdf_scanned",
        "page_count": 6,
        "original_filename": "卷.pdf",
        "file_path": str(src),
        "bounding_boxes": {
            1: [{"id": "b1", "x": 0.1, "y": 0.1, "width": 0.2, "height": 0.05, "type": "PERSON", "page": 1}],
            4: [{"id": "b4", "x": 0.1, "y": 0.2, "width": 0.2, "height": 0.05, "type": "PERSON", "page": 4}],
        },
    }


@pytest.fixture()
def store(tmp_path):
    return JobStore(str(tmp_path / "jobs.sqlite3"))


@pytest.fixture()
def wired(store, tmp_path, monkeypatch):
    job_id, item_id = _make_job(store)
    store.update_item_performance(item_id, _six_pages())
    info = _file_info(tmp_path)
    monkeypatch.setattr(file_operations, "get_file_info", lambda file_id: info if file_id == "file-1" else None)
    return job_id, item_id, info


class _FakeManager:
    """只记录 submit 的租约，不真跑渲染（渲染由 test_issue257_partial_export_task 覆盖）。"""

    def __init__(self, out_dir: str):
        self.calls: list[dict] = []
        self._tasks: dict[str, ExportTask] = {}
        self._out_dir = out_dir

    def submit(self, *, owner_id, kind, runner, title="", total_bytes=0, file_count=0, meta=None):
        task = ExportTask(
            export_id=f"exp{len(self.calls) + 1:04d}",
            owner_id=owner_id,
            kind=kind,
            out_dir=self._out_dir,
            title=title,
            total_bytes=total_bytes,
            file_count=file_count,
            created_at="2026-11-11T00:00:00+00:00",
            runner=runner,
            meta=dict(meta or {}),
        )
        self.calls.append({"meta": dict(meta or {}), "title": title, "kind": kind})
        self._tasks[task.export_id] = task
        return task


# ---------------------------------------------------------------------------
# AC1.1 页状态端点
# ---------------------------------------------------------------------------

def test_page_status_endpoint_projects_three_states(wired, store):
    job_id, item_id, _ = wired
    payload = asyncio.run(get_item_page_status(job_id, item_id, store=store, owner_id="admin"))

    assert payload["paged"] is True
    assert payload["page_count"] == 6
    assert payload["recognition_state"] == "done"
    states = {p["page"]: p["state"] for p in payload["pages"]}
    assert states == {
        1: "success",
        2: "success",
        3: "success",
        4: "failed",
        5: "success",
        6: "pending",
    }
    page4 = payload["pages"][3]
    assert page4["reason"] == "ocr boom"
    assert payload["failed_pages"] == [4]


def test_page_status_endpoint_404_for_foreign_owner(wired, store):
    job_id, item_id, _ = wired
    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(get_item_page_status(job_id, item_id, store=store, owner_id="intruder"))
    assert excinfo.value.status_code == 404


def test_page_status_endpoint_404_for_unknown_item(wired, store):
    job_id, _, _ = wired
    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(get_item_page_status(job_id, "no-such-item", store=store, owner_id="admin"))
    assert excinfo.value.status_code == 404


def test_page_status_endpoint_404_when_file_info_missing(store, tmp_path, monkeypatch):
    job_id, item_id = _make_job(store)
    store.update_item_performance(item_id, _six_pages())
    monkeypatch.setattr(file_operations, "get_file_info", lambda file_id: None)
    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(get_item_page_status(job_id, item_id, store=store, owner_id="admin"))
    assert excinfo.value.status_code == 404


# ---------------------------------------------------------------------------
# AC1.4 / AC1.5 部分导出端点
# ---------------------------------------------------------------------------

def test_partial_export_requires_ack_when_failed_page_in_span(wired, store, tmp_path, monkeypatch):
    job_id, item_id, _ = wired
    manager = _FakeManager(str(tmp_path / "exp"))
    monkeypatch.setattr(partial_export_service, "export_task_manager", manager)

    with pytest.raises(AppError) as excinfo:
        asyncio.run(
            export_item_pages(
                job_id,
                item_id,
                pages=[1, 2, 3, 5],
                acknowledge_failed_excluded=False,
                store=store,
                owner_id="admin",
            )
        )
    assert excinfo.value.status_code == 409
    detail = excinfo.value.detail
    assert excinfo.value.error_code == "failed_pages_ack_required"
    assert detail["failed_pages"] == [4]
    assert manager.calls == []


def test_partial_export_accepted_records_meta(wired, store, tmp_path, monkeypatch):
    job_id, item_id, _ = wired
    manager = _FakeManager(str(tmp_path / "exp"))
    monkeypatch.setattr(partial_export_service, "export_task_manager", manager)

    payload = asyncio.run(
        export_item_pages(
            job_id,
            item_id,
            pages=[5, 1, 3],
            acknowledge_failed_excluded=True,
            store=store,
            owner_id="admin",
        )
    )

    assert payload["export_id"] == "exp0001"
    assert payload["pages"] == [1, 3, 5]
    assert payload["excluded_failed_pages"] == [4]
    assert payload["status"] == "queued"
    assert payload["job_id"] == job_id
    assert payload["item_id"] == item_id
    assert manager.calls[0]["meta"]["pages"] == [1, 3, 5]
    assert manager.calls[0]["kind"] == "partial_redaction"


@pytest.mark.parametrize(
    ("pages", "code", "status"),
    [
        ([], "pages_empty", 400),
        ([0, 1], "pages_out_of_range", 400),
        ([1, 7], "pages_out_of_range", 400),
        ([1, 4], "failed_pages_not_allowed", 400),
        ([1, 6], "pages_not_recognized", 400),
    ],
)
def test_partial_export_rejects_invalid_selection(wired, store, tmp_path, monkeypatch, pages, code, status):
    job_id, item_id, _ = wired
    manager = _FakeManager(str(tmp_path / "exp"))
    monkeypatch.setattr(partial_export_service, "export_task_manager", manager)
    with pytest.raises(AppError) as excinfo:
        asyncio.run(
            export_item_pages(
                job_id,
                item_id,
                pages=pages,
                acknowledge_failed_excluded=True,
                store=store,
                owner_id="admin",
            )
        )
    assert excinfo.value.status_code == status
    assert excinfo.value.error_code == code
    assert manager.calls == []


def test_partial_export_rejects_non_paged_file_type(store, tmp_path, monkeypatch):
    job_id, item_id = _make_job(store)
    store.update_item_performance(item_id, _six_pages())
    info = dict(_file_info(tmp_path), file_type="docx")
    monkeypatch.setattr(file_operations, "get_file_info", lambda file_id: info)
    with pytest.raises(AppError) as excinfo:
        asyncio.run(
            export_item_pages(
                job_id,
                item_id,
                pages=[1],
                acknowledge_failed_excluded=False,
                store=store,
                owner_id="admin",
            )
        )
    assert excinfo.value.status_code == 400
    assert excinfo.value.error_code == "partial_export_unsupported_type"


def test_partial_export_404_when_file_info_missing(store, tmp_path, monkeypatch):
    job_id, item_id = _make_job(store)
    store.update_item_performance(item_id, _six_pages())
    monkeypatch.setattr(file_operations, "get_file_info", lambda file_id: None)
    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(
            export_item_pages(
                job_id,
                item_id,
                pages=[1],
                acknowledge_failed_excluded=False,
                store=store,
                owner_id="admin",
            )
        )
    assert excinfo.value.status_code == 404


def test_partial_export_404_for_foreign_owner(wired, store, tmp_path, monkeypatch):
    job_id, item_id, _ = wired
    manager = _FakeManager(str(tmp_path / "exp"))
    monkeypatch.setattr(partial_export_service, "export_task_manager", manager)
    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(
            export_item_pages(
                job_id,
                item_id,
                pages=[1],
                acknowledge_failed_excluded=False,
                store=store,
                owner_id="intruder",
            )
        )
    assert excinfo.value.status_code == 404


# ---------------------------------------------------------------------------
# AC1.7 导出记录（扫盘重建）
# ---------------------------------------------------------------------------

def _write_status(out_root: Path, export_id: str, payload: dict) -> None:
    target = out_root / "admin" / export_id
    target.mkdir(parents=True, exist_ok=True)
    (target / "export-status.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_exports_endpoint_reads_disk_records(wired, store, tmp_path, monkeypatch):
    job_id, item_id, _ = wired
    from app.core.config import settings

    out_root = tmp_path / "out"
    (out_root / "exports").mkdir(parents=True)
    monkeypatch.setattr(settings, "OUTPUT_DIR", str(out_root))
    monkeypatch.setattr(partial_export_service, "export_task_manager", _FakeManager(str(tmp_path / "exp")))

    _write_status(
        out_root / "exports",
        "expA",
        {
            "export_id": "expA",
            "kind": "partial_redaction",
            "title": "卷-部分导出(第1-3页)",
            "status": "completed",
            "job_id": job_id,
            "item_id": item_id,
            "pages": [1, 2, 3],
            "page_count": 6,
            "excluded_failed_pages": [],
            "snapshot_at": "2026-11-11T00:00:00+00:00",
            "created_at": "2026-11-11T00:00:00+00:00",
            "volumes": [{"name": "part001.zip", "size_bytes": 10, "file_count": 1}],
        },
    )
    _write_status(
        out_root / "exports",
        "expOther",
        {
            "export_id": "expOther",
            "kind": "partial_redaction",
            "status": "completed",
            "job_id": job_id,
            "item_id": "another-item",
            "created_at": "2026-11-11T00:01:00+00:00",
        },
    )

    payload = asyncio.run(
        get_item_exports(job_id, item_id, store=store, owner_id="admin")
    )
    assert [record["export_id"] for record in payload["exports"]] == ["expA"]
    assert payload["exports"][0]["pages"] == [1, 2, 3]
