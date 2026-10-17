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
    """A10：跨 alternation 的参与/不参与，两条路径的**参与集合**必须一致。

    编号约定按设计不同（不是缺陷，且谁都不许改生产契约）：
    - 子进程/代理路径 = ``enumerate(参与组)``（参与组出现序）= 生产既有实体 id 契约；
    - 真 match 路径 = ``re.groupindex`` 的**声明序**（只有真 match 拿得到声明表）。

    真 match 路径在生产不可达（生产 match 全部来自 ``safe_finditer*`` 子进程），
    故该差异不影响生产编号；但两条路径的**参与集合**（文本与 span）必须一致。
    """
    pattern = r"(?P<x>foo)|(?P<y>bar)"
    for text in ("foo", "bar"):
        proxy = safe_finditer(safe_compile(pattern), text)[0]
        real = re.match(pattern, text)
        assert real is not None
        # 两条路径连编号都一致（都按「参与组出现序」编号）；差异只出现在
        # 「命中分支无任何命名组」这一混合分支形态上，见下一个用例。
        assert match_items_indexed(proxy) == match_items_indexed(real), text


def test_proxy_keeps_legacy_whole_match_fallback_for_group_less_branch() -> None:
    """生产契约：子进程/代理路径对「pattern 声明了组、但命中分支无组」**回退整 match**。

    依据：preset ``DATE`` 的单日期分支无命名组、区间分支有 ``seg_a/seg_b``；
    若代理路径也套用 A8 的「声明但未参与 ⇒ 不产出、不回退整 match」，单日期实体会被
    整条吞掉（欠遮盖 = 漏打码）。故 A8 规则**只作用于真 match 路径**；要把两条路径
    统一，必须先改 preset（让单日期分支也带命名组），属另立 Issue。
    本用例把生产契约钉死，防止再次误改。
    """
    pattern = r"(?:(?P<a>foo)|bar)"
    proxy = safe_finditer(safe_compile(pattern), "bar")[0]
    assert match_items(proxy) == [("bar", 0, 3)]
    assert match_items_indexed(proxy) == [(0, "bar", 0, 3)]


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
