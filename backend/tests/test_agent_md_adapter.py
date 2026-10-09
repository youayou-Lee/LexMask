"""适配器规则测试：type 过滤/seal 哨兵/Markdown 与 LaTeX 剥除/image 补 OCR。全部合成内容。"""
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
