"""
Safe regex utilities with timeout protection against ReDoS
(catastrophic backtracking).

Uses ThreadPoolExecutor to enforce wall-clock timeouts on both
compile+probe and finditer, since Python's ``re`` module does not
natively support cancellation.
"""

import multiprocessing
import re
import threading
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError


class RegexTimeoutError(ValueError):
    """Raised when a regex operation exceeds its time budget."""


# ---- 超时可观测面（Issue#156）--------------------------------------------
#: 进程级超时计数。此前超时事件**只写不读**（只进日志与异常文本），运维侧只能
#: grep 日志、无法判断超时发生过几次、在哪条链；这里给一个可读快照，由
#: ``GET /health`` 暴露（``regex_timeouts`` 字段）。
_TIMEOUT_STATS_LOCK = threading.Lock()
_TIMEOUT_STATS: dict[str, int] = {}


def _record_regex_timeout(operation: str) -> None:
    """记一次超时。*operation* 取 ``safe_compile`` / ``safe_finditer`` /
    ``safe_finditer_many``（对应三条调用链）。"""
    with _TIMEOUT_STATS_LOCK:
        _TIMEOUT_STATS[operation] = _TIMEOUT_STATS.get(operation, 0) + 1


def regex_timeout_stats() -> dict[str, int]:
    """超时计数快照 ``{operation: count}``（进程级累计值，只读副本）。"""
    with _TIMEOUT_STATS_LOCK:
        return dict(_TIMEOUT_STATS)


def reset_regex_timeout_stats() -> None:
    """清空超时计数（测试用）。"""
    with _TIMEOUT_STATS_LOCK:
        _TIMEOUT_STATS.clear()


# Short probe string used to detect catastrophic backtracking at compile time.
# 25 repeating chars is enough to trigger exponential backtracking in evil
# patterns like (a+)+$ while keeping the subprocess killable.
_PROBE = "a" * 25 + "!"


def _compile_and_probe(pattern: str) -> re.Pattern:
    """Compile the pattern and run it against a short probe string."""
    compiled = re.compile(pattern, re.IGNORECASE)
    # Probe: run a quick findall to trigger any backtracking early
    compiled.findall(_PROBE)
    return compiled


def _finditer_many_in_process(
    pattern_str: str, flags: int, texts: list[str]
) -> list[list[tuple[str, int, int, list[tuple[str, int, int, str]]]]]:
    """Run finditer over several texts in ONE subprocess call.

    Same per-match payload as :func:`_finditer_in_process`, grouped per input
    text. Batched so callers that scan many short texts (per OCR block, see
    ``Issue#64``) pay one IPC round-trip instead of one per text.

    Per match the payload is a **4 元组**：整 match 文本/起止 + **参与**的命名组
    （声明顺序）。Issue#156 刻意**不**把「声明了的命名组名」带过进程边界——生产
    路径必须保持既有契约（命中分支没有任何参与命名组 ⇒ 整 match 一项，否则 preset
    ``DATE`` 的单日期分支会被整条吞掉、变成漏打码）。「声明了但全组未参与」只在真
    ``re.Match`` 路径上可判定，见 :func:`_declared_named_groups`。
    """
    compiled = re.compile(pattern_str, flags)
    group_order = sorted(compiled.groupindex, key=compiled.groupindex.get)
    per_text = []
    for text in texts:
        rows = []
        for m in compiled.finditer(text):
            named = [
                (name, m.start(name), m.end(name), m.group(name))
                for name in group_order
                if m.start(name) != -1
            ]
            rows.append((m.group(), m.start(), m.end(), named))
        per_text.append(rows)
    return per_text


def _finditer_in_process(
    pattern_str: str, flags: int, text: str
) -> list[tuple[str, int, int, list[tuple[str, int, int, str]]]]:
    """Run finditer in a subprocess and return serialisable tuples.

    Per match: whole-match text/start/end plus the participating named
    groups as ``(name, start, end, text)`` with absolute offsets (Issue#52:
    interval / birth-context patterns emit per-group entities). 载荷**不含**声明表，
    理由见 :func:`_finditer_many_in_process`。
    """
    return _finditer_many_in_process(pattern_str, flags, [text])[0]


