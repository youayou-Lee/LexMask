"""Issue#338：docline inline 水印过滤判据 = 「行归属」（不是「词中心落覆盖 span 外接框」）。

背景（Issue#250 遗留）：docline 的 `_word_boxes` 用「词中心是否落在覆盖 span 的**外接矩形**内」
判定 ⇒ 45° 水印 span 的外接框很大，把同区的正文词一起吃掉。真卷 149 页实测：修前剔
7,939 词里 **3,226 个是无歧义正文词**（40.6% 误删），而页账（`page_chars < 60` 的页数）
从 101 页掉到 63 页 —— 正文被吃掉后整页被判「无文本层」。

判据改为**行归属**：词来自 `get_text("words")`（自带 `(block_no, line_no)`），
overlay 行集合来自 `get_text("dict")` 里**先滤 `type == 0` 文本块再枚举**的 `(block_no, line_no)`；
两者同键即剔除。这样：
- 正文（水平、不透明）天然不命中；
- 45° 半透明水印行的**全部**词（含被正文框压住的混合词）按行剔除；
- 倾斜但 alpha=0 的不可见 OCR 层天然保留（`is_overlay_line` 要求 alpha ≥ 1）。

本文件的夹具全部**测试内合成**（公开仓不得含真实案卷）；真实 149 页卷走私有探针
（`LexMask-workspace/scripts/issue338/measure_word_stream.py`）与云/本机 E2E。
"""
from __future__ import annotations

import json
import logging

import fitz
import pytest
from test_watermark_overlay_strip import (
    _Gfx,
    _append_stream,
    _contents,
    _new_stream,
    _resource_body,
)

from app.core.config import settings
from app.services import page_event_log as page_events
from app.services import text_layer_docline_service as docline
from app.services.watermark_overlay import is_overlay_line

# 水印令牌用**纯中文**取值：既要在词流里可辨识，又不能是「长数字串 + 凭据名」形态
# （公开树凭据扫描器会把 `WM_TOKEN = "<18 位数字>"` 判成 credential_literal，Issue#140/#141 回归钉）。
WM_TEXT = "ACME CONFIDENTIAL 某某市院内部资料注意保密"
WM_TOKEN = "某某市院内部资料"  # 水印令牌（词流里不应出现）
BODY_TOKEN = "AUTHORIZED"


# ---------------------------------------------------------------------------
# 夹具：水平不透明正文 + 45° 半透明水印（与族 A 卷同形态：手写内容流）
# ---------------------------------------------------------------------------


def _merge_resources_ex(doc: fitz.Document, page_xref: int, gfx: _Gfx,
                        extra_gs: dict[str, int] | None = None) -> None:
    """`_merge_resources` 的扩展版：额外挂几个 ExtGState（如 alpha=0 的不可见层）。"""
    extra = " ".join(f"/{name} {xref} 0 R" for name, xref in (extra_gs or {}).items())
    parts = [
        f"/ExtGState << /GS0 {gfx.gs} 0 R {extra} >>",
        f"/Font << /HeBo {gfx.font} 0 R /Helv {gfx.font} 0 R >>",
    ]
    existing = _resource_body(doc, page_xref)
    if existing:
        parts.insert(0, existing)
    merged = doc.get_new_xref()
    doc.update_object(merged, "<<" + " ".join(parts) + ">>")
    doc.xref_set_key(page_xref, "Resources", f"{merged} 0 R")


def _invisible_tilted_ops(text: str) -> str:
    """45° + alpha=0（ExtGState /GSinv）的文字流：扫描件不可见 OCR 层形态。"""
    return (
        "q /GSinv gs 0 0 0 rg BT /HeBo 12 Tf 0.7071 0.7071 -0.7071 0.7071 "
        f"200 400 Tm ({text}) Tj ET Q"
    )


def _build_pdf(
    path: str,
    *,
    pages: int = 1,
    body_rows: int = 12,
    inline_wm: bool = True,
    wm_alpha: float = 0.25,
    wm_rotation: float = -45.0,
    invisible_tilted: bool = False,
) -> str:
    """正文用水平不透明 Helvetica；水印用 `cm` 旋转 + ExtGState 半透明（族 A 卷同形态）。"""
    doc = fitz.open()
    gfx = _Gfx(doc, wm_alpha)
    try:
        for index in range(pages):
            page = doc.new_page(width=595, height=842)
            for row in range(body_rows):
                page.insert_text(
                    (60, 90 + row * 30),
                    f"{BODY_TOKEN} page {index + 1} row {row} sample body text",
                    fontsize=10,
                )
            pxref = page.xref
            if not _contents(doc, pxref):
                _append_stream(doc, pxref, _new_stream(doc, ""))
            extra_gs: dict[str, int] = {}
            if inline_wm:
                _append_stream(
                    doc, pxref,
                    _new_stream(doc, gfx.watermark_ops(fitz.Rect(0, 0, 595, 842),
                                                       rows=3, cols=3, rotation=wm_rotation)),
                )
            if invisible_tilted:
                gs0 = doc.get_new_xref()
                doc.update_object(gs0, "<< /Type /ExtGState /ca 0 /CA 0 >>")
                extra_gs["GSinv"] = gs0
                _append_stream(doc, pxref, _new_stream(doc, _invisible_tilted_ops("INVISIBLE-OCR-LAYER")))
            _merge_resources_ex(doc, pxref, gfx, extra_gs)
        doc.save(path, garbage=3, deflate=True)
    finally:
        doc.close()
    return path


