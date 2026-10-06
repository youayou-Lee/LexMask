"""日期形态正则模块（Issue#52）：中文数字/混排/缺省形态/区间/农历别称/出生语境。

规则本体是纯配置——preset_entity_types.json 里 DATE / BIRTH_DATE 两条
regex_pattern 由 app/core/date_forms.py 的槽位生成，本文件的一致性测试
锁定两者；形态语义（命中/不命中/边界三档）也在这里钉死。
范围口径（2026-10-06 用户拍板）：相对日期词不认、裸年份不认、
斜杠/短横线日期与农历日不认（范围外，见 Issue#52）。
"""

import json
import re
from pathlib import Path

import pytest

from app.core.date_forms import BIRTH_DATE_FORMS_REGEX, DATE_FORMS_REGEX

_PRESET = json.loads(
    (Path(__file__).resolve().parents[1] / "config" / "preset_entity_types.json").read_text("utf-8")
)


def _date_matches(text: str) -> list[str]:
    return [m.group() for m in re.finditer(DATE_FORMS_REGEX, text)]


def _named_groups(pattern: str, text: str) -> list[dict[str, tuple[int, int, str]]]:
    """每个 match 的「参与匹配的命名组」：{name: (start, end, text)}。"""
    compiled = re.compile(pattern)
    out = []
    for m in compiled.finditer(text):
        groups = {
            name: (m.start(name), m.end(name), m.group(name))
            for name in compiled.groupindex
            if m.span(name) != (-1, -1)
        }
        out.append(groups)
    return out


# ---------- 一致性锁：preset JSON == 模块生成 ----------

def test_preset_patterns_match_module():
    assert _PRESET["DATE"]["regex_pattern"] == DATE_FORMS_REGEX
    assert _PRESET["BIRTH_DATE"]["regex_pattern"] == BIRTH_DATE_FORMS_REGEX


def test_patterns_compile():
    re.compile(DATE_FORMS_REGEX)
    re.compile(BIRTH_DATE_FORMS_REGEX)


# ---------- DATE：全形态命中 ----------

@pytest.mark.parametrize(
    "sentence, expected",
    [
        # 中文数字：〇系 / 十系 / OCR 混淆零
        ("本院于二〇二二年一月二十五日立案受理。", "二〇二二年一月二十五日"),
        ("被告一九九二年十月五日到庭。", "一九九二年十月五日"),
        ("二0二二年三月十五日", "二0二二年三月十五日"),
        ("二O二二年三月十五日", "二O二二年三月十五日"),
        # 混排
        ("协议签订于2022年一月二十五日。", "2022年一月二十五日"),
        ("二〇二二年1月25日", "二〇二二年1月25日"),
        # 既有全形态收编：空格 / 跨行
        ("我院于2022 年01 月25 日受理", "2022 年01 月25 日"),
        ("于2022 年1 月\n26 日告知被告人", "2022 年1 月\n26 日"),
        # 边界：年份首尾 / 零填充 / 全角
        ("2022年12月31日", "2022年12月31日"),
        ("2022年01月05日", "2022年01月05日"),
        ("１月１日", "１月１日"),
        # 缺省形态：年月（含「份」后缀）
        ("自2022年1月起租期三年。", "2022年1月"),
        ("二〇二二年三月", "二〇二二年三月"),
        ("该办法自2022年3月份起施行。", "2022年3月份"),
        # 缺省形态：月日
        ("1月25日", "1月25日"),
        ("十二月二十五日", "十二月二十五日"),
    ],
)
def test_date_forms_hit(sentence, expected):
    assert expected in _date_matches(sentence)


# ---------- DATE：农历别称月 ----------

@pytest.mark.parametrize(
    "sentence, expected",
    [("于腊月期间", "腊月"), ("当年冬月", "冬月"), ("正月初五", "正月")],
)
def test_lunar_alias_months_hit(sentence, expected):
    assert expected in _date_matches(sentence)


# ---------- DATE：区间首尾都框（命名组拆分） ----------

def test_interval_frames_both_ends_as_named_groups():
    text = "本案于2022年1月1日至1月31日期间审理"
    hits = _named_groups(DATE_FORMS_REGEX, text)
    assert len(hits) == 1, "整个区间应为一个 match"
    groups = hits[0]
    assert len(groups) == 2, "首尾两组各出一个实体"
    seg_texts = {g[2] for g in groups.values()}
    assert seg_texts == {"2022年1月1日", "1月31日"}
    for start, end, seg_text in groups.values():
        assert "至" not in seg_text, "分隔符不属于任何实体"
        assert text[start:end] == seg_text


@pytest.mark.parametrize(
    "sentence, first, second",
    [
        ("2022年1月1日到2022年3月31日", "2022年1月1日", "2022年3月31日"),
        ("2022年1月1日～1月31日", "2022年1月1日", "1月31日"),
        ("2022年1月1日至31日", "2022年1月1日", "31日"),  # 缺省尾段：纯日
        ("2022年1月至3月", "2022年1月", "3月"),  # 缺省尾段：纯月
        ("1月1日至3月31日", "1月1日", "3月31日"),  # 首段也无年
    ],
)
def test_interval_separator_and_deficient_tails(sentence, first, second):
    hits = _named_groups(DATE_FORMS_REGEX, sentence)
    assert len(hits) == 1
    assert {g[2] for g in hits[0].values()} == {first, second}