# Reusable process pool (spawned once, avoids per-call fork overhead).
# max_workers=1 is fine — only one probe runs at a time per call.
_pool: ProcessPoolExecutor | None = None


def _get_pool() -> ProcessPoolExecutor:
    global _pool
    if _pool is None:
        ctx = multiprocessing.get_context("spawn")
        _pool = ProcessPoolExecutor(max_workers=1, mp_context=ctx)
    return _pool


def safe_compile(pattern: str, timeout: float = 2.0) -> re.Pattern:
    """Compile *pattern* and test-match against a probe string.

    Raises ``RegexTimeoutError`` if the probe exceeds *timeout* seconds,
    or ``re.error`` if the pattern syntax is invalid.

    Uses a subprocess so that a stuck regex can be killed cleanly.
    """
    # First: validate syntax in the current process (fast, no subprocess)
    compiled = re.compile(pattern, re.IGNORECASE)

    # Then: probe for catastrophic backtracking in a subprocess
    pool = _get_pool()
    future = pool.submit(_compile_and_probe, pattern)
    try:
        future.result(timeout=timeout)
    except FuturesTimeoutError:
        future.cancel()
        # Kill and replace the pool so the stuck worker is terminated
        _kill_and_replace_pool()
        _record_regex_timeout("safe_compile")
        raise RegexTimeoutError(
            f"Regex compile+probe timed out after {timeout}s — "
            f"pattern may cause catastrophic backtracking (ReDoS): {pattern!r}"
        )
    return compiled


class _MatchProxy:
    """Lightweight stand-in for ``re.Match`` returned from subprocess results."""
    __slots__ = ("_text", "_start", "_end", "_named_groups")

    def __init__(
        self,
        text: str,
        start: int,
        end: int,
        named_groups: list[tuple[str, int, int, str]] | None = None,
    ):
        self._text = text
        self._start = start
        self._end = end
        self._named_groups = list(named_groups or [])

    def group(self, *args: int) -> str:
        return self._text

    def start(self) -> int:
        return self._start

    def end(self) -> int:
        return self._end

    def named_groups(self) -> list[tuple[str, int, int, str]]:
        """参与匹配的命名组 ``(name, start, end, text)``，未参与的组不出现。"""
        return list(self._named_groups)

    def declared_named_groups(self) -> list[str]:
        """本代理可见的命名组名（**只含参与本轮匹配的组**，声明顺序）。

        子进程载荷刻意不传声明表（见 :func:`_finditer_many_in_process`），因此这里
        等价于「以参与组名当声明表」——`match_items_indexed` 据此保持既有实体 id
        编号契约：命中分支没有任何参与命名组时回退整 match。真 ``re.Match`` 路径
        则由 :func:`_declared_named_groups` 从 ``match.re.groupindex`` 取**声明**
        组名，「声明了但全组未参与 ⇒ 不产出」只在该路径成立。
        """
        return [name for name, _start, _end, _text in self._named_groups]


# ---- 时间预算放缩与「match → 实体文本」语义（Issue#64 / Issue#65）----

#: 每 MB 文本给 2 秒（Issue#65 建议量级）。量级依据是 Issue#65 正文引用的 PR#63
#: 实测「0.14–0.19 s/MB」——该数字**在仓库内没有可复跑的仪器/环境脚注**，仅作
#: 量级参考，本模块不据此宣称任何对外性能结论。
TIMEOUT_SECONDS_PER_MEGABYTE = 2.0
TIMEOUT_BASE_SECONDS = 2.0
#: 预算上限。无上限时超长文档的单请求可挂到分钟级，而超时后
#: :func:`_kill_and_replace_pool` 的 ``shutdown(wait=False)`` **无法终止已在运行的
#: worker**，会留下继续烧 CPU 的孤儿进程；120 s 已远超 2 s/MB 线性下 60 MB 文本的
#: 名义预算。
TIMEOUT_MAX_SECONDS = 120.0


