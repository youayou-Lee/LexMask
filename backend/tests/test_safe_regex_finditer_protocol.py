"""safe_regex 的 finditer 超时协议（Issue#135，#109 的 follow-up）。

`safe_compile` 的超时协议已有**零计时**替身池用例（`test_safe_regex_timeout_protocol.py`），
但 `safe_finditer` / `safe_finditer_many` 的**同形超时分支**当时只在具名组传输用例里被间接
摸到：撞破预算后「取消在飞 future + 换掉被卡死的池 + 抛 `RegexTimeoutError`」这条协议、
以及 `_kill_and_replace_pool` 在 finditer 路径上的契约，都没有点名的可失败判据
（Issue#135 正文所指的 finditer 超时分支与 `safe_compile` 的同形分支，前者缺测）。

本文件把「协议」与「真实探针」拆开，协议部分完全不含计时（替身池）：
① 撞破预算后的行为（取消在飞 future + 换池 + 抛错，且预算真的送到 `future.result`，
   并钉住「先 cancel 再换池」的顺序）；
② 未撞破预算的反向对照（正常返回、不换池、不取消）；③ 默认预算必须真的送到 `future.result`；
④ 返回值必须被包装成 `_MatchProxy`（含未参与的命名组不出现）；
⑤ 批量版的三条同形协议 + 空输入不得触碰进程池；
⑥ 真实池集成对照（良性模式；本环境建不出池时自动跳过）；
⑦ 分支盲区：非超时异常不得换池、`cancel()` 返回假值时不得继续换池。

刻意**不**测「某个具体 pattern 是否撞破某预算」——那是 #109 已立的教训（计时会翻面）。
"""

import re
from concurrent.futures import TimeoutError as FuturesTimeoutError

import pytest
from process_pool_guard import requires_process_pool

from app.core import safe_regex
from app.core.safe_regex import (
    RegexTimeoutError,
    safe_finditer,
    safe_finditer_many,
)


class _TimedOutFuture:
    """替身 future：`result()` 立刻抛超时（不依赖任何真实耗时）。"""

    def __init__(self, events=None):
        self.cancel_called = False
        self.timeouts = []
        self.events = events  # 门⑥ r6 M2：记录 cancel 与换池的先后顺序

    def result(self, timeout=None):
        self.timeouts.append(timeout)
        raise FuturesTimeoutError()

    def cancel(self):
        self.cancel_called = True
        if self.events is not None:
            self.events.append("cancel")
        return True


class _OkFuture:
    """替身 future：记录 `result()` 收到的超时后返回给定载荷（零计时）。"""

    def __init__(self, payload=None):
        self.payload = payload
        self.cancel_called = False
        self.timeouts = []

    def result(self, timeout=None):
        self.timeouts.append(timeout)
        return self.payload

    def cancel(self):
        self.cancel_called = True
        return True


class _FakePool:
    def __init__(self, future):
        self.future = future
        self.submitted = []
        self.shutdowns = []
        self.names = []

    def submit(self, fn, *args, **kwargs):
        self.submitted.append(args)
        # 门①.5 r1 N1(b)：记录 callable 身份，让「谁被调用」也能在零计时用例里判红
        self.names.append(getattr(fn, "__name__", repr(fn)))
        return self.future

    def shutdown(self, wait=True, cancel_futures=False):
        self.shutdowns.append((wait, cancel_futures))


@pytest.fixture
def _patched(monkeypatch):
    """装好替身池 + 换池记录器，返回 (pool, replaced)。

    必须在用例内部通过 `_patched.pool` / `_patched.replaced` 取用：
    fixture 只负责 monkeypatch 与容器。
    """

    class _Handle:
        pass

    handle = _Handle()
    handle.replaced = []
    handle.events = []

    def _replace():
        handle.events.append("replace")
        handle.replaced.append(True)

    monkeypatch.setattr(safe_regex, "_kill_and_replace_pool", _replace)
    return handle


# ---- ① 超时协议（safe_finditer）----


