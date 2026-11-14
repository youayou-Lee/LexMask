"""Issue#276：投影层视觉列四态（``_visual_state_of`` / ``detail.visual`` / ``visual_summary``）。

钉住（设计稿 §2.5 / §3.1 真值表 / §6 变异 M3/M5/M6 感知断言）：

* 四态派生（done / skipped / failed / absent）× 旧数据 None（省键）；
* ``failed_reason`` 判定**先于** ``source=="la"``——``source="la"+reason=
  "visual_stage_error"`` 是真值表行 5 的正常形态（failed ⇒ ran=True ⇒ source 恒
  "la"），顺序翻转会把 failed 页吞成 done（M5 承重）；
* 零命中页 ``region_count==0`` 且实跑 ⇒ done（zero_hit=True），**不得**折算成
  skipped（M6 承重；「跑过且没找到」≠「根本没跑」）；
* **三态零改动**（#257 契约）：带 ``visual_failed_reason`` 的页必须投影
  success（M3 负向钉——把「有 reason 即判 failed」塞进 ``_page_state`` 的变异必须红）；
* ``visual_summary`` 五计数（done/skipped/failed/absent/pending_visual），无视觉
  证据 ⇒ None（旧数据兼容）。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.services.page_status_service import (
    _page_detail_of,
    _visual_state_of,
    build_page_status,
)


def _record(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "page": 1,
        "status": "completed",
        "duration_ms": 100,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# P1：_visual_state_of 四态 × 旧数据 None
# ---------------------------------------------------------------------------


def test_p1_old_data_and_missing_fields_yield_none() -> None:
    # 完全无字段（旧页记录 / 空记录）
    assert _visual_state_of({}) is None
    assert _visual_state_of(_record()) is None
    # 只有 source=None 且无 reason 的键（显式写 None 的脏数据按无字段处理）
    assert _visual_state_of({"visual_source": None}) is None


def test_p1_done_with_hits() -> None:
    out = _visual_state_of(_record(visual_source="la", visual_region_count=3))
    assert out == {"state": "done", "source": "la", "region_count": 3, "zero_hit": False}


def test_p1_done_zero_hit_is_not_skipped() -> None:
    """M6 承重：ran=True + region_count=0 = 完成零命中，严禁折成 skipped。"""
    out = _visual_state_of(_record(visual_source="la", visual_region_count=0))
    assert out["state"] == "done"
    assert out["zero_hit"] is True
    assert "failed_reason" not in out


def test_p1_skipped_la_unavailable() -> None:
    out = _visual_state_of(
        _record(visual_source="none", visual_failed_reason="la_unavailable")
    )
    assert out["state"] == "skipped"
    assert out["source"] == "none"
    assert out["failed_reason"] == "la_unavailable"


def test_p1_failed_stage_error_reason_wins_over_source_la() -> None:
    """M5 承重：source="la" + visual_stage_error 是真值表行 5 正常形态 ⇒ failed。

    派生顺序变异（先判 source=="la" 再判 reason）会把本态吞成 done——本用例红。
    """
    out = _visual_state_of(
        _record(visual_source="la", visual_region_count=2,
                visual_failed_reason="visual_stage_error")
    )
    assert out["state"] == "failed"
    assert out["failed_reason"] == "visual_stage_error"


def test_p1_absent_explicit_no_selection() -> None:
    out = _visual_state_of(_record(visual_source="none"))
    assert out == {"state": "absent", "source": "none"}


def test_p1_reason_only_legacy_shape_maps_to_failed() -> None:
    """只有 reason 没有 source 的极端形态（overlay 单独落账）也必须给 skipped。"""
    out = _visual_state_of(_record(visual_failed_reason="la_unavailable"))
    assert out["state"] == "skipped"


# ---------------------------------------------------------------------------
# P2：detail.visual 只在有视觉字段时出现（旧数据省键）
# ---------------------------------------------------------------------------


def test_p2_detail_visual_present_only_when_evidence() -> None:
    detail = _page_detail_of(
        _record(visual_source="la", visual_region_count=2), {}, 1
    )
    assert detail["visual"]["state"] == "done"
    assert detail["visual"]["region_count"] == 2

    old_detail = _page_detail_of(_record(), {}, 1)
    assert "visual" not in old_detail


# ---------------------------------------------------------------------------
# P3：build_page_status 卷级汇总 + 三态零改动（M3 负向钉）
# ---------------------------------------------------------------------------


def _item_row(pages: dict[str, dict[str, Any]], *, failed: dict[str, str] | None = None) -> dict[str, Any]:
    perf = {"recognition": {"page_count": 5, "pages": pages}}
    if failed is not None:
        perf["recognition"]["failed_pages"] = failed
    return {"id": "item-1", "job_id": "job-1", "file_id": "file-1", "status": "awaiting_review",
            "file_type": "image", "performance": perf}


def test_p3_summary_counts_and_three_state_untouched() -> None:
    pages = {
        "1": _record(visual_source="la", visual_region_count=2),
        "2": _record(visual_source="none", visual_failed_reason="la_unavailable"),
        "3": _record(visual_source="la", visual_region_count=1,
                     visual_failed_reason="visual_stage_error"),
        "4": _record(visual_source="none"),
        "5": _record(),  # 旧数据页（无视觉字段）
    }
    status = build_page_status(_item_row(pages))

    # 三态零改动：五个页全部 success（#257 契约；M3 变异在此红）
    assert [p["state"] for p in status["pages"]] == ["success"] * 5
    # detail.visual 四态齐全
    assert status["pages"][0]["detail"]["visual"]["state"] == "done"
    assert status["pages"][1]["detail"]["visual"]["state"] == "skipped"
    assert status["pages"][2]["detail"]["visual"]["state"] == "failed"
    assert status["pages"][3]["detail"]["visual"]["state"] == "absent"
    assert "visual" not in status["pages"][4]["detail"]
    # 卷级五计数
    assert status["visual_summary"] == {
        "done": 1, "skipped": 1, "failed": 1, "absent": 1, "pending_visual": 0,
    }


def test_p3_skipped_page_must_not_become_failed_by_reason() -> None:
    """M3 直杀形态：la_unavailable 页 = success 三态 + visual skipped（验收 2 双断言）。"""
    pages = {"1": _record(visual_source="none", visual_failed_reason="la_unavailable")}
    status = build_page_status(_item_row(pages, failed={}))
    assert status["pages"][0]["state"] == "success"
    assert status["failed_pages"] == []
    assert status["pages"][0]["detail"]["visual"]["state"] == "skipped"
    # 验收 1 判据：绿格必带 visual 维度——不存在 state=success 且 detail.visual 缺失的新数据页
    for page in status["pages"]:
        if page["state"] == "success":
            assert page["detail"]["visual"] is not None, page


def test_p3_failed_map_still_authoritative_over_visual_fields() -> None:
    """failed_pages 权威不动（#199/#257）：即便页记录带 visual 字段，清单说了算。"""
    pages = {"2": _record(visual_source="la", visual_region_count=1)}
    status = build_page_status(_item_row(pages, failed={"2": "boom"}))
    assert status["pages"][1]["state"] == "failed"


