"""Issue#273 Task 9：vision_service 四动作执行 + 整页证据 OCR 仲裁 + verify_incomplete 死线。

钉住六件事（需求书 5 组 + file 模式零改动）：

1. **action=ocr**：文本层试探零调用、稀疏探针门零调用（决策面唯一），直接 OCR+HaS；
2. **action=text_layer**：文本层成功直接用（无 OCR）；文本层异常 ⇒ OCR 兜底 +
   outcome(switched_from=text_layer / switch_reason=text_layer_stage_error /
   arbitration=switch)；
3. **action=text_layer_verify**：文本层 + 整页证据 OCR（同一 recognition_path
   图像源）→ arbitrate 择一；K1 改判 ⇒ winner=ocr/arbitration=switch；B9 空证据 ⇒
   verify_incomplete=True + 保留文本层 + arbitration=None；文本层失败 ⇒ 证据直接
   采用（不失败页）；文本层失败且证据也超时 ⇒ TimeoutError（#199 页失败语义）；
   ROUTING_VERIFY_OCR_EVIDENCE_ENABLED=False ⇒ 降级直信文本层（verify_incomplete
   + evidence_valid=False + arbitration=None）；
4. **证据死线**：ROUTING_VERIFY_OCR_TIMEOUT_SEC 注入 0.1 + 桩 sleep 0.3 ⇒
   verify_incomplete=True + 返回文本层结果 + 页不抛；
5. **action=both**：OCR 显著更好 ⇒ escalate_volume=True；平局保留文本层不升级；
6. **file 模式（page_routing=None）**：attempt → probe_lock → OCR 回落原样
   （ValueError ⇒ 稀疏探针记账照旧、稀疏探针门被咨询）+
   ``last_page_routing_outcome is None``。

打桩口径：``VisionService.__new__`` 绕过单例栈（同 test_pdf_scanned_detection.py），
两检测方法打实例属性桩并记录调用；``last_pdf_text_layer_stats`` /
``_page_ocr_blocks`` 由桩按真实方法的落点写入（异常时 stats 仍先落——关键实现事实）。
"""

from __future__ import annotations

import asyncio
import io
from types import SimpleNamespace

import pytest
from PIL import Image

from app.core.config import settings
from app.models.schemas import BoundingBox, FileType
from app.services import vision_service as vs_mod
from app.services.page_routing.types import PageDecision


def _png_bytes(width: int = 120, height: int = 160) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (250, 250, 245)).save(buf, format="PNG")
    return buf.getvalue()


def _box(index: int, type_: str = "PERSON") -> BoundingBox:
    # 不同 y 让每框几何互斥：全链后处理（IoU 去重等）不吞桩框，ids 原样到返回值。
    return BoundingBox(
        id=f"box-{index}",
        x=0.08,
        y=0.08 + 0.12 * index,
        width=0.4,
        height=0.05,
        page=1,
        type=type_,
        text=f"值{index}",
        source="ocr_has",
        source_detail="stub",
    )


def _decision(action: str) -> PageDecision:
    return PageDecision(
        page=1,
        action=action,
        needs_verify=action == "text_layer_verify",
        confidence=0.5,
        signals={},
        reason="exec-test",
        routing_version=int(settings.ROUTING_VERSION),
    )


