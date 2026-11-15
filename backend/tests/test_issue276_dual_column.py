"""Issue#276：验收 3 全链——文本列 + 视觉列双列同页（合成块 / 页账 / 事件 / 契约钉）。

钉住（设计稿 §5.3 / §7.2 / §3.4）：

1. **双列框合流**：``_detect_with_ocr_has``（PERSON / source="ocr_has"）与
   ``_detect_with_visual_features``（SIGNATURE / source="visual_features"）同页各出一框，
   ``all_boxes`` 同时含两列；``pipeline_status["visual_column"]["source"]=="la"``；
2. **§2.1 合成块四形态**（done≥1 / done zero-hit / stage_error / absent-全
   detector 通道）+ 文本列 fatal 路径合成块不可达（真值表行 6）；
3. **P0 契约钉（§7.2）**：``page_routing`` 决策产物不含视觉字段、``visual_column``
   与 ``page_routing`` 在 ``pipeline_status`` 并存互不引用；
4. **§3.4 升级开关**：``VISUAL_EXPLICIT_TYPE_PAGE_FAIL`` 默认 False（视觉异常永不
   升级页失败）；开 True 时异常包成 ``VisualStageError`` 专类升级页失败。

打桩口径：``VisionService.__new__`` 绕过单例栈（同 test_issue273_execution.py）。
"""

from __future__ import annotations

import io
from typing import Any

import pytest
from PIL import Image

from app.core.config import settings
from app.models.schemas import BoundingBox, FileType
from app.services import vision_service as vs_mod
from app.services.task_queue_metrics import _page_vision_quality_from_result


def _png_bytes(width: int = 120, height: int = 160) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (250, 250, 245)).save(buf, format="PNG")
    return buf.getvalue()


def _box(index: int, type_: str, source: str) -> BoundingBox:
    return BoundingBox(
        id=f"box-{index}",
        x=0.08,
        y=0.08 + 0.12 * index,
        width=0.4,
        height=0.05,
        page=1,
        type=type_,
        text=None,
        source=source,
        source_detail="stub",
    )


class _DualStub:
    """两检测方法桩（IMAGE 路径），记录调用、按需抛异常。"""

    def __init__(
        self,
        *,
        ocr_boxes: list[BoundingBox] | None = None,
        visual_boxes: list[BoundingBox] | None = None,
        visual_exc: Exception | None = None,
    ) -> None:
        self.calls: dict[str, list] = {"ocr": [], "visual": []}
        self.service = vs_mod.VisionService.__new__(vs_mod.VisionService)
        png = _png_bytes()

        class _Parser:
            async def read_image(self, path: str) -> bytes:  # noqa: D102
                return png

        self.service.file_parser = _Parser()

        async def _stub_ocr(image_data: bytes, page_no: int, types: list | None, draw_result: bool = True):
            self.calls["ocr"].append(list(types or []))
            return list(ocr_boxes or []), None

        async def _stub_visual(image_data: bytes, page_no: int, types: list | None, draw_result: bool = True):
            self.calls["visual"].append(list(types or []))
            if visual_exc is not None:
                raise visual_exc
            return list(visual_boxes or []), None

        async def _identity(boxes: list[BoundingBox], *args: Any, **kwargs: Any) -> list[BoundingBox]:
            return boxes

        self.service._detect_with_ocr_has = _stub_ocr
        self.service._detect_with_visual_features = _stub_visual
        # 专用检测器 / MinerU seal 通道全部打恒等桩（不影响 visual_column 口径，
        # 且隔离外部服务依赖）。
        self.service._detect_signatures_via_detector = _identity
        self.service._detect_fingerprints_via_detector = _identity

        async def _no_seal_source(boxes: list[BoundingBox], **kwargs: Any) -> list[BoundingBox]:
            return boxes

        self.service._apply_mineru_seal_source = _no_seal_source

    async def run(
        self,
        path: str,
        *,
        ocr_types: list | None = None,
        visual_types: list | None = None,
    ):
        return await self.service.detect_with_dual_pipeline(
            path,
            FileType.IMAGE,
            page=1,
            ocr_has_types=ocr_types,
            visual_feature_types=visual_types,
            include_result_image=False,
            page_routing=None,
        )


_VISUAL_ITEM = Any  # 勾选侧元素：生产是 pipeline 类型对象（带 .id），桩用 SimpleNamespace


def _simplens_types(*ids: str) -> list:
    from types import SimpleNamespace

    return [SimpleNamespace(id=i, name=i) for i in ids]


