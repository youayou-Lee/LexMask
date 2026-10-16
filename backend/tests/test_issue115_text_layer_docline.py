"""Issue#115 文档级文本层链的单测（C1 分类器 / C2 服务 / C4 接入）。

判别力说明（对应验收方案 v2 §5.0 自检）：
- 这些用例在**未实现本功能**的树上是 import error / 断言失败 —— 不是恒真条；
- 「分类器不平凡」用**成对**样本钉住（同长度、好层 USABLE vs 坏层 SUSPECT/UNUSABLE）；
- 「接入不静默」用桩把异常注入 `run_document_ner`，断言**退回旧路径且不抛**。
"""
from __future__ import annotations

import asyncio
import logging
import re

import pytest

from app.core.config import settings
from app.core.safe_regex import (
    RegexTimeoutError,
    _finditer_many_in_process,
    _MatchProxy,
    scaled_timeout,
)
from app.services import text_layer_doc_source as source
from app.services import text_layer_docline_service as docline
from app.services import text_layer_quality as quality


def _inproc_safe_compile(pattern, timeout=2.0):
    """替身 `safe_compile`：只做语法编译，不跑子进程探针。

    regex 保证层自 Issue#64/#65 起与文本链/vision 同源（冷审 C1），走
    `safe_compile` / `safe_finditer`；本机沙箱建不出进程池（Issue#109），故用
    替身保留本文件对语义的覆盖，真实池的端到端对照在
    `test_issue64_65_regex_fallback.py` / `test_issue43_case_number_floor.py`。
    """
    return re.compile(pattern)


def _inproc_safe_finditer(compiled, text, timeout=5.0):
    """替身 `safe_finditer`：复用真实序列化语义，只是不跨进程。"""
    rows = _finditer_many_in_process(compiled.pattern, compiled.flags, [text])[0]
    return [_MatchProxy(t, s, e, named) for t, s, e, named in rows]


class _NullClient:
    def ner(self, text, entity_types=None, **kwargs):
        return {}

# ------------------------------------------------------------------ Issue#129 默认值


def test_docline_is_enabled_by_default():
    """Issue#129：本链**代码默认值**为开启（用户 2026-10-15 拍板）。

    断言的是 `Settings` 的**字段默认值**而不是当前进程的生效值：后者会被
    `backend/.env` 或环境变量（`env_file` 见 `app/core/config.py`）覆盖，
    在有存量 `.env` 的机器上会让本用例变红，而代码默认其实是对的
    （门⑥ 评审 M3）。生效值的端到端证明在门④ 云上做（不导出任何
    `TEXT_LAYER_*` 环境变量重启 backend，断言链被激活且 `ocr_calls=0`）。

    判别力：本断言在 Issue#129 之前为 `False` ⇒ **不改动即失败**，不是恒真条。
    """
    field = type(settings).model_fields["TEXT_LAYER_DOCLINE_ENABLED"]
    assert field.default is True


# ------------------------------------------------------------------ C1 分类器


def test_classify_rejects_empty_and_sparse_layers():
    empty = quality.compute_metrics([])
    assert quality.classify(empty).severity == quality.UNUSABLE

    sparse = quality.compute_metrics([["甲", "乙", "丙"]])
    verdict = quality.classify(sparse)
    assert verdict.severity == quality.UNUSABLE
    assert any(reason.startswith("sparse_text_layer") for reason in verdict.reasons)


def test_classify_is_paired_on_same_length_input():
    """同长度成对样本：好层 USABLE、坏层降级 —— 钉住分类器不是恒真。"""
    good_words = [f"京市海字第{i}号" for i in range(24)]
    garbage_words = ["\ufffd\u0001\ufffd"] * 24

    good = quality.classify(quality.compute_metrics([good_words] , ink_alignment=0.18))
    bad = quality.classify(quality.compute_metrics([garbage_words], ink_alignment=0.18))

    assert quality.classify(good.metrics).severity == quality.USABLE
    assert bad.severity != quality.USABLE


def test_classify_low_ink_alignment_downgrades_usability():
    words = [f"证据材料卷宗第{i}页" for i in range(24)]
    metrics = quality.compute_metrics([words], ink_alignment=0.005)
    verdict = quality.classify(metrics)
    assert verdict.severity == quality.SUSPECT
    assert not verdict.usable
    assert any(reason.startswith("ink_alignment") for reason in verdict.reasons)


# ------------------------------------------------------------------ 分块与流


def _page(no: int, words: list[str], width: float = 595.0, height: float = 842.0) -> docline.TextLayerPage:
    boxes: list[docline.TextLayerWord] = []
    x = 50.0
    for word in words:
        boxes.append(docline.TextLayerWord(x, 700.0, x + 40.0, 720.0, word))
        x += 45.0
    return docline.TextLayerPage(no=no, width=width, height=height, words=boxes)


def test_chunks_cover_stream_without_splitting_words():
    pages = [_page(1, ["北京大学", "2023年", "第12号"])]
    stream, spans = docline.build_stream(pages)
    chunks = docline.iter_chunks(stream, chunk_chars=200, overlap=20)

    assert chunks, "至少要有一块"
    for word in ("北京大学", "2023年", "第12号"):
        assert any(word in text for text in (c[0] for c in chunks)), f"词被切碎: {word}"
    assert docline._union_coverage(chunks, len(stream)) == len(stream)


def test_union_coverage_does_not_double_count_overlap():
    chunks = [("a", 0, 10), ("b", 5, 20), ("c", 20, 30)]
    assert docline._union_coverage(chunks, 30) == 30
    assert docline._union_coverage([("a", 0, 10), ("b", 0, 10)], 30) == 10


# ------------------------------------------------------------------ 值回映


def test_find_value_spans_is_nfkc_insensitive():
    """真卷坑：层里是全角 `１－１７`，模型回半角 `1-17`。"""
    stream = "文号 １－１７ 号"
    normalized, index_map = docline._normalized_stream(stream)
    spans = docline.find_value_spans(normalized, index_map, "1-17")
    assert spans, "NFKC 归一后必须能找到"
    start, end = spans[0]
    assert "１－１７" in stream[start:end]


