"""safe_regex 编译超时协议（Issue#109）：

`safe_compile` 用子进程探针挡住灾难性回溯（ReDoS），默认预算 2.0s。原用例
`test_compile_timeout_still_enforced` 直接 `safe_compile(r"(a+)+$")` 期待默认
预算被撞破——但该模式在 25 字探针（`safe_regex._PROBE`）上的回溯耗时就在 2s
上下（本机与冷审环境多次实测落在 **2.3–2.6s** 带内，按 2.0s 预算余量仅 ≈1.2×），
机器更快或负载更轻就翻面成 `DID NOT RAISE`（CI 实测 flaky，见 Issue#109）。

现在把「协议」与「真实探针」拆开，协议部分完全不含计时：
① 撞破预算后的行为（取消在飞 future + 换池 + 抛错）——替身池，零计时；
② 未撞破预算的路径（正常返回、不换池）——替身池，零计时（反向对照）；
③ 默认预算（不传 timeout）必须真的送到 `future.result`；
④ 换池函数的契约（shutdown + 置空），以及置空后 `_get_pool()` 重建新池；
⑤ 真·爆炸模式仍被拒 / 良性模式能过——真实池集成对照，留 ≈4.8× 余量（不再是
   ≈1.2× 刀尖）；良性对照给 5.0s 以吸收冷 spawn；本环境建不出进程池时自动跳过
   （见 `requires_process_pool`）。

复现探针耗时（本机 Linux / Python 3.12，连跑三次）：

    python - <<'PY'
    import re, timeit
    p = r'(a+)+$'
    print(timeit.repeat(lambda: re.compile(p, re.I).search('a' * 25 + '!'), repeat=3, number=1))
    PY
    # ⇒ [2.349, 2.367, 2.383]（同机不同负载在此量级浮动；冷审环境 [2.432, 2.436, 2.529]）
"""

import inspect
import multiprocessing
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError

import pytest

from app.core import safe_regex
from app.core.safe_regex import RegexTimeoutError, safe_compile, safe_finditer


def _process_pool_unusable_reason() -> str | None:
    """本环境建不出进程池时返回原因，可用则返回 `None`。

    部分开发容器/CI 沙箱禁用 POSIX 信号量，`ProcessPoolExecutor(...)` 在**构造期**
    就抛 `PermissionError: [Errno 13] Permission denied`（`multiprocessing/
    synchronize.py` 的 `SemLock.__init__`）。真实池用例在这种环境下是「无法执行」
    而不是「断言失败」，故跳过而不是报错；CI（完整权限的 Linux）会执行它们。

    只把 `OSError`（含 `PermissionError`）当作「环境不可用」；其他异常应当炸出来，
    而不是被宽 `except` 静默吞掉、把用例伪装成「跳过」。
    """
    try:
        with ProcessPoolExecutor(
            max_workers=1, mp_context=multiprocessing.get_context("spawn")
        ):
            pass
    except OSError as exc:  # 含 PermissionError（SemLock 受限）
        return (
            f"本环境无法创建进程池（{type(exc).__name__}: {exc}）"
            "，真实池用例跳过；见 Issue#109"
        )
    return None


_POOL_SKIP_REASON = _process_pool_unusable_reason()

requires_process_pool = pytest.mark.skipif(
    _POOL_SKIP_REASON is not None,
    reason=_POOL_SKIP_REASON or "",
)


class _TimedOutFuture:
    """替身 future：`result()` 立刻抛超时（不依赖任何真实耗时）。"""

    def __init__(self):
        self.cancel_called = False
        self.timeouts = []

    def result(self, timeout=None):
        self.timeouts.append(timeout)
        raise FuturesTimeoutError()

    def cancel(self):
        self.cancel_called = True
        return True


class _OkFuture:
    """替身 future：记录 `result()` 收到的超时后正常返回（零计时）。"""

    def __init__(self):
        self.cancel_called = False
        self.timeouts = []

    def result(self, timeout=None):
        self.timeouts.append(timeout)
        return None

    def cancel(self):
        self.cancel_called = True
        return True


class _FakePool:
    def __init__(self, future):
        self.future = future
        self.submitted = []

    def submit(self, fn, *args, **kwargs):
        self.submitted.append(args)
        return self.future


def test_compile_timeout_still_enforced(monkeypatch):
    """预算撞破后：取消在飞 future、换掉池、抛 RegexTimeoutError。

    全程不含计时（替身池），所以换机器/换负载都不会翻面（Issue#109）。
    """
    future = _TimedOutFuture()
    pool = _FakePool(future)
    replaced = []
    monkeypatch.setattr(safe_regex, "_get_pool", lambda: pool)
    monkeypatch.setattr(
        safe_regex, "_kill_and_replace_pool", lambda: replaced.append(True)
    )

    with pytest.raises(RegexTimeoutError) as exc:
        safe_compile(r"(a+)+$", timeout=0.01)

    assert future.cancel_called, "撞破预算后必须取消在飞 future"
    assert replaced == [True], "撞破预算后必须换掉被卡死的池"
    assert future.timeouts == [0.01], "预算必须真的送到 future.result"
    assert pool.submitted == [(r"(a+)+$",)], "必须真的把探针提交出去"
    assert "catastrophic backtracking" in str(exc.value)


