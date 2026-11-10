"""Issue#257 一期 · AC1.1：页状态投影（三态）与轻量列表字段。

判据来自门①.5 冷审处置后的方案 v2：

* ``timeout`` 与 ``failed`` 都必须映射成失败（真实形态里 #199 的超时页记的是
  ``status="timeout"``，只认 ``failed`` 会让红叉页变绿）；
* 无页记录的页 → ``pending``（未跑），不得默认成功；
* ``reason`` 以 ``failed_pages`` 清单为权威（页记录里的 ``error`` 只在清单缺失时回落，
  且仅失败页给）——历史脏数据里「重试成功页仍带旧 error」不能污染成功格；
* ``recognition_state`` 以 item 状态机为权威（页账不能判「完成」，AI 靠状态机）。
"""

from __future__ import annotations

import json

import pytest

from app.services import job_metadata
from app.services.job_metadata import item_to_out
from app.services.page_status_service import build_page_status


def _perf(pages: dict[int, dict], *, page_count: int = 150, failed: dict | None = None, **extra) -> dict:
    recognition = {
        "page_count": page_count,
        "pages": {str(k): v for k, v in pages.items()},
        "failed_pages": failed if failed is not None else {},
    }
    recognition.update(extra)
    return {"recognition": recognition}


def _item_row(status: str = "awaiting_review", performance: dict | None = None) -> dict:
    return {
        "id": "item-1",
        "job_id": "job-1",
        "file_id": "file-1",
        "status": status,
        "performance_json": json.dumps(performance or {}, ensure_ascii=False),
        "created_at": "2026-11-11T00:00:00+00:00",
        "updated_at": "2026-11-11T00:00:00+00:00",
    }


def _pdf_file(page_count: int = 150) -> dict:
    return {"file_type": "pdf_scanned", "page_count": page_count, "is_scanned": True}


def _mixed_pages() -> dict[int, dict]:
    pages = {p: {"status": "completed", "duration_ms": 100 + p} for p in range(1, 148)}
    pages[133] = {"status": "timeout", "active_pages_at_end": 0}
    pages[134] = {"status": "failed", "error": "ner boom", "stage_duration_ms": {"ner": 12}}
    pages.pop(148, None)
    pages.pop(149, None)
    pages.pop(150, None)
    return pages


# ---------------------------------------------------------------------------
# 服务层
# ---------------------------------------------------------------------------

def test_mixed_page_states_projection():
    perf = _perf(_mixed_pages(), failed={"133": "page_timeout", "134": "ner boom"})
    result = build_page_status(_item_row("processing", perf), _pdf_file())

    assert result["paged"] is True
    assert result["page_count"] == 150
    assert len(result["pages"]) == 150
    assert result["failed_pages"] == [133, 134]
    counts = {}
    for page in result["pages"]:
        counts[page["state"]] = counts.get(page["state"], 0) + 1
    # 1..147 有记录（133 timeout、134 failed ⇒ 成功 145），148/149/150 三条缺记录
    assert counts == {"success": 145, "failed": 2, "pending": 3}


def test_timeout_page_is_failed_and_uses_failed_pages_reason():
    perf = _perf(_mixed_pages(), failed={"133": "page_timeout", "134": "ner boom"})
    result = build_page_status(_item_row("processing", perf), _pdf_file())
    by_page = {p["page"]: p for p in result["pages"]}

    assert by_page[133]["state"] == "failed"
    assert by_page[133]["reason"] == "page_timeout"
    assert by_page[134]["state"] == "failed"
    # 清单优先：不取页记录里的 "ner boom"（本次恰好同值，故另造不一致夹具见下一例）
    assert by_page[148]["state"] == "pending"
    assert by_page[148]["reason"] is None
    assert by_page[1]["state"] == "success"
    assert by_page[1]["reason"] is None


def test_failed_pages_manifest_is_authoritative_over_page_error():
    """AC1.1 负判据：``reason`` 不得改读 ``pages.*.error``（重试成功页的脏 error）。"""
    perf = _perf(
        {1: {"status": "completed", "error": "stale boom"}},
        page_count=2,
        failed={},
    )
    result = build_page_status(_item_row("awaiting_review", perf), _pdf_file(2))
    by_page = {p["page"]: p for p in result["pages"]}
    assert by_page[1]["state"] == "success"
    assert by_page[1]["reason"] is None


