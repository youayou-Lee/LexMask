"""Issue#281 供料产物两处替换缺陷（TDD 先红）。

1. 跨页切开的实体漏替换：实体原文被「页脚数字 + <!-- page N of M -->」切开，
   产物侧不存在连续形态，字节级 replace / 空白柔性正则都匹配不到；
2. 类型标签叠字：OCR 原文自带「案号」标签，替换值又是「案号N」，产物出现
   「案号案号N」。

核心形态（与本地真卷一致，此处全部合成数据）：
- 同一案号在上一页末被切开、下一页首续上，且另一 OCR 空白变体项把下一页
  的连续形态先吃掉——被切开项的覆盖闸/残留闸因此双假绿，真实案号出厂。
"""
from app.models.entity_schemas import Entity
from app.services.agent_md_replace import (
    render_outputs,
    render_outputs_on_structure,
    verify_structure_integrity,
)
from app.services.agent_md_types import MappingItem, Seg

SPLIT_MD = (
    "<!-- page 1 of 2 -->\n\n"
    "异议人提起执行异议之诉[案号（2023）桂0000民初00\n\n"
    "1\n\n"
    "<!-- page 2 of 2 -->\n\n"
    "号]，法院作出（2023）桂0000民初00号判决。\n"
)

LABEL_MD = "<!-- page 1 of 1 -->\n\n异议人提出[案号（2023）桂0000民初00号]。\n"


def _plain_item():
    return MappingItem(id="m1", original_text="（2023）桂0000民初00号",
                       entity_type="CASE_NUMBER", replacement="案号1")


def _ws_variant_item():
    return MappingItem(id="m2", original_text="（2023）桂0000 民初00号",
                       entity_type="CASE_NUMBER", replacement="案号2")


def test_page_split_entity_replaced_even_when_ws_variant_eats_contiguous_form():
    """真卷形态：页 3→4 切开的那处必须被替换，即使空白变体先吃掉连续形态。"""
    md, mapping_json, _ = render_outputs_on_structure(
        SPLIT_MD, [], [], [_plain_item(), _ws_variant_item()])
    verify_structure_integrity(SPLIT_MD, md)
    assert "（2023）桂0000" not in md  # 切开形态不得残留真实案号
    assert "案号1" in md  # 切开形态落占位符
    assert "案号2" in md  # 连续形态仍按空白变体项替换
    assert "<!-- page 1 of 2 -->" in md and "<!-- page 2 of 2 -->" in md
    assert mapping_json["items"][0]["replacement"] == "案号1"


def test_page_split_entity_alone_is_covered_not_rejected():
    """只有切开形态、无连续形态：旧覆盖闸会 representation_mismatch 拒出厂。"""
    md, _, _ = render_outputs_on_structure(SPLIT_MD, [], [], [_plain_item()])
    verify_structure_integrity(SPLIT_MD, md)
    assert "（2023）桂0000" not in md
    assert "案号1" in md


def test_page_split_keeps_page_number_and_marker():
    """替换值必须落在页首段、页脚数字与页注释原样保留（结构对账不变）。"""
    md, _, _ = render_outputs_on_structure(SPLIT_MD, [], [], [_plain_item()])
    assert "\n\n1\n\n<!-- page 2 of 2 -->" in md
    assert md.count("<!-- page 2 of 2 -->") == 1


def test_label_duplication_collapsed_on_structure():
    md, mapping_json, _ = render_outputs_on_structure(
        LABEL_MD, [], [], [_plain_item()])
    verify_structure_integrity(LABEL_MD, md)
    assert "[案号1]" in md
    assert "案号案号" not in md
    assert mapping_json["items"][0]["replacement"] == "案号1"


def test_label_duplication_collapsed_on_legacy_render():
    """legacy 拍平重建路径同样要收口（OCR 自带标签在 span 外）。"""
    segs = [Seg(text="异议人提出[案号（2023）桂0000民初00号]。", page_idx=0, source="text")]
    entities = [Entity(id="e1", text="（2023）桂0000民初00号", type="CASE_NUMBER",
                       start=8, end=24)]
    md, _, _ = render_outputs(segs, entities, [_plain_item()])
    assert "[案号1]" in md
    assert "案号案号" not in md


def test_excluded_item_not_touched_by_page_split_matcher():
    """excluded 项（如机构保留策略）在跨页形态下也不得替换。"""
    item = _plain_item()
    item.excluded = True
    md, mapping_json, _ = render_outputs_on_structure(SPLIT_MD, [], [], [item])
    verify_structure_integrity(SPLIT_MD, md)
    assert "（2023）桂0000民初00" in md
    assert "案号1" not in md
    assert mapping_json["items"][0]["excluded"] is True