def test_map_values_returns_page_and_normalized_bbox():
    pages = [_page(1, ["甲方：", "张三", "住北京市"]), _page(2, ["乙方：", "李四"])]
    stream, spans = docline.build_stream(pages)
    entities, failures = docline.map_values(pages, stream, spans, {"姓名": {"张三", "李四"}, "不存在": {"王五"}})

    by_value = {e.value: e for e in entities}
    assert set(by_value) == {"张三", "李四"}
    assert by_value["张三"].page == 1
    assert by_value["李四"].page == 2
    for entity in entities:
        x0, y0, x1, y1 = entity.bbox
        assert 0.0 <= x0 < x1 <= 1.0
        assert 0.0 <= y0 < y1 <= 1.0
    assert [f["value"] for f in failures] == ["王五"]


def test_residual_text_excludes_already_found_values():
    text = "甲：张三 乙：李四 丙：王五"
    residual = docline._residual_text(text, ["张三"])
    assert "张三" not in residual
    assert "王五" in residual


def test_run_document_ner_calls_model_and_counts_residual():
    pages = [_page(1, ["甲：", "张三", "乙：", "李四"])]
    calls: list[tuple[str, tuple[str, ...]]] = []

    class Client:
        def ner(self, text, labels=None, *args, **kwargs):
            calls.append((text, tuple(labels or [])))
            return {labels[0]: ["张三"] if "张三" in text else []}

    result = asyncio.run(docline.run_document_ner(pages, ["姓名"], has_client=Client(), residual_pass=True))

    assert result.ner_calls == 1
    assert result.residual_calls == 0  # residual 只剩几十字，低于下限
    assert [e.value for e in result.entities] == ["张三"]
    assert result.coverage_ratio == pytest.approx(1.0)


# ------------------------------------------------------------------ C4 接入


def _snapshot(path: str, file_type: str = "pdf_scanned") -> dict:
    return {"file_path": path, "file_type": file_type, "is_scanned": True}


def test_boxes_for_page_disabled_switch_returns_none(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "TEXT_LAYER_DOCLINE_ENABLED", False)
    boxes, memo = asyncio.run(
        source.boxes_for_page(file_id="f" * 8, snapshot=_snapshot(str(tmp_path / "a.pdf")), page=1, ocr_has_types=None)
    )
    assert boxes is None and memo is None


def test_boxes_for_page_skips_non_scanned_file(monkeypatch):
    monkeypatch.setattr(settings, "TEXT_LAYER_DOCLINE_ENABLED", True)
    boxes, memo = asyncio.run(
        source.boxes_for_page(
            file_id="f" * 8,
            snapshot={"file_path": "/nonexistent/x.pdf", "file_type": "pdf_text", "is_scanned": False},
            page=1,
            ocr_has_types=None,
        )
    )
    assert boxes is None and memo is None


def test_boxes_for_page_uses_memo_once_and_maps_pages(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "TEXT_LAYER_DOCLINE_ENABLED", True)
    path = tmp_path / "scan.pdf"
    path.write_bytes(b"%PDF-1.4 stub")
    source.reset_memo()
    builds: list[int] = []

    pages = [
        _page(1, ["张三", *[f"证据材料第{i}页" for i in range(20)]]),
        _page(2, ["李四", *[f"证据材料第{i}页" for i in range(20)]]),
    ]

    def fake_extract(_path, **_kwargs):
        return pages

    async def fake_run(page_list, labels, *, has_client, **kwargs):
        builds.append(1)
        return docline.DocLineResult(
            pages=2, chunks=1, chunk_chars=2000, stream_chars=10, layer_chars=8, covered_chars=10,
            entities=[docline.DocLineEntity(1, "PERSON", "张三", (0.1, 0.2, 0.3, 0.4)),
                      docline.DocLineEntity(2, "PERSON", "李四", (0.1, 0.2, 0.3, 0.4))],
            ner_calls=1, truncated_chunks=0, http_errors=0, residual_calls=0, seconds=0.01,
        )

    monkeypatch.setattr(source.docline, "extract_text_layer_pages", fake_extract)
    monkeypatch.setattr(source.docline, "sample_ink_alignment", lambda *a, **k: 0.18)
    monkeypatch.setattr(source.docline, "run_document_ner", fake_run)

    first, memo1 = asyncio.run(source.boxes_for_page(file_id="a" * 8, snapshot=_snapshot(str(path)), page=1, ocr_has_types=None))
    second, memo2 = asyncio.run(source.boxes_for_page(file_id="a" * 8, snapshot=_snapshot(str(path)), page=2, ocr_has_types=None))

    assert len(builds) == 1, "整卷只跑一次（per-file memo）"
    assert [b.text for b in first] == ["张三"]
    assert [b.text for b in second] == ["李四"]
    assert first[0].source == "ocr_has" and first[0].source_detail == source.SOURCE_DETAIL
    assert memo1 is memo2


def test_build_budget_gate_skips_oversized_volume(monkeypatch, tmp_path):
    """Issue#129 评审 I1：预估建流超预算的卷**不接管**，逐页回落旧线。

    背景：整卷建流在**首个请求里同步跑完**，页数过多会让该请求超过网关
    `proxy_read_timeout`（`frontend/nginx.conf.template`）而 504，且这条路径
    不经过任何回落。判别力：预算调小后 `run_document_ner` 必须**一次都不被调用**、
    本页返回 `(None, None)`；闸不存在时本用例会因模型被调用而失败。
    """
    source.reset_memo()
    path = tmp_path / "v.pdf"
    path.write_bytes(b"%PDF-1.4\n")
    calls: list[int] = []
    # 每页必须**过得了层质量门**（否则会在闸之前就 return None，让本用例失去判别力）
    pages = [_page(i, ["张三", *[f"证据材料第{j}页" for j in range(20)]]) for i in range(1, 101)]

    monkeypatch.setattr(settings, "TEXT_LAYER_DOCLINE_ENABLED", True)
    monkeypatch.setattr(settings, "TEXT_LAYER_DOCLINE_MAX_BUILD_SEC", 10.0)
    monkeypatch.setattr(settings, "TEXT_LAYER_DOCLINE_BUILD_SEC_PER_PAGE", 1.0)
    monkeypatch.setattr(source.docline, "extract_text_layer_pages", lambda *a, **k: pages)
    monkeypatch.setattr(source.docline, "sample_ink_alignment", lambda *a, **k: 0.18)

    async def fake_run(*_a, **_k):
        calls.append(1)
        raise AssertionError("超预算的卷不该调用模型")

    monkeypatch.setattr(source.docline, "run_document_ner", fake_run)

    boxes, memo = asyncio.run(
        source.boxes_for_page(
            file_id="c" * 8, snapshot=_snapshot(str(path)), page=1, ocr_has_types=None
        )
    )

    assert boxes is None and memo is None, "超预算 ⇒ 整卷不接管，调用方回落旧路径"
    assert calls == [], "预算闸必须在建流之前短路"


