"""Issue#274：首屏优先调度（第 1 页独占首发）+ 三段式阶段进度（stages 状态机）。

方案：``LexMask-workspace/docs/plans/2026-11-13-issue274-first-screen-stages.md``（v3）。
判据编号 AC1/AC2/AC3/AC4/AC8/AC9/AC10/AC11/M11 按方案 §4；测试形态沿
test_issue139/_issue238/_page_event_wiring 的既有 harness（_FakeStore/_vision_stubs/
_run/_queue），不自建平行框架。

判据面：
- AC1  独占首发：8 页卷 k=2、页 1 慢 ⇒ 页 1 仍是第一条页终态；页 1 恰跑一次（M11）；
        stage_total 恒全段页数（无 x/85 off-by-one）；开关关退回竞态；pages≤2 不独占；
        页 1 失败不熔断。
- AC2  stages 形状/时序/失败页语义/两段式单 page_loop + preflight detail 回填。
- AC3  stage_changed 事件恰两条（to/from/page=None）+ 事件写入故障 fail-open。
- AC4  加速器：默认与 config=on 都无 ②' 行（配置≠显示）；mock begin(from_prev=False)
        出现且事件不带 from。
- AC8  file 模式 stages 照常落账（回滚开关不破）。
- AC9  stage_changed 事件与 stages 账同源（每个事件 to 都有同名账行）。
- AC10 重跑：parse 窗口 stages 已清空（M4' 承重）+ 终账无上一轮残留行。
- AC11 stages 落账 fail-open：**条件式注入（仅 stages patch 抛错，N1）** ⇒ 页账/
        failed_pages/进度照常、主链完成；去 fail-open（M10）⇒ 上抛判红。
- F2   completed 页账补写 stage_duration_ms（与失败分支对称）。

ETA 的纯函数判据（AC5）在前端 ``task-stages.test.ts``。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from test_issue139_pipeline_asyncio_timeout import (
    _FILE_ID,
    _ITEM_ID,
    _JOB_ID,
    _FakeStore,
    _item,
    _patch_asyncio_timeout,
    _queue,
    _run,
    _vision_stubs,
)

from app.core.config import settings
from app.services import page_event_log as pel

_VISION_CFG: dict[str, Any] = {"visual_feature_types": [], "ocr_has_types": ["姓名"]}
_TWO_PASS_CFG: dict[str, Any] = {"visual_feature_types": ["签名"], "ocr_has_types": ["姓名"]}


# ---------------------------------------------------------------------------
# harness 扩展：replace_paths 感知 + get_item_performance（真库同形）
# ---------------------------------------------------------------------------
class _StagesFakeStore(_FakeStore):
    """issue139 替身 + get_item_performance + replace_paths 建模（真 JobStore 同形）。

    基础替身的 update_item_performance 只深合并、忽略 replace_paths —— stages 的
    「整替」语义必须被建模才能测 AC10；列表在 _deep_merge_dict 下本就整值替换，
    但显式建模 replace_paths 才不依赖这一巧合。
    """

    def get_item_performance(self, item_id: str) -> dict[str, Any]:
        from app.services.job_models import _coerce_performance

        return _coerce_performance(self.item.get("performance_json"))

    def update_item_performance(
        self, item_id: str, patch: dict[str, Any], *, replace_paths: Any = ()
    ) -> None:
        from app.services.job_models import _coerce_performance, _merge_with_replace_paths

        self.performance.append(patch)
        data = _merge_with_replace_paths(
            _coerce_performance(self.item.get("performance_json")), patch, replace_paths
        )
        self.item["performance_json"] = json.dumps(data, ensure_ascii=False, sort_keys=True)


def _merged(store: _StagesFakeStore) -> dict[str, Any]:
    return json.loads(store.item.get("performance_json") or "{}")


def _stages(store: _StagesFakeStore) -> list[dict[str, Any]]:
    raw = (_merged(store).get("recognition") or {}).get("stages")
    return raw if isinstance(raw, list) else []


def _stage_names(store: _StagesFakeStore) -> list[str]:
    return [str(row.get("name")) for row in _stages(store)]


def _stage(store: _StagesFakeStore, name: str) -> dict[str, Any] | None:
    for row in _stages(store):
        if row.get("name") == name:
            return row
    return None


def _first_terminal_page(store: _FakeStore) -> int | None:
    """按写序扫描 performance 补丁：第一条带 status 的页终态 = 最先落账页。"""
    for patch in store.performance:
        pages = (patch.get("recognition") or {}).get("pages") or {}
        for page, entry in pages.items():
            if isinstance(entry, dict) and "status" in entry:
                return int(page)
    return None


def _progress_tuples(store: _FakeStore) -> list[tuple[int, int, str]]:
    return [
        (
            int(call["kwargs"].get("current", 0) or 0),
            int(call["kwargs"].get("total", 0) or 0),
            str(call["kwargs"].get("message")),
        )
        for call in store.progress_calls
    ]


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
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _stage_changed_events(log_env) -> list[dict[str, Any]]:
    return [e for e in _events(log_env) if e.get("event") == "stage_changed"]


def _seed_stale_ledger(store: _StagesFakeStore) -> None:
    """预置上一轮旧账（全 completed stages + finished_at + 残留 progress），模拟重跑前状态。"""
    stale = {
        "recognition": {
            "stages": [
                {
                    "name": "preflight",
                    "started_at": "2026-11-12T00:00:00+00:00",
                    "finished_at": "2026-11-12T00:00:01+00:00",
                    "state": "completed",
                    "detail": {"routing_mode": "page"},
                },
                {
                    "name": "page_loop",
                    "started_at": "2026-11-12T00:00:01+00:00",
                    "finished_at": "2026-11-12T00:02:00+00:00",
                    "state": "completed",
                    "detail": {"page_count": 3, "completed_pages": 3},
                },
            ],
            "finished_at": "2026-11-12T00:02:00+00:00",
            "duration_ms": 120000,
        }
    }
    store.update_item_performance(_ITEM_ID, stale)


# ---------------------------------------------------------------------------
# AC1：独占首发
# ---------------------------------------------------------------------------
def test_a1_switch_default_is_true() -> None:
    """M2 承重：开关默认 True（缺省部署即享首屏优先，方案 §2.2.3）。"""
    assert settings.BATCH_RECOGNITION_FIRST_PAGE_PRIORITY is True


def test_a1_first_page_exclusive_launch_and_ledger_first(monkeypatch: pytest.MonkeyPatch) -> None:
    """8 页卷 k=2、页 1 慢：页 1 仍第一条页终态；页 1 恰跑一次；total 恒 8。"""
    _patch_asyncio_timeout(monkeypatch)
    monkeypatch.setattr(settings, "BATCH_RECOGNITION_PAGE_CONCURRENCY", 2, raising=False)
    monkeypatch.setattr(settings, "BATCH_RECOGNITION_FIRST_PAGE_PRIORITY", True, raising=False)
    store = _StagesFakeStore()
    queue = _queue(store)
    started: list[int] = []
    calls: dict[int, int] = {}
    exclusive_window_seen: list[int] = []

    async def _detect(file_id: str, page: int, **kwargs: Any) -> dict[str, Any]:
        calls[page] = calls.get(page, 0) + 1
        started.append(page)
        if page == 1:
            # 独占窗口观测点：页 1 在飞时其余页必须尚未启动（§2.2.1 规范 a）。
            await asyncio.sleep(0.15)
            exclusive_window_seen.extend(started)
            return {"boxes": []}
        await asyncio.sleep(0.02)
        return {"boxes": []}

    _vision_stubs(monkeypatch, pages=8, detect=_detect)

    async def scenario() -> None:
        await queue._run_vision(_item(), dict(_VISION_CFG))

    _run(scenario)

    # 独占窗口：页 1 独飞（v2/F5-a；M11 的正向观测）。
    assert exclusive_window_seen == [1], exclusive_window_seen
    # 页 1 恰好跑一次（M11 承重断言）。
    assert calls.get(1) == 1, calls
    assert sorted(calls) == [1, 2, 3, 4, 5, 6, 7, 8], calls
    # 最先落账 = 页 1（AC1 承重；现状无独占时页 2 先落账 = R1 红形态）。
    assert _first_terminal_page(store) == 1, store.page_statuses()
    # 页账全 completed。
    assert store.page_statuses() == {str(p): "completed" for p in range(1, 9)}
    # stage_total 口径：每个 bump 的 total 恒为全段页数 8（v2/F5-b，无 x/85）。
    bumps = [t for t in _progress_tuples(store) if t[2].endswith(tuple(f"page {p}/8" for p in range(1, 9))) or "/8" in t[2]]
    assert bumps, _progress_tuples(store)
    assert all(t[1] == 8 for t in bumps), _progress_tuples(store)
    # 首个 bump（current=1）对应页 1。
    assert bumps[0][0] == 1 and "page 1/8" in bumps[0][2], bumps[:2]


def test_a1_switch_off_reverts_to_race(monkeypatch: pytest.MonkeyPatch) -> None:
    """开关 False ⇒ 退回旧行为：页 2（快页）先落账（R1 红形态复现，开关有牙）。"""
    _patch_asyncio_timeout(monkeypatch)
    monkeypatch.setattr(settings, "BATCH_RECOGNITION_PAGE_CONCURRENCY", 2, raising=False)
    monkeypatch.setattr(settings, "BATCH_RECOGNITION_FIRST_PAGE_PRIORITY", False, raising=False)
    store = _StagesFakeStore()
    queue = _queue(store)

    async def _detect(file_id: str, page: int, **kwargs: Any) -> dict[str, Any]:
        await asyncio.sleep(0.15 if page == 1 else 0.02)
        return {"boxes": []}

    _vision_stubs(monkeypatch, pages=8, detect=_detect)

    async def scenario() -> None:
        await queue._run_vision(_item(), dict(_VISION_CFG))

    _run(scenario)
    assert _first_terminal_page(store) == 2, store.page_statuses()


def test_a1_small_volume_no_exclusive(monkeypatch: pytest.MonkeyPatch) -> None:
    """pages=2（≤ 触发面）：不独占——页 1、2 并发起飞（#175 a9026e3 同款判定）。"""
    _patch_asyncio_timeout(monkeypatch)
    monkeypatch.setattr(settings, "BATCH_RECOGNITION_PAGE_CONCURRENCY", 2, raising=False)
    monkeypatch.setattr(settings, "BATCH_RECOGNITION_FIRST_PAGE_PRIORITY", True, raising=False)
    store = _StagesFakeStore()
    queue = _queue(store)
    started: list[int] = []
    concurrent_window: list[int] = []

    async def _detect(file_id: str, page: int, **kwargs: Any) -> dict[str, Any]:
        started.append(page)
        if page == 1:
            await asyncio.sleep(0.15)
            concurrent_window.extend(started)
            return {"boxes": []}
        await asyncio.sleep(0.02)
        return {"boxes": []}

    _vision_stubs(monkeypatch, pages=2, detect=_detect)

    async def scenario() -> None:
        await queue._run_vision(_item(), dict(_VISION_CFG))

    _run(scenario)
    # 页 1 在飞时页 2 已同飞 = 无独占窗口；页 2 先落账。
    assert concurrent_window == [1, 2], concurrent_window
    assert _first_terminal_page(store) == 2, store.page_statuses()


