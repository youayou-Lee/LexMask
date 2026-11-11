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

from app.services import job_metadata, page_status_service
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


def _realistic_pages() -> dict[int, dict]:
    """体积门夹具的真实构成（评审 r1 裁定重订 AC1.1 预算，Issue#279 Task 8 fix）。

    构成依据：完成页 = ``#165`` text_layer_doc（ran/coverage/fallback_reason_code 等
    卷级路由证据）+ ``ocr_has`` / ``visual_features`` 两管线状态（vision_service.py
    真实块形：ran/skipped/failed/region_count/error/duration_ms/stage_duration_ms）+
    ``duration_breakdown_ms``（数值按页循环抖动）——Issue#279 A4.1 的 detail 投影会把
    stage_ms / fallback_reason_code / coverage 透传进响应，旧 32KB 夹具（裸
    status+duration_ms）测的不是真实轮询体；timeout/failed 页按 task_queue_pipelines
    的真实终态记录形状（timeout 无 pipeline_status、failed 带 error_type 与阶段块）。
    回退方案：若门⑤轮询体感超预期 → detail.stage_ms 按需加载，另立 Issue。
    """
    pages: dict[int, dict] = {}
    for p in range(1, 148):
        if p == 133:
            pages[p] = {
                "status": "timeout",
                "error_type": "PageTimeout",
                "active_pages_at_end": 0,
            }
            continue
        if p == 134:
            pages[p] = {
                "page": p,
                "status": "failed",
                "error": "ValueError: ner boom",
                "error_type": "ValueError",
                "stage_duration_ms": {"ner": 12},
                "pipeline_status": {"ocr_has": {"failed": True, "error": "ValueError: ner boom"}},
            }
            continue
        pages[p] = {
            "page": p,
            "finished_at": "2026-11-12T00:00:00+00:00",
            "duration_ms": 100 + p,
            "status": "completed",
            "active_pages_at_end": 0,
            "duration_breakdown_ms": {
                "ocr_has": 800 + (p % 5) * 25,
                "visual_features": 350 + (p % 7) * 20,
            },
            "cache_status": {},
            "pipeline_status": {
                "text_layer_doc": {
                    "ran": False,
                    "failed": True,
                    "coverage": None,
                    "verdict": None,
                    "http_errors": 0,
                    "truncated_chunks": 0,
                    "fallback_reason": "no embedded text layer",
                    "fallback_reason_code": "no_text_layer",
                    "fallback_page": p,
                },
                "ocr_has": {
                    "ran": True,
                    "skipped": False,
                    "failed": False,
                    "region_count": 4 + p % 6,
                    "error": None,
                    "duration_ms": 800 + (p % 5) * 25,
                    "stage_duration_ms": {"parse": 120 + p % 30, "ner": 90 + p % 40},
                },
                "visual_features": {
                    "ran": True,
                    "skipped": False,
                    "failed": False,
                    "region_count": 1 + p % 3,
                    "error": None,
                    "duration_ms": 350 + (p % 7) * 20,
                    "stage_duration_ms": {"detect": 90 + p % 20},
                },
            },
            "warnings": [],
        }
    return pages


def test_response_size_stays_small_for_150_pages():
    """AC1.1 体积：150 页投影序列化后必须 < 64KB（评审 r1 裁定重订，不是恒真体积断言）。

    预算重订依据：夹具改为真实构成后（每页 duration_breakdown_ms + text_layer_doc /
    ocr_has / visual_features 完整 pipeline_status，见 ``_realistic_pages``），Issue#279
    A4.1 的页详情投影（detail.stage_ms / fallback_reason_code / coverage）使真实轮询体
    突破旧 32KB 预算（实测 41.7KB）；裁定接受该增长（页格详情可见性收益 > 轮询体翻倍，
    前端仅在页格可见时轮询）。回退方案：若门⑤轮询体感超预期 → detail.stage_ms 按需
    加载另立 Issue。
    """
    perf = _perf(_realistic_pages(), failed={"133": "page_timeout", "134": "ValueError: ner boom"})
    result = build_page_status(_item_row("processing", perf), _pdf_file())
    assert len(result["pages"]) == 150
    payload = json.dumps(result, ensure_ascii=False).encode("utf-8")
    assert len(payload) < 64 * 1024


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


def test_job_item_response_schema_keeps_page_fields() -> None:
    """响应模型必须保留三枚页级字段（门④ 云实测发现 extra="ignore" 会把它们吞掉）。"""
    from app.models.job_schemas import JobItemResponse

    payload = {
        "id": "item-1",
        "job_id": "job-1",
        "file_id": "file-1",
        "status": "awaiting_review",
        "created_at": "2026-11-11T00:00:00Z",
        "updated_at": "2026-11-11T00:00:00Z",
        "paged": True,
        "page_count": 149,
        "recognition_failed_pages": [133],
    }
    out = JobItemResponse.model_validate(payload).model_dump()
    assert out["paged"] is True
    assert out["page_count"] == 149
    assert out["recognition_failed_pages"] == [133]


def _diverged_item() -> dict:
    """页记录写 completed，但失败清单里仍记着该页（两账本分叉的形态）。"""
    return {
        "id": "item-diverged",
        "job_id": "job-1",
        "file_id": "file-1",
        "status": "awaiting_review",
        "performance": {
            "recognition": {
                "page_count": 3,
                "pages": {
                    "1": {"status": "completed", "duration_ms": 10},
                    "2": {"status": "completed", "duration_ms": 20},
                    "3": {"status": "completed", "duration_ms": 30},
                },
                "failed_pages": {"2": "page retry timeout after 2s"},
            }
        },
    }


def test_failed_pages_list_wins_over_completed_record():
    """失败清单是权威：记录写 completed 也必须是 ❌，否则页格全绿而导出被拒。"""
    status = page_status_service.build_page_status(
        _diverged_item(), {"file_type": "pdf", "page_count": 3}
    )
    cells = {cell["page"]: cell for cell in status["pages"]}
    assert cells[1]["state"] == "success"
    assert cells[2]["state"] == "failed"
    assert cells[2]["reason"] == "page retry timeout after 2s"
    assert cells[3]["state"] == "success"
    assert status["failed_pages"] == [2]


def test_recognized_pages_exclude_failed_ledger_pages():
    """识别成功页集合必须减掉失败清单，否则选页白名单会放进导出必拒的页。"""
    item = _diverged_item()
    assert page_status_service.recognized_page_numbers(item, 3) == {1, 3}
    assert page_status_service.recognized_page_count(item, 3) == 2
