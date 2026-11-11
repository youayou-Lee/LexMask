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
    # 切开替换与主循环同口径按 len 降序：空白变体项（更长）先吃掉切开处，
    # 与连续形态落同一个占位符（案号2）——同一事实不应产出两个占位符。
    assert "案号1" not in md
    assert md.count("案号2") == 2
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


# ---- 追加：产物面 Markdown 转义 / 全角形态差（表示失配自愈）---------------
#
# 真卷形态：检测面 segs 拿到的是 `wxid_fixture0000001`，而 zip 官方 md 把
# 下划线转义成 `wxid\_fixture0000001`；另有全角冒号/括号的半角化差。字面
# 比对必然失配，representation_mismatch 会把整卷拦死（真卷实测 2/5 卷）。

ESCAPE_MD = (
    "<!-- page 1 of 1 -->\n\n"
    "（30）其微信号:wxid\\_fixture0000001 的聊天记录与转账截图。\n"
)

FULLWIDTH_MD = (
    "<!-- page 1 of 1 -->\n\n"
    "（31）其微信号：wxid_fixture0000002）于 2021 年 1 月 1 日。\n"
)


def _wechat_item(mid="m3", original="微信号:wxid_fixture0000001"):
    return MappingItem(id=mid, original_text=original,
                       entity_type="WECHAT_ID", replacement="微信1")


def test_markdown_escaped_entity_replaced():
    """md 里 `wxid\\_...`（md 转义）与原文 `wxid_...` 必须被视为同一实体。"""
    md, _, _ = render_outputs_on_structure(ESCAPE_MD, [], [], [_wechat_item()])
    verify_structure_integrity(ESCAPE_MD, md)
    assert "wxid" not in md
    assert "微信1" in md


def test_markdown_escaped_bare_id_replaced():
    """不带「微信号:」前缀的裸 ID 项同样要能命中转义形态。"""
    md, _, _ = render_outputs_on_structure(
        ESCAPE_MD, [], [], [_wechat_item(original="wxid_fixture0000001")])
    assert "wxid" not in md
    assert "微信1" in md


def test_fullwidth_punctuation_variant_replaced():
    """md 全角冒号 / 全角括号与检测面半角形态视为同一实体。"""
    md, _, _ = render_outputs_on_structure(
        FULLWIDTH_MD, [], [], [_wechat_item(original="微信号:wxid_fixture0000002")])
    verify_structure_integrity(FULLWIDTH_MD, md)
    assert "wxid" not in md
    assert "微信1" in md


def test_escape_and_whitespace_variant_replaced():
    """转义 + 空白差叠加：折叠视图定位后按原文区间替换。"""
    src = "<!-- page 1 of 1 -->\n\n其微信 wxid\\_fixture0000003 ) 于 2021 年转账。\n"
    md, _, _ = render_outputs_on_structure(
        src, [], [], [_wechat_item(original="wxid_fixture0000003 ) 于 2021 年")])
    assert "wxid" not in md
    assert "微信1" in md


def test_absent_entity_still_fails_closed():
    """自愈只放宽「形态差」，真失配（md 里根本没有该文本）仍须拦死。"""
    import pytest

    with pytest.raises(ValueError, match="representation_mismatch"):
        render_outputs_on_structure(
            ESCAPE_MD, [], [], [_wechat_item(original="wxid_notpresent000")])


# ---- 追加：短数字实体吃掉页标记（真卷扫描线 86→84 实测）--------------------
#
# NER 在扫描卷里产出 CASE_NUMBER='10'/'11'（纯数字误报），整文 replace 直接把
# `<!-- page 10 of 86 -->` 里的页号改掉，页标记序列对账判结构破坏、整卷 fail。

MARKER_MD = (
    "<!-- page 10 of 86 -->\n\n"
    "被害人陈述：案发时间为 10 月，损失合计 8600 元。\n\n"
    "<!-- page 11 of 86 -->\n\n"
    "附卷页码说明见 of 86 页清单。\n"
)


def test_short_numeric_entity_excluded_in_draft():
    """纯数字短原文（'10'/'008'）默认保留：整文替换会命中页号/年份。"""
    from app.services.agent_md_ner import build_mapping_draft

    items = build_mapping_draft([
        Entity(id="e1", text="10", type="CASE_NUMBER", start=0, end=2),
        Entity(id="e2", text="008", type="CASE_NUMBER", start=3, end=6),
        Entity(id="e3", text="（2023）桂0000民初00号", type="CASE_NUMBER", start=7, end=27),
    ])
    assert [i.excluded for i in items] == [True, True, False]