def test_interval_without_tail_falls_back_to_single_form():
    text = "自2022年1月1日起至"
    hits = _named_groups(DATE_FORMS_REGEX, text)
    assert len(hits) == 1
    assert hits[0] == {}, "半截区间不应产生命名组，退化为普通全形态"
    assert _date_matches(text) == ["2022年1月1日"]


# ---------- DATE：完整形态不产生嵌套重复框 ----------

def test_full_form_yields_single_match_no_nested_frames():
    """交替分支顺序（区间>全形态>年月>月日）下，完整日期只出一个 match。"""
    assert _date_matches("2022年1月25日") == ["2022年1月25日"]
    assert _date_matches("二〇二二年一月二十五日") == ["二〇二二年一月二十五日"]


# ---------- DATE：跨行 / 空格容忍（各形态至少一例，#66 教训） ----------

@pytest.mark.parametrize(
    "sentence, expected",
    [
        ("于二〇二二年一月\n二十五日作出判决", "二〇二二年一月\n二十五日"),
        ("自2022年1月1日至\n1月31日止", "2022年1月1日至\n1月31日"),
        ("被告人于腊\n月作案", "腊\n月"),
        ("2022　年1　月25　日", "2022　年1　月25　日"),  # 全角空格
    ],
)
def test_cross_line_and_fullwidth_space_tolerated(sentence, expected):
    assert expected in _date_matches(sentence)


# ---------- DATE：限界负例（越界值 / 非法组合的口径） ----------

@pytest.mark.parametrize(
    "text, expected",
    [
        ("2022年0月5日", []),  # 0 月非法，整体不框
        ("0日", []),  # 纯日本就不单独出框
        # 非法日值拖垮全形态/月日分支，但有效的年月前缀仍出框（不陪葬）
        ("2022年1月32日", ["2022年1月"]),
    ],
)
def test_out_of_range_values(text, expected):
    assert _date_matches(text) == expected


# ---------- DATE：误报与范围外（不命中三档） ----------

@pytest.mark.parametrize(
    "text",
    [
        "2022版",  # 裸年份+版
        "2022年版",
        "2022年以来",
        "近三年",
        "3个工作日",
        "31日",  # 单独纯日（只在区间尾段合法）
        "10月",  # 单独纯月
        "50月13日",  # 月份越界
        "13月",  # 月份越界
        "2022年13月45日",  # 非法组合整体不框
        "2022年",  # 裸年份
        "正在月报",  # 「正月」形近误报
        # 相对日期词（2026-10-06 拍板：本期不认，钉住）
        "同年",
        "同月",
        "同年同月",
        "次月",
        "当月",
        "同日",
        "次年",
        "次月3日",
    ],
)
def test_non_date_forms_miss(text):
    assert _date_matches(text) == []


# ---------- BIRTH_DATE：出生语境，实体只含日期组 ----------

@pytest.mark.parametrize(
    "sentence, expected",
    [
        ("出生日期：1992年10月5日。", "1992年10月5日"),
        ("张三，生于一九九二年十月五日，汉族", "一九九二年十月五日"),
        ("生日：1992年10月5日", "1992年10月5日"),
        ("1985年7月15日出生", "1985年7月15日"),
        ("张三（男，1985年7月15日出生，汉族）", "1985年7月15日"),
        ("出生日期为二〇〇二年一月二十五日", "二〇〇二年一月二十五日"),
        ("出生：1992年10月5日", "1992年10月5日"),  # 半角冒号连接
        ("出生年月：1992年10月5日", "1992年10月5日"),
        ("出生年月日：1992年10月5日", "1992年10月5日"),  # 评审 Important#1：年月日变体
        ("出生在一九九〇年十月五日", "一九九〇年十月五日"),  # 评审 Important#1：「在」连接
        ("出生于一九九二年十月五日", "一九九二年十月五日"),
    ],
)
def test_birth_context_frames_date_group_only(sentence, expected):
    compiled = re.compile(BIRTH_DATE_FORMS_REGEX)
    matches = list(compiled.finditer(sentence))
    assert len(matches) == 1
    participating = [
        name for name in compiled.groupindex if matches[0].span(name) != (-1, -1)
    ]
    assert len(participating) == 1, "实体=日期组，语境前缀/后缀不进实体"
    assert matches[0].group(participating[0]) == expected


@pytest.mark.parametrize(
    "text",
    [
        "1992年10月5日双方签订协议",  # 裸事件日期：仅出生语境开时不得误框
        "本院于2022年1月25日受理",
        "出生于北京市海淀区",  # 语境后无日期
        "出生日期不详",
        "生于1992年",  # 出生语境 × 裸年：全形态不含年 → 不框
        "生于1月25日",  # 出生语境 × 缺省形态：口径=仅全形态，缺省交语义层
    ],
)
def test_birth_pattern_misses_without_birth_context(text):
    assert list(re.finditer(BIRTH_DATE_FORMS_REGEX, text)) == []
