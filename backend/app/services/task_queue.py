"""
进程内异步任务队列。
进程内异步任务队列。
单 GPU 串行处理，无跨进程问题。
队列运行在 FastAPI 主进程的事件循环中，submit 后立即入队，后台逐个消费。
"""

from __future__ import annotations

import asyncio
import bisect
import json
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from app.services.file_parser import PdfEncryptedError
from app.services.task_queue_config import (
    _clamp_job_concurrency,
    load_persisted_job_concurrency,
    save_persisted_job_concurrency,
)
from app.services.task_queue_metrics import (
    _effective_vision_page_concurrency,  # noqa: F401  re-exported for tests/API
    _estimate_recognition_task_cost,
    _safe_int,
    _utc_iso,
    _vision_page_concurrency_reason,  # noqa: F401  re-exported for tests/API
)
from app.services.task_queue_pipelines import (
    RecognitionPipelineMixin,
    RedactionPipelineMixin,
    StructuredPipelineMixin,
)

if TYPE_CHECKING:
    from app.services.job_store import JobStore

logger = logging.getLogger(__name__)
_WORKER_ERROR_MSG_MAX_LEN = 200
# Issue#137：worker 等待队列时的兜底重试间隔（避免异常风暴下空转打满 CPU）。
_WORKER_ERROR_BACKOFF_SEC = 0.2


@dataclass
class TaskItem:
    job_id: str
    item_id: str
    file_id: str
    task_type: str = "recognition"  # "recognition" | "redaction"
    meta: dict[str, Any] = field(default_factory=dict)