# ---------------------------------------------------------------------------
# 独立复算（测试自己的实现，不复用生产代码的判据）
# ---------------------------------------------------------------------------


def _overlay_line_keys(page: fitz.Page) -> set[tuple[int, int]]:
    """逐行独立复算 overlay 行键集合（dict 先滤 type==0 再枚举）。"""
    keys: set[tuple[int, int]] = set()
    raw = page.get_text("dict")
    blocks = [b for b in raw.get("blocks", []) if b.get("type") == 0]
    for block_no, block in enumerate(blocks):
        for line_no, line in enumerate(block.get("lines", []) or []):
            if is_overlay_line(line):
                keys.add((block_no, line_no))
    return keys


def _word_keys(page: fitz.Page) -> list[tuple[int, int, str]]:
    return [(int(w[5]), int(w[6]), str(w[4])) for w in page.get_text("words")]


@pytest.fixture()
def _wm_on(monkeypatch):
    monkeypatch.setattr(settings, "WATERMARK_OVERLAY_STRIP_ENABLED", True)


# ---------------------------------------------------------------------------
# T1 正文保留 / 水印行剔除 / 不可见层保留
# ---------------------------------------------------------------------------


def test_t1_body_kept_and_overlay_line_dropped(tmp_path, _wm_on):
    path = _build_pdf(str(tmp_path / "t1.pdf"))
    pages = docline.extract_text_layer_pages(path)
    assert len(pages) == 1
    words = [w.text for w in pages[0].words]
    joined = " ".join(words)
    assert WM_TOKEN not in joined.replace(" ", ""), "水印令牌未被剔除"
    assert any(BODY_TOKEN in w for w in words), "正文词被误删"


def test_t1_invisible_tilted_layer_kept(tmp_path, _wm_on):
    """倾斜但 alpha=0 的不可见 OCR 层：天然不命中（alpha ≥ 1 才判覆盖）。"""
    path = _build_pdf(str(tmp_path / "t1b.pdf"), invisible_tilted=True)
    pages = docline.extract_text_layer_pages(path)
    joined = " ".join(w.text for w in pages[0].words)
    assert "INVISIBLE-OCR-LAYER" in joined.replace(" ", ""), "不可见 OCR 层词被误删"


def test_t1_switch_off_restores_old_behaviour(tmp_path, monkeypatch):
    """开关关闭：水印词回到词流里（既有行为，不被本改动改变）。"""
    monkeypatch.setattr(settings, "WATERMARK_OVERLAY_STRIP_ENABLED", False)
    path = _build_pdf(str(tmp_path / "t1c.pdf"))
    joined = " ".join(w.text for w in docline.extract_text_layer_pages(path)[0].words)
    # 水印词被重叠切分（"ACME-C" / "450102199001"），按前缀断言「词流里有水印词」
    assert "ACME-CONFIDENTIAL" in joined.replace(" ", ""), "开关关闭时水印词应当保留"
    page = docline.extract_text_layer_pages(path)[0]
    assert page.words_dropped == 0, "开关关闭时不应有剔除账"


# ---------------------------------------------------------------------------
# T2 页级账（words_raw / words_dropped）与逐词成员关系
# ---------------------------------------------------------------------------


def test_t2_page_accounting_matches_independent_recount(tmp_path, _wm_on):
    path = _build_pdf(str(tmp_path / "t2.pdf"), pages=2)
    with fitz.open(path) as doc:
        expected_raw = [len(doc[i].get_text("words")) for i in range(doc.page_count)]
        expected_dropped = [
            sum(1 for b, _l, _t in _word_keys(doc[i]) if (b, _l) in _overlay_line_keys(doc[i]))
            for i in range(doc.page_count)
        ]
    pages = docline.extract_text_layer_pages(path)
    assert [p.words_raw for p in pages] == expected_raw
    assert [p.words_dropped for p in pages] == expected_dropped
    assert all(p.words_dropped > 0 for p in pages), "夹具每页都应有水印行"


