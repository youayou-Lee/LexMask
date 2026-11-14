"""Issue#275 B：接线测试（设计稿 §2.5/§2.6/§5）。

钉住：签名键只在 page 模式且 chars>0 时存在（file 模式 v6 键集逐字节，变异 1/5）；
``detect_vision`` page 分支 docline 零调用、A 接入缝 stub 恒 False（变异 2）；
``accelerator_should_fill`` 三态；``ner_done`` 透传 text_context/context_chars；
registry 登记点（text_layer / ocr 两路径、扫描页预闸登记）。
"""

from __future__ import annotations

import asyncio
import io
import json

import fitz
import pytest

from app.core.config import settings
from app.services import page_event_log as pel
from app.services import page_neighbor_context as pnc
from app.services import redaction_orchestrator as orch
from app.services.ocr_has_vision_service import OCRTextBlock
from app.services.vision_service import VisionService

_LINES_CLEAR = [
    "中华人民共和国最高人民法院刑事判决书",
    "本院依法组成合议庭公开开庭审理本案",
    "检察机关指派检察员出庭支持公诉意见",
    "被告人及其辩护人均到庭参加诉讼活",
    "经审理查明被告人犯罪事实清楚证据",
    "上述事实有书证物证证人证言予以证",
    "本院认为被告人行为已构成犯罪后果",
    "依照法律规定判决如下驳回上诉维持",
    "如不服本判决可在法定期限内提出上",
    "审判长审判员书记员依法署名于文末",
    "二〇二四年三月十五日判决作出日期",
    "本件与原本核对无异专用章加盖完毕",
]


# --------------------------------------------------------------------- 配置校验


def test_neighbor_chars_validator_bounds():
    from pydantic import ValidationError

    from app.core.config import Settings

    with pytest.raises(ValidationError):
        Settings(RECOGNITION_NEIGHBOR_CONTEXT_CHARS=2001)
    with pytest.raises(ValidationError):
        Settings(RECOGNITION_NEIGHBOR_CONTEXT_CHARS=-1)
    assert Settings(RECOGNITION_NEIGHBOR_CONTEXT_CHARS=0).RECOGNITION_NEIGHBOR_CONTEXT_CHARS == 0
    assert Settings(RECOGNITION_NEIGHBOR_CONTEXT_CHARS=500).RECOGNITION_NEIGHBOR_CONTEXT_CHARS == 500


def test_accelerator_flag_validator_enum():
    from pydantic import ValidationError

    from app.core.config import Settings

    with pytest.raises(ValidationError):
        Settings(RECOGNITION_ACCELERATOR_ENABLED="turbo")
    for value in ("off", "auto", "on"):
        assert Settings(RECOGNITION_ACCELERATOR_ENABLED=value).RECOGNITION_ACCELERATOR_ENABLED == value


# --------------------------------------------------------------------- 缓存签名


_FILE_MODE_V6_KEYS = {
    "version",
    "ner_governance",
    "text_layer_docline",
    "text_layer_docline_chunk_chars",
    "page",
    "ocr_has_types",
    "visual_feature_types",
    "visual_signature_max_side",
    "visual_queries",
    "visual_flags",
    "detector_epoch",
}
_PAGE_MODE_EXTRA_KEYS = {"routing_mode", "routing_version", "routing_thresholds_fp"}


def test_page_mode_signature_adds_neighbor_key_when_chars_positive(monkeypatch):
    monkeypatch.setattr(settings, "RECOGNITION_ROUTING_MODE", "page")
    monkeypatch.setattr(settings, "RECOGNITION_NEIGHBOR_CONTEXT_CHARS", 200, raising=False)
    sig = orch._vision_signature(1, [], [])
    assert sig["neighbor_context_chars"] == 200
    assert set(sig) == _FILE_MODE_V6_KEYS | _PAGE_MODE_EXTRA_KEYS | {"neighbor_context_chars"}


