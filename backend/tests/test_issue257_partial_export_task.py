"""Issue#257 一期 · 部分导出任务全链（真 ExportTaskManager 跑一遍）。

覆盖方案 v2 的 AC1.5（产物结构/清单）与 AC1.7（记录落盘、重启后仍能列出并下载），
并钉住 AC1.3 的**主产物隔离**性质：导出过程不得改写 file_store 里的框/产物字段。
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
from app.services.partial_export_service import list_item_exports, submit_partial_export


def _make_pdf(path: Path, pages: int) -> Path:
    doc = fitz.open()
    for index in range(pages):
        page = doc.new_page(width=595, height=842)
        page.insert_text((72, 100), f"MARKER-P{index + 1}", fontsize=18)
        page.draw_rect(fitz.Rect(100, 200, 300, 260), color=(1, 0, 0), fill=(1, 0, 0))
    doc.save(path)
    doc.close()
    return path


def _file_info(src: Path) -> dict:
    boxes = {}
    for page in (1, 2, 3, 4, 5):
        boxes[page] = [
            {
                "id": f"b{page}",
                "x": 0.17,
                "y": 0.24,
                "width": 0.34,
                "height": 0.072,
                "type": "PERSON",
                "page": page,
                "source": "manual",
            }
        ]
    return {
        "file_id": "file-1",
        "file_type": "pdf_scanned",
        "page_count": 6,
        "original_filename": "测试卷.pdf",
        "file_path": str(src),
        "bounding_boxes": boxes,
    }


def _seed(store: JobStore) -> tuple[str, str]:
    job_id = store.create_job(
        job_type=JobType.TEXT_BATCH,
        title="t",
        config={"watermark_delivery": "keep"},
        owner_id="admin",
    )
    item_id = store.add_item(job_id, "file-1")
    store.update_item_status(item_id, JobItemStatus.AWAITING_REVIEW)
    pages = {str(p): {"status": "completed", "duration_ms": 120} for p in (1, 2, 3, 5)}
    pages["4"] = {"status": "failed", "error": "ocr boom"}
    store.update_item_performance(
        item_id,
        {
            "recognition": {
                "page_count": 6,
                "pages": pages,
                "failed_pages": {"4": "ocr boom"},
            }
        },
    )
    return job_id, item_id


@pytest.mark.asyncio
async def test_partial_export_task_end_to_end(tmp_path, monkeypatch):
    out_root = tmp_path / "out"
    out_root.mkdir()
    monkeypatch.setattr(settings, "OUTPUT_DIR", str(out_root))
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path))

    store = JobStore(str(tmp_path / "jobs.sqlite3"))
    job_id, item_id = _seed(store)
    src = _make_pdf(tmp_path / "src.pdf", 6)
    info = _file_info(src)
    before = json.dumps(info, sort_keys=True, ensure_ascii=False)

    manager = ExportTaskManager()
    from app.services.partial_export_service import config_from_job_config

    task = await submit_partial_export(
        owner_id="admin",
        item_row=store.get_item(item_id),
        file_info=info,
        config=config_from_job_config({"watermark_delivery": "keep"}),
        pages=[1, 2, 3, 5],
        excluded_failed_pages=[4],
        snapshot_at="2026-11-11T00:00:00+00:00",
        manager=manager,
    )

    for _ in range(300):
        if task.status in {"completed", "failed"}:
            break
        await asyncio.sleep(0.02)
    assert task.status == "completed", task.error

    sha_before = hashlib.sha256((Path(task.out_dir) / "part001.zip").read_bytes()).hexdigest()

    # 产物：一个分卷 zip，内含说明页 + 4 页
    volumes = task.public()["volumes"]
    assert [v["name"] for v in volumes] == ["part001.zip"]
    zip_path = Path(task.out_dir) / "part001.zip"
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        assert names[0] == "测试卷-部分导出(第1、2、3、5页).pdf"
        assert names[-1] == "manifest.json"
        payload = zf.read(names[0])
    pdf_path = tmp_path / "extracted.pdf"
    pdf_path.write_bytes(payload)
    with fitz.open(pdf_path) as doc:
        assert doc.page_count == 5
        cover = doc[0].get_text()
        assert "第 1、2、3、5 页" in cover
        assert "共 4 页" in cover
        assert "第 4 页" in cover and "ocr boom" in cover
        assert "2026-11-11T00:00:00+00:00" in cover
        assert "MARKER-P6" not in "".join(p.get_text() for p in doc)

    # 清单 + 状态落盘（重启后扫盘能重建）
    manifest = json.loads((Path(task.out_dir) / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["pages"] == [1, 2, 3, 5]
    assert manifest["excluded_failed_pages"] == [4]
    assert manifest["job_id"] == job_id
    status = json.loads((Path(task.out_dir) / "export-status.json").read_text(encoding="utf-8"))
    assert status["job_id"] == job_id
    assert status["item_id"] == item_id
    assert status["kind"] == "partial_redaction"

    # 模拟重启：全新 manager（内存表为空）仍能列出、查询状态、并按原字节下载
    fresh = ExportTaskManager()
    listed = list_item_exports(
        owner_id="admin", job_id=job_id, item_id=item_id, manager=fresh
    )
    assert [record["export_id"] for record in listed["exports"]] == [task.export_id]

    resurrected = fresh.get(task.export_id, "admin")
    assert resurrected is not None
    assert resurrected.status == "completed"
    assert resurrected.meta["pages"] == [1, 2, 3, 5]
    assert resurrected.meta["excluded_failed_pages"] == [4]

    download_path = fresh.volume_path(task.export_id, "admin", "part001.zip")
    assert download_path == str(zip_path)
    assert download_path and hashlib.sha256(Path(download_path).read_bytes()).hexdigest() == sha_before

    # 越权用户看不到别人的导出（目录隔离 + 归属校验）
    assert fresh.get(task.export_id, "intruder") is None
    assert fresh.volume_path(task.export_id, "intruder", "part001.zip") is None

    # 主产物隔离：file_store 里的框/字段一个字节都没动
    assert json.dumps(info, sort_keys=True, ensure_ascii=False) == before


def _status_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "OUTPUT_DIR", str(tmp_path))
    return tmp_path


def test_persisted_record_rejects_other_owner_with_colliding_dir(monkeypatch, tmp_path):
    """目录名归一后可能撞车（a@b 与 a_b），归属必须靠状态文件里的私有键判定。"""
    out_root = _status_dir(monkeypatch, tmp_path)
    from app.services.export_service import ExportTaskManager

    manager = ExportTaskManager()
    export_id = "deadbeef00000001"
    out_dir = out_root / "exports" / "_safe" / export_id
    out_dir.mkdir(parents=True)
    payload = {
        "export_id": export_id,
        "kind": "partial_redaction",
        "title": "t",
        "status": "completed",
        "error": None,
        "progress": {},
        "volumes": [{"name": "part001.zip", "bytes": 1, "file_count": 1}],
        "total_bytes": 1,
        "file_count": 1,
        "created_at": "2026-11-11T00:00:00+00:00",
        "finished_at": "2026-11-11T00:00:01+00:00",
        "_owner_id": "a@b",
    }
    (out_dir / "export-status.json").write_text(json.dumps(payload), encoding="utf-8")
    (out_dir / "part001.zip").write_bytes(b"zip")

    monkeypatch.setattr(
        "app.services.export_service._safe_owner_dir",
        lambda owner: str(out_root / "exports" / "_safe"),
    )

    assert manager.get(export_id, "a@b") is not None
    assert manager.get(export_id, "a_b") is None
    assert manager.volume_path(export_id, "a_b", "part001.zip") is None