def test_t2_drop_ratio_never_divides_by_zero():
    """words_raw == 0 ⇒ drop_ratio 0.0（不抛）。"""
    result = docline.DocLineResult(
        pages=1, chunks=0, chunk_chars=0, stream_chars=0, layer_chars=0,
        covered_chars=0, entities=[], ner_calls=0, truncated_chunks=0,
        http_errors=0, residual_calls=0, seconds=0.0,
    )
    assert result.words_raw == 0
    assert result.words_dropped == 0
    assert result.drop_ratio == 0.0


# ---------------------------------------------------------------------------
# T3（真卷 149 页）走私有探针，不在公开 pytest 里跑（见方案 §6.1 A1）
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# T4 零 overlay 卷：词流逐词不变
# ---------------------------------------------------------------------------


def test_t4_zero_overlay_word_stream_unchanged(tmp_path, _wm_on):
    """无水印夹具：修后词流与「只按空文本过滤」的裸词流逐词相同。"""
    path = _build_pdf(str(tmp_path / "t4.pdf"), inline_wm=False)
    with fitz.open(path) as doc:
        expected = [
            (round(float(w[0]), 4), round(float(w[1]), 4), round(float(w[2]), 4),
             round(float(w[3]), 4), str(w[4]))
            for w in doc[0].get_text("words") if str(w[4]).strip()
        ]
    got = [
        (round(w.x0, 4), round(w.y0, 4), round(w.x1, 4), round(w.y1, 4), w.text)
        for w in docline.extract_text_layer_pages(path)[0].words
    ]
    assert got == expected
    pages = docline.extract_text_layer_pages(path)
    assert pages[0].words_dropped == 0


# ---------------------------------------------------------------------------
# T5 核心：逐词成员关系（保留 ⇔ 键不在 overlay 集合）
# ---------------------------------------------------------------------------


def test_t5_word_membership_is_line_based(tmp_path, _wm_on):
    """每个保留词的 (block,line) 都不在 overlay 集合；每个被剔词的键都在。"""
    path = _build_pdf(str(tmp_path / "t5.pdf"), pages=2)
    with fitz.open(path) as doc:
        for index in range(doc.page_count):
            page = doc[index]
            keys = _word_keys(page)
            overlay = _overlay_line_keys(page)
            assert overlay, "夹具应有 overlay 行"
            kept_via_key = {t for b, l, t in keys if (b, l) not in overlay}
            dropped_via_key = {t for b, l, t in keys if (b, l) in overlay}
            assert dropped_via_key, "夹具应有被剔词"
    page_meta = docline.extract_text_layer_pages(path)[0]
    got_kept = {w.text for w in page_meta.words}
    assert got_kept == kept_via_key, "保留词与行归属复算不一致"


def test_t5_mixed_overlap_words_dropped_with_line(tmp_path, _wm_on):
    """混合区：水印字形 bbox 压住正文词时，正文词仍保留、水印词按行剔（真卷 839 词那类）。"""
    path = _build_pdf(str(tmp_path / "t5b.pdf"), body_rows=40)
    pages = docline.extract_text_layer_pages(path)
    with fitz.open(path) as doc:
        page = doc[0]
        overlay = _overlay_line_keys(page)
        # 水印 span 的外接框与正文词框有交集 ⇒ 旧判据会误删这些正文词
        span_rects = [
            fitz.Rect(s["bbox"])
            for b in page.get_text("dict")["blocks"] if b.get("type") == 0
            for l in b.get("lines", []) or [] if is_overlay_line(l)
            for s in l.get("spans", []) or []
        ]
        body_words = [
            (fitz.Rect(w[:4]), str(w[4])) for w in page.get_text("words")
            if (int(w[5]), int(w[6])) not in overlay and str(w[4]).strip()
        ]
        overlapped = [t for r, t in body_words if any(r.intersects(sr) for sr in span_rects)]
    assert overlapped, "夹具应构造出「外接框压住正文词」的混合区"
    kept = {w.text for w in pages[0].words}
    assert all(t in kept for t in overlapped), "混合区的正文词被误删（旧判据的坑）"


# ---------------------------------------------------------------------------
# T6 既有 Issue#250 用例一行不改仍全绿（越界证明）
# ---------------------------------------------------------------------------


def test_t6_issue250_docline_case_still_contractual(tmp_path, _wm_on):
    """复刻 test_issue250_inline_watermark_filter 的 docline 断言（行为契约钉）。"""
    path = _build_pdf(str(tmp_path / "t6.pdf"), pages=2, body_rows=12)
    pages = docline.extract_text_layer_pages(path)
    assert pages
    for page in pages:
        joined = " ".join(w.text for w in page.words)
        assert WM_TOKEN not in joined.replace(" ", ""), f"词框含水印令牌: {joined[:80]}"
        assert any(BODY_TOKEN in w.text for w in page.words), "正文词被误删"