def test_reason_falls_back_to_page_record_when_manifest_missing_entry():
    perf = _perf({2: {"status": "failed", "error": "ocr timeout"}}, page_count=2, failed={})
    result = build_page_status(_item_row("awaiting_review", perf), _pdf_file(2))
    by_page = {p["page"]: p for p in result["pages"]}
    assert by_page[2]["state"] == "failed"
    assert by_page[2]["reason"] == "ocr timeout"


def test_page_count_falls_back_to_file_info_then_zero():
    result = build_page_status(_item_row("processing", {}), _pdf_file(42))
    assert result["page_count"] == 42
    assert len(result["pages"]) == 42
    assert {p["state"] for p in result["pages"]} == {"pending"}

    result_no_hint = build_page_status(_item_row("processing", {}), {"file_type": "pdf_scanned"})
    assert result_no_hint["page_count"] == 0
    assert result_no_hint["pages"] == []


def test_non_paged_file_type_is_not_paged():
    for file_type in ("docx", "txt", "doc", "md", "agent_md"):
        result = build_page_status(_item_row("completed", _perf({}, page_count=0)), {"file_type": file_type})
        assert result["paged"] is False, file_type
        assert result["page_count"] == 0
        assert result["pages"] == []
    for file_type in ("pdf", "pdf_scanned", "image"):
        result = build_page_status(_item_row("completed", _perf({}, page_count=1)), {"file_type": file_type})
        assert result["paged"] is True, file_type


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("draft", "not_started"),
        ("pending", "not_started"),
        ("queued", "not_started"),
        ("processing", "running"),
        ("awaiting_review", "done"),
        ("completed", "done"),
        ("failed", "failed"),
        ("cancelled", "cancelled"),
    ],
)
def test_recognition_state_is_item_status_machine_driven(status, expected):
    result = build_page_status(_item_row(status, _perf({}, page_count=1)), _pdf_file(1))
    assert result["recognition_state"] == expected


def test_snapshot_at_is_iso_utc():
    from datetime import datetime

    result = build_page_status(_item_row("processing", _perf({}, page_count=1)), _pdf_file(1))
    parsed = datetime.fromisoformat(result["snapshot_at"])
    assert parsed.tzinfo is not None


def test_response_size_stays_small_for_150_pages():
    """AC1.1 体积：150 页投影序列化后必须 < 32KB（不是空响应的恒真体积断言）。"""
    perf = _perf(_mixed_pages(), failed={"133": "page_timeout", "134": "ner boom"})
    result = build_page_status(_item_row("processing", perf), _pdf_file())
    assert len(result["pages"]) == 150
    payload = json.dumps(result, ensure_ascii=False).encode("utf-8")
    assert len(payload) < 32 * 1024


# ---------------------------------------------------------------------------
# 轻量列表字段（job_metadata.item_to_out）
# ---------------------------------------------------------------------------

def test_item_to_out_carries_page_summary(monkeypatch):
    monkeypatch.setattr(
        job_metadata,
        "file_meta_for_item",
        lambda *a, **kw: {
            "filename": "a.pdf",
            "file_type": "pdf_scanned",
            "has_output": False,
            "entity_count": 0,
            "page_count": 150,
        },
    )
    perf = _perf(_mixed_pages(), failed={"133": "page_timeout", "134": "ner boom"})
    row = _item_row("awaiting_review", perf)
    out = item_to_out(row, owner_id="admin", job_id="job-1")
    assert out["paged"] is True
    assert out["page_count"] == 150
    assert out["recognition_failed_pages"] == [133, 134]


def test_item_to_out_non_paged_type(monkeypatch):
    monkeypatch.setattr(
        job_metadata,
        "file_meta_for_item",
        lambda *a, **kw: {
            "filename": "a.docx",
            "file_type": "docx",
            "has_output": False,
            "entity_count": 0,
        },
    )
    out = item_to_out(_item_row("awaiting_review", {}), owner_id="admin", job_id="job-1")
    assert out["paged"] is False
    assert out["page_count"] == 0
    assert out["recognition_failed_pages"] == []