class _ExecStub:
    """两检测方法桩 + 调用记录；按真实方法的落点写 stats/blocks。"""

    def __init__(
        self,
        *,
        tl_error: BaseException | None = None,
        tl_chars: int = 0,
        tl_boxes: int = 1,
        ocr_boxes: int = 1,
        ocr_chars: int = 0,
        ocr_sleep: float = 0.0,
        ocr_error: BaseException | None = None,
    ):
        self.calls: dict[str, list[dict]] = {"tl": [], "ocr": [], "render": []}
        self.service = vs_mod.VisionService.__new__(vs_mod.VisionService)
        png = _png_bytes()

        async def _fake_recognition_source(file_path: str):
            return file_path, None

        async def _fake_get_pdf_page_image(path: str, page_no: int, allow_strip: bool = False):
            self.calls["render"].append({"path": path, "page": page_no})
            return png

        self.service.file_parser = SimpleNamespace(get_pdf_page_image=_fake_get_pdf_page_image)
        self.service._recognition_source = _fake_recognition_source

        async def _stub_tl(file_path: str, page_no: int, types: list | None):
            self.calls["tl"].append({"file_path": file_path, "page": page_no, "types": types})
            # 真实方法异常时 stats 也已落位（关键实现事实）：先写再抛。
            self.service.last_pdf_text_layer_stats = {"char_count": tl_chars, "block_count": 3}
            if tl_error is not None:
                raise tl_error
            return [_box(i) for i in range(tl_boxes)], None

        async def _stub_ocr(image_data: bytes, page_no: int, types: list | None, draw_result: bool = True):
            self.calls["ocr"].append(
                {"page": page_no, "types": types, "draw_result": draw_result}
            )
            if ocr_sleep:
                await asyncio.sleep(ocr_sleep)
            if ocr_error is not None:
                raise ocr_error
            self.service._page_ocr_blocks = [
                SimpleNamespace(text="字" * ocr_chars, left=0, top=0, width=10, height=10)
            ]
            return [_box(100 + i) for i in range(ocr_boxes)], None

        self.service._detect_with_pdf_text_layer = _stub_tl
        self.service._detect_with_ocr_has = _stub_ocr

    async def run(self, path: str, decision: PageDecision | None = None):
        return await self.service.detect_with_dual_pipeline(
            path,
            FileType.PDF,
            page=1,
            ocr_has_types=[SimpleNamespace(id="PERSON", name="人名")],
            visual_feature_types=None,
            include_result_image=False,
            page_routing=decision,
        )


@pytest.fixture()
def exec_env(monkeypatch):
    """file-mode 探针基建打桩（记录面）+ 文本层开关钉 True。

    返回记录 dict：``probe_gate`` 收稀疏探针门调用（page 模式必须零调用 =
    决策面唯一；file 模式必须被咨询 = 今天路径原样）、``probe_records`` 收
    ValueError 时的稀疏探针记账。
    """
    monkeypatch.setattr(settings, "PDF_TEXT_LAYER_VISION_ENABLED", True)
    records: dict[str, list] = {"probe_gate": [], "probe_records": []}

    def _fake_gate(file_path, file_type):
        records["probe_gate"].append({"path": file_path, "file_type": str(file_type)})
        return False

    def _fake_lock(file_path, file_type):
        return None

    def _fake_record(file_path, file_type, *, stats=None):
        records["probe_records"].append({"path": file_path, "stats": dict(stats or {})})

    monkeypatch.setattr(vs_mod, "_should_skip_sparse_pdf_text_layer", _fake_gate)
    monkeypatch.setattr(vs_mod, "_get_pdf_text_layer_probe_lock", _fake_lock)
    monkeypatch.setattr(vs_mod, "_record_sparse_pdf_text_layer_probe", _fake_record)
    return records


# --------------------------------------------------------------- 1) action=ocr


@pytest.mark.asyncio
async def test_action_ocr_skips_text_layer(exec_env, tmp_path):
    """action=ocr：文本层试探零调用、稀疏探针门零调用，OCR+HaS 直接执行。"""
    stub = _ExecStub(tl_chars=500)
    boxes, image = await stub.run(str(tmp_path / "a.pdf"), _decision("ocr"))

    assert stub.calls["tl"] == [], "action=ocr 不得试探文本层"
    assert exec_env["probe_gate"] == [], "决策面唯一：稀疏探针门不得被咨询"
    assert len(stub.calls["ocr"]) == 1
    assert stub.calls["ocr"][0]["draw_result"] is False
    assert [b.id for b in boxes] == ["box-100"]
    assert image is None

    outcome = stub.service.last_page_routing_outcome
    assert isinstance(outcome, dict), "page 模式每页必设 outcome"
    assert outcome["winner"] == "ocr"
    assert outcome["switched_from"] is None
    assert outcome["switch_reason"] is None
    assert outcome["arbitration"] is None
    assert outcome["escalate_volume"] is False


