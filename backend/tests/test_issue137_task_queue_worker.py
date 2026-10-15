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

    # ①.5 评审 T1：替身类必须与内建 `TimeoutError` 无继承关系，否则本机 3.12 上
    # 「抛替身」会被 `except TimeoutError` 蒙过去 ⇒ 改前也绿（恒真）。
    assert not issubclass(_Fake310AsyncTimeoutError, TimeoutError), (
        "替身类必须是非 TimeoutError 子类，才能模拟 3.10 的两类分立语义"
    )

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
                # N1（①.5 评审）：连续 3 个真实空闲窗口（2s×3 = 6.0s）后仍必须存活。
                await asyncio.sleep(6.0)
                assert not queue._worker_tasks[0].done(), (
                    "连续 3 个空闲窗口后 worker 仍必须存活（只测单次即时超时会被兜底蒙过）"
                )
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
            # N2（①.5 评审）：注入异常后必须再过一整个空闲窗口仍存活（只测「随即消费」
            # 会被「异常后立刻再抛」的时序蒙过）。
            await asyncio.sleep(2.6)
            assert not queue._worker_tasks[0].done(), "等待路径异常后 worker 必须长期存活"
        finally:
            _shutdown(queue)

    asyncio.run(scenario())


def test_token_without_pending_task_is_tolerated(caplog: pytest.LogCaptureFixture) -> None:
    """token 与待处理列表失步：丢 token 留告警，不得让 worker 死于 IndexError。"""

    async def scenario() -> None:
        queue = tq.SimpleTaskQueue(concurrency=1)
        _start(queue)
        try:
            with caplog.at_level(logging.WARNING, logger="app.services.task_queue"):
                queue._queue.put_nowait(None)  # 凭空一个 token，_pending_tasks 为空
                await asyncio.sleep(2.6)
            assert not queue._worker_tasks[0].done(), "token 失步不得杀死 worker"
            assert queue.queue_size == 0, "失步 token 应被丢弃"
            # N3（①.5 评审）：丢弃必须留机器可读的告警（不能静默吞掉）。
            msgs = [r.getMessage() for r in caplog.records]
            assert any("token dropped" in m for m in msgs), f"失步 token 必须留告警：{msgs}"
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
            # B2（③ 评审）：正常退休不是故障——不得出现 ERROR 级的「died/will respawn」。
            noisy = [
                (r.levelname, r.getMessage())
                for r in caplog.records
                if r.levelno >= logging.ERROR
            ]
            assert not noisy, f"计划退休不得记 ERROR：{noisy}"
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
                m.startswith("supervisor respawned worker-") and "reason=dead worker" in m for m in msgs
            ), f"补位须留证据：{msgs}"
        finally:
            _shutdown(queue)

    asyncio.run(scenario())


def test_respawned_worker_reuses_dead_slot_and_survives_idle_window(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """B1（③ 评审）：补位必须沿用死者的 slot id，并活过下一个空闲窗口。

    改前监督补位走自增 id（`_next_worker_id`），而`concurrency=1` 时那些 id 全都
    ≥ concurrency ⇒ 补位 worker 在首个空闲窗口就被自己的退休判据判死：
    `concurrency=1` 时 10s 内池子空着占 80%、`concurrency=2` 单点死亡后 8s 内
    只有 25% 时间满员（③ 评审实测）。既有 14 例对此全绿（变异 M13 逃生）。
    """

    async def scenario() -> None:
        queue = tq.SimpleTaskQueue(concurrency=2)
        _start(queue)
        try:
            await asyncio.sleep(0.05)
            victim = queue._worker_tasks[0]
            victim_id = queue._worker_ids[victim]
            victim.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await victim
            with caplog.at_level(logging.WARNING, logger="app.services.task_queue"):
                queue._supervise_workers()
            live = [task for task in queue._worker_tasks if not task.done()]
            assert len(live) == 2, f"必须补位回 concurrency=2，实际 {len(live)}"
            assert any(queue._worker_ids[task] == victim_id for task in live), (
                "补位必须复用死者 slot id="
                f"{victim_id}（不得自增出 ≥ concurrency 的新 id）："
                f"{[(t.get_name(), queue._worker_ids[t]) for t in live]}"
            )
            await asyncio.sleep(2.6)  # 活过一个完整空闲窗口
            after = [task for task in queue._worker_tasks if not task.done()]
            assert len(after) == 2, (
                f"补位 worker 不得在空闲窗口自我退休（监督 churn）：实际 {len(after)}"
            )
        finally:
            _shutdown(queue)

    asyncio.run(scenario())


def test_restore_wake_tokens_matches_pending_minus_qsize(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """N6/M12（③ 评审）：丢 token 的差额 = `len(_pending_tasks) - qsize`，只补差额。"""

    async def scenario() -> None:
        queue = tq.SimpleTaskQueue(concurrency=1)
        _start(queue)
        try:
            await asyncio.sleep(0.05)

            async def noop_run(task: tq.TaskItem) -> None:  # noqa: ARG001
                return None

            queue._run_recognition = noop_run  # type: ignore[method-assign]
            items = [_item("item-a"), _item("item-b")]
            queue._pending_tasks.extend(items)
            for item in items:
                queue._pending_items.add(queue._task_key(item))
            queue._queue.put_nowait(None)  # 只剩 1 个 token（另一个随死亡丢失）
            with caplog.at_level(logging.WARNING, logger="app.services.task_queue"):
                queue._restore_wake_tokens()
            # 同步调用内事件循环无法插入 ⇒ 此刻断言稳定。
            assert queue.queue_size == 2, f"只补差额：期望 2，实际 {queue.queue_size}"
            msgs = [r.getMessage() for r in caplog.records]
            assert any("restored 1 lost wake token" in m for m in msgs), f"须留证据：{msgs}"
        finally:
            _shutdown(queue)

    asyncio.run(scenario())


def test_marked_retiring_worker_finishes_task_then_exits() -> None:
    """B1/M10（③ 评审）：被标记退休的 worker 必须做完手上任务再退出（第二处退休分支）。"""

    async def scenario() -> None:
        queue = tq.SimpleTaskQueue(concurrency=1)
        _start(queue)
        seen: list[str] = []

        async def fake_run(task: tq.TaskItem) -> None:
            seen.append(task.item_id)

        queue._run_recognition = fake_run  # type: ignore[method-assign]
        try:
            await asyncio.sleep(0.05)
            queue._retiring_ids.add(queue._worker_ids[queue._worker_tasks[0]])
            queue.enqueue(_item("item-retire"))
            for _ in range(60):
                if seen:
                    break
                await asyncio.sleep(0.05)
            assert seen == ["item-retire"], "退休中的 worker 仍须完成手上的任务"
            for _ in range(60):
                if queue._worker_tasks[0].done():
                    break
                await asyncio.sleep(0.05)
            assert queue._worker_tasks[0].done(), "计划退休的 worker 完成当前件后必须退出"
            queue._supervise_workers()
            live = [task for task in queue._worker_tasks if not task.done()]
            assert len(live) == 1, f"concurrency 未变，必须补齐被退休的 slot：{len(live)}"
        finally:
            _shutdown(queue)

    asyncio.run(scenario())