def scaled_timeout(text: str, base: float = TIMEOUT_BASE_SECONDS) -> float:
    """按文本长度放缩的 finditer 时间预算（Issue#65）。

    返回 ``min(TIMEOUT_MAX_SECONDS, max(base, len(text) / 1e6 *
    TIMEOUT_SECONDS_PER_MEGABYTE))``：每 MB 文本给 2 秒，短文本仍用 *base*，
    超长文本被 :data:`TIMEOUT_MAX_SECONDS` 封顶。
    """
    megabytes = len(text) / 1_000_000.0
    return min(TIMEOUT_MAX_SECONDS, max(base, megabytes * TIMEOUT_SECONDS_PER_MEGABYTE))


def _declared_named_groups(match: "re.Match[str] | _MatchProxy") -> list[str]:
    """该 match 的 pattern **声明**的命名组名（声明顺序）。

    ``_MatchProxy``（子进程结果）直接带声明表；真 ``re.Match`` 从
    ``match.re.groupindex`` 取（``{name: group_number}``，按组号升序 = 声明顺序）。
    """
    declared = getattr(match, "declared_named_groups", None)
    if callable(declared):
        return list(declared())
    groupindex = getattr(getattr(match, "re", None), "groupindex", None) or {}
    return sorted(groupindex, key=groupindex.__getitem__)


def _named_groups_from_real_match(
    match: "re.Match[str]", declared: list[str]
) -> list[tuple[str, int, int, str]]:
    """真 ``re.Match`` → 参与匹配的命名组（声明顺序、空组占位）。

    与子进程侧逐字同规则（``m.start(name) != -1`` 才算参与），保证
    ``_MatchProxy`` 与真 match 两条路径的编号基准一致（Issue#156 A10）。
    """
    out: list[tuple[str, int, int, str]] = []
    for name in declared:
        try:
            start = match.start(name)
        except (IndexError, re.error):  # 组名不属于该 pattern（理论不可达）
            continue
        if start == -1:
            continue
        out.append((name, start, match.end(name), match.group(name) or ""))
    return out


def match_items_indexed(
    match: "re.Match[str] | _MatchProxy",
) -> list[tuple[int, str, int, int]]:
    """同 :func:`match_items`，但附**命名组声明顺序下标**（Issue#64 编号契约）。

    返回 ``[(group_index, text, start, end), ...]``。命名组命中时 ``group_index``
    是该组在 ``named_groups()``（按组声明顺序、只含**参与匹配**的组）中的位置，
    **空组照样占位**——与旧实现
    ``for sub_index, (...) in enumerate(named_groups): if not text: continue``
    的编号基准逐字一致（Issue#52 起 ``regex_{type}_{i}_{j}`` 是对外契约）。
    无命名组时下标恒为 0，调用方用 ``named_groups`` 是否为空决定是否带后缀。

    语义本体只在这里实现一次：:func:`match_items` 是本函数的去下标视图。
    """
    declared = _declared_named_groups(match)
    named = (
        match.named_groups()
        if hasattr(match, "named_groups")
        else _named_groups_from_real_match(match, declared)
    )
    if declared:
        return [
            (index, text, start, end)
            for index, (_name, start, end, text) in enumerate(named)
            if text
        ]
    whole = match.group() or ""
    if not whole:
        return []
    return [(0, whole, match.start(), match.end())]