class SimpleTaskQueue(RecognitionPipelineMixin, StructuredPipelineMixin, RedactionPipelineMixin):
    """
    单例异步任务队列。

    用法:
        queue = get_task_queue()
        queue.enqueue(TaskItem(job_id=..., item_id=..., file_id=...))

    内部维护一个按优先级排序的待处理列表（_pending_tasks），并用一个
    asyncio.Queue 作为唤醒信号（每个待处理任务对应一个 token），后台 worker
    coroutine 逐个消费。
    """

    def __init__(self, concurrency: int = 1) -> None:
        # Signal queue: one None token per pending task. Workers block on it;
        # the actual TaskItem comes from the sorted _pending_tasks list.
        self._queue: asyncio.Queue[None] = asyncio.Queue()
        self._pending_tasks: list[TaskItem] = []
        self._worker_tasks: list[asyncio.Task] = []
        # Issue#137：任务 → worker_id 映射，死亡时要靠它把该 worker 手上的在场件认领回来。
        self._worker_ids: dict[asyncio.Task, int] = {}
        self._worker_respawns = 0
        # 门③ 评审 B1：只有**显式**标记为「计划退休」的 worker 才允许退出。
        # 旧实现用 `worker_id >= self._concurrency` 判退休，而监督补位走的是自增 id
        # （1、2、3… 全都 ≥ concurrency）⇒ 补位 worker 在首个空闲窗口就自我退休，
        # 池子在任何一次死亡后都回不到 concurrency，`_worker_respawns` 无意义膨胀。
        self._retiring_ids: set[int] = set()
        self._running = False
        self._current: dict[int, TaskItem | None] = {}  # worker_id -> current task
        self._pending_items: set[tuple[str, str]] = set()  # (task_type, item_id) dedupe
        self._concurrency = _clamp_job_concurrency(concurrency)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._watchdog_task: asyncio.Task | None = None
        self._enqueue_sequence = 0
        self._next_worker_id = 0

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start queue workers during application startup."""
        loop = asyncio.get_event_loop()
        if self._loop is not loop:
            self._queue = asyncio.Queue()
            self._pending_tasks.clear()
            self._worker_tasks.clear()
            self._worker_ids.clear()
            self._retiring_ids.clear()
            self._current.clear()
            self._pending_items.clear()
            self._worker_respawns = 0
            self._loop = loop
        if self._running:
            return
        self._running = True
        for i in range(self._concurrency):
            self._spawn_worker(loop, worker_id=i)
        self._next_worker_id = self._concurrency
        self._watchdog_task = loop.create_task(self._stale_processing_watchdog())
        logger.info("SimpleTaskQueue started (%d worker(s))", self._concurrency)

    def _spawn_worker(
        self, loop: asyncio.AbstractEventLoop, *, worker_id: int | None = None
    ) -> asyncio.Task:
        if worker_id is None:
            worker_id = self._next_worker_id
            self._next_worker_id += 1
        else:
            self._next_worker_id = max(self._next_worker_id, worker_id + 1)
        task = loop.create_task(
            self._worker_loop(worker_id=worker_id),
            name=f"task-queue-worker-{worker_id}",
        )
        self._worker_tasks.append(task)
        self._worker_ids[task] = worker_id
        return task

    @property
    def concurrency(self) -> int:
        return self._concurrency

    def set_concurrency(self, value: int, *, persist: bool = True) -> int:
        next_value = _clamp_job_concurrency(value)
        if persist:
            next_value = save_persisted_job_concurrency(next_value)
        self._concurrency = next_value
        if self._running and self._loop is not None:
            live_ids = sorted(
                self._worker_ids[task]
                for task in self._worker_tasks
                if not task.done() and task in self._worker_ids
            )
            if next_value < len(live_ids):
                # 缩容：显式把多余 worker 标记为「计划退休」，它们在下一个空闲窗口
                # 自行退出（由 `_worker_loop` 读 `_retiring_ids` 判定，不再按 id 大小）。
                for worker_id in live_ids[next_value:]:
                    self._retiring_ids.add(worker_id)
            else:
                for _ in range(next_value - len(live_ids)):
                    self._spawn_worker(self._loop)
        logger.info("SimpleTaskQueue concurrency set to %d", self._concurrency)
        return self._concurrency

    def stop(self) -> list[asyncio.Task]:
        """Stop workers and return tasks for shutdown awaiting."""
        self._running = False
        tasks = []
        for t in self._worker_tasks:
            if not t.done():
                t.cancel()
                tasks.append(t)
        if self._watchdog_task is not None and not self._watchdog_task.done():
            self._watchdog_task.cancel()
            tasks.append(self._watchdog_task)
        self._watchdog_task = None
        self._worker_tasks.clear()
        self._worker_ids.clear()
        self._current.clear()
        self._pending_items.clear()
        self._queue = asyncio.Queue()
        self._pending_tasks.clear()
        self._loop = None
        logger.info("SimpleTaskQueue stopped")
        return tasks

    # ------------------------------------------------------------------
    # 入队
    # ------------------------------------------------------------------

    def enqueue(self, task: TaskItem) -> None:
        task_key = self._task_key(task)
        if task_key in self._pending_items:
            logger.info(
                "skip duplicate enqueue %s  item=%s  (already pending)",
                task.task_type, task.item_id[:8],
            )
            return
        task.meta.setdefault("enqueued_at", _utc_iso())
        task.meta.setdefault("enqueued_perf_counter", time.perf_counter())
        task.meta.setdefault("enqueue_sequence", self._enqueue_sequence)
        self._enqueue_sequence += 1
        self._ensure_task_priority_metadata(task)
        self._record_task_enqueued(task)
        self._pending_items.add(task_key)
        self._insert_pending(task)
        self._queue.put_nowait(None)  # wake one waiting worker
        # Issue#137：worker 可能已经静默死亡（历史上整队列只剩这一个消费方），
        # 提交动作本身就该把消费能力救回来，而不是等重启。
        self._supervise_workers()
        logger.info(
            "enqueued %s  job=%s item=%s file=%s  priority=%s work=%s (queue_size=%d)",
            task.task_type, task.job_id[:8], task.item_id[:8], task.file_id[:8],
            task.meta.get("priority_class"), task.meta.get("estimated_work_units"),
            self._queue.qsize(),
        )

    @property
    def queue_size(self) -> int:
        return self._queue.qsize()

    @property
    def current_task(self) -> TaskItem | None:
        for t in self._current.values():
            if t is not None:
                return t
        return None

    @staticmethod
    def _task_key(task: TaskItem) -> tuple[str, str]:
        return (str(task.task_type or "recognition"), task.item_id)

    def _active_item_ids(self) -> set[str]:
        active = {task.item_id for task in self._current.values() if task is not None}
        active.update(item_id for _task_type, item_id in self._pending_items)
        return active

    def _record_task_enqueued(self, task: TaskItem) -> None:
        try:
            store = self._get_store()
            store.update_item_performance(
                task.item_id,
                {
                    task.task_type: {
                        "queued_at": task.meta.get("enqueued_at"),
                        "queue_size_at_enqueue": self._queue.qsize(),
                    },
                    "queue": {
                        "last_task_type": task.task_type,
                        "last_enqueued_at": task.meta.get("enqueued_at"),
                        "priority_class": task.meta.get("priority_class"),
                        "estimated_work_units": task.meta.get("estimated_work_units"),
                    },
                },
            )
        except Exception:
            logger.debug("unable to record enqueue diagnostics for item %s", task.item_id, exc_info=True)

    def _ensure_task_priority_metadata(self, task: TaskItem) -> None:
        if "priority_class" in task.meta and "estimated_work_units" in task.meta:
            return
        priority_class, work_units = self._estimate_task_cost(task)
        task.meta.setdefault("priority_class", priority_class)
        task.meta.setdefault("estimated_work_units", work_units)

    def _estimate_task_cost(self, task: TaskItem) -> tuple[int, int]:
        if task.task_type == "structured":
            return (0, 1)
        if task.task_type != "recognition":
            return (20, 1)
        try:
            from app.services.file_operations import get_file_info

            info = get_file_info(task.file_id) or {}
        except Exception:
            logger.debug("unable to inspect file metadata for queue priority: %s", task.file_id, exc_info=True)
            info = {}
        return _estimate_recognition_task_cost(info)

    def _task_sort_key(self, task: TaskItem) -> tuple[int, int, int, int]:
        task_type_order = 0 if task.task_type in {"structured", "recognition"} else 1
        return (
            task_type_order,
            _safe_int(task.meta.get("priority_class"), default=99),
            max(1, _safe_int(task.meta.get("estimated_work_units"), default=1)),
            _safe_int(task.meta.get("enqueue_sequence"), default=0),
        )

    def _insert_pending(self, task: TaskItem) -> None:
        """Insert into the sorted pending list (stable: equal keys keep enqueue order)."""
        try:
            bisect.insort(self._pending_tasks, task, key=self._task_sort_key)
        except Exception:
            # Fallback mirrors the legacy behaviour: on sort failure the new
            # task simply goes to the back of the queue (FIFO position).
            logger.debug("unable to insert task into sorted pending queue", exc_info=True)
            self._pending_tasks.append(task)

    def _record_task_started(self, task: TaskItem, store: Any) -> None:
        started_at = _utc_iso()
        enqueued_counter = task.meta.get("enqueued_perf_counter")
        wait_ms = None
        if isinstance(enqueued_counter, (int, float)):
            wait_ms = max(0, int(round((time.perf_counter() - float(enqueued_counter)) * 1000)))
        patch: dict[str, Any] = {
            task.task_type: {
                "started_at": started_at,
            },
            "queue": {
                "last_task_type": task.task_type,
                "last_started_at": started_at,
            },
        }
        if wait_ms is not None:
            patch[task.task_type]["queue_wait_ms"] = wait_ms
            patch["queue"]["last_wait_ms"] = wait_ms
        try:
            store.update_item_performance(task.item_id, patch)
        except Exception:
            logger.debug("unable to record start diagnostics for item %s", task.item_id, exc_info=True)

    def _record_item_performance(self, store: Any, item_id: str, patch: dict[str, Any]) -> None:
        try:
            store.update_item_performance(item_id, patch)
        except Exception:
            logger.debug("unable to record performance diagnostics for item %s", item_id, exc_info=True)

    # ------------------------------------------------------------------
    # 后台 worker
    # ------------------------------------------------------------------

    def _supervise_workers(self) -> None:
        """Issue#137：worker 静默死亡必须被发现、留证据、补位，并认领它手上的在场件。

        `enqueue()` 与看门狗（30s 间隔）都会调用：前者让「有人提交却无人消费」
        当场自愈，后者兜住长期空转（含 `set_concurrency` 缩减后正常退出的清理）。
        正常缩减（`set_concurrency` 显式标记进 `_retiring_ids` 的 worker）由 worker
        自行退出：这里只记 INFO、**不补位**；异常死亡记 ERROR 并按死者 slot id 补位
        （B1：不能自增 id，否则补位 worker 会被自己的退休判据判死）。补位先于认领，
        以免 `_reclaim_orphan` 重新入队时再触发一轮多余补位。
        """
        if not self._running:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            logger.debug("no running loop; skip worker supervision")
            return
        live = 0
        dead: list[tuple[asyncio.Task, int | None]] = []
        reusable_ids: list[int] = []
        for task in list(self._worker_tasks):
            if not task.done():
                live += 1
                continue
            self._worker_tasks.remove(task)
            worker_id = self._worker_ids.pop(task, None)
            planned = worker_id is not None and worker_id in self._retiring_ids
            if worker_id is not None:
                self._retiring_ids.discard(worker_id)
            if planned:
                # B2 修复：正常退休（缩容）不是故障：记 INFO，且绝不补位。
                logger.info(
                    "worker-%s retired (reason=planned_retirement concurrency=%d)",
                    worker_id, self._concurrency,
                )
                continue
            if task.cancelled():
                logger.error("worker task %s was cancelled; will respawn", task.get_name())
            else:
                exc = task.exception()
                logger.error(
                    "worker task %s died: %s: %s; will respawn",
                    task.get_name(), type(exc).__name__ if exc is not None else "None", exc,
                )
            dead.append((task, worker_id))
            if worker_id is not None and worker_id < self._concurrency:
                reusable_ids.append(worker_id)
        if self._loop is not None and live < self._concurrency:
            missing = self._concurrency - live
            for index in range(missing):
                # B1 修复：优先复用死者占用的 slot id（`_spawn_worker` 显式传 id，
                # 不再自增出 ≥ concurrency 的新 id ⇒ 补位 worker 不会被判退休）。
                reuse = reusable_ids[index] if index < len(reusable_ids) else None
                fresh = self._spawn_worker(self._loop, worker_id=reuse)
                logger.warning(
                    "supervisor respawned worker-%s (reason=dead worker, reused_id=%s)",
                    fresh.get_name(), reuse,
                )
            self._worker_respawns += missing
        for _task, worker_id in dead:
            if worker_id is not None:
                self._reclaim_orphan(worker_id)
        if dead:
            self._restore_wake_tokens()
        else:
            # N6 权衡：差额也可能来自 3.10 `wait_for` 的瞬态窗口（token 已被取走、
            # 任务尚未 pop）⇒ 补 token 是安全冗余动作（不会重复消费），但不能每次
            # 监督都刷 WARNING。没死过 worker 时只补不说。
            self._restore_wake_tokens(quiet=True)

    def _restore_wake_tokens(self, *, quiet: bool = False) -> None:
        """Issue#137：唤醒 token 与待处理任务必须一一对应，差额要补回。

        worker 在「取走 token」与「pop 任务」之间死亡时，token 随之丢失，而该任务
        仍留在 `_pending_tasks`：没有新 token 它就永远等不到消费者，而重复入队又被
        `_pending_items` 去重挡掉 ⇒ 该件永久卡住（业务侧表现为「排队中」不动）。
        正常运行时 `len(_pending_tasks) == qsize()`，只有丢过 token 才为正差额。
        """
        before = self._queue.qsize()
        missing = len(self._pending_tasks) - before
        if missing <= 0:
            return
        for _ in range(missing):
            self._queue.put_nowait(None)
        message = "restored %d lost wake token(s) (pending=%d queue_size=%d before restore)"
        if quiet:
            # N6：没死过 worker 时的差额多为 3.10 `wait_for` 瞬态窗口（token 已取走、
            # 任务尚未 pop），补回是安全冗余，但不是故障 ⇒ 只记 DEBUG。
            logger.debug(message, missing, len(self._pending_tasks), before)
        else:
            logger.warning(message, missing, len(self._pending_tasks), before)

    def _reclaim_orphan(self, worker_id: int) -> None:
        """worker 死亡时它手上的在场件必须重新入队。

        否则该件会永久卡住：`_pending_items` 里仍有它的键 ⇒ 后续 `enqueue` 被
        判「重复入队」而跳过；而 DB 侧探针要等 `BATCH_RECOGNITION_PAGE_TIMEOUT * 2`
        才会回收，那之前用户看到的就是「件在跑却永远不出结果」。
        """
        task = self._current.get(worker_id)
        self._current[worker_id] = None
        if task is None:
            return
        self._pending_items.discard(self._task_key(task))
        logger.warning(
            "worker-%d died holding %s job=%s item=%s; re-enqueueing the in-flight item",
            worker_id, task.task_type, str(task.job_id)[:8], str(task.item_id)[:8],
        )
        try:
            self.enqueue(task)
        except Exception:
            logger.exception(
                "failed to re-enqueue orphan item %s; watchdog will repair from store",
                str(task.item_id)[:8],
            )

    async def _stale_processing_watchdog(self) -> None:
        from app.core.config import settings

        interval_seconds = 30.0
        max_age_seconds = max(120.0, float(settings.BATCH_RECOGNITION_PAGE_TIMEOUT) * 2)
        while self._running:
            try:
                await asyncio.sleep(interval_seconds)
                # Issue#137：队列健康度必须可见——「有件、无人消费」不得再是零日志状态。
                live_workers = sum(1 for task in self._worker_tasks if not task.done())
                logger.info(
                    "queue health: workers=%d/%d queue_size=%d pending=%d",
                    live_workers, self._concurrency, self._queue.qsize(), len(self._pending_tasks),
                )
                if not self._queue.empty() and live_workers == 0:
                    logger.error(
                        "worker starvation: %d wake token(s) pending but no live worker",
                        self._queue.qsize(),
                    )
                self._supervise_workers()
                store = self._get_store()
                repaired = store.repair_stale_processing_items(
                    exclude_item_ids=self._active_item_ids(),
                    max_age_seconds=max_age_seconds,
                )
                for row in repaired:
                    self.enqueue(
                        TaskItem(
                            job_id=str(row["job_id"]),
                            item_id=str(row["item_id"]),
                            file_id=str(row["file_id"]),
                            task_type=str(row.get("task") or "recognition"),
                        )
                    )
                if repaired:
                    logger.warning(
                        "stale processing watchdog requeued %d item(s)",
                        len(repaired),
                    )
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("stale processing watchdog failed")

    async def _worker_loop(self, worker_id: int = 0) -> None:
        logger.info("worker-%d loop started", worker_id)
        while self._running:
            try:
                await asyncio.wait_for(self._queue.get(), timeout=2.0)
            # noqa: UP041 —— 不能用 ruff 的自动改写：部署环境是 Python 3.10，
            # 那里 `asyncio.TimeoutError` 与内建 `TimeoutError` 不是同一个类，
            # 只捕内建类 = 本 Issue 根因（3.11+ 起二者才合并，tuple 写法对新版同样正确）。
            except (TimeoutError, asyncio.TimeoutError):  # noqa: UP041
                # Issue#137：Python 3.10 里 `asyncio.TimeoutError` 与内建 `TimeoutError`
                # **不是同一个类**（3.11 才合并），只写 `except TimeoutError` 捕不到
                # `wait_for` 的超时 ⇒ worker 在启动后首个 2s 空闲超时即带未处理异常退出；
                # 又因任务被 `_worker_tasks` 强引用，asyncio 连
                # "Task exception was never retrieved" 都不打 ⇒ 全静默。
                # 后果：恢复件（启动瞬间入队）能跑，在飞提交件全部无人消费。
                if worker_id in self._retiring_ids:
                    # 计划退休（缩容，由 `set_concurrency` 显式标记）：留下可区分的证据，
                    # 否则监督无法把「正常退休」与「异常死亡」分开（B1/B2）。
                    logger.info(
                        "worker-%d exited (reason=planned_retirement concurrency=%d)",
                        worker_id, self._concurrency,
                    )
                    break
                continue
            except asyncio.CancelledError:
                logger.info("worker-%d exited (reason=cancelled)", worker_id)
                break
            except Exception:
                # 等待路径的任何未预期异常都不得结束 worker（否则又回到静默死亡）。
                logger.exception("worker-%d failed to wait on queue; retrying", worker_id)
                await asyncio.sleep(_WORKER_ERROR_BACKOFF_SEC)
                continue

            # One token was consumed, so exactly one pending task is ours.
            # Both structures are only mutated together on the event loop,
            # with no await between the paired mutations.
            if not self._pending_tasks:
                # Issue#137 防御：token 与待处理列表万一失步，丢弃该 token 并留证据，
                # 而不是让 `pop(0)` 以 IndexError 杀掉队列唯一的消费方。
                logger.warning(
                    "worker-%d got a wake token but no pending task; token dropped", worker_id
                )
                self._queue.task_done()
                continue
            task = self._pending_tasks.pop(0)
            self._current[worker_id] = task
            logger.info(
                "worker-%d processing %s  job=%s item=%s file=%s  (remaining=%d)",
                worker_id, task.task_type, task.job_id[:8], task.item_id[:8],
                task.file_id[:8], self._queue.qsize(),
            )
            should_exit = False
            try:
                if task.task_type == "recognition":
                    await self._run_recognition(task)
                elif task.task_type == "redaction":
                    await self._run_redaction(task)
                elif task.task_type == "structured":
                    await self._run_structured(task)
                else:
                    logger.warning("unknown task_type: %s", task.task_type)
            except PdfEncryptedError as exc:
                # Issue #30：加密 PDF 的条目失败要给人话——不能落进下面的通用
                # 元组变成 "worker: ValueError: document closed or encrypted"。
                logger.error(
                    "task failed (encrypted pdf): job=%s item=%s",
                    task.job_id[:8], task.item_id[:8],
                )
                try:
                    from app.services.job_store import JobItemStatus
                    store = self._get_store()
                    store.update_item_status(
                        task.item_id, JobItemStatus.FAILED,
                        error_message=exc.user_message,
                    )
                except Exception:
                    pass
            except (TimeoutError, OSError, RuntimeError, ValueError, KeyError, json.JSONDecodeError) as exc:
                logger.error(
                    "task failed: job=%s item=%s: %s: %s",
                    task.job_id[:8], task.item_id[:8],
                    type(exc).__name__, exc,
                )
                try:
                    from app.services.job_store import JobItemStatus
                    store = self._get_store()
                    store.update_item_status(
                        task.item_id, JobItemStatus.FAILED,
                        error_message=f"worker: {type(exc).__name__}: {str(exc)[:_WORKER_ERROR_MSG_MAX_LEN]}",
                    )
                except Exception:
                    pass
            except Exception:
                logger.exception(
                    "task failed (unexpected): job=%s item=%s", task.job_id[:8], task.item_id[:8]
                )
                try:
                    from app.services.job_store import JobItemStatus
                    store = self._get_store()
                    store.update_item_status(
                        task.item_id, JobItemStatus.FAILED,
                        error_message="worker unhandled exception",
                    )
                except Exception:
                    pass
            finally:
                self._current[worker_id] = None
                self._pending_items.discard(self._task_key(task))
                self._queue.task_done()
                logger.info(
                    "done %s  job=%s item=%s  (remaining=%d)",
                    task.task_type, task.job_id[:8], task.item_id[:8],
                    self._queue.qsize(),
                )
                if worker_id in self._retiring_ids:
                    should_exit = True
            if should_exit:
                break

        logger.info("worker-%d loop exited", worker_id)

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _get_store(self) -> JobStore:
        from app.services.job_store import get_job_store
        return get_job_store()

    def _try_update_job_status(self, store, job_id: str, status) -> None:
        from app.services.job_store import InvalidStatusTransition
        try:
            store.update_job_status(job_id, status)
        except (InvalidStatusTransition, KeyError, ValueError):
            pass  # 状态已前进或 job 不存在，忽略

    def _refresh_job_status(self, store, job_id: str) -> None:
        """Refresh the job status from item statuses."""
        from app.services.job_store import JobItemStatus, JobStatus

        job = store.get_job(job_id)
        if not job or job["status"] in (JobStatus.CANCELLED.value,):
            return
        items = store.list_items(job_id)
        if not items:
            return

        sts = [i["status"] for i in items]
        active = {JobItemStatus.PENDING.value, JobItemStatus.PROCESSING.value}
        terminal = {JobItemStatus.AWAITING_REVIEW.value, JobItemStatus.COMPLETED.value, JobItemStatus.FAILED.value}
        try:
            if any(s in active for s in sts):
                # 还有 item 在跑或排队 — job 保持活跃状态
                if any(s == JobItemStatus.PROCESSING.value for s in sts):
                    self._try_update_job_status(store, job_id, JobStatus.PROCESSING)
                else:
                    self._try_update_job_status(store, job_id, JobStatus.QUEUED)
            elif all(s == JobItemStatus.COMPLETED.value for s in sts):
                self._try_update_job_status(store, job_id, JobStatus.COMPLETED)
            elif all(s == JobItemStatus.FAILED.value for s in sts):
                self._try_update_job_status(store, job_id, JobStatus.FAILED)
            elif all(s in terminal for s in sts):
                # 混合终态：有待审 → AWAITING_REVIEW，否则 COMPLETED（含部分失败）
                if any(s == JobItemStatus.AWAITING_REVIEW.value for s in sts):
                    self._try_update_job_status(store, job_id, JobStatus.AWAITING_REVIEW)
                else:
                    self._try_update_job_status(store, job_id, JobStatus.COMPLETED)
        except Exception:
            logger.warning("_refresh_job_status failed for job %s", job_id[:8], exc_info=True)


# ------------------------------------------------------------------
# 单例
# ------------------------------------------------------------------
_instance: SimpleTaskQueue | None = None


def get_task_queue() -> SimpleTaskQueue:
    global _instance
    if _instance is None:
        from app.core.config import settings
        _instance = SimpleTaskQueue(
            concurrency=load_persisted_job_concurrency(settings.JOB_CONCURRENCY)
        )
    return _instance