# ------------------------------------------------------- 2) action=text_layer


@pytest.mark.asyncio
async def test_action_text_layer_success_no_ocr(exec_env, tmp_path):
    """action=text_layer：文本层成功 ⇒ 直接用，无 OCR；outcome pass + 证据健康。"""
    stub = _ExecStub(tl_chars=120, tl_boxes=2)
    boxes, _ = await stub.run(str(tmp_path / "a.pdf"), _decision("text_layer"))

    assert len(stub.calls["tl"]) == 1
    assert stub.calls["ocr"] == [], "文本层成功不得跑证据 OCR"
    assert exec_env["probe_gate"] == []
    assert [b.id for b in boxes] == ["box-0", "box-1"]

    outcome = stub.service.last_page_routing_outcome
    assert outcome["winner"] == "text_layer"
    assert outcome["arbitration"] == "pass"
    assert outcome["evidence_valid"] is True
    assert outcome["verify_incomplete"] is False
    assert outcome["switched_from"] is None
    assert outcome["metrics"]["chars"]["text_layer"] == 120
    assert outcome["metrics"]["entities"]["text_layer"] == 2
    assert outcome["escalate_volume"] is False


@pytest.mark.asyncio
async def test_action_text_layer_error_falls_back_to_ocr(exec_env, tmp_path):
    """action=text_layer：文本层异常 ⇒ OCR 兜底 + switch 记账（stage_error）。"""
    stub = _ExecStub(
        tl_error=ValueError("sparse native text layer (3 chars < 120)"), tl_chars=3
    )
    boxes, _ = await stub.run(str(tmp_path / "a.pdf"), _decision("text_layer"))

    assert len(stub.calls["tl"]) == 1
    assert len(stub.calls["ocr"]) == 1
    assert [b.id for b in boxes] == ["box-100"]

    outcome = stub.service.last_page_routing_outcome
    assert outcome["winner"] == "ocr"
    assert outcome["switched_from"] == "text_layer"
    assert outcome["switch_reason"] == "text_layer_stage_error"
    assert outcome["arbitration"] == "switch"
    assert outcome["evidence_valid"] is False
    assert outcome["verify_incomplete"] is True


# ------------------------------------------------- 3) action=text_layer_verify


@pytest.mark.asyncio
async def test_action_verify_switches_to_ocr_on_chars_multiple(exec_env, tmp_path):
    """K1 改判：chars_ocr > tl×1.3 ⇒ 返回 OCR 框 + winner=ocr/arbitration=switch。"""
    stub = _ExecStub(tl_chars=100, tl_boxes=2, ocr_chars=200, ocr_boxes=2)
    boxes, _ = await stub.run(str(tmp_path / "a.pdf"), _decision("text_layer_verify"))

    assert len(stub.calls["tl"]) == 1 and len(stub.calls["ocr"]) == 1
    # 证据与文本层同一 recognition_path 图像源。
    assert stub.calls["tl"][0]["file_path"] == stub.calls["render"][0]["path"]
    assert [b.id for b in boxes] == ["box-100", "box-101"]

    outcome = stub.service.last_page_routing_outcome
    assert outcome["winner"] == "ocr"
    assert outcome["switched_from"] == "text_layer"
    assert outcome["switch_reason"] == "ocr_evidence_higher_chars"
    assert outcome["arbitration"] == "switch"
    assert outcome["evidence_valid"] is True
    assert outcome["verify_incomplete"] is False
    assert outcome["metrics"]["chars"] == {"text_layer": 100, "ocr": 200}
    assert isinstance(outcome["stage_ms"]["ocr_evidence"], int)
    assert outcome["escalate_volume"] is False, "verify 页改判不升整卷"