# ---------------------------------------------------------------------------
# D1：双列同页——两列框合流 + visual_column la + region_count
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_d1_dual_column_boxes_merge_and_visual_column_la(tmp_path) -> None:
    stub = _DualStub(
        ocr_boxes=[_box(0, "PERSON", "ocr_has")],
        visual_boxes=[_box(50, "SIGNATURE", "visual_features")],
    )
    boxes, _ = await stub.run(
        str(tmp_path / "a.png"),
        ocr_types=_simplens_types("PERSON"),
        visual_types=_simplens_types("SIGNATURE"),
    )

    sources = {b.source for b in boxes}
    assert "ocr_has" in sources, [b.model_dump() for b in boxes]
    assert "visual_features" in sources, "验收 3：两列实体都在账本"

    vc = stub.service.last_pipeline_status["visual_column"]
    assert vc["source"] == "la"
    assert vc["region_count"] == 1
    assert vc["types"] == ["SIGNATURE"]
    assert vc["failed_reason"] is None


@pytest.mark.asyncio
async def test_d1b_zero_hit_is_done_not_skipped(tmp_path) -> None:
    """M6 承重（vision_service 面）：LA 实跑 0 命中 = done，非 skipped。"""
    stub = _DualStub(ocr_boxes=[], visual_boxes=[])
    await stub.run(
        str(tmp_path / "a.png"),
        ocr_types=_simplens_types("PERSON"),
        visual_types=_simplens_types("SIGNATURE"),
    )
    vc = stub.service.last_pipeline_status["visual_column"]
    assert vc["source"] == "la"
    assert vc["region_count"] == 0
    assert vc["failed_reason"] is None


# ---------------------------------------------------------------------------
# D2：stage_error 形态（真值表行 5）——页不失败 + failed_reason 机读
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_d2_visual_stage_error_page_still_completes(tmp_path) -> None:
    stub = _DualStub(
        ocr_boxes=[_box(0, "PERSON", "ocr_has")],
        visual_exc=RuntimeError("la vllm died"),
    )
    boxes, _ = await stub.run(
        str(tmp_path / "a.png"),
        ocr_types=_simplens_types("PERSON"),
        visual_types=_simplens_types("SIGNATURE"),
    )

    # 文本列成果照常返回（视觉列失败不传染）
    assert [b.id for b in boxes] == ["box-0"]
    vc = stub.service.last_pipeline_status["visual_column"]
    assert vc["source"] == "la", "record_pipeline_result 异常路径同样置 ran=True（r2 F4）"
    assert vc["failed_reason"] == "visual_stage_error"
    assert any("visual_features failed:" in w for w in stub.service.last_warnings)


# ---------------------------------------------------------------------------
# D3：absent 形态——勾选全走专用检测器通道（fingerprint 被 YOLO 替换）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_d3_all_detector_channel_absent_explicit(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "FINGERPRINT_DETECTOR_URL", "http://127.0.0.1:9101")
    stub = _DualStub(ocr_boxes=[], visual_boxes=[])
    await stub.run(
        str(tmp_path / "a.png"),
        ocr_types=None,
        visual_types=_simplens_types("fingerprint"),
    )
    vc = stub.service.last_pipeline_status["visual_column"]
    assert vc["source"] == "none"
    assert vc["types"] == [], "effective 过滤后为空 ⇒ LA 不跑"
    assert vc["requested_types"] == ["fingerprint"], "勾选侧原表进 requested（审计用）"
    assert vc["failed_reason"] is None
    # LA 检测方法零调用
    assert stub.calls["visual"] == []


# ---------------------------------------------------------------------------
# D4：文本列 fatal（真值表行 6）——合成块不可达，页失败
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_d4_text_column_fatal_no_visual_column_block(tmp_path, monkeypatch) -> None:
    from app.services.vision.has_text_analysis import HasTextNerUnavailable

    monkeypatch.setattr(settings, "HAS_NER_UNAVAILABLE_PAGE_FAIL", True, raising=False)
    stub = _DualStub(visual_boxes=[_box(50, "SIGNATURE", "visual_features")])

    async def _boom(*args: Any, **kwargs: Any) -> Any:
        raise HasTextNerUnavailable("ner_unavailable: 1/1 group(s) failed")

    stub.service._detect_with_ocr_has = _boom

    with pytest.raises(HasTextNerUnavailable):
        await stub.run(
            str(tmp_path / "a.png"),
            ocr_types=_simplens_types("PERSON"),
            visual_types=_simplens_types("SIGNATURE"),
        )

    assert "visual_column" not in stub.service.last_pipeline_status, (
        "行 6：本次运行无新账——fatal 路径在合成块之前 raise，不回读、不编造"
    )


# ---------------------------------------------------------------------------
# D5：P0 契约钉（§7.2）——page_routing 与 visual_column 并存互不引用
# ---------------------------------------------------------------------------