def test_finditer_timeout_cancels_replaces_and_raises(monkeypatch, _patched):
    """撞破预算：取消在飞 future、换池、抛 `RegexTimeoutError`（零计时）。"""
    import re

    future = _TimedOutFuture(events=_patched.events)
    pool = _FakePool(future)
    monkeypatch.setattr(safe_regex, "_get_pool", lambda: pool)
    compiled = re.compile(r"(a+)+$")

    with pytest.raises(RegexTimeoutError) as exc:
        safe_finditer(compiled, "a" * 25 + "!", timeout=0.01)

    assert future.cancel_called, "撞破预算后必须取消在飞 future"
    assert _patched.replaced == [True], "撞破预算后必须换掉被卡死的池"
    assert _patched.events == ["cancel", "replace"], (
        "门⑥ r6 M2：必须先取消在飞 future，再换掉池（顺序反了会先毁池再取消，"
        "在真实池下等于丢结果或死锁）"
    )
    assert future.timeouts == [0.01], "预算必须真的送到 future.result"
    assert pool.submitted == [
        (compiled.pattern, compiled.flags, "a" * 25 + "!")
    ], "必须真的把 finditer 任务（pattern, flags, text）提交出去"
    assert "finditer timed out" in str(exc.value), "错误文案必须可判定为 finditer 超时"
    assert "0.01" in str(exc.value), "错误文案必须带上撞破的预算值"


def test_finditer_within_budget_does_not_cancel_or_replace(monkeypatch, _patched):
    """反向对照：预算内正常返回时不得取消、不得换池。"""
    import re

    future = _OkFuture([("ab", 0, 2, [("g", 0, 1, "a")])])
    pool = _FakePool(future)
    monkeypatch.setattr(safe_regex, "_get_pool", lambda: pool)

    matches = safe_finditer(re.compile(r"ab"), "xxab", timeout=3.0)

    assert len(matches) == 1
    assert future.cancel_called is False, "预算内不得取消在飞 future"
    assert _patched.replaced == [], "预算内不得换池"
    assert future.timeouts == [3.0], "预算必须真的送到 future.result"


def test_finditer_default_budget_is_forwarded(monkeypatch, _patched):
    """不传 timeout 时，模块默认预算（5.0s）必须送到 `future.result`。"""
    import re

    future = _OkFuture([])
    monkeypatch.setattr(safe_regex, "_get_pool", lambda: _FakePool(future))

    safe_finditer(re.compile(r"x"), "text")

    assert future.timeouts == [5.0], "默认预算必须真的送到 future.result"


# ---- ④ 返回值的包装契约 ----


def test_finditer_wraps_rows_into_match_proxies(monkeypatch, _patched):
    """worker 返回的元组必须被包装成 `_MatchProxy`，命名组与偏移逐位一致。"""
    import re

    rows = [
        ("ab", 0, 2, [("g", 0, 1, "a"), ("h", 1, 2, "b")]),
        ("c", 3, 4, []),
        ("", 5, 5, []),
    ]
    monkeypatch.setattr(
        safe_regex, "_get_pool", lambda: _FakePool(_OkFuture(rows))
    )

    matches = safe_finditer(re.compile(r"."), "abcd")

    assert [m.group() for m in matches] == ["ab", "c", ""]
    assert [(m.start(), m.end()) for m in matches] == [(0, 2), (3, 4), (5, 5)]
    assert matches[0].named_groups() == [("g", 0, 1, "a"), ("h", 1, 2, "b")]
    assert matches[1].named_groups() == [], "无命名组时必须回空列表而不是 None"
    # 空匹配照样产出（下游 match_items_indexed 依赖它做「空组占位」判定）
    assert matches[2].named_groups() == []


# ---- ⑤ 批量版的同形协议 ----


def test_finditer_many_empty_texts_does_not_touch_the_pool(monkeypatch):
    """空输入必须**早退**：既不建池也不提交任务。"""
    import re

    touched = []
    monkeypatch.setattr(
        safe_regex, "_get_pool", lambda: touched.append(True) or _FakePool(_OkFuture([]))
    )

    assert safe_finditer_many(re.compile(r"x"), []) == []
    assert touched == [], "空 texts 不得触碰进程池"


