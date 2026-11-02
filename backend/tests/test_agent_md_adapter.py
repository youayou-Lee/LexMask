"""适配器规则测试：type 过滤/seal 哨兵/Markdown 与 LaTeX 剥除/image 补 OCR。全部合成内容。"""
import json

from app.services.agent_md_adapter import (
    clean_segments,
    enrich_image_blocks,
    normalize_content_list,
    strip_inline,
)
from app.services.agent_md_types import Seg


def _block(**kw):
    base = {"type": "text", "text": "正文", "page_idx": 0, "bbox": [0, 0, 100, 100]}
    base.update(kw)
    return base


def test_drops_header_footer_page_number():
    cl = [
        _block(type="header", text="XX人民法院"),
        _block(type="page_number", text="12"),
        _block(type="footer", text="第 12 页"),
        _block(text="本院认为"),
    ]
    segs, _ = clean_segments(cl)
    assert [s.text for s in segs] == ["本院认为"]


def test_seal_block_becomes_sentinel():
    cl = [_block(type="image", sub_type="seal", text="XX公司财务专用章")]
    segs, _ = clean_segments(cl)
    assert segs[0].text == "[公章]" and segs[0].source == "sentinel"


def test_table_block_stripped_to_row_text():
    html = "<table><tr><td>账号</td><td>62220202000</td></tr><tr><td>余额</td><td>134.37</td></tr></table>"
    cl = [_block(type="table", table_body=html)]
    segs, _ = clean_segments(cl)
    assert segs[0].source == "table"
    assert "账号" in segs[0].text and "62220202000" in segs[0].text
    assert "134.37" in segs[0].text and "<" not in segs[0].text


def test_image_block_becomes_placeholder_pending_enrich():
    cl = [_block(type="image", img_path="images/a.jpg", text="")]
    segs, warns = clean_segments(cl)
    assert segs[0].source == "img_ocr" and segs[0].text == ""
    assert segs[0].img_path == "images/a.jpg"
    assert any("images/a.jpg" in w for w in warns)


def test_unhandled_block_type_warns():
    cl = [_block(type="equation", text="E=mc^2")]
    segs, warns = clean_segments(cl)
    assert segs == []
    assert any("unhandled block type: equation" in w for w in warns)


def test_strip_inline_markdown_and_latex():
    assert strip_inline("## 本院认为") == "本院认为"
    assert strip_inline("金额**134.37**元") == "金额134.37元"
    assert strip_inline("$1 3 4 . 3 7$") == "1 3 4 . 3 7"
    assert strip_inline(r"\[ 1 3 4 . 3 7 \]") == "1 3 4 . 3 7"
    assert strip_inline(r"如\;下所述") == "如下所述"


def test_strip_inline_preserves_bare_parens_and_dollar():
    assert strip_inline("(2023)京01民终123号") == "(2023)京01民终123号"
    assert strip_inline("(一)") == "(一)"
    assert strip_inline("金额$100元") == "金额$100元"


def test_text_blocks_keep_reading_order_and_page():
    cl = [_block(text="第一页段", page_idx=0), _block(text="第二页段", page_idx=1)]
    segs, _ = clean_segments(cl)
    assert [s.page_idx for s in segs] == [0, 1]


class _FakeOcr:
    def __init__(self, texts=None, raise_on=None):
        self.texts = texts or []
        self.raise_on = raise_on

    def extract_text_boxes(self, image_bytes: bytes):
        if self.raise_on and image_bytes == self.raise_on:
            raise RuntimeError("ocr down")
        from app.services.ocr_service import OCRItem

        return [OCRItem(text=t, x=0, y=0, width=0, height=0, confidence=0.9) for t in self.texts]


def test_enrich_image_blocks_fills_ocr_text():
    segs = [Seg(text="", page_idx=0, source="img_ocr", img_path="images/a.jpg")]
    filled, warns = enrich_image_blocks(segs, {"images/a.jpg": b"jpg"}, _FakeOcr(texts=["截图文字"]))
    assert filled[0].text == "截图文字" and filled[0].source == "img_ocr"
    assert warns == []


