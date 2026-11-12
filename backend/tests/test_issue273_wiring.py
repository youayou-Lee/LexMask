"""Issue#273 Task 8：``detect_vision`` page/file 双模接线测试。

钉住四件事（A4 双模分流 / A8 fail-closed）：

1. **file 模式 router 零调用**：``RECOGNITION_ROUTING_MODE=file``（回滚开关）时
   ``router.decide`` 一次都不被调用、``pipeline_status`` 无 ``page_routing`` 键
   ——file 模式行为与今天逐字节一致是本任务的硬约束；
2. **缓存签名**：file 模式签名与 v6 **逐字段相同**（不加任何键——键集相等断言）；
   page 模式追加 ``routing_mode``/``routing_version``（靠新键变键使既有页缓存失效）；
3. **page 模式分流**：docline 不再被调用（``text_layer_doc_boxes_for_page`` 零调用）、
   ``router.decide`` 被调用且决策经 ``detect_with_dual_pipeline(page_routing=)``
   透传、``finalize`` 合成 ``pipeline_status["page_routing"]``（decision 落
   ``pipeline_status``、账本落 file_store）；
4. **A8 fail-closed**：加密 PDF / 页号越界在 page 模式不 5xx、决策退
   ``ocr/signal_probe_error``。

打桩口径沿用 ``tests/test_issue273_router.py``（``_StubStore`` + monkeypatch
``orch._get_file_store``/``orch._get_file_store_lock`` + 合成 PDF ``china-s``
字体）与 ``test_mask_mode_gating.py``；路由态（state registry + router 指纹 memo）
每例前后复位。Task 9 尚未产出 ``last_page_routing_outcome`` ⇒ outcome 为 None
的防御路径即本文件的最终形态（Task 9 后此处自动拿到真 outcome，断言不变）。
"""

from __future__ import annotations

import asyncio

import fitz
import pytest

from app.core.config import settings
from app.services import redaction_orchestrator as orch
from app.services.page_routing import router as router_mod
from app.services.page_routing import state as rt_state
from app.services.page_routing.types import PageDecision

# 与 test_issue273_router.py 同源：196 字符 ≥ 120×1.5 灰区带上限，非灰区、
# 全指标健康 ⇒ default_verify（该组样本在 router 家族已验证过判定路径）。
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


class _StubStore(dict):
    def set(self, key, value):
        self[key] = value


class _StubVisionService:
    """替身 vision_service：接住 ``detect_with_dual_pipeline`` 的全部 kwargs。

    记录 ``page_routing`` 透传（本任务的接线产物）而不执行任何真实识别栈；
    ``last_*`` 属性与生产实例同形（``detect_vision`` 用 getattr 收割）。
    """

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


def _make_pdf(tmp_path, lines, *, name: str = "synthetic.pdf", encrypted: bool = False) -> str:
    path = str(tmp_path / name)
    doc = fitz.open()
    page = doc.new_page()
    y = 72
    for w in lines:
        page.insert_text((72, y), w, fontsize=12, fontname="china-s")
        y += 20
    if encrypted:
        doc.save(path, encryption=fitz.PDF_ENCRYPT_AES_256, owner_pw="secret", user_pw="secret")
    else:
        doc.save(path)
    doc.close()
    return path


@pytest.fixture()
def wiring_env(monkeypatch):
    """file_store 打桩 + 路由全局态复位 + 视觉栈替身。

    返回 ``(store, detect_calls, docline_calls)`` 三个记录面：
    detect_calls 收 ``detect_with_dual_pipeline(**kwargs)``；
    docline_calls 收 ``text_layer_doc_boxes_for_page(...)`` 调用（page 模式必须为零）。
    """
    store = _StubStore()
    lock = asyncio.Lock()  # 共享锁实例：与生产「单一 _file_store_lock」同构
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


# --------------------------------------------------------------------- 缓存签名


