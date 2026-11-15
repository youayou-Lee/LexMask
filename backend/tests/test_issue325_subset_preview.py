"""Issue#325：子集预览打码（不迁移 item 状态、独立产物、实体定位在切片卷上）。"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import fitz
import pytest

from app.core.config import settings
from app.models.entity_schemas import BoundingBox, Entity
from app.models.redaction_schemas import RedactionRequest
from app.services import partial_export_service
from app.services.job_store import JobItemStatus, JobStore, JobType
from app.services.partial_export_service import (
    PartialExportError,
    config_from_job_config,
    submit_item_export,
)
from app.services.subset_preview_service import create_subset_preview

PAGE_W, PAGE_H = 595.0, 842.0
RECT = (80.0, 120.0, 240.0, 190.0)


class _FileStore(dict):
    def update_fields(self, file_id: str, updates: dict) -> None:
        self.setdefault(file_id, {}).update(updates)


def _scanned_pdf(path: Path, pages: int = 3) -> str:
    doc = fitz.open()
    for i in range(pages):
        page = doc.new_page(width=PAGE_W, height=PAGE_H)
        page.insert_text((72, 80), f"PAGE-{i + 1}", fontsize=14)
        page.draw_rect(fitz.Rect(*RECT), color=(1, 0, 0), fill=(1, 0, 0))
    doc.save(str(path))
    doc.close()
    return str(path)


def _text_pdf(path: Path, pages: int = 2) -> str:
    doc = fitz.open()
    for _i in range(pages):
        page = doc.new_page(width=PAGE_W, height=PAGE_H)
        page.insert_text((80, 120), "ALICE", fontsize=14)
    doc.save(str(path))
    doc.close()
    return str(path)


def _box(page: int) -> BoundingBox:
    x0, y0, x1, y1 = RECT
    return BoundingBox(
        id=f"b{page}",
        x=x0 / PAGE_W,
        y=y0 / PAGE_H,
        width=(x1 - x0) / PAGE_W,
        height=(y1 - y0) / PAGE_H,
        page=page,
        type="PERSON",
        text=f"PAGE-{page}",
        selected=True,
    )


def _seed(tmp_path: Path, page_count: int) -> tuple[JobStore, str, str]:
    store = JobStore(str(tmp_path / "jobs.sqlite3"))
    job_id = store.create_job(
        job_type=JobType.TEXT_BATCH,
        title="t",
        config={"watermark_delivery": "keep", "image_redaction_method": "fill"},
        owner_id="admin",
    )
    item_id = store.add_item(job_id, "file-1")
    store.update_item_status(item_id, JobItemStatus.AWAITING_REVIEW)
    pages = {str(p): {"status": "completed", "duration_ms": 100} for p in range(1, page_count + 1)}
    store.update_item_performance(
        item_id, {"recognition": {"page_count": page_count, "pages": pages, "failed_pages": {}}}
    )
    return store, job_id, item_id


def _page_sha(path: str, page: int = 1) -> str:
    with fitz.open(path) as doc:
        return hashlib.sha256(doc[page - 1].get_pixmap(dpi=72).samples).hexdigest()


def _config():
    return config_from_job_config({"watermark_delivery": "keep", "image_redaction_method": "fill"})


def _call(env, store, job_id, item_id, file_info, *, pages, boxes=None, entities=None):
    return asyncio.run(
        create_subset_preview(
            file_store=env,
            store=store,
            job_row=store.get_job(job_id),
            item_row=store.get_item(item_id),
            file_info=file_info,
            config=_config(),
            pages=pages,
            bounding_boxes=boxes,
            entities=entities,
        )
    )


@pytest.fixture()
def env(tmp_path, monkeypatch):
    out = tmp_path / "out"
    out.mkdir()
    monkeypatch.setattr(settings, "OUTPUT_DIR", str(out))
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path))
    return _FileStore()


def _file_info(tmp_path: Path, path: str, page_count: int, file_type: str = "pdf_scanned") -> dict:
    return {
        "file_id": "file-1",
        "file_type": file_type,
        "is_scanned": file_type == "pdf_scanned",
        "page_count": page_count,
        "original_filename": "t.pdf",
        "file_path": path,
        "output_path": "",
        "bounding_boxes": {str(p): [_box(p).model_dump()] for p in range(1, page_count + 1)},
    }


def test_scanned_subset_preview_is_independent_and_maps_pages(env, tmp_path):
    store, job_id, item_id = _seed(tmp_path, 3)
    src = _scanned_pdf(tmp_path / "src.pdf", 3)
    info = _file_info(tmp_path, src, 3)
    env["file-1"] = info

    result = _call(env, store, job_id, item_id, info, pages=[1, 3])

    assert result["page_map"] == {1: 1, 2: 3}
    assert result["page_count"] == 2
    assert Path(result["preview_output_path"]).is_file()
    with fitz.open(result["preview_output_path"]) as doc:
        assert doc.page_count == 2
    stored = env["file-1"]
    assert stored["processed_pages"] == [1, 3]
    assert stored["preview_output_path"] == result["preview_output_path"]
    assert stored["output_path"] == "", "子集预览不得覆盖主产物"
    assert store.get_item(item_id)["status"] == JobItemStatus.AWAITING_REVIEW.value, "不得迁移 item 状态"


def test_text_pdf_entities_are_located_on_slice(env, tmp_path):
    store, job_id, item_id = _seed(tmp_path, 2)
    src = _text_pdf(tmp_path / "text.pdf", 2)
    info = _file_info(tmp_path, src, 2, file_type="pdf")
    info["bounding_boxes"] = {}
    env["file-1"] = info
    before = _page_sha(src, 1)
    ent = Entity(id="e1", text="ALICE", type="PERSON", start=0, end=5, page=1, selected=True)

    result = _call(env, store, job_id, item_id, info, pages=[1], entities=[ent])

    assert _page_sha(result["preview_output_path"], 1) != before, "文本型 PDF 的实体必须在切片卷上真实打码"


def test_empty_pages_rejected(env, tmp_path):
    store, job_id, item_id = _seed(tmp_path, 2)
    info = _file_info(tmp_path, _scanned_pdf(tmp_path / "s.pdf", 2), 2)
    env["file-1"] = info
    with pytest.raises(PartialExportError) as exc:
        _call(env, store, job_id, item_id, info, pages=[])
    assert exc.value.code == "subset_pages_empty"


def test_unknown_page_rejected(env, tmp_path):
    store, job_id, item_id = _seed(tmp_path, 2)
    info = _file_info(tmp_path, _scanned_pdf(tmp_path / "s.pdf", 2), 2)
    env["file-1"] = info
    with pytest.raises(PartialExportError) as exc:
        _call(env, store, job_id, item_id, info, pages=[3])
    assert exc.value.code == "subset_pages_out_of_range"


def test_page_not_exportable_rejected(env, tmp_path):
    store, job_id, item_id = _seed(tmp_path, 2)
    store.update_item_performance(item_id, {"recognition": {"page_count": 2, "pages": {"1": {"status": "completed"}}, "failed_pages": {"2": "x"}}})
    info = _file_info(tmp_path, _scanned_pdf(tmp_path / "s.pdf", 2), 2)
    env["file-1"] = info
    with pytest.raises(PartialExportError) as exc:
        _call(env, store, job_id, item_id, info, pages=[1, 2])
    assert exc.value.code == "subset_pages_not_ready"
    assert exc.value.detail["pages"] == [2]


def test_non_pdf_rejected(env, tmp_path):
    store, job_id, item_id = _seed(tmp_path, 1)
    info = _file_info(tmp_path, str(tmp_path / "a.txt"), 1, file_type="txt")
    env["file-1"] = info
    with pytest.raises(PartialExportError) as exc:
        _call(env, store, job_id, item_id, info, pages=[1])
    assert exc.value.code == "subset_unsupported_type"


def test_full_export_guard_rejects_subset_preview_as_whole(env, tmp_path):
    store, job_id, item_id = _seed(tmp_path, 3)
    src = _scanned_pdf(tmp_path / "src.pdf", 3)
    info = _file_info(tmp_path, src, 3)
    info.update({"preview_output_path": str(tmp_path / "preview.pdf"), "processed_pages": [1, 2]})
    with pytest.raises(PartialExportError) as exc:
        asyncio.run(
            submit_item_export(
                owner_id="admin",
                store=store,
                job_row=store.get_job(job_id),
                item_row=store.get_item(item_id),
                file_info=info,
                config=_config(),
                kind="full",
                pages=None,
            )
        )
    assert exc.value.code == "subset_preview_incomplete"


def test_subset_preview_route_forwards_pages_and_boxes(env, tmp_path, monkeypatch):
    """路由级接线判据：删体字段/删转发，本用例必须红。"""
    from fastapi.testclient import TestClient

    from app.api import jobs as jobs_api
    from app.core.auth import require_auth
    from app.main import app

    store, job_id, item_id = _seed(tmp_path, 2)
    info = _file_info(tmp_path, _scanned_pdf(tmp_path / "s.pdf", 2), 2)
    captured: dict = {}

    async def _fake(**kwargs):
        captured.update(kwargs)
        return {"file_id": "file-1", "processed_pages": [1], "page_map": {1: 1}, "page_count": 1}

    monkeypatch.setattr(jobs_api.subset_preview_service, "create_subset_preview", _fake)
    monkeypatch.setattr(jobs_api, "_load_file_info", lambda _fid: info)
    app.dependency_overrides[require_auth] = lambda: "admin"
    app.dependency_overrides[jobs_api.get_job_store] = lambda: store
    try:
        with TestClient(app) as client:
            resp = client.post(
                f"/api/v1/jobs/{job_id}/items/{item_id}/subset-preview",
                headers={"Authorization": "Bearer test"},
                json={"pages": [1], "bounding_boxes": [_box(1).model_dump()]},
            )
    finally:
        app.dependency_overrides.pop(require_auth, None)
        app.dependency_overrides.pop(jobs_api.get_job_store, None)

    assert resp.status_code == 200, resp.text
    assert captured["pages"] == [1]
    assert captured["bounding_boxes"] and captured["bounding_boxes"][0].page == 1


def test_preview_does_not_pollute_page_count_and_can_run_again(env, tmp_path):
    """C-2：预览不得把共享记录的 page_count 改成子集页数；二次预览必须可跑。"""
    store, job_id, item_id = _seed(tmp_path, 3)
    src = _scanned_pdf(tmp_path / "src2.pdf", 3)
    info = _file_info(tmp_path, src, 3)
    env["file-1"] = info
    first = _call(env, store, job_id, item_id, info, pages=[3])
    assert env["file-1"]["page_count"] == 3
    assert env["file-1"]["preview_page_count"] == 1
    second = _call(env, store, job_id, item_id, info, pages=[3])
    assert second["processed_pages"] == [3]
    assert first["preview_output_path"] != second["preview_output_path"]


def test_text_preview_replaces_original_text(env, tmp_path):
    """C-2/判据：文本型 PDF 预览页文本层必须不再含原文（像素差不作为证据）。"""
    store, job_id, item_id = _seed(tmp_path, 2)
    src = _text_pdf(tmp_path / "text.pdf", 2)
    info = _file_info(tmp_path, src, 2, file_type="pdf")
    info["bounding_boxes"] = {}
    info["entities"] = [
        {
            "id": "e1", "text": "ALICE", "type": "PERSON", "start": 0, "end": 5,
            "page": 2, "selected": True, "source": "manual",
        }
    ]
    env["file-1"] = info
    result = _call(env, store, job_id, item_id, info, pages=[2], boxes=[])
    assert result["redacted_count"] == 1
    with fitz.open(result["preview_output_path"]) as doc:
        assert "ALICE" not in doc[0].get_text()


def test_full_export_after_full_redaction_is_not_blocked(env, tmp_path):
    """C-3：已有主产物时，残留的预览字段不得再拦 full 导出。"""
    store, job_id, item_id = _seed(tmp_path, 3)
    src = _scanned_pdf(tmp_path / "src3.pdf", 3)
    out = _scanned_pdf(tmp_path / "out3.pdf", 3)
    info = _file_info(tmp_path, src, 3)
    info.update({
        "output_path": out,
        "preview_output_path": out,
        "processed_pages": [1],
        "page_map": {1: 1},
        "page_count": 3,
    })
    env["file-1"] = info
    from app.services import partial_export_service

    async def _run():
        try:
            await partial_export_service.submit_item_export(
                owner_id="admin",
                store=store,
                job_row=store.get_job(job_id),
                item_row=store.get_item(item_id),
                file_info=info,
                config=_config(),
                kind="full",
                pages=None,
                include_summary=False,
                bounding_boxes=None,
            )
        except partial_export_service.PartialExportError as exc:
            assert exc.code != "subset_preview_incomplete"

    asyncio.run(_run())


def test_zero_hit_on_selected_page_is_rejected(env, tmp_path):
    """B-2：所选页没有实际命中（实体在别的页）不得 200 静默返回。"""
    store, job_id, item_id = _seed(tmp_path, 2)
    src = _text_pdf(tmp_path / "t2.pdf", 2)
    info = _file_info(tmp_path, src, 2, file_type="pdf")
    info["bounding_boxes"] = {}
    info["entities"] = [
        {"id": "e1", "text": "ALICE", "type": "PERSON", "start": 0, "end": 5,
         "page": 2, "selected": True, "source": "manual"}
    ]
    env["file-1"] = info
    with pytest.raises(partial_export_service.PartialExportError) as exc:
        _call(env, store, job_id, item_id, info, pages=[1], boxes=[])
    assert exc.value.code == "subset_no_regions"


def test_full_redaction_clears_preview_fields(env, tmp_path, monkeypatch):
    """B-1/B-3：真跑 execute_redaction → info 顶层清 preview_*，history 条目不得混入。"""
    from app.services import redaction_orchestrator as orch

    fid = "file-1"
    store_dict = {
        fid: {
            "id": fid,
            "page_count": 3,
            "file_path": str(tmp_path / "src.pdf"),
            "file_type": "pdf_scanned",
            "is_scanned": True,
            "preview_output_path": str(tmp_path / "old-preview.pdf"),
            "processed_pages": [1],
            "page_map": {1: 1},
            "preview_entity_map": {"A": "已打码"},
            "preview_redacted_count": 1,
            "preview_page_count": 1,
            "redaction_history": [],
        }
    }

    class _Store(dict):
        def __init__(self):
            super().__init__(store_dict)

        def get(self, key):
            return store_dict.get(key)

        def set(self, key, value):
            store_dict[key] = value

    output = tmp_path / "out.pdf"
    output.write_bytes(b"%PDF-1.4\n")

    class _Redactor:
        async def redact(self, *_a, **_k):
            return {
                "output_path": str(output),
                "output_file_id": "o1",
                "redacted_count": 2,
                "entity_map": {"A": "已打码"},
            }

    monkeypatch.setattr(orch, "_get_file_store", lambda: _Store())
    monkeypatch.setattr(orch, "_get_file_store_lock", lambda: asyncio.Lock())
    monkeypatch.setattr(orch, "Redactor", lambda: _Redactor())

    req = RedactionRequest(file_id=fid, entities=[], bounding_boxes=[], config=_config())
    asyncio.run(orch.execute_redaction(req))

    info = store_dict[fid]
    assert info["preview_output_path"] is None
    assert info["processed_pages"] == []
    assert info["page_map"] == {}
    assert not any(str(k).startswith("preview_") for k in info["redaction_history"][-1])