def test_enrich_image_blocks_degrades_on_ocr_failure():
    segs = [Seg(text="", page_idx=0, source="img_ocr", img_path="images/a.jpg")]
    filled, warns = enrich_image_blocks(segs, {"images/a.jpg": b"jpg"}, _FakeOcr(raise_on=b"jpg"))
    assert filled[0].text == "[图片]" and filled[0].source == "sentinel"
    assert len(warns) == 1


def test_enrich_image_blocks_missing_image_degrades():
    segs = [Seg(text="", page_idx=0, source="img_ocr", img_path="images/a.jpg")]
    filled, warns = enrich_image_blocks(segs, {}, _FakeOcr())
    assert filled[0].text == "[图片]"
    assert len(warns) == 1


def test_enrich_keeps_order_and_empty_ocr_joined_by_newline():
    segs = [
        Seg(text="正文", page_idx=0, source="text"),
        Seg(text="", page_idx=1, source="img_ocr", img_path="i"),
    ]
    filled, _ = enrich_image_blocks(segs, {"i": b"x"}, _FakeOcr(texts=["行一", "行二"]))
    assert filled[0].text == "正文"
    assert filled[1].text == "行一\n行二"


# ---- C1：content_list v2 嵌套形态归一化（形态依 Task 0 报告 §6c 实测，内容合成）----

def _v2_nested_real_shape():
    """按 Task 0 §6c 实测结构复刻：外层按页 list-of-lists，文本在
    block["content"][f"{type}_content"][i]["content"]，块内无 page_idx。"""
    return [
        [
            {
                "type": "page_header",
                "content": {
                    "page_header_content": [
                        {"type": "text", "content": "XX人民法院 · 第 1 页"}
                    ]
                },
                "bbox": [111, 75, 566, 94],
            },
            {
                "type": "paragraph",
                "content": {
                    "paragraph_content": [
                        {"type": "text", "content": "张三借李四人民币**一万元**"}
                    ]
                },
                "bbox": [112, 75, 566, 94],
            },
        ],
        [
            {
                "type": "paragraph",
                "content": {
                    "paragraph_content": [
                        {"type": "text", "content": "银行账号 62220202000"},
                        {"type": "text", "content": "开户行 XX 银行"},
                    ]
                },
                "bbox": [112, 200, 566, 240],
            },
        ],
    ]


def test_normalize_flat_v1_passthrough_unchanged():
    cl = [_block(text="第一页段", page_idx=0), _block(text="第二页段", page_idx=1)]
    out = normalize_content_list(cl)
    assert out == cl
    # 透传不回写页码：v1 自带 page_idx 保持原值
    assert [o["page_idx"] for o in out] == [0, 1]


def test_normalize_nested_v2_flattens_with_outer_page_index():
    out = normalize_content_list(_v2_nested_real_shape())
    assert all(isinstance(o, dict) for o in out)
    assert [o["page_idx"] for o in out] == [0, 0, 1]
    # v2 块 type 映射 v1 名：paragraph→text（clean_segments 只认 v1 名）
    assert [o["type"] for o in out] == ["header", "text", "text"]
    assert out[1]["text"] == "张三借李四人民币**一万元**"
    # 同块多个 text 部件按序拼接
    assert out[2]["text"] == "银行账号 62220202000\n开户行 XX 银行"


def test_normalize_nested_v2_flows_through_clean_segments():
    segs, warns = clean_segments(normalize_content_list(_v2_nested_real_shape()))
    # page_header 被 clean_segments 过滤；文本剥 Markdown；页码来自外层下标
    assert [s.text for s in segs] == [
        "张三借李四人民币一万元", "银行账号 62220202000\n开户行 XX 银行",
    ]
    assert [s.page_idx for s in segs] == [0, 1]
    assert [s.source for s in segs] == ["text", "text"]
    assert warns == []


