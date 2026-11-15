"""Issue#278 · 统一 item 级导出端点链：A1（选 1、3、5）、A3（区间跨失败页）+ 负向准入。

判据来源：design-issue278.md §5-A1/A3 + §8 负向用例表（含 r2 F6 的 agent 件分支）。
"""

from __future__ import annotations

import asyncio
import json
import zipfile
from pathlib import Path

import fitz
import pytest

from app.core.config import settings
from app.services.export_service import ExportTaskManager
from app.services.job_store import JobItemStatus, JobStore, JobType
from app.services.partial_export_service import (
    PartialExportError,
    config_from_job_config,
    submit_item_export,
)

PAGE_W, PAGE_H = 595.0, 842.0


def _make_pdf(path: Path, pages: int = 10) -> str:
    doc = fitz.open()
    for i in range(pages):
        page = doc.new_page(width=PAGE_W, height=PAGE_H)
        page.insert_text((72, 100), f"MARKER-P{i + 1}", fontsize=14)
        page.draw_rect(fitz.Rect(100, 200, 300, 260), color=(1, 0, 0), fill=(1, 0, 0))
    doc.save(str(path))
    doc.close()
    return str(path)


def _boxes(page_count: int) -> dict[int, list[dict]]:
    return {
        p: [
            {
                "id": f"b{p}",
                "x": 0.17,
                "y": 0.24,
                "width": 0.34,
                "height": 0.072,
                "type": "PERSON",
                "page": p,
                "source": "manual",
            }
        ]
        for p in range(1, page_count + 1)
    }


def _seed(
    store: JobStore,
    *,
    page_count: int = 10,
    failed: dict[int, str] | None = None,
    manual: dict[str, dict] | None = None,
) -> tuple[str, str]:
    failed = failed or {}
    job_id = store.create_job(
        job_type=JobType.TEXT_BATCH, title="t", config={"watermark_delivery": "keep"}, owner_id="admin"
    )
    item_id = store.add_item(job_id, "file-1")
    store.update_item_status(item_id, JobItemStatus.AWAITING_REVIEW)
    pages: dict[str, dict] = {}
    for p in range(1, page_count + 1):
        pages[str(p)] = (
            {"status": "failed", "error": failed[p]} if p in failed else {"status": "completed", "duration_ms": 100}
        )
    recognition: dict = {"page_count": page_count, "pages": pages, "failed_pages": dict(failed)}
    if manual:
        recognition["manual_verified_pages"] = manual
    store.update_item_performance(item_id, {"recognition": recognition})
    return job_id, item_id


def _file_info(src: Path, *, page_count: int = 10, output_path: Path | None = None,
               file_type: str = "pdf_scanned", vl_md_meta: dict | None = None) -> dict:
    info = {
        "file_id": "file-1",
        "file_type": file_type,
        "page_count": page_count,
        "original_filename": "测试卷.pdf",
        "file_path": str(src),
        "bounding_boxes": _boxes(page_count),
    }
    if output_path is not None:
        info["output_path"] = str(output_path)
    if vl_md_meta is not None:
        info["vl_md_meta"] = vl_md_meta
    return info


@pytest.fixture()
def export_env(tmp_path, monkeypatch):
    out_root = tmp_path / "out"
    out_root.mkdir()
    monkeypatch.setattr(settings, "OUTPUT_DIR", str(out_root))
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path))
    return out_root, tmp_path


def _run(store, item_id, file_info, job_row=None, *, kind, pages=None, include_summary=False, expect="completed"):
    manager = ExportTaskManager()
    item_row = store.get_item(item_id)

    async def _flow():
        task = await submit_item_export(
            owner_id="admin",
            store=store,
            job_row=store.get_job(item_row["job_id"]),
            item_row=item_row,
            file_info=file_info,
            config=config_from_job_config({"watermark_delivery": "keep"}),
            kind=kind,
            pages=pages,
            include_summary=include_summary,
            manager=manager,
        )
        if expect == "error":
            return task
        for _ in range(600):
            if task.status in {"completed", "failed"}:
                break
            await asyncio.sleep(0.02)
        return task

    task = asyncio.run(_flow())
    if expect != "error":
        assert task.status == expect, task.error
    return task


def _zip_entries(task) -> list[str]:
    with zipfile.ZipFile(Path(task.out_dir) / "part001.zip") as zf:
        return zf.namelist()


