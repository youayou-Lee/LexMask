"""子集预览产物的生命周期（门④ 验收发现：识别收尾不得清掉子集预览）。

场景（用户 2026-11-16 本地 86 页真卷实弹验收）：
1. 识别跑到 54/86 → 点「开始匿名化」→ 子集预览产物落盘，结果页 54 页可翻可下载；
2. 后台识别继续跑到 86/86 → item 迁到 ``AWAITING_REVIEW``；
3. **产物不得被清**——否则用户在结果页继续翻页/下载会拿到 400「子集预览尚未生成」
   （子集预览是本轮识别期的产物，识别收尾不是「旧产物失效」）。

反向判据（必须仍然清）：
* 重跑识别（``AWAITING_REVIEW`` → ``PENDING``）；
* 打码开始（``AWAITING_REVIEW`` → ``PROCESSING``，全卷成品即将覆盖）；
* 全卷打码成功（``COMPLETED``）；
* ``clear_file_output()`` 默认仍清 preview（清产/驳回/删除路径）。
"""

from __future__ import annotations

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