@pytest.mark.asyncio
async def test_action_verify_keeps_text_layer_on_tie(exec_env, tmp_path):
    """平局保留文本层：arbitration=pass（保留文本层且证据有效且非 incomplete）。"""
    stub = _ExecStub(tl_chars=100, tl_boxes=2, ocr_chars=110, ocr_boxes=2)
    boxes, _ = await stub.run(str(tmp_path / "a.pdf"), _decision("text_layer_verify"))

    assert [b.id for b in boxes] == ["box-0", "box-1"]
    outcome = stub.service.last_page_routing_outcome
    assert outcome["winner"] == "text_layer"
    assert outcome["arbitration"] == "pass"
    assert outcome["evidence_valid"] is True
    assert outcome["verify_incomplete"] is False
    assert outcome["switched_from"] is None
    assert outcome["escalate_volume"] is False


@pytest.mark.asyncio
async def test_action_verify_empty_evidence_b9(exec_env, tmp_path):
    """B9 硬约束：证据空返回（0 chars + 0 entities）⇒ 保留文本层 + verify_incomplete。"""
    stub = _ExecStub(tl_chars=100, tl_boxes=2, ocr_boxes=0, ocr_chars=0)
    boxes, _ = await stub.run(str(tmp_path / "a.pdf"), _decision("text_layer_verify"))

    assert len(stub.calls["ocr"]) == 1
    assert [b.id for b in boxes] == ["box-0", "box-1"]
    outcome = stub.service.last_page_routing_outcome
    assert outcome["winner"] == "text_layer"
    assert outcome["evidence_valid"] is False
    assert outcome["verify_incomplete"] is True
    assert outcome["arbitration"] is None
    assert outcome["escalate_volume"] is False


@pytest.mark.asyncio
async def test_action_verify_tl_failed_adopts_ocr_evidence(exec_env, tmp_path):
    """文本层失败 ⇒ 证据 OCR 结果直接采用（不失败页），stage_error 记账。"""
    stub = _ExecStub(
        tl_error=ValueError("scanned page with embedded OCR text layer (900 chars)"),
        tl_chars=900,
        ocr_chars=300,
        ocr_boxes=3,
    )
    boxes, _ = await stub.run(str(tmp_path / "a.pdf"), _decision("text_layer_verify"))

    assert len(stub.calls["tl"]) == 1 and len(stub.calls["ocr"]) == 1
    assert [b.id for b in boxes] == ["box-100", "box-101", "box-102"]
    outcome = stub.service.last_page_routing_outcome
    assert outcome["winner"] == "ocr"
    assert outcome["switched_from"] == "text_layer"
    assert outcome["switch_reason"] == "text_layer_stage_error"
    assert outcome["arbitration"] == "switch"
    assert outcome["evidence_valid"] is True
    assert outcome["verify_incomplete"] is False


@pytest.mark.asyncio
async def test_action_verify_tl_failed_and_evidence_timeout_raises(exec_env, monkeypatch, tmp_path):
    """文本层失败且证据 OCR 也超时 ⇒ RoutingEvidenceTimeout（页失败走 #199 语义）。"""
    monkeypatch.setattr(settings, "ROUTING_VERIFY_OCR_TIMEOUT_SEC", 0.1)
    stub = _ExecStub(
        tl_error=ValueError("sparse native text layer (3 chars < 120)"),
        tl_chars=3,
        ocr_sleep=0.3,
    )
    with pytest.raises(TimeoutError) as exc_info:  # RoutingEvidenceTimeout 是其子类
        await stub.run(str(tmp_path / "a.pdf"), _decision("text_layer_verify"))

    # C1：专类而非裸 TimeoutError——is_page_fatal 只认专类（分类档专测另钉）。
    from app.services.vision_service import RoutingEvidenceTimeout, is_page_fatal_stage_error

    assert isinstance(exc_info.value, RoutingEvidenceTimeout)
    assert "routing" in str(exc_info.value)
    assert is_page_fatal_stage_error(exc_info.value) is True

    outcome = stub.service.last_page_routing_outcome
    assert outcome["winner"] == "ocr"
    assert outcome["switch_reason"] == "text_layer_stage_error"
    assert outcome["verify_incomplete"] is True
    assert outcome["escalate_volume"] is False


