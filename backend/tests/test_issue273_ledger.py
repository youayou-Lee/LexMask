"""Issue#273 Task 10：批量队列页账本 routing 字段（三条收尾路径）。

``performance.recognition.pages.<p>`` 新增（只增、字段缺省不写 None 噪声）：
``text_source`` / ``text_metrics`` / ``stage_ms`` / ``switched_from`` /
``switch_reason`` / ``verify_incomplete``。

* 成功页（completed）：从 ``pipeline_status["page_routing"]``（Task 8 finalize
  合成）提取（A5 正向）；
* 超时/失败页：决策即落账（Task 7）——识别结果不可得，但「本页按什么文本来源
  跑的」在 run_page 之前已写进 file_store ``info["page_routing"][str(page)]``，
  从那里补记（B6）；file 模式下 file_store 也没有 page_routing ⇒ 零键，
  与「file 模式账本无新字段」一致。

harness 复用 test_issue139（同一队列/桩形态；test_issue199 同源）。
"""

from __future__ import annotations

from typing import Any

import pytest
from test_issue139_pipeline_asyncio_timeout import (
    _VISION_CFG,
    _Fake310AsyncTimeoutError,
    _FakeStore,
    _item,
    _patch_asyncio_timeout,
    _queue,
    _run,
    _vision_stubs,
)

# 账本新增字段的全集（test 4 的零噪声判据 + 成功页不回落 file_store 的判据）
_NEW_FIELDS = {
    "text_source",
    "text_metrics",
    "stage_ms",
    "switched_from",
    "switch_reason",
    "verify_incomplete",
}

_TEXT_SOURCE_DOMAIN = {"text_layer", "ocr", "both"}


def _boxes_result(page: int) -> dict[str, Any]:
    return {"boxes": [{"id": f"p{page}-1", "page": page, "type": "PERSON", "text": "张三"}]}


def _ledger_entry(page: int) -> dict[str, Any]:
    """file_store ``info["page_routing"][str(page)]`` 的决策即落账形状（router._judge_and_record）。"""
    return {
        "decision": {
            "page": page,
            "action": "text_layer",
            "needs_verify": False,
            "confidence": 1.0,
            "signals": {"page": page, "chars": 980},
            "reason": "clear_text_layer",
            "routing_version": 1,
        },
        "text_source": "text_layer",
        "text_metrics": {"action": "text_layer", "reason": "clear_text_layer"},
        "decided_at": "2026-11-12T00:00:00+00:00",
    }


def _pipeline_routing_status(page: int) -> dict[str, Any]:
    """``pipeline_status["page_routing"]`` 的 finalize 合成形状（Task 7/T8 契约）。"""
    return {
        "decision": {
            "page": page,
            "action": "text_layer",
            "needs_verify": True,
            "confidence": 0.5,
            "signals": {"page": page, "chars": 402},
            "reason": "boundary_chars_band",
            "routing_version": 1,
        },
        "outcome": {"winner": "text_layer", "arbitration": "pass", "stage_ms": {"evidence_ocr": 120}},
        "text_source": "text_layer",
        "switched_from": None,
        "switch_reason": None,
        "verify_incomplete": False,
        "stage_ms": {"evidence_ocr": 120},
    }


def _stub_vision(
    monkeypatch: pytest.MonkeyPatch,
    *,
    pages: int,
    detect: Any,
    file_info_extra: dict[str, Any] | None = None,
    file_type: str = "image",
) -> None:
    """test_issue139._vision_stubs 的可扩展版：file_info 可带 page_routing 账本。"""
    import app.services.file_operations as fo
    import app.services.redaction_orchestrator as ro

    def _get_file_info(file_id: str) -> dict[str, Any]:
        info: dict[str, Any] = {"file_id": file_id, "page_count": pages, "file_type": file_type}
        if file_info_extra:
            info.update(file_info_extra)
        return info

    monkeypatch.setattr(fo, "get_file_info", _get_file_info)
    monkeypatch.setattr(fo, "vision_detect", detect)

    async def _la_available() -> None:
        return None

    monkeypatch.setattr(ro, "ensure_visual_service_available", _la_available)


