"""还原工具测试(Issue#70 T7/T1-T08):三类形态/一对多/幻觉/最长优先/后界断言/嵌套/幂等。

红线:单测一律构造假数据,真实姓名只进加密区 round-trip 冒烟(独立脚本,不入此文件)。
"""

from app.services.restore_service import (
    normalize_mapping,
    restore,
)

# ---------- T01 normalize 三格式 ----------


def test_normalize_new_structure():
    m = normalize_mapping(
        {
            "[PERSON_1]": {"type": "PERSON", "texts": ["张三"]},
            "[ID_CARD_1]": {"type": "ID_CARD", "texts": ["110...", "120..."]},
        }
    )
    assert m.entries["[PERSON_1]"].texts == ["张三"]
    assert m.entries["[PERSON_1]"].type == "PERSON"
    assert m.entries["[ID_CARD_1]"].texts == ["110...", "120..."]


def test_normalize_legacy_structure():
    m = normalize_mapping({"[PERSON_1]": {"text": "张三"}})
    assert m.entries["[PERSON_1]"].texts == ["张三"]


def test_normalize_string_value_dual_direction():
    # 字符串值双向(评审 I2 定稿):value 是占位符/化名形态=反查 {原文: 替换词};
    # 否则=T1 退化 {替换词: 原文}
    m = normalize_mapping(
        {
            "张三": "[PERSON_1]",  # 反查方向(preview entity_map)
            "某人民法院1": "广东省清远市清城区人民法院",  # T1 退化方向
        }
    )
    assert m.entries["[PERSON_1]"].texts == ["张三"]
    assert m.entries["某人民法院1"].texts == ["广东省清远市清城区人民法院"]


def test_preview_entity_map_roundtrip_direction():
    # build_preview_entity_map 产物 {原文: 替换词} 喂入可正确还原(评审 I2 实测面)
    from app.services.restore_service import restore as _r

    m = normalize_mapping({"陈文清": "[PERSON_1]"})
    r = _r("委托人[PERSON_1]到案。", m)
    assert r.restored_text == "委托人陈文清到案。"


def test_digit_ended_key_partial_swallow_exposed():
    # 评审 I1:某公司12 中的 某公司1 不部分吞吃;该形态进 unknown 显式暴露
    m = normalize_mapping({"某公司1": {"texts": ["甲公司"]}})
    r = restore("涉案某公司12与某公司13。", m)
    assert "甲公司2" not in r.restored_text and "甲公司3" not in r.restored_text
    assert "某公司12" in r.restored_text  # 原样保留(不猜)
    assert set(r.unknown) == {"某公司1"}


def test_year_era_suffix_boundary():
    # 评审 C1:「1990年代」「2023年初」是高频法律文书形态,不得静默损坏
    m = normalize_mapping({"2023年": {"texts": ["2023年7月13日"]}, "1990年": {"texts": ["1990年5月1日"]}})
    r = restore("上世纪1990年代出生,2023年初案发,2023年年底宣判。", m)
    assert r.restored_text == "上世纪1990年代出生,2023年初案发,2023年年底宣判。"
    assert {a["key"] for a in r.ambiguous} == {"1990年", "2023年"}
    assert r.hits["skipped_boundary"] == 3


def test_year_legit_continuation_restores():
    # C1 对照面:「起/以来/前后/起诉」合法续接,正常还原
    m = normalize_mapping({"2023年": {"texts": ["2023年7月13日"]}})
    r = restore("自2023年起诉后,2023年以来行骗三次。", m)
    assert r.restored_text.count("2023年7月13日") == 2
    assert r.ambiguous == []


def test_used_first_reported():
    # 评审 I3:policy=first 取首条须在报告中标注
    m = normalize_mapping({"[PERSON_1]": {"texts": ["甲一", "甲二"]}})
    r = restore("嫌疑人[PERSON_1]。", m, policy="first")
    assert "甲一" in r.restored_text
    assert r.used_first == ["[PERSON_1]"]
    r_safe = restore("嫌疑人[PERSON_1]。", m)
    assert r_safe.used_first == []


def test_native_someword_not_unknown():
    # 评审 M1:原生「某甲」「某些」不报 unknown;只认带数字后缀合成形态
    m = normalize_mapping({"[PERSON_1]": {"texts": ["张三"]}})
    r = restore("某甲与某些证人指认[PERSON_1]。", m)
    assert r.unknown == []
    assert r.restored_text == "某甲与某些证人指认张三。"


def test_ambiguous_dedup_with_occurrences():
    # 评审 M2:同 key 多次碰撞去重附 occurrences
    m = normalize_mapping({"2023年": {"texts": ["2023年7月13日"]}})
    r = restore("2023年3月、2023年5月、2023年7月各一次。", m)
    amb = [a for a in r.ambiguous if a["key"] == "2023年"]
    assert len(amb) == 1 and amb[0]["occurrences"] == 3


