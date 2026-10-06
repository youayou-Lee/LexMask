# Issue#56 M1 Task 4 —— 按页型分域转录比对 compare.py 的离线单测。
# 零网络、零真实案卷数据（全部合成占位符）。
# 与计划底稿脚手架的差异（实现 API 不变，见 task-4-report）：
#   底稿 test_empty_page_single_side 用页型 "empty"——页型枚举裁定有效值
#   为 body/table/seal_handwriting/edge（Task 6 PAGE_TYPES 对齐），改用 "edge"，
#   断言值（verdict / dispute kind）逐字保留。
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "eval"))
from gt import compare, gt_schema  # noqa: E402


# ---- 计划底稿样例（断言值逐字保留；"empty"→"edge" 为页型枚举裁定） ----------

def test_body_consistent():
    r = compare.compare_transcripts("甲行乙行", "甲行乙行", "body")
    assert r["verdict"] == "consistent"


def test_body_low_coverage_dispute():
    r = compare.compare_transcripts("甲" * 60, "乙" * 60, "body")
    assert r["verdict"] == "dispute" and r["disputes"][0]["kind"] == "low_coverage"


def test_edge_page_single_side():  # 探针实证：空页 VL 吐水印字
    r = compare.compare_transcripts("", "PoweredbyTest", "edge")
    assert r["verdict"] == "dispute" and r["disputes"][0]["kind"] == "single_side"


def test_table_entity_set_mismatch():
    a, b = "号码110122198110227771", "号码110122198110229999"
    r = compare.compare_transcripts(a, b, "table")
    assert r["verdict"] == "dispute" and r["disputes"][0]["kind"] == "set_mismatch"


# ---- 结果形状契约 ------------------------------------------------------------

def test_result_shape_contract():
    r = compare.compare_transcripts("甲" * 30, "甲" * 30, "body")
    assert set(r) == {"page_type", "verdict", "coverage", "disputes"}
    assert r["page_type"] == "body"
    assert set(r["coverage"]) == {"a_in_b", "b_in_a"}
    assert all(isinstance(v, float) for v in r["coverage"].values())
    assert r["disputes"] == []


def test_dispute_detail_is_evidence_bearing_str():
    r = compare.compare_transcripts("甲" * 60, "乙" * 60, "body")
    d = r["disputes"][0]
    assert isinstance(d["detail"], str) and d["detail"]
    r2 = compare.compare_transcripts("号码110122198110227771", "号码110122198110229999", "table")
    d2 = r2["disputes"][0]
    assert isinstance(d2["detail"], str) and d2["detail"]
    # 互含失败的实体文本须出现在 detail 中（可追溯证据）
    assert "110122198110227771" in d2["detail"]
    assert "110122198110229999" in d2["detail"]


# ---- 正文窗域：双向滑窗覆盖率 ------------------------------------------------
# a = "甲"*n + "乙"*k，b = "甲"*n + "丙"*k（win=8）：
#   a 侧命中窗 = 全甲窗（i ≤ n-8，共 n-7 个），脱靶窗 = k 个，
#   覆盖率 = (n-7)/(n+k-7)；n=97,k=10 → 90/100 = 0.90（恰达阈值）；
#   n=96,k=10 → 89/99 ≈ 0.899（低于阈值）。

def test_body_coverage_at_threshold_is_consistent():
    a = "甲" * 97 + "乙" * 10
    b = "甲" * 97 + "丙" * 10
    r = compare.compare_transcripts(a, b, "body")
    assert r["verdict"] == "consistent" and r["disputes"] == []
    assert r["coverage"]["a_in_b"] == pytest.approx(0.9)
    assert r["coverage"]["b_in_a"] == pytest.approx(0.9)


def test_body_coverage_below_threshold_is_low_coverage():
    a = "甲" * 96 + "乙" * 10
    b = "甲" * 96 + "丙" * 10
    r = compare.compare_transcripts(a, b, "body")
    assert r["verdict"] == "dispute" and r["disputes"][0]["kind"] == "low_coverage"
    assert r["coverage"]["a_in_b"] < 0.9 and r["coverage"]["b_in_a"] < 0.9


def test_cov_threshold_parameter_plumbed():
    a = "甲" * 96 + "乙" * 10
    b = "甲" * 96 + "丙" * 10
    strict = compare.compare_transcripts(a, b, "body")                   # 默认 0.9 → dispute
    lax = compare.compare_transcripts(a, b, "body", cov_threshold=0.85)  # 放宽 → consistent
    assert strict["verdict"] == "dispute"
    assert lax["verdict"] == "consistent"


def test_body_directional_coverage_is_bidirectional():
    # b 侧单方多字：a 全部命中 b，但 b 的含尾窗在 a 中脱靶 → 仍按低覆盖争议
    a = "甲" * 30
    b = "甲" * 30 + "乙" * 10
    r = compare.compare_transcripts(a, b, "body")
    assert r["coverage"]["a_in_b"] == pytest.approx(1.0)
    assert r["coverage"]["b_in_a"] < 0.9
    assert r["verdict"] == "dispute" and r["disputes"][0]["kind"] == "low_coverage"


