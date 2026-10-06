# Issue#56 M1 Task 5 —— 仲裁表 arbitrate.py（R1-R7 全分支）的离线单测。
# 零网络、零真实案卷数据（全部合成占位符）。
# 计划底稿未给出测试脚手架，按 brief 接口逐分支构造种子用例：
# Entity/CompareResult 以字典形状直接构造（与 Task 3/4 产物同形）。
import copy
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "eval"))
from gt import arbitrate  # noqa: E402

ALL_RULES = {"R1", "R2", "R3", "R4", "R5", "R6", "R7"}


def _ent(text, etype, n0, n1, origin="regex"):
    """合成 Entity（Task 3 形状）；测试内 span_original == span_normalized。"""
    return {"text": text, "type": etype,
            "span_original": [n0, n1], "span_normalized": [n0, n1], "origin": origin}


def _cmp(verdict, kind=None, page_type="body", detail="合成争议：样本证据"):
    """合成 CompareResult（Task 4 形状）。"""
    disputes = [] if kind is None else [{"kind": kind, "detail": detail}]
    return {"page_type": page_type, "verdict": verdict,
            "coverage": {"a_in_b": 1.0, "b_in_a": 1.0}, "disputes": disputes}


# ---- R1：两云一致且无冲突 ------------------------------------------------------

def test_r1_consistent_no_conflict():
    e1 = _ent("110122198110227771", "身份证号", 2, 20)
    e2 = _ent("钱明涛", "姓名", 0, 3, origin="ner")
    a = [dict(e1), dict(e2)]
    b = [dict(e1), dict(e2)]
    arb = arbitrate.arbitrate_page(_cmp("consistent"), a, b, None, "body")
    assert arb["auto_resolved"] == [] and arb["disputed"] == []
    # 桶内按键（span_normalized, text）有序：钱明涛(0,3) 在身份证(2,20) 前
    assert [e["text"] for e in arb["consistent"]] == ["钱明涛", "110122198110227771"]


# ---- R2：一致但类型/读数冲突 ---------------------------------------------------

def test_r2_regex_beats_ner_auto_resolved():
    # T3 冲突标注形态：同 span 异型两条均保留、origin=conflict；正则类型名恰一名在场 → 正则胜
    a = [_ent("13800138000", "电话", 0, 11, origin="conflict"),
         _ent("13800138000", "姓名", 0, 11, origin="conflict")]
    b = [_ent("13800138000", "电话", 0, 11, origin="regex")]
    arb = arbitrate.arbitrate_page(_cmp("consistent"), a, b, None, "body")
    assert arb["disputed"] == [] and arb["consistent"] == []
    assert len(arb["auto_resolved"]) == 1
    entry = arb["auto_resolved"][0]
    assert entry["rule"] == "R2" and entry["entity"]["type"] == "电话"
    assert entry["source"] == "a"  # 胜者实例取 a 侧


def test_r2_ner_vs_ner_conflict_disputed():
    # 同键异型、无正则类型名在场（人名 vs 机构名）→ NER 间冲突 disputed
    a = [_ent("钱明涛", "姓名", 0, 3, origin="ner")]
    b = [_ent("钱明涛", "机构名", 0, 3, origin="ner")]
    arb = arbitrate.arbitrate_page(_cmp("consistent"), a, b, None, "body")
    assert arb["auto_resolved"] == [] and arb["consistent"] == []
    assert len(arb["disputed"]) == 1
    d = arb["disputed"][0]
    assert d["rule"] == "R2" and d["entity"]["text"] == "钱明涛"
    assert {e["type"] for e in d["candidates"]["a"]} == {"姓名"}
    assert {e["type"] for e in d["candidates"]["b"]} == {"机构名"}


def test_r2_two_regex_type_names_still_unclear_disputed():
    # ≥2 个正则类型名在场（电话 vs 银行卡号）：通道来源不可辨 → 仍不明升级
    a = [_ent("1380013800011", "电话", 0, 13, origin="conflict")]
    b = [_ent("1380013800011", "银行卡号", 0, 13, origin="conflict")]
    arb = arbitrate.arbitrate_page(_cmp("consistent"), a, b, None, "body")
    assert arb["auto_resolved"] == [] and arb["consistent"] == []
    assert arb["disputed"][0]["rule"] == "R2"


