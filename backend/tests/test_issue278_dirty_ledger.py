"""Issue#278 · #280 账本脏形状不阻断导出（§4 #280 §2.0 行 + §8 dirty_ledger）。

账本 ``recognition.manual_verified_pages`` 出现 str/None/list/缺键/非 dict 条目等
脏形状时：manual 快照按空集处理、导出照常完成（fail-closed 只在 #280 侧，消费侧防御）。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import fitz
import pytest

from app.core.config import settings
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


@pytest.mark.parametrize(
    "dirty",
    [
        None,
        "not-a-dict",
        ["7", 8],
        {"7": "junk"},
        {"7": None},
        {"x": {"verified_at": "v"}},
    ],
)
def test_dirty_ledger_export_still_completes(tmp_path, monkeypatch, dirty):
    out_root = tmp_path / "out"
    out_root.mkdir()
    monkeypatch.setattr(settings, "OUTPUT_DIR", str(out_root))
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path))

    store = JobStore(str(tmp_path / "jobs.sqlite3"))
    job_id = store.create_job(
        job_type=JobType.TEXT_BATCH, title="t", config={"watermark_delivery": "keep"}, owner_id="admin"
    )
    item_id = store.add_item(job_id, "file-1")
    store.update_item_status(item_id, JobItemStatus.AWAITING_REVIEW)
    pages = {str(p): {"status": "completed", "duration_ms": 100} for p in range(1, 4)}
    recognition: dict = {
        "page_count": 3,
        "pages": pages,
        "failed_pages": {},
        "manual_verified_pages": dirty,
    }
    store.update_item_performance(item_id, {"recognition": recognition})

    doc = fitz.open()
    for i in range(3):
        page = doc.new_page(width=595, height=842)
        page.insert_text((72, 100), f"MARKER-P{i + 1}", fontsize=14)
    doc.save(str(tmp_path / "src.pdf"))
    doc.close()

    info = {
        "file_id": "file-1",
        "file_type": "pdf_scanned",
        "page_count": 3,
        "original_filename": "测试卷.pdf",
        "file_path": str(tmp_path / "src.pdf"),
        "bounding_boxes": {
            p: [{"id": f"b{p}", "x": 0.17, "y": 0.24, "width": 0.34, "height": 0.072,
                 "type": "PERSON", "page": p, "source": "manual"}]
            for p in range(1, 4)
        },
    }

    manager = ExportTaskManager()
    item_row = store.get_item(item_id)

    async def _flow():
        task = await submit_item_export(
            owner_id="admin", store=store, job_row=store.get_job(job_id), item_row=item_row,
            file_info=info, config=config_from_job_config({"watermark_delivery": "keep"}),
            kind="partial", pages=[1, 2], manager=manager,
        )
        for _ in range(600):
            if task.status in {"completed", "failed"}:
                break
            await asyncio.sleep(0.02)
        return task

    task = asyncio.run(_flow())
    assert task.status == "completed", task.error

    manifest = json.loads((Path(task.out_dir) / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["manual_verified_pages"] == []
    assert manifest["manual_verified_pages_count"] == 0
    assert manifest["pages"] == [1, 2]