def test_page_mode_signature_no_neighbor_key_when_chars_zero(monkeypatch):
    """chars=0 ⇒ 键消失、缓存回 #273 键集（§2.7 回滚路径）。"""
    monkeypatch.setattr(settings, "RECOGNITION_ROUTING_MODE", "page")
    monkeypatch.setattr(settings, "RECOGNITION_NEIGHBOR_CONTEXT_CHARS", 0, raising=False)
    sig = orch._vision_signature(1, [], [])
    assert "neighbor_context_chars" not in sig
    assert set(sig) == _FILE_MODE_V6_KEYS | _PAGE_MODE_EXTRA_KEYS


def test_file_mode_signature_never_has_neighbor_key(monkeypatch):
    """变异 5：file 模式签名与 v6 逐键比较——多键即红。"""
    monkeypatch.setattr(settings, "RECOGNITION_ROUTING_MODE", "file")
    monkeypatch.setattr(settings, "RECOGNITION_NEIGHBOR_CONTEXT_CHARS", 200, raising=False)
    sig = orch._vision_signature(1, [], [])
    assert set(sig) == _FILE_MODE_V6_KEYS
    assert "neighbor_context_chars" not in sig


# --------------------------------------------------------------------- A 接入缝


class _StubStore(dict):
    def set(self, key, value):
        self[key] = value


class _StubVisionService:
    def __init__(self, calls: list[dict]):
        self._calls = calls
        self.last_warnings: list[str] = []
        self.last_pipeline_status: dict = {"ocr_has": {"ran": True}}
        self.last_duration_ms: dict = {"ocr_has": 5}

    async def detect_with_dual_pipeline(self, **kwargs):
        self._calls.append(kwargs)
        return [], None

    def _deduplicate_boxes(self, boxes):
        return list(boxes)


def _make_pdf(tmp_path, lines, *, name: str = "synthetic.pdf") -> str:
    path = str(tmp_path / name)
    doc = fitz.open()
    page = doc.new_page()
    y = 72
    for w in lines:
        page.insert_text((72, y), w, fontsize=12, fontname="china-s")
        y += 20
    doc.save(path)
    doc.close()
    return path


@pytest.fixture()
def wiring_env(monkeypatch):
    from app.services.page_routing import router as router_mod
    from app.services.page_routing import state as rt_state

    store = _StubStore()
    lock = asyncio.Lock()
    monkeypatch.setattr(orch, "_get_file_store", lambda: store)
    monkeypatch.setattr(orch, "_get_file_store_lock", lambda: lock)
    monkeypatch.setattr(settings, "ROUTING_SAMPLE_PAGES", 0)

    detect_calls: list[dict] = []

    def _vision_factory():
        return _StubVisionService(detect_calls)

    monkeypatch.setattr(orch, "VisionService", _vision_factory)

    docline_calls: list[dict] = []

    async def _stub_docline(**kwargs):
        docline_calls.append(kwargs)
        return None, None

    monkeypatch.setattr(orch, "text_layer_doc_boxes_for_page", _stub_docline)

    rt_state.reset_registry()
    router_mod.reset_caches()
    yield store, detect_calls, docline_calls
    rt_state.reset_registry()
    router_mod.reset_caches()


@pytest.mark.asyncio
async def test_page_mode_docline_not_called_with_seam_closed(wiring_env, monkeypatch, tmp_path):
    """变异 2 正向钉（B 定稿态）：page 模式整卷链零触发——A 缝 stub 恒 False。"""
    store, detect_calls, docline_calls = wiring_env
    monkeypatch.setattr(settings, "RECOGNITION_ROUTING_MODE", "page")
    monkeypatch.setattr(settings, "RECOGNITION_ACCELERATOR_ENABLED", "off", raising=False)
    path = _make_pdf(tmp_path, _LINES_CLEAR)
    store["f-1"] = {"file_type": "pdf", "file_path": path}

    await orch.detect_vision("f-1", page=1, selected_visual_feature_types=[])

    assert docline_calls == [], "page 模式 A 缝关闭时不得调 text_layer_doc_boxes_for_page"