def test_boxes_for_page_falls_back_on_page_without_layer(monkeypatch, tmp_path):
    """**混合卷**（部分页无文本层）：无层页必须返回 None 让调用方回落旧路径。

    若返回空列表，调用方会认为「文档级链说这页 0 个实体」⇒ 该页**静默丢检**，
    正是验收 D8 要挡住的失败模式。
    """
    monkeypatch.setattr(settings, "TEXT_LAYER_DOCLINE_ENABLED", True)
    path = tmp_path / "mixed.pdf"
    path.write_bytes(b"%PDF-1.4 stub")
    source.reset_memo()

    def dense(who: str) -> list[str]:
        return [who, *[f"证据材料第{i}页正文内容" for i in range(29)]]
    pages = [
        _page(1, dense("张三")),
        _page(2, dense("李四")),
        _page(3, []),  # 云上实测形态：extract 会把无文本层的页也返回，words 为空
    ]

    async def fake_run(page_list, labels, *, has_client, **kwargs):
        return docline.DocLineResult(
            pages=3, chunks=1, chunk_chars=2000, stream_chars=10, layer_chars=8, covered_chars=10,
            entities=[docline.DocLineEntity(1, "PERSON", "张三", (0.1, 0.2, 0.3, 0.4))],
            ner_calls=1, truncated_chunks=0, http_errors=0, residual_calls=0, seconds=0.01,
        )

    monkeypatch.setattr(source.docline, "extract_text_layer_pages", lambda *a, **k: pages)
    monkeypatch.setattr(source.docline, "sample_ink_alignment", lambda *a, **k: 0.18)
    monkeypatch.setattr(source.docline, "run_document_ner", fake_run)

    covered, memo = asyncio.run(
        source.boxes_for_page(file_id="b" * 8, snapshot=_snapshot(str(path)), page=1, ocr_has_types=None)
    )
    assert [b.text for b in covered] == ["张三"]
    assert memo is not None

    # 第 3 页 words 为空（无文本层）⇒ 必须回落（None），而不是空列表
    uncovered, memo3 = asyncio.run(
        source.boxes_for_page(file_id="b" * 8, snapshot=_snapshot(str(path)), page=3, ocr_has_types=None)
    )
    assert uncovered is None, "无层页必须回落旧路径，不能返回空列表造成静默丢检"
    assert memo3 is None


def test_label_to_type_id_maps_model_buckets_back_to_canonical_ids():
    """云上首轮真实事故：模型返回的是**中文标签桶**，直接当 type 会让下游按 id
    过滤全部落空（实测召回 0/121）。此用例钉住反向映射。"""
    from app.services.vision.has_text_payload import build_label_to_type_id

    mapping = build_label_to_type_id(None)
    assert mapping.get("姓名") == "PERSON"
    assert mapping.get("身份证号") == "ID_CARD"

    boxes = source._to_boxes(
        [docline.DocLineEntity(1, "姓名", "张三", (0.1, 0.2, 0.3, 0.4))], 1, mapping
    )
    assert [b.type for b in boxes] == ["PERSON"]

    # 查不到的标签保留原样（识别出来是啥就是啥），不丢
    unknown = source._to_boxes(
        [docline.DocLineEntity(1, "某种新类型", "x", (0.1, 0.2, 0.3, 0.4))], 1, mapping
    )
    assert [b.type for b in unknown] == ["某种新类型"]


def test_boxes_for_page_swallows_errors_and_falls_back(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "TEXT_LAYER_DOCLINE_ENABLED", True)
    path = tmp_path / "scan.pdf"
    path.write_bytes(b"%PDF-1.4 stub")
    source.reset_memo()

    def boom(*_args, **_kwargs):
        raise RuntimeError("text layer exploded")

    monkeypatch.setattr(source.docline, "extract_text_layer_pages", boom)
    boxes, memo = asyncio.run(source.boxes_for_page(file_id="b" * 8, snapshot=_snapshot(str(path)), page=1, ocr_has_types=None))
    assert boxes is None and memo is None, "异常必须退回旧路径而不是抛出"


def test_boxes_for_page_marks_unusable_layer(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "TEXT_LAYER_DOCLINE_ENABLED", True)
    path = tmp_path / "scan.pdf"
    path.write_bytes(b"%PDF-1.4 stub")
    source.reset_memo()
    monkeypatch.setattr(source.docline, "extract_text_layer_pages", lambda *a, **k: [_page(1, ["\ufffd\u0001"])])
    monkeypatch.setattr(source.docline, "sample_ink_alignment", lambda *a, **k: 0.0)
    boxes, memo = asyncio.run(source.boxes_for_page(file_id="c" * 8, snapshot=_snapshot(str(path)), page=1, ocr_has_types=None))
    assert boxes is None and memo is None, "不可用层必须回落"

# ------------------------------------------------ Issue#115 门⑥ 评审回归（C/I 类）


def _dense(who: str) -> list[str]:
    return [who, *[f"证据材料第{i}页正文内容" for i in range(29)]]


def test_find_value_spans_rejects_match_inside_longer_word():
    """评审 I-4：`张三` 不得匹配进 `张三丰` 内部（否则框会盖住半个词）。

    旧实现用裸 `str.find`，会把 `张三` 匹配到 `张三丰` 里的前两个字，产出
    「位置错 + 范围盖住不该涂的字」的框。
    """
    pages = [_page(1, ["甲方", "张三", "乙方张三丰"])]
    stream, word_spans = docline.build_stream(pages)
    normalized, index_map = docline._normalized_stream(stream)
    starts = {span.start for span in word_spans}
    ends = {span.end for span in word_spans}

    bounded = docline.find_value_spans(
        normalized, index_map, "张三", word_starts=starts, word_ends=ends, stream=stream
    )
    assert len(bounded) == 1, f"只应命中独立成词的那一处，实际 {bounded}"

    # 去掉边界校验（旧行为）会多命中一次——证明这个校验确实在起作用
    unbounded = docline.find_value_spans(normalized, index_map, "张三")
    assert len(unbounded) == 2, "旧行为（无边界校验）会额外匹配进「张三丰」"


