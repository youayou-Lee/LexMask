"""Issue#137 回归：批量队列 worker 在空闲超时后必须存活、异常不得静默杀死 worker。

根因回顾（云实例 Python 3.10.12 实测）：`_worker_loop` 用
`except TimeoutError:` 捕 `asyncio.wait_for()` 的超时，而 3.10 里
`asyncio.TimeoutError` 与内建 `TimeoutError` **不是同一个类**（3.11 才合并），
⇒ worker 在启动后首个 2s 空闲超时即带未处理异常退出，且任务被 `_worker_tasks`
强引用，asyncio 连 "Task exception was never retrieved" 都不打 ⇒ 全静默；
在飞提交的 `smart_batch` 从此无人消费（只有「重启恢复路径」能跑）。

因为本机（3.12）复现不出该类关系差异，这里用**版本无关**的替身钉住修复：
把 `task_queue.asyncio.TimeoutError` 换成与内建 `TimeoutError` 无继承关系的假类，
再让 `wait_for` 抛它——只有显式 `except (TimeoutError, asyncio.TimeoutError)`
才捕得到（3.10 语义）。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

import pytest

import app.services.task_queue as tq


def _item(item_id: str = "item-0001") -> tq.TaskItem:
    return tq.TaskItem(job_id="job-0001", item_id=item_id, file_id="file-0001", task_type="recognition")


def _start(queue: tq.SimpleTaskQueue) -> None:
    queue.start()


def _shutdown(queue: tq.SimpleTaskQueue) -> None:
    tasks = queue.stop()
    for task in tasks:
        task.cancel()


class _Fake310AsyncTimeoutError(Exception):
    """模拟 Python 3.10 的 `asyncio.TimeoutError`：与内建 `TimeoutError` 无继承关系。"""


def test_worker_survives_idle_timeout() -> None:
    """空闲超过 2s 等待窗口后，worker 任务不得结束（3.10 上改前会 done）。"""

    async def scenario() -> None:
        queue = tq.SimpleTaskQueue(concurrency=1)
        _start(queue)
        try:
            await asyncio.sleep(2.6)
            assert queue._worker_tasks, "启动后应有 worker 任务"
            assert all(not task.done() for task in queue._worker_tasks), (
                "worker 不得在空闲超时后结束："
                f"{[(t.get_name(), t.done(), t.exception() if t.done() and not t.cancelled() else None) for t in queue._worker_tasks]}"
            )
        finally:
            _shutdown(queue)

    asyncio.run(scenario())


def test_worker_loop_catches_asyncio_timeout_error(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """3.10 语义下（asyncio.TimeoutError 独立类）worker 必须存活并继续消费。

    只断言「存活」会被外层兜底 `except Exception` 蒙过去（变异 M01 实测逃生）：
    真正被钉住的是**该超时被识别为空闲**——不报 ERROR、不触发补位，
    也不吃掉 `worker_id >= concurrency` 的正常退休分支（缩容语义，见
    `backend/app/api/auth.py`）。
    """
    calls = {"n": 0}
    real_wait_for = asyncio.wait_for

    async def fake_wait_for(awaitable, timeout=None):  # noqa: ANN001, ANN202
        calls["n"] += 1
        if calls["n"] <= 6:
            awaitable.close()  # 超时路径上原协程不会被驱动，显式关闭避免告警
            raise _Fake310AsyncTimeoutError()
        return await real_wait_for(awaitable, timeout=timeout)

    monkeypatch.setattr(tq.asyncio, "TimeoutError", _Fake310AsyncTimeoutError)
    monkeypatch.setattr(tq.asyncio, "wait_for", fake_wait_for)

    async def scenario() -> None:
        queue = tq.SimpleTaskQueue(concurrency=1)
        _start(queue)
        seen: list[str] = []

        async def fake_run(task: tq.TaskItem) -> None:
            seen.append(task.item_id)

        queue._run_recognition = fake_run  # type: ignore[method-assign]
        try:
            with caplog.at_level(logging.ERROR, logger="app.services.task_queue"):
                await asyncio.sleep(0.05)
                assert not queue._worker_tasks[0].done(), "3.10 语义的超时后 worker 必须存活"
                queue.enqueue(_item("item-310"))
                for _ in range(60):
                    if seen:
                        break
                    await asyncio.sleep(0.05)
                assert seen == ["item-310"], f"存活下来的 worker 必须继续消费，实际 {seen}"
                assert queue._worker_respawns == 0, "空闲超时不得被当成死亡去补位"
            noisy = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
            assert not [m for m in noisy if "failed to wait on queue" in m], (
                f"3.10 语义的空闲超时被当成异常记录了（应走 except 分支静默续等）：{noisy}"
            )
        finally:
            _shutdown(queue)

    asyncio.run(scenario())


def test_enqueue_after_idle_is_consumed() -> None:
    """启动 → 空闲 > 超时窗口 → 入队 ⇒ 必须被消费（「在飞提交无人消费」的直接回归）。"""

    async def scenario() -> None:
        queue = tq.SimpleTaskQueue(concurrency=1)
        _start(queue)
        seen: list[str] = []

        async def fake_run(task: tq.TaskItem) -> None:
            seen.append(task.item_id)

        queue._run_recognition = fake_run  # type: ignore[method-assign]
        try:
            await asyncio.sleep(2.6)  # 改前：worker 已死，token 会堆到队列里无人取
            queue.enqueue(_item("item-late"))
            for _ in range(60):
                if seen:
                    break
                await asyncio.sleep(0.05)
            assert seen == ["item-late"], f"空闲后的在飞任务必须被消费，实际 {seen}"
            assert queue.queue_size == 0 and not queue._pending_tasks
        finally:
            _shutdown(queue)

    asyncio.run(scenario())


def test_worker_unexpected_exception_does_not_kill_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """等待路径的未预期异常：记录后重试，worker 仍能消费后续任务。"""
    calls = {"n": 0}
    real_wait_for = asyncio.wait_for

    async def fake_wait_for(awaitable, timeout=None):  # noqa: ANN001, ANN202
        calls["n"] += 1
        if calls["n"] == 1:
            awaitable.close()
            raise RuntimeError("boom in wait path")
        return await real_wait_for(awaitable, timeout=timeout)

    monkeypatch.setattr(tq.asyncio, "wait_for", fake_wait_for)

    async def scenario() -> None:
        queue = tq.SimpleTaskQueue(concurrency=1)
        _start(queue)
        seen: list[str] = []

        async def fake_run(task: tq.TaskItem) -> None:
            seen.append(task.item_id)

        queue._run_recognition = fake_run  # type: ignore[method-assign]
        try:
            await asyncio.sleep(0.05)
            assert not queue._worker_tasks[0].done(), "等待异常不得结束 worker"
            queue.enqueue(_item("item-after-boom"))
            for _ in range(60):
                if seen:
                    break
                await asyncio.sleep(0.05)
            assert seen == ["item-after-boom"]
        finally:
            _shutdown(queue)

    asyncio.run(scenario())


def test_token_without_pending_task_is_tolerated() -> None:
    """token 与待处理列表失步：丢 token 留告警，不得让 worker 死于 IndexError。"""

    async def scenario() -> None:
        queue = tq.SimpleTaskQueue(concurrency=1)
        _start(queue)
        try:
            queue._queue.put_nowait(None)  # 凭空一个 token，_pending_tasks 为空
            await asyncio.sleep(2.6)
            assert not queue._worker_tasks[0].done(), "token 失步不得杀死 worker"
            assert queue.queue_size == 0, "失步 token 应被丢弃"
            queue._queue.put_nowait(None)
        finally:
            _shutdown(queue)

    asyncio.run(scenario())


def test_supervise_workers_respawns_dead_worker(caplog: pytest.LogCaptureFixture) -> None:
    """存活性监督：worker 死亡后被剪除并补位至 concurrency，且留错误日志。"""

    async def scenario() -> None:
        queue = tq.SimpleTaskQueue(concurrency=2)
        _start(queue)
        try:
            await asyncio.sleep(0.05)
            victims = [task for task in queue._worker_tasks if not task.done()]
            assert len(victims) == 2
            victims[0].cancel()
            await asyncio.sleep(0.05)
            assert victims[0].done()
            with caplog.at_level(logging.WARNING, logger="app.services.task_queue"):
                queue._supervise_workers()
            live = [task for task in queue._worker_tasks if not task.done()]
            assert len(live) == 2, f"必须补位回 concurrency=2，实际 {len(live)}"
            assert victims[0] not in queue._worker_tasks, "死掉的 worker 任务应被剪除"
            assert any("was cancelled" in record.message or "died" in record.message for record in caplog.records)
        finally:
            _shutdown(queue)

    asyncio.run(scenario())


def test_supervise_workers_does_not_inflate_after_shrink() -> None:
    """`set_concurrency` 缩减后，正常退出的 worker 不得被监督逻辑又撑回去。"""

    async def scenario() -> None:
        queue = tq.SimpleTaskQueue(concurrency=2)
        _start(queue)
        try:
            queue.set_concurrency(1, persist=False)
            await asyncio.sleep(2.6)  # 让 worker-1 在下一个空闲超时按 id 规则正常退出
            queue._supervise_workers()
            live = [task for task in queue._worker_tasks if not task.done()]
            assert len(live) == 1, f"缩减到 1 后不得再补位，实际 {len(live)}"
        finally:
            _shutdown(queue)

    asyncio.run(scenario())


def test_stop_is_clean_and_idempotent() -> None:
    """收尾：stop() 取消 worker、清空队列与待处理列表（不因新增监督逻辑而残留）。"""

    async def scenario() -> None:
        queue = tq.SimpleTaskQueue(concurrency=2)
        _start(queue)
        await asyncio.sleep(0.05)
        tasks = queue.stop()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.gather(*tasks, return_exceptions=True)
        assert queue._worker_tasks == []
        assert queue.queue_size == 0
        assert not queue._pending_tasks
        assert queue._running is False
        assert queue._worker_ids == {}

    asyncio.run(scenario())


def test_concurrency_zero_and_negative_are_clamped(monkeypatch: pytest.MonkeyPatch) -> None:
    """边界：concurrency=0 / 负数必须夹到 1（不得出现「启动 0 个 worker 却自认在跑」）。"""
    monkeypatch.setattr(tq, "save_persisted_job_concurrency", lambda value: value)
    assert tq.SimpleTaskQueue(concurrency=0).concurrency == 1
    assert tq.SimpleTaskQueue(concurrency=-3).concurrency == 1
    assert tq.SimpleTaskQueue(concurrency=1).concurrency == 1

    async def scenario() -> None:
        queue = tq.SimpleTaskQueue(concurrency=0)
        _start(queue)
        try:
            assert queue.concurrency == 1
            assert len([t for t in queue._worker_tasks if not t.done()]) == 1
        finally:
            _shutdown(queue)

    asyncio.run(scenario())


def test_lost_wake_token_is_restored() -> None:
    """token 丢失（worker 取走后死亡）⇒ 监督必须按差额补回，否则该件永久「排队中」。"""

    async def scenario() -> None:
        queue = tq.SimpleTaskQueue(concurrency=1)
        _start(queue)
        seen: list[str] = []

        async def fake_run(task: tq.TaskItem) -> None:
            seen.append(task.item_id)

        queue._run_recognition = fake_run  # type: ignore[method-assign]
        try:
            # 手工造「token 已丢、任务还在 pending」的失步态（正常路径无法出现，
            # 只有 worker 在 get 与 pop 之间死亡才会留下这种状态）。
            queue._pending_tasks.append(_item("item-lost-token"))
            queue._pending_items.add(queue._task_key(queue._pending_tasks[0]))
            assert queue.queue_size == 0
            queue._supervise_workers()
            assert queue.queue_size == 1, "差额 token 必须被补回"
            for _ in range(60):
                if seen:
                    break
                await asyncio.sleep(0.05)
            assert seen == ["item-lost-token"], f"补回 token 后该件必须被消费，实际 {seen}"
            assert not queue._pending_tasks
        finally:
            _shutdown(queue)

    asyncio.run(scenario())


def test_orphan_in_flight_item_is_reenqueued_after_worker_death() -> None:
    """worker 死亡时手上正在处理的件必须重新入队（否则去重集合会让它永远卡住）。"""

    async def scenario() -> None:
        queue = tq.SimpleTaskQueue(concurrency=1)
        _start(queue)
        seen: list[str] = []

        async def fake_run(task: tq.TaskItem) -> None:
            seen.append(task.item_id)

        queue._run_recognition = fake_run  # type: ignore[method-assign]
        try:
            item = _item("item-orphan")
            # 造「已在飞」态：任务已从 pending 弹出、token 已消费、_current 指着它。
            queue._current[0] = item
            queue._pending_items.add(queue._task_key(item))
            victim = queue._worker_tasks[0]
            victim.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await victim
            queue._supervise_workers()
            for _ in range(60):
                if seen:
                    break
                await asyncio.sleep(0.05)
            assert seen == ["item-orphan"], f"在场件必须被认领回来处理，实际 {seen}"
            assert queue._current.get(0) is None
            assert not queue._pending_items
        finally:
            _shutdown(queue)

    asyncio.run(scenario())


def test_dead_worker_entries_do_not_accumulate() -> None:
    """死任务与映射必须被清理（否则每次重启/死亡都在 `_worker_tasks` 里堆垃圾）。"""

    async def scenario() -> None:
        queue = tq.SimpleTaskQueue(concurrency=2)
        _start(queue)
        try:
            await asyncio.sleep(0.05)
            victim = queue._worker_tasks[0]
            victim.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await victim
            assert victim.done()
            queue._supervise_workers()
            live = [t for t in queue._worker_tasks if not t.done()]
            assert len(live) == queue.concurrency, f"补位后应恰好 concurrency 个活 worker，实际 {len(live)}"
            assert victim not in queue._worker_tasks
            assert victim not in queue._worker_ids
            assert len(queue._worker_ids) == len(live)
            assert queue._worker_respawns >= 1
        finally:
            _shutdown(queue)

    asyncio.run(scenario())

def test_planned_retirement_is_logged_and_not_respawned(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """D4b：`set_concurrency(2→1)` 后多余 worker 按计划退休，监督不得把它撑回来。"""

    async def scenario() -> None:
        queue = tq.SimpleTaskQueue(concurrency=2)
        _start(queue)
        try:
            await asyncio.sleep(0.05)
            with caplog.at_level(logging.INFO, logger="app.services.task_queue"):
                queue.set_concurrency(1, persist=False)
                for _ in range(160):
                    if len([t for t in queue._worker_tasks if not t.done()]) == 1:
                        break
                    await asyncio.sleep(0.05)
                assert len([t for t in queue._worker_tasks if not t.done()]) == 1, "多余 worker 必须退休"
                queue._supervise_workers()
                queue._supervise_workers()
                await asyncio.sleep(0.2)
                live = [t for t in queue._worker_tasks if not t.done()]
                assert len(live) == 1, f"计划退休不得被监督补位（churn）：live={len(live)}"
            msgs = [r.getMessage() for r in caplog.records]
            assert any("planned_retirement" in m for m in msgs), f"退休须留证据：{msgs}"
            assert not any("supervisor respawned" in m for m in msgs), f"不得补位：{msgs}"
        finally:
            _shutdown(queue)

    asyncio.run(scenario())


def test_respawn_logs_the_dead_worker(caplog: pytest.LogCaptureFixture) -> None:
    """D3b/D4⑤：补位必须留出「谁死了、谁顶上」的机器可读证据。"""

    async def scenario() -> None:
        queue = tq.SimpleTaskQueue(concurrency=1)
        _start(queue)
        try:
            await asyncio.sleep(0.05)
            victim = queue._worker_tasks[0]
            victim.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await victim
            with caplog.at_level(logging.WARNING, logger="app.services.task_queue"):
                queue._supervise_workers()
            msgs = [r.getMessage() for r in caplog.records]
            assert any(
                m.startswith("supervisor respawned worker-") and "reason=dead worker-" in m for m in msgs
            ), f"补位须留证据：{msgs}"
        finally:
            _shutdown(queue)

    asyncio.run(scenario())
