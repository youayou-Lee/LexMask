"""Issue#278 · A5 统一分支：full 与 partial 同一端点同一 manifest 结构 + full 两档物理断言。

r2 F1：无失败页卷 → 逐字节直取（sha256 == output_path）；有失败页卷 → 结构级切页
（失败页物理不进包，V9 杀手）。r2 F8：重启后 kind 断言（meta 不带 kind、task 顶层同源）。
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


def _seed(store: JobStore, *, page_count=6, failed=None, manual=None) -> tuple[str, str]:
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


def _file_info(src: Path, page_count=6, product: Path | None = None) -> dict:
    info = {
        "file_id": "file-1",
        "file_type": "pdf_scanned",
        "page_count": page_count,
        "original_filename": "测试卷.pdf",
        "file_path": str(src),
        "bounding_boxes": _boxes(page_count),
    }
    if product is not None:
        info["output_path"] = str(product)
    return info


@pytest.fixture()
def export_env(tmp_path, monkeypatch):
    out_root = tmp_path / "out"
    out_root.mkdir()
    monkeypatch.setattr(settings, "OUTPUT_DIR", str(out_root))
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path))
    return out_root, tmp_path


def _run_full(store, item_id, file_info):
    manager = ExportTaskManager()
    item_row = store.get_item(item_id)

    async def _flow():
        task = await submit_item_export(
            owner_id="admin", store=store, job_row=store.get_job(item_row["job_id"]),
            item_row=item_row, file_info=file_info,
            config=config_from_job_config({"watermark_delivery": "keep"}),
            kind="full", manager=manager,
        )
        for _ in range(600):
            if task.status in {"completed", "failed"}:
                break
            await asyncio.sleep(0.02)
        return task

    task = asyncio.run(_flow())
    assert task.status == "completed", task.error
    return task


def _run_partial(store, item_id, file_info, pages):
    manager = ExportTaskManager()
    item_row = store.get_item(item_id)

    async def _flow():
        task = await submit_item_export(
            owner_id="admin", store=store, job_row=store.get_job(item_row["job_id"]),
            item_row=item_row, file_info=file_info,
            config=config_from_job_config({"watermark_delivery": "keep"}),
            kind="partial", pages=pages, manager=manager,
        )
        for _ in range(600):
            if task.status in {"completed", "failed"}:
                break
            await asyncio.sleep(0.02)
        return task

    task = asyncio.run(_flow())
    assert task.status == "completed", task.error
    return task


def _manifest(task) -> dict:
    return json.loads((Path(task.out_dir) / "manifest.json").read_text(encoding="utf-8"))


def _extract_pdf(task, tmp_path: Path, tag: str) -> Path:
    with zipfile.ZipFile(Path(task.out_dir) / "part001.zip") as zf:
        pdf_name = next(n for n in zf.namelist() if n.endswith(".pdf"))
        target = tmp_path / f"extracted-{tag}.pdf"
        target.write_bytes(zf.read(pdf_name))
        return target


def test_full_no_failed_pages_is_byte_identical_copy(export_env, tmp_path):
    out_root, tmp_path = export_env
    store = JobStore(str(tmp_path / "jobs.sqlite3"))
    job_id, item_id = _seed(store, page_count=4)
    src = Path(_make_pdf(tmp_path / "src.pdf", 4))
    product = out_root / "product.pdf"
    product.write_bytes(src.read_bytes())
    info = _file_info(src, page_count=4, product=product)

    task = _run_full(store, item_id, info)
    pdf = _extract_pdf(task, tmp_path, "full-clean")
    assert hashlib.sha256(pdf.read_bytes()).hexdigest() == hashlib.sha256(product.read_bytes()).hexdigest()
    manifest = _manifest(task)
    assert manifest["kind"] == "full"
    assert manifest["pages"] == [1, 2, 3, 4]


def test_full_with_failed_pages_structurally_slices_failed_out(export_env, tmp_path):
    """V9 杀手：有失败页卷的 full 不能 copyfile 本体（失败页物理不进包）。"""
    out_root, tmp_path = export_env
    store = JobStore(str(tmp_path / "jobs.sqlite3"))
    job_id, item_id = _seed(store, page_count=6, failed={4: "boom"})
    src = Path(_make_pdf(tmp_path / "src.pdf", 6))
    product = out_root / "product.pdf"
    product.write_bytes(src.read_bytes())
    info = _file_info(src, page_count=6, product=product)

    task = _run_full(store, item_id, info)
    pdf = _extract_pdf(task, tmp_path, "full-failed")
    with fitz.open(pdf) as doc:
        assert doc.page_count == 5, "包页数 == len(exportable)"
        texts = "".join(p.get_text() for p in doc)
        assert "MARKER-P4" not in texts, "失败页物理不进包"
        for p in (1, 2, 3, 5, 6):
            assert f"MARKER-P{p}" in texts
    manifest = _manifest(task)
    assert 4 not in manifest["pages"]


def test_full_and_partial_same_branch_and_manifest_keys(export_env, tmp_path):
    out_root, tmp_path = export_env
    store = JobStore(str(tmp_path / "jobs.sqlite3"))
    job_id, item_id = _seed(
        store,
        page_count=6,
        failed={4: "boom"},
        manual={"5": {"verified_at": "2026-11-15T04:00:00+00:00", "operator": "alice", "box_count": 1}},
    )
    src = Path(_make_pdf(tmp_path / "src.pdf", 6))
    product = out_root / "product.pdf"
    product.write_bytes(src.read_bytes())
    info = _file_info(src, page_count=6, product=product)

    task_full = _run_full(store, item_id, info)
    exportable = [1, 2, 3, 5, 6]
    task_partial = _run_partial(store, item_id, info, exportable)

    m_full = _manifest(task_full)
    m_partial = _manifest(task_partial)
    assert set(m_full) == set(m_partial), f"键集合不一致: {set(m_full) ^ set(m_partial)}"
    assert m_full["kind"] == "full"
    assert m_partial["kind"] == "partial"
    # manual 页在 full 缺省集里且被标注
    assert 5 in m_full["pages"]
    assert m_full["manual_verified_pages"] == [
        {"page": 5, "box_count": 1, "verified_at": "2026-11-15T04:00:00+00:00", "operator": "alice"}
    ]
    assert m_full["pages"] == [1, 2, 3, 5, 6]


def test_unified_chain_single_endpoint_source_assertion():
    """源码断言：jobs.py 恰好一个端点调 submit_item_export（旧 partial-export 端点已删）。"""
    import inspect

    import app.api.jobs as jobs_module

    source = inspect.getsource(jobs_module)
    assert "partial-export" not in source, "旧端点必须删除"
    assert source.count("submit_item_export") == 1
    assert "/export\"" in source or "/export'" in source or '"/export"' in source


def test_restart_preserves_kind_via_public_keys(export_env, tmp_path):
    """r2 F8：task kind 顶层携带，重启扫盘重建后 kind 仍为 full/partial（meta 不带 kind）。"""
    out_root, tmp_path = export_env
    store = JobStore(str(tmp_path / "jobs.sqlite3"))
    job_id, item_id = _seed(store, page_count=4)
    src = Path(_make_pdf(tmp_path / "src.pdf", 4))
    product = out_root / "product.pdf"
    product.write_bytes(src.read_bytes())
    info = _file_info(src, page_count=4, product=product)

    task_full = _run_full(store, item_id, info)
    task_partial = _run_partial(store, item_id, info, [1, 2])

    fresh = ExportTaskManager()
    for task, expected in ((task_full, "full"), (task_partial, "partial")):
        resurrected = fresh.get(task.export_id, "admin")
        assert resurrected is not None
        assert resurrected.kind == expected
        assert "kind" not in (resurrected.meta or {}), "meta 不带 kind（无双写）"
    listed = fresh.get(task_full.export_id, "admin")
    assert listed.public()["kind"] == "full"


def test_export_task_manager_runs_runner_exactly_once():
    """manager 级直接判据（评审疑虑③）：submit 后 runner 恰执行一次、队列排干。

    历史缺陷（本支 A4 验证期实测暴露并修复）：_ensure_worker 预入队 + put_nowait
    双投 ⇒ 同一导出 runner 跑两遍（zip 双写、事件翻倍）。worker 循环终态闸是
    唯一守卫，此处给一条不绕 export 链的直接判据。
    """
    import asyncio

    from app.services.export_service import ExportTaskManager

    manager = ExportTaskManager()
    calls = {"n": 0}

    async def _runner(_task, _progress):
        calls["n"] += 1
        return [{"name": "part001.zip", "size_bytes": 1, "file_count": 1}]

    async def _flow():
        task = manager.submit(owner_id="admin", kind="partial", runner=_runner, title="t")
        for _ in range(600):
            if task.status in {"completed", "failed"}:
                break
            await asyncio.sleep(0.02)
        return task

    task = asyncio.run(_flow())
    assert task.status == "completed", task.error
    assert calls["n"] == 1, f"runner 被执行了 {calls['n']} 次（双重入队复发）"
    assert manager._queue.qsize() == 0, "队列排干后不得残留任务条目"
