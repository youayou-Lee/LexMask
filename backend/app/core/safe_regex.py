"""
Safe regex utilities with timeout protection against ReDoS
(catastrophic backtracking).

Uses ThreadPoolExecutor to enforce wall-clock timeouts on both
compile+probe and finditer, since Python's ``re`` module does not
natively support cancellation.
"""

import multiprocessing
import re
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError


class RegexTimeoutError(ValueError):
    """Raised when a regex operation exceeds its time budget."""


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
    interval / birth-context patterns emit per-group entities).
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


# ---- 时间预算放缩与「match → 实体文本」语义（Issue#64 / Issue#65）----

#: 正则兜底实测成本 ≈0.14–0.19 s/MB（Issue#52 实测，随文本线性增长）。
#: 历史实现写死 2.0s，按线性外推约 10–14MB 文本就会超时并静默丢弃整篇
#: 保证层（Issue#65）。按 2 s/MB 放缩 ⇒ 约 10× 实测余量，下限保留 2.0s。
TIMEOUT_SECONDS_PER_MB = 1.0
TIMEOUT_BASE_SECONDS = 2.0


def scaled_timeout(text: str, base: float = TIMEOUT_BASE_SECONDS) -> float:
    """按文本长度放缩的 finditer 时间预算（Issue#65）。

    返回 ``max(base, len(text) / 5e5 * TIMEOUT_SECONDS_PER_MB)``：每 MB 文本
    给 2 秒（对照实测 0.14–0.19 s/MB ≈10× 余量），短文本仍用 *base*。
    """
    megabytes = len(text) / 500_000.0
    return max(base, megabytes * TIMEOUT_SECONDS_PER_MB)


def match_items(match: "re.Match[str] | _MatchProxy") -> list[tuple[str, int, int]]:
    """一次正则匹配 → ``[(text, start, end), ...]``（Issue#64 唯一事实源）。

    命名组语义（与 Issue#52 文本链一致）：**有参与匹配的命名组**时，每个非空
    组各出一项——区间 pattern 的 ``seg_a``/``seg_b`` 拆成首尾两项、出生语境
    pattern 的 ``birth_date*`` 只出日期，分隔符与语境词不入实体；声明了命名组
    但全组为空 ⇒ 不产出（保持文本链既有语义，不回退整 match）；**无命名组** ⇒
    整 match 非空时出一项。

    文本链（``hybrid_ner_service._custom_regex_extract``）与 vision 兜底
    （``ocr_has_vision_service._apply_regex_fallback``）共用本函数，避免两处
    粒度分歧——vision 侧曾直接用 ``m.group(0)``，把「出生日期：」「至」等
    语境词/分隔符整段吞成伪实体（Issue#64，欠遮盖方向）。

    兼容 ``_MatchProxy``（有 ``named_groups``）与真 ``re.Match``（无 ⇒ 按
    无命名组处理）。
    """
    named = match.named_groups() if hasattr(match, "named_groups") else []
    if named:
        return [(text, start, end) for _name, start, end, text in named if text]
    whole = match.group() or ""
    if not whole:
        return []
    return [(whole, match.start(), match.end())]


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
        raise RegexTimeoutError(
            f"Regex finditer timed out after {timeout}s — "
            f"pattern may cause catastrophic backtracking (ReDoS)"
        )
    return [_MatchProxy(t, s, e, named) for t, s, e, named in raw]


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
        raise RegexTimeoutError(
            f"Regex finditer timed out after {timeout}s over {len(texts)} texts — "
            f"pattern may cause catastrophic backtracking (ReDoS)"
        )
    return [[_MatchProxy(t, s, e, named) for t, s, e, named in rows] for rows in raw]


def _kill_and_replace_pool() -> None:
    """Shut down the stuck process pool and create a fresh one."""
    global _pool
    if _pool is not None:
        _pool.shutdown(wait=False, cancel_futures=True)
        _pool = None