# ---- 正文窗域：格式残差自动放行 ----------------------------------------------
# Task 2 归一化只剥行首 #，行中 #/## 存活；两侧仅剩此类残差 → auto_ok_format

def test_body_format_residue_auto_ok_format():
    a = "甲" * 30 + "##" + "乙" * 30
    b = "甲" * 30 + "乙" * 30
    r = compare.compare_transcripts(a, b, "body")
    assert r["verdict"] == "auto_ok_format" and r["disputes"] == []


def test_body_single_hash_residue_auto_ok_format():
    a = "甲" * 20 + "#附注" + "乙" * 20
    b = "甲" * 20 + "附注" + "乙" * 20
    r = compare.compare_transcripts(a, b, "body")
    assert r["verdict"] == "auto_ok_format" and r["disputes"] == []


def test_body_residue_plus_real_difference_still_dispute():
    # 残差之外还有真实文本差异 → 不走 auto_ok_format，按覆盖率裁决
    a = "甲" * 30 + "##" + "乙" * 60
    b = "甲" * 30 + "丙" * 60
    r = compare.compare_transcripts(a, b, "body")
    assert r["verdict"] == "dispute" and r["disputes"][0]["kind"] == "low_coverage"


# ---- 边缘/低密度域：单方多字 --------------------------------------------------

def test_edge_both_empty_consistent():
    r = compare.compare_transcripts("", "", "edge")
    assert r["verdict"] == "consistent"
    assert r["coverage"] == {"a_in_b": 1.0, "b_in_a": 1.0}


def test_edge_single_side_reverse_direction():
    r = compare.compare_transcripts("水印残迹", "", "edge")
    assert r["verdict"] == "dispute" and r["disputes"][0]["kind"] == "single_side"
    assert "a 侧" in r["disputes"][0]["detail"]


def test_low_density_by_length_overrides_body_page_type():
    # 页型 body 但任一侧归一化长度 <20 → 低密度域：短文本无结构，任何差异记单方多字
    r = compare.compare_transcripts("甲乙丙丁", "甲乙丙戊", "body")
    assert r["verdict"] == "dispute" and r["disputes"][0]["kind"] == "single_side"


def test_low_density_identical_short_is_consistent():
    r = compare.compare_transcripts("甲乙丙丁", "甲乙丙丁", "body")
    assert r["verdict"] == "consistent"


# ---- 表格域：实体集合互含 ------------------------------------------------------

def test_table_entity_sets_mutual_containment_consistent():
    # 同一实体嵌在两侧不同上下文中——集合域只看实体，不看外围散文
    a = "当事人号码110122198110227771详见"
    b = "号码110122198110227771附页备注"
    r = compare.compare_transcripts(a, b, "table")
    assert r["verdict"] == "consistent" and r["disputes"] == []


def test_table_one_side_missing_entities_is_set_mismatch():
    a = "甲" * 20                     # 无实体
    b = "乙" * 10 + "110122198110227771"  # 有实体（len 28）
    r = compare.compare_transcripts(a, b, "table")
    assert r["verdict"] == "dispute" and r["disputes"][0]["kind"] == "set_mismatch"


def test_table_both_entity_free_falls_back_to_window():
    # 双侧实体集皆空 → 集合域无信号，退回正文窗域裁决（实现在模块 docstring 声明）
    r = compare.compare_transcripts("甲" * 25, "乙" * 25, "table")
    assert r["verdict"] == "dispute" and r["disputes"][0]["kind"] == "low_coverage"
    r2 = compare.compare_transcripts("合计项甲乙丙丁戊己庚辛", "合计项甲乙丙丁戊己庚辛", "table")
    assert r2["verdict"] == "consistent"


# ---- 页型枚举与 seal_handwriting 兜底 ----------------------------------------

def test_seal_handwriting_dense_falls_back_to_body_window():
    r = compare.compare_transcripts("甲" * 40, "乙" * 40, "seal_handwriting")
    assert r["verdict"] == "dispute" and r["disputes"][0]["kind"] == "low_coverage"
    r2 = compare.compare_transcripts("手印页备注" * 5, "手印页备注" * 5, "seal_handwriting")
    assert r2["verdict"] == "consistent"


def test_page_types_constant_alignment():
    # T4 评审 Minor#3 收编：PAGE_TYPES 单一事实源在 gt_schema，compare 只 re-export
    # （不得自带字面量）——对齐断言从「值相等」升级为「同一对象」
    assert compare.PAGE_TYPES is gt_schema.PAGE_TYPES
    assert set(gt_schema.PAGE_TYPES) == {"body", "table", "seal_handwriting", "edge"}


def test_unknown_page_type_rejected():
    with pytest.raises(ValueError):
        compare.compare_transcripts("甲" * 30, "甲" * 30, "empty")  # 底稿旧枚举名，已废
    with pytest.raises(ValueError):
        compare.compare_transcripts("甲" * 30, "甲" * 30, "photo")
