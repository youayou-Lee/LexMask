# Issue#137 设计：批量队列 worker 静默死亡修复

> 关联 Issue：[#137](https://github.com/youayou-Lee/LexMask/issues/137)（`smart_batch` 在飞入队后 worker 不取件，仅「重启恢复路径」会被处理）
> 验收方案（私有）：`LexMask-workspace/docs/acceptance/2026-10-16-issue137-worker-timeout-验收方案.md`

## 1. 现象

云实例（Python **3.10.12**）上，运行中的后端里新提交的 `smart_batch` 作业长期停在 `status=queued` / item `status=pending`，日志里只有入队那行 `enqueued recognition … (queue_size=1)`，**没有任何** `worker-N processing` 行；重启后端后同一作业 ~51 秒内完成。

## 2. 根因（已复现、有机器证据）

`backend/app/services/task_queue.py::SimpleTaskQueue._worker_loop` 用

```python
await asyncio.wait_for(self._queue.get(), timeout=2.0)
...
except TimeoutError:          # ← 3.10 上捕不到
```

在 **Python 3.10** 中 `asyncio.TimeoutError` 与内建 `TimeoutError` 是**两个不相干的类**（3.11 才合并）：

```
# 云实例 Python 3.10.12
asyncio.TimeoutError is TimeoutError            -> False
issubclass(asyncio.TimeoutError, TimeoutError)  -> False
# 本地开发机 Python 3.12.3：两者均 True ⇒ 本地复现不出
```

因此每个 worker 在**启动后首个 2 秒空闲超时**时抛出未捕获的 `asyncio.TimeoutError` 而退出；又因为任务对象被 `self._worker_tasks` 强引用，asyncio 连 `Task exception was never retrieved` 都不打印 ⇒ **完全静默、无日志可查**。

这与「只有重启恢复路径能跑」完全自洽：恢复件在 lifespan 中于启动后 ~0s 入队（worker 尚未触发首个 2s 超时），而在飞提交件都发生在 2s 之后。

实例插桩证据（`[DIAG137]`）：

```
start:       id(self)=…592  id(_queue)=…640  running=False conc=2 workers=[]
worker-0 enter: id(self)=…592 id(_queue)=…448 conc=2
worker-1 enter: id(self)=…592 id(_queue)=…448 conc=2
enqueue:     id(self)=…592  id(_queue)=…448  qsize=1 pending=1 running=True conc=2
             workers=[('Task-3', True), ('Task-4', True)]   ← 两个 worker 任务均已 done
```

入队与 worker 用的是**同一个队列对象**（id 一致），`running=True`、token 在队列、`pending=1`，只是**没有存活的 worker**。

## 3. 修复设计

改动只落在 `backend/app/services/task_queue.py`（外加测试）。

1. **超时捕获跨版本正确**：`except (TimeoutError, asyncio.TimeoutError):`。
   3.11+ 两者同一，元组写法无副作用。
2. **worker 循环兜底**：worker 主体的任何未预期异常都不得静默结束 worker——记录（`logger.exception`）后继续下一轮，带 0.2s 退避避免异常风暴空转。
3. **worker 存活性监督**：看门狗周期内检查 `self._worker_tasks` 中 `done()` 的 worker，**记录其异常**（`task.exception()`）并按 `concurrency` 补位；worker 退出必须留一行日志（含 worker_id 与原因）。
4. **待处理列表防御**：token 与 `_pending_tasks` 若失步（空表却取到 token），跳过该 token 并告警，而不是让 `pop(0)` 以 `IndexError` 杀死 worker。
5. **可观测性**：入队/消费日志补齐 worker 存活数，便于一眼看出「队列有件但无人消费」。

不改：优先级排序、去重（`_pending_items`）、`set_concurrency` 语义、上报字段口径——避免动到 #131 测速所依赖的行为。

## 4. 测试设计（与 3.10/3.11+ 双版本兼容）

| 用例 | 断言 |
|---|---|
| `test_worker_survives_idle_timeout` | worker 空闲超过超时窗口后 `task.done()` 仍为 False（3.10 回归核心） |
| `test_worker_loop_catches_asyncio_timeout_error` | monkeypatch `asyncio.wait_for` 抛 `asyncio.TimeoutError()` ⇒ worker 不退出、继续消费后入队的任务 |
| `test_enqueue_after_idle_is_consumed` | 启动 → 空闲 > 超时窗口 → 入队 ⇒ 任务在 2s 内被消费（端到端队列语义） |
| `test_worker_unexpected_exception_does_not_kill_it` | 注入 runner 抛异常 ⇒ 记录后 worker 存活，后续任务仍被消费 |
| `test_watchdog_respawns_dead_worker` | 人为 cancel 一个 worker ⇒ 监督逻辑补位至 `concurrency` |
| `test_token_without_pending_task_is_tolerated` | 制造 token 多于待处理项 ⇒ 不抛 `IndexError`，worker 存活 |

## 5. 验收与验证矩阵

- **自动化**：上表用例；`backend/tests/` 中既有队列/作业相关用例零回归；`eval-ng` 基线与全量失败名集合与 preview 基线逐条一致。
- **云实例（门④，必需）**：该缺陷只在 Python 3.10 复现 ⇒ 必须在云实例上验证：重启带修复的后端 → 空闲 >2s → 提交 `smart_batch` ⇒ 作业在飞被消费（`worker-N processing` 出现、状态脱离 `queued`）。
- **回归面**：#131 的 D7 parity 依赖该路径，修复后重跑；`--driver batch` 与旧 `benchmark_throughput.py` 共用此路径，两侧同受益。