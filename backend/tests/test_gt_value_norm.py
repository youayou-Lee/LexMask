# Issue#56 P0 —— 数值/金额归一 value_norm.py 的离线单测。
# 零网络、零真实案卷数据（全部合成数值）。
#
# 口径：归一到 (数值, 单位) 后**必须相等，无粒度容忍**——与日期正相反。
# 理由是脱敏场景里数值差一位就是漏遮蔽（hardcase 数字分级实测：银行卡号 exact 仅 14.3%）。
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "eval"))
from gt import value_norm  # noqa: E402


# ---- 金额的等价写法 --------------------------------------------------------

@pytest.mark.parametrize("a,b", [
    ("10000元", "1万元"),
    ("10000元", "1万元整"),
    ("10000元", "壹万元整"),
    ("10000元", "人民币10000元"),
    ("10000元", "10,000元"),
    ("10000元", "10000块"),
    ("15000元", "1.5万元"),
    ("1000元", "10000角"),
    ("1000元", "1000元整"),
])
def test_amount_equivalent_forms(a, b):
    r = value_norm.value_equal(a, b, kind="amount")
    assert r["verdict"] == "same", r["reason"]


def test_mixed_arabic_and_scale_word():
    """「3万5千」是真实案卷常见写法——早期只认纯阿拉伯前缀，会读成 3 配单位「万5千」。"""
    assert value_norm.normalize_number("3万5千元", kind="amount") == {
        "value": "35000", "unit": "元"}
    assert value_norm.value_equal("3万5千元", "35000元", "amount")["verdict"] == "same"


@pytest.mark.parametrize("raw,expect", [
    ("三万五千元", "35000"),
    ("一百零五元", "105"),
    ("壹万元", "10000"),
    ("二〇二三万元", "20230000"),
])
def test_chinese_numerals(raw, expect):
    assert value_norm.normalize_number(raw, kind="amount")["value"] == expect


def test_decimal_is_exact_not_float():
    """用 Decimal 而非 float：0.07 万元不能变成 700.0000000000001。"""
    got = value_norm.normalize_number("0.07万元", kind="amount")
    assert got == {"value": "700", "unit": "元"}
    assert isinstance(got["value"], str), "value 必须是字符串（Decimal 精确 + 可 JSON 序列化）"


# ---- 值差必须判不同 --------------------------------------------------------

@pytest.mark.parametrize("a,b", [
    ("10000元", "100000元"),   # 差一个 0
    ("10000元", "10001元"),    # 差一位
    ("3万5千元", "3万6千元"),
])
def test_value_difference_is_different(a, b):
    r = value_norm.value_equal(a, b, kind="amount")
    assert r["verdict"] == "different"
    assert "数值不等" in r["reason"]


def test_foreign_currency_never_equated():
    """没有汇率就不得判等——「保守正确」胜过「聪明地猜」。"""
    r = value_norm.value_equal("10000港币", "10000元", kind="amount")
    assert r["verdict"] == "different"
    assert "单位不等" in r["reason"]


def test_missing_unit_defaults_to_yuan_for_amount():
    assert value_norm.normalize_number("10000", kind="amount") == {
        "value": "10000", "unit": "元"}


# ---- 生命体征 / 年龄 -------------------------------------------------------

@pytest.mark.parametrize("a,b,kind", [
    ("35岁", "三十五岁", "quantity"),
    ("35岁", "35周岁", "quantity"),
    ("38.5℃", "38.5度", "quantity"),
    ("38.5℃", "38.5摄氏度", "quantity"),
])
def test_quantity_equivalent_forms(a, b, kind):
    assert value_norm.value_equal(a, b, kind)["verdict"] == "same"


def test_scale_char_as_unit_head_not_swallowed():
    """「5千克」的「千」是单位词头，不是 ×1000——吞掉就变成 5000 克。"""
    assert value_norm.normalize_number("5千克", kind="quantity") == {
        "value": "5", "unit": "kg"}
    assert value_norm.value_equal("5千克", "5kg", "quantity")["verdict"] == "same"


# ---- 不可解析取严 ----------------------------------------------------------

def test_unrecognized_unit_rejected_for_quantity():
    """认不出的单位在非金额域一律判不可解析（走人审），不猜。"""
    assert value_norm.normalize_number("5千米", kind="quantity") is None


def test_ordinary_text_is_not_read_as_a_number():
    """「一般情况」不能被读成「数值 1 + 单位 般情况」。"""
    assert value_norm.normalize_number("一般情况", kind="quantity") is None
    assert value_norm.value_equal("一般情况", "一般情况", "quantity")["verdict"] == "unparsable"


@pytest.mark.parametrize("raw", ["很多", "许多", "", "   ", "逾"])
def test_unparsable(raw):
    assert value_norm.normalize_number(raw) is None


def test_unparsable_is_conservative_not_pass():
    r = value_norm.value_equal("很多", "许多")
    assert r["verdict"] == "unparsable"
    assert r["equal"] is False, "不可解析不得被当成相等放行"


def test_verdicts_enum_complete():
    assert set(value_norm.VERDICTS) == {"same", "different", "unparsable"}


def test_parse_chinese_number_shared_primitive():
    """date_norm 依赖这个原语，它的行为不能被本模块的改动带偏。"""
    assert value_norm.parse_chinese_number("三万五千") == 35000
    assert value_norm.parse_chinese_number("二〇二三") == 2023
    assert value_norm.parse_chinese_number("一百零五") == 105
    assert value_norm.parse_chinese_number("很多") is None
    assert value_norm.parse_chinese_number("") is None