# ---------------------------------------------------------------------------
# 1) 成功页：page_routing 在 pipeline_status ⇒ pages.<p> routing 字段齐备（A5 正向）
# ---------------------------------------------------------------------------
def test_completed_page_records_routing_fields_from_pipeline_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _FakeStore()
    queue = _queue(store)

    async def _detect(file_id: str, page: int, **kwargs: Any) -> dict[str, Any]:
        result = _boxes_result(page)
        if page == 1:
            result["pipeline_status"] = {"page_routing": _pipeline_routing_status(page)}
        return result

    _vision_stubs(monkeypatch, pages=2, detect=_detect)

    async def scenario() -> None:
        await queue._run_vision(_item(), dict(_VISION_CFG))

    _run(scenario)
    entry = store.page_entry(1)
    assert entry.get("status") == "completed", store.performance
    # text_source 非空且值域合法
    assert entry.get("text_source") == "text_layer", entry
    assert entry.get("text_source") in _TEXT_SOURCE_DOMAIN, entry
    # text_metrics.action / reason 非空且与决策一致；signals 原样透传
    metrics = entry.get("text_metrics") or {}
    assert metrics.get("action") == "text_layer", entry
    assert metrics.get("reason") == "boundary_chars_band", entry
    assert metrics.get("signals") == {"page": 1, "chars": 402}, entry
    # stage_ms / verify_incomplete 随块落账
    assert entry.get("stage_ms") == {"evidence_ocr": 120}, entry
    assert entry.get("verify_incomplete") is False, entry
    # switched_from/switch_reason 为 None ⇒ 缺省不写（不写 None 噪声）
    assert "switched_from" not in entry, entry
    assert "switch_reason" not in entry, entry
    # 反向：无 page_routing 的成功页（页 2）零新字段——不从 file_store 回落
    clean = store.page_entry(2)
    assert clean.get("status") == "completed", store.performance
    assert not (set(clean) & _NEW_FIELDS), set(clean) & _NEW_FIELDS


# ---------------------------------------------------------------------------
# 2) 超时页：决策已落 file_store ⇒ pages.<p> timeout 记录含 text_source（B6）
# ---------------------------------------------------------------------------
def test_timeout_page_records_routing_fields_from_file_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_asyncio_timeout(monkeypatch)
    store = _FakeStore()
    queue = _queue(store)

    async def _detect(file_id: str, page: int, **kwargs: Any) -> dict[str, Any]:
        if page == 1:
            raise _Fake310AsyncTimeoutError()
        return _boxes_result(page)

    _stub_vision(
        monkeypatch,
        pages=2,
        detect=_detect,
        # 决策即落账：两页的决策都已在 run_page 之前写进 file_store
        file_info_extra={"page_routing": {"1": _ledger_entry(1), "2": _ledger_entry(2)}},
    )

    async def scenario() -> None:
        await queue._run_vision(_item(), dict(_VISION_CFG))  # 页级隔离：不得上抛

    _run(scenario)
    entry = store.page_entry(1)
    assert entry.get("status") == "timeout", store.performance
    assert entry.get("text_source") == "text_layer", entry
    metrics = entry.get("text_metrics") or {}
    assert metrics.get("action") == "text_layer", entry
    assert metrics.get("reason") == "clear_text_layer", entry
    # 反向钉：成功页即使 file_store 里有其决策，也不从 file_store 补记
    #（成功路径只认 pipeline_status；page 2 的 pipeline_status 无路由块 ⇒ 零键）
    done = store.page_entry(2)
    assert done.get("status") == "completed", store.performance
    assert not (set(done) & _NEW_FIELDS), set(done) & _NEW_FIELDS


# ---------------------------------------------------------------------------
# 3) 失败页：同 2——failed 记录含 file_store 已落账的决策证据
# ---------------------------------------------------------------------------
def test_failed_page_records_routing_fields_from_file_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _FakeStore()
    queue = _queue(store)

    async def _detect(file_id: str, page: int, **kwargs: Any) -> dict[str, Any]:
        if page == 1:
            raise ValueError("page boom")
        return _boxes_result(page)

    _stub_vision(
        monkeypatch,
        pages=2,
        detect=_detect,
        file_info_extra={"page_routing": {"1": _ledger_entry(1), "2": _ledger_entry(2)}},
    )

    async def scenario() -> None:
        await queue._run_vision(_item(), dict(_VISION_CFG))

    _run(scenario)
    entry = store.page_entry(1)
    assert entry.get("status") == "failed", store.performance
    assert entry.get("text_source") == "text_layer", entry
    metrics = entry.get("text_metrics") or {}
    assert metrics.get("action") == "text_layer", entry
    assert metrics.get("reason") == "clear_text_layer", entry
    # 门⑥ 既有钉不回退：失败页的 pipeline_status[stage].failed 仍在
    assert (entry.get("pipeline_status") or {}).get("视觉识别", {}).get("failed") is True, entry
    done = store.page_entry(2)
    assert done.get("status") == "completed", store.performance
    assert not (set(done) & _NEW_FIELDS), set(done) & _NEW_FIELDS


