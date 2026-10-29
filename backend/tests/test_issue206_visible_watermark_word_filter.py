"""Issue#206 阶段 2'：可见平铺文本水印探测 + 建链流词级过滤。

形态（门④ 实测）：水印不是像素，是盖在扫描图上的倾斜半透明平铺文本层——每页
1 条 text-showing op，把同一个 4 字词平铺几十次；正文层同字体但 `alpha=0`
（不可见）。docline 把水印词当正文送 NER ⇒ 实测一卷 31 页里 77.7% 的框是纯水印框。

本文件的夹具全部**合成**（`fitz.TextWriter` + `morph` 旋转 + `opacity`），不含真实
案卷内容；水印词用化名，PII 面为零。

钉住的判据：
- 探测：倾斜（|dir.y|≥0.2）**且**半透明（0<alpha<220）；**alpha=0 的倾斜不可见层
  必须判 `no_watermark`**（阶段 1 的红测教训：只写 alpha<220 会把它误判成水印）。
- 过滤：字符集 ⊆ 水印字符集 **且** 字长 ≥2 **且 词框被本页水印字形框覆盖 ≥0.35**
  才可能是水印词；本页落在水印字形上的同形词 ≥8 次才整组丢弃（正文位置的同形词
  一律保留——门④ GT 实测：族 B 不可见正文层是带水印图的 OCR，同页有十几份水印
  串，纯文本判据会把正文实例误丢）。每页丢字符超「每页水印字符数中位数 ×1.5」时
  该页保持原样；无几何 / 无 profile / 开关关 / 异常 ⇒ 原样返回。
- 接线：三道资格闸吃**未过滤**页，只有 `run_document_ner` 吃过滤后的页。
- 可观测：`docline_word_filter` 只带计数与比例，不带字面量。
"""

from __future__ import annotations

import asyncio

import fitz

from app.core.config import settings
from app.services import text_layer_doc_source as source
from app.services import text_layer_docline_service as docline
from app.services import visible_text_watermark as vwm

_WM_WORD = "某市某院"  # 4 字化名水印词（结构对齐真卷：机构简称平铺）
_BODY_LINE = "本案证据材料经依法调取并逐页核对后附卷在案以备审查判断之用"  # 25 字正文


def _write_rotated(page, point, text: str, *, angle: float, opacity: float, size: float = 28.0) -> None:
    writer = fitz.TextWriter(page.rect, color=(0.25, 0.25, 0.25))
    writer.append(point, text, font=fitz.Font("china-s"), fontsize=size)
    writer.write_text(page, opacity=opacity, morph=(fitz.Point(point), fitz.Matrix(angle)))


def _wm_pdf(path, *, pages: int = 3, tiles: int = 65, angle: float = 30.0,
            opacity: float = 0.3, body_alpha: float = 0.0, size: float = 28.0) -> str:
    """族 B 形态：倾斜半透明平铺水印 + 不可见正文层（`body_alpha=0`）。"""
    doc = fitz.open()
    for _ in range(pages):
        page = doc.new_page(width=595, height=842)
        cols, rows = 5, 13
        placed = 0
        for row in range(rows):
            for col in range(cols):
                if placed >= tiles:
                    break
                _write_rotated(
                    page,
                    (40.0 + col * 110.0, 40.0 + row * 40.0),
                    _WM_WORD,
                    angle=angle,
                    opacity=opacity,
                    size=size,
                )
                placed += 1
        if body_alpha is not None:
            # 正文放**下半页**：与平铺水印的字形框不重叠 ⇒ 几何判据不会把正文判成水印
            writer = fitz.TextWriter(page.rect)
            for i in range(5):
                writer.append((40.0, 620.0 + i * 40.0), _BODY_LINE, font=fitz.Font("china-s"), fontsize=11)
            writer.write_text(page, opacity=body_alpha)
    doc.save(path)
    doc.close()
    return str(path)


def _plain_pdf(path) -> str:
    """干净卷：水平不透明正文。"""
    doc = fitz.open()
    for _ in range(3):
        page = doc.new_page(width=595, height=842)
        writer = fitz.TextWriter(page.rect, color=(0, 0, 0))
        for i in range(20):
            writer.append((40.0, 40.0 + i * 38.0), f"第{i}段正文内容：这是普通不透明文字。",
                          font=fitz.Font("china-s"), fontsize=12)
        writer.write_text(page, opacity=1.0)
    doc.save(path)
    doc.close()
    return str(path)


