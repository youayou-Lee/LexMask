"""Issue#156 项①：正则超时事件必须**可读**（此前只写不读）。

超时此前只进日志与异常文本（`safe_compile` / `safe_finditer` /
`safe_finditer_many` 三处），运维侧无法回答「超时发生过几次、在哪条链」。
本文件钉住：进程级计数按链分开、可累加、可清零，并经 `GET /health` 暴露。
"""
import asyncio
import re
from concurrent.futures import TimeoutError as FuturesTimeoutError

import pytest

from app.core import safe_regex


class _TimedOutFuture:
    """替身 future：`result()` 立刻抛 FuturesTimeoutError（零计时）。"""

    def __init__(self) -> None:
        self.cancelled = False

    def result(self, timeout=None):  # noqa: ANN001
        raise FuturesTimeoutError()

    def cancel(self) -> bool:
        self.cancelled = True
        return True


class _FakePool:
    def __init__(self) -> None:
        self.submitted: list[str] = []

    def submit(self, fn, *args, **kwargs):  # noqa: ANN001
        self.submitted.append(fn.__name__)
        return _TimedOutFuture()

    def shutdown(self, **kwargs) -> None:  # noqa: ANN003
        return None


@pytest.fixture(autouse=True)
def _clean_stats():
    safe_regex.reset_regex_timeout_stats()
    yield
    safe_regex.reset_regex_timeout_stats()


def _swap_pool(monkeypatch) -> _FakePool:  # noqa: ANN001
    pool = _FakePool()
    monkeypatch.setattr(safe_regex, "_get_pool", lambda: pool)
    monkeypatch.setattr(safe_regex, "_pool", pool)
    return pool


def test_safe_finditer_timeout_is_counted(monkeypatch) -> None:
    _swap_pool(monkeypatch)
    with pytest.raises(safe_regex.RegexTimeoutError):
        safe_regex.safe_finditer(re.compile("x"), "x", timeout=0.001)
    assert safe_regex.regex_timeout_stats() == {"safe_finditer": 1}


def test_safe_finditer_many_timeout_is_counted_separately(monkeypatch) -> None:
    _swap_pool(monkeypatch)
    with pytest.raises(safe_regex.RegexTimeoutError):
        safe_regex.safe_finditer_many(re.compile("x"), ["x", "y"], timeout=0.001)
    assert safe_regex.regex_timeout_stats() == {"safe_finditer_many": 1}


def test_safe_compile_timeout_is_counted_separately(monkeypatch) -> None:
    _swap_pool(monkeypatch)
    with pytest.raises(safe_regex.RegexTimeoutError):
        safe_regex.safe_compile(r"(a+)+$", timeout=0.001)
    assert safe_regex.regex_timeout_stats() == {"safe_compile": 1}


def test_counters_accumulate_per_operation(monkeypatch) -> None:
    _swap_pool(monkeypatch)
    for _ in range(2):
        with pytest.raises(safe_regex.RegexTimeoutError):
            safe_regex.safe_finditer(re.compile("x"), "x", timeout=0.001)
    with pytest.raises(safe_regex.RegexTimeoutError):
        safe_regex.safe_compile(r"(a+)+$", timeout=0.001)
    assert safe_regex.regex_timeout_stats() == {"safe_finditer": 2, "safe_compile": 1}


def test_reset_clears_counters(monkeypatch) -> None:
    _swap_pool(monkeypatch)
    with pytest.raises(safe_regex.RegexTimeoutError):
        safe_regex.safe_finditer(re.compile("x"), "x", timeout=0.001)
    safe_regex.reset_regex_timeout_stats()
    assert safe_regex.regex_timeout_stats() == {}


def test_successful_call_does_not_bump_counters(monkeypatch) -> None:
    """反向对照：没超时就必须是 0（防止「恒 +1」式假可观测）。"""
    pool = _FakePool()
    monkeypatch.setattr(safe_regex, "_get_pool", lambda: pool)
    monkeypatch.setattr(safe_regex, "_pool", pool)
    assert safe_regex.regex_timeout_stats() == {}


def test_health_exposes_regex_timeouts(monkeypatch) -> None:
    _swap_pool(monkeypatch)
    with pytest.raises(safe_regex.RegexTimeoutError):
        safe_regex.safe_finditer(re.compile("x"), "x", timeout=0.001)
    from app.main import health_check

    resp = asyncio.run(health_check())
    assert resp.regex_timeouts == {"safe_finditer": 1}