def test_short_numeric_item_still_rejected_by_renderer():
    """律师把短数字项显式改成 keep→replace 时，渲染层硬闸兜底。"""
    import pytest

    with pytest.raises(ValueError, match="degenerate_original"):
        render_outputs_on_structure(
            MARKER_MD, [], [],
            [MappingItem(id="m9", original_text="10", entity_type="CASE_NUMBER",
                         replacement="案号67")])


def test_page_markers_masked_roundtrip():
    """页标记在替换期间换成哨兵，还原后逐字节一致。"""
    from app.services.agent_md_replace import (
        _mask_page_markers,
        _unmask_page_markers,
    )

    masked, markers = _mask_page_markers(MARKER_MD)
    assert "<!-- page" not in masked
    assert len(markers) == 2
    assert _unmask_page_markers(masked, markers) == MARKER_MD


def test_marker_content_never_replaced():
    """映射原文与页标记同形（'of 86'）时，页标记必须原样保留。"""
    md, _, _ = render_outputs_on_structure(
        MARKER_MD, [], [],
        [MappingItem(id="m10", original_text="of 86", entity_type="CASE_NUMBER",
                     replacement="页码1")])
    verify_structure_integrity(MARKER_MD, md)
    assert "<!-- page 10 of 86 -->" in md and "<!-- page 11 of 86 -->" in md
    assert "见 页码1 页清单" in md


def test_marker_digit_neighbour_replacement_keeps_marker():
    """正文里的短数字邻近页标记时，替换只落在正文，页标记不动。"""
    md, _, _ = render_outputs_on_structure(
        MARKER_MD, [], [],
        [MappingItem(id="m11", original_text="10 月", entity_type="TIME",
                     replacement="月份1")])
    verify_structure_integrity(MARKER_MD, md)
    assert "<!-- page 10 of 86 -->" in md
    assert "案发时间为 月份1" in md


# ---- 追加：正则补层（数字类 PII / 微信 ID 召回兜底）------------------------
#
# 真卷 86 页扫描件验收时 NER 漏了 21 个身份证号、22 个手机号——供料产物要给
# 下游 AI 用，漏一个就是一次泄露。补一层与 vl_md 同口径的正则。

def _segs(*texts_pages):
    return [Seg(text=t, page_idx=p, source="text") for t, p in texts_pages]


def test_regex_pii_entities_offsets_and_pages():
    from app.services.agent_md_ner import regex_pii_entities

    segs = _segs(
        ("身份证号110101199001010001", 0),
        ("联系电话13800138000，微信:wxid_fixture0000001", 1),
    )
    ents = {e.type: e for e in regex_pii_entities(segs)}
    assert ents["ID_CARD"].text == "110101199001010001"
    assert (ents["ID_CARD"].start, ents["ID_CARD"].end) == (4, 22)
    assert ents["ID_CARD"].page == 1
    assert ents["PHONE"].text == "13800138000" and ents["PHONE"].page == 2
    assert ents["WECHAT_ALIPAY"].text == "wxid_fixture0000001"
    # 全局偏移口径：第二段起点 = 第一段长 + "\n\n"
    assert ents["PHONE"].start == len("身份证号110101199001010001") + 2 + 4


def test_regex_pii_skips_overlap_with_ner():
    from app.services.agent_md_ner import merge_regex_entities

    text = "电话13800138000"
    ner = [Entity(id="e1", text="13800138000", type="PHONE",
                  start=2, end=13, page=1)]
    merged = merge_regex_entities(ner, _segs((text, 0)))
    assert merged == ner  # 模型已召回，正则不再重复出项


def test_regex_pii_wxid_masked_by_draft():
    from app.services.agent_md_ner import build_mapping_draft, regex_pii_entities

    ents = regex_pii_entities(_segs(("微信号:wxid_fixture0000001", 0)))
    items = build_mapping_draft(ents)
    assert len(items) == 1
    assert items[0].entity_type == "WECHAT_ALIPAY"
    assert items[0].replacement == "支付账号1"
    assert items[0].excluded is False


def test_regex_pii_kept_when_ner_entity_is_junk_overlap():
    """NER 的 1 字符垃圾实体（真卷 CASE_NUMBER:'4'）不能吞掉整个身份证号。"""
    from app.services.agent_md_ner import merge_regex_entities

    text = "张三 110101199001010000 副校长"
    ner = [Entity(id="e1", text="4", type="CASE_NUMBER", start=4, end=5, page=1)]
    merged = merge_regex_entities(ner, _segs((text, 0)))
    assert any(e.type == "ID_CARD" and e.text == "110101199001010000" for e in merged)