def _tile_box(col: int, row: int) -> tuple[float, float, float, float]:
    x0 = 60.0 + col * 110.0
    y0 = 70.0 + row * 58.0
    return x0, y0, x0 + 55.0, y0 + 28.0


def _body_box(index: int, text: str) -> tuple[float, float, float, float]:
    x0, y0 = 40.0, 780.0 + index * 18.0
    return x0, y0, x0 + max(8.0, 11.0 * len(text)), y0 + 14.0


def _pages_with_geometry(
    specs: list[dict],
) -> tuple[list[docline.TextLayerPage], dict[int, list[fitz.Rect]]]:
    """构造词页 + 与之匹配的「水印字形框」。specs 每项：`tiles`（平铺份数）、
    `body`（正文词列表）、`extra`（额外词，可指定 `covered` 表示落在水印字形上）、
    `geometry`（False ⇒ 该页不给字形框）。"""
    pages: list[docline.TextLayerPage] = []
    rects: dict[int, list[fitz.Rect]] = {}
    for index, spec in enumerate(specs, start=1):
        words: list[docline.TextLayerWord] = []
        page_rects: list[fitz.Rect] = []
        tiles = int(spec.get("tiles", 0))
        placed = 0
        for row in range(13):
            for col in range(5):
                if placed >= tiles:
                    break
                x0, y0, x1, y1 = _tile_box(col, row)
                words.append(docline.TextLayerWord(x0=x0, y0=y0, x1=x1, y1=y1, text=_WM_WORD))
                page_rects.append(fitz.Rect(x0, y0, x1, y1))
                placed += 1
        for body_index, text in enumerate(spec.get("body", [])):
            x0, y0, x1, y1 = _body_box(body_index, text)
            words.append(docline.TextLayerWord(x0=x0, y0=y0, x1=x1, y1=y1, text=text))
        for extra_index, item in enumerate(spec.get("extra", [])):
            if isinstance(item, tuple):
                text, covered = item
            else:
                text, covered = item, False
            if covered:
                x0, y0, x1, y1 = _tile_box(0, 12)  # 与平铺同位（几何上算水印）
                page_rects.append(fitz.Rect(x0, y0, x1, y1))
            else:
                x0, y0, x1, y1 = _body_box(50 + extra_index, text)
            words.append(docline.TextLayerWord(x0=x0, y0=y0, x1=x1, y1=y1, text=text))
        pages.append(
            docline.TextLayerPage(no=index, width=595.0, height=842.0, words=words)
        )
        if spec.get("geometry", True) and page_rects:
            rects[index] = page_rects
    return pages, rects


def _profile(charset_words: str = _WM_WORD, per_page: int = 260) -> vwm.WatermarkProfile:
    return vwm.WatermarkProfile(
        wm_charset=frozenset(set(charset_words)),
        signature=("china-s", 0.866, -0.5, 0.3),
        pages_probed=5,
        pages_hit=5,
        visible_share=1.0,
        top_sig_share=1.0,
        size_spread=0.0,
        wm_chars_per_page={1: per_page, 2: per_page, 3: per_page},
    )


# ------------------------------------------------------------------ 过滤（规则）


def test_filter_drops_covered_tiled_words():
    pages, rects = _pages_with_geometry([{"tiles": 65, "body": [_BODY_LINE]}] * 2)
    filtered, stats = vwm.filter_watermark_words(pages, _profile(), wm_rects=rects)

    assert stats["applied"] is True and stats["code"] == "applied"
    assert stats["pages_filtered"] == 2
    assert stats["words_dropped"] == 65 * 2
    assert stats["chars_dropped"] == len(_WM_WORD) * 65 * 2
    for page in filtered:
        assert all(w.text != _WM_WORD for w in page.words)
        assert any(w.text == _BODY_LINE for w in page.words)  # 正文还在


