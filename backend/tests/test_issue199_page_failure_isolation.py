"""Issue#199：页级失败隔离——单页超时/失败不得作废整卷（用户 2026-10-27 明确要求）。

事故形态（2026-10-28 第四卷门④实测）：页 133 密集表格页单页耗尽 300s → run_page
TimeoutError 上抛 → run_page_stage 取消全部兄弟页 → 整 item failed、149 页成果清零。

设计（门①.5 冷审 r1 修订后）：
- **隔离口径 = 只捕 Exception 系**：`asyncio.CancelledError`/`KeyboardInterrupt`/
  `SystemExit` 是 BaseException 天然穿透（既有 t6 契约钉保持绿）；`MemoryError`
  显式 re-raise（OOM 下继续跑其余页放大故障）。
- **失败清单无条件写**（含空 dict）：`recognition.failed_pages` 深合并在重跑全成功
  后必须清空旧键，否则 reject→重跑留下假警示（冷审 I1）。
- **不新增状态档**（与底稿偏差，冷审已核可）：单文件流识别终态 = AWAITING_REVIEW；
  安全意图由失败清单 + 任务卡渲染 + 打码确认弹窗承载。
- **skip_review 守卫**（冷审 C1）：带失败页的条目不 honor skip_item_review，强制停
  审阅——封死「无人在环直通打码、失败页被当干净页出厂」的唯一路径。
- **导出报告**（冷审 C1b）：per-file 带失败页号（error_message 会活不过打码完成，
  结构化清单才是权威载体）。

harness 复用 test_issue139（同一队列/桩形态；该文件的 T2/T10a/T12 语义被本 Issue
反转后已原地重写）。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import pytest

import app.services.task_queue as tq
from app.core.config import settings
from app.services.job_store import JobItemStatus
from test_issue139_pipeline_asyncio_timeout import (
    _Fake310AsyncTimeoutError,
    _FakeStore,
    _item,
    _patch_asyncio_timeout,
    _queue,
    _run,
    _vision_stubs,
)

_VISION_CFG: dict[str, Any] = {"visual_feature_types": [], "ocr_has_types": ["姓名"]}


def _perf_patches(store: _FakeStore) -> list[dict[str, Any]]:
    return store.performance


def _failed_pages_of(store: _FakeStore) -> dict[str, str] | None:
    """深合并语义下读取最终 failed_pages（后面写的覆盖先写的）。"""
    merged: dict[str, Any] = {}
    for patch in _perf_patches(store):
        rec = patch.get("recognition") or {}
        if "failed_pages" in rec:
            merged = rec["failed_pages"]
    return merged if merged or merged == {} else None


def _boxes_result(page: int) -> dict[str, Any]:
    return {"boxes": [{"id": f"p{page}-1", "page": page, "type": "PERSON", "text": "张三"}]}


# ---------------------------------------------------------------------------
# U1：单页超时被隔离——其余页完成、不整卷作废、失败清单落账
# ---------------------------------------------------------------------------
def test_u1_page_timeout_isolated_others_complete(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_asyncio_timeout(monkeypatch)
    store = _FakeStore()
    queue = _queue(store)

    async def _detect(file_id: str, page: int, **kwargs: Any) -> dict[str, Any]:
        if page == 2:
            raise _Fake310AsyncTimeoutError()
        return _boxes_result(page)

    _vision_stubs(monkeypatch, pages=3, detect=_detect)

    async def scenario() -> None:
        await queue._run_vision(_item(), dict(_VISION_CFG))  # 不得上抛

    _run(scenario)
    statuses = store.page_statuses()
    assert statuses == {"1": "completed", "2": "timeout", "3": "completed"}, statuses
    failed = _failed_pages_of(store)
    assert failed is not None and list(failed) == ["2"], failed
    assert "timeout" in str(failed["2"]), failed


# ---------------------------------------------------------------------------
# U2：失败清单无条件写——重跑全成功后旧失败键被清空（冷审 I1）
# ---------------------------------------------------------------------------
def test_u2_failed_pages_cleared_on_clean_rerun(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_asyncio_timeout(monkeypatch)
    store = _FakeStore()
    queue = _queue(store)

    real_sleep = asyncio.sleep

    async def _flaky(file_id: str, page: int, **kwargs: Any):
        if page == 2 and not getattr(_flaky, "healed", False):
            _flaky.healed = True
            raise _Fake310AsyncTimeoutError()
        return _boxes_result(page)

    _vision_stubs(monkeypatch, pages=2, detect=_flaky)

    async def first() -> None:
        await queue._run_vision(_item(), dict(_VISION_CFG))

    _run(first)
    assert list(_failed_pages_of(store) or {}) == ["2"]

    async def _healthy(file_id: str, page: int, **kwargs: Any) -> dict[str, Any]:
        await real_sleep(0)
        return _boxes_result(page)

    _vision_stubs(monkeypatch, pages=2, detect=_healthy)

    async def second() -> None:
        await queue._run_vision(_item(), dict(_VISION_CFG))

    _run(second)
    assert _failed_pages_of(store) == {}, "重跑全成功必须无条件清空失败清单（冷审 I1）"


# ---------------------------------------------------------------------------
# U3/U4：吞咽边界——MemoryError 上抛；CancelledError 穿透（BaseException 不捕）
# ---------------------------------------------------------------------------
def test_u3_memory_error_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _FakeStore()
    queue = _queue(store)

    async def _detect(file_id: str, page: int, **kwargs: Any) -> dict[str, Any]:
        raise MemoryError("oom")

    _vision_stubs(monkeypatch, pages=2, detect=_detect)

    async def scenario() -> None:
        with pytest.raises(MemoryError):
            await queue._run_vision(_item(), dict(_VISION_CFG))

    _run(scenario)


def test_u4_cancelled_error_passes_through(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _FakeStore()
    queue = _queue(store)

    async def _detect(file_id: str, page: int, **kwargs: Any) -> dict[str, Any]:
        raise asyncio.CancelledError()

    _vision_stubs(monkeypatch, pages=2, detect=_detect)

    async def scenario() -> None:
        with pytest.raises(asyncio.CancelledError):
            await queue._run_vision(_item(), dict(_VISION_CFG))

    _run(scenario)


# ---------------------------------------------------------------------------
# U5/U6：两阶段调度——合并阶段跳过失败页；二阶段失败 reason 带 stage 标签（冷审 I3）
# ---------------------------------------------------------------------------
def _two_stage_cfg() -> dict[str, Any]:
    return {"visual_feature_types": ["SIGNATURE"], "ocr_has_types": ["姓名"]}


def test_u5_merge_stage_skips_failed_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_asyncio_timeout(monkeypatch)
    store = _FakeStore()
    queue = _queue(store)
    seen_merge: list[int] = []

    async def _detect(file_id: str, page: int, **kwargs: Any) -> dict[str, Any]:
        merge_existing = bool(kwargs.get("merge_existing"))
        if merge_existing:
            seen_merge.append(page)
            return _boxes_result(page)
        if page == 1:
            raise _Fake310AsyncTimeoutError()
        return _boxes_result(page)

    _vision_stubs(monkeypatch, pages=2, detect=_detect)

    async def scenario() -> None:
        await queue._run_vision(_item(), dict(_two_stage_cfg()))

    _run(scenario)
    assert seen_merge == [2], "合并阶段必须跳过一阶段失败页"
    assert store.page_entry(1).get("status") == "timeout", store.performance
    failed = _failed_pages_of(store)
    assert failed is not None and list(failed) == ["1"], failed


def test_u6_second_stage_failure_reason_carries_stage(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _FakeStore()
    queue = _queue(store)

    async def _detect(file_id: str, page: int, **kwargs: Any) -> dict[str, Any]:
        if bool(kwargs.get("merge_existing")) and page == 1:
            raise ValueError("merge boom")
        return _boxes_result(page)

    _vision_stubs(monkeypatch, pages=2, detect=_detect)

    async def scenario() -> None:
        await queue._run_vision(_item(), dict(_two_stage_cfg()))

    _run(scenario)
    failed = _failed_pages_of(store)
    assert failed is not None and list(failed) == ["1"], failed
    assert "视觉特征识别" in str(failed["1"]), failed
    assert store.page_entry(1).get("status") == "failed", store.performance
    assert store.page_entry(2).get("status") == "completed", store.performance


# ---------------------------------------------------------------------------
# U7：全失败卷——合并阶段空集直达收尾，不挂起（冷审 I3a）
# ---------------------------------------------------------------------------
def test_u7_all_pages_failed_still_terminates(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _FakeStore()
    queue = _queue(store)

    async def _detect(file_id: str, page: int, **kwargs: Any) -> dict[str, Any]:
        raise ValueError("service down")

    _vision_stubs(monkeypatch, pages=3, detect=_detect)

    async def scenario() -> None:
        await asyncio.wait_for(queue._run_vision(_item(), dict(_two_stage_cfg())), timeout=5)

    _run(scenario)
    assert store.page_statuses() == {"1": "failed", "2": "failed", "3": "failed"}
    assert list(_failed_pages_of(store) or {}) == ["1", "2", "3"]


# ---------------------------------------------------------------------------
# U8：条目流转——带失败页的单文件流仍到 AWAITING_REVIEW（非 FAILED）
# ---------------------------------------------------------------------------
def test_u8_item_reaches_awaiting_review_not_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _FakeStore()
    queue = _queue(store)

    async def _parse_ok(task: tq.TaskItem) -> None:
        return None

    monkeypatch.setattr(queue, "_parse_file", _parse_ok)

    async def _detect(file_id: str, page: int, **kwargs: Any) -> dict[str, Any]:
        if page == 2:
            raise ValueError("bad page")
        return _boxes_result(page)

    _vision_stubs(monkeypatch, pages=2, detect=_detect)

    async def scenario() -> None:
        await queue._run_recognition(_item())

    _run(scenario)
    review_calls = [c for c in store.status_calls if c["status"] == JobItemStatus.AWAITING_REVIEW.value]
    assert review_calls, store.status_calls
    failed_calls = [c for c in store.status_calls if c["status"] == JobItemStatus.FAILED.value]
    assert not failed_calls, failed_calls
    errors = [c["error_message"] for c in store.error_message_calls]
    assert errors and "2" in str(errors), errors
    # AC2-r2 实测回归钉：AWAITING_REVIEW 迁移不得洗掉失败摘要
    # （update_item_status 用传入值整列覆盖，须显式带上既有 error_message）
    assert review_calls[-1]["error_message"] and "2" in str(
        review_calls[-1]["error_message"]
    ), review_calls[-1]


# ---------------------------------------------------------------------------
# U9：成功路径回归钉——performance 唯一新增键 = recognition.failed_pages == {}（冷审 I1 口径）
# ---------------------------------------------------------------------------
def test_u9_success_path_only_addition_is_empty_failed_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _FakeStore()
    queue = _queue(store)

    async def _detect(file_id: str, page: int, **kwargs: Any) -> dict[str, Any]:
        return _boxes_result(page)

    _vision_stubs(monkeypatch, pages=2, detect=_detect)

    async def scenario() -> None:
        await queue._run_vision(_item(), dict(_VISION_CFG))

    _run(scenario)
    assert store.page_statuses() == {"1": "completed", "2": "completed"}
    assert _failed_pages_of(store) == {}
    # 每页账目的键集合不变（新增信息只允许出现在 recognition.failed_pages 一处）
    for patch in _perf_patches(store):
        for entry in (patch.get("recognition", {}).get("pages") or {}).values():
            assert set(entry) <= {
                "page", "started_at", "finished_at", "duration_ms", "status",
                "active_pages_at_end", "active_pages_at_start", "page_concurrency",
                "duration_breakdown_ms", "cache_status", "pipeline_status", "warnings",
            }, set(entry)


# ---------------------------------------------------------------------------
# U10：skip_review 守卫——带失败页不 honor skip_item_review（冷审 C1a）
# ---------------------------------------------------------------------------
def test_u10_skip_review_guard_forces_awaiting_review(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _FakeStore()
    store.item["performance_json"] = json.dumps(
        {"recognition": {"failed_pages": {"2": "timeout"}}}
    )
    queue = _queue(store)
    enqueued: list[tq.TaskItem] = []
    monkeypatch.setattr(queue, "enqueue", lambda task: enqueued.append(task))

    queue._mark_recognition_complete(
        _item(),
        {"skip_item_review": True},
        store,
    )
    statuses = [c["status"] for c in store.status_calls]
    assert statuses == [JobItemStatus.AWAITING_REVIEW.value], store.status_calls
    assert not enqueued, "带失败页必须停审阅，不得直通打码"


def test_u10b_clean_item_still_honors_skip_review(monkeypatch: pytest.MonkeyPatch) -> None:
    """回归钉：干净条目的 skip_item_review 语义不变。"""
    store = _FakeStore()
    store.item["performance_json"] = json.dumps({"recognition": {"failed_pages": {}}})
    queue = _queue(store)
    enqueued: list[tq.TaskItem] = []
    monkeypatch.setattr(queue, "enqueue", lambda task: enqueued.append(task))

    queue._mark_recognition_complete(_item(), {"skip_item_review": True}, store)
    assert len(enqueued) == 1 and enqueued[0].task_type == "redaction"