@pytest.mark.asyncio
async def test_accelerator_should_fill_true_opens_seam(wiring_env, monkeypatch, tmp_path):
    """A 缝语义：seam 打开（未来 A 落地）⇒ 允许走 text_layer_doc_boxes_for_page。

    本支 stub 恒 False，这里 monkeypatch 桩函数本身验证缝的通断逻辑。
    """
    store, detect_calls, docline_calls = wiring_env
    monkeypatch.setattr(settings, "RECOGNITION_ROUTING_MODE", "page")

    import app.services.accelerator as acc

    async def _always(**kwargs):
        docline_calls.append(kwargs)
        return None, None

    monkeypatch.setattr(orch, "text_layer_doc_boxes_for_page", _always)
    monkeypatch.setattr(acc, "accelerator_should_fill", lambda file_id: True)

    path = _make_pdf(tmp_path, _LINES_CLEAR)
    store["f-1"] = {"file_type": "pdf", "file_path": path}

    await orch.detect_vision("f-1", page=1, selected_visual_feature_types=[])

    assert len(docline_calls) == 1, "seam 打开时 page 分支必须调 docline 填页"


# ------------------------------------------------------- accelerator stub 三态


def test_accelerator_stub_off_returns_false_without_warning(caplog, monkeypatch):
    import logging

    import app.services.accelerator as acc

    monkeypatch.setattr(settings, "RECOGNITION_ACCELERATOR_ENABLED", "off", raising=False)
    with caplog.at_level(logging.WARNING, logger="app.services.accelerator"):
        assert acc.accelerator_should_fill("f-1") is False
    assert not any("accelerator" in r.message.lower() for r in caplog.records)


@pytest.mark.parametrize("mode", ["auto", "on"])
def test_accelerator_stub_unimplemented_returns_false_with_warning(caplog, monkeypatch, mode):
    import logging

    import app.services.accelerator as acc

    monkeypatch.setattr(settings, "RECOGNITION_ACCELERATOR_ENABLED", mode, raising=False)
    with caplog.at_level(logging.WARNING, logger="app.services.accelerator"):
        assert acc.accelerator_should_fill("f-1") is False
    assert any("not implemented" in r.message for r in caplog.records), (
        "auto/on 本期与 off 同行为，但必须 warning 留痕（防「配了没生效」静默）"
    )


# ------------------------------------------------------------- ner_done 透传


_ITEM_ID = "item-1"
_JOB_ID = "job-1"


def _events(log_dir, job_id=_JOB_ID, item_id=_ITEM_ID):
    path = log_dir / job_id / f"{item_id}.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_ner_done_schema_accepts_text_context_fields(log_env):
    """白名单扩容：ner_done optional + text_context/context_chars；未声明字段仍拒写。"""
    pel.set_event_context(_JOB_ID, _ITEM_ID)
    try:
        assert pel.log_event(
            "ner_done",
            ms=10,
            regions=2,
            text_context="neighbor",
            context_chars=355,
        )
    finally:
        pel.clear_event_context()
    events = _events(log_env)
    ner = [e for e in events if e["event"] == "ner_done"]
    assert len(ner) == 1
    assert ner[0]["text_context"] == "neighbor"
    assert ner[0]["context_chars"] == 355


def test_ner_done_schema_still_rejects_undeclared_fields(log_env):
    pel.set_event_context(_JOB_ID, _ITEM_ID)
    try:
        assert not pel.log_event("ner_done", ms=1, regions=1, source="bogus")
    finally:
        pel.clear_event_context()
    assert [e for e in _events(log_env) if e["event"] == "ner_done"] == []