def test_filter_keeps_body_position_copies_of_watermark_text():
    """承重：正文位置的同形词（几何上不被水印字形覆盖）即使字符集全在水印字符集里、
    同页水印份数远超阈值，也不得丢——这是门④ GT 抓出来的漏打码风险的守门测试。"""
    pages, rects = _pages_with_geometry(
        [{"tiles": 65, "body": [], "extra": [(_WM_WORD, False), (_WM_WORD, False)]}]
    )
    filtered, stats = vwm.filter_watermark_words(pages, _profile(), wm_rects=rects)
    kept = [w.text for w in filtered[0].words]
    assert kept.count(_WM_WORD) == 2
    assert stats["words_dropped"] == 65


def test_filter_requires_geometry_repeat_not_page_repeat():
    """只有 3 份落在水印字形上（<8）⇒ 一份也不丢（跨页重复不算）。"""
    pages, rects = _pages_with_geometry([{"tiles": 3, "body": [_BODY_LINE]}])
    filtered, stats = vwm.filter_watermark_words(pages, _profile(), wm_rects=rects)
    assert stats["applied"] is False and stats["words_dropped"] == 0
    assert [w.text for w in filtered[0].words].count(_WM_WORD) == 3


def test_filter_single_char_words_are_never_dropped():
    pages, rects = _pages_with_geometry(
        [{"tiles": 65, "body": [], "extra": [("院", True) for _ in range(12)]}]
    )
    filtered, stats = vwm.filter_watermark_words(pages, _profile(), wm_rects=rects)
    assert [w.text for w in filtered[0].words].count("院") == 12
    assert stats["words_dropped"] == 65


def test_filter_high_frequency_body_phrase_is_kept():
    """AC22：正文高频短语（同页 5 次、全卷上百次）即便字符集 ⊆ 水印字符集也不得丢。"""
    phrase = "本院认为"
    pages, rects = _pages_with_geometry(
        [{"tiles": 65, "body": [phrase] * 5, "extra": [(phrase, False) for _ in range(5)]}]
    )
    profile = _profile(charset_words=_WM_WORD + phrase)
    filtered, stats = vwm.filter_watermark_words(pages, profile, wm_rects=rects)
    kept = [w.text for w in filtered[0].words]
    assert kept.count(phrase) == 10
    assert stats["words_dropped"] == 65


def test_filter_page_without_geometry_is_kept_verbatim():
    pages, rects = _pages_with_geometry([{"tiles": 65, "body": [_BODY_LINE], "geometry": False}])
    filtered, stats = vwm.filter_watermark_words(pages, _profile(), wm_rects=rects)
    assert filtered[0] is pages[0]
    assert stats["pages_skipped_no_geometry"] == 1
    assert stats["applied"] is False


def test_filter_skips_page_when_drop_exceeds_watermark_budget():
    """丢字符数超出「每页水印字符数中位数 × 1.5」⇒ 该页原样保留（不变式越界）。"""
    # 65 份平铺 = 260 字符；预算 = 每页水印字符数中位数(100) × 1.5 = 150 ⇒ 越界
    pages, rects = _pages_with_geometry([{"tiles": 65, "body": [_BODY_LINE]}])
    filtered, stats = vwm.filter_watermark_words(pages, _profile(per_page=100), wm_rects=rects)
    assert filtered[0] is pages[0]
    assert stats["code"] == "filter_invariant_exceeded"
    assert stats["pages_skipped_invariant"] == 1
    assert stats["words_dropped"] == 0


def test_filter_strips_thin_page_body_under_120_chars():
    """薄页（正文 <120 字）必须照样剥：靠总字符占比判会把最需要过滤的页全部挡下。"""
    thin = "本案证据材料"  # 6 字正文
    pages, rects = _pages_with_geometry([{"tiles": 65, "body": [thin]}])
    filtered, stats = vwm.filter_watermark_words(pages, _profile(), wm_rects=rects)
    assert stats["applied"] is True and stats["pages_filtered"] == 1
    kept = [w.text for w in filtered[0].words]
    assert kept.count(_WM_WORD) == 0 and kept.count(thin) == 1


def test_filter_noop_without_profile():
    pages, rects = _pages_with_geometry([{"tiles": 65, "body": [_BODY_LINE]}])
    filtered, stats = vwm.filter_watermark_words(pages, None, wm_rects=rects)
    assert filtered is pages
    assert stats["code"] == "no_watermark" and stats["applied"] is False