def test_r2_cross_side_reading_gap_on_consistent_page():
    # compare 一致但两侧实体集不合（窗域阈值下的实体级差异）→ 升级，不用 md 仲裁
    a = [_ent("110122198110227771", "身份证号", 2, 20)]
    arb = arbitrate.arbitrate_page(_cmp("consistent"), a, [], None, "body")
    assert arb["consistent"] == [] and arb["auto_resolved"] == []
    assert arb["disputed"][0]["rule"] == "R2"
    assert arb["disputed"][0]["entity"]["text"] == "110122198110227771"


# ---- R3 / R4：两云分歧 + md 佐证单方 -------------------------------------------

def test_r3_md_agrees_with_vl_adopt_b():
    agreed = _ent("钱明涛", "姓名", 30, 33, origin="regex+ner")
    x = _ent("110122198110227771", "身份证号", 2, 20)  # 仅 v6 读到（被否）
    y = _ent("110122198110229999", "身份证号", 2, 20)  # 仅 VL 读到，md 同读
    arb = arbitrate.arbitrate_page(_cmp("dispute", "low_coverage"),
                                   [dict(x), dict(agreed)], [dict(y), dict(agreed)],
                                   [dict(y)], "body")
    assert [e["text"] for e in arb["consistent"]] == ["钱明涛"]
    assert len(arb["auto_resolved"]) == 1  # 恰好一条
    entry = arb["auto_resolved"][0]
    assert entry["rule"] == "R3" and entry["entity"]["text"] == "110122198110229999"
    assert entry["source"] == "b"  # 采 VL，胜出源在案
    assert len(arb["disputed"]) == 1  # v6 被否读数留痕（A4 无静默丢弃）
    d = arb["disputed"][0]
    assert d["rule"] == "R3" and d["entity"]["text"] == "110122198110227771"
    assert d["candidates"]["a"] and d["candidates"]["b"] == [] and d["candidates"]["md"] == []


def test_r4_md_agrees_with_v6_adopt_a():
    x = _ent("110122198110227771", "身份证号", 2, 20)  # v6 读到，md 同读
    y = _ent("110122198110229999", "身份证号", 2, 20)  # VL 单边改动（被否）
    arb = arbitrate.arbitrate_page(_cmp("dispute", "low_coverage"),
                                   [dict(x)], [dict(y)], [dict(x)], "body")
    assert len(arb["auto_resolved"]) == 1  # 恰好一条
    entry = arb["auto_resolved"][0]
    assert entry["rule"] == "R4" and entry["entity"]["text"] == "110122198110227771"
    assert entry["source"] == "a"  # 采 v6，胜出源在案
    assert arb["disputed"][0]["rule"] == "R4"
    assert arb["disputed"][0]["entity"]["text"] == "110122198110229999"


# ---- R5：三方各执 / md 无法佐证单方 --------------------------------------------

def test_r5_three_way_dispute_disputed_with_gap():
    x = _ent("110122198110227771", "身份证号", 2, 20)
    y = _ent("110122198110229999", "身份证号", 2, 20)
    z = _ent("110122198110223333", "身份证号", 2, 20)  # md 第三种读法
    arb = arbitrate.arbitrate_page(_cmp("dispute", "low_coverage"),
                                   [dict(x)], [dict(y)], [dict(z)], "body")
    assert arb["consistent"] == [] and arb["auto_resolved"] == []
    entity_entries = [d for d in arb["disputed"] if "entity" in d]
    assert {d["entity"]["text"] for d in entity_entries} == {
        "110122198110227771", "110122198110229999", "110122198110223333"}
    assert all(d["rule"] == "R5" for d in arb["disputed"])
    gaps = [d for d in arb["disputed"] if "gap" in d]
    assert len(gaps) == 1 and gaps[0]["candidates"]["detail"]


def test_md_backing_both_sides_falls_to_r5():
    # md 同时佐证两侧（自相矛盾）→ 不支持任何单方
    x = _ent("110122198110227771", "身份证号", 2, 20)
    y = _ent("110122198110229999", "身份证号", 2, 20)
    arb = arbitrate.arbitrate_page(_cmp("dispute", "low_coverage"),
                                   [dict(x)], [dict(y)], [dict(x), dict(y)], "body")
    assert arb["auto_resolved"] == []
    entity_entries = [d for d in arb["disputed"] if "entity" in d]
    assert {d["entity"]["text"] for d in entity_entries} == {
        "110122198110227771", "110122198110229999"}
    assert all(d["rule"] == "R5" for d in arb["disputed"])


