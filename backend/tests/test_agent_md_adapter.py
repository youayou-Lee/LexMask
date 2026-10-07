"""适配器规则测试：type 过滤/seal 哨兵/Markdown 与 LaTeX 剥除。全部合成内容。"""
from app.services.agent_md_adapter import clean_segments, strip_inline


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
    assert any("images/a.jpg" in w for w in warns)


def test_strip_inline_markdown_and_latex():
    assert strip_inline("## 本院认为") == "本院认为"
    assert strip_inline("金额**134.37**元") == "金额134.37元"
    assert strip_inline("$1 3 4 . 3 7$") == "1 3 4 . 3 7"
    assert strip_inline(r"\[ 1 3 4 . 3 7 \]") == "1 3 4 . 3 7"
    assert strip_inline(r"如\;下所述") == "如下所述"


def test_text_blocks_keep_reading_order_and_page():
    cl = [_block(text="第一页段", page_idx=0), _block(text="第二页段", page_idx=1)]
    segs, _ = clean_segments(cl)
    assert [s.page_idx for s in segs] == [0, 1]
