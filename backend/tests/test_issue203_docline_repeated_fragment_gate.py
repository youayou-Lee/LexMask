"""Issue#203：预建链「跨页高频重复片段覆盖」闸。

事故形态（2026-10-28 第四卷两次真实复现）：扫描件内嵌文本层被防泄漏水印的打散
碎片词支配（页均真实内容 ~250 字符，水印串重复上千次），文档级链资格闸无水印
维度放行 → 整卷 21 万字符切成上百块跑 NER → 建链 await 撞单页 300s 门限 →
整卷作废。本闸在 `run_document_ner` **之前**拒绝此类卷（冷审 Minor-2：门必须
在 NER 前），机读原因回落逐页线。

判据（#203 正文）：跨页高频重复 n-gram 覆盖占比——出现 ≥K 次、跨越 ≥M 个不同页
的重复片段所覆盖字符占文档流比例。PII 最小化：一切对外观测面只带计数与占比，
**不带命中片段字面量**（水印串含真实姓名+编号）。

夹具形态按第四卷实测碎片校准（r1 Important-2/Minor-3）：水印以 2~4 字词 + 一枚
长数字词的**词组**重复出现，与真实内容词交错——不是整串连续文本。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from app.core.config import settings
from app.services import text_layer_doc_source as source
from app.services import text_layer_docline_service as docline

# 化名水印词组（结构对齐真实卷：机构碎片 + 姓名碎片 + 17 位数字串；不含真实 PII）
_STAMP_WORDS = ("某市某院", "某人某号", "0" * 17)


def _page(no: int, words: list[str], width: float = 595.0, height: float = 842.0):
    boxes = [
        docline.TextLayerWord(x0=float(i * 10), y0=0.0, x1=float(i * 10 + 8), y1=10.0, text=t)
        for i, t in enumerate(words)
    ]
    return docline.TextLayerPage(no=no, width=width, height=height, words=boxes)


def _watermark_pages(page_count: int, *, stamp_repeats: int, content_words: int) -> list:
    """每页 = stamp_repeats 组水印词组 + content_words 个互异内容词（2~4 字碎片形态）。"""
    pages = []
    for no in range(1, page_count + 1):
        words: list[str] = []
        for _ in range(stamp_repeats):
            words.extend(_STAMP_WORDS)
        for i in range(content_words):
            words.append(f"证据{no}页第{i}项材料")
        pages.append(_page(no, words))
    return pages


def _clean_pages(page_count: int, *, header: str = "某省某市人民法院某审某号", content_words: int = 60) -> list:
    """干净卷：每页一枚页眉（跨页重复，但占流长个位数百分比）+ 大量互异内容词。

    页眉长度**必须 ≥ `TEXT_LAYER_REPEATED_TOKEN_MIN_CHARS`(10)**，否则它根本不进
    counts，防误伤钉就成了假防护（门⑥ r2 F10：旧夹具页眉 9 字从未被检验）。
    """
    assert len(header) >= settings.TEXT_LAYER_REPEATED_TOKEN_MIN_CHARS, "夹具页眉必须够长"
    pages = []
    for no in range(1, page_count + 1):
        words = [header, f"王五{no}", *[f"讯问笔录第{no}页第{i}段问答内容记录" for i in range(content_words)]]
        pages.append(_page(no, words))
    return pages


def _gate_settings(monkeypatch, *, max_coverage: float, token_chars: int = 10, repeats: int = 4, min_pages: int = 5):
    monkeypatch.setattr(settings, "TEXT_LAYER_MAX_REPEATED_FRAGMENT_COVERAGE", max_coverage)
    monkeypatch.setattr(settings, "TEXT_LAYER_REPEATED_TOKEN_MIN_CHARS", token_chars)
    monkeypatch.setattr(settings, "TEXT_LAYER_REPEATED_FRAGMENT_MIN_REPEATS", repeats)
    monkeypatch.setattr(settings, "TEXT_LAYER_REPEATED_FRAGMENT_MIN_PAGES", min_pages)
    monkeypatch.setattr(settings, "TEXT_LAYER_DOCLINE_MAX_BUILD_SEC", 600.0)
    monkeypatch.setattr(settings, "TEXT_LAYER_DOCLINE_BUILD_SEC_PER_PAGE", 1.6)
    monkeypatch.setattr(settings, "TEXT_LAYER_MIN_CHARS_PER_PAGE", 60.0)
    monkeypatch.setattr(settings, "TEXT_LAYER_PAGE_MIN_CHARS", 60)


class _RecordingClient:
    """记录 NER 调用的替身（AC1 承重断言：被拒卷调用数必须为 0）。"""

    def __init__(self):
        self.ner_calls = 0

    def ner(self, text, labels=None, **kwargs):
        self.ner_calls += 1
        return {}

    def ner_checked(self, text, labels=None, **kwargs):
        self.ner_calls += 1
        return {}, True


def _patch_extract(monkeypatch, pages):
    monkeypatch.setattr(source.docline, "extract_text_layer_pages", lambda _path, **_k: pages)
    monkeypatch.setattr(source.docline, "sample_ink_alignment", lambda *a, **k: 0.5)


def _run_build(monkeypatch, tmp_path, pages, client):
    _patch_extract(monkeypatch, pages)
    monkeypatch.setattr(source, "has_client", client)
    path = tmp_path / "scan.pdf"
    path.write_bytes(b"%PDF-1.4 stub")
    return asyncio.run(
        source._build(
            "a" * 8,
            {"file_path": str(path), "file_type": "pdf_scanned", "is_scanned": True},
            ["人名", "编号"],
            None,
            None,
        )
    )


# ---------------------------------------------------------------- 闸：拒绝支路


def test_watermark_dominated_volume_rejected_before_ner(monkeypatch, tmp_path):
    """水印支配卷：闸必须在 run_document_ner 之前拒绝，NER 调用数 = 0（冷审 Minor-2）。"""
    _gate_settings(monkeypatch, max_coverage=0.30)
    client = _RecordingClient()
    with pytest.raises(source._TransientIneligible) as excinfo:
        _run_build(monkeypatch, tmp_path, _watermark_pages(12, stamp_repeats=8, content_words=15), client)
    assert excinfo.value.code == "repeated_fragment_coverage"
    assert client.ner_calls == 0, "闸必须在 NER 之前生效"


def test_rejection_is_permanent_negative_cache(monkeypatch, tmp_path):
    """AC4：拒绝落 permanent（retry_at=inf）负缓存——重试不重建、不再撞墙。"""
    _gate_settings(monkeypatch, max_coverage=0.30)
    _patch_extract(monkeypatch, _watermark_pages(12, stamp_repeats=8, content_words=15))
    path = tmp_path / "scan.pdf"
    path.write_bytes(b"%PDF-1.4 stub")
    source.reset_memo()
    boxes, memo = asyncio.run(
        source.boxes_for_page(
            file_id="b" * 8,
            snapshot={"file_path": str(path), "file_type": "pdf_scanned", "is_scanned": True},
            page=1,
            ocr_has_types=None,
        )
    )
    assert boxes is None
    assert memo is not None and memo.get("unusable") is True
    assert memo.get("reason_code") == "repeated_fragment_coverage"
    assert memo.get("retry_at") == float("inf")


def test_rejection_reason_detail_carries_no_fragment_literal(monkeypatch, tmp_path):
    """AC4 PII 最小化：机读面只带计数与占比，不带命中片段字面量（水印串含真实姓名+编号）。"""
    _gate_settings(monkeypatch, max_coverage=0.30)
    client = _RecordingClient()
    with pytest.raises(source._TransientIneligible) as excinfo:
        _run_build(monkeypatch, tmp_path, _watermark_pages(12, stamp_repeats=8, content_words=15), client)
    payload = json.dumps(
        {"reason": str(excinfo.value), "detail": excinfo.value.detail},
        ensure_ascii=False,
    )
    for word in _STAMP_WORDS:
        assert word not in payload, f"机读面不得出现命中片段字面量：{word[:4]}…"
    assert "repeated_fragment_coverage" in payload


# ---------------------------------------------------------------- 闸：放行支路


def test_clean_volume_with_repeated_headers_still_reaches_ner(monkeypatch, tmp_path):
    """防误伤钉：页眉类跨页重复（真实卷常态）不得触发闸，NER 照常到达。

    夹具页眉按 F10 修为 ≥10 字（旧夹具 9 字不进 counts=假防护）。
    """
    _gate_settings(monkeypatch, max_coverage=0.30)
    client = _RecordingClient()
    pages = _clean_pages(12)
    cov = docline.measure_repeated_fragment_coverage(pages)["coverage"]
    assert len(pages[0].words[0].text) >= settings.TEXT_LAYER_REPEATED_TOKEN_MIN_CHARS
    assert cov < 0.05, f"跨页页眉占比须远低于阈值（实测 {cov:.4f}）"
    result = _run_build(monkeypatch, tmp_path, pages, client)
    assert client.ner_calls >= 1, "干净卷必须照常进入 NER"
    assert result is not None and "covered_pages" in result


def test_near_threshold_volume_passes_gate(monkeypatch, tmp_path):
    """贴阈值内侧（门⑥ r2 F2 重做）：夹具覆盖率**实测**略低于**冻结默认阈值** 0.15，
    并显式断言数值——旧版用宽泛测试阈值 0.30 + 不断言数值，名不副实、无边界保护。
    """
    frozen = 0.15
    _gate_settings(monkeypatch, max_coverage=frozen)
    pages = _watermark_pages(12, stamp_repeats=5, content_words=40)
    cov = docline.measure_repeated_fragment_coverage(pages)["coverage"]
    assert frozen * 0.65 < cov < frozen, f"夹具必须真贴阈值内侧（实测 {cov:.4f} vs 阈值 {frozen}）"
    client = _RecordingClient()
    result = _run_build(monkeypatch, tmp_path, pages, client)
    assert client.ner_calls >= 1
    assert result is not None and "covered_pages" in result


def test_gate_uses_strict_greater_than_threshold(monkeypatch, tmp_path):
    """等号归属钉（门⑥ r2 F2：`>`↔`>=` 变异在旧测里 SURVIVED）：覆盖率 == 阈值时放行。"""
    _gate_settings(monkeypatch, max_coverage=0.15)
    monkeypatch.setattr(
        source.docline,
        "measure_repeated_fragment_coverage",
        lambda *_a, **_k: {"coverage": 0.15, "fragment_kinds": 1, "max_fragment_pages": 12},
    )
    client = _RecordingClient()
    result = _run_build(monkeypatch, tmp_path, _clean_pages(12), client)
    assert client.ner_calls >= 1, "coverage == 阈值不得拒绝（判据是严格大于）"
    assert result is not None


def test_issue203_factory_defaults_frozen():
    """AC5：四键出厂默认值冻结（判据名值经 AC0 冻结后进 config，带默认值）。"""
    assert settings.TEXT_LAYER_REPEATED_TOKEN_MIN_CHARS == 10
    assert settings.TEXT_LAYER_REPEATED_FRAGMENT_MIN_REPEATS == 8
    assert settings.TEXT_LAYER_REPEATED_FRAGMENT_MIN_PAGES == 5
    assert settings.TEXT_LAYER_MAX_REPEATED_FRAGMENT_COVERAGE == 0.15


def test_single_page_repeats_without_spanning_are_not_counted():
    """F1 回归钉：≥k 次但**不跨 m 页**的长词不得进覆盖分子（旧实现会把它计入）。"""
    single = [_page(1, ["0" * 17] * 20 + ["证据材料内容词"] * 5)]
    ignored = docline.measure_repeated_fragment_coverage(single, min_token_chars=10, min_repeats=4, min_pages=3)
    assert ignored == {"coverage": 0.0, "fragment_kinds": 0, "max_fragment_pages": 0}
    counted = docline.measure_repeated_fragment_coverage(single, min_token_chars=10, min_repeats=4, min_pages=1)
    assert counted["coverage"] > 0.5, "放开跨页条件后同一夹具必须可见 ⇒ 差异只来自跨页条件"


def test_nonspanning_high_frequency_tokens_do_not_inflate_coverage():
    """F1 定量钉：页内高频长 token（不跨页）不得放大 coverage。

    评审实测旧实现把它放大 7.5×（0.0311→0.2325），在出厂阈值 0.15 下会误拒正常卷。
    """
    header = "某省某市人民法院刑事审判庭讯问笔录材料"

    def _volume(with_per_page_stamps: bool) -> list:
        pages = []
        for no in range(1, 13):
            words = [header]
            if with_per_page_stamps:
                words.extend([f"{no:02d}号证据材料清单编号甲乙丙丁戊"] * 8)
            words.extend(f"证据{no}页第{i}项材料内容记录" for i in range(40))
            pages.append(_page(no, words))
        return pages

    with_stamps = docline.measure_repeated_fragment_coverage(_volume(True))
    without = docline.measure_repeated_fragment_coverage(_volume(False))
    assert with_stamps["fragment_kinds"] == 1, "只有跨页页眉算达标长词"
    assert with_stamps["coverage"] < 0.15, (
        f"页内高频长 token 不得把 coverage 推过出厂阈值（实测 {with_stamps['coverage']:.4f}）"
    )
    assert with_stamps["coverage"] < without["coverage"], "不跨页长词只能稀释分母，绝不允许放大分子"


def test_measure_degenerate_inputs_return_empty_payload():
    """F11/F13：退化输入（无页/纯短碎片/单页）不得抛错，一律零载荷。

    「纯 <10 字碎片卷」这条同时把 #203 的已知盲区（F13）写成判据：短碎片不可见。
    """
    empty = {"coverage": 0.0, "fragment_kinds": 0, "max_fragment_pages": 0}
    assert docline.measure_repeated_fragment_coverage([], min_token_chars=10, min_repeats=4, min_pages=3) == empty
    short_only = [_page(no, ["某院"] * 30) for no in range(1, 9)]
    assert (
        docline.measure_repeated_fragment_coverage(short_only, min_token_chars=10, min_repeats=4, min_pages=3) == empty
    )
    one_page = [_page(1, ["证据材料内容词"] * 10)]
    assert (
        docline.measure_repeated_fragment_coverage(one_page, min_token_chars=10, min_repeats=4, min_pages=3) == empty
    )


def test_mixed_volume_covered_pages_semantics_unchanged(monkeypatch, tmp_path):
    """AC3：新维度不影响 covered_pages 语义——无层页不因闸存在而改变覆盖判定。"""
    _gate_settings(monkeypatch, max_coverage=0.30)
    client = _RecordingClient()
    pages = _clean_pages(2)
    pages.append(_page(3, []))  # 无文本层的页
    result = _run_build(monkeypatch, tmp_path, pages, client)
    assert result is not None
    assert result["covered_pages"] == {1, 2}


# ---------------------------------------------------------------- 度量函数本体


def test_measure_returns_numeric_payload_only():
    """度量载荷只有计数与占比（PII 最小化的度量侧根基）。"""
    pages = _watermark_pages(6, stamp_repeats=4, content_words=20)
    metrics = docline.measure_repeated_fragment_coverage(pages, min_token_chars=10, min_repeats=4, min_pages=3)
    assert set(metrics) == {"coverage", "fragment_kinds", "max_fragment_pages"}
    assert all(isinstance(v, (int, float)) for v in metrics.values())
    assert 0.0 <= metrics["coverage"] <= 1.0


def test_measure_repeated_stamp_scores_high_single_pass_scores_zero():
    """判别力：重复水印长词（高熵 token）覆盖显著；模板样板/页眉卷覆盖趋零。"""
    stamped = _watermark_pages(8, stamp_repeats=8, content_words=15)
    clean = _clean_pages(8, content_words=40)
    hi = docline.measure_repeated_fragment_coverage(stamped, min_token_chars=10, min_repeats=4, min_pages=3)
    lo = docline.measure_repeated_fragment_coverage(clean, min_token_chars=10, min_repeats=4, min_pages=3)
    assert hi["coverage"] > 0.30
    assert lo["coverage"] < 0.05
    assert hi["coverage"] >= 5 * max(lo["coverage"], 0.01), "判别间隔须 ≥5×（#203 AC0 口径）"


def test_measure_short_stream_is_zero():
    pages = [_page(1, ["太短"])]
    metrics = docline.measure_repeated_fragment_coverage(pages, min_token_chars=10, min_repeats=4, min_pages=3)
    assert metrics["coverage"] == 0.0