def test_md_silent_falls_to_r5():
    # md 缺席/沉默，两云分歧无第三方佐证 → 升级
    x = _ent("110122198110227771", "身份证号", 2, 20)
    y = _ent("110122198110229999", "身份证号", 2, 20)
    arb = arbitrate.arbitrate_page(_cmp("dispute", "low_coverage"),
                                   [dict(x)], [dict(y)], None, "body")
    assert arb["auto_resolved"] == []
    assert {d["rule"] for d in arb["disputed"]} == {"R5"}


def test_md_only_reading_escalates_r5_on_consistent_page():
    # md 独有读数（两云皆无）：无两云佐证，任何路径升级、不静默丢弃
    e = _ent("钱明涛", "姓名", 0, 3, origin="ner")
    m = _ent("沪A12345", "车牌号", 10, 16)
    arb = arbitrate.arbitrate_page(_cmp("consistent"), [dict(e)], [dict(e)], [dict(m)], "body")
    assert [x["text"] for x in arb["consistent"]] == ["钱明涛"]
    assert arb["auto_resolved"] == []
    assert len(arb["disputed"]) == 1
    assert arb["disputed"][0]["rule"] == "R5"
    assert arb["disputed"][0]["entity"]["text"] == "沪A12345"


# ---- R6：单方多字/集合不合 → 不走仲裁整页升级 -----------------------------------

def test_r6_single_side_empty_page_gap_only():
    # 探针实证场景：空页 VL 吐水印字、无实体 → 页级 gap 记录
    arb = arbitrate.arbitrate_page(_cmp("dispute", "single_side", page_type="edge"),
                                   [], [], None, "edge")
    assert arb["consistent"] == [] and arb["auto_resolved"] == []
    assert len(arb["disputed"]) == 1
    d = arb["disputed"][0]
    assert d["rule"] == "R6" and "gap" in d and d["candidates"]["detail"]


def test_r6_set_mismatch_all_readings_disputed_no_arbitration():
    # 表格集合不合：全部读数逐条 disputed，零采信（不走仲裁）
    x = _ent("110122198110227771", "身份证号", 2, 20)
    y = _ent("110122198110229999", "身份证号", 2, 20)
    arb = arbitrate.arbitrate_page(_cmp("dispute", "set_mismatch", page_type="table"),
                                   [dict(x)], [dict(y)], None, "table")
    assert arb["auto_resolved"] == []
    entries = [d for d in arb["disputed"] if "entity" in d]
    assert {d["entity"]["text"] for d in entries} == {
        "110122198110227771", "110122198110229999"}
    assert all(d["rule"] == "R6" for d in arb["disputed"])
    assert any("gap" in d for d in arb["disputed"])


# ---- R7：纯格式差 → 自动放行 ----------------------------------------------------

def test_r7_format_only_auto_resolved():
    e = _ent("钱明涛", "姓名", 0, 3, origin="ner")
    arb = arbitrate.arbitrate_page(_cmp("auto_ok_format"), [dict(e)], [dict(e)], None, "body")
    assert arb["consistent"] == [] and arb["disputed"] == []
    assert len(arb["auto_resolved"]) == 1
    assert arb["auto_resolved"][0]["rule"] == "R7"
    assert arb["auto_resolved"][0]["entity"]["text"] == "钱明涛"


def test_r7_unaligned_readings_not_silently_dropped():
    # R7 页上未对齐读数（残差干扰抽取的异常）→ 仍升级，不静默丢弃
    e = _ent("钱明涛", "姓名", 0, 3, origin="ner")
    extra = _ent("13800138000", "电话", 5, 16)
    arb = arbitrate.arbitrate_page(_cmp("auto_ok_format"),
                                   [dict(e), dict(extra)], [dict(e)], None, "body")
    assert arb["auto_resolved"] and arb["auto_resolved"][0]["rule"] == "R7"
    assert arb["disputed"] and arb["disputed"][0]["rule"] == "R7"
    assert arb["disputed"][0]["entity"]["text"] == "13800138000"


# ---- 输出形状 / 值域 / 纯函数性 -------------------------------------------------