def test_p3_no_visual_evidence_yields_none_summary() -> None:
    pages = {"1": _record(), "2": _record(status=None, started_at="t")}  # 旧数据 + 起跑页
    status = build_page_status(_item_row(pages))
    assert status["visual_summary"] is None
    # 起跑页无终态记录也不给视觉字段
    assert "visual" not in status["pages"][1]["detail"]


def test_p3_pending_visual_counts_nonterminal_pages_when_evidence_exists() -> None:
    pages = {
        "1": _record(visual_source="none"),
        "2": _record(status=None, started_at="t"),  # 在跑：视觉列未到终态
    }
    status = build_page_status(_item_row(pages))
    assert status["visual_summary"]["absent"] == 1
    assert status["visual_summary"]["pending_visual"] == 1


# ---------------------------------------------------------------------------
# P4：recognized_page_numbers 选页白名单不受视觉字段影响（#257 契约）
# ---------------------------------------------------------------------------


def test_p4_recognized_pages_unchanged_by_visual_fields() -> None:
    from app.services.page_status_service import recognized_page_numbers

    pages = {
        "1": _record(visual_source="none", visual_failed_reason="la_unavailable"),
        "2": _record(visual_source="la", visual_region_count=0),
    }
    item = _item_row(pages, failed={})
    assert recognized_page_numbers(item, 2) == {1, 2}


@pytest.mark.parametrize(
    "record,expected",
    [
        ({"visual_source": "la", "visual_region_count": 1}, "done"),
        ({"visual_source": "none", "visual_failed_reason": "la_unavailable"}, "skipped"),
        ({"visual_source": "la", "visual_failed_reason": "visual_stage_error"}, "failed"),
        ({"visual_source": "none"}, "absent"),
    ],
)
def test_p5_state_matrix(record: dict[str, Any], expected: str) -> None:
    assert _visual_state_of(_record(**record))["state"] == expected
