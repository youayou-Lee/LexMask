"""Issue#217 PR②：视觉检测服务的任务入口秒级预检（fail-closed）。

现状（改前）：勾选视觉识别项但 LA 服务不在场时，任务入口直接发起 LA 调用，
在 ``VISUAL_FEATURES_TIMEOUT=240`` 上叠加 retry_async 重试，实测挂起场景
单次 240.2s、全链 ≈723s（探针报告 2026-10-31）。用户裁定：上传页就该看见
服务在不在（前端徽标），任务入口也必须秒级明确失败——绝不静默跳过
（印章是打码对象，漏检=泄漏，承 #199/#203 fail-closed 原则）。

本文件钉三件事：
1. 预检通过（服务可达）不拦截；
2. 预检失败立即抛 ``visual_service_unavailable``（AppError），文案含三选指引；
3. 负结果短 TTL 缓存（5s，与 HaS ``_HAS_NEGATIVE_HEALTH_TTL_SEC`` 同源模式），
   TTL 内不重复发探测请求。
外加接线钉：``detect_vision`` 源码必须真的调用预检（AST 级，防回归为装饰性）。
"""
from __future__ import annotations

import asyncio
import inspect

import pytest

from app.core.errors import AppError
from app.services import redaction_orchestrator as orch


def _run(coro):
    return asyncio.run(coro)


def test_preflight_passes_when_probe_ok(monkeypatch):
    """服务可达 ⇒ 不拦截。"""
    calls = []

    async def ok_probe():
        calls.append(1)

    monkeypatch.setattr(orch, "_probe_visual_service", ok_probe)
    monkeypatch.setattr(orch, "_visual_down_until", 0.0)
    _run(orch.ensure_visual_service_available())
    assert calls, "probe 必须真的被调用（否则是恒真断言）"


def test_preflight_fails_fast_with_actionable_error(monkeypatch):
    """服务不可达 ⇒ 立即 AppError（不是 240s 长挂）。"""
    async def down_probe():
        raise ConnectionError("refused")

    monkeypatch.setattr(orch, "_probe_visual_service", down_probe)
    monkeypatch.setattr(orch, "_visual_down_until", 0.0)
    with pytest.raises(AppError) as ei:
        _run(orch.ensure_visual_service_available())
    err = ei.value
    assert err.error_code == "visual_service_unavailable", err.error_code
    assert err.status_code in (400, 503)
    # 指引文案：三条出路至少各提其一（本地开、云端、取消勾选）
    msg = err.message
    assert ("--with-visual" in msg) or ("视觉" in msg and "启动" in msg), msg
    assert ("cloud" in msg) or ("云端" in msg), msg
    assert ("取消勾选" in msg) or ("不勾" in msg) or ("取消选择" in msg), msg


def test_preflight_negative_cache_within_ttl(monkeypatch):
    """TTL 内第二次调用不得再发探测（负缓存），但仍快速失败。"""
    probe_calls = []

    async def down_probe():
        probe_calls.append(1)
        raise ConnectionError("refused")

    monkeypatch.setattr(orch, "_probe_visual_service", down_probe)
    monkeypatch.setattr(orch, "_visual_down_until", 0.0)
    with pytest.raises(AppError):
        _run(orch.ensure_visual_service_available())
    assert len(probe_calls) == 1
    # 第二次：TTL 内（_visual_down_until 未过期——上一轮失败已把它推到未来）
    with pytest.raises(AppError) as ei2:
        _run(orch.ensure_visual_service_available())
    assert len(probe_calls) == 1, "TTL 内不得重复探测"
    assert ei2.value.error_code == "visual_service_unavailable"


def test_preflight_probe_success_resets_negative_cache(monkeypatch):
    """探测成功要清掉负缓存（服务回来后任务立即可跑）。"""
    state = {"down": False}

    async def flippy_probe():
        if state["down"]:
            raise ConnectionError("refused")

    monkeypatch.setattr(orch, "_probe_visual_service", flippy_probe)
    monkeypatch.setattr(orch, "_visual_down_until", 0.0)
    state["down"] = True
    with pytest.raises(AppError):
        _run(orch.ensure_visual_service_available())
    state["down"] = False
    monkeypatch.setattr(orch, "_visual_down_until", 0.0)  # TTL 外
    _run(orch.ensure_visual_service_available())  # 不抛 = 服务恢复可用


def test_detect_vision_is_wired_to_preflight():
    """接线钉（AST 级）：detect_vision 源码必须调用 ensure_visual_service_available。

    改前未实现时本测试红（TDD 起点）；装饰性接线（函数存在但 detect_vision
    不调用）同样判红。
    """
    src = inspect.getsource(orch.detect_vision)
    assert "ensure_visual_service_available" in src, (
        "detect_vision 必须在进入视觉阶段前调用秒级预检（Issue#217 PR②）"
    )