def test_finditer_many_timeout_cancels_replaces_and_raises(monkeypatch, _patched):
    """批量版撞破预算：同形协议 + 文案必须带上批量规模。"""
    import re

    future = _TimedOutFuture(events=_patched.events)
    pool = _FakePool(future)
    monkeypatch.setattr(safe_regex, "_get_pool", lambda: pool)
    compiled = re.compile(r"(a+)+$")

    with pytest.raises(RegexTimeoutError) as exc:
        safe_finditer_many(compiled, ["a", "b", "c"], timeout=0.02)

    assert future.cancel_called, "撞破预算后必须取消在飞 future"
    assert _patched.replaced == [True], "撞破预算后必须换掉被卡死的池"
    assert _patched.events == ["cancel", "replace"], "门⑥ r6 M2：批量路径同样先取消再换池"
    assert future.timeouts == [0.02], "预算必须真的送到 future.result"
    assert pool.submitted == [
        (compiled.pattern, compiled.flags, ["a", "b", "c"])
    ], "必须把整批（pattern, flags, texts）一次提交出去"
    assert "over 3 texts" in str(exc.value), "错误文案必须带上批量规模"
    assert "0.02" in str(exc.value), "门⑥ r6 M1：批量文案必须带上撞破的预算值（与单条路径对齐）"


def test_finditer_many_returns_one_list_per_text_in_order(monkeypatch, _patched):
    """批量版必须「一进一出、顺序对应」，空行回空列表。"""
    import re

    payload = [
        [("a", 0, 1, [])],
        [],
        [("b", 2, 3, [("g", 2, 3, "b")]), ("c", 4, 5, [])],
    ]
    monkeypatch.setattr(
        safe_regex, "_get_pool", lambda: _FakePool(_OkFuture(payload))
    )

    result = safe_finditer_many(re.compile(r"."), ["a", "", "bc"])

    assert len(result) == 3, "每个输入文本必须且只能对应一个结果列表"
    assert [m.group() for m in result[0]] == ["a"]
    assert result[1] == []
    assert [m.group() for m in result[2]] == ["b", "c"]
    assert result[2][1].named_groups() == []


def test_finditer_many_default_budget_is_forwarded(monkeypatch, _patched):
    """批量版不传 timeout 时同样送到模块默认预算。"""
    import re

    future = _OkFuture([[("a", 0, 1, [])]])
    monkeypatch.setattr(safe_regex, "_get_pool", lambda: _FakePool(future))

    safe_finditer_many(re.compile(r"a"), ["a"])

    assert future.timeouts == [5.0], "默认预算必须真的送到 future.result"


def test_after_finditer_timeout_pool_is_rebuilt_not_reused(monkeypatch):
    """换池后 `_get_pool()` 必须**新建**池，而不是复用被卡死的那个（Issue#135）。"""

    stale = _FakePool(_OkFuture([]))
    built = []

    class _Fresh(_FakePool):
        pass

    def _build(*args, **kwargs):
        pool = _Fresh(_OkFuture([]))
        built.append(pool)
        return pool

    monkeypatch.setattr(safe_regex, "_pool", stale)
    monkeypatch.setattr(safe_regex, "ProcessPoolExecutor", _build)

    safe_regex._kill_and_replace_pool()

    assert safe_regex._pool is None, "换池后必须置空"
    assert stale.shutdowns == [(False, True)], (
        "换池必须对旧池 shutdown(wait=False, cancel_futures=True)"
    )

    rebuilt = safe_regex._get_pool()

    assert rebuilt is not stale, "取池必须新建实例，不得复用被卡死的那个"
    assert built == [rebuilt], "新建必须走 ProcessPoolExecutor 且只建一次"
    assert safe_regex._get_pool() is rebuilt, "取池必须复用新建实例（不得每次新建）"


# ---- ⑥ 真实池集成对照（良性模式；建不出池时跳过）----


@requires_process_pool
def test_real_finditer_benign_pattern_returns_matches():
    """真实池上跑一个良性模式：必须真的回匹配（余量足够，不做计时判据）。"""
    import re

    matches = safe_finditer(re.compile(r"\d+"), "a12b345", timeout=5.0)

    assert [m.group() for m in matches] == ["12", "345"]
    assert [(m.start(), m.end()) for m in matches] == [(1, 3), (4, 7)]


@requires_process_pool
def test_real_finditer_many_batches_in_one_call():
    """真实池上批量调用：一次提交、按序回多组（含无匹配的文本）。"""
    import re

    result = safe_finditer_many(re.compile(r"\d+"), ["a1", "", "b22c33"], timeout=5.0)

    assert [[m.group() for m in rows] for rows in result] == [["1"], [], ["22", "33"]]

# ---- ⑦ 分支盲区（门①.5 r1 B3）：非超时异常与 cancel 失败 ----


class _NonTimeoutFuture:
    """`result()` 抛**非超时**异常（worker 里真炸），`cancel()` 返回 True。"""

    def __init__(self, exc):
        self._exc = exc
        self.cancel_called = False

    def result(self, timeout=None):
        raise self._exc

    def cancel(self):
        self.cancel_called = True
        return True