def test_map_values_keeps_same_column_repeats():
    """评审 I-4：同一页、同一列、同 x0 的两个不同出现必须各出一个框。

    旧去重键是 (页, 类型, 值, int(x0*10000))，不含 y ⇒ 第二处被静默丢弃。
    """
    words = [
        docline.TextLayerWord(50.0, 700.0, 90.0, 720.0, "张三"),
        docline.TextLayerWord(50.0, 600.0, 90.0, 620.0, "张三"),
    ]
    pages = [docline.TextLayerPage(no=1, width=595.0, height=842.0, words=words)]
    stream, word_spans = docline.build_stream(pages)
    entities, failures = docline.map_values(pages, stream, word_spans, {"PERSON": {"张三"}})
    assert [e.value for e in entities] == ["张三", "张三"], "两处出现必须都保留"
    assert entities[0].bbox != entities[1].bbox
    assert failures == []


def test_covered_page_with_zero_boxes_falls_back(monkeypatch, tmp_path):
    """评审 C-1：覆盖页产出 0 框时不得清空 OCR 类型，必须回落旧路径。"""
    monkeypatch.setattr(settings, "TEXT_LAYER_DOCLINE_ENABLED", True)
    path = tmp_path / "scan.pdf"
    path.write_bytes(b"%PDF-1.4 stub")
    source.reset_memo()

    pages = [_page(1, _dense("张三")), _page(2, _dense("李四"))]

    async def fake_run(page_list, labels, *, has_client, **kwargs):
        return docline.DocLineResult(
            pages=2, chunks=1, chunk_chars=2000, stream_chars=10, layer_chars=8, covered_chars=10,
            entities=[docline.DocLineEntity(1, "PERSON", "张三", (0.1, 0.2, 0.3, 0.4))],
            ner_calls=1, truncated_chunks=0, http_errors=0, residual_calls=0, seconds=0.01,
        )

    monkeypatch.setattr(source.docline, "extract_text_layer_pages", lambda *a, **k: pages)
    monkeypatch.setattr(source.docline, "sample_ink_alignment", lambda *a, **k: 0.18)
    monkeypatch.setattr(source.docline, "run_document_ner", fake_run)

    ok, _ = asyncio.run(
        source.boxes_for_page(file_id="z" * 8, snapshot=_snapshot(str(path)), page=1, ocr_has_types=None)
    )
    assert [b.text for b in ok] == ["张三"]

    empty, memo2 = asyncio.run(
        source.boxes_for_page(file_id="z" * 8, snapshot=_snapshot(str(path)), page=2, ocr_has_types=None)
    )
    assert empty is None, "0 框的覆盖页必须回落，不能被当成「这页没实体」"
    assert memo2 is not None


def test_thin_page_is_not_covered(monkeypatch, tmp_path):
    """评审 C-2：页级下限——薄挂层页不能被卷级平均「救」进覆盖集。"""
    monkeypatch.setattr(settings, "TEXT_LAYER_DOCLINE_ENABLED", True)
    monkeypatch.setattr(settings, "TEXT_LAYER_PAGE_MIN_CHARS", 60)
    # 卷级门调低到 60，把本用例**隔离在页级门**上（卷级平均本来就有独立用例）
    monkeypatch.setattr(settings, "TEXT_LAYER_MIN_CHARS_PER_PAGE", 60)
    path = tmp_path / "mixed.pdf"
    path.write_bytes(b"%PDF-1.4 stub")
    source.reset_memo()

    pages = [
        _page(1, _dense("张三")),          # 厚层页
        _page(2, ["第2页"]),                # 薄挂层页：只有 3 个字（云上实测形态）
        _page(3, []),                      # 完全无层
    ]

    async def fake_run(page_list, labels, *, has_client, **kwargs):
        return docline.DocLineResult(
            pages=3, chunks=1, chunk_chars=2000, stream_chars=10, layer_chars=8, covered_chars=10,
            entities=[docline.DocLineEntity(1, "PERSON", "张三", (0.1, 0.2, 0.3, 0.4)),
                      docline.DocLineEntity(2, "PERSON", "第2页", (0.1, 0.2, 0.3, 0.4))],
            ner_calls=1, truncated_chunks=0, http_errors=0, residual_calls=0, seconds=0.01,
        )

    monkeypatch.setattr(source.docline, "extract_text_layer_pages", lambda *a, **k: pages)
    monkeypatch.setattr(source.docline, "sample_ink_alignment", lambda *a, **k: 0.18)
    monkeypatch.setattr(source.docline, "run_document_ner", fake_run)

    thick, memo = asyncio.run(
        source.boxes_for_page(file_id="t" * 8, snapshot=_snapshot(str(path)), page=1, ocr_has_types=None)
    )
    assert memo["covered_pages"] == {1}, f"只有厚层页算覆盖，实际 {memo['covered_pages']}"
    assert memo["uncovered_pages"] == [2, 3]

    thin, _ = asyncio.run(
        source.boxes_for_page(file_id="t" * 8, snapshot=_snapshot(str(path)), page=2, ocr_has_types=None)
    )
    assert thin is None, "薄挂层页必须回落旧路径（该页的图像内容只能靠 OCR 拿到）"


def test_regex_guarantee_layer_runs_on_document_stream(monkeypatch):
    """评审 I-3：regex 保证层必须在文档级链上补跑（旧链有 regex 回落）。

    否则配置了正则类型的租户在覆盖页上会丢掉这些类型（静默漏检）。

    冷审 C1 后本链改走 `safe_compile` / `safe_finditer`（全卷流上也有预算），
    故这里挂 in-process 替身——语义断言不变，进程池由专项用例覆盖。
    """
    monkeypatch.setattr(docline, "safe_compile", _inproc_safe_compile)
    monkeypatch.setattr(docline, "safe_finditer", _inproc_safe_finditer)
    pages = [_page(1, ["案号", "（2022）京0101刑初123号", "姓名", "张三"])]

    class Client:
        def ner(self, text, entity_types=None, **kwargs):
            return {}

    result = asyncio.run(
        docline.run_document_ner(
            pages,
            ["姓名"],
            has_client=Client(),
            regex_types=[("CASE_NUMBER", r"刑初\d{1,6}号")],
        )
    )
    types = {e.type for e in result.entities}
    assert "CASE_NUMBER" in types, f"regex 类型必须被补进结果，实际 {types}"
    cn = [e for e in result.entities if e.type == "CASE_NUMBER"]
    assert cn and "刑初123号" in cn[0].value
    # 框必须收紧到命中的那几个字（而不是整词涂掉）
    assert (cn[0].bbox[2] - cn[0].bbox[0]) < 0.5, f"框被插值收紧了才对，实际 {cn[0].bbox}"