def test_normalize_mixed_and_warnings():
    m = normalize_mapping(
        {
            "[PERSON_1]": {"type": "PERSON", "texts": ["张三"]},
            "[BAD_1]": {},  # 空 texts → warning
            "[BAD_2]": {"text": ""},  # 空原文 → warning
        }
    )
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
    assert amb[0]["occurrences"] == 1


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
    m = normalize_mapping(
        {
            "[PERSON_1]": {"texts": ["张三"]},
            "[PERSON_10]": {"texts": ["李四"]},
        }
    )
    r = restore("[PERSON_10]与[PERSON_1]同行。", m)
    assert r.restored_text == "李四与张三同行。"


# ---------- T03 化名词还原 ----------


def test_pseudonym_direct_restore():
    m = normalize_mapping({"某人民法院1": {"texts": ["广东省清远市清城区人民法院"]}})
    r = restore("某人民法院1刑事判决书。", m)
    assert r.restored_text == "广东省清远市清城区人民法院刑事判决书。"


def test_pseudonym_longest_first_full_restore():
    m = normalize_mapping(
        {
            "某人民法院1": {"texts": ["清远市清城区人民法院"]},
            "某人民法院10": {"texts": ["广州市中级人民法院"]},
        }
    )
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
    m = normalize_mapping(
        {
            "[PERSON_1]": {"texts": ["张三"]},
            "某公司1": {"texts": ["某科技有限公司"]},
        }
    )
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
    m = normalize_mapping(
        {
            "[PERSON_1]": {"texts": ["张三[PERSON_2]"]},
            "[PERSON_2]": {"texts": ["李四"]},
        }
    )
    r = restore("[PERSON_1]指使[PERSON_2]。", m)
    # 张三[PERSON_2] 中的 [PERSON_2] 不得再被替换
    assert r.restored_text == "张三[PERSON_2]指使李四。"


# ---------- T05 API(T05 用 TestClient,鉴权见 T08) ----------


def test_restore_service_empty_and_plain():
    m = normalize_mapping({"[PERSON_1]": {"texts": ["张三"]}})
    r0 = restore("", m)
    assert r0.restored_text == "" and r0.restored_count == 0
    r1 = restore("没有任何敏感信息的普通文本。", m)
    assert r1.restored_count == 0 and r1.ambiguous == [] and r1.unknown == []
    # 空映射:占位符进 unknown
    r2 = restore("[PERSON_1]。", normalize_mapping({}))
    assert r2.unknown == ["[PERSON_1]"]


def test_idempotent_second_pass_unknown_stable():
    # 评审 M1 连带:二次还原报告级也幂等(unknown 不新增)
    m = normalize_mapping({"[PERSON_1]": {"texts": ["张三"]}, "某公司1": {"texts": ["某科技有限公司"]}})
    r1 = restore("[PERSON_1]任职于某公司1。", m)
    r2 = restore(r1.restored_text, m)
    assert r2.unknown == []


# ---------- Issue#75 缺陷修复:agent-md items 映射表格式 ----------


def test_normalize_agent_md_items_format():
    """agent-md 三件套映射表 {"items": [...]}:替换词→原文还原;excluded 跳过。"""
    raw = {
        "items": [
            {"id": "e1", "original_text": "袁某1", "entity_type": "PERSON", "replacement": "袁吃霄", "excluded": False},
            {
                "id": "e2",
                "original_text": "2023年5月1日",
                "entity_type": "DATE",
                "replacement": "[DATE_1]",
                "excluded": False,
            },
            {
                "id": "e3",
                "original_text": "某银行",
                "entity_type": "ORG",
                "replacement": "",
                "excluded": False,
            },  # 空替换词→跳过
            {
                "id": "e4",
                "original_text": "保留字段",
                "entity_type": "ORG",
                "replacement": "保留字段",
                "excluded": True,
            },  # excluded→跳过
        ]
    }
    m = normalize_mapping(raw)
    assert m.entries["袁吃霄"].texts == ["袁某1"]
    assert m.entries["袁吃霄"].type == "PERSON"
    assert m.entries["[DATE_1]"].texts == ["2023年5月1日"]
    assert "保留字段" not in m.entries  # excluded 不入表(未替换无需还原)
    assert any("excluded" in w for w in m.parse_warnings)
    assert any("e3" in w or "为空" in w for w in m.parse_warnings)