def test_compile_within_budget_does_not_replace_pool(monkeypatch):
    """反向对照：预算未撞破时不得换池，且必须正常返回编译结果。

    与上一条同为替身池、零计时——两条合起来才排除了「只是在测总抛错」
    （否则 `safe_compile` 退化成无条件抛错，两条都会过）。
    """
    future = _OkFuture()
    pool = _FakePool(future)
    replaced = []
    monkeypatch.setattr(safe_regex, "_get_pool", lambda: pool)
    monkeypatch.setattr(
        safe_regex, "_kill_and_replace_pool", lambda: replaced.append(True)
    )

    compiled = safe_compile(r"(?P<a>\d+)", timeout=2.0)

    assert compiled.pattern == r"(?P<a>\d+)"
    assert replaced == [], "预算未撞破时不得换池"
    assert not future.cancel_called, "预算未撞破时不得取消 future"


def test_default_budget_is_forwarded_to_the_pool(monkeypatch):
    """不传 timeout 时必须用上生产默认预算。

    其余用例都显式传了 timeout，若默认值被改坏（例如 2.0 → 20.0）将无人发现。
    这条把「默认预算」钉成契约：原用例的全部意义正是「默认预算能挡住灾难性
    回溯」，改动预算就必须同步改这里与文档。
    """
    future = _OkFuture()
    monkeypatch.setattr(safe_regex, "_get_pool", lambda: _FakePool(future))

    compiled = safe_compile(r"(?P<a>\d+)")  # 不传 timeout ⇒ 走默认预算

    default = inspect.signature(safe_compile).parameters["timeout"].default
    assert default == 2.0, "生产默认预算已变，需同步本条与文档（Issue#109）"
    assert future.timeouts == [default], "默认预算必须真的送到 future.result"
    assert compiled.pattern == r"(?P<a>\d+)"


def test_kill_and_replace_pool_shuts_down_and_clears(monkeypatch):
    """换池契约（kill 半）：先 shutdown(wait=False, cancel_futures=True)，再置空。"""

    class _RecordingPool:
        def __init__(self):
            self.calls = []

        def shutdown(self, **kwargs):
            self.calls.append(kwargs)

    stale = _RecordingPool()
    monkeypatch.setattr(safe_regex, "_pool", stale)

    safe_regex._kill_and_replace_pool()

    assert stale.calls == [{"wait": False, "cancel_futures": True}]
    assert safe_regex._pool is None


def test_get_pool_rebuilds_a_fresh_pool_after_kill(monkeypatch):
    """换池契约（replace 半）：置空后 `_get_pool()` 必须重建一个新池，不复用旧池。

    用替身 `ProcessPoolExecutor` 断言构造参数，故不产生真实子进程、零计时。
    """

    class _StalePool:
        def __init__(self):
            self.shutdown_calls = []

        def shutdown(self, **kwargs):
            self.shutdown_calls.append(kwargs)

    class _RecordingExecutor:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    built = []

    def _build(**kwargs):
        executor = _RecordingExecutor(**kwargs)
        built.append(executor)
        return executor

    stale = _StalePool()
    monkeypatch.setattr(safe_regex, "_pool", stale)
    monkeypatch.setattr(safe_regex, "ProcessPoolExecutor", _build)

    safe_regex._kill_and_replace_pool()
    assert safe_regex._pool is None
    assert stale.shutdown_calls == [{"wait": False, "cancel_futures": True}]

    fresh = safe_regex._get_pool()

    assert fresh is not stale, "置空后不得复活旧池"
    assert fresh is built[-1]
    assert len(built) == 1, "只应新建一个池（不是顺手多建几个）"
    assert built[-1].kwargs == {
        "max_workers": 1,
        "mp_context": multiprocessing.get_context("spawn"),
    }


@requires_process_pool
def test_real_exploding_pattern_rejected_with_small_budget():
    """真·灾难性回溯模式在真实池上仍被拒。

    先跑一次正常模式，让首个 worker 在预算之外完成 spawn/启动；随后这条计时
    调用复用同一个已就绪的池，所以 0.5s 预算量的是「提交往返 + 探针」，不含
    进程启动。热池后正常探针 ~ms，而 `(a+)+$` 对 25 字探针本机多次实测为
    2.3–2.6s ⇒ 0.5s 预算余量 ≈4.7–5.0×（记 ≈4.8×）；原实现按默认 2.0s 只有
    ≈1.2×，正是坐在刀尖上才 flaky（Issue#109）。
    """
    safe_compile(r"(?P<warmup>\d+)", timeout=5.0)
    with pytest.raises(RegexTimeoutError):
        safe_compile(r"(a+)+$", timeout=0.5)


@requires_process_pool
def test_benign_pattern_within_budget_does_not_raise():
    """真实池集成对照：正常模式必须能过（跨机器/负载都不该翻面）。

    这条可能紧接上一条（上一条撞破预算后已换池、`_pool` 置空）执行，故首调含
    **冷 spawn 启动开销**；预算给到 5.0s 吸收启动抖动，而正常日期正则本身只需
    ~ms——真正被检验的是「正常模式不会被误判为灾难性回溯」，不是单页耗时。
    """
    compiled = safe_compile(r"(?P<y>\d{4})年(?P<m>\d{1,2})月", timeout=5.0)
    assert compiled.pattern == r"(?P<y>\d{4})年(?P<m>\d{1,2})月"
    assert [m.group() for m in safe_finditer(compiled, "2022年11月 与 2023年1月")] == [
        "2022年11月",
        "2023年1月",
    ]