def test_docline_regex_layer_uses_named_group_semantics(monkeypatch):
    """冷审 C1 附带：第三条 runner 与文本链/vision **同粒度**。

    旧实现取 `m.group(0).strip()`：出生语境会把「出生日期：」一起当成命中值。
    同源后命名组决定命中值 ⇒ 语境词/分隔符不入，与 Issue#52 文本链一致。
    """
    monkeypatch.setattr(docline, "safe_compile", _inproc_safe_compile)
    monkeypatch.setattr(docline, "safe_finditer", _inproc_safe_finditer)
    pages = [_page(1, ["出生日期：1992年10月5日"])]
    pattern = r"出生日期：?(?P<birth_date>\d{4}年\d{1,2}月\d{1,2}日)"

    result = asyncio.run(
        docline.run_document_ner(
            pages, [], has_client=_NullClient(), regex_types=[("BIRTH_DATE", pattern)]
        )
    )
    values = sorted(e.value for e in result.entities if e.type == "BIRTH_DATE")
    assert values == ["1992年10月5日"], f"语境词不得进命中值，实际 {values}"

    # 区间同理：拆首尾两项、分隔符不入（pattern 落在单个词内，避免词分隔符干扰）
    interval_pages = [_page(1, ["本案于2022年1月1日至1月31日期间审理"])]
    interval_pattern = r"(?P<seg_a>\d{4}年\d{1,2}月\d{1,2}日)至(?P<seg_b>\d{1,2}月\d{1,2}日)"
    interval = asyncio.run(
        docline.run_document_ner(
            interval_pages, [], has_client=_NullClient(), regex_types=[("DATE", interval_pattern)]
        )
    )
    dates = sorted(e.value for e in interval.entities if e.type == "DATE")
    assert dates == ["1月31日", "2022年1月1日"], f"区间必须拆两项且不含「至」，实际 {dates}"


def test_docline_regex_layer_budget_scales_and_timeout_is_survivable(monkeypatch, caplog):
    """冷审 C1：全卷流上必须有预算，且超时只告警不打断整卷（旧实现裸 finditer）。"""
    captured: dict = {}

    def capturing_finditer(compiled, text, timeout=5.0):
        captured["text"] = text
        captured["timeout"] = timeout
        return []

    monkeypatch.setattr(docline, "safe_compile", _inproc_safe_compile)
    monkeypatch.setattr(docline, "safe_finditer", capturing_finditer)
    pages = [_page(1, ["案号", "（2022）京0101刑初123号"])]

    result = asyncio.run(
        docline.run_document_ner(
            pages, [], has_client=_NullClient(), regex_types=[("CASE_NUMBER", r"刑初\d{1,6}号")]
        )
    )
    assert captured["timeout"] == scaled_timeout(captured["text"]) == 2.0, "短流取 base 预算"
    assert captured["text"] == "".join(w.text + docline._WORD_SEP for w in pages[0].words)
    assert not [e for e in result.entities if e.type == "CASE_NUMBER"]

    def exploding_finditer(compiled, text, timeout=5.0):
        raise RegexTimeoutError("Regex finditer timed out")

    monkeypatch.setattr(docline, "safe_finditer", exploding_finditer)
    with caplog.at_level(logging.WARNING, logger="app.services.text_layer_docline_service"):
        timed_out = asyncio.run(
            docline.run_document_ner(
                pages, [], has_client=_NullClient(), regex_types=[("CASE_NUMBER", r"刑初\d{1,6}号")]
            )
        )

    assert not [e for e in timed_out.entities if e.type == "CASE_NUMBER"]
    message = next(
        r.getMessage() for r in caplog.records if "SEARCH TIMEOUT" in r.getMessage()
    )
    assert "CASE_NUMBER" in message
    assert "chars=" in message and "budget=" in message


def test_boxes_for_page_passes_tenant_regex_types_into_build(monkeypatch, tmp_path):
    """接入层必须把租户 regex 类型传进整卷构建（而不是整卷禁用）。"""
    monkeypatch.setattr(settings, "TEXT_LAYER_DOCLINE_ENABLED", True)
    path = tmp_path / "scan.pdf"
    path.write_bytes(b"%PDF-1.4 stub")
    source.reset_memo()
    seen: list = []

    monkeypatch.setattr(source, "_regex_types", lambda owner: [("CASE_NUMBER", r"X\d+")])

    async def fake_run(page_list, labels, *, has_client, regex_types=None, **kwargs):
        seen.append(regex_types)
        return docline.DocLineResult(
            pages=1, chunks=1, chunk_chars=2000, stream_chars=10, layer_chars=8, covered_chars=10,
            entities=[docline.DocLineEntity(1, "PERSON", "张三", (0.1, 0.2, 0.3, 0.4))],
            ner_calls=1, truncated_chunks=0, http_errors=0, residual_calls=0, seconds=0.01,
        )

    monkeypatch.setattr(source.docline, "extract_text_layer_pages", lambda *a, **k: [_page(1, _dense("张三"))])
    monkeypatch.setattr(source.docline, "sample_ink_alignment", lambda *a, **k: 0.18)
    monkeypatch.setattr(source.docline, "run_document_ner", fake_run)

    boxes, _ = asyncio.run(
        source.boxes_for_page(
            file_id="g" * 8, snapshot=_snapshot(str(path)), page=1, ocr_has_types=None, owner_id=object()
        )
    )
    assert boxes is not None
    assert seen == [[("CASE_NUMBER", r"X\d+")]], f"regex 类型必须透传，实际 {seen}"