def test_normalize_nested_v2_plain_string_content_block():
    nested = [[{"type": "paragraph", "content": "纯字符串正文", "bbox": [0, 0, 1, 1]}]]
    out = normalize_content_list(nested)
    assert out[0]["page_idx"] == 0 and out[0]["text"] == "纯字符串正文"
    segs, _ = clean_segments(out)
    assert segs[0].text == "纯字符串正文" and segs[0].page_idx == 0


def test_normalize_nested_v2_preserves_seal_table_image_info():
    nested = [[
        {"type": "image", "sub_type": "seal",
         "content": {"image_content": [{"type": "image", "content": "XX公司财务专用章"}]}},
        {"type": "table",
         "content": {"table_content": [{"type": "table_cell", "content": "账号 6222",
                                        "table_body": "<table><tr><td>账号 6222</td></tr></table>"}]}},
        {"type": "image",
         "content": {"image_content": [{"type": "image", "content": "",
                                        "img_path": "images/p0.jpg"}]}},
    ]]
    out = normalize_content_list(nested)
    assert out[0].get("sub_type") == "seal"
    assert "table_body" in out[1]
    assert out[2].get("img_path") == "images/p0.jpg"
    segs, _ = clean_segments(out)
    assert segs[0].text == "[公章]" and segs[0].source == "sentinel"
    assert segs[1].source == "table" and "6222" in segs[1].text
    assert segs[2].source == "img_ocr" and segs[2].img_path == "images/p0.jpg"


def test_normalize_defends_against_malformed_input():
    assert normalize_content_list(None) == []
    assert normalize_content_list([]) == []
    assert normalize_content_list("not-a-list") == []
    # 嵌套外层混入裸块/垃圾值不炸
    nested = [[{"type": "paragraph", "content": {"paragraph_content": [{"type": "text",
                                                                        "content": "正文"}]}}],
              "garbage", 42]
    out = normalize_content_list(nested)
    assert [o["text"] for o in out] == ["正文"]


# ---- M1：enrich_image_blocks 键匹配鲁棒性（zip 前缀键 vs content_list img_path）----

def test_enrich_image_exact_key_still_works():
    segs = [Seg(text="", page_idx=0, source="img_ocr", img_path="images/p0.jpg")]
    filled, _ = enrich_image_blocks(segs, {"images/p0.jpg": b"jpg"}, _FakeOcr(texts=["切图文字"]))
    assert filled[0].text == "切图文字"


def test_enrich_image_zip_prefixed_key_unique_suffix_match():
    # 客户端以完整 zip 路径为键：<stem>/<parse_method>/images/p0.jpg
    segs = [Seg(text="", page_idx=0, source="img_ocr", img_path="images/p0.jpg")]
    images = {"case01/auto/images/p0.jpg": b"jpg"}
    filled, warns = enrich_image_blocks(segs, images, _FakeOcr(texts=["切图文字"]))
    assert filled[0].text == "切图文字" and warns == []


def test_enrich_image_basename_unique_match():
    segs = [Seg(text="", page_idx=0, source="img_ocr", img_path="p0.jpg")]
    images = {"case01/auto/images/p0.jpg": b"jpg", "case01/auto/images/p1.jpg": b"j2"}
    filled, _ = enrich_image_blocks(segs, images, _FakeOcr(texts=["按名命中"]))
    assert filled[0].text == "按名命中"


def test_enrich_image_ambiguous_basename_degrades_to_sentinel():
    segs = [Seg(text="", page_idx=0, source="img_ocr", img_path="p0.jpg")]
    images = {"a/images/p0.jpg": b"j1", "b/images/p0.jpg": b"j2"}
    filled, warns = enrich_image_blocks(segs, images, _FakeOcr())
    assert filled[0].text == "[图片]" and filled[0].source == "sentinel"
    assert len(warns) == 1


# ---- Issue#219：4.x docvortex.middle schema 分流与归一 ----