# ---------------------------------------------------------------------------
# 4) file 模式：pages.<p> 无新字段（字段缺省，不写 None 噪声）
# ---------------------------------------------------------------------------
def test_file_mode_records_no_routing_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import settings

    monkeypatch.setattr(settings, "RECOGNITION_ROUTING_MODE", "file", raising=False)
    _patch_asyncio_timeout(monkeypatch)
    store = _FakeStore()
    queue = _queue(store)

    async def _detect(file_id: str, page: int, **kwargs: Any) -> dict[str, Any]:
        if page == 1:
            raise _Fake310AsyncTimeoutError()
        return _boxes_result(page)

    # file 模式：file_store 无 page_routing 账本、识别结果无路由块（回滚口径）
    _vision_stubs(monkeypatch, pages=2, detect=_detect)

    async def scenario() -> None:
        await queue._run_vision(_item(), dict(_VISION_CFG))

    _run(scenario)
    assert store.page_statuses() == {"1": "timeout", "2": "completed"}, store.performance
    for patch in store.performance:
        for page, entry in (patch.get("recognition", {}).get("pages") or {}).items():
            overlap = set(entry) & _NEW_FIELDS
            assert not overlap, f"page {page} 出现 file 模式不该有的字段: {overlap}"


# ---------------------------------------------------------------------------
# 5) prime 门双案钉：probe 预热只属 file 模式（page 模式决策面唯一 ⇒ 跳过）
# ---------------------------------------------------------------------------
def _stub_prime_probe(monkeypatch: pytest.MonkeyPatch, calls: list[dict[str, Any]]) -> None:
    """替身 ``vision_service.prime_pdf_text_layer_sparse_probe``（_run_vision 调用时才 import）。"""
    import app.services.vision_service as vs

    async def _prime(file_path: str, file_type: Any, *, page: int = 1) -> dict[str, Any]:
        calls.append({"file_path": file_path, "file_type": file_type, "page": page})
        return {"ran": True, "pages_probed": 1}

    monkeypatch.setattr(vs, "prime_pdf_text_layer_sparse_probe", _prime)


def _recognition_with_probe(store: _FakeStore) -> dict[str, Any]:
    for patch in store.performance:
        rec = patch.get("recognition") or {}
        if "pdf_text_layer_sparse_probe" in rec:
            return rec
    raise AssertionError(f"performance 未记录 pdf_text_layer_sparse_probe: {store.performance}")


def test_page_mode_skips_sparse_probe_prime(monkeypatch: pytest.MonkeyPatch) -> None:
    """page 模式（默认）：pdf_scanned 多页也**不调** probe 预热——决策面唯一。"""
    from app.core.config import settings

    monkeypatch.setattr(settings, "RECOGNITION_ROUTING_MODE", "page", raising=False)
    store = _FakeStore()
    queue = _queue(store)

    async def _detect(file_id: str, page: int, **kwargs: Any) -> dict[str, Any]:
        return _boxes_result(page)

    prime_calls: list[dict[str, Any]] = []
    _stub_prime_probe(monkeypatch, prime_calls)
    _stub_vision(monkeypatch, pages=2, detect=_detect, file_type="pdf_scanned")

    async def scenario() -> None:
        await queue._run_vision(_item(), dict(_VISION_CFG))

    _run(scenario)
    assert prime_calls == [], "page 模式不得预热 sparse probe（文本来源由逐页路由决策指挥）"
    assert _recognition_with_probe(store)["pdf_text_layer_sparse_probe"] == {"ran": False}


def test_file_mode_primes_sparse_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """file 模式（回滚口径）：probe 预热被调用、结果透传进 performance。"""
    from app.core.config import settings

    monkeypatch.setattr(settings, "RECOGNITION_ROUTING_MODE", "file", raising=False)
    store = _FakeStore()
    queue = _queue(store)

    async def _detect(file_id: str, page: int, **kwargs: Any) -> dict[str, Any]:
        return _boxes_result(page)

    prime_calls: list[dict[str, Any]] = []
    _stub_prime_probe(monkeypatch, prime_calls)
    _stub_vision(
        monkeypatch,
        pages=2,
        detect=_detect,
        file_type="pdf_scanned",
        file_info_extra={"file_path": "/store/f-1.pdf"},
    )

    async def scenario() -> None:
        await queue._run_vision(_item(), dict(_VISION_CFG))

    _run(scenario)
    assert len(prime_calls) == 1, prime_calls
    assert prime_calls[0]["page"] == 1
    assert prime_calls[0]["file_path"] == "/store/f-1.pdf"
    assert _recognition_with_probe(store)["pdf_text_layer_sparse_probe"] == {"ran": True, "pages_probed": 1}