def test_block_errors_over_limit_fall_back_and_are_negative_cached(monkeypatch, tmp_path):
    """评审 I-5/I-6：块失败超阈 ⇒ 整卷回落；且不得每页重算整卷。"""
    monkeypatch.setattr(settings, "TEXT_LAYER_DOCLINE_ENABLED", True)
    monkeypatch.setattr(settings, "TEXT_LAYER_MAX_BLOCK_ERRORS", 0)
    path = tmp_path / "scan.pdf"
    path.write_bytes(b"%PDF-1.4 stub")
    source.reset_memo()
    builds: list[int] = []

    def fake_extract(*_a, **_k):
        builds.append(1)
        return [_page(1, _dense("张三")), _page(2, _dense("李四"))]

    async def fake_run(page_list, labels, *, has_client, **kwargs):
        return docline.DocLineResult(
            pages=2, chunks=2, chunk_chars=2000, stream_chars=10, layer_chars=8, covered_chars=10,
            entities=[], ner_calls=1, truncated_chunks=0, http_errors=1, residual_calls=0, seconds=0.01,
        )

    monkeypatch.setattr(source.docline, "extract_text_layer_pages", fake_extract)
    monkeypatch.setattr(source.docline, "sample_ink_alignment", lambda *a, **k: 0.18)
    monkeypatch.setattr(source.docline, "run_document_ner", fake_run)

    for page in (1, 2):
        boxes, memo = asyncio.run(
            source.boxes_for_page(file_id="e" * 8, snapshot=_snapshot(str(path)), page=page, ocr_has_types=None)
        )
        assert boxes is None and memo is None, "块失败超阈必须整卷回落"
    assert len(builds) == 1, "临时故障要负缓存，不能每页重算整卷"


def test_document_ner_dispatches_through_global_gpu_gate(monkeypatch):
    """评审 I-5：块级派发必须经过全局 GPU 闸门，不得自建信号量绕开。"""
    import contextlib

    pages = [_page(1, ["甲：", "张三", "乙：", "李四"])]
    entered: list[str] = []

    @contextlib.asynccontextmanager
    async def fake_slot(label):
        entered.append(label)
        yield

    class Client:
        def ner(self, text, entity_types=None, **kwargs):
            return {}

    monkeypatch.setattr(docline, "shared_gpu_inference_slot", fake_slot)
    asyncio.run(docline.run_document_ner(pages, ["姓名"], has_client=Client()))

    assert entered, "块级调用必须取 shared_gpu_inference_slot（全局闸位）"
    assert any("TextLayerDocLine" in label for label in entered)


# ------------------------------------------------- 门⑥ 二轮修复的回归（C1/C2/I5）


def test_map_values_maps_subword_values_inside_longer_words():
    """门⑥ 二轮 Critical#2：值常是**词内子串**（`被告人张三丰`）。

    旧链 `match_entities_to_ocr` 支持 sub-word positioning；新链若要求「整词相等」，
    这四种形态全部 0 框且不告警 ⇒ 比旧链更窄的静默漏检。
    """
    pages = [_page(1, [("被告人张三丰被告人张三丰被告人张三丰被告人张三丰被告人张三丰被告人张三丰被告人张三丰被告人张三丰被告人张三丰被告人张三丰被告人张三丰被告人张三丰被告人张三丰被告人张三丰被告人张三丰被告人张三丰被告人张三丰被告人张三丰被告人张三丰被告人张三丰被告人张三丰被告人张三丰被告人张三丰被告人张三丰被告人张三丰被告人张三丰被告人张三丰被告人张三丰被告人张三丰被告人张三丰"), "电话13612345678", "编号（2022）京0101刑初123号"])]
    stream, spans = docline.build_stream(pages)
    entities, failures = docline.map_values(
        pages, stream, spans, {"姓名": {"张三丰"}, "手机号": {"13612345678"}}
    )
    got = {e.value: e for e in entities}
    assert set(got) == {"张三丰", "13612345678"}, f"词内子串必须落框：{failures}"
    assert failures == []
    whole_word_width = 40.0 / 595.0
    for entity in entities:
        x0, _y0, x1, _y1 = entity.bbox
        assert 0.0 < x0 < x1 <= 1.0
        # 框必须**收紧到命中字符**；若退化成整词，中文长「词」会被整块涂黑
        assert (x1 - x0) < whole_word_width + 1e-9
    assert got["张三丰"].bbox[0] > 0.0, "词内命中必须带非零起点（证明插值生效）"


def test_map_values_rejects_value_spanning_two_pages():
    """门⑥ 二轮 Critical#2 之二：页间分隔符与词间同为空格，跨页会拼出假实体。"""
    pages = [_page(1, ["甲方张三"]), _page(2, ["丰住本市"])]
    stream, spans = docline.build_stream(pages)
    entities, failures = docline.map_values(pages, stream, spans, {"姓名": {"张三丰"}})
    assert entities == []
    assert [f["reason"] for f in failures] == ["cross_page"]


def test_docline_result_map_failure_ratio_is_defined():
    result = docline.DocLineResult(
        pages=1, chunks=1, chunk_chars=2_000, stream_chars=10, layer_chars=10,
        covered_chars=10, entities=[], ner_calls=1, residual_calls=0,
        truncated_chunks=0, http_errors=0, seconds=0.1,
    )
    assert result.map_failure_ratio == 0.0
    result.map_failures.append({"type": "t", "value": "v", "reason": "not_found_in_stream"})
    assert result.map_failure_ratio == 1.0


def test_coverage_drops_when_a_chunk_fails(monkeypatch):
    """门⑥ 二轮 Important#5：覆盖率必须**能失败**。

    旧写法对**全部**块求并集，而块首尾相接+重叠 ⇒ 并集恒等于整条流 ⇒
    `coverage_ratio` 结构上恒为 1.0。改为只统计成功块后，失败块会如实压低它。
    """
    pages = [_page(1, [f"第{i}号文件材料" for i in range(40)])]
    state = {"n": 0}

    class _FakeHas:
        def ner(self, _text: str, _labels: list[str]) -> dict[str, list[str]]:
            state["n"] += 1
            if state["n"] == 2:
                raise RuntimeError("boom")
            return {"姓名": ["第1号文件材料"]}

    result = asyncio.run(
        docline.run_document_ner(
            pages, ["姓名"], has_client=_FakeHas(), chunk_chars=60, overlap=0
        )
    )
    assert result.chunks > 1, "样本必须真的分成多块，否则本用例无判别力"
    assert result.failed_chunks >= 1
    assert result.coverage_ratio < 1.0


