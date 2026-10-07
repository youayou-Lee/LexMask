"""safe_regex 命名捕获组传输（Issue#52）：

子进程隔离的 finditer 只回传整段 span，命名组 span 需要一并带回——
区间拆首尾、出生语境只出日期组都依赖它。既有 group()/start()/end()
语义不变（向后兼容，唯一使用方是 hybrid Stage 2）。
"""

import pytest

from app.core.safe_regex import RegexTimeoutError, safe_compile, safe_finditer


def test_named_groups_transport_across_subprocess():
    compiled = safe_compile(r"(?P<a>\d+)-(?P<b>[a-z]+)")
    matches = safe_finditer(compiled, " 12-ab 34-cd")
    assert len(matches) == 2
    groups = matches[0].named_groups()
    assert [(n, t) for n, _, _, t in groups] == [("a", "12"), ("b", "ab")]
    assert groups[0] == ("a", 1, 3, "12")  # 相对原文的绝对偏移
    assert matches[1].named_groups() == [("a", 7, 9, "34"), ("b", 10, 12, "cd")]


def test_unnamed_pattern_returns_empty_named_groups():
    compiled = safe_compile(r"\d+")
    matches = safe_finditer(compiled, "x 42 y")
    assert matches[0].named_groups() == []
    assert matches[0].group() == "42"
    assert (matches[0].start(), matches[0].end()) == (2, 4)


def test_optional_group_not_participating_is_omitted():
    compiled = safe_compile(r"(?:(?P<seg>\d\d))?(?P<always>x)")
    matches = safe_finditer(compiled, "yy xz 12x")
    assert len(matches) == 2
    # 第一个 match 的 seg 未参与 → 只回传参与组
    assert [(n, t) for n, _, _, t in matches[0].named_groups()] == [("always", "x")]
    assert [(n, t) for n, _, _, t in matches[1].named_groups()] == [
        ("seg", "12"),
        ("always", "x"),
    ]


def test_compile_timeout_still_enforced():
    with pytest.raises(RegexTimeoutError):
        safe_compile(r"(a+)+$")