# ---------------------------------------------------------------------------
# A1：选 1、3、5 → 恰 3 页、无说明页、manifest 对账
# ---------------------------------------------------------------------------

def test_partial_pages_exact_package(export_env, tmp_path):
    out_root, tmp_path = export_env
    store = JobStore(str(tmp_path / "jobs.sqlite3"))
    job_id, item_id = _seed(store, page_count=10, failed={4: "x"})
    src = Path(_make_pdf(tmp_path / "src.pdf", 10))
    info = _file_info(src)

    task = _run(store, item_id, info, kind="partial", pages=[1, 3, 5])
    names = _zip_entries(task)
    pdf_names = [n for n in names if n.endswith(".pdf")]
    assert len(pdf_names) == 1, names
    assert "说明.txt" not in names, "默认关：说明.txt 不存在"

    extracted = tmp_path / "extracted.pdf"
    with zipfile.ZipFile(Path(task.out_dir) / "part001.zip") as zf:
        extracted.write_bytes(zf.read(pdf_names[0]))
    with fitz.open(extracted) as doc:
        assert doc.page_count == 3, "无说明页，恰 3 页"
        texts = "".join(p.get_text() for p in doc)
        assert "MARKER-P2" not in texts and "MARKER-P4" not in texts

    manifest = json.loads((Path(task.out_dir) / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["pages"] == [1, 3, 5]
    assert manifest["page_count"] == 3
    assert manifest["kind"] == "partial"
    assert manifest["source_page_count"] == 10


# ---------------------------------------------------------------------------
# A3：区间跨失败页 → 自动排除 + 原因码
# ---------------------------------------------------------------------------

def test_failed_pages_excluded_with_reason(export_env, tmp_path):
    out_root, tmp_path = export_env
    store = JobStore(str(tmp_path / "jobs.sqlite3"))
    reason = "视觉识别 page timeout after 300s: HaS request deadline exceeded"
    job_id, item_id = _seed(store, page_count=10, failed={4: reason})
    src = Path(_make_pdf(tmp_path / "src.pdf", 10))
    info = _file_info(src)

    # 页格上失败页不可选（#257 谓词）⇒ 用户点 1-5 区间，实际选中集 = [1,2,3,5]；
    # 区间 1..5 跨失败页 4 ⇒ 自动排除并带原因码（与显式选失败页 400 并存）。
    task = _run(store, item_id, info, kind="partial", pages=[1, 2, 3, 5])
    pdf_names = [n for n in _zip_entries(task) if n.endswith(".pdf")]
    extracted = tmp_path / "extracted.pdf"
    with zipfile.ZipFile(Path(task.out_dir) / "part001.zip") as zf:
        extracted.write_bytes(zf.read(pdf_names[0]))
    with fitz.open(extracted) as doc:
        assert doc.page_count == 4, "失败页 4 物理不进包"
    manifest = json.loads((Path(task.out_dir) / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["pages"] == [1, 2, 3, 5]
    assert manifest["excluded_failed_pages"] == [{"page": 4, "reason_code": reason}]


# ---------------------------------------------------------------------------
# 负向准入
# ---------------------------------------------------------------------------

def test_partial_without_pages_is_400_pages_required(export_env, tmp_path):
    out_root, tmp_path = export_env
    store = JobStore(str(tmp_path / "jobs.sqlite3"))
    job_id, item_id = _seed(store)
    src = Path(_make_pdf(tmp_path / "src.pdf", 10))
    info = _file_info(src)
    with pytest.raises(PartialExportError) as excinfo:
        _run(store, item_id, info, kind="partial", pages=None, expect="error")
    assert excinfo.value.code == "pages_required_for_partial"


def test_full_empty_exportable_is_409_export_no_pages(export_env, tmp_path):
    out_root, tmp_path = export_env
    store = JobStore(str(tmp_path / "jobs.sqlite3"))
    job_id, item_id = _seed(store, page_count=3, failed={1: "a", 2: "b", 3: "c"})
    src = Path(_make_pdf(tmp_path / "src.pdf", 3))
    info = _file_info(src, page_count=3, output_path=src)
    with pytest.raises(PartialExportError) as excinfo:
        _run(store, item_id, info, kind="full", expect="error")
    assert excinfo.value.code == "export_no_pages"


def test_full_without_output_is_409_export_not_ready(export_env, tmp_path):
    out_root, tmp_path = export_env
    store = JobStore(str(tmp_path / "jobs.sqlite3"))
    job_id, item_id = _seed(store, page_count=3)
    src = Path(_make_pdf(tmp_path / "src.pdf", 3))
    info = _file_info(src, page_count=3)  # 无 output_path
    with pytest.raises(PartialExportError) as excinfo:
        _run(store, item_id, info, kind="full", expect="error")
    assert excinfo.value.code == "export_not_ready"


def test_agent_partial_still_type_gated(export_env, tmp_path):
    out_root, tmp_path = export_env
    store = JobStore(str(tmp_path / "jobs.sqlite3"))
    job_id, item_id = _seed(store)
    src = tmp_path / "src.md"
    src.write_text("# 卷\n", encoding="utf-8")
    info = _file_info(src, file_type="md")
    with pytest.raises(PartialExportError) as excinfo:
        _run(store, item_id, info, kind="partial", pages=[1], expect="error")
    assert excinfo.value.code == "partial_export_unsupported_type"


def test_agent_full_is_202_with_md_package_and_artifacts(export_env, tmp_path):
    """r2 F6：full 跳类型闸；agent 件 full → 202 且 pages=[]/page_count=0、
    附 mapping/retained 两份产物（§2.1 full 准入与包形态 + §4 #194/#196 行）。"""
    out_root, tmp_path = export_env
    store = JobStore(str(tmp_path / "jobs.sqlite3"))
    job_id, item_id = _seed(store, page_count=0)
    product = out_root / "测试卷-脱敏.md"
    product.write_text("# 脱敏稿\n", encoding="utf-8")
    mapping = out_root / "测试卷-映射表.csv"
    mapping.write_text("id,original\n", encoding="utf-8")
    retained = out_root / "测试卷-保留清单.csv"
    retained.write_text("id\n", encoding="utf-8")
    info = {
        "file_id": "file-1",
        "file_type": "md",
        "page_count": 0,
        "original_filename": "测试卷.md",
        "file_path": str(tmp_path / "src.md"),
        "output_path": str(product),
        "vl_md_meta": {"mapping_path": str(mapping), "retained_path": str(retained)},
    }
    (tmp_path / "src.md").write_text("# 原稿\n", encoding="utf-8")

    task = _run(store, item_id, info, kind="full")
    names = _zip_entries(task)
    assert "测试卷-脱敏.md" in names, names
    assert "测试卷-映射表.csv" in names
    assert "测试卷-保留清单.csv" in names
    manifest = json.loads((Path(task.out_dir) / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["kind"] == "full"
    assert manifest["pages"] == []
    assert manifest["page_count"] == 0
    assert manifest["source_page_count"] == 0
    assert manifest["route_summary"] == {"text_layer": 0, "ocr": 0, "accelerator": 0, "both": 0, "unknown": 0}
    assert {f["name"] for f in manifest["files"]} == {"测试卷-脱敏.md", "测试卷-映射表.csv", "测试卷-保留清单.csv"}


def test_agent_full_missing_artifact_recorded_in_skipped(export_env, tmp_path):
    """附产物缺失：记 manifest skipped，不回滚主成品（§4 #194/#196 行）。"""
    out_root, tmp_path = export_env
    store = JobStore(str(tmp_path / "jobs.sqlite3"))
    job_id, item_id = _seed(store, page_count=0)
    product = out_root / "卷-脱敏.md"
    product.write_text("# 脱敏稿\n", encoding="utf-8")
    info = {
        "file_id": "file-1",
        "file_type": "md",
        "page_count": 0,
        "original_filename": "卷.md",
        "file_path": str(tmp_path / "src.md"),
        "output_path": str(product),
        "vl_md_meta": {"mapping_path": str(out_root / "不存在.csv")},
    }
    (tmp_path / "src.md").write_text("# 原稿\n", encoding="utf-8")
    task = _run(store, item_id, info, kind="full")
    manifest = json.loads((Path(task.out_dir) / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["skipped"], "缺失的附产物须登记"
    assert any(item["reason"].startswith("missing_agent_artifact") for item in manifest["skipped"])
    names = _zip_entries(task)
    assert "卷-脱敏.md" in names