def test_empty_ocr_has_types_skips_whole_chain(monkeypatch, tmp_path):
    """门⑥ 二轮 Critical#1：用户明确「不跑 HaS」（空类型表）时整条链必须跳过。

    `has_text_payload` 的 `if vision_types else 默认` 会把空列表当 falsy 而回落到
    38 类默认表 ⇒ 本链会替用户跑满 38 类，且账号停用的类型在此复活。
    """
    monkeypatch.setattr(settings, "TEXT_LAYER_DOCLINE_ENABLED", True)
    built: list[int] = []

    async def _fake_build(*_a, **_k):
        built.append(1)
        return None

    monkeypatch.setattr(source, "_build", _fake_build)
    path = tmp_path / "a.pdf"
    path.write_bytes(b"%PDF-1.4\n")

    boxes, memo = asyncio.run(
        source.boxes_for_page(
            file_id="f" * 8, snapshot=_snapshot(str(path)), page=1, ocr_has_types=[]
        )
    )
    assert boxes is None and memo is None
    assert built == [], "空类型表不得触发建卷（否则默认 38 类会复活）"


def _person_type():
    """门⑥ 四轮 Important#1：类型表必须传**带 .id 的对象**。

    传裸字符串 `["PERSON"]` 时 `_build_has_text_type_names` 推出的标签表为空，
    `boxes_for_page` 会在 `if not labels: return None, None` 提前返回，`_build`
    根本不执行 ⇒ 那些「卷级门可失败」用例是恒真断言（实测 `_build` 调用次数=0）。
    """
    from app.services.pipeline_service import PRESET_OCR_HAS_TYPES

    return next(item for item in PRESET_OCR_HAS_TYPES if item.id == "PERSON")

def test_volume_level_gates_are_fail_able(monkeypatch, tmp_path):
    """门⑥ 二轮 Important#5：卷级门（失败块/回映失败率/覆盖率）必须真能挡住建卷。"""
    monkeypatch.setattr(settings, "TEXT_LAYER_DOCLINE_ENABLED", True)
    monkeypatch.setattr(source.docline, "extract_text_layer_pages", lambda *_a, **_k: [
        docline.TextLayerPage(
            no=1, width=595.0, height=842.0,
            words=[docline.TextLayerWord(50.0, 700.0, 90.0, 720.0, ("被告人张三丰被告人张三丰被告人张三丰被告人张三丰被告人张三丰被告人张三丰被告人张三丰被告人张三丰被告人张三丰被告人张三丰被告人张三丰被告人张三丰被告人张三丰被告人张三丰被告人张三丰被告人张三丰被告人张三丰被告人张三丰被告人张三丰被告人张三丰被告人张三丰被告人张三丰被告人张三丰被告人张三丰被告人张三丰被告人张三丰被告人张三丰被告人张三丰被告人张三丰被告人张三丰"))],
        )
    ])
    monkeypatch.setattr(source.docline, "sample_ink_alignment", lambda *_a, **_k: 0.18)

    async def _bad_coverage(*_a, **_k):
        return docline.DocLineResult(
            pages=1, chunks=2, chunk_chars=2_000, stream_chars=100, layer_chars=100,
            covered_chars=40, entities=[], ner_calls=2, residual_calls=0,
            truncated_chunks=0, http_errors=0, seconds=0.1, failed_chunks=1,
            map_failures=[],
        )

    monkeypatch.setattr(source.docline, "run_document_ner", _bad_coverage)
    path = tmp_path / "b.pdf"
    path.write_bytes(b"%PDF-1.4\n")
    boxes, memo = asyncio.run(
        source.boxes_for_page(
            file_id="g" * 8, snapshot=_snapshot(str(path)), page=1, ocr_has_types=[_person_type()]
        )
    )
    assert boxes is None and memo is None, "失败块必须让整卷回落旧路径"

    async def _low_coverage(*_a, **_k):
        return docline.DocLineResult(
            pages=1, chunks=2, chunk_chars=2_000, stream_chars=100, layer_chars=100,
            covered_chars=40, entities=[], ner_calls=2, residual_calls=0,
            truncated_chunks=0, http_errors=0, seconds=0.1, failed_chunks=0,
            map_failures=[],
        )

    monkeypatch.setattr(source.docline, "run_document_ner", _low_coverage)
    source.reset_memo()
    boxes, memo = asyncio.run(
        source.boxes_for_page(
            file_id="h" * 8, snapshot=_snapshot(str(path)), page=1, ocr_has_types=[_person_type()]
        )
    )
    assert boxes is None and memo is None, "覆盖率 0.40 < 0.90 必须回落旧路径"


def test_map_values_keeps_both_word_and_embedded_hits() -> None:
    """门⑥ 第三轮 Critical：**不得**做「整词优先」的全文档收口。

    旧规则「只要该值在本文档里作为整词出现过，就只取整词命中」会把**贴标签**的
    合法命中一并毙掉（`张三` 在 `张三丰` 里 / 案号在 `编号（2022）…号` 里），
    且 `failures` 为空 ⇒ 那处保持明文却毫无信号。中文案卷的主流形态正是「标签紧贴值」。
    现在两侧都出框：整词那次出整词框，词内那次出**收紧到值本身**的框。
    """
    pages = [_page(1, ["张三", "张三丰"])]
    stream, spans = docline.build_stream(pages)
    entities, failures = docline.map_values(pages, stream, spans, {"姓名": {"张三"}})
    assert failures == []
    assert len(entities) == 2, "整词与词内命中都必须在框里（全文档收口已被删除）"
    whole = [e for e in entities if abs(e.bbox[0] - 50.0 / 595.0) < 1e-6]
    embedded = [e for e in entities if e not in whole]
    assert len(whole) == 1 and len(embedded) == 1
    assert abs((whole[0].bbox[2] - whole[0].bbox[0]) - 40.0 / 595.0) < 1e-6
    # 词内那次必须**收紧**到值宽（2 字），不能扩成整词「张三丰」（3 字）
    w = embedded[0].bbox[2] - embedded[0].bbox[0]
    assert w < 40.0 / 595.0, f"词内命中未收紧：{w:.4f}"


def test_map_values_keeps_label_prefixed_occurrence_when_value_also_standalone() -> None:
    """门⑥ 第三轮 Critical 的复现场景：同一值「贴标签」与「独立出现」并存。

    旧的全文档收口下，只要页 2 有独立整词命中，页 1 的 `编号（2022）京0101刑初123号`
    里那次就会被丢掉且 `failures` 为空 ⇒ 该处保持明文。此用例钉住两侧都出框。
    """
    case_no = "（2022）京0101刑初123号"
    pages = [_page(1, ["编号" + case_no]), _page(2, [case_no])]
    stream, spans = docline.build_stream(pages)
    entities, failures = docline.map_values(pages, stream, spans, {"案号": {case_no}})
    assert failures == []
    assert {e.page for e in entities} == {1, 2}, "贴标签那次被静默毙掉了"