def _v4_doc(blocks_by_page):
    """docvortex.middle 形状合成：[(page_idx, [block, ...]), ...]（全合成文本）。"""
    return {
        "schema": "docvortex.middle", "schema_version": "2.0",
        "metadata": {"producer": {"name": "mineru", "version": "4.1.3"}},
        "pages": [
            {"page_idx": idx, "blocks": blocks}
            for idx, blocks in blocks_by_page
        ],
        "is_full_document": True,
    }


class TestDetectSchema:
    def test_v1_flat_list(self):
        from app.services.agent_md_adapter import detect_schema
        assert detect_schema([{"type": "text", "text": "x"}]) == "v1"

    def test_v2_nested_list(self):
        from app.services.agent_md_adapter import detect_schema
        assert detect_schema([[{"type": "text", "content": {}}]]) == "v2"

    def test_v4_docvortex(self):
        from app.services.agent_md_adapter import detect_schema
        assert detect_schema(_v4_doc([(0, [])])) == "v4"

    def test_unknown_shapes(self):
        from app.services.agent_md_adapter import detect_schema
        assert detect_schema({"middle": True}) == "unknown"
        assert detect_schema({"schema": "docvortex.other"}) == "unknown"
        # R2 形似负例：判等放宽为 startswith("docvortex.middle") 时此例必须仍 unknown
        assert detect_schema({"schema": "docvortex.middle.v3"}) == "unknown"
        assert detect_schema("text") == "unknown"
        assert detect_schema(None) == "unknown"

    def test_schema_key_exact_match_not_prefix(self):
        # 变异③判据：schema 判等是全等，不是前缀
        from app.services.agent_md_adapter import detect_schema
        assert detect_schema({"schema": "docvortex"}) == "unknown"


