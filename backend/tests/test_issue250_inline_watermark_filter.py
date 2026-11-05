"""Issue#250：inline 平铺水印（并入主流 / 文本型 PDF）识别面过滤。

用户三份新样本（testdata/水印测试样本/）实测形态：水印文字直接在主内容流
文字层里（45° 旋转 + 半透明 + 大字号 + 高频重复），probe 不命中 ⇒ 独立流摘除
管不了 ⇒ 文本型主链 / 页级文本层分支 / docline 全部吃到水印文字。

判据：`_is_overlay_span` 同款特征（|dir.y|≥0.2 + alpha∈[1,220]）做**行级剔除**，
- 文本型主链 pages 零水印令牌、正文保留、计数>0；
- 零覆盖行卷文本逐字节不变（零扰动由构造保证）；
- 页级文本层分支 / docline 词框同判据剔除；
- 功能关闭时一切回到原行为。
"""

from __future__ import annotations

import os

import fitz
import pytest
from test_watermark_overlay_strip import BODY_INVISIBLE, WM_ID, build_pdf  # noqa: F401

from app.core.config import settings
from app.services import text_layer_docline_service as docline
from app.services.file_parser import FileParser


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    """parse/_validate_path 需要文件在 UPLOAD_DIR 内；outputs 隔离同理。"""
    up = tmp_path / "uploads"
    out = tmp_path / "outputs"
    up.mkdir()
    out.mkdir()
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(up))
    monkeypatch.setattr(settings, "OUTPUT_DIR", str(out))
    yield


@pytest.fixture()
def _wm_on(monkeypatch):
    monkeypatch.setattr(settings, "WATERMARK_OVERLAY_STRIP_ENABLED", True)


def _make(tmp_path, name: str, **kwargs) -> str:
    path = str(tmp_path / "uploads" / name)
    build_pdf(path, **kwargs)
    return path


def _make_dense(tmp_path, name: str, *, pages: int, inline_wm: bool) -> str:
    """密排不可见正文（过文本密度阈值）+ 可选 inline 水印——贴近真实文本型卷。"""
    import fitz
    from test_watermark_overlay_strip import _append_stream, _Gfx, _new_stream

    path = str(tmp_path / "uploads" / name)
    doc = fitz.open()
    try:
        gfx = _Gfx(doc)
        for index in range(pages):
            page = doc.new_page(width=595, height=842)
            for row in range(24):
                page.insert_text(
                    (60, 80 + row * 28),
                    f"{BODY_INVISIBLE} 第{index + 1}页第{row}行 讯问笔录内容记录样例文本",
                    fontsize=9,
                    render_mode=3,
                )
            pxref = page.xref
            if not _contents(doc, pxref):
                _append_stream(doc, pxref, _new_stream(doc, ""))
            if inline_wm:
                _append_stream(
                    doc, pxref, _new_stream(doc, gfx.watermark_ops(fitz.Rect(0, 0, 595, 842)))
                )
            from test_watermark_overlay_strip import _merge_resources

            _merge_resources(doc, pxref, gfx)
        doc.save(path, garbage=3, deflate=True)
    finally:
        doc.close()
    return path


def _contents(doc: fitz.Document, page_xref: int) -> list[int]:
    from test_watermark_overlay_strip import _contents as _c

    return _c(doc, page_xref)


def _parse_pages(path: str) -> tuple[list[str], int]:
    import asyncio

    parser = FileParser()
    from app.models.schemas import FileType

    result = asyncio.run(parser.parse(path, FileType.PDF))
    return result.pages, result.watermark_inline_filtered


def test_parse_filters_inline_watermark_keeps_body(tmp_path, _wm_on):
    """文本型主链：inline 水印行剔除、正文保留、计数>0。"""
    path = _make_dense(tmp_path, "inline.pdf", pages=3, inline_wm=True)
    pages, filtered = _parse_pages(path)
    assert filtered > 0, "应有剔除计数"
    assert len(pages) == 3
    for page_text in pages:
        assert WM_ID not in page_text.replace(" ", ""), f"页文本仍含水印令牌: {page_text[:80]}"
        assert BODY_INVISIBLE.split()[0] in page_text, "正文被误删"


