"""Issue#115 文档级文本层链的单测（C1 分类器 / C2 服务 / C4 接入）。

判别力说明（对应验收方案 v2 §5.0 自检）：
- 这些用例在**未实现本功能**的树上是 import error / 断言失败 —— 不是恒真条；
- 「分类器不平凡」用**成对**样本钉住（同长度、好层 USABLE vs 坏层 SUSPECT/UNUSABLE）；
- 「接入不静默」用桩把异常注入 `run_document_ner`，断言**退回旧路径且不抛**。
"""
from __future__ import annotations

import asyncio

import pytest

from app.core.config import settings
from app.services import text_layer_doc_source as source
from app.services import text_layer_docline_service as docline
from app.services import text_layer_quality as quality


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


def test_boxes_for_page_falls_back_on_page_without_layer(monkeypatch, tmp_path):
    """**混合卷**（部分页无文本层）：无层页必须返回 None 让调用方回落旧路径。

    若返回空列表，调用方会认为「文档级链说这页 0 个实体」⇒ 该页**静默丢检**，
    正是验收 D8 要挡住的失败模式。
    """
    monkeypatch.setattr(settings, "TEXT_LAYER_DOCLINE_ENABLED", True)
    path = tmp_path / "mixed.pdf"
    path.write_bytes(b"%PDF-1.4 stub")
    source.reset_memo()

    dense = lambda who: [who, *[f"证据材料第{i}页正文内容" for i in range(29)]]
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