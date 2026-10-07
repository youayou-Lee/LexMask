"""还原工具测试(Issue#70 T7/T1-T08):三类形态/一对多/幻觉/最长优先/后界断言/嵌套/幂等。

红线:单测一律构造假数据,真实姓名只进加密区 round-trip 冒烟(独立脚本,不入此文件)。
"""

import pytest

from app.services.restore_service import (
    Mapping,
    RestoreResult,
    normalize_mapping,
    restore,
)

# ---------- T01 normalize 三格式 ----------

def test_normalize_new_structure():
    m = normalize_mapping({
        "[PERSON_1]": {"type": "PERSON", "texts": ["张三"]},
        "[ID_CARD_1]": {"type": "ID_CARD", "texts": ["110...", "120..."]},
    })
    assert m.entries["[PERSON_1]"].texts == ["张三"]
    assert m.entries["[PERSON_1]"].type == "PERSON"
    assert m.entries["[ID_CARD_1]"].texts == ["110...", "120..."]


def test_normalize_legacy_structure():
    m = normalize_mapping({"[PERSON_1]": {"text": "张三"}})
    assert m.entries["[PERSON_1]"].texts == ["张三"]


def test_normalize_degenerate_string_value():
    # T1 结构的退化写法:{替换词: 原文字符串}
    m = normalize_mapping({"张三": "[PERSON_1]", "某人民法院1": "广东省清远市清城区人民法院"})
    assert m.entries["张三"].texts == ["[PERSON_1]"]
    assert m.entries["某人民法院1"].texts == ["广东省清远市清城区人民法院"]


def test_normalize_mixed_and_warnings():
    m = normalize_mapping({
        "[PERSON_1]": {"type": "PERSON", "texts": ["张三"]},
        "[BAD_1]": {},               # 空 texts → warning
        "[BAD_2]": {"text": ""},     # 空原文 → warning
    })
    assert len(m.entries) == 1
    assert len(m.parse_warnings) == 2


# ---------- T02 占位符还原 ----------

def test_placeholder_unique_restores():
    m = normalize_mapping({"[PERSON_1]": {"texts": ["张三"]}})
    r = restore("被告人[PERSON_1]到庭。", m)
    assert r.restored_text == "被告人张三到庭。"
    assert r.restored_count == 1 and r.ambiguous == [] and r.unknown == []


def test_placeholder_multi_safe_keeps_and_candidates():
    m = normalize_mapping({"[BIRTH_DATE_1]": {"texts": ["1976年3月2日", "1976年11月8日"]}})
    r = restore("甲[PERSON_1],出生日期[BIRTH_DATE_1]。", m)
    assert "[BIRTH_DATE_1]" in r.restored_text  # 默认不猜
    amb = [a for a in r.ambiguous if a["key"] == "[BIRTH_DATE_1]"]
    assert amb and amb[0]["candidates"] == ["1976年3月2日", "1976年11月8日"]


def test_placeholder_multi_first_policy():
    m = normalize_mapping({"[BIRTH_DATE_1]": {"texts": ["1976年3月2日", "1976年11月8日"]}})
    r = restore("出生日期[BIRTH_DATE_1]。", m, policy="first")
    assert "1976年3月2日" in r.restored_text
    assert r.restored_count == 1


def test_unknown_placeholder_and_pseudonym():
    m = normalize_mapping({"[PERSON_1]": {"texts": ["张三"]}})
    r = restore("[PERSON_1]与[PERSON_999]、某派出所99。", m)
    assert set(r.unknown) == {"[PERSON_999]", "某派出所99"}
    assert "某派出所99" in r.restored_text  # 不假装还原


def test_placeholder_numeric_prefix_no_swallow():
    m = normalize_mapping({
        "[PERSON_1]": {"texts": ["张三"]},
        "[PERSON_10]": {"texts": ["李四"]},
    })
    r = restore("[PERSON_10]与[PERSON_1]同行。", m)
    assert r.restored_text == "李四与张三同行。"


# ---------- T03 化名词还原 ----------

def test_pseudonym_direct_restore():
    m = normalize_mapping({"某人民法院1": {"texts": ["广东省清远市清城区人民法院"]}})
    r = restore("某人民法院1刑事判决书。", m)
    assert r.restored_text == "广东省清远市清城区人民法院刑事判决书。"


def test_pseudonym_longest_first_full_restore():
    m = normalize_mapping({
        "某人民法院1": {"texts": ["清远市清城区人民法院"]},
        "某人民法院10": {"texts": ["广州市中级人民法院"]},
    })
    r = restore("某人民法院10与某人民法院1。", m)
    assert r.restored_text == "广州市中级人民法院与清远市清城区人民法院。"


# ---------- T03a 泛化词后界断言 ----------

def test_generalized_key_boundary_guard():
    m = normalize_mapping({"2023年": {"texts": ["2023年(出生)"]}})
    # 后界续接日期 → 碰撞,跳过进 ambiguous
    r = restore("于2023年3月7日案发。", m)
    assert "于2023年3月7日案发。" == r.restored_text
    assert [a["key"] for a in r.ambiguous] == ["2023年"]
    # 孤立出现 → 正常还原
    r2 = restore("本案发生于2023年。", m)
    assert "2023年(出生)" in r2.restored_text


def test_generalized_key_region_boundary():
    m = normalize_mapping({"广东省": {"texts": ["广东省(某登记地)"]}})
    r = restore("户籍广东省清远市。", m)
    assert "户籍广东省清远市。" == r.restored_text  # 后界是"清",续接区划,跳过
    r2 = restore("登记地为广东省。", m)
    assert "广东省(某登记地)" in r2.restored_text


# ---------- T04 混合/幂等/嵌套 ----------

def test_mixed_placeholders_and_pseudonyms():
    m = normalize_mapping({
        "[PERSON_1]": {"texts": ["张三"]},
        "某公司1": {"texts": ["某科技有限公司"]},
    })
    r = restore("[PERSON_1]任职于某公司1。", m)
    assert r.restored_text == "张三任职于某科技有限公司。"


def test_idempotent_second_pass():
    m = normalize_mapping({"[PERSON_1]": {"texts": ["张三"]}, "某公司1": {"texts": ["某科技有限公司"]}})
    r1 = restore("[PERSON_1]任职于某公司1。", m)
    r2 = restore(r1.restored_text, m)
    assert r2.restored_text == r1.restored_text
    assert r2.restored_count == 0


def test_nested_replacement_not_cascaded():
    # 原文含另一 key 形态:还原插入的文本不得被级联再替换
    m = normalize_mapping({
        "[PERSON_1]": {"texts": ["张三[PERSON_2]"]},
        "[PERSON_2]": {"texts": ["李四"]},
    })
    r = restore("[PERSON_1]指使[PERSON_2]。", m)
    # 张三[PERSON_2] 中的 [PERSON_2] 不得再被替换
    assert r.restored_text == "张三[PERSON_2]指使李四。"


# ---------- T05 API(T05 用 TestClient,鉴权见 T08) ----------

@pytest.mark.asyncio
async def test_restore_service_empty_and_plain():
    m = normalize_mapping({"[PERSON_1]": {"texts": ["张三"]}})
    r0 = restore("", m)
    assert r0.restored_text == "" and r0.restored_count == 0
    r1 = restore("没有任何敏感信息的普通文本。", m)
    assert r1.restored_count == 0 and r1.ambiguous == [] and r1.unknown == []
    # 空映射:占位符进 unknown
    r2 = restore("[PERSON_1]。", normalize_mapping({}))
    assert r2.unknown == ["[PERSON_1]"]
