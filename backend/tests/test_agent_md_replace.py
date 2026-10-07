# backend/tests/test_agent_md_replace.py
from app.models.entity_schemas import Entity
from app.services.agent_md_replace import apply_decisions, render_outputs
from app.services.agent_md_types import MappingItem, Seg


def _fixture():
    segs = [Seg(text="张三借李四人民币一万元", page_idx=0, source="text")]
    ents = [
        Entity(id="a", text="张三", type="PERSON", start=0, end=2),
        Entity(id="b", text="李四", type="PERSON", start=3, end=5),
    ]
    mapping = [
        MappingItem(id="e1", original_text="张三", entity_type="PERSON", replacement="[人名_1]"),
        MappingItem(id="e2", original_text="李四", entity_type="PERSON", replacement="[人名_2]"),
    ]
    return segs, ents, mapping


def test_apply_decisions_exclude_and_custom():
    _, _, mapping = _fixture()
    out = apply_decisions(mapping, [
        {"id": "e1", "action": "exclude"},
        {"id": "e2", "action": "custom", "replacement": "[人名_9]"},
    ])
    assert out[0].excluded is True and out[0].replacement == "[人名_1]"
    assert out[1].replacement == "[人名_9]" and out[1].excluded is False


def test_render_outputs_replaces_and_keeps_excluded():
    segs, ents, mapping = _fixture()
    mapping = apply_decisions(mapping, [{"id": "e1", "action": "exclude"}])
    md, mapping_json, retained = render_outputs(segs, ents, mapping)
    assert "张三借[人名_2]人民币一万元" in md
    assert mapping_json["items"][0]["excluded"] is True
    assert retained["retained_fields"][0]["text"] == "张三"


def test_render_outputs_empty_entities_passthrough():
    segs = [Seg(text="无实体段落", page_idx=0, source="text")]
    md, mapping_json, retained = render_outputs(segs, [], [])
    assert md == "无实体段落"
    assert mapping_json["items"] == [] and retained["retained_fields"] == []


def _two_seg_fixture_unannotated():
    """实体只在段 1 标注；同值在段 2 再次出现但 NER 未标注。"""
    segs = [
        Seg(text="张三借李四人民币一万元", page_idx=0, source="text"),
        Seg(text="经查，张三另欠李四两千元", page_idx=1, source="text"),
    ]
    ents = [
        Entity(id="a", text="张三", type="PERSON", start=0, end=2),
        Entity(id="b", text="李四", type="PERSON", start=3, end=5),
    ]
    mapping = [
        MappingItem(id="e1", original_text="张三", entity_type="PERSON", replacement="[人名_1]"),
        MappingItem(id="e2", original_text="李四", entity_type="PERSON", replacement="[人名_2]"),
    ]
    return segs, ents, mapping


def test_full_text_fallback_replaces_unannotated_occurrences():
    segs, ents, mapping = _two_seg_fixture_unannotated()
    md, _, retained = render_outputs(segs, ents, mapping)
    assert "张三" not in md and "李四" not in md  # 未标注处也零残留
    assert "经查，[人名_1]另欠[人名_2]两千元" in md
    assert retained["retained_fields"] == []  # 兜底不产生保留字段


def test_excluded_item_not_replaced_by_fallback():
    segs, ents, mapping = _two_seg_fixture_unannotated()
    mapping = apply_decisions(mapping, [{"id": "e1", "action": "exclude"}])
    md, _, retained = render_outputs(segs, ents, mapping)
    assert "张三" in md  # excluded 原文保留（含未标注出现处）
    assert "李四" not in md  # 未 excluded 项兜底仍生效，含未标注出现处
    # 保留字段只来自 span 标注处（段1，page 1），兜底不追加
    assert [r["text"] for r in retained["retained_fields"]] == ["张三"]


def test_excluded_substring_longer_replacement_wins():
    # 「张三」是「张三建设有限公司」的子串且被 excluded；
    # 未标注的公司名须整体按长文本替换，不能因短文本保留而漏替换。
    text = "张三建设有限公司与张三本人均到庭"
    segs = [Seg(text=text, page_idx=0, source="text")]
    ents = [Entity(id="a", text="张三", type="PERSON", start=9, end=11)]
    mapping = [
        MappingItem(id="e1", original_text="张三建设有限公司", entity_type="ORG", replacement="[公司_1]"),
        MappingItem(id="e2", original_text="张三", entity_type="PERSON", replacement="[人名_1]"),
    ]
    mapping = apply_decisions(mapping, [{"id": "e2", "action": "exclude"}])
    md, _, retained = render_outputs(segs, ents, mapping)
    assert "[公司_1]与张三本人均到庭" in md  # 长文本优先整体替换
    assert "张三建设有限公司" not in md
    assert "张三" in md  # excluded 独立出现处按定义保留
    assert [r["text"] for r in retained["retained_fields"]] == ["张三"]
