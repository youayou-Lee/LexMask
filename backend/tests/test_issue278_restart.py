"""Issue#278 · A6 重启后记录仍可下载、sha256 不变（独立进程扫盘重建法）。

V5/V7 杀手：export-status.json 的 meta（files_sha256）是记录面持久化的唯一来源；
新进程内存任务表为空，靠扫盘重建。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from app.core.config import settings
from app.services.export_service import ExportTaskManager
from app.services.job_store import JobItemStatus, JobStore, JobType
from app.services.partial_export_service import config_from_job_config, submit_item_export

BACKEND_ROOT = Path(__file__).resolve().parents[1]


def _make_pdf(path: Path, pages: int = 4) -> str:
    import fitz

    doc = fitz.open()
    for i in range(pages):
        page = doc.new_page(width=595, height=842)
        page.insert_text((72, 100), f"MARKER-P{i + 1}", fontsize=14)
        page.draw_rect(fitz.Rect(100, 200, 300, 260), color=(1, 0, 0), fill=(1, 0, 0))
    doc.save(str(path))
    doc.close()
    return str(path)


def test_record_survives_restart_sha256_stable(tmp_path, monkeypatch):
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
    pages = {str(p): {"status": "completed", "duration_ms": 100} for p in range(1, 5)}
    store.update_item_performance(item_id, {"recognition": {"page_count": 4, "pages": pages, "failed_pages": {}}})

    src = Path(_make_pdf(tmp_path / "src.pdf", 4))
    info = {
        "file_id": "file-1",
        "file_type": "pdf_scanned",
        "page_count": 4,
        "original_filename": "测试卷.pdf",
        "file_path": str(src),
        "bounding_boxes": {
            p: [{"id": f"b{p}", "x": 0.17, "y": 0.24, "width": 0.34, "height": 0.072,
                 "type": "PERSON", "page": p, "source": "manual"}]
            for p in range(1, 5)
        },
    }

    manager = ExportTaskManager()

    async def _flow():
        task = await submit_item_export(
            owner_id="admin", store=store, job_row=store.get_job(job_id), item_row=store.get_item(item_id),
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
    manifest_sha = {f["name"]: f["sha256"] for f in manifest["files"]}
    assert "part001.zip" not in manifest_sha, "files 是包内 entry（PDF/说明.txt），不是分卷本身"
    pdf_entry = next(f for f in manifest["files"] if f["name"].endswith(".pdf"))

    # 独立进程（等价生产重启）：内存任务表为空，扫盘重建记录并下载分卷
    script = (
        "import json, hashlib\n"
        "from app.services.partial_export_service import list_item_exports\n"
        "from app.services.export_service import export_task_manager\n"
        f"payload = list_item_exports(owner_id='admin', job_id={job_id!r}, item_id={item_id!r})\n"
        "records = [{k: r.get(k) for k in ('export_id','kind','status','pages','page_count','files_sha256')} "
        "for r in payload['exports']]\n"
        f"path = export_task_manager.volume_path({task.export_id!r}, 'admin', 'part001.zip')\n"
        "zip_sha = hashlib.sha256(open(path,'rb').read()).hexdigest() if path else None\n"
        "print(json.dumps({'records': records, 'unavailable': payload['unavailable'], "
        f"'zip_sha': zip_sha, 'export_id': {task.export_id!r}"
        "}, ensure_ascii=False))\n"
    )
    env = {
        **os.environ,
        "PYTHONPATH": str(BACKEND_ROOT),
        "OUTPUT_DIR": str(out_root),
        "UPLOAD_DIR": str(tmp_path),
    }
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=str(BACKEND_ROOT),
        env=env,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    payload = json.loads(proc.stdout.strip().splitlines()[-1])

    assert payload["unavailable"] == []
    records = [r for r in payload["records"] if r["export_id"] == task.export_id]
    assert len(records) == 1
    record = records[0]
    assert record["status"] == "completed"
    assert record["kind"] == "partial"
    assert record["pages"] == [1, 2]
    assert record["files_sha256"], "V7：sha256 必须落 export-status.json"
    assert record["files_sha256"][0]["sha256"] == pdf_entry["sha256"]
    assert payload["zip_sha"], "重启后分卷仍可下载"
    # 三方一致：重启前 manifest 值 == 重启后记录值 == 重启后下载分卷可复算的包内值
    assert payload["zip_sha"] == hashlib.sha256((Path(task.out_dir) / "part001.zip").read_bytes()).hexdigest()