def test_map_values_keeps_embedded_hit_in_same_page() -> None:
    """A2 变体：两种出现都在同一页时，旧收口同样只留一条。"""
    case_no = "（2022）京0101刑初123号"
    pages = [_page(1, ["编号" + case_no, case_no])]
    stream, spans = docline.build_stream(pages)
    entities, _failures = docline.map_values(pages, stream, spans, {"案号": {case_no}})
    assert len(entities) == 2


def test_fold_matches_normalized_stream_on_decomposed_marks() -> None:
    """门⑥ 三轮 Minor#5：带组合记号的姓名不得静默漏检（合成/分解两种写法都要命中）。"""
    import unicodedata

    pages = [_page(1, ["姓名 Jose\u0301 电话"])]
    stream, spans = docline.build_stream(pages)
    composed = unicodedata.normalize("NFC", "Jose\u0301")
    decomposed = unicodedata.normalize("NFD", composed)
    for probe in (composed, decomposed, "Jose"):
        entities, _failures = docline.map_values(pages, stream, spans, {"姓名": {probe}})
        assert entities, f"带组合记号的姓名漏检：{probe!r}"

def test_overlapping_word_spans_boundaries() -> None:
    """二分定位的边界：词首在区间之前但交叠要收；端点相切（不交叠）要排除。"""
    pages = [_page(1, ["前面", "张三挂号", "后面"])]
    stream, spans = docline.build_stream(pages)
    starts = [s.start for s in spans]
    # 命中「三挂」：落在第二个词内部，且该词的 start 小于命中起点
    target = stream.index("三挂")
    hit = docline._overlapping_word_spans(target, target + 2, spans, starts)
    assert len(hit) == 1, hit
    assert hit[0].word.text == "张三挂号"
    # 端点相切：起点正好等于某词 end 的区间不该收进那个词
    touching = docline._overlapping_word_spans(spans[1].end, spans[1].end + 1, spans, starts)
    assert all(s.word.text != "张三挂号" for s in touching), touching


def test_map_values_word_lookup_is_sublinear_in_document_size() -> None:
    """规模回归（P1 189 页实测）：命中区间 → 词的定位**不能**对每个命中重扫全部词。

    用**确定性的读取次数**断言，不用耗时（#109 的教训：计时断言会 flaky）。
    词表换成计数列表：修复前每命中重扫全表 ⇒ 命中数 × 词数（本用例 ≈4×10^8 次）；
    修复后二分 ⇒ 词数 + 命中数 × log(词数)。
    """
    class CountingSpans(list):
        def __init__(self, items):
            super().__init__(items)
            self.reads = 0

        def __getitem__(self, index):
            self.reads += 1
            return super().__getitem__(index)

        def __iter__(self):
            for item in super().__iter__():
                self.reads += 1
                yield item

    n_words, n_values = 20000, 200
    pages = [_page(1, [f"值{i % n_values}号" for i in range(n_words)])]
    stream, spans = docline.build_stream(pages)
    counting = CountingSpans(spans)
    values = {"T": {f"值{i}号" for i in range(n_values)}}

    entities, _failures = docline.map_values(pages, stream, counting, values)

    assert len(entities) == n_words
    bound = len(spans) * 2 + n_words * 64
    assert counting.reads < bound, (
        f"词表被读取 {counting.reads} 次（上限 {bound}）⇒ 命中定位退化成了重扫全部词"
    )


def test_truncated_ratio_gate_falls_back(monkeypatch, tmp_path):
    """门⑥ 四轮 Important#1：截断过半必须让整卷回落，且阈值出自配置（双向验证）。"""
    monkeypatch.setattr(settings, "TEXT_LAYER_DOCLINE_ENABLED", True)
    monkeypatch.setattr(source.docline, "extract_text_layer_pages", lambda *_a, **_k: [
        docline.TextLayerPage(
            no=1, width=595.0, height=842.0,
            words=[docline.TextLayerWord(50.0, 700.0, 90.0, 720.0, ("被告人张三丰被告人张三丰被告人张三丰被告人张三丰被告人张三丰被告人张三丰被告人张三丰被告人张三丰被告人张三丰被告人张三丰被告人张三丰被告人张三丰被告人张三丰被告人张三丰被告人张三丰被告人张三丰被告人张三丰被告人张三丰被告人张三丰被告人张三丰被告人张三丰被告人张三丰被告人张三丰被告人张三丰被告人张三丰被告人张三丰被告人张三丰被告人张三丰被告人张三丰被告人张三丰"))],
        )
    ])
    monkeypatch.setattr(source.docline, "sample_ink_alignment", lambda *_a, **_k: 0.18)

    async def _truncated(*_a, **_k):
        return docline.DocLineResult(
            pages=1, chunks=10, chunk_chars=2_000, stream_chars=100, layer_chars=100,
            covered_chars=100, entities=[], ner_calls=20, residual_calls=0,
            truncated_chunks=6, http_errors=0, seconds=0.1, failed_chunks=0,
            map_failures=[],
        )

    monkeypatch.setattr(source.docline, "run_document_ner", _truncated)
    path = tmp_path / "t.pdf"
    path.write_bytes(b"%PDF-1.4\n")
    monkeypatch.setattr(settings, "TEXT_LAYER_MAX_TRUNCATED_RATIO", 0.50)
    boxes, memo = asyncio.run(
        source.boxes_for_page(
            file_id="j" * 8, snapshot=_snapshot(str(path)), page=1,
            ocr_has_types=[_person_type()],
        )
    )
    assert boxes is None and memo is None, "6/10 截断超过 0.50 必须回落旧路径"

    # 阈值放宽到 0.90 ⇒ 同一输入必须能接管（证明门真的读配置而不是硬编码）
    monkeypatch.setattr(settings, "TEXT_LAYER_MAX_TRUNCATED_RATIO", 0.90)
    source.reset_memo()
    boxes2, memo2 = asyncio.run(
        source.boxes_for_page(
            file_id="j" * 8, snapshot=_snapshot(str(path)), page=1,
            ocr_has_types=[_person_type()],
        )
    )
    assert memo2 is not None and memo2.get("truncated_chunks") == 6, "放宽阈值后应接管并记录截断数"
