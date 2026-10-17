"""`safe_regex.match_items` 与 `scaled_timeout`（Issue#64 / Issue#65）。

`match_items` / `match_items_indexed` 是「一次正则匹配 → 待遮盖文本」的**唯一事实
源**：文本链（`hybrid_ner_service._custom_regex_extract`）、vision 兜底
（`ocr_has_vision_service._apply_regex_fallback`）与文档级 docline 保证层
（`text_layer_docline_service`）都经它产出实体文本。旧 vision 实现取
`m.group(0)`，会把「出生日期：」语境词、区间分隔符整段吞成伪实体（欠遮盖方向，
Issue#64）。

本文件只构造 `_MatchProxy` / 真 `re.Match`，**不碰进程池**，故任何环境都能跑。
"""

from __future__ import annotations

import re

from app.core import safe_regex
from app.core.safe_regex import (
    TIMEOUT_BASE_SECONDS,
    TIMEOUT_MAX_SECONDS,
    _MatchProxy,
    match_items,
    match_items_indexed,
    safe_compile,
    safe_finditer,
    safe_finditer_many,
    scaled_timeout,
)

# ── match_items：命名组语义 ────────────────────────────────

def test_named_groups_yield_one_item_each_without_separator():
    """区间 pattern：seg_a/seg_b 各出一项，分隔符「至」不入任何一项。"""
    text = "本案于2022年1月1日至1月31日期间审理"
    seg_a, seg_b = "2022年1月1日", "1月31日"
    start_a = text.index(seg_a)
    start_b = text.index(seg_b)
    match = _MatchProxy(
        text, start_a, start_b + len(seg_b),
        [("seg_a", start_a, start_a + len(seg_a), seg_a),
         ("seg_b", start_b, start_b + len(seg_b), seg_b)],
    )

    items = match_items(match)

    assert items == [
        (seg_a, start_a, start_a + len(seg_a)),
        (seg_b, start_b, start_b + len(seg_b)),
    ]
    assert all("至" not in item_text for item_text, _s, _e in items)


def test_birth_context_group_excludes_context_words():
    """出生语境 pattern：只出 birth_date 组，语境词「出生日期：」不入。"""
    text = "出生日期：1992年10月5日。"
    date_part = "1992年10月5日"
    start = text.index(date_part)
    match = _MatchProxy(
        text, 0, len(text),
        [("birth_date", start, start + len(date_part), date_part)],
    )

    assert match_items(match) == [(date_part, start, start + len(date_part))]


def test_declared_group_all_empty_yields_nothing():
    """声明了命名组但组全为空 ⇒ 不产出，且**不回退整 match**。

    这是文本链既有语义（Issue#52）：pattern 一旦声明命名组，输出粒度就以组为
    准；组全空意味着「这次匹配没有可遮盖的部分」。
    """
    match = _MatchProxy("abc", 0, 3, [("maybe", 0, 0, "")])

    assert match_items(match) == []


def test_no_named_groups_returns_whole_match():
    """无命名组 ⇒ 整 match 一项（车牌/案号这类未分组的 pattern）。"""
    text = "悬挂粤RH0472号牌"
    # 预设 LICENSE_PLATE 形如 `[省][A-Z]\s*[A-Z0-9]{4,5}号?牌?`，故整 match 含「号牌」
    # `_MatchProxy` 第一参数是**命中的那段文本**，start/end 是它在原文里的绝对偏移
    matched = "粤RH0472号牌"
    start = text.index(matched)
    match = _MatchProxy(matched, start, start + len(matched), [])

    assert match_items(match) == [(matched, start, start + len(matched))]


def test_empty_whole_match_yields_nothing():
    """整 match 为空串 ⇒ 不产出（旧 vision 实现靠 `.strip()` 挡住的同一情形）。"""
    assert match_items(_MatchProxy("", 5, 5, [])) == []


def test_real_re_match_is_treated_as_unescaped_whole_match():
    """真 `re.Match` 没有 `named_groups()` ⇒ 按无命名组处理，返回整 match。"""
    match = re.compile(r"a+").search("baaa")

    assert match_items(match) == [("aaa", 1, 4)]


# ── match_items_indexed：声明顺序编号基准（冷审 I1）─────────