def test_regex_pii_dropped_when_ner_covers_whole_span():
    """NER 已完整覆盖（且会打码）时不重复出项。"""
    from app.services.agent_md_ner import merge_regex_entities

    text = "身份证号110101199001010000"
    ner = [Entity(id="e1", text="110101199001010000", type="ID_CARD",
                  start=4, end=22, page=1)]
    assert merge_regex_entities(ner, _segs((text, 0))) == ner


def test_regex_pii_type_wins_over_wrong_ner_type():
    """同一 18 位号码被 NER 标成 CASE_NUMBER 时，产物应按 ID_CARD 出占位符。"""
    from app.services.agent_md_ner import merge_regex_entities

    text = "身份证号码:110101199001010000"
    ner = [Entity(id="e1", text="110101199001010000", type="CASE_NUMBER",
                  start=6, end=24, page=1)]
    merged = merge_regex_entities(ner, _segs((text, 0)))
    assert [(e.type, e.text) for e in merged] == [("ID_CARD", "110101199001010000")]


CONFUSABLE_SPLIT_MD = (
    "<!-- page 1 of 2 -->\n\n"
    "异议人提起执行异议之诉[案号（2023）桂0000民初0O\n\n"
    "1\n\n"
    "<!-- page 2 of 2 -->\n\n"
    "号]，法院作出（2023）桂0000民初00号判决。\n"
)


def test_page_split_heals_ocr_confusable_variant():
    """跨页切开处 OCR 把 0 认成 O（同族易混字符）：仍须按同一实体替换。

    真实扫描件里被切开的一半经常带 OCR 字符级噪声（0/O、1/l、5/S、8/B），
    若切分正则只认逐字相等，这一形态会静默漏网。
    """
    md, _, _ = render_outputs_on_structure(
        CONFUSABLE_SPLIT_MD, [], [], [_plain_item()])
    verify_structure_integrity(CONFUSABLE_SPLIT_MD, md)
    assert "民初0O" not in md
    assert "（2023）桂0000民初0O" not in md
    assert "案号1" in md


def test_confusable_variant_only_split_covered_not_rejected():
    """只有 OCR 易混变体的切开形态（无连续形态）也不得被覆盖闸拒出厂。"""
    md_src = (
        "<!-- page 1 of 2 -->\n\n"
        "异议人提起执行异议之诉[案号（2023）桂0000民初0O\n\n"
        "1\n\n"
        "<!-- page 2 of 2 -->\n\n"
        "号]。\n"
    )
    md, _, _ = render_outputs_on_structure(
        md_src, [], [], [_plain_item()])
    verify_structure_integrity(md_src, md)
    assert "民初0O" not in md
    assert "案号1" in md


# ---- 门⑥ 冷评审追加：同文本多切点 / 异形噪声 / 嵌套前缀 / 4 位数字 ----

def _two_split_md(noise: str) -> str:
    return (
        "<!-- page 1 of 3 -->\n\n"
        "第一处[案号（2023）桂0000民初00\n\n1\n\n<!-- page 2 of 3 -->\n\n号]。\n\n"
        "另处（2023）桂0000民初00\n\n" + noise + "\n\n"
        "<!-- page 3 of 3 -->\n\n号]。\n"
    )


def test_two_split_occurrences_with_odd_noise_do_not_leak():
    """同文本被切开两处、噪声形态不同（4 位页脚/大写 Page/中文页脚）都必须替换。"""
    for noise in ("1000", "<!-- Page 2 of 3 -->", "第 2 页 共 3 页"):
        src = _two_split_md(noise)
        md, _, _ = render_outputs_on_structure(src, [], [], [_plain_item()])
        assert "（2023）桂0000" not in md, noise
        assert md.count("案号1") == 2, noise


def test_split_ordering_long_entity_not_cut_by_prefix():
    """短实体是长实体的前缀时，切开替换必须按 len 降序，不得把长实体切断。"""
    src = ("<!-- page 1 of 2 -->\n\n张\n\n1\n\n<!-- page 2 of 2 -->\n\n"
           "三丰律师事务所，张三丰本人。\n")
    items = [
        MappingItem(id="m1", original_text="张三", entity_type="PERSON", replacement="人1"),
        MappingItem(id="m2", original_text="张三丰", entity_type="PERSON", replacement="人2"),
    ]
    md, _, _ = render_outputs_on_structure(src, [], [], items)
    assert "人2丰律师事务所" not in md
    assert "人1三丰律师事务所" in md or "人2" in md
    assert "人2本人" in md


