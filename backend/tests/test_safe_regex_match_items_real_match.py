"""Issue#156 项②：`match_items*` 对**真 `re.Match`** 的命名组语义。

PR#152 评审 A 组边界里 A2/A8/A10 当时挂账，其中 A8 的关键是**真 pattern**：
`(?:(?P<a>foo)|bar)` 命中 `bar` 时，声明了命名组但一个都没参与 ⇒ 必须
**不产出、也不回退整 match**（回退会把分支字面量当实体，欠/过遮盖都错）。
"""
import re

from app.core.safe_regex import match_items, match_items_indexed, safe_compile, safe_finditer


def test_real_match_declaration_order_and_empty_slot() -> None:
    """真 match：命名组按声明序占位；可选组未参与时不占位。"""
    hit = re.match(r"(?P<a>foo)(?P<b>bar)?", "foobar")
    assert hit is not None
    assert match_items_indexed(hit) == [(0, "foo", 0, 3), (1, "bar", 3, 6)]

    only_first = re.match(r"(?P<a>foo)(?P<b>bar)?", "foo")
    assert only_first is not None
    assert match_items_indexed(only_first) == [(0, "foo", 0, 3)]


def test_real_match_declared_but_unparticipated_emits_nothing() -> None:
    """A8：声明了命名组但全组未参与 ⇒ 空（不得回退成整 match）。"""
    hit = re.match(r"(?:(?P<a>foo)|bar)", "bar")
    assert hit is not None and hit.group() == "bar"
    assert match_items(hit) == []
    assert match_items_indexed(hit) == []


def test_proxy_and_real_match_agree_on_alternation_participation() -> None:
    """A10：跨 alternation 的参与/不参与，两条路径给同一答案。"""
    pattern = r"(?P<x>foo)|(?P<y>bar)"
    for text in ("foo", "bar"):
        proxy = safe_finditer(safe_compile(pattern), text)[0]
        real = re.match(pattern, text)
        assert real is not None
        assert match_items_indexed(proxy) == match_items_indexed(real), text


def test_no_named_groups_still_emits_whole_match_both_paths() -> None:
    pattern = r"(?:foo)|bar"
    proxy = safe_finditer(safe_compile(pattern), "bar")[0]
    real = re.match(pattern, "bar")
    assert real is not None
    assert match_items(proxy) == [("bar", 0, 3)]
    assert match_items(real) == [("bar", 0, 3)]


def test_birth_context_suffix_only_emits_date() -> None:
    """A2：后缀语境（「DATE 出生」）不得把语境词吞成实体。

    反面对照（改前形态：给语境词也起组名）会多出一项 ⇒ 本用例确实有判别力。
    """
    fixed = r"(?P<birth_date>\d{4}年\d{1,2}月\d{1,2}日)\s*(?:出生|生于)?"
    proxy = safe_finditer(safe_compile(fixed), "1990年5月4日出生")[0]
    assert match_items(proxy) == [("1990年5月4日", 0, 9)]

    old_form = r"(?P<birth_date>\d{4}年\d{1,2}月\d{1,2}日)\s*(?P<ctx>出生|生于)?"
    old_proxy = safe_finditer(safe_compile(old_form), "1990年5月4日出生")[0]
    assert len(match_items(old_proxy)) == 2, "改前形态（语境词也成组）本会多出一项"
