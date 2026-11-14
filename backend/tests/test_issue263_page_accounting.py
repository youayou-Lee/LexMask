"""Issue#263 页级记账链 —— HasTextNerUnavailable 进 recognition.failed_pages（AC5/AC6）。

方案：LexMask-workspace/docs/plans/2026-11-13-issue263-ner-fail-closed.md（v3）
判据（AC5）：页级 stage 捕获该异常 ⇒ `recognition.failed_pages` 含该页、reason 格式
``{stage_label}: HasTextNerUnavailable: ner_unavailable: ...``；
`recognition.pages.<p>.error_type == "HasTextNerUnavailable"`；
#279 `page_failed` 事件发射且 `error_type=="HasTextNerUnavailable"`；条目终态
`awaiting_review` 非 `failed`。
判据（AC6）：失败页不随交付出厂（#199 既有闸回归钉）+ 清账重跑无条件清空。

harness 复用 test_issue139/_page_event_wiring_pipeline（同一队列/桩形态）。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from test_issue139_pipeline_asyncio_timeout import (
    _ITEM_ID,
    _JOB_ID,
    _FakeStore,
    _item,
    _queue,
    _run,
)
from test_page_event_wiring_pipeline import _patch_recognition_io

from app.core.config import settings
from app.services import page_event_log as pel
from app.services.job_store import JobItemStatus
from app.services.vision.has_text_analysis import HasTextNerUnavailable

_VISION_CFG: dict[str, Any] = {"visual_feature_types": [], "ocr_has_types": ["姓名"]}


@pytest.fixture()
def log_env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "PAGE_EVENT_LOG_ENABLED", True)
    monkeypatch.setattr(settings, "PAGE_EVENT_LOG_DIR", str(tmp_path / "logs"))
    pel.reset_for_tests()
    yield tmp_path / "logs"
    pel.reset_for_tests()


def _events(log_env, job_id: str = _JOB_ID, item_id: str = _ITEM_ID) -> list[dict[str, Any]]:
    path = log_env / job_id / f"{item_id}.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _of(events: list[dict[str, Any]], event: str) -> list[dict[str, Any]]:
    return [e for e in events if e["event"] == event]


def _failed_pages_of(store: _FakeStore) -> dict[str, Any] | None:
    """深合并语义下读取最终 failed_pages（同 test_issue199 口径）。"""
    merged: dict[str, Any] = {}
    for patch in store.performance:
        rec = patch.get("recognition") or {}
        if "failed_pages" in rec:
            merged = rec["failed_pages"]
    return merged if merged or merged == {} else None


def _boxes_result(page: int) -> dict[str, Any]:
    return {"boxes": [{"id": f"p{page}-1", "page": page, "type": "PERSON", "text": "张三"}]}


# ---------------------------------------------------------------------------
# AC5：页级记账链
# ---------------------------------------------------------------------------
def test_ac5_ner_unavailable_page_accounting(log_env, monkeypatch: pytest.MonkeyPatch) -> None:
    store = _FakeStore()
    queue = _queue(store)

    async def _detect(file_id: str, page: int, **kwargs: Any) -> dict[str, Any]:
        if page == 2:
            raise HasTextNerUnavailable("ner_unavailable: 1/1 group(s) failed; not merging partial results")
        return _boxes_result(page)

    _patch_recognition_io(monkeypatch, queue, pages=2, detect=_detect)
    _run(lambda: queue._run_recognition(_item()))

    # ① failed_pages 落账 + reason 格式（前缀=失败段 stage_label；机读 token 首段存活）
    failed = _failed_pages_of(store)
    assert failed is not None and list(failed) == ["2"], failed
    reason = str(failed["2"])
    assert reason.startswith(("视觉识别: HasTextNerUnavailable", "OCR+HaS识别: HasTextNerUnavailable")), reason
    assert "ner_unavailable" in reason, reason
    # ② 页记录镜像：error_type 机读 + status failed
    entry = store.page_entry(2)
    assert entry.get("status") == "failed"
    assert entry.get("error_type") == "HasTextNerUnavailable"
    # ③ #279 page_failed 事件：error_type 机读 + 消息含 token
    failed_events = _of(_events(log_env), "page_failed")
    assert len(failed_events) == 1, failed_events
    event = failed_events[0]
    assert event["page"] == 2
    assert event["error_type"] == "HasTextNerUnavailable"
    assert "ner_unavailable" in event["message_sanitized"]
    # ④ 条目终态 awaiting_review 非 failed（#199 页级隔离，不连坐整卷）
    review_calls = [c for c in store.status_calls if c["status"] == JobItemStatus.AWAITING_REVIEW.value]
    failed_calls = [c for c in store.status_calls if c["status"] == JobItemStatus.FAILED.value]
    assert review_calls and not failed_calls, store.status_calls


# ---------------------------------------------------------------------------
# AC5b：阶段内异常的升格接线钉（镜像 #209 I3 判据——M2 变异的承重杀手）
# ---------------------------------------------------------------------------
def test_ac5b_ner_unavailable_propagates_out_of_the_page_pipeline(monkeypatch):
    """NER 服务级异常必须**从页管线真上抛**到页级调用方。

    镜像 #209 门⑥ I3 的接线判据：`record_pipeline_result` 只把阶段异常降级为
    warning，**全靠 `is_page_fatal_stage_error` 把本类升格成 page_fatal_error 再
    raise**。摘掉该判定（变异 M2）⇒ 页按「ocr_has 阶段失败但页 completed」收尾
    ⇒ 静默零实体——正是 #263 的病。此前的 AC5 夹具在 vision_detect 边界抛异常，
    绕过了该判定（M2 活变异实证），本用例把注入点钉在阶段内。
    """
    from app.services.vision_service import FileType, VisionService

    monkeypatch.setattr(settings, "HAS_NER_UNAVAILABLE_PAGE_FAIL", True, raising=False)

    svc = VisionService()

    class _Parser:
        async def read_image(self, path):  # noqa: D102
            return b"fake-image"

        async def get_pdf_page_image(self, path, page):  # noqa: D102
            return b"fake-image"

    svc.file_parser = _Parser()

    async def _boom(*_args, **_kwargs):
        raise HasTextNerUnavailable("ner_unavailable: 1/1 group(s) failed; not merging partial results")

    monkeypatch.setattr(svc, "_detect_with_ocr_has", _boom)

    with pytest.raises(HasTextNerUnavailable) as excinfo:
        asyncio.run(svc.detect_with_dual_pipeline("x.png", FileType.IMAGE, 1, ["身份证号"], []))
    assert "ner_unavailable" in str(excinfo.value)


# ---------------------------------------------------------------------------
# AC6：失败页不随交付出厂（#199 既有闸回归钉）+ 清账重跑
# ---------------------------------------------------------------------------
def test_ac6_failed_pages_cleared_on_clean_rerun_after_ner_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """NER 故障页重跑全绿 ⇒ failed_pages 无条件清空（冷审 I1 口径，#199 语义）。"""
    store = _FakeStore()
    queue = _queue(store)
    real_sleep = asyncio.sleep

    async def _flaky(file_id: str, page: int, **kwargs: Any):
        if page == 2 and not getattr(_flaky, "healed", False):
            _flaky.healed = True
            raise HasTextNerUnavailable("ner_unavailable: health check negative within TTL")
        return _boxes_result(page)

    _patch_recognition_io(monkeypatch, queue, pages=2, detect=_flaky)

    async def first() -> None:
        await queue._run_vision(_item(), dict(_VISION_CFG))

    _run(first)
    assert list(_failed_pages_of(store) or {}) == ["2"]

    async def _healthy(file_id: str, page: int, **kwargs: Any) -> dict[str, Any]:
        await real_sleep(0)
        return _boxes_result(page)

    _patch_recognition_io(monkeypatch, queue, pages=2, detect=_healthy)

    async def second() -> None:
        await queue._run_vision(_item(), dict(_VISION_CFG))

    _run(second)
    assert _failed_pages_of(store) == {}, "重跑全成功必须无条件清空失败清单"


def test_ac6_delivery_gate_blocks_on_ner_failed_pages() -> None:
    """交付裁定单一真源：NER 失败页 ⇒ ready_for_delivery=False（AC6 回归钉）。"""
    from app.services.job_management_service import _delivery_blocking_reasons, _is_ready_for_delivery

    failed_pages = [2]
    ready = _is_ready_for_delivery(
        item={"status": JobItemStatus.AWAITING_REVIEW.value},
        has_output=True,
        review_confirmed=True,
        redacted_skip_reason=None,
        recognition_failed_pages=failed_pages,
    )
    assert ready is False
    reasons = _delivery_blocking_reasons(
        item={"status": JobItemStatus.AWAITING_REVIEW.value},
        has_output=True,
        review_confirmed=True,
        redacted_skip_reason=None,
        recognition_failed_pages=failed_pages,
    )
    assert "recognition_failed_pages" in reasons, reasons