def test_indexed_keeps_declaration_order_when_leading_group_is_empty():
    """空声明组**占位**：旧实现按 `enumerate(named_groups)` 编号，故首个非空组的下标
    是 1 而不是 0。租户自定义 pattern（如 `(?P<opt>X*)(?P<date>\\d{4})`）可命中此情形，
    首版实现「先滤空再编号」会让已对外承诺的实体 id 漂移（冷审 I1）。
    """
    date = "1992"
    match = _MatchProxy(
        "1992", 0, 4,
        [("opt", 0, 0, ""), ("date", 0, 4, date)],
    )

    assert match_items_indexed(match) == [(1, date, 0, 4)]
    # 去下标视图不受影响
    assert match_items(match) == [(date, 0, 4)]


def test_indexed_multiple_groups_keep_declaration_index():
    match = _MatchProxy(
        "ab", 0, 2, [("first", 0, 1, "a"), ("second", 1, 2, "b")],
    )

    assert match_items_indexed(match) == [(0, "a", 0, 1), (1, "b", 1, 2)]


def test_subprocess_payload_keeps_declaration_order():
    """端到端钉住**真子进程 payload** 的命名组取序（r1 自造变异 ① 的补丁）。

    走 `safe_finditer`（真子进程路径，非手工 `_MatchProxy`），两个命名组的编号与顺序
    必须按声明序；倒置取序应当被判红（见 `_i156/mutation.txt` 的 M9）。
    """
    assert match_items_indexed(safe_finditer(safe_compile(r"(?P<first>ab)(?P<second>cd)"), "abcd")[0]) == [
        (0, "ab", 0, 2),
        (1, "cd", 2, 4),
    ]
    assert match_items_indexed(safe_finditer(safe_compile(r"(?P<c>\d{4})-(?P<d>\d{2})"), "2026-10")[0]) == [
        (0, "2026", 0, 4),
        (1, "10", 5, 7),
    ]


def test_indexed_without_named_groups_uses_index_zero():
    match = _MatchProxy("粤RH0472号牌", 2, 11, [])

    assert match_items_indexed(match) == [(0, "粤RH0472号牌", 2, 11)]


# ── scaled_timeout：预算放缩 ───────────────────────────────

def test_scaled_timeout_keeps_base_for_short_text():
    assert scaled_timeout("") == TIMEOUT_BASE_SECONDS
    assert scaled_timeout("短文本") == TIMEOUT_BASE_SECONDS
    # 恰好 0.5M 字符 ⇒ 1.0s < base 2.0s，仍取 base
    assert scaled_timeout("x" * 500_000) == TIMEOUT_BASE_SECONDS


def test_scaled_timeout_grows_two_seconds_per_megabyte():
    """1M 字符 ⇒ 2.0s，5M 字符 ⇒ 10.0s（2 s/MB）。

    对照口径：Issue#65 正文记录的 PR#63 评审期实测 0.14–0.19 s/MB（线性），
    故 2 s/MB 约留 10× 余量；该实测无独立仪器附注，只作量级参考。
    """
    assert scaled_timeout("x" * 1_000_000) == 2.0
    assert scaled_timeout("x" * 5_000_000) == 10.0
    assert scaled_timeout("x" * 10_000_000) > scaled_timeout("x" * 1_000_000)


def test_scaled_timeout_base_override_still_applies():
    assert scaled_timeout("x" * 10_000, base=0.5) == 0.5


def test_scaled_timeout_is_capped():
    """放缩预算必须有上限（冷审 N2）：100 MB 不再线性外推到 200 s。"""
    assert scaled_timeout("x" * 100_000_000) == TIMEOUT_MAX_SECONDS
    assert scaled_timeout("x" * 200_000_000) == TIMEOUT_MAX_SECONDS


# ── safe_finditer_many：空输入短路（冷审 N3）───────────────

def test_safe_finditer_many_empty_texts_short_circuits(monkeypatch):
    """空块列表必须直接返回 `[]`，**不建池**、不碰子进程（空白页高频路径）。

    冷审 M-2：只断言返回值在能建池的 CI 上没有杀变力（删掉短路后
    `_finditer_many_in_process([])` 同样返回 `[]`），故这里把 `_get_pool`
    换成会炸的替身——只要空输入走了建池路径，本用例立刻失败，任何环境都成立。
    """

    def _boom():
        raise AssertionError("空输入不得建池（safe_finditer_many 应先短接）")

    monkeypatch.setattr(safe_regex, "_get_pool", _boom)
    assert safe_finditer_many(re.compile("x"), []) == []