def test_a1_m3_trigger_face_pages2_k1_no_exclusive(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """触发面 `>2` 子句的杀手测态：pages=2 ∧ k=1（验收 M3 修复轮）。

    现有 pages=2∧k=2 测态下 `len(page_ids) > page_concurrency`（2>2=False）
    先否决，掩蔽 `>2` 子句——M3 变异（>2→>1）前后行为全同而存活。k=1 时
    第二子句放行（2>1=True），`>2` 成为唯一否决面：正常代码不独占
    （2>2=False、绝无 exclusive launch 日志），变异体必然进入独占窗口。
    独占窗口的显式事件 = 「first-page exclusive launch」日志行（run_page 的
    store 侧副作用全在 page_sem 之内，k=1 下与非独占同形，不可作观测面）。
    k=1 是 GPU 压力强制降并发的真实可达场景（_effective_vision_page_concurrency）。
    """
    import logging

    _patch_asyncio_timeout(monkeypatch)
    monkeypatch.setattr(settings, "BATCH_RECOGNITION_PAGE_CONCURRENCY", 1, raising=False)
    monkeypatch.setattr(settings, "BATCH_RECOGNITION_FIRST_PAGE_PRIORITY", True, raising=False)
    store = _StagesFakeStore()
    queue = _queue(store)
    started: list[int] = []

    async def _detect(file_id: str, page: int, **kwargs: Any) -> dict[str, Any]:
        started.append(page)
        await asyncio.sleep(0.02)
        return {"boxes": []}

    _vision_stubs(monkeypatch, pages=2, detect=_detect)

    async def scenario() -> None:
        await queue._run_vision(_item(), dict(_VISION_CFG))

    with caplog.at_level(logging.INFO, logger="app.services.task_queue_pipelines"):
        _run(scenario)

    # 杀手断言：pages=2（≤ 触发面）即使 k=1 也不得进入独占首发窗口。
    exclusive_lines = [
        r.getMessage() for r in caplog.records if "first-page exclusive launch" in r.getMessage()
    ]
    assert exclusive_lines == [], exclusive_lines
    # 防假绿：不独占不影响终态集合与页账本完整性（page_statuses 键为字符串页号）。
    assert sorted(store.page_statuses()) == ["1", "2"], store.page_statuses()
    assert sorted(started) == [1, 2], started


def test_a1_page1_failure_does_not_block_rest(monkeypatch: pytest.MonkeyPatch) -> None:
    """页 1 失败不熔断（#199 页级隔离）：其余页照常完成。"""
    _patch_asyncio_timeout(monkeypatch)
    monkeypatch.setattr(settings, "BATCH_RECOGNITION_PAGE_CONCURRENCY", 2, raising=False)
    monkeypatch.setattr(settings, "BATCH_RECOGNITION_FIRST_PAGE_PRIORITY", True, raising=False)
    store = _StagesFakeStore()
    queue = _queue(store)

    async def _detect(file_id: str, page: int, **kwargs: Any) -> dict[str, Any]:
        if page == 1:
            raise ValueError("page-1-boom")
        await asyncio.sleep(0.02)
        return {"boxes": []}

    _vision_stubs(monkeypatch, pages=4, detect=_detect)

    async def scenario() -> None:
        await queue._run_vision(_item(), dict(_VISION_CFG))

    _run(scenario)
    statuses = store.page_statuses()
    assert statuses.get("1") == "failed", statuses
    assert {statuses[str(p)] for p in range(2, 5)} == {"completed"}, statuses


# ---------------------------------------------------------------------------
# AC2：stages 形状 / 失败页语义 / 两段式 / preflight detail 回填
# ---------------------------------------------------------------------------
def test_a2_stages_shape_and_order(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_asyncio_timeout(monkeypatch)
    store = _StagesFakeStore()
    queue = _queue(store)
    _vision_stubs(monkeypatch, pages=3, detect=_ok_detect)

    async def scenario() -> None:
        await queue._run_vision(_item(), dict(_VISION_CFG))

    _run(scenario)

    rows = _stages(store)
    assert [r["name"] for r in rows] == ["preflight", "page_loop"], rows
    for row in rows:
        assert set(row) == {"name", "started_at", "finished_at", "state", "detail"}, row
        assert row["started_at"] and row["finished_at"], row
        assert row["state"] == "completed", row
    assert rows[0]["started_at"] <= rows[1]["started_at"], rows
    detail = rows[1]["detail"]
    assert detail["page_count"] == 3, detail
    assert detail["page_concurrency"] >= 1, detail
    assert detail["segment_total"] == 1, detail
    assert detail["completed_pages"] == 3, detail
    assert detail["failed_pages_count"] == 0, detail
    assert _stage(store, "preflight")["detail"]["routing_mode"] == str(settings.RECOGNITION_ROUTING_MODE)


def _ok() -> dict[str, Any]:
    return {"boxes": []}


async def _ok_detect(file_id: str, page: int, **kwargs: Any) -> dict[str, Any]:
    """vision_detect 桩：空框成功（可 await）。"""
    return {"boxes": []}


def test_a2_failed_pages_keep_page_loop_completed(monkeypatch: pytest.MonkeyPatch) -> None:
    """失败页不改 page_loop state（诚实记账走 failed_pages），detail 计数如实。"""
    _patch_asyncio_timeout(monkeypatch)
    store = _StagesFakeStore()
    queue = _queue(store)

    async def _detect(file_id: str, page: int, **kwargs: Any) -> dict[str, Any]:
        if page == 2:
            raise ValueError("bad-page")
        return {"boxes": []}

    _vision_stubs(monkeypatch, pages=3, detect=_detect)

    async def scenario() -> None:
        await queue._run_vision(_item(), dict(_VISION_CFG))

    _run(scenario)
    page_loop = _stage(store, "page_loop")
    assert page_loop is not None and page_loop["state"] == "completed", page_loop
    assert page_loop["detail"]["completed_pages"] == 2, page_loop
    assert page_loop["detail"]["failed_pages_count"] == 1, page_loop
    failed = (_merged(store).get("recognition") or {}).get("failed_pages")
    assert failed and "2" in failed, failed


def test_a2_two_pass_segments_one_page_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """两段式（OCR 段 + 视觉合并段）：stages 仍是一条 page_loop，detail 如实。"""
    _patch_asyncio_timeout(monkeypatch)
    store = _StagesFakeStore()
    queue = _queue(store)
    _vision_stubs(monkeypatch, pages=3, detect=_ok_detect)

    async def scenario() -> None:
        await queue._run_vision(_item(), dict(_TWO_PASS_CFG))

    _run(scenario)
    assert _stage_names(store) == ["preflight", "page_loop"], _stages(store)
    page_loop = _stage(store, "page_loop")
    assert page_loop is not None and page_loop["state"] == "completed", page_loop
    assert page_loop["detail"]["segment_total"] == 2, page_loop
    assert page_loop["detail"]["completed_pages"] == 3, page_loop
    # 两段都跑过：合并段页账重写后带视觉段标签（F2 stage_duration_ms 由段标签可分）。
    statuses = store.page_statuses()
    assert statuses == {"1": "completed", "2": "completed", "3": "completed"}, statuses


def test_a2_precheck_detail_backfill(monkeypatch: pytest.MonkeyPatch) -> None:
    """preflight detail 回填：la_probe/page_concurrency/sparse_probe/routing_mode。"""
    _patch_asyncio_timeout(monkeypatch)
    store = _StagesFakeStore()
    queue = _queue(store)
    # 勾视觉要素 ⇒ LA 预检真被探测（harness 桩成可用）⇒ la_probe=ok。
    _vision_stubs(monkeypatch, pages=3, detect=_ok_detect)

    async def scenario() -> None:
        await queue._run_vision(_item(), dict(_TWO_PASS_CFG))

    _run(scenario)
    preflight = _stage(store, "preflight")
    assert preflight is not None, _stages(store)
    detail = preflight["detail"]
    assert detail["la_probe"] == "ok", detail
    assert detail["page_concurrency"] >= 1, detail
    assert "sparse_probe" in detail, detail
    assert detail["routing_mode"] in {"page", "file"}, detail


# ---------------------------------------------------------------------------
# AC3/AC9：stage_changed 事件 + 同源
# ---------------------------------------------------------------------------
def _patch_full_chain(monkeypatch: pytest.MonkeyPatch, store: _FakeStore, detect: Any, pages: int = 3) -> None:
    """全链（_run_recognition）打桩：解析短路 + 视觉桩（沿 page_event_wiring 形态）。"""
    import app.services.file_operations as fo

    _patch_asyncio_timeout(monkeypatch)
    store.job["config_json"] = json.dumps(_VISION_CFG)
    _vision_stubs(monkeypatch, pages=pages, detect=detect)

    async def _noop_parse(task: Any) -> None:
        return None

    monkeypatch.setattr(queue, "_parse_file", _noop_parse)


def queue_for_patch(store: _FakeStore):
    return _queue(store)


def test_a3_stage_changed_events_and_ledger_same_source(monkeypatch: pytest.MonkeyPatch, log_env) -> None:
    """AC3/AC9：恰两条 stage_changed（preflight 无 from、page_loop 带 from、page=None），
    且每个事件 to 与 stages 账行同名（同源派生）。"""
    store = _StagesFakeStore()
    queue = _queue(store)

    async def _detect(file_id: str, page: int, **kwargs: Any) -> dict[str, Any]:
        return {"boxes": []}

    _vision_stubs(monkeypatch, pages=3, detect=_detect)

    async def _noop_parse(task: Any) -> None:
        return None

    monkeypatch.setattr(queue, "_parse_file", _noop_parse)

    async def scenario() -> None:
        await queue._run_recognition(_item())

    _run(scenario)

    events = _stage_changed_events(log_env)
    assert len(events) == 2, events
    assert events[0]["to"] == "preflight" and "from" not in events[0], events[0]
    assert events[1]["to"] == "page_loop" and events[1].get("from") == "preflight", events[1]
    for e in events:
        assert e["page"] is None, e
        assert e["job_id"] == _JOB_ID and e["item_id"] == _ITEM_ID, e
    # AC9：事件与账同源。
    names = _stage_names(store)
    assert names == [e["to"] for e in events] == ["preflight", "page_loop"], (names, events)


def test_a3_event_fault_injection_fail_open(monkeypatch: pytest.MonkeyPatch, log_env) -> None:
    """AC3 故障注入：事件写入抛错 ⇒ 主链与 stages 落账不受影响。"""
    store = _StagesFakeStore()
    queue = _queue(store)

    async def _detect(file_id: str, page: int, **kwargs: Any) -> dict[str, Any]:
        return {"boxes": []}

    _vision_stubs(monkeypatch, pages=3, detect=_detect)

    async def _noop_parse(task: Any) -> None:
        return None

    monkeypatch.setattr(queue, "_parse_file", _noop_parse)

    def _boom_fd(*args: Any, **kwargs: Any) -> Any:
        raise OSError("injected fd failure")

    # 打在 log_event 内部写入口（_open_fd）：log_event 自身契约 = 任何失败只计数
    # 返回 False、绝不上抛——这正是 #279 的 fail-open 面；替换 log_event 本体会
    # 连带击穿 run_page 页事件调用点（生产代码未包 try，靠的是该契约）。
    monkeypatch.setattr(pel, "_open_fd", _boom_fd)

    async def scenario() -> None:
        await queue._run_recognition(_item())

    _run(scenario)
    assert store.page_statuses() == {"1": "completed", "2": "completed", "3": "completed"}
    assert _stage_names(store) == ["preflight", "page_loop"], _stages(store)


# ---------------------------------------------------------------------------
# AC4：加速器段（配置≠显示 + mock begin 无 from）
# ---------------------------------------------------------------------------
def test_a4_no_accelerator_row_by_default_or_config_only(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_asyncio_timeout(monkeypatch)
    # RECOGNITION_ACCELERATOR_ENABLED 尚不存在（#275 交付）；pydantic settings 拒绝
    # setattr 未声明字段 ⇒ 用 object.__setattr__ 注入实例属性模拟「配置 on」，
    # 验证代码路径不读配置（stages 自声明）⇒ 仍无 ②' 行。
    object.__setattr__(settings, "RECOGNITION_ACCELERATOR_ENABLED", "on")
    store = _StagesFakeStore()
    queue = _queue(store)
    _vision_stubs(monkeypatch, pages=3, detect=_ok_detect)

    try:
        async def scenario() -> None:
            await queue._run_vision(_item(), dict(_VISION_CFG))

        _run(scenario)
    finally:
        object.__delattr__(settings, "RECOGNITION_ACCELERATOR_ENABLED")
    assert "accelerator" not in _stage_names(store), _stages(store)

    # mock 驱动：加速器真实开跑（#275 生产者未来的调用形态）⇒ 出现 ②' 行且事件无 from。
    events_before = len(_stage_changed_events(log_env_dir_default()))
    queue._stage_begin(store, _ITEM_ID, "accelerator", {"blocks": 26}, from_prev=False)
    assert _stage_names(store)[-1] == "accelerator", _stages(store)
    acc = _stage(store, "accelerator")
    assert acc is not None and acc["state"] == "running", acc
    # page_loop 不受影响。
    assert _stage(store, "page_loop")["state"] == "completed"


def log_env_dir_default() -> Any:
    from app.services.page_event_log import _log_root

    return _log_root().parent


def test_a4_accelerator_begin_event_has_no_from(monkeypatch: pytest.MonkeyPatch, log_env) -> None:
    """v2/F8 + v3/M6/M9 承重：并行段 begin 事件——**带页上下文也强制 page=None**
    （M6：显式 page=None 是防线）、省略 from（复盘工具不得误读为 ② 后继）。"""
    store = _StagesFakeStore()
    queue = _queue(store)
    # 预置一条 page_loop 行（M9 承重前提：prev 非空，from_prev=False 才有判别力）。
    store.update_item_performance(
        _ITEM_ID,
        {"recognition": {"stages": [
            {"name": "preflight", "started_at": "2026-11-13T00:00:00+00:00",
             "finished_at": "2026-11-13T00:00:01+00:00", "state": "completed", "detail": {}},
            {"name": "page_loop", "started_at": "2026-11-13T00:00:01+00:00",
             "finished_at": None, "state": "running", "detail": {}},
        ]}},
        replace_paths=(("recognition", "stages"),),
    )
    # 挂任务级上下文 + **页上下文**（M6 判据的承重前提：显式 page=None 必须能
    # 压过已设置的页号）后直接驱动 begin。
    pel.set_event_context(_JOB_ID, _ITEM_ID, _FILE_ID)
    pel.set_current_page(7)
    try:
        queue._stage_begin(store, _ITEM_ID, "accelerator", {"blocks": 1}, from_prev=False)
    finally:
        pel.clear_event_context()
        pel.reset_for_tests()
    assert "accelerator" in _stage_names(store), _stages(store)
    events = [e for e in _events(log_env) if e.get("event") == "stage_changed"]
    assert len(events) == 1, events
    assert events[0]["page"] is None, events[0]   # M6：带页上下文仍强制 None
    assert "from" not in events[0], events[0]     # M9：并行段无 from


# ---------------------------------------------------------------------------
# AC8：file 模式回滚路径 stages 照常落账
# ---------------------------------------------------------------------------
def test_a8_file_mode_stages_still_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_asyncio_timeout(monkeypatch)
    monkeypatch.setattr(settings, "RECOGNITION_ROUTING_MODE", "file", raising=False)
    store = _StagesFakeStore()
    queue = _queue(store)
    _vision_stubs(monkeypatch, pages=3, detect=_ok_detect)

    async def scenario() -> None:
        await queue._run_vision(_item(), dict(_VISION_CFG))

    _run(scenario)
    assert _stage_names(store) == ["preflight", "page_loop"], _stages(store)
    assert _stage(store, "preflight")["detail"]["routing_mode"] == "file", _stage(store, "preflight")
    assert all(row["state"] == "completed" for row in _stages(store))


# ---------------------------------------------------------------------------
# AC10/M4'：重跑清账（parse 窗口无旧账）+ 终账无残留行
# ---------------------------------------------------------------------------
def test_a10_rerun_clears_at_parse_and_replaces_rows(monkeypatch: pytest.MonkeyPatch, log_env) -> None:
    """重跑：识别 try 入口（parse 前）stages 已清空（M4' 承重）；终账恰两条新行（AC10）。"""
    store = _StagesFakeStore()
    _seed_stale_ledger(store)
    queue = _queue(store)

    parse_time_stages: list[Any] = []

    async def _detect(file_id: str, page: int, **kwargs: Any) -> dict[str, Any]:
        return {"boxes": []}

    _vision_stubs(monkeypatch, pages=3, detect=_detect)

    async def _spy_parse(task: Any) -> None:
        parse_time_stages.append(((_merged(store).get("recognition") or {}).get("stages")))

    monkeypatch.setattr(queue, "_parse_file", _spy_parse)

    async def scenario() -> None:
        await queue._run_recognition(_item())

    _run(scenario)

    # M4' 承重：parse 窗口内 stages 已清（清账写点在 parse 之前）。
    assert parse_time_stages == [[]], parse_time_stages
    # AC10：终账恰两条新行，无上一轮残留。
    rows = _stages(store)
    assert [r["name"] for r in rows] == ["preflight", "page_loop"], rows
    assert all(r["state"] == "completed" for r in rows), rows
    assert rows[1]["detail"]["page_count"] == 3, rows


# ---------------------------------------------------------------------------
# AC11/N1：条件式故障注入（仅 stages patch 抛错）
# ---------------------------------------------------------------------------
def test_a11_conditional_injection_fail_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC11（v3/N1 条件式注入）：仅 stages 写抛错 ⇒ 页账/failed_pages/进度照常、主链完成。

    M10（去 fail-open）下该注入会把异常上抛出 _run_vision ⇒ 本用例判红。
    实现注记：``_record_item_performance``（task_queue.py 既有）自带一层 fail-open，
    会兜住 store 抛错——为让本用例真正钉住 ``_stage_begin/_stage_end`` **自己**的
    fail-open（M10 变异面），这里把它桩成直通（不吞异常），使 stages 写路径的
    唯一防线就是被测的 try/except。
    """
    _patch_asyncio_timeout(monkeypatch)
    store = _StagesFakeStore()
    queue = _queue(store)
    original_update = store.update_item_performance

    # 直通桩：_record_item_performance 不吞异常（正常实现下异常由 _stage_begin 的
    # try 吸收；M10 变异（去 try）下直传 scenario ⇒ 判红）。
    def _passthrough_record(store_: Any, item_id: str, patch: dict[str, Any], *, replace_paths: Any = ()) -> None:
        store_.update_item_performance(item_id, patch, replace_paths=replace_paths)

    queue._record_item_performance = _passthrough_record  # type: ignore[method-assign]

    def _conditional_update(item_id: str, patch: dict[str, Any], **kwargs: Any) -> None:
        rec = patch.get("recognition") if isinstance(patch, dict) else None
        if isinstance(rec, dict) and "stages" in rec:
            raise RuntimeError("injected stages-write failure")
        return original_update(item_id, patch, **kwargs)

    store.update_item_performance = _conditional_update  # type: ignore[method-assign]

    async def _detect(file_id: str, page: int, **kwargs: Any) -> dict[str, Any]:
        if page == 2:
            raise ValueError("bad-page")
        return {"boxes": []}

    _vision_stubs(monkeypatch, pages=3, detect=_detect)

    async def scenario() -> None:
        await queue._run_vision(_item(), dict(_VISION_CFG))

    _run(scenario)  # 不上抛 = 主链完成

    statuses = store.page_statuses()
    assert statuses == {"1": "completed", "2": "failed", "3": "completed"}, statuses
    failed = (_merged(store).get("recognition") or {}).get("failed_pages")
    assert failed and "2" in failed, failed
    # 进度照常（update_item_progress 是独立通道）。
    completes = [t for t in _progress_tuples(store) if "vision_recognition_complete" in t[2]]
    assert completes, _progress_tuples(store)


# ---------------------------------------------------------------------------
# F2：completed 页账补写 stage_duration_ms（与失败分支对称）
# ---------------------------------------------------------------------------
def test_f2_completed_page_carries_stage_duration_ms(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_asyncio_timeout(monkeypatch)
    store = _StagesFakeStore()
    queue = _queue(store)
    _vision_stubs(monkeypatch, pages=2, detect=_ok_detect)

    async def scenario() -> None:
        await queue._run_vision(_item(), dict(_VISION_CFG))

    _run(scenario)
    entry = store.page_entry(1)
    assert entry.get("status") == "completed", entry
    sd = entry.get("stage_duration_ms")
    assert isinstance(sd, dict) and set(sd) == {"视觉识别"}, entry
    assert isinstance(sd["视觉识别"], int) and sd["视觉识别"] >= 0, entry


def test_a2_precheck_la_probe_skipped_when_no_visual_types(monkeypatch: pytest.MonkeyPatch) -> None:
    """OCR-only（la_available=None=无需探测）⇒ la_probe=skipped（诚实记账）。"""
    _patch_asyncio_timeout(monkeypatch)
    store = _StagesFakeStore()
    queue = _queue(store)
    _vision_stubs(monkeypatch, pages=2, detect=_ok_detect)

    async def scenario() -> None:
        await queue._run_vision(_item(), dict(_VISION_CFG))

    _run(scenario)
    detail = _stage(store, "preflight")["detail"]
    assert detail["la_probe"] == "skipped", detail