def test_agent_md_items_roundtrip_restore():
    """端到端:脱敏稿 + items 映射表 → 还原出原文;excluded 项原文不动。"""
    mapping = normalize_mapping(
        {
            "items": [
                {
                    "id": "e1",
                    "original_text": "袁某1",
                    "entity_type": "PERSON",
                    "replacement": "袁吃霄",
                    "excluded": False,
                },
                {
                    "id": "e2",
                    "original_text": "工商银行",
                    "entity_type": "ORG",
                    "replacement": "某银行",
                    "excluded": True,
                },
            ]
        }
    )
    r = restore("袁吃霄 借款于某银行。", mapping)
    assert r.restored_text == "袁某1 借款于某银行。"
    assert r.restored_count == 1


# ---------- Issue#75 括号化名口径（2026-10-08 用户定稿） ----------


def test_bracketed_pseudonym_items_roundtrip_restore():
    """[袁某一]/[袁某] 形态映射（items 格式）→ 字面扫描还原原文。"""
    mapping = normalize_mapping(
        {
            "items": [
                {
                    "id": "e1",
                    "original_text": "袁吃霄",
                    "entity_type": "PERSON",
                    "replacement": "[袁某一]",
                    "excluded": False,
                },
                {
                    "id": "e2",
                    "original_text": "袁飞",
                    "entity_type": "PERSON",
                    "replacement": "[袁某二]",
                    "excluded": False,
                },
                {
                    "id": "e3",
                    "original_text": "张三",
                    "entity_type": "PERSON",
                    "replacement": "[张某]",
                    "excluded": False,
                },
            ]
        }
    )
    r = restore("[袁某一] 与 [袁某二] 及 [张某] 均到庭。", mapping)
    assert r.restored_text == "袁吃霄 与 袁飞 及 张三 均到庭。"
    assert r.restored_count == 3
    assert r.unknown == [] and r.ambiguous == []


def test_bracketed_pseudonym_adjacency_not_corrupted():
    """相邻数字/汉字不吞吃：[袁某一]2 不得按 [袁某一] 部分还原成 袁吃霄2 的残缺形态。"""
    mapping = normalize_mapping(
        {
            "items": [
                {
                    "id": "e1",
                    "original_text": "袁吃霄",
                    "entity_type": "PERSON",
                    "replacement": "[袁某一]",
                    "excluded": False,
                },
            ]
        }
    )
    r = restore("文书号=[袁某一]2023号", mapping)
    # ] 后无边界吞吃问题：整体命中还原（] 是 key 尾字符，字面匹配即完整命中）
    assert r.restored_text == "文书号=袁吃霄2023号"
    # 独立出现完整还原
    r2 = restore("[袁某一]借款。", mapping)
    assert r2.restored_text == "袁吃霄借款。"


def test_bracketed_pseudonym_missing_from_mapping_reported_unknown():
    """映射表没有的括号化名 → unknown 显式暴露（不被当原文残留）。"""
    mapping = normalize_mapping(
        {
            "items": [
                {
                    "id": "e1",
                    "original_text": "张三",
                    "entity_type": "PERSON",
                    "replacement": "[张某]",
                    "excluded": False,
                },
            ]
        }
    )
    r = restore("[张某]与[李某]到庭。", mapping)
    assert "[李某]" in r.unknown
    assert r.restored_text == "张三与[李某]到庭。"


# ---------- Issue#87 还原契约括号免疫（裸形态=Agent 剥括号后的产物） ----------


def test_issue87_strip_bracket_full_recovery():
    # A1 核心回归：产物 [X] 被 Agent 剥成裸 X 后，配原映射表（key 带括号）仍全量还原
    mapping = normalize_mapping(
        {
            "[袁某]": {"texts": ["袁吃霄"]},
            "[袁某一]": {"texts": ["袁大头"]},
            "[机构_1]": {"texts": ["某县工商行政管理局"]},
        }
    )
    raw = "袁某与袁某一均在[机构_1]工作。"
    stripped = raw.replace("[", "").replace("]", "")
    r = restore(stripped, mapping)
    assert r.unknown == []
    assert r.restored_text == "袁吃霄与袁大头均在某县工商行政管理局工作。"
    assert r.restored_count == 3


def test_issue87_bare_pseudonym_boundary_ambiguity():
    # A3：裸化名后接中文数字=边界歧义，不猜；后接普通字正常还原
    mapping = normalize_mapping({"[袁某一]": {"texts": ["袁大二"]}})
    r = restore("袁某一二涉案。", mapping)
    assert r.restored_text == "袁某一二涉案。"
    assert any(a["key"] == "[袁某一]" and a.get("reason") == "boundary" for a in r.ambiguous)
    r2 = restore("袁某一在案发地出现。", mapping)
    assert r2.restored_text == "袁大二在案发地出现。"
    assert r2.ambiguous == []