def test_parse_text_byte_identical_when_no_overlay(tmp_path, _wm_on):
    """零覆盖行卷：文本与 get_text() 逐字节一致、计数=0（零扰动构造钉）。"""
    path = _make_dense(tmp_path, "clean.pdf", pages=2, inline_wm=False)
    doc = fitz.open(path)
    expected = [page.get_text() for page in doc]
    doc.close()
    pages, filtered = _parse_pages(path)
    assert filtered == 0
    assert pages == expected


def test_parse_disabled_switch_keeps_watermark(tmp_path, monkeypatch):
    """识别面总开关关闭 ⇒ 过滤不生效（回到原行为）。"""
    monkeypatch.setattr(settings, "WATERMARK_OVERLAY_STRIP_ENABLED", False)
    path = _make_dense(tmp_path, "inline-off.pdf", pages=3, inline_wm=True)
    pages, filtered = _parse_pages(path)
    assert filtered == 0
    assert any(WM_ID.replace(" ", "") in p.replace(" ", "") for p in pages), "关闭时应保留水印文字"


def test_page_text_blocks_skip_overlay_lines(tmp_path, _wm_on, monkeypatch):
    """页级文本层分支（get_pdf_page_text_blocks）：覆盖行不进 block。"""
    path = _make(tmp_path, "blocks.pdf", pages=2, body="textlayer", inline_wm=True)
    parser = FileParser()
    import asyncio

    result = asyncio.run(parser.get_pdf_page_text_blocks(path, 1))
    blocks = result[0] if isinstance(result, tuple) else result
    assert blocks, "应有正文 block"
    texts = " ".join(b.text for b in blocks)
    assert WM_ID.replace(" ", "") not in texts.replace(" ", ""), "block 仍含水印令牌"


def test_docline_word_boxes_drop_overlay_words(tmp_path, _wm_on):
    """docline 词框提取：覆盖词被剔（inline 卷兜底，防 docline 吃水印）。"""
    path = _make(tmp_path, "docline-inline.pdf", pages=2, body="textlayer", inline_wm=True)
    pages = docline.extract_text_layer_pages(path)
    assert pages
    for page in pages:
        joined = " ".join(w.text for w in page.words)
        assert WM_ID.replace(" ", "") not in joined.replace(" ", ""), f"词框含水印令牌: {joined[:80]}"
        assert any(BODY_INVISIBLE.split()[0] in w.text for w in page.words), "正文词被误删"


def test_real_samples_watermark_free(tmp_path, _wm_on):
    """用户三份真实样本：pages 零水印词、正文保留（CI 无 git-crypt 自动跳过）。"""
    samples = [
        "收条2024桂01民终11861号.pdf",
        "诉讼费退费申请表2024桂01民终11861号.pdf",
        "需求确认-律师问卷-脱敏喂云端AI-2026-10-06.pdf",
    ]
    base = os.environ.get("ISSUE250_SAMPLES_DIR")
    pytest.importorskip("fitz")
    if not base:
        here = os.path.dirname(__file__)
        base = None
        for up in range(1, 6):
            cand = os.path.join(here, *([".."] * up), "testdata", "水印测试样本")
            if os.path.isdir(cand):
                base = os.path.abspath(cand)
                break
        if not base:
            pytest.skip("testdata/水印测试样本 不可用（非本机环境）")
    tokens = {"绝密", "严禁复制", "李游"}
    found_any = False
    import shutil

    for name in samples:
        src = os.path.join(base, name)
        if not os.path.exists(src):
            continue
        found_any = True
        path = str(tmp_path / "uploads" / name)
        shutil.copyfile(src, path)
        pages, filtered = _parse_pages(path)
        assert filtered > 0, f"{name} 应有剔除计数"
        joined = "".join(pages)
        for tok in tokens:
            assert tok not in joined, f"{name} 仍含水印词「{tok}」"
    assert found_any, "样本目录不可用（CI 环境应 skip）"
