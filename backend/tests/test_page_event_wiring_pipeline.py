# Copyright 2026 LexMask Contributors

"""Issue#279 A2.3：run_page 页终态事件接线 + 上下文 + DB error_type/page_retry_counts 镜像。

四层（计划裁定：沿仓内既有 queue pipeline 测试形态，不自建平行框架）：

* 管线集成级 —— `_run_recognition` 全链驱动（复用 test_issue139 的 _FakeStore/_queue
  harness + monkeypatch ``file_operations.vision_detect`` 三态），真上下文传递：
  入口 set_event_context → run_page（asyncio.create_task 子协程）内 log_event
  落盘带 job/item/page；三态各钉事件与 DB ``error_type`` 镜像；
* `_run_page_retry` —— 复用 test_issue257_page_retry_task 的真 JobStore harness，
  钉 retry_done / page_done(source=retry) / page_failed(stage=retry) 与
  ``recognition.page_retry_counts`` 的 replace 纪律（增量、不残留旧键）；
* `submit_page_retry` —— monkeypatch enqueue，钉 retry_started 每页一条
  attempt=旧计数+1、trigger="manual"，及 submit 期 store 读失败 fail-open；
* `retry_count_of` 直测（正常/缺失/垃圾/无方法替身）。

fail-open 钉法：事件开关关闭时 DB 镜照写、识别行为不变。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from test_issue139_pipeline_asyncio_timeout import (
    _ITEM_ID,
    _JOB_ID,
    _Fake310AsyncTimeoutError,
    _FakeStore,
    _item,
    _patch_asyncio_timeout,
    _queue,
    _run,
)
from test_issue257_page_retry_task import _boxes_by_page, _Harness, _patch_detect

from app.core.config import settings
from app.services import page_event_log as pel
from app.services import page_retry_service
from app.services.job_models import JobItemStatus, JobType
from app.services.job_store import JobStore
from app.services.task_queue_pipelines import retry_count_of

_VISION_CFG: dict[str, Any] = {"visual_feature_types": [], "ocr_has_types": ["姓名"]}


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------
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


class _PerfFakeStore(_FakeStore):
    """issue139 替身 + ``get_item_performance``（真库同形：从 performance_json 回读）。

    仅用于 attempts>1 的用例——基础替身没有该方法，run_page 的 retry_count_of
    走 fail-open 按 0 计（这正是无账首跑的口径）。
    """

    def get_item_performance(self, item_id: str) -> dict[str, Any]:
        return json.loads(self.item.get("performance_json") or "{}")


def _patch_recognition_io(
    monkeypatch: pytest.MonkeyPatch,
    queue: Any,
    *,
    pages: int = 1,
    detect: Any,
) -> None:
    """`_run_recognition` → `_run_ner_or_vision` → `_run_vision` 全链依赖打桩。

    沿 test_issue139._vision_stubs 同款（image 类型走视觉线）+ 解析段短路。
    """
    import app.services.file_operations as fo
    import app.services.redaction_orchestrator as ro

    monkeypatch.setattr(
        fo,
        "get_file_info",
        lambda file_id: {"file_id": file_id, "page_count": pages, "file_type": "image"},
    )
    monkeypatch.setattr(fo, "vision_detect", detect)

    async def _la_available() -> None:
        return None

    monkeypatch.setattr(ro, "ensure_visual_service_available", _la_available)

    async def _no_parse(task: Any) -> None:
        return None

    monkeypatch.setattr(queue, "_parse_file", _no_parse)


# ---------------------------------------------------------------------------
# retry_count_of 直测（计划 Step 1 ①）
# ---------------------------------------------------------------------------
class TestRetryCountOf:
    def test_normal_missing_and_garbage(self, tmp_path) -> None:
        store = JobStore(str(tmp_path / "jobs.sqlite3"))
        jid = store.create_job(job_type=JobType.TEXT_BATCH, title="t", owner_id="admin")
        iid = store.add_item(jid, "file-1")
        store.update_item_performance(
            iid, {"recognition": {"page_retry_counts": {"3": 2, "bad": "not-a-number"}}}
        )
        assert retry_count_of(store, iid, 3) == 2
        assert retry_count_of(store, iid, 4) == 0  # 缺失页
        assert retry_count_of(store, iid, "bad") == 0  # 值垃圾
        # 整体垃圾（非 dict）
        store.update_item_performance(
            iid, {"recognition": {"page_retry_counts": "garbage"}}
        )
        assert retry_count_of(store, iid, 3) == 0
        # 条目缺失（真库 KeyError）
        assert retry_count_of(store, "no-such-item", 1) == 0

    def test_store_without_the_method_counts_zero(self, tmp_path) -> None:
        """替身 store（无 get_item_performance）按 0——run_page 里绝不上抛。"""
        assert retry_count_of(object(), "item-x", 1) == 0

    def test_none_store_counts_zero(self) -> None:
        assert retry_count_of(None, "item-x", 1) == 0


# ---------------------------------------------------------------------------
# 管线集成级：_run_recognition → run_page 三态（计划 Step 1 ①②）
# ---------------------------------------------------------------------------
class TestRunRecognitionPageEvents:
    def test_timeout_page_emits_page_failed_and_db_mirror(
        self, log_env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_asyncio_timeout(monkeypatch)
        store = _FakeStore()
        queue = _queue(store)

        async def _detect(file_id: str, page: int, **kwargs: Any) -> dict[str, Any]:
            raise _Fake310AsyncTimeoutError()

        _patch_recognition_io(monkeypatch, queue, detect=_detect)
        _run(lambda: queue._run_recognition(_item()))

        failed = _of(_events(log_env), "page_failed")
        assert len(failed) == 1
        event = failed[0]
        # 上下文传递：入口 set_event_context → 子协程（create_task）事件带 job/item
        assert event["job_id"] == _JOB_ID
        assert event["item_id"] == _ITEM_ID
        assert event["page"] == 1
        assert event["stage"] == "视觉识别"
        assert event["error_type"] == "PageTimeout"
        assert "timeout after" in event["message_sanitized"]
        assert event["traceback_frames"] == []  # 超时无堆栈
        assert event["attempts"] == 1  # 无重试账 → 0+1（首跑=1）
        assert isinstance(event["ms"], int) and event["ms"] >= 0
        # DB 镜像：timeout 页记录带 error_type
        entry = store.page_entry(1)
        assert entry.get("status") == "timeout"
        assert entry.get("error_type") == "PageTimeout"
        # 超时页不发成功事件
        assert _of(_events(log_env), "page_done") == []
        assert _of(_events(log_env), "ocr_done") == []

    def test_exception_page_emits_page_failed_with_frames(
        self, log_env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = _FakeStore()
        queue = _queue(store)

        async def _detect(file_id: str, page: int, **kwargs: Any) -> dict[str, Any]:
            raise ValueError("页内识别崩了")

        _patch_recognition_io(monkeypatch, queue, detect=_detect)
        _run(lambda: queue._run_recognition(_item()))

        failed = _of(_events(log_env), "page_failed")
        assert len(failed) == 1
        event = failed[0]
        assert event["page"] == 1
        assert event["stage"] == "视觉识别"
        assert event["error_type"] == "ValueError"
        assert event["message_sanitized"] == "页内识别崩了"
        # 代码帧堆栈：帧在测试文件（raise 现场），且不含消息文本
        assert any("test_page_event_wiring_pipeline" in f for f in event["traceback_frames"])
        assert all("页内识别崩了" not in f for f in event["traceback_frames"])
        assert event["attempts"] == 1
        # DB 镜像：failed 页记录带 error_type（与既有 error 并列）
        entry = store.page_entry(1)
        assert entry.get("status") == "failed"
        assert entry.get("error_type") == "ValueError"
        assert "ValueError" in str(entry.get("error"))

    def test_completed_page_emits_ocr_ner_page_done(
        self, log_env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = _FakeStore()
        queue = _queue(store)

        async def _detect(file_id: str, page: int, **kwargs: Any) -> dict[str, Any]:
            return {
                "boxes": [],
                "duration_ms": {"ocr": 12, "ner": 30},
                "pipeline_status": {
                    "ocr_has": {"ran": True, "region_count": 3, "duration_ms": 55},
                    "visual_features": {"ran": True, "region_count": 2},
                },
            }

        _patch_recognition_io(monkeypatch, queue, detect=_detect)
        _run(lambda: queue._run_recognition(_item()))

        events = _events(log_env)
        ocr = _of(events, "ocr_done")
        assert len(ocr) == 1
        assert ocr[0]["page"] == 1
        assert ocr[0]["ms"] == 55
        assert ocr[0]["regions"] == 3
        assert ocr[0]["source"] == "ocr"
        ner = _of(events, "ner_done")
        assert len(ner) == 1
        assert ner[0]["ms"] == 55 and ner[0]["regions"] == 3
        assert "source" not in ner[0]  # ner_done 无 source 字段
        done = _of(events, "page_done")
        assert len(done) == 1
        assert done[0]["page"] == 1
        assert done[0]["ms"] >= 0
        assert done[0]["stage_ms"] == {"ocr": 12, "ner": 30}
        assert done[0]["regions"] == 5  # ocr 3 + visual 2
        assert done[0]["source"] == "page_pipeline"  # 无 docline 链
        # 成功页 DB 记录不带 error_type
        assert "error_type" not in store.page_entry(1)
        assert store.page_entry(1).get("status") == "completed"

    def test_page_done_source_docline_when_docline_ran(
        self, log_env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = _FakeStore()
        queue = _queue(store)

        async def _detect(file_id: str, page: int, **kwargs: Any) -> dict[str, Any]:
            return {
                "boxes": [],
                "pipeline_status": {
                    "ocr_has": {"ran": True, "region_count": 1, "duration_ms": 5},
                    "text_layer_doc": {"ran": True, "verdict": "ok"},
                },
            }

        _patch_recognition_io(monkeypatch, queue, detect=_detect)
        _run(lambda: queue._run_recognition(_item()))

        done = _of(_events(log_env), "page_done")
        assert len(done) == 1
        assert done[0]["source"] == "docline"
        assert done[0]["regions"] == 1  # visual_features 缺席按 0

    def test_attempts_mirror_prior_retry_counts(
        self, log_env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """attempts = page_retry_counts 里该页既有次数 + 1。"""
        _patch_asyncio_timeout(monkeypatch)
        store = _PerfFakeStore()
        store.update_item_performance(
            _ITEM_ID, {"recognition": {"page_retry_counts": {"1": 2}}}
        )
        queue = _queue(store)

        async def _detect(file_id: str, page: int, **kwargs: Any) -> dict[str, Any]:
            raise ValueError("again")

        _patch_recognition_io(monkeypatch, queue, detect=_detect)
        _run(lambda: queue._run_recognition(_item()))

        failed = _of(_events(log_env), "page_failed")
        assert len(failed) == 1
        assert failed[0]["attempts"] == 3

    def test_context_cleared_after_recognition(
        self, log_env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """finally 的 clear_event_context：跑完后裸写事件被拒（dropped_no_context）。"""
        store = _FakeStore()
        queue = _queue(store)

        async def _detect(file_id: str, page: int, **kwargs: Any) -> dict[str, Any]:
            return {"boxes": []}

        _patch_recognition_io(monkeypatch, queue, detect=_detect)
        _run(lambda: queue._run_recognition(_item()))

        before = pel.stats()["dropped_no_context"]
        done_before = len(_of(_events(log_env), "page_done"))
        assert pel.log_event("page_done", page=1, ms=1) is False
        assert pel.stats()["dropped_no_context"] == before + 1
        # 清上下文后的写不落盘（事件条数不变）
        assert len(_of(_events(log_env), "page_done")) == done_before

    def test_events_disabled_db_mirror_still_written(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """fail-open：事件开关关闭 → 事件丢弃计数，DB 镜像照写、识别行为不变。"""
        monkeypatch.setattr(settings, "PAGE_EVENT_LOG_ENABLED", False)
        pel.reset_for_tests()
        try:
            _patch_asyncio_timeout(monkeypatch)
            store = _FakeStore()
            queue = _queue(store)

            async def _detect(file_id: str, page: int, **kwargs: Any) -> dict[str, Any]:
                raise ValueError("boom")

            _patch_recognition_io(monkeypatch, queue, detect=_detect)
            _run(lambda: queue._run_recognition(_item()))

            assert pel.stats()["dropped_disabled"] >= 1
            entry = store.page_entry(1)
            assert entry.get("status") == "failed"
            assert entry.get("error_type") == "ValueError"  # DB 镜像不依赖事件开关
        finally:
            pel.reset_for_tests()


# ---------------------------------------------------------------------------
# _run_page_retry：retry_done / page_done(retry) / page_failed(retry) + counts
# ---------------------------------------------------------------------------
def _patch_timeout_detect(monkeypatch: pytest.MonkeyPatch, harness: _Harness) -> None:
    import app.services.file_operations as fo

    async def _detect(file_id: str, page: int, **kwargs: Any) -> dict[str, Any]:
        harness.calls.append((file_id, page, kwargs))
        raise TimeoutError("page retry timed out")

    monkeypatch.setattr(fo, "vision_detect", _detect)


class TestRunPageRetryEvents:
    def test_success_emits_retry_done_and_page_done_retry(
        self, tmp_path, log_env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness = _Harness(tmp_path)
        _boxes_by_page(harness, {"5": []})
        _patch_detect(monkeypatch, harness, behaviour="ok")

        asyncio.run(harness.queue._run_page_retry(harness.task([5])))

        events = _events(log_env, job_id=harness.jid, item_id=harness.iid)
        done = _of(events, "retry_done")
        assert len(done) == 1
        assert done[0]["page"] == 5
        assert done[0]["attempt"] == 1
        assert done[0]["outcome"] == "success"
        assert isinstance(done[0]["ms"], int) and done[0]["ms"] >= 0
        page_done = _of(events, "page_done")
        assert len(page_done) == 1
        assert page_done[0]["page"] == 5
        assert page_done[0]["source"] == "retry"
        assert page_done[0]["regions"] is None
        assert page_done[0]["stage_ms"] == {}
        assert page_done[0]["ms"] == done[0]["ms"]  # 同一 elapsed 口径
        assert _of(events, "page_failed") == []  # 成功不发 page_failed
        recognition = harness.perf()["recognition"]
        assert recognition["page_retry_counts"] == {"5": 1}
        assert recognition["page_retry_pages"] == []
        assert "5" not in (recognition.get("failed_pages") or {})

    def test_failure_emits_retry_done_failed_and_page_failed(
        self, tmp_path, log_env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness = _Harness(tmp_path)
        _patch_detect(monkeypatch, harness, behaviour="fail")  # ValueError("ner boom again")

        asyncio.run(harness.queue._run_page_retry(harness.task([5])))

        events = _events(log_env, job_id=harness.jid, item_id=harness.iid)
        done = _of(events, "retry_done")
        assert len(done) == 1
        assert done[0]["outcome"] == "failed"
        assert done[0]["attempt"] == 1
        failed = _of(events, "page_failed")
        assert len(failed) == 1
        event = failed[0]
        assert event["page"] == 5
        assert event["stage"] == "retry"
        assert event["error_type"] == "ValueError"
        assert "ner boom again" in event["message_sanitized"]
        # raise 现场在复用的替身文件里（_patch_detect 来自 test_issue257）——
        # 帧必须指向代码现场且不含消息文本
        assert any("test_issue257_page_retry_task.py" in f for f in event["traceback_frames"])
        assert all("ner boom again" not in f for f in event["traceback_frames"])
        assert event["attempts"] == 1  # attempts=index（第 1 轮重试）
        recognition = harness.perf()["recognition"]
        assert recognition["page_retry_counts"] == {"5": 1}
        assert "5" in (recognition.get("failed_pages") or {})  # 失败账保持

    def test_timeout_emits_page_failed_pagetimeout(
        self, tmp_path, log_env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness = _Harness(tmp_path)
        _patch_timeout_detect(monkeypatch, harness)

        asyncio.run(harness.queue._run_page_retry(harness.task([5])))

        events = _events(log_env, job_id=harness.jid, item_id=harness.iid)
        failed = _of(events, "page_failed")
        assert len(failed) == 1
        event = failed[0]
        assert event["stage"] == "retry"
        assert event["error_type"] == "PageTimeout"
        assert event["traceback_frames"] == []
        assert "retry timeout after" in event["message_sanitized"]
        assert event["attempts"] == 1
        done = _of(events, "retry_done")
        assert len(done) == 1 and done[0]["outcome"] == "failed"
        assert harness.perf()["recognition"]["page_retry_counts"] == {"5": 1}

    def test_counts_increment_from_prior_counts_and_keep_other_pages(
        self, tmp_path, log_env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """counts 在既有账上逐页 +1（其余页保留、被重试页旧值递增）。"""
        harness = _Harness(tmp_path)
        harness.store.update_item_performance(
            harness.iid, {"recognition": {"page_retry_counts": {"5": 2, "9": 5}}}
        )
        _boxes_by_page(harness, {"5": []})
        _patch_detect(monkeypatch, harness, behaviour="ok")

        asyncio.run(harness.queue._run_page_retry(harness.task([5])))

        recognition = harness.perf()["recognition"]
        assert recognition["page_retry_counts"] == {"5": 3, "9": 5}
        # 成功重试不发 page_failed
        assert _of(_events(log_env, job_id=harness.jid, item_id=harness.iid), "page_failed") == []

    def test_multi_page_retry_emits_per_page(
        self, tmp_path, log_env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness = _Harness(tmp_path)
        harness.store.update_item_performance(
            harness.iid,
            {"recognition": {"failed_pages": {"5": "ocr boom", "7": "ner boom"}}},
        )
        _boxes_by_page(harness, {"5": [], "7": []})
        _patch_detect(monkeypatch, harness, behaviour="ok")

        asyncio.run(harness.queue._run_page_retry(harness.task([5, 7])))

        events = _events(log_env, job_id=harness.jid, item_id=harness.iid)
        done = _of(events, "retry_done")
        assert [(e["page"], e["attempt"], e["outcome"]) for e in done] == [
            (5, 1, "success"),
            (7, 2, "success"),
        ]
        assert [(e["page"], e["source"]) for e in _of(events, "page_done")] == [
            (5, "retry"),
            (7, "retry"),
        ]
        assert harness.perf()["recognition"]["page_retry_counts"] == {"5": 1, "7": 1}


# ---------------------------------------------------------------------------
# submit_page_retry：retry_started（计划 Step 1 ③）
# ---------------------------------------------------------------------------
def _submit_store(tmp_path, *, counts: dict[str, int] | None = None):
    store = JobStore(str(tmp_path / "jobs.sqlite3"))
    jid = store.create_job(job_type=JobType.TEXT_BATCH, title="t", owner_id="admin")
    iid = store.add_item(jid, "file-1")
    store.update_item_status(iid, JobItemStatus.PROCESSING)
    store.update_item_status(iid, JobItemStatus.AWAITING_REVIEW)
    perf: dict[str, Any] = {
        "recognition": {
            "page_count": 10,
            "failed_pages": {"5": "ocr boom", "7": "ner boom"},
        }
    }
    if counts is not None:
        perf["recognition"]["page_retry_counts"] = counts
    store.update_item_performance(iid, perf)
    return store, jid, iid


class TestSubmitPageRetryEvents:
    def test_emits_retry_started_per_page_with_next_attempt(
        self, tmp_path, log_env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store, jid, iid = _submit_store(tmp_path, counts={"5": 2})
        enqueued: list[Any] = []

        result = asyncio.run(
            page_retry_service.submit_page_retry(
                store=store,
                job=store.get_job(jid),
                item=store.get_item(iid),
                pages=[5, 7],
                enqueue=lambda task: enqueued.append(task),
            )
        )

        assert result["queued_pages"] == [5, 7]
        assert len(enqueued) == 1
        started = _of(_events(log_env, job_id=jid, item_id=iid), "retry_started")
        assert [(e["page"], e["attempt"]) for e in started] == [(5, 3), (7, 1)]
        assert all(e["trigger"] == "manual" for e in started)
        assert all(e["job_id"] == jid and e["item_id"] == iid for e in started)
        # submit 只写标记，不动 counts 账
        assert store.get_item_performance(iid)["recognition"]["page_retry_counts"] == {"5": 2}

    def test_first_attempt_is_one_when_no_counts(
        self, tmp_path, log_env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store, jid, iid = _submit_store(tmp_path)
        asyncio.run(
            page_retry_service.submit_page_retry(
                store=store,
                job=store.get_job(jid),
                item=store.get_item(iid),
                pages=[5],
                enqueue=lambda task: None,
            )
        )
        started = _of(_events(log_env, job_id=jid, item_id=iid), "retry_started")
        assert [(e["page"], e["attempt"]) for e in started] == [(5, 1)]

    def test_fail_open_when_perf_unreadable(
        self, tmp_path, log_env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """store 读账失败：提交链不受影响（fail-open），事件缺账按 1。"""
        store, jid, iid = _submit_store(tmp_path)
        enqueued: list[Any] = []

        class _NoPerfStore:
            def __getattr__(self, name):  # 其余方法转真 store
                return getattr(store, name)

        def _boom(item_id: str) -> dict[str, Any]:
            raise RuntimeError("perf unreadable")

        wrapper = _NoPerfStore()
        monkeypatch.setattr(wrapper, "get_item_performance", _boom, raising=False)
        result = asyncio.run(
            page_retry_service.submit_page_retry(
                store=wrapper,
                job=store.get_job(jid),
                item=store.get_item(iid),
                pages=[5],
                enqueue=lambda task: enqueued.append(task),
            )
        )
        assert result["queued_pages"] == [5]
        assert len(enqueued) == 1
        started = _of(_events(log_env, job_id=jid, item_id=iid), "retry_started")
        assert [(e["page"], e["attempt"]) for e in started] == [(5, 1)]
