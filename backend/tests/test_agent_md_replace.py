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
