"""Issue#238：进度卡诚实化——识别段「页完成」写点 + current_segment + 打码逐页上报。

三处失真的语义钉（设计见 #238 与已验收交互预览）：
- 「87/149」必须 = 真实完成页数（超时/失败页计入，单调防回退）——页**开跑**不再写进展；
- 段数动态：两段式（勾视觉要素且多页）写 ``recognition.current_segment``
  (index/total/label/concurrency)，合并段跳过失败页时 total 缩水为该段实际页数
  （进度条不卡死）；单段 total=1 不编号；
- 打码段逐页上报「打码 page p/N」：扫描件/图片栅格化路径逐页回调；文本/docx
  快链路无页级回调；单调防回退；打码起跑先写 ``redaction_started``。

harness 复用 test_issue139（同一队列/桩形态）。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from test_issue139_pipeline_asyncio_timeout import (
    _FILE_ID,
    _ITEM_ID,
    _JOB_ID,
    _Fake310AsyncTimeoutError,
    _FakeStore,
    _item,
    _patch_asyncio_timeout,
    _queue,
    _run,
    _vision_stubs,
)

import app.services.task_queue as tq
from app.core.config import settings

_VISION_CFG: dict[str, Any] = {"visual_feature_types": [], "ocr_has_types": ["姓名"]}
_TWO_PASS_CFG: dict[str, Any] = {"visual_feature_types": ["签名"], "ocr_has_types": ["姓名"]}


def _boxes_result(page: int) -> dict[str, Any]:
    return {"boxes": [{"id": f"p{page}-1", "page": page, "type": "PERSON", "text": "张三"}]}


def _progress_messages(store: _FakeStore) -> list[str]:
    return [str(call["kwargs"].get("message")) for call in store.progress_calls]


def _progress_currents(store: _FakeStore) -> list[int]:
    return [int(call["kwargs"].get("current", 0)) for call in store.progress_calls]


def _merged_performance(store: _FakeStore) -> dict[str, Any]:
    return json.loads(store.item.get("performance_json") or "{}")


def _segment_patches(store: _FakeStore) -> list[dict[str, Any]]:
    return [
        p["recognition"]["current_segment"]
        for p in store.performance
        if isinstance(p.get("recognition", {}).get("current_segment"), dict)
    ]


# ---------------------------------------------------------------------------
# 识别段：页完成写点 + 单调
# ---------------------------------------------------------------------------
def test_v1_progress_written_at_page_completion_not_start(monkeypatch: pytest.MonkeyPatch) -> None:
    """页在飞时只允许有 queued 一条进展；终态逐页上报 1/3→3/3。"""
    _patch_asyncio_timeout(monkeypatch)
    store = _FakeStore()
    queue = _queue(store)
    gate = asyncio.Event()

    async def _detect(file_id: str, page: int, **kwargs: Any) -> dict[str, Any]:
        await gate.wait()
        return _boxes_result(page)

    _vision_stubs(monkeypatch, pages=3, detect=_detect)

    async def scenario() -> list[dict[str, Any]]:
        runner = asyncio.create_task(queue._run_vision(_item(), dict(_VISION_CFG)))
        await asyncio.sleep(0.05)
        mid = list(store.progress_calls)  # 页全部在飞：不允许出现页开跑进展
        gate.set()
        await runner
        return mid

    mid = _run(scenario)
    assert [str(c["kwargs"].get("message")) for c in mid] == [
        "Vision recognition queued for 3 page(s)"
    ], mid
    assert _progress_messages(store) == [
        "Vision recognition queued for 3 page(s)",
        "视觉识别 page 1/3",
        "视觉识别 page 2/3",
        "视觉识别 page 3/3",
        "vision_recognition_complete",
    ], _progress_messages(store)
    assert _progress_currents(store) == [0, 1, 2, 3, 3], _progress_currents(store)  # 末位=收尾写 current=pages
    assert all(c["kwargs"].get("stage") == "vision" for c in store.progress_calls)


def test_v2_current_is_monotonic_under_out_of_order_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """并行下完成顺序 3→2→1：current 单调不减、终值=页数。"""
    _patch_asyncio_timeout(monkeypatch)
    monkeypatch.setattr(settings, "BATCH_RECOGNITION_PAGE_CONCURRENCY", 3)
    store = _FakeStore()
    queue = _queue(store)
    gates = {p: asyncio.Event() for p in (1, 2, 3)}

    async def _detect(file_id: str, page: int, **kwargs: Any) -> dict[str, Any]:
        await gates[page].wait()
        return _boxes_result(page)

    _vision_stubs(monkeypatch, pages=3, detect=_detect)

    async def scenario() -> None:
        runner = asyncio.create_task(queue._run_vision(_item(), dict(_VISION_CFG)))
        await asyncio.sleep(0.05)
        for p in (3, 2, 1):  # 逆序放行
            gates[p].set()
            await asyncio.sleep(0.02)
        await runner

    _run(scenario)
    finals = _progress_currents(store)
    assert finals == sorted(finals) and finals[0] == 0 and finals[-1] == 3, finals


def test_v3_two_pass_reports_segments_and_per_stage_reset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """两段式：current_segment 1→2 覆写；合并段从 1/2 重新起扫（不接续第一段）。"""
    _patch_asyncio_timeout(monkeypatch)
    store = _FakeStore()
    queue = _queue(store)
    detect_calls: list[dict[str, Any]] = []

    async def _detect(file_id: str, page: int, **kwargs: Any) -> dict[str, Any]:
        detect_calls.append(kwargs)
        return _boxes_result(page)

    _vision_stubs(monkeypatch, pages=2, detect=_detect)

    async def scenario() -> None:
        await queue._run_vision(_item(), dict(_TWO_PASS_CFG))

    _run(scenario)
    segments = _segment_patches(store)
    assert segments[0]["index"] == 1 and segments[0]["total"] == 2, segments
    assert segments[0]["label"] == "OCR+HaS识别", segments
    seg = _merged_performance(store)["recognition"]["current_segment"]
    assert seg["index"] == 2 and seg["total"] == 2, seg
    assert seg["label"] == "视觉特征识别", seg
    assert seg["concurrency"] >= 1, seg
    messages = _progress_messages(store)
    assert messages[0] == "Vision recognition queued for 2 page(s)"
    assert "OCR+HaS识别 page 1/2" in messages and "OCR+HaS识别 page 2/2" in messages
    assert "视觉特征识别 page 1/2" in messages and "视觉特征识别 page 2/2" in messages
    merge_currents = [
        int(c["kwargs"].get("current", 0))
        for c in store.progress_calls
        if "视觉特征识别" in str(c["kwargs"].get("message", ""))
    ]
    assert merge_currents == [1, 2], merge_currents
    # 两段各扫一遍全部页：detect 共 2页×2段 = 4 次
    assert len(detect_calls) == 4, len(detect_calls)


def test_v4_merge_stage_total_shrinks_when_pages_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """合并段跳过一阶段失败页：total=2（该段实际页数），进度条不卡死在 3。"""
    _patch_asyncio_timeout(monkeypatch)
    store = _FakeStore()
    queue = _queue(store)

    async def _detect(file_id: str, page: int, **kwargs: Any) -> dict[str, Any]:
        if page == 2 and kwargs.get("merge_existing") is not True:
            raise RuntimeError("boom")
        return _boxes_result(page)

    _vision_stubs(monkeypatch, pages=3, detect=_detect)

    async def scenario() -> None:
        await queue._run_vision(_item(), dict(_TWO_PASS_CFG))

    _run(scenario)
    merge_msgs = [
        str(c["kwargs"].get("message"))
        for c in store.progress_calls
        if "视觉特征识别" in str(c["kwargs"].get("message", ""))
    ]
    assert merge_msgs == ["视觉特征识别 page 1/2", "视觉特征识别 page 2/2"], merge_msgs
    # 一阶段失败页计入已完成：页 2 的失败让第一段计数走到 3/3
    pass1_msgs = [
        str(c["kwargs"].get("message"))
        for c in store.progress_calls
        if "OCR+HaS识别" in str(c["kwargs"].get("message", ""))
    ]
    assert pass1_msgs == ["OCR+HaS识别 page 1/3", "OCR+HaS识别 page 2/3", "OCR+HaS识别 page 3/3"], pass1_msgs


def test_v5_single_pass_segment_total_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """单段（未选视觉要素）：total=1 不编号，label=视觉识别。"""
    _patch_asyncio_timeout(monkeypatch)
    store = _FakeStore()
    queue = _queue(store)

    async def _detect(file_id: str, page: int, **kwargs: Any) -> dict[str, Any]:
        return _boxes_result(page)

    _vision_stubs(monkeypatch, pages=2, detect=_detect)

    async def scenario() -> None:
        await queue._run_vision(_item(), dict(_VISION_CFG))

    _run(scenario)
    seg = _merged_performance(store)["recognition"]["current_segment"]
    assert seg["index"] == 1 and seg["total"] == 1 and seg["label"] == "视觉识别", seg
    messages = _progress_messages(store)
    assert "视觉识别 page 1/2" in messages and "视觉识别 page 2/2" in messages


def test_v6_timeout_pages_count_toward_completed(monkeypatch: pytest.MonkeyPatch) -> None:
    """超时页同样计入已完成——页账走到头才算数，进度条不卡条。"""
    _patch_asyncio_timeout(monkeypatch)
    store = _FakeStore()
    queue = _queue(store)

    async def _detect(file_id: str, page: int, **kwargs: Any) -> dict[str, Any]:
        if page == 2:
            raise _Fake310AsyncTimeoutError()
        return _boxes_result(page)

    _vision_stubs(monkeypatch, pages=2, detect=_detect)

    async def scenario() -> None:
        await queue._run_vision(_item(), dict(_VISION_CFG))  # 不得上抛

    _run(scenario)
    assert _progress_currents(store)[-1] == 2, _progress_currents(store)
    assert "视觉识别 page 2/2" in _progress_messages(store), _progress_messages(store)


# ---------------------------------------------------------------------------
# 打码段：起跑 0/N + 逐页回调 + 单调
# ---------------------------------------------------------------------------
def _redaction_task() -> tq.TaskItem:
    return tq.TaskItem(job_id=_JOB_ID, item_id=_ITEM_ID, file_id=_FILE_ID, task_type="redaction")


def _stub_redaction_io(
    monkeypatch: pytest.MonkeyPatch,
    *,
    page_count: int,
    exec_cb: Callable[..., Any] | None = None,
) -> None:
    import app.services.file_operations as fo

    monkeypatch.setattr(
        fo,
        "get_file_info",
        lambda file_id: {"file_id": file_id, "page_count": page_count, "file_type": "pdf_scanned"},
    )

    async def _default_exec(
        file_id: str, entities: Any, boxes: Any, config: Any, progress_cb: Any = None
    ) -> None:
        return None

    monkeypatch.setattr(fo, "execute_redaction_request", exec_cb or _default_exec)


def test_r1_redaction_reports_start_and_per_page_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """起跑 0/N（redaction_started）→ 逐页「打码 page p/N」→ COMPLETED。"""
    _patch_asyncio_timeout(monkeypatch)
    store = _FakeStore()
    queue = _queue(store)
    seen_cb: list[Any] = []

    async def _exec(
        file_id: str, entities: Any, boxes: Any, config: Any, progress_cb: Any = None
    ) -> None:
        seen_cb.append(progress_cb)
        if progress_cb is not None:
            progress_cb(1, 3)
            progress_cb(2, 3)

    _stub_redaction_io(monkeypatch, page_count=3, exec_cb=_exec)

    async def scenario() -> None:
        await queue._run_redaction(_redaction_task())

    _run(scenario)
    assert len(seen_cb) == 1 and seen_cb[0] is not None, seen_cb
    red = [
        (c["kwargs"].get("current"), c["kwargs"].get("total"), str(c["kwargs"].get("message")))
        for c in store.progress_calls
        if c["kwargs"].get("stage") == "redaction"
    ]
    assert red == [
        (0, 3, "redaction_started"),
        (1, 3, "打码 page 1/3"),
        (2, 3, "打码 page 2/3"),
    ], red
    assert store.status_calls[-1]["status"] == "completed", store.status_calls


def test_r2_redaction_progress_is_monotonic(monkeypatch: pytest.MonkeyPatch) -> None:
    """回调乱序/重复不回退：2 后收到 1 被丢弃，3 继续。"""
    _patch_asyncio_timeout(monkeypatch)
    store = _FakeStore()
    queue = _queue(store)

    async def _exec(
        file_id: str, entities: Any, boxes: Any, config: Any, progress_cb: Any = None
    ) -> None:
        assert progress_cb is not None
        progress_cb(2, 3)
        progress_cb(1, 3)  # 乱序：必须被单调闸丢弃
        progress_cb(2, 3)  # 重复：同样丢弃
        progress_cb(3, 3)

    _stub_redaction_io(monkeypatch, page_count=3, exec_cb=_exec)

    async def scenario() -> None:
        await queue._run_redaction(_redaction_task())

    _run(scenario)
    red = [int(c["kwargs"].get("current", 0)) for c in store.progress_calls if c["kwargs"].get("stage") == "redaction"]
    assert red == [0, 2, 3], red


def test_r3_redaction_without_page_count_passes_no_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """docx/txt 等无页概念（page_count<=0）：不传回调，仅起跑一条进展。"""
    _patch_asyncio_timeout(monkeypatch)
    store = _FakeStore()
    queue = _queue(store)

    async def _exec(
        file_id: str, entities: Any, boxes: Any, config: Any, progress_cb: Any = None
    ) -> None:
        assert progress_cb is None, progress_cb

    _stub_redaction_io(monkeypatch, page_count=0, exec_cb=_exec)

    async def scenario() -> None:
        await queue._run_redaction(_redaction_task())

    _run(scenario)
    red = [c for c in store.progress_calls if c["kwargs"].get("stage") == "redaction"]
    assert len(red) == 1 and red[0]["kwargs"].get("current") == 0, red


# ---------------------------------------------------------------------------
# 回调链穿透：file_operations → orchestrator → Redactor → 栅格化逐页
# ---------------------------------------------------------------------------
def test_r4_execute_redaction_request_threads_progress_cb(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.services.redaction_orchestrator as ro
    from app.models.schemas import RedactionConfig
    from app.services import file_operations

    seen: dict[str, Any] = {}

    async def _fake_exec(request: Any, progress_cb: Any = None) -> None:
        seen["cb"] = progress_cb

    monkeypatch.setattr(ro, "execute_redaction", _fake_exec)
    def cb(current: int, total: int) -> None:
        return None

    async def scenario() -> None:
        await file_operations.execute_redaction_request(
            "f1", entities=[], bounding_boxes=[], config=RedactionConfig(), progress_cb=cb
        )

    _run(scenario)
    assert seen["cb"] is cb, seen


def test_r5_redactor_dispatch_passes_cb_to_image_path_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """栅格化路径（PDF_SCANNED）收到回调；文本 PDF 路径（docx 回转）不涉及回调。"""
    from app.models.schemas import RedactionConfig
    from app.services.redactor import Redactor

    red = Redactor.__new__(Redactor)  # 跳过 __init__（VisionService 构造重）
    image_cb_seen: list[Any] = []

    async def _fake_image(
        file_path: str,
        file_type: Any,
        boxes: Any,
        output_path: str,
        config: Any,
        progress_cb: Any = None,
    ) -> int:
        image_cb_seen.append(progress_cb)
        return 0

    async def _fake_docx_roundtrip(file_path: str, output_path: str, entities: Any, context: Any) -> int:
        return 0

    monkeypatch.setattr(red, "_redact_image", _fake_image)
    monkeypatch.setattr(red, "_redact_pdf_via_docx", _fake_docx_roundtrip)
    def cb(current: int, total: int) -> None:
        return None

    config = RedactionConfig()

    async def scanned() -> None:
        await red.redact(
            file_info={"file_type": "pdf_scanned", "file_path": "/tmp/x.pdf", "is_scanned": True},
            entities=[],
            bounding_boxes=[],
            config=config,
            progress_cb=cb,
        )

    _run(scanned)
    assert image_cb_seen == [cb], image_cb_seen

    async def text_pdf() -> None:
        await red.redact(
            file_info={"file_type": "pdf", "file_path": "/tmp/x.pdf", "is_scanned": False},
            entities=[],
            bounding_boxes=[],
            config=config,
            progress_cb=cb,
        )

    _run(text_pdf)
    assert image_cb_seen == [cb], image_cb_seen  # 文本路径未再触达栅格化分支


def test_r6_vision_redact_pdf_reports_per_page(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """栅格化逐页回调：2 页 PDF → (1,2),(2,2)。"""
    import fitz

    # open_pdf_checked 带路径白名单（只放行 uploads/output 区），测试夹具绕过为裸打开
    import app.services.file_parser as fp
    from app.services.vision_service import VisionService

    monkeypatch.setattr(fp, "open_pdf_checked", lambda path: fitz.open(path))

    src = fitz.open()
    src.new_page(width=100, height=120)
    src.new_page(width=100, height=120)
    pdf_path = tmp_path / "in.pdf"
    src.save(str(pdf_path))
    src.close()
    out_path = tmp_path / "out.pdf"

    svc = object.__new__(VisionService)  # _redact_pdf 不依赖实例状态
    calls: list[tuple[int, int]] = []

    def _record(current: int, total: int) -> None:
        calls.append((current, total))

    async def scenario() -> None:
        await svc._redact_pdf(
            str(pdf_path), [], str(out_path), "fill", 75, "#000000", progress_cb=_record
        )

    _run(scenario)
    assert calls == [(1, 2), (2, 2)], calls
    assert out_path.exists()


def test_r7_vision_redact_image_reports_single_page(tmp_path: Path) -> None:
    """单图无逐页概念：上报 (1, 1)。"""
    from PIL import Image as PILImage

    from app.services.vision_service import VisionService

    img_path = tmp_path / "in.png"
    PILImage.new("RGB", (10, 10)).save(img_path)
    out_path = tmp_path / "out.png"

    svc = object.__new__(VisionService)
    calls: list[tuple[int, int]] = []

    def _record(current: int, total: int) -> None:
        calls.append((current, total))

    async def scenario() -> None:
        await svc._redact_image(
            str(img_path), [], str(out_path), "fill", 75, "#000000", progress_cb=_record
        )

    _run(scenario)
    assert calls == [(1, 1)], calls
    assert out_path.exists()