def test_issue87_bare_placeholder_restore_and_ambiguity():
    # A4：裸占位符（中文标签）认；_N 后接数字=歧义不猜
    mapping = normalize_mapping({"[机构_1]": {"texts": ["甲公司"]}})
    r = restore("被告机构_1于案发。", mapping)
    assert r.restored_text == "被告甲公司于案发。"
    r2 = restore("被告机构_12于案发。", mapping)
    assert r2.restored_text == "被告机构_12于案发。"
    assert any(a["key"] == "[机构_1]" and a.get("reason") == "boundary" for a in r2.ambiguous)
    assert set(r2.unknown) == set()  # 机构_1 是 机构_12 的相关 key，不报 unknown


def test_issue87_reverse_mapping_with_bare_replacement():
    # A5：反查映射 {原文: 裸替换词} 兼容（Agent 回吐剥括号映射）
    m = normalize_mapping({"袁吃霄": "袁某", "甲公司": "机构_1"})
    assert m.entries["袁某"].texts == ["袁吃霄"]
    assert m.entries["机构_1"].texts == ["甲公司"]
    r = restore("袁某在机构_1任职。", m)
    assert r.restored_text == "袁吃霄在甲公司任职。"


def test_issue87_bare_same_prefix_longest_match():
    # B1：同前缀共存（[袁某]+[袁某一]）裸文本最长匹配，唯一不串号
    mapping = normalize_mapping(
        {
            "[袁某]": {"texts": ["袁大"]},
            "[袁某一]": {"texts": ["袁大二"]},
        }
    )
    r = restore("袁某一与袁某均涉案。", mapping)
    assert r.restored_text == "袁大二与袁大均涉案。"
    assert r.restored_count == 2
    assert r.unknown == []


def test_issue87_bare_unknown_exposure():
    # B2：剥括号后形似替换词但映射表没有 → unknown 显式暴露（安全信号不失守）。
    # 无序号裸「姓某」不报：与原生文本不可区分（评审复核修订，见 PR 说明）
    mapping = normalize_mapping(
        {
            "[机构_1]": {"texts": ["甲公司"]},
            "[袁某]": {"texts": ["袁大"]},
        }
    )
    r = restore("机构_99与赵某一出现在现场。", mapping)
    assert set(r.unknown) >= {"机构_99", "赵某一"}
    assert "袁某" not in r.unknown and "机构_1" not in r.unknown


def test_issue87_bare_multicandidate_safe_and_first():
    # B3：一对多裸形态沿用既有 safe/first 语义
    mapping = normalize_mapping({"[机构_1]": {"texts": ["甲公司", "乙公司"]}})
    r = restore("机构_1涉案。", mapping)
    assert r.restored_text == "机构_1涉案。"
    assert any(a["key"] == "[机构_1]" and a.get("candidates") == ["甲公司", "乙公司"] for a in r.ambiguous)
    r2 = restore("机构_1涉案。", mapping, policy="first")
    assert r2.restored_text == "甲公司涉案。"
    assert r2.used_first == ["[机构_1]"]


def test_issue87_boundary_ambiguity_schema_contract():
    # B4：边界歧义元素沿用现有 schema {key, candidates, reason, occurrences}
    mapping = normalize_mapping({"[袁某一]": {"texts": ["袁大二"]}})
    r = restore("袁某一二。", mapping)
    a = next(a for a in r.ambiguous if a.get("reason") == "boundary")
    assert set(a) >= {"key", "candidates", "reason", "occurrences"}
    assert a["key"] == "[袁某一]" and a["candidates"] == ["袁大二"]


def test_issue87_native_words_not_touched():
    # B5：原生「某些/某甲/某某」不得被误还原或误报 unknown
    mapping = normalize_mapping({"[袁某]": {"texts": ["袁大"]}})
    r = restore("某些人员与某甲有关，某某证人说袁某在场。", mapping)
    assert r.restored_text == "某些人员与某甲有关，某某证人说袁大在场。"
    assert r.unknown == []


def test_issue87_large_text_no_blowup():
    # B6：可选括号组不得引入灾难性回溯——1MB 级文本还原耗时护栏
    import time

    mapping = normalize_mapping(
        {
            **{f"[机构_{i}]": {"texts": [f"甲公司{i}"]} for i in range(1, 51)},
            "[袁某]": {"texts": ["袁大"]},
        }
    )
    body = "袁某在机构_1与机构_2之间往返，" * 40000  # ≈1MB
    t0 = time.monotonic()
    r = restore(body, mapping)
    elapsed = time.monotonic() - t0
    assert r.restored_count > 0
    assert elapsed < 10.0


def test_issue87_bare_pseudonym_requires_cjk_surname():
    # 评审 Minor1：裸化名「姓」须为汉字，a某二/1某三 等非汉字前缀不命中 unknown
    mapping = normalize_mapping({"[袁某]": {"texts": ["袁大"]}})
    r = restore("代码 a某二、编号 1某三 出现。", mapping)
    assert r.unknown == []