# ------------------------------------------------------------------ 编排入口（回落）


def test_maybe_filter_pages_disabled_by_default(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "VISIBLE_WATERMARK_WORD_FILTER_ENABLED", False)
    pages, _rects = _pages_with_geometry([{"tiles": 65, "body": [_BODY_LINE]}])
    filtered, stats = vwm.maybe_filter_pages(_wm_pdf(tmp_path / "wm2.pdf"), pages)
    assert filtered is pages
    assert stats["code"] == "disabled"


def test_maybe_filter_pages_probe_failure_falls_back(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "VISIBLE_WATERMARK_WORD_FILTER_ENABLED", True)

    def _boom(*_args, **_kwargs):
        raise RuntimeError("probe explode")

    monkeypatch.setattr(vwm, "probe_visible_watermark", _boom)
    pages, _rects = _pages_with_geometry([{"tiles": 65, "body": [_BODY_LINE]}])
    filtered, stats = vwm.maybe_filter_pages(str(tmp_path / "whatever.pdf"), pages)
    assert filtered is pages
    assert stats["code"] == "filter_failed"


def test_maybe_filter_pages_end_to_end_on_pdf(tmp_path, monkeypatch):
    """真 PDF 端到端：探测 → 取几何 → 过滤，水印词全消、正文留下。"""
    monkeypatch.setattr(settings, "VISIBLE_WATERMARK_WORD_FILTER_ENABLED", True)
    path = _wm_pdf(tmp_path / "wm3.pdf")
    pages = docline.extract_text_layer_pages(path)
    filtered, stats = vwm.maybe_filter_pages(path, pages)
    assert stats["code"] == "applied", stats
    # 合成夹具里贴着页顶的平铺可能根本不在词层里（PyMuPDF 裁掉极端字形），
    # 因此期望值按「词层里实际存在的平铺份数」算，不写死 65×页数
    expected = sum(1 for page in pages for w in page.words if w.text == _WM_WORD)
    assert expected > 0
    assert stats["chars_dropped"] == len(_WM_WORD) * expected
    assert stats["pages_skipped_no_geometry"] == 0
    for page in filtered:
        assert all(w.text != _WM_WORD for w in page.words)
        assert any(w.text.startswith("本案证据") for w in page.words)  # 正文还在


def _usable_verdict():
    class _Verdict:
        usable = True
        severity = "USABLE"
        reasons: list[str] = []

    return _Verdict()


def _docline_result(pages: int) -> docline.DocLineResult:
    return docline.DocLineResult(
        pages=pages, chunks=1, chunk_chars=100, stream_chars=100, layer_chars=100,
        covered_chars=100, entities=[], ner_calls=1, truncated_chunks=0, http_errors=0,
        residual_calls=0, seconds=0.1,
    )


def test_build_feeds_only_ner_with_filtered_pages(monkeypatch, tmp_path):
    """硬钉：三道资格闸吃**未过滤**页，`run_document_ner` 吃过滤后的页。"""
    original_pages, _rects = _pages_with_geometry([{"tiles": 65, "body": [_BODY_LINE]}] * 2)
    filtered_pages, _r2 = _pages_with_geometry([{"tiles": 0, "body": [_BODY_LINE]}] * 2)
    seen: dict[str, object] = {}

    monkeypatch.setattr(source, "_labels", lambda _types: ["姓名"])
    monkeypatch.setattr(source, "build_label_to_type_id", lambda _types: {})
    monkeypatch.setattr(docline, "extract_text_layer_pages", lambda *_a, **_k: original_pages)
    monkeypatch.setattr(docline, "sample_ink_alignment", lambda *_a, **_k: 0.5)

    class _Metrics:
        ink_alignment = 0.5

    monkeypatch.setattr(source.quality, "compute_metrics", lambda *_a, **_k: _Metrics())
    monkeypatch.setattr(source.quality, "classify", lambda *_a, **_k: _usable_verdict())

    def _repeat(pages):
        seen["repeat_pages"] = pages
        return {"coverage": 0.0, "fragment_kinds": 0, "max_fragment_pages": 0}

    monkeypatch.setattr(docline, "measure_repeated_fragment_coverage", _repeat)

    def _filter(path, pages):
        seen["filter_input"] = pages
        return filtered_pages, {"applied": True, "code": "applied", "chars_dropped": 260}

    monkeypatch.setattr(vwm, "maybe_filter_pages", _filter)

    async def _ner(pages, labels, **_kwargs):
        seen["ner_pages"] = pages
        return _docline_result(len(pages))

    monkeypatch.setattr(docline, "run_document_ner", _ner)

    real_path = tmp_path / "whatever.pdf"
    real_path.write_bytes(b"%PDF-1.4\n")
    snapshot = {
        "file_path": str(real_path),
        "file_type": "pdf_scanned",
        "is_scanned": True,
    }
    memo = asyncio.run(source._build("fid", snapshot, ["姓名"], ["t1"], []))
    assert seen["repeat_pages"] is original_pages  # 闸未被过滤影响
    assert seen["filter_input"] is original_pages
    assert seen["ner_pages"] is filtered_pages
    assert memo is not None and memo["word_filter"]["code"] == "applied"


