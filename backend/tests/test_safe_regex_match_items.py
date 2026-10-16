"""`safe_regex.match_items` 与 `scaled_timeout`（Issue#64 / Issue#65）。

`match_items` 是「一次正则匹配 → 待遮盖文本」的**唯一事实源**：文本链
（`hybrid_ner_service._custom_regex_extract`）与 vision 兜底
（`ocr_has_vision_service._apply_regex_fallback`）都经它产出实体文本。旧
vision 实现取 `m.group(0)`，会把「出生日期：」语境词、区间分隔符整段吞成
伪实体（欠遮盖方向，Issue#64）。

本文件只构造 `_MatchProxy` / 真 `re.Match`，**不碰进程池**，故任何环境都能跑。
"""

from __future__ import annotations

import re

from app.core.safe_regex import (
    TIMEOUT_BASE_SECONDS,
    _MatchProxy,
    match_items,
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


# ── scaled_timeout：预算放缩 ───────────────────────────────

def test_scaled_timeout_keeps_base_for_short_text():
    assert scaled_timeout("") == TIMEOUT_BASE_SECONDS
    assert scaled_timeout("短文本") == TIMEOUT_BASE_SECONDS
    # 恰好 0.5M 字符 ⇒ 1.0s < base 2.0s，仍取 base
    assert scaled_timeout("x" * 500_000) == TIMEOUT_BASE_SECONDS


def test_scaled_timeout_grows_two_seconds_per_megabyte():
    """1M 字符 ⇒ 2.0s，5M 字符 ⇒ 10.0s（2 s/MB，对照实测 0.14–0.19 s/MB ≈10×）。"""
    assert scaled_timeout("x" * 1_000_000) == 2.0
    assert scaled_timeout("x" * 5_000_000) == 10.0
    assert scaled_timeout("x" * 10_000_000) > scaled_timeout("x" * 1_000_000)


def test_scaled_timeout_base_override_still_applies():
    assert scaled_timeout("x" * 10_000, base=0.5) == 0.5