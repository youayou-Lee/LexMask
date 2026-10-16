# Issue#56 P0 —— 日期粒度归一 date_norm.py 的离线单测。
# 零网络、零真实案卷数据（全部合成日期）。
#
# 口径依据（2026-10-14 用户拍板）：标准形态 "2023年6月15日"，**粒度差算同一实体**。
# 注意：粒度差只是"算同一实体"，不等于"GT 生产可自动放行"——放行还要过证据闸门，
# 因为"2023年"可能是 OCR 截断，也可能是模型凭空不补月日。这一层在 gate.py 里。
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "eval"))
from gt import date_norm  # noqa: E402


# ---- 形态归一（同一日期的各种写法必须归一到同一元组） ----------------------

@pytest.mark.parametrize("raw", [
    "2023年6月15日",
    "2023年06月15日",
    "2023-06-15",
    "2023/6/15",
    "2023.6.15",
    "20230615",
    "二〇二三年六月十五日",
    "2023年6月15号",
])
def test_same_date_many_forms(raw):
    assert date_norm.normalize_date(raw) == {
        "year": 2023, "month": 6, "day": 15,
        "hour": None, "minute": None, "second": None,
    }


@pytest.mark.parametrize("raw,expect", [
    ("2023年6月", (2023, 6, None)),
    ("2023年", (2023, None, None)),
    ("6月15日", (None, 6, 15)),
])
def test_partial_dates(raw, expect):
    p = date_norm.normalize_date(raw)
    assert (p["year"], p["month"], p["day"]) == expect


@pytest.mark.parametrize("raw,expect", [
    ("10:30:25", (10, 30, 25)),
    ("10时30分25秒", (10, 30, 25)),
    ("十时三十分", (10, 30, None)),
])
def test_times(raw, expect):
    p = date_norm.normalize_date(raw)
    assert (p["hour"], p["minute"], p["second"]) == expect


def test_afternoon_marker_shifts_hour():
    """「下午3点」是 15 时——不补 12 小时就会与「15时」判成 different。"""
    assert date_norm.normalize_date("下午3点")["hour"] == 15


@pytest.mark.parametrize("raw", ["去年", "多名", "若干", "", "   "])
def test_unparsable(raw):
    assert date_norm.normalize_date(raw) is None


def test_invalid_calendar_rejected():
    """2 月 30 日不是日期，不能当合法读数放过。"""
    assert date_norm.normalize_date("2023年2月30日") is None
    assert date_norm.normalize_date("2023年13月1日") is None


# ---- 判等三态 --------------------------------------------------------------

def test_full_equality_is_strict():
    r = date_norm.date_equal("2023年6月15日", "2023-06-15")
    assert r["verdict"] == "same"
    assert r["strict"] is True and r["lenient"] is True
    assert r["granularity_gap"] is False
    assert r["gap_direction"] is None


def test_granularity_gap_is_same_lenient_but_not_strict():
    """用户拍板的口径核心：2023年 ≡ 2023年6月15日（同一实体）。"""
    r = date_norm.date_equal("2023年", "2023年6月15日")
    assert r["verdict"] == "same_granularity_gap"
    assert r["granularity_gap"] is True
    assert r["lenient"] is True, "宽松口径必须判为同一实体"
    assert r["strict"] is False, "严格口径不得判等（信息量不同）"


def test_gap_direction_names_the_coarser_side():
    r1 = date_norm.date_equal("2023年", "2023年6月15日")
    assert r1["gap_direction"] == "a_coarser"
    r2 = date_norm.date_equal("2023年6月15日", "2023年")
    assert r2["gap_direction"] == "b_coarser"


def test_missing_year_still_comparable():
    """「6月15日」的字段集是「2002年6月15日」的真子集——也是粒度差，不是不可比。"""
    r = date_norm.date_equal("6月15日", "2002年6月15日")
    assert r["verdict"] == "same_granularity_gap"
    assert r["gap_direction"] == "a_coarser"


def test_gap_direction_records_which_side_omits_more():
    """方向是给证据闸门用的：**更细**的一侧才可能是凭空补全，必须分开记。"""
    coarse = date_norm.date_equal("2023年", "2023年6月15日")
    fine = date_norm.date_equal("2023年6月15日", "2023年")
    assert coarse["gap_direction"] != fine["gap_direction"]


@pytest.mark.parametrize("a,b,field", [
    ("2023年6月15日", "2023年6月16日", "day"),
    ("2023年6月15日", "2023年7月15日", "month"),
    ("2002年1月20日", "2003年1月20日", "year"),
])
def test_conflicting_value_is_different(a, b, field):
    r = date_norm.date_equal(a, b)
    assert r["verdict"] == "different"
    assert field in r["reason"]


def test_incomparable_field_sets_are_different_not_gap():
    """3月 与 6月 字段集互不包含——是冲突，不是粒度差，绝不能算同一实体。"""
    r = date_norm.date_equal("3月", "6月")
    assert r["verdict"] == "different"
    assert r["lenient"] is False


def test_time_granularity_gap():
    r = date_norm.date_equal("10:30", "10:30:25")
    assert r["verdict"] == "same_granularity_gap"
    assert r["gap_direction"] == "a_coarser"


def test_unparsable_side():
    r = date_norm.date_equal("去年", "2023年")
    assert r["verdict"] == "unparsable"
    assert r["lenient"] is False and r["strict"] is False


# ---- 双分数 ----------------------------------------------------------------

def test_date_scores_outputs_both_and_never_merges():
    """两个分数都出、不合并——口径将来再变时历史数据不作废。"""
    s = date_norm.date_scores("2023年", "2023年6月15日")
    assert s == {"date_strict": False, "date_lenient": True}
    s2 = date_norm.date_scores("2023年6月15日", "2023年6月15日")
    assert s2 == {"date_strict": True, "date_lenient": True}
    s3 = date_norm.date_scores("2023年6月15日", "2023年6月16日")
    assert s3 == {"date_strict": False, "date_lenient": False}


def test_render_date_uses_standard_form():
    parts = date_norm.normalize_date("2023-06-15")
    assert date_norm.render_date(parts) == date_norm.STANDARD_FORM
    assert date_norm.render_date(None) == ""


def test_verdict_and_direction_enums_declared():
    assert set(date_norm.VERDICTS) == {"same", "same_granularity_gap",
                                       "different", "unparsable"}
    assert set(date_norm.GAP_DIRECTIONS) == {"a_coarser", "b_coarser"}


def test_every_verdict_is_reachable():
    """四个判定值都必须有能产出它的输入——否则枚举里有死值。"""
    seen = {
        date_norm.date_equal("2023年6月15日", "2023-06-15")["verdict"],
        date_norm.date_equal("2023年", "2023年6月15日")["verdict"],
        date_norm.date_equal("2023年6月15日", "2023年6月16日")["verdict"],
        date_norm.date_equal("去年", "2023年")["verdict"],
    }
    assert seen == set(date_norm.VERDICTS)