def test_file_mode_signature_identical_to_v6(monkeypatch):
    """file 模式签名 dict 与今天 v6 **逐字段一致**：键集相等 + version=6。

    键集相等（而非只查 routing_* 缺席）钉住「不许给 file 模式加任何键」——
    多出的键同样会让既有页缓存失效，违背回滚开关的字面语义。
    """
    monkeypatch.setattr(settings, "RECOGNITION_ROUTING_MODE", "file")
    sig = orch._vision_signature(1, [], [])
    assert set(sig) == {
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
    assert sig["version"] == 6
    assert "routing_mode" not in sig and "routing_version" not in sig


def test_page_mode_signature_has_routing_fields(monkeypatch):
    """page 模式追加 ``routing_mode``/``routing_version``/``routing_thresholds_fp``，
    ``version`` 字段保持 6。

    靠新键变键：老缓存 key（无这三个键）与 page 模式新 key 不相等 ⇒ 既有页缓存
    全量失效重建（首次 cache miss），不会回放 page/file 旧口径的框。
    阈值指纹（终审 I-2/M-4）：值必须与 ``thresholds_fingerprint(get_routing_thresholds())``
    一致——阈值翻 env 而不 bump ROUTING_VERSION 时签名仍变（ner_governance 先例语义）。
    """
    monkeypatch.setattr(settings, "RECOGNITION_ROUTING_MODE", "page")
    sig = orch._vision_signature(1, [], [])
    assert sig["routing_mode"] == "page"
    assert sig["routing_version"] == int(settings.ROUTING_VERSION)
    assert sig["version"] == 6  # v6 字段不动，靠新增键变键
    from app.core.routing_thresholds import get_routing_thresholds, thresholds_fingerprint

    expected_fp = thresholds_fingerprint(get_routing_thresholds())
    assert sig["routing_thresholds_fp"] == expected_fp
    # 指纹形态钉：16 位 hex（sha256 前 16 位）
    assert len(expected_fp) == 16
    int(expected_fp, 16)


# --------------------------------------------------------------------- 双模分流


@pytest.mark.asyncio
async def test_file_mode_router_never_called(wiring_env, monkeypatch, tmp_path):
    """file 模式（回滚开关）：router.decide 零调用、pipeline_status 无 page_routing 键。"""
    store, detect_calls, docline_calls = wiring_env
    monkeypatch.setattr(settings, "RECOGNITION_ROUTING_MODE", "file")
    decide_calls: list = []

    async def _counting_decide(*args, **kwargs):
        decide_calls.append((args, kwargs))
        raise AssertionError("file 模式不得调用 router.decide")

    monkeypatch.setattr(router_mod, "decide", _counting_decide)

    path = _make_pdf(tmp_path, _LINES_CLEAR)
    store["f-1"] = {"file_type": "pdf", "file_path": path}

    result = await orch.detect_vision("f-1", page=1, selected_visual_feature_types=[])

    assert decide_calls == [], "file 模式 router.decide 必须零调用"
    assert "page_routing" not in result.pipeline_status
    # file 分支确实走了 docline（行为未变：docline 被调用、page_routing=None 透传）
    assert len(docline_calls) == 1
    assert detect_calls and detect_calls[0].get("page_routing") is None


@pytest.mark.asyncio
async def test_page_mode_routes_and_records(wiring_env, monkeypatch, tmp_path):
    """page 模式：docline 零调用、decide→透传→finalize 全链、账本+pipeline_status 落地。"""
    store, detect_calls, docline_calls = wiring_env
    monkeypatch.setattr(settings, "RECOGNITION_ROUTING_MODE", "page")

    path = _make_pdf(tmp_path, _LINES_CLEAR)
    store["f-1"] = {"file_type": "pdf", "file_path": path}

    result = await orch.detect_vision("f-1", page=1, selected_visual_feature_types=[])

    # 分流：page 模式不碰 docline（文本来源改由路由决策指挥，Task 9 消费）
    assert docline_calls == [], "page 模式不得调用 text_layer_doc_boxes_for_page"
    # 决策经 detect_with_dual_pipeline(page_routing=) 透传（Task 9 的入口）
    assert len(detect_calls) == 1
    passed = detect_calls[0].get("page_routing")
    assert isinstance(passed, PageDecision)
    assert passed.action == "text_layer_verify"
    assert passed.reason == "default_verify"

    # finalize 合成 pipeline_status["page_routing"]（Task 9 未产出 outcome ⇒ None 防御）
    routing = result.pipeline_status.get("page_routing")
    assert isinstance(routing, dict)
    assert routing["decision"]["reason"] == "default_verify"
    assert routing["decision"]["action"] == "text_layer_verify"
    assert routing["outcome"] is None
    assert routing["text_source"] == "text_layer"
    assert routing["switched_from"] is None and routing["switch_reason"] is None
    assert routing["verify_incomplete"] is False
    # decide 阶段计时：orchestrator 兜底补 judge 键（decide 未提供时）
    assert isinstance(routing["decision"].get("stage_ms"), dict)
    assert isinstance(routing["decision"]["stage_ms"].get("judge"), int)

    # 决策即落账 + finalize 终值：file_store info["page_routing"]["1"]
    entry = store["f-1"]["page_routing"]["1"]
    assert entry["decision"]["reason"] == "default_verify"
    assert entry["text_source"] == "text_layer"
    assert entry["decided_at"]


@pytest.mark.asyncio
async def test_encrypted_and_zero_page_fail_closed(wiring_env, monkeypatch, tmp_path):
    """A8：加密 PDF / 页号越界在 page 模式不 5xx、决策 fail-closed 退 ocr。"""
    store, detect_calls, _docline_calls = wiring_env
    monkeypatch.setattr(settings, "RECOGNITION_ROUTING_MODE", "page")

    # 加密 PDF（needs_pass，无口令）⇒ probe_error ⇒ ocr/signal_probe_error
    enc_path = _make_pdf(tmp_path, _LINES_CLEAR, name="encrypted.pdf", encrypted=True)
    store["f-1"] = {"file_type": "pdf", "file_path": enc_path}
    result = await orch.detect_vision("f-1", page=1, selected_visual_feature_types=[])
    routing = result.pipeline_status["page_routing"]
    assert routing["decision"]["reason"] == "signal_probe_error"
    assert routing["decision"]["action"] == "ocr"
    assert routing["text_source"] == "ocr"
    assert store["f-1"]["page_routing"]["1"]["text_source"] == "ocr"

    # 页号越界（1 页卷要第 2 页）⇒ 同一 fail-closed 族
    plain_path = _make_pdf(tmp_path, _LINES_CLEAR, name="single.pdf")
    store["f-2"] = {"file_type": "pdf", "file_path": plain_path}
    result2 = await orch.detect_vision("f-2", page=2, selected_visual_feature_types=[])
    routing2 = result2.pipeline_status["page_routing"]
    assert routing2["decision"]["reason"] == "signal_probe_error"
    assert routing2["text_source"] == "ocr"
