"""Issue#322：选页直接导出必须包含当前手动拉框（请求框权威）。

判据：请求框是整份替换（非按页合并）——带上全页快照时，改过的页变化、
同框页逐页像素不变；快照缺页则 400 fail-closed 不回落 store；
full 导出忽略请求框（主产物直取）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import zipfile
from pathlib import Path

import fitz
import pytest

from app.core.config import settings
from app.models.entity_schemas import BoundingBox
from app.services.export_service import ExportTaskManager
from app.services.job_store import JobItemStatus, JobStore, JobType
from app.services.partial_export_service import (
    PartialExportError,
    config_from_job_config,
    submit_item_export,
)

PAGE_W, PAGE_H = 595.0, 842.0
RECT_A = (80.0, 120.0, 200.0, 180.0)
RECT_B = (80.0, 420.0, 200.0, 480.0)


def _make_pdf(path: Path, pages: int = 3) -> str:
    doc = fitz.open()
    for i in range(pages):
        page = doc.new_page(width=PAGE_W, height=PAGE_H)
        page.insert_text((72, 80), f"MARKER-P{i + 1}", fontsize=14)
        for rect in (RECT_A, RECT_B):
            page.draw_rect(fitz.Rect(*rect), color=(1, 0, 0), fill=(1, 0, 0))
    doc.save(str(path))
    doc.close()
    return str(path)


def _box(page: int, rect=RECT_A, box_id: str | None = None) -> dict:
    x0, y0, x1, y1 = rect
    return {
        "id": box_id or f"b{page}",
        "x": x0 / PAGE_W,
        "y": y0 / PAGE_H,
        "width": (x1 - x0) / PAGE_W,
        "height": (y1 - y0) / PAGE_H,
        "type": "PERSON",
        "page": page,
        "source": "manual",
        "selected": True,
    }


def _seed(store: JobStore, page_count: int = 3) -> tuple[str, str]:
    job_id = store.create_job(
        job_type=JobType.TEXT_BATCH,
        title="t",
        config={
            "watermark_delivery": "keep",
            "image_redaction_method": "fill",
            "image_fill_color": "#000000",
            "image_redaction_strength": 75,
        },
        owner_id="admin",
    )
    item_id = store.add_item(job_id, "file-1")
    store.update_item_status(item_id, JobItemStatus.AWAITING_REVIEW)
    pages = {str(p): {"status": "completed", "duration_ms": 100} for p in range(1, page_count + 1)}
    store.update_item_performance(
        item_id, {"recognition": {"page_count": page_count, "pages": pages, "failed_pages": {}}}
    )
    return job_id, item_id


def _page_pixel_sha(pdf_path: Path) -> dict[int, str]:
    out = {}
    with fitz.open(pdf_path) as doc:
        for index, page in enumerate(doc):
            out[index + 1] = hashlib.sha256(page.get_pixmap(dpi=72).samples).hexdigest()
    return out


def _extract_pdf(task, tmp_path: Path, tag: str) -> Path:
    with zipfile.ZipFile(Path(task.out_dir) / "part001.zip") as zf:
        name = next(n for n in zf.namelist() if n.endswith(".pdf"))
        target = tmp_path / f"extracted-{tag}.pdf"
        target.write_bytes(zf.read(name))
        return target


async def _run(store, job_id, item_id, file_info, config, *, kind, pages, manager, boxes=None):
    task = await submit_item_export(
        owner_id="admin",
        store=store,
        job_row=store.get_job(job_id),
        item_row=store.get_item(item_id),
        file_info=file_info,
        config=config,
        kind=kind,
        pages=pages,
        manager=manager,
        bounding_boxes=boxes,
    )
    for _ in range(800):
        if task.status in {"completed", "failed"}:
            break
        await asyncio.sleep(0.02)
    assert task.status == "completed", task.error
    return task


@pytest.fixture()
def env(tmp_path, monkeypatch):
    out_root = tmp_path / "out"
    out_root.mkdir()
    monkeypatch.setattr(settings, "OUTPUT_DIR", str(out_root))
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path))
    return tmp_path


def test_request_boxes_snapshot_replaces_whole_source_and_requires_all_pages(env, tmp_path):
    store = JobStore(str(tmp_path / "jobs.sqlite3"))
    job_id, item_id = _seed(store, 3)
    src = Path(_make_pdf(tmp_path / "src.pdf", 3))
    file_info = {
        "file_id": "file-1",
        "file_type": "pdf_scanned",
        "page_count": 3,
        "original_filename": "测试卷.pdf",
        "file_path": str(src),
        # store 旧框：每页都在 RECT_A（真实卷每页都有框）
        "bounding_boxes": {str(p): [_box(p)] for p in range(1, 4)},
    }
    before = json.dumps(file_info, sort_keys=True, ensure_ascii=False)
    manager = ExportTaskManager()
    config = config_from_job_config({"watermark_delivery": "keep", "image_redaction_method": "fill"})

    base = asyncio.run(
        _run(store, job_id, item_id, file_info, config, kind="partial", pages=[1, 2, 3], manager=manager)
    )
    sha_base = _page_pixel_sha(_extract_pdf(base, tmp_path, "base"))

    # 请求框：页 2 的 RECT_B + 页 3 的 RECT_A（对象形态，验证 BoundingBox 入参）
    request_boxes = [
        # 画布快照含每页全部框；页 1 与 store 同框 → 应逐字节不变
        BoundingBox.model_validate(_box(1, RECT_A, "req-1")),
        BoundingBox.model_validate(_box(2, RECT_B, "req-2")),
        BoundingBox.model_validate(_box(3, RECT_B, "req-3")),
    ]
    edited = asyncio.run(
        _run(
            store, job_id, item_id, file_info, config,
            kind="partial", pages=[1, 2, 3], manager=manager, boxes=request_boxes,
        )
    )
    sha_edited = _page_pixel_sha(_extract_pdf(edited, tmp_path, "edited"))

    assert sha_edited[2] != sha_base[2], "请求框所在页 2 必须变化（请求框优先生效）"
    assert sha_edited[3] != sha_base[3], "请求框所在页 3 必须变化（store 无该页框）"
    # 页 1：请求框把页 1 也带上了（画布全页快照），且与 store 同框 → 与 base 相同
    assert sha_edited[1] == sha_base[1], "请求框与 store 同框的页不得变化"

    # 快照只读：file_store 一个字节没动
    assert json.dumps(file_info, sort_keys=True, ensure_ascii=False) == before


def test_request_boxes_ignored_for_full_export(env, tmp_path):
    store = JobStore(str(tmp_path / "jobs.sqlite3"))
    job_id, item_id = _seed(store, 2)
    import shutil

    src = Path(_make_pdf(tmp_path / "src.pdf", 2))
    main = tmp_path / "out" / "main.pdf"
    shutil.copyfile(src, main)
    file_info = {
        "file_id": "file-1",
        "file_type": "pdf_scanned",
        "page_count": 2,
        "original_filename": "测试卷.pdf",
        "file_path": str(src),
        "output_path": str(main),  # full 直取主产物
        "bounding_boxes": {str(p): [_box(p)] for p in range(1, 3)},
    }
    manager = ExportTaskManager()
    config = config_from_job_config({"watermark_delivery": "keep", "image_redaction_method": "fill"})
    task = asyncio.run(
        _run(
            store, job_id, item_id, file_info, config,
            kind="full", pages=None, manager=manager,
            boxes=[BoundingBox.model_validate(_box(2, RECT_B, "req-2"))],
        )
    )
    got = _extract_pdf(task, tmp_path, "full")
    assert hashlib.sha256(got.read_bytes()).hexdigest() == hashlib.sha256(main.read_bytes()).hexdigest()


def test_request_boxes_missing_page_fails_closed(env, tmp_path):
    """请求框是整份替换：缺页不回落 file_store，而是 400 fail-closed（I-2 语义钉）。"""
    store = JobStore(str(tmp_path / "jobs.sqlite3"))
    job_id, item_id = _seed(store, 2)
    src = Path(_make_pdf(tmp_path / "src.pdf", 2))
    file_info = {
        "file_id": "file-1",
        "file_type": "pdf_scanned",
        "page_count": 2,
        "original_filename": "测试卷.pdf",
        "file_path": str(src),
        "bounding_boxes": {str(p): [_box(p)] for p in range(1, 3)},
    }
    manager = ExportTaskManager()
    config = config_from_job_config({"watermark_delivery": "keep", "image_redaction_method": "fill"})
    with pytest.raises(PartialExportError) as excinfo:
        asyncio.run(
            _run(
                store, job_id, item_id, file_info, config,
                kind="partial", pages=[1, 2], manager=manager,
                boxes=[BoundingBox.model_validate(_box(1))],
            )
        )
    assert excinfo.value.code == "pages_without_boxes"
    assert excinfo.value.detail["pages"] == [2]


def test_export_route_forwards_request_boxes(env, tmp_path, monkeypatch):
    """路由级接线判据（I-1）：删体字段/删转发，本用例必须红。"""
    from fastapi.testclient import TestClient

    from app.api import jobs as jobs_api
    from app.core.auth import require_auth
    from app.main import app

    store = JobStore(str(tmp_path / "jobs.sqlite3"))
    job_id, item_id = _seed(store, 2)
    src = Path(_make_pdf(tmp_path / "src.pdf", 2))
    file_info = {
        "file_id": "file-1",
        "file_type": "pdf_scanned",
        "page_count": 2,
        "original_filename": "测试卷.pdf",
        "file_path": str(src),
        "bounding_boxes": {str(p): [_box(p)] for p in range(1, 3)},
    }
    captured: dict = {}

    async def _fake_submit(**kwargs):
        captured.update(kwargs)

        class _Task:
            export_id = "exp-route-test"

            def public(self):
                return {"export_id": self.export_id}

        return _Task()

    monkeypatch.setattr(jobs_api.partial_export_service, "submit_item_export", _fake_submit)
    monkeypatch.setattr(jobs_api, "_load_file_info", lambda _fid: file_info)
    app.dependency_overrides[require_auth] = lambda: "admin"
    app.dependency_overrides[jobs_api.get_job_store] = lambda: store
    try:
        with TestClient(app) as client:
            resp = client.post(
                f"/api/v1/jobs/{job_id}/items/{item_id}/export",
                headers={"Authorization": "Bearer test"},  # CSRF middleware 跳过 Bearer
                json={
                    "kind": "partial",
                    "pages": [1],
                    "bounding_boxes": [_box(1), _box(2)],
                },
            )
    finally:
        app.dependency_overrides.pop(require_auth, None)
        app.dependency_overrides.pop(jobs_api.get_job_store, None)

    assert resp.status_code == 202, resp.text
    got = captured["bounding_boxes"]
    assert got is not None and len(got) == 2
    assert got[0].page == 1 and got[1].page == 2
