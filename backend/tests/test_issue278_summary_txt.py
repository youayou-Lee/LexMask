"""Issue#278 · A2 说明.txt 开关 + build_summary_lines 段落 + 编码。

r2 F2：凡两次渲染/重建产物对比一律**页级像素哈希**（get_pixmap(dpi=72).samples 的
sha256）；文件字节级断言只用于 zip entry 集合差这一层。
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
from app.services.export_service import ExportTaskManager
from app.services.job_store import JobItemStatus, JobStore, JobType
from app.services.partial_export_service import (
    build_summary_lines,
    config_from_job_config,
    submit_item_export,
    write_summary_txt,
)

PAGE_W, PAGE_H = 595.0, 842.0


def _make_pdf(path: Path, pages: int = 6) -> str:
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


def _seed(store: JobStore, *, page_count: int = 6, failed: dict[int, str] | None = None,
          manual: dict[str, dict] | None = None) -> tuple[str, str]:
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


def _file_info(src: Path, *, page_count: int = 6) -> dict:
    return {
        "file_id": "file-1",
        "file_type": "pdf_scanned",
        "page_count": page_count,
        "original_filename": "测试卷.pdf",
        "file_path": str(src),
        "bounding_boxes": _boxes(page_count),
    }


def _page_pixel_sha(pdf_path: str) -> list[str]:
    shas = []
    with fitz.open(pdf_path) as doc:
        for page in doc:
            pix = page.get_pixmap(dpi=72)
            shas.append(hashlib.sha256(pix.samples).hexdigest())
    return shas


def _run(store, item_id, file_info, *, include_summary):
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
            kind="partial",
            pages=[1, 3, 5],
            include_summary=include_summary,
            manager=manager,
        )
        for _ in range(600):
            if task.status in {"completed", "failed"}:
                break
            await asyncio.sleep(0.02)
        return task

    task = asyncio.run(_flow())
    assert task.status == "completed", task.error
    return task


@pytest.fixture()
def export_env(tmp_path, monkeypatch):
    out_root = tmp_path / "out"
    out_root.mkdir()
    monkeypatch.setattr(settings, "OUTPUT_DIR", str(out_root))
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path))
    return out_root, tmp_path


def test_include_summary_toggle(export_env, tmp_path):
    out_root, tmp_path = export_env
    store = JobStore(str(tmp_path / "jobs.sqlite3"))
    job_id, item_id = _seed(store, page_count=6, failed={4: "boom"})
    src = Path(_make_pdf(tmp_path / "src.pdf", 6))
    info = _file_info(src)

    task_off = _run(store, item_id, info, include_summary=False)
    task_on = _run(store, item_id, info, include_summary=True)

    def _entries(task):
        with zipfile.ZipFile(Path(task.out_dir) / "part001.zip") as zf:
            return set(zf.namelist())

    names_off = _entries(task_off)
    names_on = _entries(task_on)
    assert "说明.txt" not in names_off
    assert "说明.txt" in names_on
    # zip entry 集合差恰为 {说明.txt}（字节级断言只在这一层成立，r2 F2）
    assert names_on - names_off == {"说明.txt"}
    assert names_off - names_on == set()

    # 页级像素哈希：开关不影响 PDF 本体（页数 + 逐页像素全等）
    def _pdf_pages(task):
        with zipfile.ZipFile(Path(task.out_dir) / "part001.zip") as zf:
            pdf_name = next(n for n in zf.namelist() if n.endswith(".pdf"))
            target = tmp_path / f"extracted-{task.export_id}.pdf"
            target.write_bytes(zf.read(pdf_name))
            return target

    pdf_off = _pdf_pages(task_off)
    pdf_on = _pdf_pages(task_on)
    with fitz.open(pdf_off) as a, fitz.open(pdf_on) as b:
        assert a.page_count == b.page_count == 3
    assert _page_pixel_sha(str(pdf_off)) == _page_pixel_sha(str(pdf_on))


def test_summary_txt_content_and_encoding(export_env, tmp_path):
    out_root, tmp_path = export_env
    store = JobStore(str(tmp_path / "jobs.sqlite3"))
    job_id, item_id = _seed(
        store,
        page_count=6,
        failed={4: "视觉识别 page timeout"},
        manual={"5": {"verified_at": "2026-11-15T03:00:00+00:00", "operator": "alice", "box_count": 2}},
    )
    src = Path(_make_pdf(tmp_path / "src.pdf", 6))
    info = _file_info(src)
    task = _run(store, item_id, info, include_summary=True)

    with zipfile.ZipFile(Path(task.out_dir) / "part001.zip") as zf:
        raw = zf.read("说明.txt")
    # UTF-8 无 BOM
    assert not raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8")
    assert "本次排除的失败页" in text
    assert "第 4 页" in text and "视觉识别 page timeout" in text
    assert "导出 ID" in text
    # 人工补框段逐页列举（#280 §2.5）
    assert "第 5 页为人工补框结果，未经自动识别校验" in text


def test_write_summary_txt_utf8_no_bom(tmp_path):
    out = tmp_path / "说明.txt"
    write_summary_txt(["【导出说明】", "共 3 页"], str(out))
    raw = out.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    assert raw.decode("utf-8").splitlines() == ["【导出说明】", "共 3 页"]


# ---------------------------------------------------------------------------
# build_summary_lines 段落
# ---------------------------------------------------------------------------

def _lines(**overrides):
    kwargs = dict(
        pages=[1, 3, 5],
        page_count=6,
        excluded=[(4, "boom")],
        snapshot_at="2026-11-15T00:00:00+00:00",
        generated_at="2026-11-15T00:01:00+00:00",
        export_id="0123456789abcdef",
        recognized_pages_at_export=5,
    )
    kwargs.update(overrides)
    return build_summary_lines(**kwargs)


def test_summary_lines_no_cover_plus_one():
    """删除说明页后：共 N 页就是包内页数，不得再出现「含说明页 1 页」语义。"""
    lines = _lines()
    text = "\n".join(lines)
    assert "共 3 页" in text
    assert "含说明页" not in text


def test_summary_lines_manual_pages_segment():
    lines = _lines(manual_pages=[5])
    text = "\n".join(lines)
    assert "第 5 页为人工补框结果，未经自动识别校验" in text


def test_summary_lines_verify_incomplete_segment_kept():
    lines = _lines(verify_incomplete_pages=[3])
    text = "\n".join(lines)
    assert "未完成对账页：3" in text
    assert "该页按文本层结果交付，建议复核" in text


def test_summary_lines_lists_pages_individually():
    text = "\n".join(_lines())
    assert "第 1、3、5 页" in text
    assert "第 1-5 页" not in text
    assert "0123456789abcdef" in text