class TestNormalizeDocvortex:
    def test_text_and_paragraph_title_to_text_with_newline_join(self):
        from app.services.agent_md_adapter import normalize_docvortex
        doc = _v4_doc([(0, [
            {"type": "text", "index": 0, "bbox": [0, 0, 1, 1],
             "content": [{"type": "text", "content": "段一"}, {"type": "text", "content": "段二"}]},
            {"type": "paragraph_title", "index": 1, "level": 2,
             "content": [{"type": "text", "content": "标题"}]},
        ])])
        flat, warns = normalize_docvortex([doc])
        assert warns == []
        assert flat[0]["type"] == "text" and flat[0]["text"] == "段一\n段二"
        assert flat[0]["page_idx"] == 0
        assert flat[1]["type"] == "text" and flat[1]["text"] == "标题"
        assert "level" not in flat[1]

    def test_table_body_floats_and_text_parts_become_caption(self):
        from app.services.agent_md_adapter import normalize_docvortex
        doc = _v4_doc([(1, [
            {"type": "table", "index": 2, "bbox": [0, 0, 1, 1],
             "content": [
                 {"type": "text", "content": "表：证据清单"},
                 {"type": "table_body", "content": "<table><tr><td>1</td></tr></table>"},
             ]},
        ])])
        flat, warns = normalize_docvortex([doc])
        assert flat[0]["type"] == "table"
        assert flat[0]["table_body"] == "<table><tr><td>1</td></tr></table>"
        assert flat[0]["table_caption"] == "表：证据清单"
        assert flat[0]["page_idx"] == 1

    def test_image_block_maps_without_img_path(self):
        from app.services.agent_md_adapter import normalize_docvortex
        doc = _v4_doc([(0, [
            {"type": "image", "index": 7, "bbox": [0, 0, 1, 1],
             "content": [{"type": "image_body", "index": 7, "content": ""}]},
        ])])
        flat, warns = normalize_docvortex([doc])
        assert flat[0]["type"] == "image"
        assert "img_path" not in flat[0]

    def test_unknown_block_type_passthrough_counted_by_clean(self):
        from app.services.agent_md_adapter import CleanStats, clean_segments, normalize_docvortex
        doc = _v4_doc([(0, [{"type": "equation", "index": 3,
                             "content": [{"type": "text", "content": "E=mc2"}]}])])
        flat, warns = normalize_docvortex([doc])
        assert flat[0]["type"] == "equation"
        stats = CleanStats()
        segs, cwarns = clean_segments(flat, stats=stats)
        assert segs == [] and stats.dropped_block_types == {"equation": 1}
        assert any("equation" in w for w in cwarns)

    def test_page_idx_from_outer_and_block_self_value_not_overwritten(self):
        from app.services.agent_md_adapter import normalize_docvortex
        doc = _v4_doc([(2, [{"type": "text", "index": 0, "page_idx": 5,
                             "content": [{"type": "text", "content": "自带页码"}]}])])
        flat, _ = normalize_docvortex([doc])
        assert flat[0]["page_idx"] == 5  # 块自带不覆写（同 v2 口径）

    def test_duplicate_docs_deduped(self):
        from app.services.agent_md_adapter import normalize_docvortex
        doc = _v4_doc([(0, [{"type": "text", "index": 0,
                             "content": [{"type": "text", "content": "唯一正文"}]}])])
        flat, warns = normalize_docvortex([doc, json.loads(json.dumps(doc))])
        assert warns == []
        assert len(flat) == 1

    def test_multiple_distinct_docs_warn(self):
        from app.services.agent_md_adapter import normalize_docvortex
        d1 = _v4_doc([(0, [{"type": "text", "index": 0,
                            "content": [{"type": "text", "content": "甲"}]}])])
        d2 = _v4_doc([(0, [{"type": "text", "index": 0,
                            "content": [{"type": "text", "content": "乙"}]}])])
        flat, warns = normalize_docvortex([d1, d2])
        assert len(flat) == 2
        assert any("mineru4_multiple_docs" in w for w in warns)

    def test_empty_and_malformed_inputs(self):
        from app.services.agent_md_adapter import normalize_docvortex
        assert normalize_docvortex([]) == ([], [])
        flat, _ = normalize_docvortex([{"schema": "docvortex.middle"}])  # 无 pages
        assert flat == []
        flat, _ = normalize_docvortex([_v4_doc([(0, [{"type": "text"}])])])  # 块无 content
        assert flat[0]["type"] == "text" and "text" not in flat[0]

    def test_normalize_flows_through_clean_segments(self):
        # 端到端：v4 → 扁平 → clean_segments 产段（含表格剥行）
        from app.services.agent_md_adapter import clean_segments, normalize_docvortex
        doc = _v4_doc([
            (0, [{"type": "text", "index": 0,
                  "content": [{"type": "text", "content": "原告：张示例"}]}]),
            (1, [{"type": "table", "index": 0,
                  "content": [{"type": "table_body",
                               "content": "<table><tr><td>序号</td></tr><tr><td>1</td></tr></table>"}]}]),
        ])
        flat, _ = normalize_docvortex([doc])
        segs, warns = clean_segments(flat)
        assert [s.text for s in segs] == ["原告：张示例", "序号\n1"]
        assert [s.page_idx for s in segs] == [0, 1]
        assert warns == []


# ---- Issue#219 R4：markdown_body_chars（docvortex_shortfall 守卫的参照口径）----

def test_markdown_body_chars_strips_markup_and_counts_body():
    from app.services.agent_md_adapter import markdown_body_chars
    md = "# 标题\n\n正文一段\n\n<!-- page 1 of 3 -->\n\n![图](images/p0.jpg)\n"
    # 正文 = "标题" + "正文一段" = 6；页注释与图片语法剥除，空白不计
    assert markdown_body_chars(md) == 6


def test_markdown_body_chars_excludes_table_html():
    from app.services.agent_md_adapter import markdown_body_chars
    # 表格字符由 v4 table_body 侧计入，md 侧剥 <table>…</table>
    assert markdown_body_chars("前文<table><tr><td>一</td></tr></table>后文") == 4


