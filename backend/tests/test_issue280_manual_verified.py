# Copyright 2026 LexMask Contributors

"""Issue#280 · A1/A2：交付闸语义——只剩 manual_verified 放行、仍有 failed 阻断。

#199 红线不退让：``_is_ready_for_delivery`` 的谓词表达式不变（阻断输入仍是
``failed_pages``）；manual 页放行由**账本维护**承载（verify 物理移出 failed_pages /
revoke 写回）。本文件钉住报告面的三件事：

1. ``manual_verified_pages`` per-file 字段（manifest 契约的报告侧同源口径）；
2. 无失败页但有人工页 ⇒ ``error`` 用中性英文警示（同 ``_failed_pages_export_warning`` 风格）；
3. summary 新增 ``manual_verified_selected_pages`` 聚合键。
"""

from __future__ import annotations

import pytest

import app.services.job_management_service as jms
from app.core.config import settings
from app.services.job_store import JobItemStatus, JobStore, JobType


def _fake_info(fid: str) -> dict:
    return {
        "original_filename": f"{fid}.pdf",
        "file_type": "pdf_scanned",
        "file_size": 1234,
        "page_count": 10,
        "output_path": f"/outputs/{fid}.pdf",
        "entities": [
            {"text": "张三", "type": "PERSON", "page": 1, "start": 0, "end": 2,
             "source": "has", "confidence": 0.95},
        ],
        "bounding_boxes": [
            {"type": "PERSON", "text": "张三", "page": 1, "x": 0.1, "y": 0.2,
             "width": 0.3, "height": 0.05, "source": "ocr_has", "selected": True},
            {"type": "PERSON", "text": "", "page": 5, "x": 0.2, "y": 0.3,
             "width": 0.3, "height": 0.05, "source": "manual", "selected": True},
            {"type": "PERSON", "text": "", "page": 5, "x": 0.6, "y": 0.3,
             "width": 0.2, "height": 0.05, "source": "manual", "selected": True},
        ],
    }


@pytest.fixture()
def job_env(tmp_path, monkeypatch):
    # has_output 要求 output_path 是 OUTPUT_DIR 下真实存在的文件（_redacted_output_state）。
    out_root = tmp_path / "out"
    out_root.mkdir()
    monkeypatch.setattr(settings, "OUTPUT_DIR", str(out_root))
    output_path = out_root / "file_0001.pdf"
    output_path.write_bytes(b"%PDF-1.4 fake")
    store = JobStore(str(tmp_path / "jobs.sqlite3"))
    job_id = store.create_job(job_type=JobType.TEXT_BATCH, title="t", owner_id="tenant_a")
    item_id = store.add_item(job_id, "file_0001")
    store.update_item_status(item_id, JobItemStatus.PROCESSING)
    store.update_item_status(item_id, JobItemStatus.AWAITING_REVIEW)
    store.update_item_status(item_id, JobItemStatus.COMPLETED)
    infos = {"file_0001": _fake_info("file_0001")}
    infos["file_0001"]["output_path"] = str(output_path)
    monkeypatch.setattr(jms, "_safe_file_info", lambda fid: (infos.get(fid), None))
    return store, job_id, item_id


def _seed_performance(store, item_id, *, failed: dict, manual: dict | None) -> None:
    recognition: dict = {
        "page_count": 10,
        "pages": {
            "1": {"status": "completed"},
            **({str(p): {"status": "failed", "error": reason} for p, reason in failed.items()}),
            **({"5": {"status": "completed"}} if manual else {}),
        },
        "failed_pages": failed,
    }
    if manual is not None:
        recognition["manual_verified_pages"] = manual
    store.update_item_performance(item_id, {"recognition": recognition})


_LEDGER = {
    "5": {
        "verified_at": "2026-11-15T08:00:00+00:00",
        "operator": "apitester",
        "box_count": 2,
        "original_reason": "视觉识别 page timeout after 300s",
    }
}


def test_delivery_gate_passes_with_only_manual_pages(job_env):
    """A1：failed_pages 已清空（verify 移出）+ ledger 有人工页 ⇒ 交付闸放行。"""
    store, job_id, item_id = job_env
    _seed_performance(store, item_id, failed={}, manual=_LEDGER)

    report = jms.build_export_report(store, job_id)
    assert report["summary"]["ready_for_delivery"] is True
    entry = report["files"][0]
    assert entry["manual_verified_pages"] == [
        {"page": 5, "box_count": 2, "verified_at": "2026-11-15T08:00:00+00:00",
         "operator": "apitester"}
    ]
    assert "manually annotated" in entry["error"]
    assert "pages 5" in entry["error"]
    assert "NOT verified by the automated pipeline" in entry["error"]
    assert entry["ready_for_delivery"] is True
    # summary 聚合键
    assert report["summary"]["manual_verified_selected_pages"] == 1
    # 流式行同源
    rows = list(jms.iter_report_file_rows(store, job_id))
    assert rows[0]["manual_verified_pages"] == entry["manual_verified_pages"]
    assert "manually annotated" in rows[0]["error"]


def test_delivery_gate_blocks_with_unresolved_failed_page(job_env):
    """A2：仍有 failed 页未处理 ⇒ 409 语义（红线不退让），manual 页不能豁免。"""
    store, job_id, item_id = job_env
    _seed_performance(store, item_id, failed={"3": "ocr boom"}, manual=_LEDGER)

    report = jms.build_export_report(store, job_id)
    assert report["summary"]["ready_for_delivery"] is False
    entry = report["files"][0]
    assert entry["ready_for_delivery"] is False
    assert "recognition_failed_pages" in entry["blocking_reasons"]
    # 失败警示优先于人工标注提示（error 是失败页口径）
    assert "failed recognition" in entry["error"]
    assert report["summary"]["manual_verified_selected_pages"] == 1


def test_manual_pages_export_note_helpers(job_env):
    store, job_id, _item_id = job_env
    manual_pages = [
        {"page": 5, "box_count": 2, "verified_at": "2026-11-15T08:00:00+00:00",
         "operator": "apitester"},
        {"page": 7, "box_count": 1, "verified_at": "2026-11-15T08:05:00+00:00",
         "operator": "apitester"},
    ]
    note = jms._manual_pages_export_note(manual_pages)
    assert note.startswith("2 page(s) manually annotated after recognition failure")
    assert "pages 5, 7" in note
    assert jms._manual_pages_export_note([]) == ""
