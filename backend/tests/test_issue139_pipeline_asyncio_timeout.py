"""Issue#139 回归：Python 3.10 上页级/管线级超时必须落 timeout 语义，且不得被 I/O 分支吞掉。

根因回顾（云实例 `/root/.venvs/app/bin/python` = 3.10.12 实测）：
- 3.10 里 `asyncio.TimeoutError`（= `asyncio.exceptions.TimeoutError`）与内建
  `TimeoutError` **不是同一个类**（3.11 才合并），也**不是** `OSError` 子类；
- 3.11+ 里两者是同一个类（本机 3.12 恒 `asyncio.TimeoutError is TimeoutError`），
  所以本机**复现不出**该类关系差异；
- `_run_vision` 的页级 `asyncio.wait_for(..., timeout=page_timeout)` 在 3.10 上抛的是
  `asyncio.exceptions.TimeoutError` ⇒ 只写 `except TimeoutError` 的三处（页级、管线级、
  `_run_recognition` 错误梯）都捕不到，页账被记成 `failed`、错误梯落到通用分支。

承重结论只有两条，本文件据此钉住：
1. **只捕内建类会漏** ⇒ 三处必须写 `except (TimeoutError, asyncio.TimeoutError)`；
2. **内建 `TimeoutError` 是 `OSError` 子类**（Python ≥3.3），而 `_run_recognition` 错误梯里
   `except (FileNotFoundError, OSError)` 原本排在 `except TimeoutError` **之前** ⇒ 即使三处
   捕获类型都改对，冒到错误梯的超时仍会被 I/O 分支先吞掉、记成 "recognition I/O error"
   （本机 3.12 上今天就如此）⇒ 超时分支必须重排到 I/O 分支之前。

因为本机复现不出类关系差异，T1/T2/T3 用**版本无关**的替身钉住修复：把
`asyncio.TimeoutError` 换成与内建 `TimeoutError`（及 `OSError`）**无继承关系**的假类，
再让被调用方抛它——只有显式元组捕获才捕得到（= 3.10 语义）。T4/T9 钉住第 2 条（分支顺序），
T5/T6/T10a/T10b 是反向护栏（防「放宽成 `except Exception:`」与「I/O 错误被吃成 timeout」），
T11 是环境前置门。是否 3.10 上真执行由门④的 skipped 计数与 T11 判定（见验收方案 AC3）。

本机（3.12）无 pytest-asyncio，按仓内既有约定（同 `test_issue137_task_queue_worker.py`）
用同步用例 + 内层 `async def scenario()` + `asyncio.run(scenario())`。

验收方案与判定表：`LexMask-workspace/github-issues/issue139-asyncio-timeout-plan.md`。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Coroutine

import pytest

import app.services.task_queue as tq
from app.services.job_store import JobItemStatus

_ITEM_ID = "item-0001"
_JOB_ID = "job-0001"
_FILE_ID = "file-0001"

_VISION_CFG: dict[str, Any] = {"visual_feature_types": [], "ocr_has_types": ["姓名"]}


class _Fake310AsyncTimeoutError(Exception):
    """模拟 Python 3.10 的 `asyncio.TimeoutError`：与内建 `TimeoutError`/`OSError` 无继承关系。"""


def _item(item_id: str = _ITEM_ID) -> tq.TaskItem:
    return tq.TaskItem(job_id=_JOB_ID, item_id=item_id, file_id=_FILE_ID, task_type="recognition")


class _FakeStore:
    """最小 JobStore 替身：记录状态/性能补丁，可选在写进展时抛指定异常。"""

    def __init__(
        self,
        *,
        progress_exc: BaseException | None = None,
        item_status: str = JobItemStatus.PROCESSING.value,
        job_status: str = "processing",
    ) -> None:
        self.item = {"id": _ITEM_ID, "job_id": _JOB_ID, "status": item_status}
        self.job = {"id": _JOB_ID, "status": job_status, "config_json": "{}"}
        self.status_calls: list[dict[str, Any]] = []
        self.performance: list[dict[str, Any]] = []
        self.progress_calls: list[dict[str, Any]] = []
        self.progress_exc = progress_exc

    # --- 读 ---
    def get_job(self, job_id: str) -> dict[str, Any] | None:
        return self.job if job_id == _JOB_ID else None

    def get_item(self, item_id: str) -> dict[str, Any] | None:
        return self.item if item_id == _ITEM_ID else None

    def list_items(self, job_id: str) -> list[dict[str, Any]]:
        return [self.item] if job_id == _JOB_ID else []

    # --- 写 ---
    def update_item_status(self, item_id: str, status: str, error_message: str | None = None) -> None:
        self.status_calls.append({"item_id": item_id, "status": status, "error_message": error_message})

    def update_item_performance(self, item_id: str, patch: dict[str, Any]) -> None:
        self.performance.append(patch)

    def update_item_progress(self, *args: Any, **kwargs: Any) -> None:
        self.progress_calls.append({"args": args, "kwargs": kwargs})
        # `_run_vision` 在进入 try 之前也会写一次「Vision recognition queued…」进展；
        # 注入点必须落在 try 块内（= 页循环之后的那次 complete 进展），否则异常不会被捕获。
        if self.progress_exc is not None and "complete" in str(kwargs.get("message", "")):
            raise self.progress_exc

    def update_job_status(self, *args: Any, **kwargs: Any) -> None:
        return None

    def touch_job_updated(self, *args: Any, **kwargs: Any) -> None:
        return None

    # --- 断言辅助 ---
    def page_entry(self, page: str | int) -> dict[str, Any]:
        merged: dict[str, Any] = {}
        for patch in self.performance:
            entry = (patch.get("recognition", {}).get("pages", {}) or {}).get(str(page))
            if isinstance(entry, dict):
                merged.update(entry)
        return merged

    def page_statuses(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for patch in self.performance:
            pages = patch.get("recognition", {}).get("pages", {}) or {}
            for page, entry in pages.items():
                if isinstance(entry, dict) and "status" in entry:
                    out[str(page)] = str(entry["status"])
        return out

    def failed_error_message(self) -> str | None:
        for call in reversed(self.status_calls):
            if call["status"] == JobItemStatus.FAILED.value:
                return call["error_message"]
        return None


def _queue(store: _FakeStore) -> tq.SimpleTaskQueue:
    queue = tq.SimpleTaskQueue(concurrency=1)
    queue._get_store = lambda: store  # type: ignore[method-assign]
    return queue


def _patch_asyncio_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """把 `asyncio.TimeoutError` 换成 3.10 语义的替身（`tqp.asyncio` 与 `asyncio` 同一模块对象）。"""
    monkeypatch.setattr(asyncio, "TimeoutError", _Fake310AsyncTimeoutError)


def _vision_stubs(monkeypatch: pytest.MonkeyPatch, *, pages: int, detect: Callable[..., Any]) -> None:
    """把 `_run_vision` 的两个运行期依赖换成桩（该函数在调用时才 import）。"""
    import app.services.file_operations as fo

    monkeypatch.setattr(
        fo, "get_file_info", lambda file_id: {"file_id": file_id, "page_count": pages, "file_type": "image"}
    )
    monkeypatch.setattr(fo, "vision_detect", detect)


def _run(coro_factory: Callable[[], Coroutine[Any, Any, Any]]) -> Any:
    return asyncio.run(coro_factory())


# ---------------------------------------------------------------------------
# T0：夹具自检（非 AC 条目，只防替身写错导致后面全空转）
# ---------------------------------------------------------------------------
def test_t0_fake_stand_in_is_not_a_timeout_subclass() -> None:
    assert not issubclass(_Fake310AsyncTimeoutError, TimeoutError), "替身必须与内建 TimeoutError 无继承关系"
    assert not issubclass(_Fake310AsyncTimeoutError, OSError), "替身必须与 OSError 无继承关系（3.10 语义）"


# ---------------------------------------------------------------------------
# T11：环境前置门（本机 3.11+ 跳过；3.10 上必须真执行且通过）
# ---------------------------------------------------------------------------
@pytest.mark.skipif(
    asyncio.TimeoutError is TimeoutError,
    reason="环境前置门：本机 3.11+ 两类已合并 ⇒ 本用例只在 3.10 上有意义（3.10 上必须真执行并通过）",
)
def test_t11_environment_gate_asyncio_timeout_is_distinct() -> None:
    assert asyncio.TimeoutError is not TimeoutError
    assert not issubclass(asyncio.TimeoutError, OSError)


# ---------------------------------------------------------------------------
# T1：错误梯必须把 3.10 风格的 asyncio 超时判成 timeout（改前红）
# ---------------------------------------------------------------------------
def test_t1_recognition_ladder_marks_asyncio_timeout_as_timeout(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _patch_asyncio_timeout(monkeypatch)
    store = _FakeStore()
    queue = _queue(store)

    async def _raise(task: tq.TaskItem) -> None:
        raise _Fake310AsyncTimeoutError()

    monkeypatch.setattr(queue, "_parse_file", _raise)

    async def scenario() -> None:
        await queue._run_recognition(_item())

    with caplog.at_level(logging.DEBUG):
        _run(scenario)

    assert "recognition timeout" in caplog.text, caplog.text
    assert "failed (unexpected)" not in caplog.text, caplog.text
    assert "I/O error" not in caplog.text, caplog.text
    assert store.failed_error_message() == "recognition timed out", store.status_calls


# ---------------------------------------------------------------------------
# T4：错误梯顺序——内建 TimeoutError（⊂ OSError）必须走 timeout 分支而非 I/O 分支
# ---------------------------------------------------------------------------
def test_t4_builtin_timeout_escapes_to_timeout_branch_not_io_branch(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    store = _FakeStore()
    queue = _queue(store)

    async def _raise(task: tq.TaskItem) -> None:
        raise TimeoutError("boom-page-timeout")

    monkeypatch.setattr(queue, "_parse_file", _raise)

    async def scenario() -> None:
        await queue._run_recognition(_item())

    with caplog.at_level(logging.DEBUG):
        _run(scenario)

    assert "recognition timeout" in caplog.text, caplog.text
    assert "I/O error" not in caplog.text, caplog.text
    assert store.failed_error_message() == "boom-page-timeout", store.status_calls


# ---------------------------------------------------------------------------
# T5 / T9：反向护栏——普通异常与 I/O 异常不得被吃成 timeout
# ---------------------------------------------------------------------------
def test_t5_generic_error_is_not_reported_as_timeout(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _patch_asyncio_timeout(monkeypatch)
    store = _FakeStore()
    queue = _queue(store)

    async def _raise(task: tq.TaskItem) -> None:
        raise ValueError("not-a-timeout")

    monkeypatch.setattr(queue, "_parse_file", _raise)

    async def scenario() -> None:
        await queue._run_recognition(_item())

    with caplog.at_level(logging.DEBUG):
        _run(scenario)

    assert "recognition failed: ValueError" in caplog.text, caplog.text
    assert "recognition timeout" not in caplog.text, caplog.text
    assert store.failed_error_message() == "not-a-timeout", store.status_calls


def test_t9_os_error_still_takes_io_branch(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _patch_asyncio_timeout(monkeypatch)
    store = _FakeStore()
    queue = _queue(store)

    async def _raise(task: tq.TaskItem) -> None:
        raise PermissionError("permission-denied")

    monkeypatch.setattr(queue, "_parse_file", _raise)

    async def scenario() -> None:
        await queue._run_recognition(_item())

    with caplog.at_level(logging.DEBUG):
        _run(scenario)

    assert "recognition I/O error" in caplog.text, caplog.text
    assert "recognition timeout" not in caplog.text, caplog.text
    assert store.failed_error_message() == "permission-denied", store.status_calls


# ---------------------------------------------------------------------------
# T6：CancelledError 是 BaseException，不得被任何分支吞掉
# ---------------------------------------------------------------------------
def test_t6_cancelled_error_is_not_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_asyncio_timeout(monkeypatch)
    store = _FakeStore()
    queue = _queue(store)

    async def _raise(task: tq.TaskItem) -> None:
        raise asyncio.CancelledError()

    monkeypatch.setattr(queue, "_parse_file", _raise)

    async def scenario() -> None:
        with pytest.raises(asyncio.CancelledError):
            await queue._run_recognition(_item())

    _run(scenario)
    assert store.failed_error_message() is None, store.status_calls


# ---------------------------------------------------------------------------
# T2 / T10a：页级（`_run_page` 的 wait_for 之后）
# ---------------------------------------------------------------------------
def test_t2_page_level_asyncio_timeout_is_recorded_as_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_asyncio_timeout(monkeypatch)
    store = _FakeStore()
    queue = _queue(store)

    async def _detect(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise _Fake310AsyncTimeoutError()

    _vision_stubs(monkeypatch, pages=1, detect=_detect)

    async def scenario() -> None:
        with pytest.raises(TimeoutError) as excinfo:
            await queue._run_vision(_item(), dict(_VISION_CFG))
        assert "timed out after" in str(excinfo.value), str(excinfo.value)

    _run(scenario)
    assert store.page_entry(1).get("status") == "timeout", store.performance


def test_t10a_page_level_non_timeout_failure_stays_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_asyncio_timeout(monkeypatch)
    store = _FakeStore()
    queue = _queue(store)

    async def _detect(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise ValueError("bad-page")

    _vision_stubs(monkeypatch, pages=1, detect=_detect)

    async def scenario() -> None:
        with pytest.raises(ValueError):
            await queue._run_vision(_item(), dict(_VISION_CFG))

    _run(scenario)
    assert store.page_entry(1).get("status") == "failed", store.performance


# ---------------------------------------------------------------------------
# T3 / T10b：管线级（`_run_vision` 外层 try 的完善阶段）
# ---------------------------------------------------------------------------
def test_t3_pipeline_level_asyncio_timeout_escapes_as_builtin_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_asyncio_timeout(monkeypatch)
    store = _FakeStore(progress_exc=_Fake310AsyncTimeoutError())
    queue = _queue(store)

    async def _detect(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"boxes": []}

    _vision_stubs(monkeypatch, pages=1, detect=_detect)

    async def scenario() -> None:
        with pytest.raises(TimeoutError) as excinfo:
            await queue._run_vision(_item(), dict(_VISION_CFG))
        assert type(excinfo.value) is TimeoutError, type(excinfo.value)
        assert "vision recognition timed out after" in str(excinfo.value), str(excinfo.value)

    _run(scenario)


def test_t10b_pipeline_level_non_timeout_failure_is_not_reclassified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_asyncio_timeout(monkeypatch)
    store = _FakeStore(progress_exc=ValueError("bad-finalize"))
    queue = _queue(store)

    async def _detect(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"boxes": []}

    _vision_stubs(monkeypatch, pages=1, detect=_detect)

    async def scenario() -> None:
        with pytest.raises(ValueError):
            await queue._run_vision(_item(), dict(_VISION_CFG))

    _run(scenario)


# ---------------------------------------------------------------------------
# T7：多页混合批次不得串扰（1 页成功 / 1 页超时 / 1 页成功）
# ---------------------------------------------------------------------------
def test_t7_multi_page_batch_records_pages_independently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.config import settings

    _patch_asyncio_timeout(monkeypatch)
    monkeypatch.setattr(settings, "BATCH_RECOGNITION_PAGE_CONCURRENCY", 3, raising=False)
    store = _FakeStore()
    queue = _queue(store)

    async def _detect(file_id: str, page: int, **kwargs: Any) -> dict[str, Any]:
        if page == 2:
            await asyncio.sleep(0.05)
            raise _Fake310AsyncTimeoutError()
        return {"boxes": []}

    _vision_stubs(monkeypatch, pages=3, detect=_detect)

    async def scenario() -> None:
        with pytest.raises(TimeoutError):
            await queue._run_vision(_item(), dict(_VISION_CFG))

    _run(scenario)
    statuses = store.page_statuses()
    assert statuses.get("2") == "timeout", statuses
    assert "failed" not in statuses.values(), statuses
    for page in ("1", "3"):
        if page in statuses:
            assert statuses[page] == "completed", statuses


# ---------------------------------------------------------------------------
# T8：边界——缺省 / 0 / None 的 page_count 归一为 1（`or 1` + `max(1, pages)`）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("page_count", [None, 0])
def test_t8_page_count_normalisation(
    monkeypatch: pytest.MonkeyPatch, page_count: int | None
) -> None:
    import app.services.file_operations as fo

    _patch_asyncio_timeout(monkeypatch)
    store = _FakeStore()
    queue = _queue(store)
    seen: list[int] = []

    def _file_info(file_id: str) -> dict[str, Any]:
        info: dict[str, Any] = {"file_id": file_id, "file_type": "image"}
        if page_count is not None:
            info["page_count"] = page_count
        return info

    async def _detect(file_id: str, page: int, **kwargs: Any) -> dict[str, Any]:
        seen.append(page)
        return {"boxes": []}

    monkeypatch.setattr(fo, "get_file_info", _file_info)
    monkeypatch.setattr(fo, "vision_detect", _detect)

    async def scenario() -> None:
        await queue._run_vision(_item(), dict(_VISION_CFG))

    _run(scenario)
    assert seen == [1], seen
    assert store.page_entry(1).get("status") == "completed", store.performance