def test_text_layer_doc_status_exposes_word_filter():
    from app.services.redaction_orchestrator import _text_layer_doc_status

    stats = {"applied": True, "code": "applied", "chars_dropped": 8060}
    status = _text_layer_doc_status(
        [object()], {"pages": 31, "entities": 1, "chunks": 7, "word_filter": stats}, 1
    )
    assert status["word_filter"] == stats
    # 失败卷（marker）也要带这块，否则回落时看不到过滤发生过
    marker_status = _text_layer_doc_status(None, {"unusable": True, "reason_code": "coverage"}, 1)
    assert marker_status["word_filter"] is None


# ------------------------------------------------------------------ 异常与边界（AC23）


def test_maybe_filter_pages_corrupt_pdf_falls_back(tmp_path, monkeypatch):
    """损坏 PDF：探测抛错 ⇒ 原样返回 + `filter_failed`，绝不引入新失败。"""
    monkeypatch.setattr(settings, "VISIBLE_WATERMARK_WORD_FILTER_ENABLED", True)
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"%PDF-1.4\n not really a pdf \x00\x01")
    pages, _rects = _pages_with_geometry([{"tiles": 65, "body": [_BODY_LINE]}])
    filtered, stats = vwm.maybe_filter_pages(str(broken), pages)
    assert filtered is pages
    assert stats["code"] == "filter_failed"


def test_probe_returns_none_for_encrypted_without_password(tmp_path):
    """加密卷（未解密落盘）：探测一律 None ⇒ 不剥（生产链里 parse 阶段已解密，轮不到这里）。"""
    path = tmp_path / "locked.pdf"
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    _write_rotated(page, (60.0, 70.0), _WM_WORD, angle=30.0, opacity=0.3)
    doc.save(str(path), encryption=fitz.PDF_ENCRYPT_AES_256, user_pw="123456", owner_pw="123456")
    doc.close()
    assert vwm.probe_visible_watermark(str(path)) is None


def test_probe_returns_none_for_empty_document(tmp_path):
    path = tmp_path / "empty.pdf"
    doc = fitz.open()
    doc.new_page(width=595, height=842)
    doc.save(str(path))
    doc.close()
    assert vwm.probe_visible_watermark(str(path)) is None


def test_memo_knob_key_tracks_filter_settings(monkeypatch):
    """AC27：开关与三个阈值任一变化都必须改变 memo key（否则两臂零差异会假通过）。"""
    from app.services import text_layer_doc_source as source

    baseline = source._word_filter_knob_key()
    for name, value in (
        ("VISIBLE_WATERMARK_WORD_FILTER_ENABLED", True),
        ("VISIBLE_WATERMARK_WORD_MIN_REPEAT", 9),
        ("VISIBLE_WATERMARK_WORD_COVER_MIN", 0.5),
        ("VISIBLE_WATERMARK_MAX_DROP_VS_WM_CHARS", 2.0),
        ("VISIBLE_WATERMARK_PROBE_PAGES", 4),
    ):
        monkeypatch.setattr(settings, name, value)
        assert source._word_filter_knob_key() != baseline, name
        monkeypatch.undo()
