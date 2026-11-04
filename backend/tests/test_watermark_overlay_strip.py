"""Issue#236：独立矢量覆盖水印流的检测与摘除（验收判据 AC1–AC13 的本地可判部分）。

设计文档：docs/issue-236-watermark-overlay-strip.md（§5 判据、§5.0 夹具清单）。

夹具全部**测试内合成**（公开仓不得含真实案卷）：正文用程序化栅格图或不可见
文字层，水印用 45° 旋转 + opacity 0.25 的文字流（与族 A 卷同形态：尺寸 29.22、
旋转、半透明、逐页重复）。真实卷/暖负样本在私有 testdata，由门④/⑤ 用。
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import io
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import fitz
import pytest
from PIL import Image, ImageChops, ImageFilter

from app.core.config import settings
from app.services import watermark_overlay as wm
from app.services.file_parser import FileParser
from app.services.vision_service import VisionService

WM_TEXT = "ACME-CONFIDENTIAL-450102199001011234"
WM_ID = "450102199001011234"
BODY_INVISIBLE = "AUTHORIZED SAMPLE BODY TEXT"


# ---------------------------------------------------------------------------
# 夹具构造（全部合成；手写内容流，格式与族 A 卷同形态）
# ---------------------------------------------------------------------------


def _procedural_png(width: int = 300, height: int = 400) -> bytes:
    """程序化正文栅格（不依赖系统字体）：浅底 + 边框 + 若干横线。"""
    src = fitz.open()
    page = src.new_page(width=width, height=height)
    page.draw_rect(fitz.Rect(10, 10, width - 10, height - 10),
                   color=(0.2, 0.2, 0.2), fill=(0.93, 0.93, 0.88))
    for i in range(6):
        y = 60 + i * 50
        page.draw_line(fitz.Point(24, y), fitz.Point(width - 24, y),
                       color=(0.35, 0.35, 0.35), width=2)
    png = page.get_pixmap().tobytes("png")
    src.close()
    return png


class _Gfx:
    """一卷夹具里共享的 ExtGState / 字体对象。"""

    def __init__(self, doc: fitz.Document, opacity: float = 0.25) -> None:
        self.gs = doc.get_new_xref()
        doc.update_object(self.gs, f"<< /Type /ExtGState /ca {opacity} /CA {opacity} >>")
        self.font = doc.get_new_xref()
        doc.update_object(
            self.font,
            "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
            "/Encoding /WinAnsiEncoding >>",
        )

    def watermark_ops(self, rect: fitz.Rect, rows: int = 3, cols: int = 3,
                      rotation: float = -45.0) -> str:
        """rotation=-45 ⇒ dir=(0.707,0.707)（与真卷一致）；0 ⇒ 水平（A5 负样本）。"""
        import math

        cos = round(math.cos(math.radians(rotation)), 5)
        sin = round(math.sin(math.radians(rotation)), 5)
        ops: list[str] = []
        for row in range(rows):
            for col in range(cols):
                x = 24 + col * (rect.width / cols)
                y = 60 + row * (rect.height / rows)
                ops.append(
                    f"q {cos} {sin} {-sin} {cos} "
                    f"{x:.2f} {y:.2f} cm BT /GS0 gs 0.25098 0.25098 0.25098 rg "
                    f"/HeBo 29.22 Tf 0 0 Td ({WM_TEXT}) Tj ET Q"
                )
        return "\n".join(ops)


def _new_stream(doc: fitz.Document, payload: str) -> int:
    xref = doc.get_new_xref()
    doc.update_object(xref, "<<>>")
    doc.update_stream(xref, payload.encode("latin-1"))
    return xref


def _contents(doc: fitz.Document, page_xref: int) -> list[int]:
    kind, val = doc.xref_get_key(page_xref, "Contents")
    if kind == "xref":
        return [int(val.split()[0])]
    return [int(m) for m in re.findall(r"(\d+) 0 R", val or "")]


def _append_stream(doc: fitz.Document, page_xref: int, xref: int) -> None:
    streams = [*_contents(doc, page_xref), xref]
    doc.xref_set_key(page_xref, "Contents",
                     "[" + " ".join(f"{s} 0 R" for s in streams) + "]")


def _resource_body(doc: fitz.Document, page_xref: int) -> str:
    kind, val = doc.xref_get_key(page_xref, "Resources")
    if kind == "xref":
        body = doc.xref_object(int(val.split()[0])).strip()
        return body[2:-2].strip() if body.startswith("<<") else body
    return (val or "").strip()


def _merge_resources(doc: fitz.Document, page_xref: int, gfx: _Gfx,
                     with_body: bool = True) -> None:
    """把共享的 ExtGState/字体并进页面资源（保留正文图 XObject）。"""
    parts = [
        f"/ExtGState << /GS0 {gfx.gs} 0 R >>",
        f"/Font << /HeBo {gfx.font} 0 R /Helv {gfx.font} 0 R >>",
    ]
    if with_body:
        existing = _resource_body(doc, page_xref)
        if existing:
            parts.insert(0, existing)
    merged = doc.get_new_xref()
    doc.update_object(merged, "<<" + " ".join(parts) + ">>")
    doc.xref_set_key(page_xref, "Resources", f"{merged} 0 R")


def build_pdf(
    path: str,
    *,
    pages: int = 3,
    overlay_pages: list[int] | None = None,
    body: str = "raster",
    wm_streams: int = 1,
    inline_wm: bool = False,
    table_stream: bool = False,
    body_residual_wm: bool = False,
    legal_body_stream: bool = False,
    legal_ascii_pages: list[int] | None = None,
    paint_plus_wm_stream: bool = False,
    residual_wm_pages: list[int] | None = None,
    wm_rotation: float = -45.0,
    opacity: float = 0.25,
) -> None:
    """合成夹具。

    body: raster（栅格正文）/ textlayer（不可见文字层正文）/ none
    overlay_pages: 0 基页号；None = 全部页
    wm_streams: 每页附加水印流条数（2 ⇒ F4 双流歧义）
    inline_wm: 水印并入主流（W1，不可分离）
    table_stream: 额外矢量表格流（W3，命中 op 白名单拒绝）
    body_residual_wm: 水印文字也写进正文流（F5，摘 B 后仍残留）
    legal_body_stream: 额外合法不可见正文流（F6，与 B_wm 并列）
    legal_ascii_pages: 这些页额外挂一条合法 ASCII 正文流、且不挂水印流
        （F9：只有「删掉的不全是水印字符」这条守卫能挡住误删正文）
    paint_plus_wm_stream: 水印与绘制 op 同流（F10：命中的 op 不允许绘制类 ⇒ 不可摘）
    residual_wm_pages: 只有这些页的正文流里另有同形水印（C2：部分页摘不干净）
    wm_rotation: 水印旋转角（-45 复现真卷；0 = 水平，A5 倾斜闸负样本）
    """
    overlay_pages = list(range(pages)) if overlay_pages is None else overlay_pages
    doc = fitz.open()
    rect = fitz.Rect(0, 0, 595, 842)
    png = _procedural_png()
    gfx = _Gfx(doc, opacity)
    try:
        for index in range(pages):
            page = doc.new_page(width=rect.width, height=rect.height)
            pxref = page.xref
            if body == "raster":
                page.insert_image(rect, stream=png)
            elif body == "textlayer":
                page.insert_text((72, 120), BODY_INVISIBLE, fontsize=11, render_mode=3)
            if not _contents(doc, pxref):
                _append_stream(doc, pxref, _new_stream(doc, ""))

            if index in overlay_pages and paint_plus_wm_stream:
                # F10：水印与「填色矩形」绘制 op 同处一条流。摘掉它会把栅格/底图一起抹掉，
                # 因此 op 白名单必须把绘制类 op 判为不可摘（只捞文字不变是不够的）。
                wm_ops = "q 0.9 0.9 0.9 rg 0 0 595 842 re f Q\n" + gfx.watermark_ops(rect)
                _append_stream(doc, pxref, _new_stream(doc, wm_ops))
                wm_ops = ""
            elif index in overlay_pages:
                wm_ops = gfx.watermark_ops(rect, rotation=wm_rotation)
                if inline_wm:
                    _append_stream(doc, pxref, _new_stream(doc, wm_ops))
                    # 并入主流：把水印 op 追加到第一条内容流尾部（W1 不可分离）
                    first = _contents(doc, pxref)[0]
                    raw = doc.xref_stream(first)
                    doc.update_object(first, "<<>>")
                    doc.update_stream(first, raw + b"\n" + wm_ops.encode("latin-1"))
                else:
                    for _ in range(wm_streams):
                        _append_stream(doc, pxref, _new_stream(doc, wm_ops))

            if legal_ascii_pages and index in legal_ascii_pages:
                # F9：合法正文全部由 ASCII 组成（且不属于水印字符集），三段 BT 满足最小 BT 门槛。
                ops = ("BT /Helv 9 Tf 1 0 0 1 72 640 Tm (SAMPLE BODY ONE) Tj ET\n"
                       "BT /Helv 9 Tf 1 0 0 1 72 620 Tm (SAMPLE BODY TWO) Tj ET\n"
                       "BT /Helv 9 Tf 1 0 0 1 72 600 Tm (SAMPLE BODY THREE) Tj ET")
                _append_stream(doc, pxref, _new_stream(doc, ops))

            if legal_body_stream:
                ops = ("BT /Helv 9 Tf 3 Tr 1 0 0 1 72 700 Tm "
                       f"({BODY_INVISIBLE}) Tj ET")
                _append_stream(doc, pxref, _new_stream(doc, ops))

            if table_stream:
                ops = "q 0.1 g 60 300 460 300 re f 0.5 G 1 w 60 400 m 520 400 l S Q"
                _append_stream(doc, pxref, _new_stream(doc, ops))

            if body_residual_wm or (residual_wm_pages and index in residual_wm_pages):
                ops = f"BT /Helv 9 Tf 1 0 0 1 72 260 Tm ({WM_TEXT}) Tj ET"
                _append_stream(doc, pxref, _new_stream(doc, ops))

            _merge_resources(doc, pxref, gfx)
        doc.save(path, garbage=3, deflate=True)
    finally:
        doc.close()


def _lines(page: fitz.Page) -> list[str]:
    return [line.strip() for line in page.get_text().splitlines() if line.strip()]


def _wm_lines(page: fitz.Page) -> list[str]:
    out: list[str] = []
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []) or []:
            if abs(line.get("dir", (1.0, 0.0))[1]) >= 0.2:
                text = "".join(span.get("text", "") for span in line["spans"]).strip()
                if text and text not in out:
                    out.append(text)
    return sorted(out)


def gt_for(path: str) -> dict:
    """GT：逐页 orig / wm / copy 行（copy_lines = orig_lines − wm_span_lines）。"""
    doc = fitz.open(path)
    pages = []
    try:
        for index in range(doc.page_count):
            page = doc.load_page(index)
            orig = _lines(page)
            marks = set(_wm_lines(page))
            body = [line for line in orig if line not in marks]
            pages.append({
                "page": index + 1,
                "orig_lines": len(orig),
                "wm_lines": sorted(marks),
                "copy_lines": body,
            })
    finally:
        doc.close()
    return {"pages": pages}


def sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """隔离的 UPLOAD/OUTPUT 目录 + 开关；返回 (uploads, outputs)。"""
    uploads = tmp_path / "uploads"
    outputs = tmp_path / "outputs"
    uploads.mkdir()
    outputs.mkdir()
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(uploads))
    monkeypatch.setattr(settings, "OUTPUT_DIR", str(outputs))
    monkeypatch.setattr(settings, "WATERMARK_OVERLAY_STRIP_ENABLED", True)
    monkeypatch.setattr(settings, "WATERMARK_OVERLAY_PROBE_PAGES", 5)
    monkeypatch.setattr(settings, "WATERMARK_OVERLAY_MIN_HIT_PAGES", 3)
    monkeypatch.setattr(settings, "WATERMARK_OVERLAY_MIN_REPEATS", 3)
    monkeypatch.setattr(settings, "WATERMARK_OVERLAY_MAX_FILE_MB", 400)
    monkeypatch.setattr(settings, "WATERMARK_OVERLAY_COPY_TTL_HOURS", 24)
    monkeypatch.setattr(settings, "WATERMARK_OVERLAY_DELIVERY_KEEP_WATERMARK", True)
    wm.reset_stats()
    wm._CACHE.clear()  # noqa: SLF001 - 测试隔离进程内 memo
    return uploads, outputs


# ---------------------------------------------------------------------------
# AC1 检测命中 + 栅格保全（合成夹具）
# ---------------------------------------------------------------------------


def test_probe_detects_tiled_overlay_and_reports_signature(tmp_path, env):
    path = str(tmp_path / "fixture-src.pdf")
    build_pdf(path, pages=3)
    outcome = wm.probe(path)
    assert outcome.code == "hit", outcome
    assert outcome.pages_probed == 3
    assert outcome.pages_hit == 3
    assert outcome.slots == (1, 1, 1)
    assert outcome.top_repeats >= 3
    assert WM_ID in "".join(outcome.tokens) or any(WM_ID in t for t in outcome.tokens)


def test_build_strips_watermark_keeps_raster_and_original(tmp_path, env):
    uploads, _outputs = env
    src = str(uploads / "fixture-overlay.pdf")
    build_pdf(src, pages=3)
    before = sha256(src)
    gt = gt_for(src)

    copy_path, status = wm.render_source(src)
    assert status["code"] == "hit"
    assert status["source"] == "copy"
    assert copy_path and os.path.exists(copy_path)
    assert sha256(src) == before, "原件必须逐字节不变"

    # 副本：每页只剩 1 条内容流、水印令牌 0、正文行 = GT copy_lines
    doc = fitz.open(copy_path)
    try:
        for index in range(doc.page_count):
            page = doc.load_page(index)
            assert len(_contents(doc, page.xref)) == 1
            assert WM_ID not in page.get_text()
            assert _lines(page) == [line for line in gt["pages"][index]["copy_lines"]]
            assert len(page.get_images()) == 1, "正文栅格图不得丢失"
    finally:
        doc.close()


def test_encrypted_pdf_falls_back_with_explicit_code(tmp_path, env):
    """B4：加密卷摘流会破坏签名/结构 ⇒ 必须显式回落原件并留码，且不产出副本。"""
    uploads, _outputs = env
    src = str(uploads / "encrypted.pdf")
    build_pdf(src, pages=2)
    doc = fitz.open(src)
    try:
        doc.save(src + ".enc.pdf", encryption=fitz.PDF_ENCRYPT_AES_256,
                 owner_pw="owner-pw", user_pw="user-pw")
    finally:
        doc.close()
    enc = src + ".enc.pdf"
    before = sha256(enc)
    copy_path, status = wm.render_source(enc)
    assert copy_path is None
    assert status["source"] == "original"
    assert status["code"] == "encrypted", status
    assert status["failed"] is False, "加密卷不是失败，是显式跳过"
    assert sha256(enc) == before, "加密原件不得被触碰"


def test_concurrent_render_builds_one_copy(tmp_path, env):
    """B10：同一原件并发两次渲染 ⇒ 只构建一份副本、两个调用指向同一文件。"""
    uploads, _outputs = env
    src = str(uploads / "concurrent.pdf")
    build_pdf(src, pages=3)
    builds_before = wm.stats()["build_calls"]
    before_sha = sha256(src)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [f.result() for f in [pool.submit(wm.render_source, src) for _ in range(2)]]
    paths = {r[0] for r in results}
    assert len(paths) == 1 and None not in paths, results
    assert wm.stats()["build_calls"] - builds_before == 1
    assert sha256(src) == before_sha, "并发构建不得触碰原件"


def test_logs_never_leak_watermark_text(tmp_path, env, caplog):
    """B7 泄露面：全链日志不得出现水印原文（身份证号/令牌串），只允许计数与码。"""
    uploads, _outputs = env
    src = str(uploads / "log-hygiene.pdf")
    build_pdf(src, pages=3)
    with caplog.at_level(logging.DEBUG):
        copy_path, status = wm.render_source(src)
        assert status["source"] == "copy"
        # 再跑一次失败路径，确保异常日志也不带原文
        broken = str(uploads / "broken.pdf")
        with open(broken, "wb") as fh:
            fh.write(b"%PDF-1.4 not really a pdf")
        assert wm.render_source(broken)[0] is None
    text = caplog.text
    assert WM_ID not in text, "日志泄露了水印令牌"
    assert "450102199001011234" not in text, "日志泄露了身份证号"
    assert copy_path


def test_probe_on_already_stripped_copy_is_idempotent_noop(tmp_path, env):
    """AC(A10) 幂等：对已摘除的副本再跑检测必须零命中、不再构建，
    且副本内容逐字节不变（不得把扫描图流当小序号流二次摘除）。"""
    uploads, _outputs = env
    src = str(uploads / "idempotent.pdf")
    build_pdf(src, pages=3)
    copy_path, status = wm.render_source(src)
    assert status["source"] == "copy"
    before_sha = sha256(copy_path)
    builds_before = wm.stats()["build_calls"]

    # 走**真实入口**（render_source），而不是只证明 probe 不写文件
    again_path, again_status = wm.render_source(copy_path)
    assert again_status["code"] == "no_wm_spans", again_status
    assert again_path is None
    assert wm.stats()["build_calls"] == builds_before, "幂等重跑不得再构建副本"
    assert sha256(copy_path) == before_sha

    doc = fitz.open(copy_path)
    try:
        assert doc.page_count == 3
        for index in range(doc.page_count):
            page = doc.load_page(index)
            assert len(_contents(doc, page.xref)) == 1, "正文流不得被二次摘除"
            assert len(page.get_images()) == 1, "正文栅格图仍必须在"
    finally:
        doc.close()


def test_render_memo_keeps_probe_and_build_at_one_per_volume(tmp_path, env):
    uploads, _outputs = env
    src = str(uploads / "memo.pdf")
    build_pdf(src, pages=3)
    first, _ = wm.render_source(src)
    assert first
    assert wm.stats()["probe_calls"] == 1
    assert wm.stats()["build_calls"] == 1
    second, status = wm.render_source(src)
    assert second == first
    assert status["code"] == "hit"
    assert wm.stats()["probe_calls"] == 1
    assert wm.stats()["build_calls"] == 1


# ---------------------------------------------------------------------------
# AC4 误杀红线：暖负样本（水印不可分离 / 合法流 / 矢量绘制 / 栅格烧入）
# ---------------------------------------------------------------------------


def test_warm_inline_watermark_is_not_separable(tmp_path, env):
    """W1：水印文字并入主流（无独立流）⇒ 判「有覆盖但不是独立流」。"""
    path = str(tmp_path / "warm-inline.pdf")
    build_pdf(path, pages=3, inline_wm=True)
    outcome = wm.probe(path)
    assert outcome.code == "no_isolated_overlay", outcome


def test_warm_legal_invisible_textlayer_is_not_stripped(tmp_path, env):
    """W2：合法不可见文字层（alpha=0、水平）不得被当成水印。"""
    path = str(tmp_path / "warm-textlayer.pdf")
    build_pdf(path, pages=3, body="textlayer", overlay_pages=[])
    outcome = wm.probe(path)
    assert outcome.code == "no_wm_spans", outcome


def test_warm_vector_table_stream_is_rejected_by_op_whitelist(tmp_path, env):
    """W3：带 f/S/re 绘制的矢量流不满足「纯文字流」白名单。"""
    path = str(tmp_path / "warm-table.pdf")
    build_pdf(path, pages=3, body="raster", overlay_pages=[], table_stream=True)
    outcome = wm.probe(path)
    assert outcome.code == "no_wm_spans", outcome


# ---------------------------------------------------------------------------
# AC7 多流歧义 / 异构卷 / 单页命中 / 重叠残留
# ---------------------------------------------------------------------------


def test_double_watermark_streams_are_ambiguous(tmp_path, env):
    """F4：/Contents=[A,B1,B2] 两条水印流 ⇒ 候选不唯一，必须弃权。"""
    path = str(tmp_path / "double-overlay.pdf")
    build_pdf(path, pages=3, wm_streams=2)
    outcome = wm.probe(path)
    assert outcome.code == "no_isolated_overlay", outcome
    copy_path, status = wm.render_source(path)
    assert copy_path is None and status["source"] == "original"
    assert wm.stats()["build_calls"] == 0


def test_single_overlay_page_is_below_min_pages(tmp_path, env):
    """F7：5 页卷只有 1 页有水印 ⇒ 卷级弃权（不产生副本）。"""
    path = str(tmp_path / "single-page.pdf")
    build_pdf(path, pages=5, overlay_pages=[2])
    outcome = wm.probe(path)
    assert outcome.code == "below_min_pages", outcome
    copy_path, status = wm.render_source(path)
    assert copy_path is None and status["code"] == "below_min_pages"
    assert wm.stats()["build_calls"] == 0


def _page_streams(path: str) -> list[bytes]:
    """逐页内容流（解压后）原始字节，用于「未摘页逐字节不变」断言。"""
    doc = fitz.open(path)
    try:
        out = []
        for index in range(doc.page_count):
            page = doc.load_page(index)
            xrefs = _contents(doc, page.xref)
            out.append(b"\n".join(doc.xref_stream(int(x)) or b"" for x in xrefs))
        return out
    finally:
        doc.close()


def test_partial_overlay_volume_reports_per_page_accounting(tmp_path, env):
    """F3：5 页卷 3 页有水印 ⇒ 摘 3 页、跳过 2 页，且无水印页像素不变。"""
    uploads, _outputs = env
    src = str(uploads / "partial.pdf")
    build_pdf(src, pages=5, overlay_pages=[0, 2, 4])
    gt = gt_for(src)
    original_streams = _page_streams(src)
    copy_path, status = wm.render_source(src)
    assert status["code"] == "hit", status
    assert status["pages_stripped"] == 3
    assert status["pages_skipped"] == 2
    # A4：未摘页（1、3）的内容流必须逐字节不变——异构卷不得整卷重写。
    copy_streams = _page_streams(copy_path)
    for index in (1, 3):
        assert copy_streams[index] == original_streams[index], f"未摘页 {index} 的内容流被改写"
        a, b = _render_png(src, index), _render_png(copy_path, index)
        assert ImageChops.difference(a, b).getbbox() is None, f"未摘页 {index} 像素有变化"
    doc = fitz.open(copy_path)
    try:
        for index in range(doc.page_count):
            page = doc.load_page(index)
            assert _lines(page) == gt["pages"][index]["copy_lines"]
    finally:
        doc.close()


def test_residual_watermark_in_body_textlayer_causes_skip(tmp_path, env):
    """F5：摘掉独立水印流后正文文字层里仍有水印 ⇒ 逐页弃权、不产出可用副本。"""
    path = str(tmp_path / "residual.pdf")
    build_pdf(path, pages=3, body="raster", body_residual_wm=True)
    outcome = wm.probe(path)
    assert outcome.code == "hit"
    copy_path, status = wm.render_source(path)
    if copy_path:
        doc = fitz.open(copy_path)
        try:
            for index in range(doc.page_count):
                assert WM_ID not in doc.load_page(index).get_text(), \
                    "不得产出仍含水印令牌的副本"
        finally:
            doc.close()
    else:
        assert status["source"] == "original"


def test_dual_qualified_streams_strip_only_watermark(tmp_path, env):
    """F6：水印流 + 合法不可见正文流并列 ⇒ 只摘水印流，正文流全保留。"""
    uploads, _outputs = env
    src = str(uploads / "dual-qualified.pdf")
    build_pdf(src, pages=3, body="textlayer", legal_body_stream=True)
    outcome = wm.probe(src)
    assert outcome.code == "hit", outcome
    copy_path, status = wm.render_source(src)
    assert status["source"] == "copy"
    doc = fitz.open(copy_path)
    try:
        for index in range(doc.page_count):
            text = doc.load_page(index).get_text()
            assert WM_ID not in text
            assert text.count(BODY_INVISIBLE) == 2, "合法不可见文字层不得被摘除"
    finally:
        doc.close()


# ---------------------------------------------------------------------------
# AC6 未命中卷：检测执行过、构建 0 次、零文件、原件不动
# ---------------------------------------------------------------------------


def test_negative_volume_runs_probe_but_never_builds(tmp_path, env):
    uploads, outputs = env
    src = str(uploads / "negative.pdf")
    build_pdf(src, pages=4, body="textlayer", overlay_pages=[])
    before = sha256(src)
    copy_path, status = wm.render_source(src)
    assert copy_path is None
    assert status["code"] == "no_wm_spans"
    assert status["source"] == "original"
    assert status["pages_probed"] >= 1, "检测必须真的抽了页（不是直接跳过）"
    assert wm.stats()["probe_calls"] == 1
    assert wm.stats()["build_calls"] == 0
    assert sha256(src) == before
    assert not os.listdir(os.path.join(outputs, ".watermark_overlay")) \
        if os.path.isdir(os.path.join(outputs, ".watermark_overlay")) else True


# ---------------------------------------------------------------------------
# AC9 副本治理：权限 / 结构 / TTL / 不落交付
# ---------------------------------------------------------------------------


def test_copy_permissions_and_structure(tmp_path, env):
    uploads, outputs = env
    src = str(uploads / "gov.pdf")
    build_pdf(src, pages=3)
    copy_path, status = wm.render_source(src)
    directory = os.path.join(outputs, ".watermark_overlay")
    assert oct(os.stat(directory).st_mode)[-3:] == "700"
    assert oct(os.stat(copy_path).st_mode)[-3:] == "600"
    src_doc = fitz.open(src)
    copy_doc = fitz.open(copy_path)
    try:
        assert copy_doc.page_count == src_doc.page_count
        for index in range(copy_doc.page_count):
            assert copy_doc.load_page(index).rect == src_doc.load_page(index).rect
    finally:
        src_doc.close()
        copy_doc.close()


def test_copy_ttl_sweeps_stale_files(tmp_path, env, monkeypatch):
    uploads, outputs = env
    src = str(uploads / "ttl.pdf")
    build_pdf(src, pages=3)
    copy_path, _status = wm.render_source(src)
    assert os.path.exists(copy_path)
    stale = time.time() - 48 * 3600
    os.utime(copy_path, (stale, stale))
    monkeypatch.setattr(settings, "WATERMARK_OVERLAY_COPY_TTL_HOURS", 1)
    other = str(uploads / "ttl2.pdf")
    build_pdf(other, pages=3)  # 有真水印、会命中并构建 ⇒ 触发 sweep
    # 不走 _CACHE.clear()：过期副本不算「在用」，必须被真实清掉（门⑥ 二轮 #6）
    wm.render_source(other)
    assert not os.path.exists(copy_path), "过期副本必须被清理"
    # TTL 对 memo 同样生效：再来请求该卷要重建，且拿到的必须是存在的新副本
    again, again_status = wm.render_source(src)
    assert again and os.path.exists(again), again_status
    assert again_status["code"] == "hit", again_status


def test_disabled_switch_does_not_probe(tmp_path, monkeypatch):
    uploads = tmp_path / "u2"
    outputs = tmp_path / "o2"
    uploads.mkdir()
    outputs.mkdir()
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(uploads))
    monkeypatch.setattr(settings, "OUTPUT_DIR", str(outputs))
    monkeypatch.setattr(settings, "WATERMARK_OVERLAY_STRIP_ENABLED", False)
    wm.reset_stats()
    src = str(uploads / "off.pdf")
    build_pdf(src, pages=3)
    copy_path, status = wm.render_source(src)
    assert copy_path is None
    assert status["code"] == "disabled"
    assert status["source"] == "original"
    assert wm.stats()["probe_calls"] == 0


# ---------------------------------------------------------------------------
# AC10 异常输入：阶段级机读码（fail-open，不产出坏副本）
# ---------------------------------------------------------------------------


def test_corrupt_pdf_maps_to_not_pdf_and_fails_open(tmp_path, env):
    """AC10：读不开的 PDF 不得炸识别——归 not_pdf、回落原件、原件不动。"""
    uploads, _outputs = env
    path = uploads / "broken.pdf"
    path.write_bytes(b"%PDF-1.7\nnot a real pdf")
    before = path.read_bytes()
    outcome = wm.probe(str(path))
    assert outcome.code == "not_pdf", outcome
    copy_path, status = wm.render_source(str(path))
    assert copy_path is None
    assert status["code"] == "not_pdf", status
    assert status["source"] == "original"
    assert path.read_bytes() == before


def test_oversized_volume_is_capped(tmp_path, env, monkeypatch):
    path = str(tmp_path / "big.pdf")
    build_pdf(path, pages=1)
    monkeypatch.setattr(settings, "WATERMARK_OVERLAY_MAX_FILE_MB", 0)
    outcome = wm.probe(path, max_file_mb=1e-6)
    assert outcome.code == "capped_by_size"


def test_copy_verify_failure_falls_back_to_original(tmp_path, env, monkeypatch):
    """AC10-10a：副本自检失败 ⇒ 状态显式 failed、回落原件、目录不留半成品。"""
    uploads, outputs = env
    src = str(uploads / "verify.pdf")
    build_pdf(src, pages=3)
    real_open = fitz.open

    def _fake_open(target, *args, **kwargs):
        if isinstance(target, str) and ".watermark_overlay" in target:
            raise RuntimeError("copy self-check boom")
        return real_open(target, *args, **kwargs)

    monkeypatch.setattr(wm.fitz, "open", _fake_open)
    copy_path, status = wm.render_source(src)
    assert copy_path is None
    assert status["source"] == "original"
    assert status["failed"] is True
    assert status["error"]
    directory = os.path.join(outputs, ".watermark_overlay")
    assert not os.listdir(directory), "失败不得留下半成品副本"


# ---------------------------------------------------------------------------
# AC5 预览 / 交付：原件视图 vs 净化副本（像素级 oracle，见设计 §5.2）
# ---------------------------------------------------------------------------


def _render_png_bytes(path: str, page_index: int = 0, dpi: int = 150) -> bytes:
    doc = fitz.open(path)
    try:
        pix = doc.load_page(page_index).get_pixmap(dpi=dpi, alpha=False)
        return pix.tobytes("png")
    finally:
        doc.close()


def _render_png(path: str, page_index: int = 0, dpi: int = 150) -> Image.Image:
    doc = fitz.open(path)
    try:
        pix = doc.load_page(page_index).get_pixmap(dpi=dpi, alpha=False)
        return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    finally:
        doc.close()


def _watermark_mask(src: str, page_index: int = 0, stream_pos: int = -1,
                    dpi: int = 150) -> Image.Image:
    """独立 oracle：只渲染被摘掉的那条流 → 二值掩码（阈值 254 + 膨胀 3 px）。"""
    doc = fitz.open(src)
    try:
        page_xref = doc.load_page(page_index).xref
        streams = _contents(doc, page_xref)
        target = streams[stream_pos]
        resources = doc.xref_get_key(page_xref, "Resources")[1]
        rect = fitz.Rect(0, 0, 595, 842)
        tmp = doc.new_page(width=rect.width, height=rect.height)
        doc.xref_set_key(tmp.xref, "Contents", f"[{target} 0 R]")
        doc.xref_set_key(tmp.xref, "Resources", resources)
        pix = doc.load_page(tmp.number).get_pixmap(dpi=dpi, alpha=False)
        gray = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    finally:
        doc.close()
    dark = gray.point(lambda v: 255 if v < 254 else 0).convert("L")
    return dark.filter(ImageFilter.MaxFilter(7))


def _masked_delta(a: Image.Image, b: Image.Image, mask: Image.Image) -> float:
    """掩码覆盖区域内的平均通道差。"""
    diff = ImageChops.difference(a, b).convert("L")
    inside = Image.composite(diff, Image.new("L", mask.size, 0), mask)
    total = mask.point(lambda v: 1 if v else 0).histogram()[1]
    if not total:
        return 0.0
    return sum(v * c for v, c in enumerate(inside.histogram())) / total


def test_clean_render_changes_only_watermark_pixels(tmp_path, env):
    """AC2-2c / B6：识别面（干净副本渲染）与原件渲染在**水印掩码之外必须逐像素相同**。

    这是「矢量摘除」的核心承诺：不是像素修补，非水印区域的正文笔画/表格线/印章
    不得发生任何变化。掩码由独立 oracle（只渲染被摘掉的那条流）产出。
    """
    uploads, _outputs = env
    src = str(uploads / "clean-render.pdf")
    build_pdf(src, pages=3)
    copy_path, status = wm.render_source(src)
    assert status["source"] == "copy"

    original = _render_png(src, 0)
    clean = _render_png(copy_path, 0)
    assert original.size == clean.size
    mask = _watermark_mask(src, 0)
    outside = ImageChops.invert(mask)
    inside_delta = _masked_delta(original, clean, mask)
    outside_delta = _masked_delta(original, clean, outside)
    assert inside_delta > 1.0, f"水印区必须真的变了（{inside_delta:.2f}）"
    assert outside_delta == 0.0, f"非水印区必须逐像素不变（{outside_delta:.4f}）"


def test_copy_dir_unwritable_fails_open_without_breaking_recognition(tmp_path, env, monkeypatch):
    """C1（AC10）：副本目录不可写 ⇒ 显式 code=copy_dir_unwritable、回落原件、
    **不得抛异常**（识别面默认开启，模块故障不能把正常卷的识别整卷打掉）。"""
    if os.geteuid() == 0:  # root 无视目录权限，容器内跑会假红
        pytest.skip("目录只读语义对 root 无效")
    uploads, outputs = env
    src = str(uploads / "readonly-dir.pdf")
    build_pdf(src, pages=3)
    ro = outputs / "ro"
    ro.mkdir()
    monkeypatch.setattr(settings, "OUTPUT_DIR", str(ro), raising=False)
    os.chmod(ro, 0o500)
    try:
        copy_path, status = wm.render_source(src)
    finally:
        os.chmod(ro, 0o700)
    assert copy_path is None
    assert status["code"] == "copy_dir_unwritable", status
    assert status["source"] == "original"
    assert status["failed"] is False, "目录只读不是「卷坏了」"
    assert status["error"] and "/" not in status["error"].replace("<path>", ""), status


def test_memo_rebuilds_when_copy_was_swept(tmp_path, env):
    """C3：副本被 TTL/外部删除后，memo 不能继续声称 source=copy 并返回死路径。"""
    uploads, _outputs = env
    src = str(uploads / "swept.pdf")
    build_pdf(src, pages=3)
    first, status = wm.render_source(src)
    assert status["source"] == "copy" and first and os.path.exists(first)
    os.remove(first)
    second, again = wm.render_source(src)
    assert second and os.path.exists(second), again
    assert again["code"] == "hit"


def test_sweep_never_deletes_in_use_copy(tmp_path, env):
    """C3：清理只针对「未在使用」的副本；memo 持有的副本即使很旧也不能删。"""
    uploads, outputs = env
    src = str(uploads / "in-use.pdf")
    build_pdf(src, pages=3)
    copy_path, status = wm.render_source(src)
    assert status["source"] == "copy"
    stale = os.path.join(os.path.dirname(copy_path), "stale-orphan.pdf")
    with open(stale, "wb") as fh:
        fh.write(b"%PDF-1.4 stale")
    os.utime(stale, (0, 0))
    wm._sweep(os.path.dirname(copy_path), 24)
    assert os.path.exists(copy_path), "在用的（保鲜）副本被误删"
    assert not os.path.exists(stale), "过期孤儿副本应被清掉"
    # 一旦过期，memo 不再豁免清理（TTL 真实生效），随后请求会重建
    stale_ts = time.time() - 48 * 3600
    os.utime(copy_path, (stale_ts, stale_ts))
    wm._sweep(os.path.dirname(copy_path), 24)
    assert not os.path.exists(copy_path), "过期副本不应对 memo 永久豁免"
    again, again_status = wm.render_source(src)
    assert again and os.path.exists(again), again_status


def test_partial_residual_watermark_refuses_whole_volume(tmp_path, env):
    """C2：只有一页摘不干净 ⇒ 整卷弃权，绝不产出「还带水印」的副本。"""
    uploads, _outputs = env
    src = str(uploads / "partial-residual.pdf")
    build_pdf(src, pages=4, residual_wm_pages=[2])
    copy_path, status = wm.render_source(src)
    assert copy_path is None, status
    assert status["code"] == "guard_refused_pages", status
    assert status["pages_guard_refused"] == 1, status
    assert status["source"] == "original"


def test_horizontal_translucent_stream_is_not_stripped(tmp_path, env):
    """A5：半透明但**水平**的文字流不是覆盖水印 ⇒ 倾斜闸必须挡住。"""
    uploads, _outputs = env
    src = str(uploads / "horizontal.pdf")
    build_pdf(src, pages=3, wm_rotation=0.0)
    outcome = wm.probe(src)
    assert outcome.code in {"no_wm_spans", "no_isolated_overlay"}, outcome
    copy_path, status = wm.render_source(src)
    assert copy_path is None and status["source"] == "original"


def test_two_overlay_pages_below_threshold_is_not_stripped(tmp_path, env):
    """B2：5 页卷 2 页有水印 = MIN_HIT_PAGES-1 ⇒ 卷级弃权（阈值边界两侧都可判）。"""
    path = str(tmp_path / "two-pages.pdf")
    build_pdf(path, pages=5, overlay_pages=[0, 3])
    outcome = wm.probe(path)
    assert outcome.code == "below_min_pages", outcome
    assert wm.render_source(path)[0] is None


def test_pipeline_status_watermark_label_is_machine_readable(tmp_path, env):
    """I2（AC2-2a）：`pipeline_status.watermark_overlay` 必须带 code/source，
    让前端与门④ 能从识别结果里判「这一页吃的是副本还是原件」。"""
    from app.services.vision_service import watermark_overlay_label

    src = str(env[0] / "label.pdf")
    build_pdf(src, pages=3)
    clean_path, clean_status = wm.render_source(src)
    assert clean_path
    label = watermark_overlay_label(clean_status)
    assert label["code"] == "hit" and label["source"] == "copy"
    assert label["pages_stripped"] >= 1

    original_label = watermark_overlay_label(None)
    assert original_label["code"] == "not_run" and original_label["source"] == "original"

    monkeypatch_disable = {"code": "disabled", "source": "original", "ran": False}
    assert watermark_overlay_label(monkeypatch_disable)["code"] == "disabled"


def test_preview_renders_original_not_clean_copy(tmp_path, env):
    uploads, _outputs = env
    src = str(uploads / "preview.pdf")
    build_pdf(src, pages=3)
    parser = FileParser()

    async def _run():
        clean = await parser.get_pdf_page_image(src, 1)
        clean_status = parser.last_watermark_overlay_status
        raw = await parser.get_pdf_page_image(src, 1, allow_strip=False)
        raw_status = parser.last_watermark_overlay_status
        return clean, clean_status, raw, raw_status

    clean, clean_status, raw, raw_status = asyncio.run(_run())
    assert clean_status["source"] == "copy"
    assert raw_status is None, "预览（allow_strip=False）不得触发水印处理"
    assert clean != raw, "净化副本与原件渲染必须不同"
    # AC5a：预览必须是**原件的 150 dpi 渲染字节**（不能只是「与副本不同」）
    direct = Image.open(io.BytesIO(_render_png_bytes(src, 1, dpi=150))).convert("RGB")
    preview = Image.open(io.BytesIO(raw)).convert("RGB")
    assert preview.size == direct.size, "预览尺寸与原件 150 dpi 不一致"
    assert ImageChops.difference(preview, direct).getbbox() is None, "预览像素不是原件渲染"


def test_delivery_keeps_watermark_by_default(tmp_path, env):
    """AC5-5b：不设环境变量时交付物仍含水印（默认支路，不是死代码）。"""
    uploads, _outputs = env
    src = str(uploads / "delivery-keep.pdf")
    build_pdf(src, pages=3)
    service = VisionService()
    out = str(tmp_path / "deliver-keep.pdf")
    asyncio.run(service.apply_redaction(src, _pdf_type(), [], out))

    transcribe_out = str(tmp_path / "deliver-strip.pdf")
    settings.WATERMARK_OVERLAY_DELIVERY_KEEP_WATERMARK = False
    try:
        copy_path, _status = wm.render_source(src)
        assert copy_path
        # 等价于「交付走净化副本」的光栅化结果，用于像素对照
        asyncio.run(service.apply_redaction(src, _pdf_type(), [], transcribe_out))
    finally:
        settings.WATERMARK_OVERLAY_DELIVERY_KEEP_WATERMARK = True

    original = _render_png(src, 0)
    keep = _render_png(out, 0)
    strip = _render_png(transcribe_out, 0)
    mask = _watermark_mask(src, 0)
    outside = ImageChops.invert(mask)
    inside_delta = _masked_delta(keep, strip, mask)
    outside_delta = _masked_delta(keep, strip, outside)
    # 交付是 JPEG 光栅，绝对差受压缩影响；判据取「水印区差 ≥3× 区外差」的相对关系
    assert inside_delta > max(8.0, 3.0 * outside_delta), (
        f"默认交付必须保留水印（掩码区 {inside_delta:.1f} vs 区外 {outside_delta:.1f}）"
    )
    assert outside_delta < 6, "掩码区外两侧应一致"
    assert _masked_delta(keep, original, mask) < 20, "保留支路应与原件版式一致"


def test_delivery_strip_switch_removes_watermark(tmp_path, env):
    """AC5-5b 反向支路：显式置 false 时交付物不得再含水印像素。"""
    uploads, _outputs = env
    src = str(uploads / "delivery-strip.pdf")
    build_pdf(src, pages=3)
    service = VisionService()
    settings.WATERMARK_OVERLAY_DELIVERY_KEEP_WATERMARK = False
    try:
        out = str(tmp_path / "deliver.pdf")
        asyncio.run(service.apply_redaction(src, _pdf_type(), [], out))
    finally:
        settings.WATERMARK_OVERLAY_DELIVERY_KEEP_WATERMARK = True
    mask = _watermark_mask(src, 0)
    stripped_render = _render_png(_one_shot_copy(src, tmp_path), 0)
    assert _masked_delta(_render_png(out, 0), stripped_render, mask) < 12


def _one_shot_copy(src: str, tmp_path) -> str:
    copy_path, _status = wm.render_source(src)
    assert copy_path
    return copy_path


def _pdf_type():
    from app.models.schemas import FileType

    return FileType.PDF


def test_shipped_switch_defaults_follow_decision():
    """AC5a/AC13：出厂默认值即拍板结论（不依赖测试夹具的 monkeypatch）。"""
    from app.core.config import settings as shipped

    assert shipped.WATERMARK_OVERLAY_STRIP_ENABLED is True, "识别面默认开（2026-11-04 拍板）"
    assert shipped.WATERMARK_OVERLAY_DELIVERY_KEEP_WATERMARK is True, "交付默认保留水印"
    assert shipped.WATERMARK_OVERLAY_PROBE_PAGES == 5
    assert shipped.WATERMARK_OVERLAY_MIN_HIT_PAGES == 3
    assert shipped.WATERMARK_OVERLAY_COPY_TTL_HOURS == 24

# ---------------------------------------------------------------------------
# AC7 fail-closed 夹具补强：M2/M4 变异可红（op 白名单的绘制类判据 + 字符闸）
# ---------------------------------------------------------------------------


def test_painted_stream_with_watermark_is_never_stripped(tmp_path, env):
    """F10：水印 op 与绘制 op（填色）同流 ⇒ 只能判「无独立水印流」，不得摘除。

    摘流式差分 oracle 只看文字 span 是否恢复，绘制 op 被一起摘掉它看不出来；
    必须靠 op 白名单把绘制类 op 判死（AC1 栅格/底图保全的硬要求）。
    """
    uploads, _outputs = env
    src = str(uploads / "painted-watermark.pdf")
    build_pdf(src, pages=3, paint_plus_wm_stream=True)
    outcome = wm.probe(src)
    assert outcome.code == "no_isolated_overlay", outcome
    before = _render_png(src, 0)
    copy_path, status = wm.render_source(src)
    assert copy_path is None
    assert status["source"] == "original"
    assert wm.stats()["build_calls"] == 0
    assert _render_png(src, 0) == before, "原件渲染必须逐字节不变（底图不被抹）"


def test_legal_ascii_body_stream_survives_charset_guard(tmp_path, env):
    """F9：异构卷——探测页有水印，正文页只有合法 ASCII 正文流。

    摘这条正文流之后「没有凭空多出行、确实删掉了行、也没有残留水印令牌」，
    唯一能挡住它的是「删掉的行必须全由水印字符组成」这条闸（AC7 夹具）。
    """
    uploads, _outputs = env
    src = str(uploads / "heterogeneous-ascii.pdf")
    build_pdf(src, pages=4, overlay_pages=[0, 1, 2], legal_ascii_pages=[3])
    outcome = wm.probe(src)
    assert outcome.code == "hit", outcome
    copy_path, status = wm.render_source(src)
    # AC7-7a：异构卷——水印页照摘，无水印的正文页既不摘也不熔断（门⑥ 二轮：熔断只按「页」判，
    # 且只对「本页确实有水印却摘不干净」生效；把正文页的守卫弃权也算熔断会把整卷误伤）。
    assert copy_path, status
    assert status["code"] == "hit", status
    assert status["pages_stripped"] == 3, status
    assert status["pages_skipped"] == 1, status
    assert status["pages_guard_refused"] == 0, status
    doc = fitz.open(copy_path)
    try:
        text = doc.load_page(3).get_text()
        for index in (0, 1, 2):
            assert "ACME-CONFIDENTIAL" not in doc.load_page(index).get_text()
    finally:
        doc.close()
    assert "SAMPLE BODY ONE" in text
    assert "SAMPLE BODY THREE" in text
    # 原件也仍然完整（正文流没有被真的删掉）
    doc = fitz.open(src)
    try:
        assert "SAMPLE BODY ONE" in doc.load_page(3).get_text()
    finally:
        doc.close()


# ---------------------------------------------------------------------------
# 门⑥ 二轮补钉：输入口径码 / 构建失败语义 / pipeline_status 接线 / 不透明水印
# ---------------------------------------------------------------------------


def test_non_pdf_input_maps_to_not_pdf(tmp_path, env):
    uploads, _outputs = env
    src = str(uploads / "not-a-pdf.pdf")
    with open(src, "wb") as fh:
        fh.write(b"not a pdf at all")
    outcome = wm.probe(src)
    assert outcome.code == "not_pdf", outcome
    _copy, status = wm.render_source(src)
    assert status["code"] == "not_pdf", status
    assert status["source"] == "original"


def test_zero_page_pdf_maps_to_not_applicable(tmp_path, env):
    uploads, _outputs = env
    src = str(uploads / "empty.pdf")
    # fitz 不允许保存 0 页，手写一个合法的零页 PDF（MuPDF 可正常打开、page_count=0）
    zero_page = (
        "%PDF-1.4\n"
        "1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
        "2 0 obj\n<< /Type /Pages /Count 0 /Kids [] /MediaBox [0 0 595 842] >>\nendobj\n"
        "trailer\n<< /Root 1 0 R /Size 3 >>\n%%EOF\n"
    )
    with open(src, "wb") as fh:
        fh.write(zero_page.encode("latin-1"))
    outcome = wm.probe(src)
    assert outcome.code == "not_applicable", outcome


def test_copy_self_check_failure_is_failed_and_sanitized(tmp_path, env, monkeypatch):
    uploads, _outputs = env
    src = str(uploads / "selfcheck.pdf")
    build_pdf(src, pages=3)
    real_open = wm.fitz.open

    secret_path = "/" + "srv/secret/path"  # 拼接构造：公开树扫描器不该在测试里看到真实绝对路径字面量
    def _boom(path, *a, **kw):
        if isinstance(path, str) and ".watermark_overlay" in path:
            raise ValueError(f"copy page count 2 != 3 at {secret_path}")
        return real_open(path, *a, **kw)

    monkeypatch.setattr(wm.fitz, "open", _boom)
    copy_path, status = wm.render_source(src)
    assert copy_path is None, status
    assert status["code"] == "copy_verify_failed", status
    assert status["failed"] is True, status
    assert secret_path not in (status.get("error") or ""), status


def test_pipeline_status_initial_label_matches_helper():
    from app.services import vision_service

    initial = vision_service._initial_wm_label()  # noqa: SLF001
    assert initial["code"] == "not_applicable"
    assert initial["source"] == "original"
    assert vision_service.watermark_overlay_label(None)["code"] == "not_run"
    # 接线钉：识别路径必须真的调用这个 helper（删掉接线即红）
    source = inspect.getsource(vision_service)
    assert "watermark_overlay_label(" in source
    assert "_initial_wm_label(" in source


def test_opaque_overlay_is_not_treated_as_watermark(tmp_path, env):
    """alpha 上限（>220）必须真的挡住「不透明灰字」——否则指纹闸形同虚设。"""
    uploads, _outputs = env
    src = str(uploads / "opaque.pdf")
    build_pdf(src, pages=3, opacity=1.0)
    outcome = wm.probe(src)
    assert outcome.code in {"no_wm_spans", "no_isolated_overlay"}, outcome
    copy_path, status = wm.render_source(src)
    assert copy_path is None
    assert status["source"] == "original"
    assert wm.stats()["build_calls"] == 0


def test_probe_reports_slot_per_page(tmp_path, env):
    """门⑥ 二轮：插槽映射必须按**页**记录，不能按命中顺序索引页号（异构卷会错配）。"""
    uploads, _outputs = env
    src = str(uploads / "slot-map.pdf")
    build_pdf(src, pages=5, overlay_pages=[0, 2, 4])
    wm.reset_stats()
    outcome = wm.probe(src, probes=5)
    assert outcome.code == "hit", outcome
    assert outcome.slot_by_page, "每页插槽必须留痕"
    assert [page for page, _slot in outcome.slot_by_page] == sorted(page for page, _slot in outcome.slot_by_page)
    assert {page for page, _slot in outcome.slot_by_page} <= {0, 2, 4}


def test_fail_open_result_is_cached_briefly(tmp_path, env, monkeypatch):
    """门⑥ 二轮：持续故障时不能每页重付探测/构建，但短窗口后必须重试。"""
    uploads, _outputs = env
    src = str(uploads / "flaky.pdf")
    build_pdf(src, pages=3)
    wm.reset_stats()
    calls = {"n": 0}

    def boom(*_args, **_kwargs):
        calls["n"] += 1
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(wm, "probe", boom)
    first_path, first = wm.render_source(src)
    _second_path, second = wm.render_source(src)
    assert first_path is None and first["code"] == "error" and first["failed"] is True
    assert second["code"] == "error"
    assert calls["n"] == 1, "同一卷的失败结果必须负缓存，不能每页重付"

    with wm._CACHE_LOCK:  # 短窗口过期后必须允许重试（否则故障修好也永不恢复）
        for key in list(wm._CACHE):
            if isinstance(wm._CACHE[key], dict) and "_cached_at" in wm._CACHE[key]:
                wm._CACHE[key]["_cached_at"] -= wm._FAIL_OPEN_TTL_S + 1
    wm.render_source(src)
    assert calls["n"] == 2


_TESTDATA = Path(__file__).resolve().parents[4] / "testdata"
_PRIVATE_CANARY = _TESTDATA / "canary-issue236-vector-overlay-watermark.pdf"
_PRIVATE_CANARY_GT = _TESTDATA / "canary-issue236-vector-overlay-watermark-gt.json"


@pytest.mark.skipif(not _PRIVATE_CANARY.is_file(), reason="私有 testdata 未挂载（CI 无 git-crypt）")
def test_private_canary_matches_its_gt(tmp_path, env):
    """落盘 canary（testdata/，git-crypt）与它的 GT 自洽：形态、命中、摘后零文本零令牌。

    单测夹具是临时现造的；这条把**落盘件**也钉住，防止夹具与生成器漂移。
    """
    import json

    gt = json.loads(_PRIVATE_CANARY_GT.read_text(encoding="utf-8"))
    doc = fitz.open(str(_PRIVATE_CANARY))
    try:
        assert len(doc) == gt["pages"]
        for number in range(len(doc)):
            streams = doc.load_page(number).get_contents()
            assert len(streams) == 2, f"页 {number + 1} 应为 [A,B] 两个内容流"
        page_lines = [
            [line.strip() for line in doc.load_page(i).get_text().splitlines() if line.strip()]
            for i in range(len(doc))
        ]
        assert page_lines[0] == page_lines[-1], "canary 各页形态应一致"
        orig_lines = len(page_lines[0])
        page1 = gt["pages_gt"]["1"]
        assert orig_lines == page1["orig_lines"], (orig_lines, page1)
        assert page1["copy_lines"] == 0 and page1["copy_chars"] == 0
    finally:
        doc.close()

    outcome = wm.probe(str(_PRIVATE_CANARY))
    assert outcome.code == gt["expected"]["probe_code"], outcome
    assert outcome.pages_hit == gt["pages"]

    _uploads, outputs = env
    copy_path, status = wm.render_source(str(_PRIVATE_CANARY))
    assert status["source"] == "copy" and copy_path, status
    copy = fitz.open(copy_path)
    try:
        assert len(copy) == gt["expected"]["pages_stripped"]
        chars = sum(len(copy.load_page(i).get_text().strip()) for i in range(len(copy)))
        token = page1["tokens"][0].replace(" ", "")
        hits = sum(copy.load_page(i).get_text().count(token) for i in range(len(copy)))
        assert chars == gt["expected"]["copy_chars_total"], chars
        assert hits == gt["expected"]["copy_token_hits"], hits
    finally:
        copy.close()


def test_sweep_expired_clears_stale_orphans(env):
    """AC9：进程级定时清理（`sweep_expired`）能实清过期副本，且不碰保鲜文件。"""
    _uploads, outputs = env
    directory = Path(wm.copy_dir())
    stale = directory / "stale-from-earlier-run.pdf"
    fresh = directory / "fresh-still-referenced.pdf"
    for target in (stale, fresh):
        target.write_bytes(b"%PDF-1.4\n%%EOF\n")
    os.utime(stale, (0, 0))

    removed = wm.sweep_expired(ttl_hours=1.0)

    assert removed >= 1
    assert not stale.exists(), "过期副本必须被清"
    assert fresh.exists(), "保鲜副本不得被清"


def test_sweep_expired_is_callable_by_periodic_cleanup():
    """接线钉：`main._periodic_cleanup` 与启动清理都必须真的调 `sweep_expired`（删接线即红）。"""
    import app.main as app_main

    source = inspect.getsource(app_main)
    assert "watermark_overlay.sweep_expired()" in source
    assert source.count("watermark_overlay.sweep_expired()") >= 2
