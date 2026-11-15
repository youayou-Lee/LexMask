"""Issue#276：页账双列记账——队列层四终态写入点 + 字段契约（设计稿 §2.2 / §2.7 / §5.1）。

钉住：

* **completed 写点**：``**_visual_fields_from_quality(quality)``（quality 派生在前）
  + ``**page_visual_ledger`` overlay（reason 在后，键集收窄为
  ``{visual_failed_reason}``，r2 F6）；
* **timeout / 异常写点恒零键**（r2 F1：写回仅在检测整体成功后发生，失败页本次
  运行无新账，回读只会命中上一运行的陈旧账 ⇒ 不回读、不编造）；
* **retry-green 写点补 fresh 回读**（r2 F2：``**_visual_fields_from_file_store``；
  M7 承重——漏补则翻绿页被整记录替换清零、静默退化 absent）；
* **验收 1/2（本地形态）**：LA 不可用 ⇒ 每页 ``visual_source="none"`` +
  ``visual_failed_reason="la_unavailable"``、页 status completed、``failed_pages``
  空（回归钉 #256），投影后绿格必带 visual 维度、不得标记「完整完成」。

harness 复用 test_issue139 / test_issue256（同队列/桩形态）。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from test_issue139_pipeline_asyncio_timeout import (
    _FakeStore,
    _item,
    _queue,
    _run,
    _vision_stubs,
)

import app.services.page_status_service as pss
import app.services.redaction_orchestrator as ro
from app.core.errors import AppError

_VISION_CFG: dict[str, Any] = {"visual_feature_types": ["SIGNATURE"], "ocr_has_types": ["姓名"]}


def _visual_column_block(source: str, *, region_count: int = 0,
                         failed_reason: str | None = None) -> dict[str, Any]:
    """镜像 vision_service §2.1 合成块形状（桩按真实落点输出，供 quality 派生面测）。"""
    return {
        "source": source,
        "types": [] if source == "none" else ["SIGNATURE"],
        "requested_types": ["SIGNATURE"],
        "region_count": region_count,
        "failed_reason": failed_reason,
    }


def _detect_with_visual_column(calls: list[dict[str, Any]]):
    """识别桩：按本次调用的 visual 要素回真形 visual_column（镜像编排层行为）。"""

    async def _detect(file_id: str, page: int, **kwargs: Any) -> dict[str, Any]:
        visual = list(kwargs.get("visual_feature_types") or [])
        calls.append({"page": page, "visual": visual, "merge": bool(kwargs.get("merge_existing"))})
        if visual:
            block = _visual_column_block("la", region_count=1)
        else:
            block = _visual_column_block("none")
        return {
            "boxes": [{"id": f"p{page}-1", "page": page, "type": "PERSON", "text": "张三"}],
            "pipeline_status": {"visual_column": block},
            "warnings": [],
        }

    return _detect


def _pages_of(store: _FakeStore) -> dict[str, dict[str, Any]]:
    perf = json.loads(str(store.item.get("performance_json") or "{}"))
    pages = (perf.get("recognition") or {}).get("pages")
    assert isinstance(pages, dict), perf
    return pages


def _failed_pages(store: _FakeStore) -> dict[str, str]:
    perf = json.loads(str(store.item.get("performance_json") or "{}"))
    raw = (perf.get("recognition") or {}).get("failed_pages")
    return {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}


def _la_down_probe() -> None:
    raise AppError(
        status_code=503,
        error_code="visual_service_unavailable",
        message="视觉检测服务（LocateAnything）未启动：本地请运行 ./start_local.sh --with-visual",
    )


# ---------------------------------------------------------------------------
# Q1：验收 1/2（本地）——LA 不可用：completed 写点 quality 派生 + overlay reason
# ---------------------------------------------------------------------------


def test_q1_la_unavailable_pages_record_skipped_and_stay_completed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    _vision_stubs(monkeypatch, pages=3, detect=_detect_with_visual_column(calls))
    monkeypatch.setattr(ro, "ensure_visual_service_available", _la_down_probe)

    store = _FakeStore()
    store.job["config_json"] = json.dumps(_VISION_CFG)
    queue = _queue(store)

    async def _parse_ok(task: Any) -> None:
        return None

    monkeypatch.setattr(queue, "_parse_file", _parse_ok)
    _run(lambda: queue._run_recognition(_item()))

    # 单段剥离路径：每页一次、不带视觉要素（#256 既有语义不回归）
    assert sorted(c["page"] for c in calls) == [1, 2, 3]
    assert all(c["visual"] == [] for c in calls)

    pages = _pages_of(store)
    for page_no in ("1", "2", "3"):
        entry = pages[page_no]
        assert entry["status"] == "completed", entry
        assert entry["visual_source"] == "none", entry
        assert entry["visual_types"] == [], entry
        assert entry["visual_region_count"] == 0, entry
        assert entry["visual_failed_reason"] == "la_unavailable", entry
    assert _failed_pages(store) == {}, "验收 2：不得因 LA 缺席判失败（回归 #256）"

    # 投影：skipped 三态 + 卷级计数（验收 1 判据）
    status = pss.build_page_status(
        {"id": store.item["id"], "job_id": store.item["job_id"],
         "file_id": store.item.get("file_id"), "status": "awaiting_review",
         "file_type": "image", "performance": json.loads(str(store.item["performance_json"]))}
    )
    assert status["visual_summary"]["skipped"] == 3
    for page in status["pages"]:
        assert page["state"] == "success"
        assert page["detail"]["visual"]["state"] == "skipped"


# ---------------------------------------------------------------------------
# Q2：LA 可用两段式——第二段 merge 写点带 la 账，终态无 reason
# ---------------------------------------------------------------------------


def test_q2_la_up_two_stage_final_ledger_is_la_done(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    _vision_stubs(monkeypatch, pages=2, detect=_detect_with_visual_column(calls))
    store = _FakeStore()
    store.job["config_json"] = json.dumps(_VISION_CFG)
    queue = _queue(store)
    _run(lambda: queue._run_vision(_item(), dict(_VISION_CFG)))

    merge_calls = [c for c in calls if c["merge"]]
    assert len(merge_calls) == 2, calls

    pages = _pages_of(store)
    for page_no in ("1", "2"):
        entry = pages[page_no]
        assert entry["status"] == "completed"
        assert entry["visual_source"] == "la", entry
        assert entry["visual_types"] == ["SIGNATURE"], entry
        assert entry["visual_region_count"] == 1, entry
        assert "visual_failed_reason" not in entry, entry


# ---------------------------------------------------------------------------
# Q3/Q4：timeout / 异常写点恒零键（r2 F1：不回读、不编造、不残留旧账）
# ---------------------------------------------------------------------------


def test_q3_timeout_page_has_zero_visual_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _timeout_detect(file_id: str, page: int, **kwargs: Any) -> dict[str, Any]:
        raise TimeoutError("page wall")

    _vision_stubs(monkeypatch, pages=1, detect=_timeout_detect)
    store = _FakeStore()
    store.job["config_json"] = json.dumps(_VISION_CFG)
    queue = _queue(store)
    _run(lambda: queue._run_vision(_item(), dict(_VISION_CFG)))

    entry = _pages_of(store)["1"]
    assert entry["status"] == "timeout"
    for key in ("visual_source", "visual_types", "visual_region_count", "visual_failed_reason"):
        assert key not in entry, f"timeout 页不得带视觉字段（r2 F1），got {key}"


def test_q4_failed_page_has_zero_visual_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _boom_detect(file_id: str, page: int, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("ner boom")

    _vision_stubs(monkeypatch, pages=2, detect=_boom_detect)
    store = _FakeStore()
    store.job["config_json"] = json.dumps(_VISION_CFG)
    queue = _queue(store)
    _run(lambda: queue._run_vision(_item(), dict(_VISION_CFG)))

    pages = _pages_of(store)
    assert {k: v["status"] for k, v in pages.items()} == {"1": "failed", "2": "failed"}
    for page_no, entry in pages.items():
        for key in ("visual_source", "visual_types", "visual_region_count", "visual_failed_reason"):
            assert key not in entry, f"failed 页 {page_no} 不得带视觉字段（r2 F1），got {key}"
    assert "1" in _failed_pages(store) and "2" in _failed_pages(store)


# ---------------------------------------------------------------------------
# Q5：retry-green 写点 fresh 回读（r2 F2 / M7 承重）——翻绿页带本次运行新账
# ---------------------------------------------------------------------------


def test_q5_retry_green_reads_fresh_visual_account(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_issue257_page_retry_task import _Harness

    # 重试路径会挂页事件上下文并写 JSONL——导流到 tmp，避免污染真实 DATA_DIR/logs
    from app.core.config import settings

    monkeypatch.setattr(settings, "PAGE_EVENT_LOG_DIR", str(tmp_path / "logs"))
    from app.services import page_event_log as pel

    pel.reset_for_tests()

    harness = _Harness(tmp_path, page_count=6)
    # 旧账：页 5 带上一次运行的陈旧 visual 字段（source=none + la_unavailable）
    harness.store.update_item_performance(
        harness.iid,
        {"recognition": {"pages": {"5": {
            "visual_source": "none",
            "visual_failed_reason": "la_unavailable",
            "visual_region_count": 0,
        }}}},
    )

    async def _detect(file_id: str, page: int, **kwargs: Any) -> dict[str, Any]:
        harness.calls.append((file_id, page, kwargs))
        # 镜像编排层成功路径：detect 整体成功 ⇒ 先写回 file_store vision_quality
        from app.services.file_management_service import file_store

        info = file_store.get(file_id) or {"file_id": file_id}
        vision_quality = dict(info.get("vision_quality") or {})
        vision_quality[page] = {
            "pipeline_status": {"visual_column": _visual_column_block("la", region_count=2)},
        }
        info["vision_quality"] = vision_quality
        file_store.set(file_id, info)
        return {"bounding_boxes": [], "entities": []}

    import app.services.file_operations as fo

    monkeypatch.setattr(fo, "vision_detect", _detect)

    asyncio.run(harness.queue._run_page_retry(harness.task([5])))

    page5 = harness.perf()["recognition"]["pages"]["5"]
    assert page5["status"] == "completed"
    assert page5["visual_source"] == "la", page5
    assert page5["visual_types"] == ["SIGNATURE"], page5
    assert page5["visual_region_count"] == 2, page5
    assert "visual_failed_reason" not in page5, "旧账不得残留（整记录替换 + 新值）"
    pel.reset_for_tests()


# ---------------------------------------------------------------------------
# Q6：page_done 事件带视觉终态（§2.4 白名单扩容后的生产者面）
# ---------------------------------------------------------------------------


@pytest.fixture()
def log_env(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    from app.core.config import settings
    from app.services import page_event_log as pel

    monkeypatch.setattr(settings, "PAGE_EVENT_LOG_ENABLED", True)
    monkeypatch.setattr(settings, "PAGE_EVENT_LOG_DIR", str(tmp_path / "logs"))
    pel.reset_for_tests()
    yield tmp_path / "logs"
    pel.reset_for_tests()


def _page_done_events(log_env: Any) -> list[dict[str, Any]]:
    path = log_env / "job-0001" / "item-0001.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and json.loads(line)["event"] == "page_done"
    ]


def test_q6_page_done_event_carries_visual_fields(
    log_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []
    _vision_stubs(monkeypatch, pages=1, detect=_detect_with_visual_column(calls))
    store = _FakeStore()
    store.job["config_json"] = json.dumps(_VISION_CFG)
    queue = _queue(store)

    async def _parse_ok(task: Any) -> None:
        return None

    monkeypatch.setattr(queue, "_parse_file", _parse_ok)
    # 走 _run_recognition 入口：#279 的页事件上下文在识别入口挂（_run_vision 直调
    # 不挂上下文，log_event 会 dropped_no_context）。
    _run(lambda: queue._run_recognition(_item()))

    done = _page_done_events(log_env)
    assert done, "page_done 事件必须落盘"
    assert done[-1]["visual_source"] == "la"
    assert "visual_failed_reason" not in done[-1]


def test_q6b_page_done_event_carries_la_unavailable_reason(
    log_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []
    _vision_stubs(monkeypatch, pages=1, detect=_detect_with_visual_column(calls))
    monkeypatch.setattr(ro, "ensure_visual_service_available", _la_down_probe)
    store = _FakeStore()
    store.job["config_json"] = json.dumps(_VISION_CFG)
    queue = _queue(store)

    async def _parse_ok(task: Any) -> None:
        return None

    monkeypatch.setattr(queue, "_parse_file", _parse_ok)
    _run(lambda: queue._run_recognition(_item()))

    done = _page_done_events(log_env)
    assert done, "page_done 事件必须落盘"
    assert done[-1]["visual_source"] == "none"
    assert done[-1]["visual_failed_reason"] == "la_unavailable"


def test_q6c_visual_types_key_rejected_by_whitelist(log_env: Any) -> None:
    """M4 负向钉：visual_types（list）不入事件白名单——携带即拒写整事件。"""
    from app.services import page_event_log

    page_event_log.set_event_context("job-0001", "item-0001", "file-0001")
    page_event_log.set_current_page(1)
    before = page_event_log.stats()["dropped_unknown_field"]
    assert page_event_log.log_event("page_done", visual_types=["official_seal"]) is False
    assert page_event_log.stats()["dropped_unknown_field"] == before + 1
    # 正向：visual_source / visual_failed_reason 两个标量位都在白名单内
    assert page_event_log.log_event("page_done", visual_source="la") is True
    assert page_event_log.log_event(
        "page_done", visual_source="none", visual_failed_reason="la_unavailable"
    ) is True


# ---------------------------------------------------------------------------
# Q7：extract helper 契约（无块零键 / failed_reason 仅有值时带键）
# ---------------------------------------------------------------------------


def test_q7_visual_fields_from_quality_shapes() -> None:
    from app.services.task_queue_pipelines import _visual_fields_from_quality

    assert _visual_fields_from_quality({}) == {}
    assert _visual_fields_from_quality({"pipeline_status": {}}) == {}
    fields = _visual_fields_from_quality(
        {"pipeline_status": {"visual_column": {
            "source": "la", "types": ["SIGNATURE"], "region_count": 2, "failed_reason": None,
        }}}
    )
    assert fields == {
        "visual_source": "la",
        "visual_types": ["SIGNATURE"],
        "visual_region_count": 2,
    }
    fields_err = _visual_fields_from_quality(
        {"pipeline_status": {"visual_column": {
            "source": "la", "types": [], "region_count": 0, "failed_reason": "visual_stage_error",
        }}}
    )
    assert fields_err["visual_failed_reason"] == "visual_stage_error"


def test_q7b_visual_fields_from_file_store_reads_vision_quality(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.file_management_service import file_store
    from app.services.task_queue_pipelines import _visual_fields_from_file_store

    file_store.set("file-276", {
        "file_id": "file-276",
        "vision_quality": {2: {"pipeline_status": {"visual_column": {
            "source": "la", "types": ["SIGNATURE"], "region_count": 3, "failed_reason": None,
        }}}},
    })
    try:
        fields = _visual_fields_from_file_store("file-276", 2)
        assert fields == {
            "visual_source": "la", "visual_types": ["SIGNATURE"], "visual_region_count": 3,
        }
        # 无账 ⇒ 零键
        assert _visual_fields_from_file_store("file-276", 9) == {}
        assert _visual_fields_from_file_store("file-missing", 1) == {}
    finally:
        file_store.pop("file-276")
