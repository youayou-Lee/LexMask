"""子集预览产物的生命周期（门④ 验收发现：识别收尾不得清掉子集预览）。

场景（用户 2026-11-16 本地 86 页真卷实弹验收）：
1. 识别跑到 54/86 → 点「开始匿名化」→ 子集预览产物落盘，结果页 54 页可翻可下载；
2. 后台识别继续跑到 86/86 → item 迁到 ``AWAITING_REVIEW``；
3. **产物不得被清**——否则用户在结果页继续翻页/下载会拿到 400「子集预览尚未生成」
   （子集预览是本轮识别期的产物，识别收尾不是「旧产物失效」）。

反向判据（必须仍然清）：
* 重跑识别（``AWAITING_REVIEW`` → ``PENDING``）；
* 打码开始（``AWAITING_REVIEW`` → ``PROCESSING``，全卷成品即将覆盖）；
* 全卷打码成功（``COMPLETED``）——元数据面见
  ``test_issue325_subset_preview.py::test_full_redaction_clears_preview_fields``，
  本文件只补「连磁盘预览 PDF 一起删」这一层（门⑥ 建议 2）；
* ``clear_file_output()`` 默认仍清 preview（清产/驳回/删除路径）。

入口闸（门⑥ 必修）：item 已终态（``COMPLETED``/``REVIEW_APPROVED``/``REDACTING``/
``CANCELLED``）时 ``POST .../subset-preview`` 必 409——否则产出的预览没有任何迁移会再清它，
``preview=true`` 会长期返回一份陈旧子集。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.services import file_management_service as fms
from app.services.job_store import JobItemStatus, JobStore, JobType

PREVIEW_FIELDS = (
    "preview_output_path",
    "processed_pages",
    "page_map",
    "preview_entity_map",
    "preview_redacted_count",
    "preview_page_count",
    "preview_created_at",
)


def _seed(tmp_path: Path, monkeypatch, *, status: JobItemStatus):
    """落一份 file_store 记录 + 一个 item（初始状态由调用方指定）。

    顺序要紧：item 状态迁移本身会触发产物清理（这正是被测语义），所以 file_store 与
    预览文件必须在状态落定之后再写，否则种子阶段就把预览清掉了（假红）。
    """
    store = JobStore(str(tmp_path / "jobs.sqlite3"))
    job_id = store.create_job(
        job_type=JobType.TEXT_BATCH, title="t", config={}, owner_id="admin"
    )
    item_id = store.add_item(job_id, "file-1")
    if status is not JobItemStatus.PENDING:
        # PENDING → AWAITING_REVIEW / PROCESSING 都是合法迁移
        store.update_item_status(item_id, JobItemStatus.AWAITING_REVIEW)
        if status is not JobItemStatus.AWAITING_REVIEW:
            store.update_item_status(item_id, status)

    preview = tmp_path / "preview.pdf"
    preview.write_bytes(b"%PDF-1.4\n%preview\n")
    info = {
        "id": "file-1",
        "file_type": "pdf_scanned",
        "page_count": 86,
        "file_path": str(tmp_path / "src.pdf"),
        "preview_output_path": str(preview),
        "processed_pages": list(range(1, 55)),
        "page_map": {idx: idx for idx in range(1, 55)},
        "preview_entity_map": {"A": "已打码"},
        "preview_redacted_count": 727,
        "preview_page_count": 54,
        "preview_created_at": "2026-11-16T00:00:00Z",
    }

    class _Store(dict):
        def get(self, key):
            return dict.get(self, key)

        def set(self, key, value):
            self[key] = value

    fs = _Store({"file-1": info})
    monkeypatch.setattr(fms, "file_store", fs)
    monkeypatch.setattr(fms.settings, "OUTPUT_DIR", str(tmp_path))
    monkeypatch.setattr(fms.settings, "UPLOAD_DIR", str(tmp_path))
    return store, job_id, item_id, fs, preview


def test_recognition_done_keeps_subset_preview(tmp_path, monkeypatch):
    """PROCESSING → AWAITING_REVIEW（识别收尾）：预览产物与元数据必须原样存活。"""
    store, _job_id, item_id, fs, preview = _seed(tmp_path, monkeypatch, status=JobItemStatus.PROCESSING)

    store.update_item_status(item_id, JobItemStatus.AWAITING_REVIEW)

    info = fs["file-1"]
    assert info["preview_output_path"] is not None
    assert info["preview_page_count"] == 54
    assert len(info["page_map"]) == 54
    assert preview.is_file(), "预览 PDF 不得在识别收尾时被删"


def test_rerun_job_clears_subset_preview(tmp_path, monkeypatch):
    """重跑任务（submit_job 把未终态条目重置回 PENDING）：预览必须失效，防读到上一轮旧遮罩。"""
    store, job_id, item_id, fs, preview = _seed(
        tmp_path, monkeypatch, status=JobItemStatus.PROCESSING
    )

    store.submit_job(job_id)

    assert store.get_item(item_id)["status"] == JobItemStatus.PENDING.value
    info = fs["file-1"]
    assert "preview_output_path" not in info
    assert all(field not in info for field in PREVIEW_FIELDS)
    assert not preview.exists()


def test_redact_start_clears_subset_preview(tmp_path, monkeypatch):
    """AWAITING_REVIEW → PROCESSING（开始打码）：全卷成品即将覆盖，预览必须失效。"""
    store, _job_id, item_id, fs, preview = _seed(
        tmp_path, monkeypatch, status=JobItemStatus.AWAITING_REVIEW
    )

    store.update_item_status(item_id, JobItemStatus.PROCESSING)

    assert "preview_output_path" not in fs["file-1"]
    assert not preview.exists()


def test_clear_file_output_default_still_drops_preview(tmp_path, monkeypatch):
    """清产/驳回/删除路径（默认参数）仍必须连预览一起清——E 侧不回退。"""
    store, _job_id, _item_id, fs, preview = _seed(
        tmp_path, monkeypatch, status=JobItemStatus.AWAITING_REVIEW
    )

    assert fms.clear_file_output("file-1") is True

    assert all(field not in fs["file-1"] for field in PREVIEW_FIELDS)
    assert not preview.exists()


@pytest.mark.parametrize("include_preview", [True, False])
def test_clear_file_output_include_preview_switch(tmp_path, monkeypatch, include_preview):
    """include_preview=False：只清全卷产物，预览保留（识别收尾专用语义）。"""
    store, _job_id, _item_id, fs, preview = _seed(
        tmp_path, monkeypatch, status=JobItemStatus.AWAITING_REVIEW
    )

    fms.clear_file_output("file-1", include_preview=include_preview)

    if include_preview:
        assert "preview_output_path" not in fs["file-1"]
        assert not preview.exists()
    else:
        assert fs["file-1"]["preview_output_path"] is not None
        assert preview.is_file()


# ---------------------------------------------------------------------------
# 入口闸：终态不得再造子集预览（门⑥ 必修）
# ---------------------------------------------------------------------------


def _set_item_status_direct(store: JobStore, item_id: str, status: JobItemStatus) -> None:
    """直写状态（不经状态机）——本用例只关心入口闸读到的值，不关心迁移合法性。"""
    import sqlite3

    conn = sqlite3.connect(store._path)
    try:
        conn.execute("UPDATE job_items SET status = ? WHERE id = ?", (status.value, item_id))
        conn.commit()
    finally:
        conn.close()


@pytest.mark.parametrize(
    "status",
    [
        JobItemStatus.COMPLETED,
        JobItemStatus.REVIEW_APPROVED,
        JobItemStatus.REDACTING,
        JobItemStatus.CANCELLED,
    ],
)
def test_subset_preview_endpoint_rejects_terminal_item(tmp_path, monkeypatch, status):
    """终态 item 再造预览 ⇒ 409（产物永不清、preview=true 长期返回陈旧子集）。"""
    from fastapi.testclient import TestClient

    from app.api import jobs as jobs_api
    from app.core.auth import require_auth
    from app.main import app

    store, job_id, item_id, _fs, _preview = _seed(
        tmp_path, monkeypatch, status=JobItemStatus.PROCESSING
    )
    _set_item_status_direct(store, item_id, status)

    calls: list[dict] = []

    async def _fake(**kwargs):
        calls.append(kwargs)
        return {"file_id": "file-1", "processed_pages": [], "page_map": {}, "page_count": 0}

    monkeypatch.setattr(jobs_api.subset_preview_service, "create_subset_preview", _fake)
    app.dependency_overrides[require_auth] = lambda: "admin"
    app.dependency_overrides[jobs_api.get_job_store] = lambda: store
    try:
        with TestClient(app) as client:
            resp = client.post(
                f"/api/v1/jobs/{job_id}/items/{item_id}/subset-preview",
                headers={"Authorization": "Bearer test"},
                json={"pages": [1], "bounding_boxes": []},
            )
    finally:
        app.dependency_overrides.pop(require_auth, None)
        app.dependency_overrides.pop(jobs_api.get_job_store, None)

    assert resp.status_code == 409, resp.text
    assert resp.json()["error_code"] == "subset_preview_item_not_in_flight"
    assert calls == [], "闸门必须在触达 subset_preview_service 之前拒"


def test_subset_preview_endpoint_allows_in_flight_item(tmp_path, monkeypatch):
    """反向：识别在飞（PROCESSING）必须放行——否则把功能本身堵死。"""
    from fastapi.testclient import TestClient

    from app.api import jobs as jobs_api
    from app.core.auth import require_auth
    from app.main import app

    store, job_id, item_id, _fs, _preview = _seed(
        tmp_path, monkeypatch, status=JobItemStatus.PROCESSING
    )

    async def _fake(**kwargs):
        return {"file_id": "file-1", "processed_pages": [1], "page_map": {1: 1}, "page_count": 1}

    monkeypatch.setattr(jobs_api.subset_preview_service, "create_subset_preview", _fake)
    app.dependency_overrides[require_auth] = lambda: "admin"
    app.dependency_overrides[jobs_api.get_job_store] = lambda: store
    try:
        with TestClient(app) as client:
            resp = client.post(
                f"/api/v1/jobs/{job_id}/items/{item_id}/subset-preview",
                headers={"Authorization": "Bearer test"},
                json={"pages": [1], "bounding_boxes": []},
            )
    finally:
        app.dependency_overrides.pop(require_auth, None)
        app.dependency_overrides.pop(jobs_api.get_job_store, None)

    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# 门⑥ 建议 2：派生文件的磁盘善后（孤儿 PDF）
# ---------------------------------------------------------------------------


def test_full_redaction_removes_preview_file_from_disk(tmp_path, monkeypatch):
    """全卷打码成功：清元数据的同时必须删掉旧子集 PDF（OUTPUT_DIR 无清扫器）。"""
    from app.models.redaction_schemas import RedactionRequest
    from app.services import redaction_orchestrator as orch
    from app.services.partial_export_service import config_from_job_config

    _store, _job_id, _item_id, fs, preview = _seed(
        tmp_path, monkeypatch, status=JobItemStatus.AWAITING_REVIEW
    )
    store_dict = {
        "file-1": {
            **fs["file-1"],
            "is_scanned": True,
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
                "entity_map": {},
            }

    monkeypatch.setattr(orch, "_get_file_store", lambda: _Store())
    monkeypatch.setattr(orch, "_get_file_store_lock", lambda: asyncio.Lock())
    monkeypatch.setattr(orch, "Redactor", lambda: _Redactor())

    req = RedactionRequest(file_id="file-1", entities=[], bounding_boxes=[])
    req.config = config_from_job_config({"watermark_delivery": "keep", "image_redaction_method": "fill"})
    asyncio.run(orch.execute_redaction(req))

    assert store_dict["file-1"]["preview_output_path"] is None
    assert not preview.exists(), "全卷成品已覆盖 ⇒ 旧子集 PDF 不得留在盘上"


def test_delete_file_removes_subset_preview(tmp_path, monkeypatch):
    """删文件：预览 PDF 一并删（否则孤儿永久留存）。"""
    _store, _job_id, _item_id, fs, preview = _seed(
        tmp_path, monkeypatch, status=JobItemStatus.AWAITING_REVIEW
    )

    class _Store(dict):
        def get(self, key):
            return dict.get(self, key)

        def delete_items_for_file(self, _fid):
            return None

    monkeypatch.setattr(fms, "file_store", _Store(dict(fs)))
    monkeypatch.setattr(fms, "_get_job_store", lambda: _Store())

    snapshot = asyncio.run(fms.delete_file("file-1"))

    assert snapshot is not None
    assert not preview.exists(), "删文件后预览 PDF 不得留在 OUTPUT_DIR"


# ---------------------------------------------------------------------------
# 门⑥ 建议 3：启动期修复只看全卷产物，不得连带清预览
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_output_path", ["/etc/passwd", 12345])
def test_repair_output_records_keeps_preview(tmp_path, monkeypatch, bad_output_path):
    """output_path 不合法（越界路径 / 非字符串）时只清全卷字段，preview_* 原样保留。

    两个参数分别对应 ``repair_file_store_output_records`` 的两条修复分支——两条都不得
    顺带清掉子集预览（两者生命周期不同）。
    """
    _store, _job_id, _item_id, fs, preview = _seed(
        tmp_path, monkeypatch, status=JobItemStatus.AWAITING_REVIEW
    )
    info = dict(fs["file-1"])
    info["output_path"] = bad_output_path  # ⇒ 触发启动期修复
    info["output_file_id"] = "stale"

    class _Store(dict):
        def get(self, key):
            return dict.get(self, key)

        def set(self, key, value):
            self[key] = value

        def items(self):
            return {"file-1": info}.items()

    store_obj = _Store({"file-1": info})
    monkeypatch.setattr(fms, "file_store", store_obj)

    repaired = fms.repair_file_store_output_records()

    assert repaired == 1
    after = store_obj["file-1"]
    assert "output_path" not in after
    assert "output_file_id" not in after
    assert after["preview_output_path"] is not None
    assert preview.is_file()