def test_markdown_body_chars_probe_anchor_ratio():
    """探针锚点回归（0.9684 的可复算性）：探针 md 剥除后 443 字。"""
    from app.services.agent_md_adapter import markdown_body_chars
    # 探针 md 的首行形态（页注释 + 标题 + 合并行）——构造等价片段验剥除口径
    md = ("<!-- page 1 of 3 -->\n\n## 示例市示例区人民法院 民事判决书\n\n"
          "案号:（2026）示0101民初219号\n")
    expected = len("示例市示例区人民法院 民事判决书案号:（2026）示0101民初219号".replace(" ", ""))
    assert markdown_body_chars(md) == expected


def test_markdown_body_chars_freezes_probe_anchor_401():
    # 门⑥ M-5：探针 md（全合成 canary，可入公开树）原文嵌入，字面冻结可复算
    # （现行口径真值 401；粗口径 443 未剥页注释，已弃用——见设计文档 I2 锚定更新）
    from app.services.agent_md_adapter import markdown_body_chars
    probe_md = '<!-- page 1 of 3 -->\n\n## 示例市示例区人民法院 民事判决书\n\n案号:（2026）示0101民初219号（虚构案号，仅供格式探针）原告:张示例，男，1970年1月1日出生，住示例市示例区示例街道1号。\n\n被告:李演示，男，1980年2月2日出生，住示例市示例区演示路2号。\n\n本院于2026年3月1日立案受理，依法适用普通程序，公开开庭进行了审理。\n\n<!-- page 2 of 3 -->\n\n证据清单\n\n| 序号 | 证据名称 | 页码 | 证明目的 |\n| --- | --- | --- | --- |\n| 1 | 借条一份 | 1-2 | 证明借款关系成立 |\n| 2 | 银行流水 | 3-5 | 证明款项交付 |\n| 3 | 微信聊天记录 | 6-9 | 证明催讨事实 |\n\n<!-- page 3 of 3 -->\n\n## 审理查明\n\n经审理查明:2025年6月1日，被告向原告借款人民币壹拾万元整，约定2025年12月31日前归还。\n\n上述事实，有原告提交的借条、银行流水及庭审笔录在卷佐证，本院予以确认。\n\n依照《中华人民共和国民法典》第六百七十五条之规定，判决如下:\n\n被告李演示于本判决生效之日起十日内偿还原告张示例借款本金人民币壹拾万元整。\n\n[图形占位:示例印章轮廓，非真实印章]'
    assert markdown_body_chars(probe_md) == 401
def test_unknown_part_type_warns_not_absorbed():
    # 门⑥ I-2：R5「未知部件 type → 告警」的接线判据——删该分支全量必须红
    from app.services.agent_md_adapter import normalize_docvortex
    doc = _v4_doc([(0, [{"type": "text", "index": 0,
                         "content": [{"type": "text", "content": "正常正文"},
                                      {"type": "equation", "content": "E=mc2"}]}])])
    flat, warns = normalize_docvortex([doc])
    assert any("unhandled docvortex part type: equation" in w for w in warns)
    assert flat[0]["text"] == "正常正文"  # 未知部件不被 text 吸收


def test_malformed_structure_warns_not_silent():
    # 门⑥ I-3：畸形结构（schema 漂移）跳过必须留痕，不静默蒸发
    from app.services.agent_md_adapter import normalize_docvortex
    flat, warns = normalize_docvortex(["not-a-doc"])
    assert flat == [] and any("mineru4_malformed" in w for w in warns)
    flat, warns = normalize_docvortex([{"schema": "docvortex.middle", "pages": "oops"}])
    assert flat == [] and any("mineru4_malformed" in w for w in warns)
    doc = {"schema": "docvortex.middle", "pages": [
        {"page_idx": 0, "blocks": "oops"},
        {"page_idx": 1, "blocks": [{"type": "text", "content": [
            {"type": "text", "content": "后页正常"}]}]},
        "not-a-page",
    ]}
    flat, warns = normalize_docvortex([doc])
    assert len(flat) == 1 and flat[0]["page_idx"] == 1
    assert any("mineru4_malformed" in w for w in warns)