class _UncancellableTimedOutFuture:
    """超时且 `cancel()` 返回 **False**（真实在飞任务几乎总是如此）。"""

    def __init__(self):
        self.cancel_called = False
        self.timeouts = []

    def result(self, timeout=None):
        self.timeouts.append(timeout)
        raise FuturesTimeoutError()

    def cancel(self):
        self.cancel_called = True
        return False


def test_finditer_non_timeout_exception_is_not_relabelled(monkeypatch, _patched):
    """V4 型变异判红（`except FuturesTimeoutError` 放宽成 `except Exception`）。

    worker 内的真异常必须**原样传播**、**不换池**、文案不含 `timed out`
    ——否则任何 worker 崩溃都会被记成 ReDoS 超时并换掉进程池。
    """
    import re

    boom = re.error("boom: bad pattern at runtime")
    pool = _FakePool(_NonTimeoutFuture(boom))
    monkeypatch.setattr(safe_regex, "_get_pool", lambda: pool)
    with pytest.raises(re.error) as excinfo:
        safe_finditer(re.compile("a+"), "aaa")
    assert not isinstance(excinfo.value, RegexTimeoutError)
    assert "timed out" not in str(excinfo.value)
    assert _patched.replaced == []
    assert pool.shutdowns == []


def test_finditer_many_non_timeout_exception_is_not_relabelled(monkeypatch, _patched):
    """同 V4，批量路径对称。"""
    import re

    pool = _FakePool(_NonTimeoutFuture(re.error("boom: bad pattern at runtime")))
    monkeypatch.setattr(safe_regex, "_get_pool", lambda: pool)
    with pytest.raises(re.error):
        safe_finditer_many(re.compile("a+"), ["aaa"])
    assert _patched.replaced == []


def test_finditer_timeout_raises_even_when_cancel_returns_false(monkeypatch, _patched):
    """V10 型变异判红（cancel 失败则静默回空）。

    `cancel()` 返回 False 只说明「在飞任务没被取消」，**不能**当作「不抛超时」的理由
    ——否则超时被吞成零命中（静默漏检）。换池 + 抛错都必须照做。
    """
    future = _UncancellableTimedOutFuture()
    pool = _FakePool(future)
    monkeypatch.setattr(safe_regex, "_get_pool", lambda: pool)
    with pytest.raises(RegexTimeoutError):
        safe_finditer(re.compile("a+"), "aaa")
    assert future.cancel_called is True
    assert _patched.replaced == [True]
    assert pool.names == ["_finditer_in_process"]  # N1(b)：callable 身份


def test_finditer_many_timeout_raises_even_when_cancel_returns_false(monkeypatch, _patched):
    """同 V10，批量路径对称。"""
    pool = _FakePool(_UncancellableTimedOutFuture())
    monkeypatch.setattr(safe_regex, "_get_pool", lambda: pool)
    with pytest.raises(RegexTimeoutError):
        safe_finditer_many(re.compile("a+"), ["aaa"])
    assert _patched.replaced == [True]
    assert pool.names == ["_finditer_many_in_process"]  # N1(b)


def test_after_timeout_the_next_call_lands_on_a_new_pool_object(monkeypatch):
    """N4：端到端换池——**不** patch `_kill_and_replace_pool`，只 patch `ProcessPoolExecutor`。

    第一次调用超时 → 老池被关；第二次调用必须落在**另一个**池实例上（不得复用被污染的池）。
    """
    made = []

    def _factory(*_args, **_kwargs):
        pool = _FakePool(
            _TimedOutFuture() if not made else _OkFuture([("aaa", 0, 3, [])])
        )
        made.append(pool)
        return pool

    monkeypatch.setattr(safe_regex, "ProcessPoolExecutor", _factory)
    monkeypatch.setattr(safe_regex, "_pool", None, raising=False)
    with pytest.raises(RegexTimeoutError):
        safe_finditer(re.compile("a+"), "aaa")
    assert len(made) == 1
    matches = safe_finditer(re.compile("a+"), "aaa")
    assert len(made) == 2, "超时后必须建新池，不得复用被污染的池对象"
    assert made[0].shutdowns, "老池必须被关闭"
    assert made[1].submitted[0][0] == "a+" and made[1].submitted[0][2] == "aaa"
    assert [m.group() for m in matches] == ["aaa"]