def test_is_page_fatal_timeout_tier_only_matches_dedicated_class():
    """C1 回归钉：is_page_fatal 的超时档只匹配 RoutingEvidenceTimeout 专类。

    裸 ``TimeoutError`` / ``socket.timeout``（含 ocr_cache inflight 兜底的既有
    消息形态）必须维持「stage failed + 页 completed」原行为（改动中被 C1 破坏、
    本钉恢复）；NER 预算档与普通结果不受影响。
    """
    import socket

    from app.services.vision.has_text_analysis import HasTextNerPageBudgetExceeded
    from app.services.vision_service import RoutingEvidenceTimeout, is_page_fatal_stage_error

    assert is_page_fatal_stage_error(RoutingEvidenceTimeout("routing: boom")) is True
    assert is_page_fatal_stage_error(TimeoutError()) is False
    # socket.timeout 于 3.10+ 是 TimeoutError 的别名——保留别名形态的显式回归钉。
    assert is_page_fatal_stage_error(socket.timeout()) is False  # noqa: UP041
    # ocr_cache inflight 兜底的确切消息形态（_wait_for_ocr_output_inflight）
    assert is_page_fatal_stage_error(TimeoutError("等待同图 OCR in-flight 结果超时（300s），按失败处理")) is False
    assert is_page_fatal_stage_error(HasTextNerPageBudgetExceeded("budget")) is True
    assert is_page_fatal_stage_error(([], None)) is False


# --------------------------------------------------------- 4) 证据 OCR 证据降级


@pytest.mark.asyncio
async def test_action_verify_evidence_disabled_degrades_to_text_layer(exec_env, monkeypatch, tmp_path):
    """ROUTING_VERIFY_OCR_EVIDENCE_ENABLED=False ⇒ 降级直信文本层（无证据 OCR）。"""
    monkeypatch.setattr(settings, "ROUTING_VERIFY_OCR_EVIDENCE_ENABLED", False)
    stub = _ExecStub(tl_chars=100, tl_boxes=2, ocr_chars=999)
    boxes, _ = await stub.run(str(tmp_path / "a.pdf"), _decision("text_layer_verify"))

    assert stub.calls["ocr"] == [], "证据关闭不得跑 OCR"
    assert [b.id for b in boxes] == ["box-0", "box-1"]
    outcome = stub.service.last_page_routing_outcome
    assert outcome["winner"] == "text_layer"
    assert outcome["evidence_valid"] is False
    assert outcome["verify_incomplete"] is True
    assert outcome["arbitration"] is None
    assert outcome["escalate_volume"] is False
    assert outcome["stage_ms"]["ocr_evidence"] == 0


@pytest.mark.asyncio
async def test_action_verify_evidence_timeout_keeps_text_layer(exec_env, monkeypatch, tmp_path):
    """证据 OCR 死线（0.1s 死线 + 0.3s 桩）⇒ verify_incomplete + 返回文本层 + 页不抛。"""
    monkeypatch.setattr(settings, "ROUTING_VERIFY_OCR_TIMEOUT_SEC", 0.1)
    stub = _ExecStub(tl_chars=100, tl_boxes=2, ocr_chars=999, ocr_sleep=0.3)
    boxes, _ = await stub.run(str(tmp_path / "a.pdf"), _decision("text_layer_verify"))

    assert [b.id for b in boxes] == ["box-0", "box-1"], "证据超时保留文本层结果，页不失败"
    outcome = stub.service.last_page_routing_outcome
    assert outcome["winner"] == "text_layer"
    assert outcome["evidence_valid"] is False
    assert outcome["verify_incomplete"] is True
    assert outcome["arbitration"] is None
    assert outcome["escalate_volume"] is False
    assert isinstance(outcome["stage_ms"]["ocr_evidence"], int)
    assert outcome["stage_ms"]["ocr_evidence"] >= 50


# ------------------------------------------------------------- 5) action=both