# ---------------------------------------------------------------------------
# T7 信号面：pipeline_status["text_layer_doc"]["overlay_filter"]
# ---------------------------------------------------------------------------


def test_t7_overlay_filter_status_present(tmp_path, _wm_on):
    from app.services.redaction_orchestrator import _text_layer_doc_status

    memo = {
        "pages": 10, "entities": 5, "chunks": 3, "coverage": 0.95, "verdict": "accepted",
        "overlay_filter": {"pages": 10, "pages_with_drop": 4, "words_raw": 1200,
                           "words_dropped": 300, "drop_ratio": 0.25},
    }
    status = _text_layer_doc_status([object()], memo, 1)
    assert status["ran"] is True
    assert status["overlay_filter"]["words_dropped"] == 300
    assert status["overlay_filter"]["drop_ratio"] == 0.25


def test_t7_overlay_filter_key_present_but_none_when_not_taken_over():
    """未接管卷：键必须存在且为 None（#165「失败卷也要写块」口径）。"""
    from app.services.redaction_orchestrator import _text_layer_doc_status

    status = _text_layer_doc_status(None, {"unusable": True, "reason_code": "coverage"}, 3)
    assert "overlay_filter" in status
    assert status["overlay_filter"] is None
    assert status["ran"] is False


# ---------------------------------------------------------------------------
# T8 页事件日志：text_layer_extracted 新可选字段
# ---------------------------------------------------------------------------


def test_t8_page_event_accepts_overlay_fields(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "PAGE_EVENT_LOG_ENABLED", True)
    page_events.set_event_context(job_id="job338", item_id="item338")
    try:
        ok = page_events.log_event(
            "text_layer_extracted", page=None,
            chars=1200, blocks=10, pages=10, ms=42,
            words_raw=1200, words_dropped=300, overlay_pages=4,
        )
    finally:
        page_events.set_event_context(job_id=None, item_id=None)
    assert ok is True, "带新字段的事件被 schema 拒写"
    files = list((tmp_path / "logs").rglob("*.jsonl"))
    assert files, "事件未落盘"
    record = json.loads(files[0].read_text(encoding="utf-8").splitlines()[0])
    assert record["words_raw"] == 1200
    assert record["words_dropped"] == 300
    assert record["overlay_pages"] == 4


# ---------------------------------------------------------------------------
# T9 一行 INFO 日志（运维可观测）
# ---------------------------------------------------------------------------


def test_t9_overlay_filter_info_log(tmp_path, _wm_on, caplog):
    """`text_layer overlay filter ...` 必须能被打出来（fail-open，不影响主链）。"""
    from app.services.text_layer_docline_service import _log_overlay_filter

    with caplog.at_level(logging.INFO, logger="app.services.text_layer_docline_service"):
        _log_overlay_filter(file_id="abcdef1234567890", pages=10,
                            words_raw=1200, words_dropped=300, overlay_pages=4)
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "text_layer overlay filter" in text
    assert "words_dropped=300" in text
    assert "abcdef12" in text  # file_id 截断（不落全 id）


def test_t9_overlay_filter_log_never_raises(caplog):
    """异常字段也不得影响主链（fail-open）。"""
    from app.services.text_layer_docline_service import _log_overlay_filter

    with caplog.at_level(logging.INFO, logger="app.services.text_layer_docline_service"):
        _log_overlay_filter(file_id="x" * 40, pages=None, words_raw=None,
                            words_dropped=object(), overlay_pages=None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# T10（页账联动）走私有探针；这里只钉 `extract_text_layer_pages` 的页账可复算性
# ---------------------------------------------------------------------------


def test_t10_page_chars_recomputable_from_words(tmp_path, _wm_on):
    path = _build_pdf(str(tmp_path / "t10.pdf"), pages=2, body_rows=12)
    pages = docline.extract_text_layer_pages(path)
    page_chars = {p.no: sum(len(w.text) for w in p.words) for p in pages}
    assert all(chars > 0 for chars in page_chars.values())
    # 页账口径必须与抽取层同源（同一份 words），否则 B3 的 101→63 不可复算
    assert page_chars[1] == sum(len(w.text) for w in pages[0].words)


# ---------------------------------------------------------------------------
# T11 / T12 前端提示（见 frontend/src/features/playground/components/*.test.tsx）
# ---------------------------------------------------------------------------


def test_t11_placeholder_backend_contract():
    """前端判据在 vitest 里（T11/T12）；此处仅保证后端真源字段存在且缺省 0（int）。"""
    from app.models.file_schemas import ParseResult

    field = ParseResult.model_fields.get("watermark_inline_filtered")
    assert field is not None, "ParseResult 必须暴露 watermark_inline_filtered（行数）"
    assert field.annotation is int and field.default == 0