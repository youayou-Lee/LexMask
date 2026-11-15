# Copyright 2026 LexMask Contributors

"""Issue#280 · E1：页状态投影第四态 ``manual_verified``。

判据（设计稿 §5-E1 / §2.3）：

* 四态判定矩阵：success / failed / manual_verified / pending；
* ``failed_pages`` 权威优先：页同时在 failed_pages 与 manual ledger（整卷重跑再失败）
  ⇒ 投影回 failed（红叉），manual 条目让位；
* manual 页详情携带 ``manual: {box_count, verified_at, operator, auto_completed}``；
* 卷级 ``manual_verified_pages`` 升序列表；
* ``exportable_page_numbers = recognized ∪ manual``（部分导出准入用）；
* ``recognized_page_numbers`` 口径不变（纯自动成功，不含 manual 页）。
"""

from __future__ import annotations

import json

from app.services.page_status_service import (
    build_page_status,
    exportable_page_numbers,
    recognized_page_numbers,
)


def _perf(pages: dict[int, dict], *, page_count: int = 10, failed: dict | None = None,
          manual: dict | None = None) -> dict:
    recognition: dict = {
        "page_count": page_count,
        "pages": {str(k): v for k, v in pages.items()},
        "failed_pages": failed if failed is not None else {},
    }
    if manual is not None:
        recognition["manual_verified_pages"] = manual
    return {"recognition": recognition}


def _item_row(status: str = "awaiting_review", performance: dict | None = None) -> dict:
    return {
        "id": "item-1",
        "job_id": "job-1",
        "file_id": "file-1",
        "status": status,
        "performance_json": json.dumps(performance or {}, ensure_ascii=False),
    }


def _pdf_file(page_count: int = 10) -> dict:
    return {"file_type": "pdf_scanned", "page_count": page_count, "is_scanned": True}


_LEDGER_ENTRY = {
    "verified_at": "2026-11-15T08:00:00+00:00",
    "operator": "apitester",
    "box_count": 3,
    "original_reason": "视觉识别 page timeout after 300s",
}


def test_projection_four_states():
    pages = {
        1: {"status": "completed"},
        2: {"status": "failed", "error": "boom"},
        4: {"status": "completed"},  # 自动成功重跑后的 manual 页（auto_completed=True）
        # 页 5：manual 页且页账无记录（verify 后未重跑）
        # 页 6：完全无记录 → pending
    }
    manual = {"5": dict(_LEDGER_ENTRY), "4": dict(_LEDGER_ENTRY, box_count=1)}
    perf = _perf(pages, failed={"2": "boom"}, manual=manual)
    result = build_page_status(_item_row(performance=perf), _pdf_file())

    by_page = {cell["page"]: cell for cell in result["pages"]}
    assert by_page[1]["state"] == "success"
    assert by_page[2]["state"] == "failed"
    assert by_page[4]["state"] == "manual_verified"
    assert by_page[5]["state"] == "manual_verified"
    assert by_page[6]["state"] == "pending"

    # 卷级 manual 清单：升序、字段齐全
    assert result["manual_verified_pages"] == [
        {"page": 4, "box_count": 1, "verified_at": "2026-11-15T08:00:00+00:00",
         "operator": "apitester"},
        {"page": 5, "box_count": 3, "verified_at": "2026-11-15T08:00:00+00:00",
         "operator": "apitester"},
    ]

    # manual 详情：operator / verified_at / box_count / auto_completed
    assert by_page[4]["manual"]["auto_completed"] is True
    assert by_page[5]["manual"]["auto_completed"] is False
    assert by_page[5]["manual"]["operator"] == "apitester"
    assert by_page[5]["manual"]["box_count"] == 3
    assert by_page[5]["manual"]["verified_at"] == "2026-11-15T08:00:00+00:00"
    # 非 manual 页不带 manual 键
    assert "manual" not in by_page[1]
    assert "manual" not in by_page[6]


def test_failed_pages_priority_over_manual_ledger():
    """整卷重跑再失败：页同时出现在 failed_pages 与 ledger ⇒ 投影 failed（红叉优先）。"""
    pages = {5: {"status": "failed", "error": "rerun boom"}}
    perf = _perf(pages, failed={"5": "rerun boom"}, manual={"5": dict(_LEDGER_ENTRY)})
    result = build_page_status(_item_row(performance=perf), _pdf_file())

    cell = result["pages"][4]
    assert cell["state"] == "failed"
    assert cell["reason"] == "rerun boom"
    # 卷级失败清单如实（#199 口径不动）
    assert result["failed_pages"] == [5]
    # ledger 条目仍在卷级清单里（投影不清理，用户可重验/撤销）
    assert result["manual_verified_pages"] == [
        {"page": 5, "box_count": 3, "verified_at": _LEDGER_ENTRY["verified_at"],
         "operator": "apitester"}
    ]


def test_exportable_pages_union_recognized_and_manual():
    pages = {1: {"status": "completed"}, 2: {"status": "completed"}}
    perf = _perf(pages, failed={}, manual={"5": dict(_LEDGER_ENTRY)})
    item = _item_row(performance=perf)

    # recognized 口径不变：纯自动成功，manual 页不在其中
    assert recognized_page_numbers(item, 10) == {1, 2}
    # exportable = recognized ∪ manual
    assert exportable_page_numbers(item, 10) == {1, 2, 5}


def test_manual_ledger_shape_anomaly_is_ignored():
    """ledger 形状异常（list / 脏键）按空处理，投影不崩。"""
    perf = _perf({1: {"status": "completed"}}, manual=["oops"])
    result = build_page_status(_item_row(performance=perf), _pdf_file())
    assert result["manual_verified_pages"] == []
    assert result["pages"][0]["state"] == "success"

    perf2 = _perf({1: {"status": "completed"}}, manual={"0": {"box_count": 1}, "x": {}})
    result2 = build_page_status(_item_row(performance=perf2), _pdf_file())
    assert result2["manual_verified_pages"] == []