def test_output_shape_contract():
    e = _ent("钱明涛", "姓名", 0, 3, origin="ner")
    x = _ent("110122198110227771", "身份证号", 2, 20)
    y = _ent("110122198110229999", "身份证号", 2, 20)
    # consistent 桶 = 裸 Entity 列表
    arb = arbitrate.arbitrate_page(_cmp("consistent"), [dict(e)], [dict(e)], None, "body")
    assert set(arb) == {"consistent", "auto_resolved", "disputed"}
    assert all(set(x) == {"text", "type", "span_original", "span_normalized", "origin"}
               for x in arb["consistent"])
    # auto_resolved 条目 = {entity, rule, source}；disputed 条目 = {entity|gap, rule, candidates}
    arb2 = arbitrate.arbitrate_page(_cmp("dispute", "low_coverage"),
                                    [dict(x)], [dict(y)], [dict(y)], "body")
    for entry in arb2["auto_resolved"]:
        assert set(entry) == {"entity", "rule", "source"}
        assert set(entry["entity"]) == {"text", "type", "span_original",
                                        "span_normalized", "origin"}
    for d in arb2["disputed"]:
        assert "entity" in d or "gap" in d
        assert d["rule"] in ALL_RULES
        assert set(d["candidates"]) == {"a", "b", "md"}


def test_a3_all_seven_branches_leave_trace():
    # A3：R1-R7 每分支至少一条种子用例走通；R1 无独立标注（consistent 桶即留痕）
    e = _ent("钱明涛", "姓名", 0, 3, origin="ner")
    x = _ent("110122198110227771", "身份证号", 2, 20)
    y = _ent("110122198110229999", "身份证号", 2, 20)
    z = _ent("110122198110223333", "身份证号", 2, 20)
    ph = _ent("13800138000", "电话", 0, 11, origin="conflict")
    nm = _ent("13800138000", "姓名", 0, 11, origin="conflict")
    scenarios = [
        arbitrate.arbitrate_page(_cmp("consistent"), [dict(e)], [dict(e)], None, "body"),  # R1
        arbitrate.arbitrate_page(_cmp("consistent"), [dict(ph), dict(nm)],
                                 [dict(ph)], None, "body"),                                # R2
        arbitrate.arbitrate_page(_cmp("dispute", "low_coverage"),
                                 [dict(x)], [dict(y)], [dict(y)], "body"),                 # R3
        arbitrate.arbitrate_page(_cmp("dispute", "low_coverage"),
                                 [dict(x)], [dict(y)], [dict(x)], "body"),                 # R4
        arbitrate.arbitrate_page(_cmp("dispute", "low_coverage"),
                                 [dict(x)], [dict(y)], [dict(z)], "body"),                 # R5
        arbitrate.arbitrate_page(_cmp("dispute", "set_mismatch", page_type="table"),
                                 [dict(x)], [dict(y)], None, "table"),                     # R6
        arbitrate.arbitrate_page(_cmp("auto_ok_format"), [dict(e)], [dict(e)], None, "body"),  # R7
    ]
    rules = {d["rule"] for arb in scenarios for d in arb["auto_resolved"] + arb["disputed"]}
    assert rules == {"R2", "R3", "R4", "R5", "R6", "R7"}
    assert scenarios[0]["consistent"]                       # R1：一致集
    assert scenarios[2]["auto_resolved"][0]["rule"] == "R3"
    assert scenarios[3]["auto_resolved"][0]["rule"] == "R4"
    assert scenarios[6]["auto_resolved"][0]["rule"] == "R7"


def test_pure_function_deterministic_and_inputs_untouched():
    x = _ent("110122198110227771", "身份证号", 2, 20)
    m = _ent("110122198110229999", "身份证号", 2, 20)
    a, b, md = [dict(x)], [dict(m)], [dict(m)]
    a_snap, md_snap, cmp_snap = copy.deepcopy(a), copy.deepcopy(md), _cmp("dispute", "low_coverage")
    r1 = arbitrate.arbitrate_page(cmp_snap, a, b, md, "body")
    r2 = arbitrate.arbitrate_page(cmp_snap, a, b, md, "body")
    assert r1 == r2  # 同入参同输出（无随机/时钟）
    assert a == a_snap and md == md_snap  # 入参不被修改


# ---- 入参校验 -------------------------------------------------------------------

def test_unknown_page_type_rejected():
    with pytest.raises(ValueError):
        arbitrate.arbitrate_page(_cmp("consistent"), [], [], None, "empty")  # 底稿旧枚举名，已废
    with pytest.raises(ValueError):
        arbitrate.arbitrate_page(_cmp("consistent"), [], [], None, "photo")


def test_unknown_verdict_rejected():
    with pytest.raises(ValueError):
        arbitrate.arbitrate_page(_cmp("weird"), [], [], None, "body")