@pytest.mark.asyncio
async def test_action_both_escalates_on_significant_ocr(exec_env, tmp_path):
    """action=both：OCR 显著更好（K1 改判）⇒ escalate_volume=True。"""
    stub = _ExecStub(tl_chars=100, tl_boxes=1, ocr_chars=200, ocr_boxes=3)
    boxes, _ = await stub.run(str(tmp_path / "a.pdf"), _decision("both"))

    assert len(stub.calls["tl"]) == 1 and len(stub.calls["ocr"]) == 1
    assert [b.id for b in boxes] == ["box-100", "box-101", "box-102"]
    outcome = stub.service.last_page_routing_outcome
    assert outcome["winner"] == "ocr"
    assert outcome["switch_reason"] == "ocr_evidence_higher_chars"
    assert outcome["arbitration"] == "switch"
    assert outcome["evidence_valid"] is True
    assert outcome["verify_incomplete"] is False
    assert outcome["escalate_volume"] is True


@pytest.mark.asyncio
async def test_action_both_tie_keeps_text_layer_no_escalate(exec_env, tmp_path):
    """action=both 平局：保留文本层，escalate_volume=False（不构成整卷升级依据）。"""
    stub = _ExecStub(tl_chars=100, tl_boxes=2, ocr_chars=110, ocr_boxes=2)
    boxes, _ = await stub.run(str(tmp_path / "a.pdf"), _decision("both"))

    assert [b.id for b in boxes] == ["box-0", "box-1"]
    outcome = stub.service.last_page_routing_outcome
    assert outcome["winner"] == "text_layer"
    assert outcome["arbitration"] == "pass"
    assert outcome["escalate_volume"] is False


# --------------------------------------------------------- 6) file 模式零改动


@pytest.mark.asyncio
async def test_file_mode_text_layer_success_path_unchanged(exec_env, tmp_path):
    """file 模式（page_routing=None）：文本层成功路径原样（门被咨询、无 OCR、outcome None）。"""
    stub = _ExecStub(tl_chars=200, tl_boxes=2)
    boxes, _ = await stub.run(str(tmp_path / "a.pdf"), None)

    assert len(exec_env["probe_gate"]) == 1, "file 模式照旧咨询稀疏探针门"
    assert stub.calls["ocr"] == []
    assert [b.id for b in boxes] == ["box-0", "box-1"]
    assert stub.service.last_page_routing_outcome is None


@pytest.mark.asyncio
async def test_file_mode_valueerror_falls_back_with_probe_record(exec_env, tmp_path):
    """file 模式：文本层 ValueError ⇒ 稀疏探针记账 + OCR 回落 + outcome None。"""
    stub = _ExecStub(
        tl_error=ValueError("sparse native text layer (3 chars < 120)"),
        tl_chars=3,
        ocr_chars=50,
    )
    boxes, _ = await stub.run(str(tmp_path / "a.pdf"), None)

    assert len(exec_env["probe_gate"]) == 1
    assert len(exec_env["probe_records"]) == 1, "ValueError ⇒ 稀疏探针记账（今天路径）"
    assert exec_env["probe_records"][0]["stats"]["char_count"] == 3
    assert len(stub.calls["ocr"]) == 1
    assert [b.id for b in boxes] == ["box-100"]
    assert stub.service.last_page_routing_outcome is None, "file 模式 outcome 恒 None"


# ------------------------------------------------------------ 防御性分支钉子


@pytest.mark.asyncio
async def test_unknown_action_fail_closed_to_ocr(exec_env, tmp_path):
    """未知动作（judge 只产四种）⇒ fail-closed 走 OCR + outcome 记账。"""
    stub = _ExecStub(tl_chars=100)
    boxes, _ = await stub.run(str(tmp_path / "a.pdf"), _decision("mystery"))

    assert stub.calls["tl"] == []
    assert len(stub.calls["ocr"]) == 1
    outcome = stub.service.last_page_routing_outcome
    assert outcome["winner"] == "ocr"
    assert outcome["arbitration"] is None
    assert outcome["escalate_volume"] is False