def test_d5_routing_and_visual_column_extraction_orthogonal() -> None:
    """quality 提取面：routing 字段不含视觉键、visual 字段不含文本键。"""
    from app.services.task_queue_pipelines import (
        _routing_fields_from_quality,
        _visual_fields_from_quality,
    )

    quality = {
        "pipeline_status": {
            "page_routing": {
                "text_source": "ocr",
                "decision": {"action": "ocr", "reason": "r", "signals": {}},
                "stage_ms": {},
            },
            "visual_column": {
                "source": "la", "types": ["SIGNATURE"], "requested_types": ["SIGNATURE"],
                "region_count": 1, "failed_reason": None,
            },
        }
    }
    routing_fields = _routing_fields_from_quality(quality)
    visual_fields = _visual_fields_from_quality(quality)

    assert set(routing_fields) == {"text_source", "text_metrics", "stage_ms"}
    assert all(not k.startswith("visual") for k in routing_fields), "P0 契约：路由产物零视觉字段"
    assert set(visual_fields) == {"visual_source", "visual_types", "visual_region_count"}
    assert all(k not in {"text_source", "text_metrics", "stage_ms"} for k in visual_fields)
    # 互不引用：块内无对方引用
    pr = quality["pipeline_status"]["page_routing"]
    vc = quality["pipeline_status"]["visual_column"]
    assert "visual" not in json_keys(pr)
    assert "routing" not in json_keys(vc) and "text_source" not in json_keys(vc)


def json_keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        out: set[str] = set()
        for k, v in value.items():
            out.add(str(k))
            out |= json_keys(v)
        return out
    return set()


# ---------------------------------------------------------------------------
# D6：§3.4 升级开关——默认关（页照常 completed）；开 True 时包 VisualStageError 升级页失败
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_d6_gate_default_off_visual_error_never_fails_page(tmp_path) -> None:
    from app.services.vision_service import is_page_fatal_stage_error

    assert getattr(settings, "VISUAL_EXPLICIT_TYPE_PAGE_FAIL", False) is False
    assert is_page_fatal_stage_error(RuntimeError("la boom")) is False
    assert is_page_fatal_stage_error(ValueError("la boom")) is False

    stub = _DualStub(ocr_boxes=[_box(0, "PERSON", "ocr_has")], visual_exc=ValueError("la boom"))
    boxes, _ = await stub.run(
        str(tmp_path / "a.png"),
        ocr_types=_simplens_types("PERSON"),
        visual_types=_simplens_types("SIGNATURE"),
    )
    # 页照常收尾（非 raise），文本成果不受影响
    assert [b.id for b in boxes] == ["box-0"]
    vc = stub.service.last_pipeline_status["visual_column"]
    assert vc["failed_reason"] == "visual_stage_error"


@pytest.mark.asyncio
async def test_d6b_gate_on_wraps_into_visual_stage_error(tmp_path, monkeypatch) -> None:
    from app.services.vision_service import VisualStageError, is_page_fatal_stage_error

    monkeypatch.setattr(settings, "VISUAL_EXPLICIT_TYPE_PAGE_FAIL", True)
    stub = _DualStub(visual_exc=ValueError("la boom"))
    with pytest.raises(VisualStageError) as exc_info:
        await stub.run(
            str(tmp_path / "a.png"),
            ocr_types=_simplens_types("PERSON"),
            visual_types=_simplens_types("SIGNATURE"),
        )
    assert "la boom" in str(exc_info.value)
    assert is_page_fatal_stage_error(exc_info.value) is True, "开关开 ⇒ 显式勾选类型跑挂升级页失败"


def test_d6c_visual_stage_error_is_timeout_orthogonal() -> None:
    """专类语义：VisualStageError 不是 TimeoutError 族，与既有超时路径互不连坐。"""
    from app.services.vision_service import VisualStageError

    assert not issubclass(VisualStageError, TimeoutError)


# ---------------------------------------------------------------------------
# D7：quality 提取面（result 形状 → visual_column 可达）
# ---------------------------------------------------------------------------


def test_d7_pipeline_status_carries_visual_column_through_result() -> None:
    """编排层 ``_page_vision_quality_from_result`` 把 ``pipeline_status``（含
    ``visual_column``）原样带进 quality——页账派生的数据通路证据。"""

    class _Result:
        duration_ms = {}
        cache_status = {}
        warnings: list[str] = []
        pipeline_status = {
            "visual_column": {
                "source": "la", "types": ["SIGNATURE"], "requested_types": ["SIGNATURE"],
                "region_count": 1, "failed_reason": None,
            }
        }

    quality = _page_vision_quality_from_result(_Result())
    assert quality["pipeline_status"]["visual_column"]["source"] == "la"