def test_split_entity_four_page_footer_still_fails_closed_when_unmatched():
    """噪声超出容错集（如正文夹在中间）时仍 fail-closed，不能静默出厂。"""
    src = ("<!-- page 1 of 2 -->\n\n[案号（2023）桂0000民初00\n\n"
           "正文无关一句话\n\n<!-- page 2 of 2 -->\n\n号]。\n")
    import pytest

    with pytest.raises(ValueError, match="representation_mismatch|zero_residual_violation"):
        render_outputs_on_structure(src, [], [], [_plain_item()])


def test_regex_layer_respects_disabled_types():
    """识别范围契约：owner 关掉 PHONE/ID_CARD 时补层不得越权打码。"""
    from app.services.agent_md_ner import regex_pii_entities

    segs = _segs(("电话13800138000，身份证110101199001010000", 0))
    only_phone = regex_pii_entities(segs, {"PHONE"})
    assert [e.type for e in only_phone] == ["PHONE"]
    assert regex_pii_entities(segs, set()) == []


def test_four_digit_numeric_draft_default_excluded():
    """4 位纯数字（年份/4 位页码）同样默认保留——2023 被替换会毁年份与案号本体。"""
    from app.services.agent_md_ner import build_mapping_draft

    entities = [
        Entity(id="e1", text="2023", type="CASE_NUMBER", start=0, end=4),
        Entity(id="e2", text="（2023）桂0000民初00号", type="CASE_NUMBER", start=5, end=24),
    ]
    items = build_mapping_draft(entities)
    by_orig = {i.original_text: i for i in items}
    assert by_orig["2023"].excluded is True
    assert by_orig["（2023）桂0000民初00号"].excluded is False


def test_four_digit_numeric_rejected_by_render_gate():
    """审阅里把 4 位纯数字改 keep→replace 时，渲染层硬闸 fail-closed。"""
    import pytest

    item = MappingItem(id="m1", original_text="2023", entity_type="CASE_NUMBER",
                       replacement="案号1")
    with pytest.raises(ValueError, match="degenerate_original"):
        render_outputs_on_structure("<!-- page 1 of 1 -->\n\n2023 年。\n", [], [], [item])


def test_label_collapse_does_not_rewrite_unrelated_body_text():
    """收口只作用于本次替换造出的叠字；正文里本来就有「案号案号7」不得被改。"""
    src = "<!-- page 1 of 1 -->\n\n原告张三；说明：案号案号7 与 案号7。\n"
    item = MappingItem(id="m1", original_text="张三", entity_type="PERSON",
                       replacement="案号7")
    md, _, _ = render_outputs_on_structure(src, [], [], [item])
    assert "原告案号7" in md
    assert "说明：案号案号7 与 案号7。" in md


def test_entity_type_ids_accept_config_objects_strings_and_dicts():
    """类型配置对象经 str() 是 repr 而非 id；补层过滤必须按 id 取。"""
    from types import SimpleNamespace

    from app.services.agent_md_pipeline_service import _entity_type_ids

    ids = _entity_type_ids([
        SimpleNamespace(id="PHONE"),
        "ID_CARD",
        {"id": "WECHAT_ALIPAY"},
        SimpleNamespace(type_id="BANK_CARD"),
        SimpleNamespace(),
    ])
    assert ids == {"PHONE", "ID_CARD", "WECHAT_ALIPAY", "BANK_CARD"}


def test_regex_layer_works_with_config_objects():
    """回归钉：用配置对象当识别范围时，身份证/手机仍必须被打码。"""
    from types import SimpleNamespace

    from app.services.agent_md_ner import merge_regex_entities
    from app.services.agent_md_pipeline_service import _entity_type_ids

    text = "身份证110101199001010000，电话13800138000。"
    merged = merge_regex_entities(
        [], _segs((text, 0)),
        _entity_type_ids([SimpleNamespace(id="ID_CARD"), SimpleNamespace(id="PHONE")]),
    )
    kinds = sorted({e.type for e in merged})
    assert kinds == ["ID_CARD", "PHONE"]


def test_label_dup_created_by_page_split_is_collapsed():
    """跨页切开处原文自带「案号」标签：替换后「[案号案号1」必须收成「[案号1」。"""
    md = (
        "<!-- page 1 of 2 -->\n\n异议人提出[案号（2023）桂0000民初00\n\n"
        "1\n\n<!-- page 2 of 2 -->\n\n号]。\n"
    )
    item = MappingItem(id="m1", original_text="（2023）桂0000民初00号",
                       entity_type="CASE_NUMBER", replacement="案号1")
    out, _, _ = render_outputs_on_structure(md, [], [], [item])
    assert "案号案号" not in out
    assert "[案号1" in out
