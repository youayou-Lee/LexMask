"""Issue#302：正则保证层必须与任务级识别项勾选求交。

根因：`_apply_regex_fallback`（OCR 图像线 + 文本层线）与 docline 链的
`text_layer_doc_source._regex_types` 都只看 `get_regex_types`（账号级启用 +
带 regex_pattern），与本次任务勾选无关 —— 未勾选 DATE 仍被 DATE 正则打码。

语义定稿（Issue#302）：「勾什么识别什么」对正则保证层同样成立——
- 传了勾选集（set）⇒ 正则类型与勾选集求交后再跑；
- 勾选未传（None，内部调用方/默认路径）⇒ 保持旧行为（全部启用正则类型照跑）；
- 账号级停用仍是上限（get_regex_types 已过滤 enabled=False，本层只再收窄）。

测试基建沿用 test_issue64_65_regex_fallback 的替身池口径（不依赖真实进程池）。
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from types import SimpleNamespace

from test_issue43_case_number_floor import _block_with_chars

from app.core.config import settings
from app.core.safe_regex import _finditer_many_in_process, _MatchProxy
from app.services import entity_type_service as ets
from app.services import ocr_has_vision_service as vision
from app.services import text_layer_doc_source as source
from app.services import text_layer_docline_service as docline

_PRESET = json.loads(
    (Path(__file__).resolve().parents[1] / "config" / "preset_entity_types.json").read_text("utf-8")
)


class _Type:
    def __init__(self, id: str, regex_pattern: str | None = None):
        self.id = id
        self.regex_pattern = regex_pattern
        self.enabled = True
        self.use_llm = True
        self.name = id


def _type(type_id: str) -> _Type:
    return _Type(type_id, (_PRESET.get(type_id) or {}).get("regex_pattern"))


def _inproc_safe_compile(pattern, timeout=2.0):
    """替身 `safe_compile`：只做语法编译，不跑子进程探针。"""
    return re.compile(pattern, re.IGNORECASE)


def _inproc_finditer_many(compiled, texts, timeout=5.0):
    """替身 `safe_finditer_many`：复用真实序列化逻辑，只是不跨进程。"""
    rows = _finditer_many_in_process(compiled.pattern, compiled.flags, list(texts))
    return [[_MatchProxy(t, s, e, named) for t, s, e, named in r] for r in rows]


def _stub_fallback_env(monkeypatch, regex_type_ids: list[str]):
    """替身池口径：compile/finditer 计数 + 伪实体捕获，get_regex_types 可控。"""
    captured: list[dict] = []
    calls = {"compile": 0, "finditer": 0}

    def counting_compile(pattern, timeout=2.0):
        calls["compile"] += 1
        return _inproc_safe_compile(pattern, timeout)

    def counting_finditer(compiled, texts, timeout=5.0):
        calls["finditer"] += 1
        return _inproc_finditer_many(compiled, texts, timeout)

    monkeypatch.setattr(vision, "safe_compile", counting_compile)
    monkeypatch.setattr(vision, "safe_finditer_many", counting_finditer)
    monkeypatch.setattr(
        ets, "get_regex_types",
        lambda owner_id=None: [_type(tid) for tid in regex_type_ids],
    )
    monkeypatch.setattr(
        "app.services.vision.ocr_pipeline.match_entities_to_ocr",
        lambda blocks, entities: captured.extend(entities) or [],
    )
    monkeypatch.setattr(
        "app.services.vision.ocr_entity_match.split_regions_across_lines",
        lambda regions, blocks: [],
    )
    return captured, calls


def _capture_fallback_calls(monkeypatch):
    """接线钉：捕获两入口传给 _apply_regex_fallback 的 selected_type_ids。"""
    seen: list[set[str] | None] = []

    def fake_fallback(self, ocr_blocks, width, height, *, selected_type_ids=None):
        seen.append(selected_type_ids)
        return []

    return seen, fake_fallback


# ── AC1：未勾选 DATE ⇒ 正则层不出 DATE ─────────────────────────


def test_regex_fallback_skips_unselected_types(monkeypatch):
    """勾选集不含 DATE/BIRTH_DATE ⇒ 这两个正则类型一次都不跑（AC1）。"""
    captured, calls = _stub_fallback_env(monkeypatch, ["DATE", "LICENSE_PLATE", "BIRTH_DATE"])
    svc = vision.OcrHasVisionService()

    regions = svc._apply_regex_fallback(
        [_block_with_chars("2022年1月1日 京A12345号牌")],
        120, 20,
        selected_type_ids={"GEN_NAME"},
    )

    assert regions == []
    assert captured == [], "未勾选的正则类型不得产出伪实体"
    assert calls == {"compile": 0, "finditer": 0}, "未勾选的正则类型连编译/检索都不该跑"


def test_regex_fallback_runs_selected_regex_type(monkeypatch):
    """勾选含 DATE ⇒ 正则兜底照常命中（AC2）。"""
    captured, calls = _stub_fallback_env(monkeypatch, ["DATE"])
    svc = vision.OcrHasVisionService()

    svc._apply_regex_fallback(
        [_block_with_chars("2022年1月1日")],
        120, 20,
        selected_type_ids={"DATE", "GEN_NAME"},
    )

    assert calls["compile"] == 1 and calls["finditer"] == 1
    assert any(e.get("type") == "DATE" for e in captured), "勾选的 DATE 必须仍由正则层补齐"


def test_regex_fallback_none_selection_keeps_all(monkeypatch):
    """勾选未传（None）⇒ 旧行为：全部启用正则类型照跑（AC4）。"""
    captured, calls = _stub_fallback_env(monkeypatch, ["DATE", "LICENSE_PLATE"])
    svc = vision.OcrHasVisionService()

    svc._apply_regex_fallback(
        [_block_with_chars("2022年1月1日 京A12345号牌")],
        120, 20,
        selected_type_ids=None,
    )

    assert calls["compile"] == 2 and calls["finditer"] == 2
    assert {e.get("type") for e in captured} == {"DATE", "LICENSE_PLATE"}


def test_regex_fallback_selection_intersects_not_replaces(monkeypatch):
    """求交而非替换：勾选集里没有 regex_pattern 的类型（如 GEN_NAME）不影响判断，
    勾选集超出启用正则类型时以启用集为准。"""
    captured, calls = _stub_fallback_env(monkeypatch, ["DATE"])
    svc = vision.OcrHasVisionService()

    svc._apply_regex_fallback(
        [_block_with_chars("2022年1月1日")],
        120, 20,
        selected_type_ids={"GEN_NAME", "DATE", "NOT_A_REAL_TYPE"},
    )

    assert calls["compile"] == 1
    assert any(e.get("type") == "DATE" for e in captured)


# ── 接线钉：两入口必须把勾选传进保证层 ──────────────────────────


def _vision_type(type_id: str):
    return SimpleNamespace(id=type_id, name=type_id)


def test_detect_from_text_blocks_passes_selection_to_fallback(monkeypatch):
    """文本层线接线钉：勾选不含 DATE ⇒ 传下去的 selected_type_ids 不含 DATE（AC1）。"""
    seen, fake_fallback = _capture_fallback_calls(monkeypatch)
    monkeypatch.setattr(vision.OcrHasVisionService, "_apply_regex_fallback", fake_fallback)

    async def fake_ner(self, blocks, types, duration_ms, **kwargs):
        return []

    monkeypatch.setattr(vision.OcrHasVisionService, "_invoke_has_text_analysis", fake_ner)
    svc = vision.OcrHasVisionService()
    blocks = [_block_with_chars("2022年1月1日 张三于本院立案")]

    asyncio.run(svc.detect_from_text_blocks(blocks, [_vision_type("GEN_NAME")]))

    assert seen == [{"GEN_NAME"}], "文本层线必须把勾选集原样传给保证层"

    seen.clear()
    asyncio.run(svc.detect_from_text_blocks(blocks, None))
    assert seen == [None], "勾选未传 ⇒ None（旧行为）"


def test_detect_and_draw_passes_selection_to_fallback(monkeypatch):
    """OCR 图像线接线钉：同上（AC1）。"""
    seen, fake_fallback = _capture_fallback_calls(monkeypatch)
    monkeypatch.setattr(vision.OcrHasVisionService, "_apply_regex_fallback", fake_fallback)

    def fake_paddle(self, image, **kwargs):
        return [_block_with_chars("2022年1月1日 张三")], []

    async def fake_ner(self, blocks, types, duration_ms, **kwargs):
        return []

    monkeypatch.setattr(vision.OcrHasVisionService, "_prepare_image",
                        lambda self, image_bytes: (None, 100, 100))
    monkeypatch.setattr(vision.OcrHasVisionService, "_run_paddle_ocr", fake_paddle)
    monkeypatch.setattr(vision.OcrHasVisionService, "_invoke_has_text_analysis", fake_ner)

    svc = vision.OcrHasVisionService()
    asyncio.run(svc.detect_and_draw(b"\x89PNG stub", vision_types=[_vision_type("GEN_NAME")], draw_result=False))

    assert seen == [{"GEN_NAME"}], "OCR 图像线必须把勾选集原样传给保证层"


# ── AC3：docline 链同样求交 ────────────────────────────────────


def _page(no: int, words: list[str], width: float = 595.0, height: float = 842.0) -> docline.TextLayerPage:
    boxes: list[docline.TextLayerWord] = []
    x = 50.0
    for word in words:
        boxes.append(docline.TextLayerWord(x, 700.0, x + 40.0, 720.0, word))
        x += 45.0
    return docline.TextLayerPage(no=no, width=width, height=height, words=boxes)


def test_docline_regex_types_intersects_with_selection(monkeypatch):
    """_regex_types 与 ocr_has_types 求交：未勾选 DATE ⇒ docline 保证层不含 DATE。"""
    monkeypatch.setattr(
        ets, "get_regex_types",
        lambda owner_id=None: [_type("DATE"), _type("LICENSE_PLATE")],
    )

    got = source._regex_types("owner1", [_Type("GEN_NAME"), _Type("LICENSE_PLATE")])
    assert [tid for tid, _ in got] == ["LICENSE_PLATE"], "未勾选的 DATE 不得进 docline 保证层"

    got_all = source._regex_types("owner1", None)
    assert {tid for tid, _ in got_all} == {"DATE", "LICENSE_PLATE"}, "勾选未传 ⇒ 旧行为"

    got_empty_run = source._regex_types("owner1", [])
    assert got_empty_run == [], "空勾选（明确不跑 HaS）⇒ 正则层也不跑"


def test_docline_boxes_for_page_passes_filtered_regex_types(monkeypatch, tmp_path):
    """docline 接线钉：boxes_for_page 收到的勾选集要落到 run_document_ner 的 regex_types。"""
    monkeypatch.setattr(settings, "TEXT_LAYER_DOCLINE_ENABLED", True)
    path = tmp_path / "scan.pdf"
    path.write_bytes(b"%PDF-1.4 stub")
    source.reset_memo()

    pages = [
        _page(1, ["2022年1月1日", *[f"证据材料第{i}页" for i in range(20)]]),
        _page(2, ["李四", *[f"证据材料第{i}页" for i in range(20)]]),
    ]

    def fake_extract(_path, **_kwargs):
        return pages

    captured_kwargs: dict = {}

    async def fake_run(page_list, labels, *, has_client, **kwargs):
        captured_kwargs.update(kwargs)
        return docline.DocLineResult(
            pages=2, chunks=1, chunk_chars=2000, stream_chars=10, layer_chars=8, covered_chars=10,
            entities=[docline.DocLineEntity(1, "PERSON", "李四", (0.1, 0.2, 0.3, 0.4))],
            ner_calls=1, truncated_chunks=0, http_errors=0, residual_calls=0, seconds=0.01,
        )

    monkeypatch.setattr(source.docline, "extract_text_layer_pages", fake_extract)
    monkeypatch.setattr(source.docline, "sample_ink_alignment", lambda *a, **k: 0.18)
    monkeypatch.setattr(source.docline, "run_document_ner", fake_run)
    monkeypatch.setattr(
        ets, "get_regex_types",
        lambda owner_id=None: [_type("DATE"), _type("LICENSE_PLATE")],
    )

    selection = [_Type("GEN_NAME"), _Type("LICENSE_PLATE")]
    asyncio.run(source.boxes_for_page(
        file_id="b" * 8,
        snapshot={"file_path": str(path), "file_type": "pdf_scanned"},
        page=1,
        ocr_has_types=selection,
        owner_id="owner1",
    ))

    assert "regex_types" in captured_kwargs, "run_document_ner 必须收到 regex_types"
    assert [tid for tid, _ in captured_kwargs["regex_types"]] == ["LICENSE_PLATE"], (
        "docline 保证层必须只跑勾选过的正则类型"
    )
