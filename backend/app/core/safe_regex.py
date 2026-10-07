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


def _finditer_in_process(
    pattern_str: str, flags: int, text: str
) -> list[tuple[str, int, int, list[tuple[str, int, int, str]]]]:
    """Run finditer in a subprocess and return serialisable tuples.

    Per match: whole-match text/start/end plus the participating named
    groups as ``(name, start, end, text)`` with absolute offsets (Issue#52:
    interval / birth-context patterns emit per-group entities).
    """
    compiled = re.compile(pattern_str, flags)
    group_order = sorted(compiled.groupindex, key=compiled.groupindex.get)
    out = []
    for m in compiled.finditer(text):
        named = [
            (name, m.start(name), m.end(name), m.group(name))
            for name in group_order
            if m.start(name) != -1
        ]
        out.append((m.group(), m.start(), m.end(), named))
    return out


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


def _kill_and_replace_pool() -> None:
    """Shut down the stuck process pool and create a fresh one."""
    global _pool
    if _pool is not None:
        _pool.shutdown(wait=False, cancel_futures=True)
        _pool = None
