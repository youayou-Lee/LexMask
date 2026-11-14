"""Issue#278 · A4「改框即生效」：draft 草稿优先覆盖 file_store（V6 杀手）。

r2 F3：负向对照 = exportable 全集两轮导出（P1/P2 各记逐页像素哈希表）——
判据「改框页 ≠、其余页逐页 ==」只有全集两轮才成立（单页包里没有对照页）。
快照纪律：导出只读、不回写 file_store（AC1.3 不破）。
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
from app.services.partial_export_service import config_from_job_config, submit_item_export

PAGE_W, PAGE_H = 595.0, 842.0
BOX_RECT = (100.0, 200.0, 300.0, 260.0)


def _make_pdf(path: Path, pages: int = 4) -> str:
    doc = fitz.open()
    for i in range(pages):
        page = doc.new_page(width=PAGE_W, height=PAGE_H)
        page.insert_text((72, 100), f"MARKER-P{i + 1}", fontsize=14)
        page.draw_rect(fitz.Rect(*BOX_RECT), color=(1, 0, 0), fill=(1, 0, 0))
    doc.save(str(path))
    doc.close()
    return str(path)


def _box_dict(page: int, y: float = 200.0, box_id: str | None = None) -> dict:
    x0, y0, x1, y1 = BOX_RECT
    return {
        "id": box_id or f"b{page}",
        "x": x0 / PAGE_W,
        "y": (y - y0 + y0) / PAGE_H,
        "width": (x1 - x0) / PAGE_W,
        "height": (y1 - y0) / PAGE_H,
        "type": "PERSON",
        "page": page,
        "source": "manual",
    }


def _seed(store: JobStore) -> tuple[str, str]:
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
    pages = {str(p): {"status": "completed", "duration_ms": 100} for p in range(1, 5)}
    store.update_item_performance(item_id, {"recognition": {"page_count": 4, "pages": pages, "failed_pages": {}}})
    return job_id, item_id


def _page_pixel_sha(pdf_path: str) -> dict[int, str]:
    out = {}
    with fitz.open(pdf_path) as doc:
        for index, page in enumerate(doc):
            pix = page.get_pixmap(dpi=72)
            out[index + 1] = hashlib.sha256(pix.samples).hexdigest()
    return out


def _extract_pdf(task, tmp_path: Path, tag: str) -> Path:
    with zipfile.ZipFile(Path(task.out_dir) / "part001.zip") as zf:
        pdf_name = next(n for n in zf.namelist() if n.endswith(".pdf"))
        target = tmp_path / f"extracted-{tag}.pdf"
        target.write_bytes(zf.read(pdf_name))
        return target


@pytest.fixture()
def export_env(tmp_path, monkeypatch):
    out_root = tmp_path / "out"
    out_root.mkdir()
    monkeypatch.setattr(settings, "OUTPUT_DIR", str(out_root))
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path))
    return out_root, tmp_path


def test_reedit_changes_pixels_only_on_edited_page(export_env, tmp_path):
    out_root, tmp_path = export_env
    store = JobStore(str(tmp_path / "jobs.sqlite3"))
    job_id, item_id = _seed(store)
    src = Path(_make_pdf(tmp_path / "src.pdf", 4))
    file_info = {
        "file_id": "file-1",
        "file_type": "pdf_scanned",
        "page_count": 4,
        "original_filename": "测试卷.pdf",
        "file_path": str(src),
        # file_store 旧框：页 3 框在原位
        "bounding_boxes": {p: [_box_dict(p)] for p in range(1, 5)},
    }
    before = json.dumps(file_info, sort_keys=True, ensure_ascii=False)

    # ① 第一轮导出（exportable 全集）
    manager = ExportTaskManager()
    item_row = store.get_item(item_id)
    config = config_from_job_config(
        {
            "watermark_delivery": "keep",
            "image_redaction_method": "fill",
            "image_fill_color": "#000000",
            "image_redaction_strength": 75,
        }
    )
    async def _flow1():
        task = await submit_item_export(
            owner_id="admin", store=store, job_row=store.get_job(job_id), item_row=item_row,
            file_info=file_info, config=config, kind="partial", pages=[1, 2, 3, 4], manager=manager,
        )
        for _ in range(600):
            if task.status in {"completed", "failed"}:
                break
            await asyncio.sleep(0.02)
        return task

    task_p1 = asyncio.run(_flow1())
    assert task_p1.status == "completed", task_p1.error
    p1 = _extract_pdf(task_p1, tmp_path, "p1")
    sha_p1 = _page_pixel_sha(str(p1))

    # ② 审阅画布改页 3 的框（落 review_draft_json；file_store 不动）
    draft_boxes = [_box_dict(1), _box_dict(2), _box_dict(3, y=420.0, box_id="b3-edited"), _box_dict(4)]
    store.save_item_review_draft(
        item_id, {"entities": [], "bounding_boxes": draft_boxes}
    )

    # ③ 第二轮导出（同一全集）
    async def _flow2():
        task = await submit_item_export(
            owner_id="admin", store=store, job_row=store.get_job(job_id), item_row=item_row,
            file_info=file_info, config=config, kind="partial", pages=[1, 2, 3, 4], manager=manager,
        )
        for _ in range(600):
            if task.status in {"completed", "failed"}:
                break
            await asyncio.sleep(0.02)
        return task

    task_p2 = asyncio.run(_flow2())
    assert task_p2.status == "completed", task_p2.error
    p2 = _extract_pdf(task_p2, tmp_path, "p2")
    sha_p2 = _page_pixel_sha(str(p2))

    assert sha_p2[3] != sha_p1[3], "改框页（页 3）像素必须变化"
    for page in (1, 2, 4):
        assert sha_p2[page] == sha_p1[page], f"未改页（页 {page}）必须逐页不变"

    # 快照只读：file_store 框账一个字节没动（不被导出写回）
    assert json.dumps(file_info, sort_keys=True, ensure_ascii=False) == before
