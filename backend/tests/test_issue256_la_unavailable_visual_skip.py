"""Issue#256：队列路径 LA 不可用 → 跳过视觉识别段（不整卷记页失败）+ AppError str 可读。

事故形态（2026-11-10 本地栈 ff4b2f8，canary 10 页 22s 复现）：
- 队列两段式第二段（视觉特征识别 merge pass）逐页进 ``detect_vision``，每页触发
  LA 预检（Issue#217 fail-closed）→ 本地栈无 LA（``visual-features:8090`` 不可达）
  → 每页抛 ``AppError(visual_service_unavailable)`` → 整卷 149/10 页全部记入
  ``failed_pages``，前端按「全部页失败」呈现——而第一段 OCR+HaS 的框/实体实际全部产出。
- 页失败 reason 只有 ``视觉特征识别: AppError: ``（空串）：``AppError.__init__``
  从未调 ``super().__init__``，kwargs 构造（生产全部预检类抛点同款）时
  ``Exception.__new__`` 收不到位置参数 ⇒ ``args=()`` ⇒ ``str(exc)==""``，
  错误码/指引全丢，排障无法定位。

产品语义（用户 2026-11-04 PR#235 已裁定，本 Issue 落实到后端队列路径）：
**LA 不可用不再拦截识别**——提交侧/运行侧剔除视觉要素（空数组=显式不跑该段），
显式记录跳过（不静默丢功能）；OCR/NER blocker 语义不变；云端（LA 在场）零变化。

harness 复用 test_issue139（与 test_issue199 同一队列/桩形态）。
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from test_issue139_pipeline_asyncio_timeout import (
    _FakeStore,
    _item,
    _queue,
    _run,
    _vision_stubs,
)

import app.services.redaction_orchestrator as ro
from app.core.errors import AppError
from app.services.job_store import JobItemStatus

_VISION_CFG: dict[str, Any] = {"visual_feature_types": ["SIGNATURE"], "ocr_has_types": ["姓名"]}


def _boxes_result(page: int) -> dict[str, Any]:
    return {"boxes": [{"id": f"p{page}-1", "page": page, "type": "PERSON", "text": "张三"}]}


def _la_down_probe() -> None:
    """生产同款的 LA 预检失败：kwargs 构造的 AppError（str 为空的形态也在钉）。"""
    raise AppError(
        status_code=503,
        error_code="visual_service_unavailable",
        message="视觉检测服务（LocateAnything）未启动：本地请运行 ./start_local.sh --with-visual",
    )


def _la_calls() -> list[dict[str, Any]]:
    return []


def _recording_detect(calls: list[dict[str, Any]]):
    async def _detect(file_id: str, page: int, **kwargs: Any) -> dict[str, Any]:
        calls.append(
            {
                "page": page,
                "ocr": list(kwargs.get("ocr_has_types") or []),
                "visual": list(kwargs.get("visual_feature_types") or []),
                "merge": bool(kwargs.get("merge_existing")),
            }
        )
        return _boxes_result(page)

    return _detect


def _skip_record(store: _FakeStore) -> dict[str, Any] | None:
    """读**合并视图**（store.item.performance_json）。

    patch 列表读法会把「最后一个含该键的 patch」误当最终态——deep-merge 下
    空 dict 不清除旧键（U2 假绿的教训，真库已实证），判据必须看合并结果。
    """
    perf = json.loads(str(store.item.get("performance_json") or "{}"))
    rec = (perf.get("recognition") or {}).get("visual_detection_skipped")
    return rec if isinstance(rec, dict) and rec else None


def _failed_pages(store: _FakeStore) -> dict[str, str] | None:
    perf = json.loads(str(store.item.get("performance_json") or "{}"))
    rec = (perf.get("recognition") or {}).get("failed_pages")
    if isinstance(rec, dict):
        return {str(k): str(v) for k, v in rec.items()}
    return None


# ---------------------------------------------------------------------------
# A：AppError 的 str 必须携带 message（kwargs / 位置参数两形态）——页失败 reason 的根
# ---------------------------------------------------------------------------

def test_a1_apperror_kwargs_construction_str_carries_message() -> None:
    exc = AppError(status_code=503, error_code="visual_service_unavailable", message="视觉检测服务未启动")
    assert str(exc) == "视觉检测服务未启动"


def test_a2_apperror_positional_construction_str_carries_message() -> None:
    exc = AppError(400, "SOME_CODE", "有些消息")
    assert str(exc) == "有些消息"


# ---------------------------------------------------------------------------
# V1：两段式 + LA 不可用 → 第二段整段跳过，第一段成果照常交付，零页失败
# ---------------------------------------------------------------------------

def test_v1_la_down_two_stage_skips_visual_stage(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    _vision_stubs(monkeypatch, pages=2, detect=_recording_detect(calls))
    monkeypatch.setattr(ro, "ensure_visual_service_available", _la_down_probe)

    store = _FakeStore()
    store.job["config_json"] = json.dumps(_VISION_CFG)
    queue = _queue(store)

    async def _parse_ok(task: Any) -> None:
        return None

    monkeypatch.setattr(queue, "_parse_file", _parse_ok)
    _run(lambda: queue._run_recognition(_item()))

    # 只跑第一段（OCR+HaS）：每页一次、不带视觉要素、不带 merge_existing
    assert len(calls) == 2, f"stage-2 must be skipped entirely, got {calls}"
    assert all(c["merge"] is False for c in calls)
    assert all(c["visual"] == [] for c in calls)
    assert sorted(c["page"] for c in calls) == [1, 2]

    # 失败账为空、无失败摘要、条目照常到审阅态（非 FAILED）
    assert _failed_pages(store) == {}
    assert store.error_message_calls == []
    review_calls = [c for c in store.status_calls if c["status"] == JobItemStatus.AWAITING_REVIEW.value]
    assert review_calls, store.status_calls
    assert review_calls[-1]["error_message"] is None, review_calls[-1]
    assert not [c for c in store.status_calls if c["status"] == JobItemStatus.FAILED.value]

    # 跳过显式落账（不静默丢功能）
    skip = _skip_record(store)
    assert skip is not None
    assert skip.get("reason") == "la_unavailable"
    assert "LocateAnything" in str(skip.get("message") or "")


# ---------------------------------------------------------------------------
# V2：单页 + LA 不可用 → 剔除视觉要素后照常单段识别（同 PR#235 提交侧剔除语义）
# ---------------------------------------------------------------------------

def test_v2_la_down_single_page_strips_visual_types(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    _vision_stubs(monkeypatch, pages=1, detect=_recording_detect(calls))
    monkeypatch.setattr(ro, "ensure_visual_service_available", _la_down_probe)

    store = _FakeStore()
    store.job["config_json"] = json.dumps(_VISION_CFG)
    queue = _queue(store)

    async def _parse_ok(task: Any) -> None:
        return None

    monkeypatch.setattr(queue, "_parse_file", _parse_ok)
    _run(lambda: queue._run_recognition(_item()))

    assert len(calls) == 1
    assert calls[0]["visual"] == []
    assert calls[0]["merge"] is False
    assert _failed_pages(store) == {}
    assert _skip_record(store) is not None
    review_calls = [c for c in store.status_calls if c["status"] == JobItemStatus.AWAITING_REVIEW.value]
    assert review_calls, store.status_calls
    assert review_calls[-1]["error_message"] is None


# ---------------------------------------------------------------------------
# V3：LA 可用 → 两段式照旧（云端零变化的契约钉）
# ---------------------------------------------------------------------------

def test_v3_la_up_two_stage_runs_as_before(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    _vision_stubs(monkeypatch, pages=2, detect=_recording_detect(calls))
    # _vision_stubs 默认把 LA 预检桩成可用；此处显式再钉一次以自证
    async def _la_up() -> None:
        return None

    monkeypatch.setattr(ro, "ensure_visual_service_available", _la_up)

    store = _FakeStore()
    queue = _queue(store)
    _run(lambda: queue._run_vision(_item(), dict(_VISION_CFG)))

    # 两段式：第一段 2 页（无视觉）+ 第二段 2 页（merge_existing=True、带视觉要素）
    assert len(calls) == 4, f"expected two-stage 2+2 calls, got {calls}"
    merge_calls = [c for c in calls if c["merge"]]
    assert len(merge_calls) == 2
    assert all(c["visual"] == ["SIGNATURE"] for c in merge_calls)
    assert _failed_pages(store) == {}
    assert _skip_record(store) is None


# ---------------------------------------------------------------------------
# V4：预检自身异常（非 visual_service_unavailable）不得被吞——fail-closed 保持
# ---------------------------------------------------------------------------

def test_v4_unexpected_probe_error_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    _vision_stubs(monkeypatch, pages=2, detect=_recording_detect(calls))

    def _boom() -> None:
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(ro, "ensure_visual_service_available", _boom)

    store = _FakeStore()
    queue = _queue(store)
    with pytest.raises(RuntimeError, match="probe exploded"):
        _run(lambda: queue._run_vision(_item(), dict(_VISION_CFG)))
    assert calls == []


# ---------------------------------------------------------------------------
# V5：LA 恢复后重跑——陈旧跳过账必须清除（deep-merge 语义，None 写入才清得掉）
# ---------------------------------------------------------------------------

def test_v5_skip_record_cleared_on_recovered_rerun(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    _vision_stubs(monkeypatch, pages=1, detect=_recording_detect(calls))

    store = _FakeStore()
    queue = _queue(store)

    monkeypatch.setattr(ro, "ensure_visual_service_available", _la_down_probe)
    _run(lambda: queue._run_vision(_item(), dict(_VISION_CFG)))
    assert _skip_record(store) is not None, "第一次运行应落跳过账"

    async def _la_up() -> None:
        return None

    monkeypatch.setattr(ro, "ensure_visual_service_available", _la_up)
    _run(lambda: queue._run_vision(_item(), dict(_VISION_CFG)))
    assert _skip_record(store) is None, "LA 恢复后重跑必须清陈旧跳过账（合并视图）"