def test_ner_done_old_shape_still_writable(log_env):
    """旧 producer（docline 卷级 ner_done）不带新字段照常落盘。"""
    pel.set_event_context(_JOB_ID, _ITEM_ID)
    try:
        assert pel.log_event("ner_done", calls=6, ms=100, entities=8)
    finally:
        pel.clear_event_context()
    ner = [e for e in _events(log_env) if e["event"] == "ner_done"]
    assert len(ner) == 1
    assert "text_context" not in ner[0]


# ------------------------------------------------------- vision_service 登记点


class _StubParserTextLayer:
    def __init__(self, text: str, *, scanned: bool = False):
        self._text = text
        self._scanned = scanned

    async def get_pdf_page_text_blocks(self, file_path, page):
        return [OCRTextBlock(text=self._text, polygon=[[0, 0], [1, 0], [1, 1], [0, 1]], confidence=1.0)], 100, 100

    async def is_pdf_page_scanned(self, file_path, page):
        return self._scanned

    def has_fragmented_text_layer(self, text):
        return False


class _StubOcrHasTextOnly:
    async def detect_from_text_blocks(self, blocks, pipeline_types):
        return []


@pytest.mark.asyncio
async def test_text_layer_path_registers_page_text(tmp_path):
    vs = VisionService()
    vs.file_parser = _StubParserTextLayer("第二页文本层内容" + "补" * 80)  # ≥ PDF_TEXT_LAYER_MIN_CHARS
    vs.ocr_has_service = _StubOcrHasTextOnly()
    path = str(tmp_path / "doc.pdf")

    await vs._detect_with_pdf_text_layer(path, 2, [])

    prev, _nxt, meta = pnc.resolve_neighbors(path, 3, 200)
    assert prev == "第二页文本层内容" + "补" * 80
    assert meta["prev_source"] == "registry:text_layer"


@pytest.mark.asyncio
async def test_scanned_page_registers_before_gate(tmp_path):
    """§2.3 有意为之：扫描页在扫描闸之前登记（碎片串对邻居仍是真实墨迹）。"""
    vs = VisionService()
    vs.file_parser = _StubParserTextLayer("扫描页碎片文本", scanned=True)
    vs.ocr_has_service = _StubOcrHasTextOnly()
    path = str(tmp_path / "scan.pdf")

    with pytest.raises(ValueError, match="scanned page"):
        await vs._detect_with_pdf_text_layer(path, 2, [])

    prev, _nxt, meta = pnc.resolve_neighbors(path, 3, 200)
    assert prev == "扫描页碎片文本"
    assert meta["prev_source"] == "registry:text_layer"


@pytest.mark.asyncio
async def test_ocr_path_registers_page_text(tmp_path, monkeypatch):
    vs = VisionService()

    class _StubOcrHasDraw:
        async def detect_and_draw(self, image_data, vision_types=None, draw_result=True, blocks_out=None):
            blocks_out.append(
                OCRTextBlock(text="OCR第二页内容", polygon=[[0, 0], [1, 0], [1, 1], [0, 1]], confidence=1.0)
            )
            return [], None

    vs.ocr_has_service = _StubOcrHasDraw()
    path = str(tmp_path / "doc.pdf")
    vs._neighbor_registry_path = path  # detect_with_dual_pipeline activate 时落位

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (16, 16), "white").save(buf, format="PNG")

    await vs._detect_with_ocr_has(buf.getvalue(), 2, None)

    prev, _nxt, meta = pnc.resolve_neighbors(path, 3, 200)
    assert prev == "OCR第二页内容"
    assert meta["prev_source"] == "registry:ocr"


@pytest.fixture()
def log_env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "PAGE_EVENT_LOG_ENABLED", True)
    monkeypatch.setattr(settings, "PAGE_EVENT_LOG_DIR", str(tmp_path / "logs"))
    pel.reset_for_tests()
    yield tmp_path / "logs"
    pel.reset_for_tests()
