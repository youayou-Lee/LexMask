"""Issue#236：独立矢量覆盖水印流的检测与摘除（验收判据 AC1–AC13 的本地可判部分）。

设计文档：docs/issue-236-watermark-overlay-strip.md（§5 判据、§5.0 夹具清单）。

夹具全部**测试内合成**（公开仓不得含真实案卷）：正文用程序化栅格图或不可见
文字层，水印用 45° 旋转 + opacity 0.25 的文字流（与族 A 卷同形态：尺寸 29.22、
旋转、半透明、逐页重复）。真实卷/暖负样本在私有 testdata，由门④/⑤ 用。
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import re
import time

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

    def watermark_ops(self, rect: fitz.Rect, rows: int = 3, cols: int = 3) -> str:
        ops: list[str] = []
        for row in range(rows):
            for col in range(cols):
                x = 24 + col * (rect.width / cols)
                y = 60 + row * (rect.height / rows)
                ops.append(
                    "q 0.70711 -0.70711 0.70711 0.70711 "
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
                wm_ops = gfx.watermark_ops(rect)
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

            if body_residual_wm:
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


def test_partial_overlay_volume_reports_per_page_accounting(tmp_path, env):
    """F3：5 页卷 3 页有水印 ⇒ 摘 3 页、跳过 2 页，且无水印页像素不变。"""
    uploads, _outputs = env
    src = str(uploads / "partial.pdf")
    build_pdf(src, pages=5, overlay_pages=[0, 2, 4])
    gt = gt_for(src)
    copy_path, status = wm.render_source(src)
    assert status["code"] == "hit", status
    assert status["pages_stripped"] == 3
    assert status["pages_skipped"] == 2
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
    assert status["duration_ms"] >= 0
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
    build_pdf(other, pages=1)  # 触发一次构建 → 触发 sweep
    wm._CACHE.clear()  # noqa: SLF001
    wm.render_source(other)
    assert not os.path.exists(copy_path), "过期副本必须被清理"


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


def test_corrupt_pdf_returns_probe_error(tmp_path, env):
    path = tmp_path / "broken.pdf"
    path.write_bytes(b"%PDF-1.7\nnot a real pdf")
    outcome = wm.probe(str(path))
    assert outcome.code == "error"
    assert outcome.error


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
    assert copy_path, status
    assert status["pages_stripped"] == 3, status
    assert status["pages_skipped"] == 1, status
    doc = fitz.open(copy_path)
    try:
        text = doc.load_page(3).get_text()
    finally:
        doc.close()
    assert "SAMPLE BODY ONE" in text
    assert "SAMPLE BODY THREE" in text