def match_items(match: "re.Match[str] | _MatchProxy") -> list[tuple[str, int, int]]:
    """一次正则匹配 → ``[(text, start, end), ...]``（Issue#64 唯一事实源）。

    命名组语义（与 Issue#52 文本链一致）：**有参与匹配的命名组**时，每个非空
    组各出一项——区间 pattern 的 ``seg_a``/``seg_b`` 拆成首尾两项、出生语境
    pattern 的 ``birth_date*`` 只出日期，分隔符与语境词不入实体；声明了命名组
    但全组为空 ⇒ 不产出、不回退整 match（**只适用于真 ``re.Match`` 路径**；
    ``_MatchProxy`` 路径见下文分歧说明）；**无命名组** ⇒ 整 match 非空时出一项。

    文本链（``hybrid_ner_service._custom_regex_extract``）、vision 兜底
    （``ocr_has_vision_service._apply_regex_fallback``）与文档级链
    （``text_layer_docline_service``）共用本函数（或它的带下标版本），避免各链
    粒度分歧——vision 侧曾直接用 ``m.group(0)``，把「出生日期：」「至」等
    语境词/分隔符整段吞成伪实体（Issue#64，欠遮盖方向）。

    兼容 ``_MatchProxy``（子进程结果，只带**参与**组）与**真 ``re.Match``**
    （Issue#156：声明组从 ``match.re.groupindex`` 取，参与判据与子进程侧同规则
    ``m.start(name) != -1``）。两条路径在「命中分支没有任何参与命名组」时**有意分歧**：

    * 真 ``re.Match`` 路径——pattern **声明过**命名组而本轮全未参与 ⇒ 不产出、不回退；
    * ``_MatchProxy`` 路径——跨进程边界不传声明表，按既有契约回退整 match
      （生产 preset ``DATE`` 的单日期分支正是此形态；在此回退消失即为漏打码，
      回归闸 ``tests/test_issue156_date_preset_regression.py`` 钉死）。

    该分歧是**设计选择**，不是缺陷：统一它需要改 preset（给单日期分支补命名组），
    属另一 Issue 的范围。
    """
    return [
        (text, start, end)
        for _index, text, start, end in match_items_indexed(match)
    ]


def safe_finditer(
    compiled: re.Pattern, text: str, timeout: float = 5.0
) -> list:
    """Run ``compiled.finditer(text)`` with a wall-clock *timeout*.

    Returns a list of lightweight match-like objects.
    Raises ``RegexTimeoutError`` on timeout.
    """
    pool = _get_pool()
    future = pool.submit(
        _finditer_in_process, compiled.pattern, compiled.flags, text
    )
    try:
        raw = future.result(timeout=timeout)
    except FuturesTimeoutError:
        future.cancel()
        _kill_and_replace_pool()
        _record_regex_timeout("safe_finditer")
        raise RegexTimeoutError(
            f"Regex finditer timed out after {timeout}s — "
            f"pattern may cause catastrophic backtracking (ReDoS)"
        )
    return [
        _MatchProxy(t, s, e, named) for t, s, e, named in raw
    ]


def safe_finditer_many(
    compiled: re.Pattern, texts: list[str], timeout: float = 5.0
) -> list[list]:
    """Run ``compiled.finditer`` over *texts* in ONE worker call (Issue#64).

    Returns one match list per input text, in order. Callers that scan many
    short texts (vision fallback: per OCR block) need the ReDoS timeout but
    must not pay one subprocess round-trip per text.

    Raises ``RegexTimeoutError`` on timeout (the whole batch is abandoned).
    """
    if not texts:
        return []
    pool = _get_pool()
    future = pool.submit(
        _finditer_many_in_process, compiled.pattern, compiled.flags, list(texts)
    )
    try:
        raw = future.result(timeout=timeout)
    except FuturesTimeoutError:
        future.cancel()
        _kill_and_replace_pool()
        _record_regex_timeout("safe_finditer_many")
        raise RegexTimeoutError(
            f"Regex finditer timed out after {timeout}s over {len(texts)} texts — "
            f"pattern may cause catastrophic backtracking (ReDoS)"
        )
    return [
        [_MatchProxy(t, s, e, named) for t, s, e, named in rows]
        for rows in raw
    ]


def _kill_and_replace_pool() -> None:
    """Shut down the stuck process pool and create a fresh one."""
    global _pool
    if _pool is not None:
        _pool.shutdown(wait=False, cancel_futures=True)
        _pool = None
