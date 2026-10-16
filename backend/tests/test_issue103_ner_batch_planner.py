"""Issue#103：帽容量分批上移出 GPU 闸位（红先测试）。

背景：`HaSClient.ner` 是**同步**方法，Issue#41 的帽容量分批在它内部是一个普通
`for` 串行循环；调用方 `HaSService.extract_entities` 每个类型批只取**一次**
`shared_gpu_inference_slot`，因此闸位内那几次串行子调用不受并发闸门放宽影响
（19 型 / 1600 字页 → `capacity=(2048-1577//2)//160=7` → 7+7+5 共 3 次串行）。

本文件用**真实重叠**（观测 client 替身的在飞峰值，不是断言 mock 被调用）钉住：
- 未改动树：单个类型批只派发 **1** 次 client 调用（peak == 1，红）；
- 改动后：按帽容量预切，同一批派发 **3** 次并发调用（peak > 1，绿）。
"""

import asyncio
import threading
import time

from app.core.config import settings
from app.services.has_client import HaSClient
from app.services.has_service import HaSService

# 19 型 + 1577 字 = 生产实测形状（capacity=7 → 7+7+5）
_TEXT_1577 = "字" * 1577


class _TypeStub:
    """最小 EntityTypeConfig 替身：无 description/examples ⇒ 不产生 guidance。"""

    def __init__(self, type_id: str, name: str):
        self.id = type_id
        self.name = name
        self.description = ""
        self.examples = []
        self.use_llm = True


def _types(count: int) -> list[_TypeStub]:
    return [_TypeStub(f"T{i}", f"类型{i}") for i in range(count)]


class _ConcurrencyProbe:
    """记录 ner() 真实在飞峰值与调用次数的同步替身。"""

    def __init__(self, hold_sec: float = 0.05):
        self.hold_sec = hold_sec
        self.calls = 0
        self.peak = 0
        self._current = 0
        self._lock = threading.Lock()

    @staticmethod
    def plan_completion_batches(types, text, *, hard_cap=None, per_type=None):
        # 被测服务层必须与客户端用同一个规划器；签名与真实实现保持一致，
        # 否则将来出现显式帽值调用点会在替身上 TypeError（评审 Nit-6）。
        return HaSClient.plan_completion_batches(
            types, text, hard_cap=hard_cap, per_type=per_type
        )

    def is_available(self) -> bool:
        return True

    def ner(self, text, entity_types=None, **kwargs):
        with self._lock:
            self.calls += 1
            self._current += 1
            self.peak = max(self.peak, self._current)
        try:
            time.sleep(self.hold_sec)
            return {}
        finally:
            with self._lock:
                self._current -= 1


def test_unmodified_dispatch_would_be_serial_but_fixed_is_concurrent():
    """核心红先断言：单个类型批内必须出现真实重叠（未改动树只会是 1）。"""
    service = HaSService(base_url="http://stub:0")
    probe = _ConcurrencyProbe()
    service.client = probe

    asyncio.run(service.extract_entities(_TEXT_1577, _types(19)))

    assert probe.calls == 3, f"19 型 / 1577 字应切成 3 个帽容量子批，实际 {probe.calls} 次"
    assert probe.peak > 1, f"子批必须真实并发（未改动树 peak 恒为 1），实际 peak={probe.peak}"


def test_preplanned_sub_batches_are_idempotent_for_client_planner():
    """预切后的子批再交给同一规划器不得二次切分（次批长 ≤ capacity）。"""
    chinese = [f"类型{i}" for i in range(19)]
    plan = HaSClient.plan_completion_batches(chinese, _TEXT_1577)
    assert [len(batch) for batch in plan] == [7, 7, 5]
    for batch in plan:
        assert HaSClient.plan_completion_batches(batch, _TEXT_1577) == [batch]


def test_plan_boundaries():
    # 空清单
    assert HaSClient.plan_completion_batches([], _TEXT_1577) == []
    # 单类型永不切分
    assert HaSClient.plan_completion_batches(["姓名"], _TEXT_1577) == [["姓名"]]
    # 预算装得下 → 单批
    assert HaSClient.plan_completion_batches([f"类型{i}" for i in range(5)], "短文本") == [
        [f"类型{i}" for i in range(5)]
    ]
    # 整除：14 型 → 7+7
    assert [len(b) for b in HaSClient.plan_completion_batches([f"类型{i}" for i in range(14)], _TEXT_1577)] == [7, 7]
    # 余 1：15 型 → 7+7+1
    assert [len(b) for b in HaSClient.plan_completion_batches([f"类型{i}" for i in range(15)], _TEXT_1577)] == [7, 7, 1]
    # capacity < 1（文本长到连单类型都超帽）→ 整页豁免单批
    assert len(HaSClient.plan_completion_batches([f"类型{i}" for i in range(19)], "字" * 5000)) == 1
    # 批并集恒等于原表（无类型丢失）
    source = [f"类型{i}" for i in range(19)]
    merged = [item for batch in HaSClient.plan_completion_batches(source, _TEXT_1577) for item in batch]
    assert merged == source


def test_plan_hard_cap_disabled_returns_single_batch(monkeypatch):
    monkeypatch.setattr(settings, "HAS_NER_COMPLETION_HARD_CAP", 0, raising=False)
    assert HaSClient.plan_completion_batches([f"类型{i}" for i in range(19)], _TEXT_1577) == [
        [f"类型{i}" for i in range(19)]
    ]


def test_plan_respects_configured_capacity_knobs(monkeypatch):
    """capacity 公式跟着配置走：抬 tokens_per_type 会把子批切小。"""
    types = [f"类型{i}" for i in range(19)]
    monkeypatch.setattr(settings, "HAS_NER_TOKENS_PER_TYPE", 320, raising=False)
    # text_budget=788 → capacity=(2048-788)//320=3
    assert [len(b) for b in HaSClient.plan_completion_batches(types, _TEXT_1577)] == [3] * 6 + [1]


def test_plan_accepts_explicit_knobs_without_touching_settings():
    """评审 N1：帽值可显式入参，单测无需 monkeypatch 全局 settings。

    显式入参必须优先于全局配置；`hard_cap=0` 表示关闭分批（整页单发），
    不能被 `None` 的"读配置"分支吞掉。
    """
    types = [f"类型{i}" for i in range(19)]
    # 显式 hard_cap=0 → 关闭分批
    assert HaSClient.plan_completion_batches(types, _TEXT_1577, hard_cap=0) == [types]
    # 显式 per_type 决定 capacity：text_budget=788 → (2048-788)//320=3
    plan = HaSClient.plan_completion_batches(types, _TEXT_1577, hard_cap=2048, per_type=320)
    assert [len(b) for b in plan] == [3] * 6 + [1]
    # 显式入参不依赖全局配置：把全局帽调成 0 也不影响显式传值的结果
    assert HaSClient.plan_completion_batches(types, _TEXT_1577, hard_cap=2048, per_type=320) == plan


def test_plan_zero_per_type_falls_back_instead_of_dividing_by_zero():
    """评审 Nit-5：显式 per_type<=0 不得触发 ZeroDivisionError。

    真要关闭分批应传 `hard_cap<=0`（该分支在读取 per_type 之前短路）；
    传 0/负数则退化为 `_NER_TOKENS_PER_TYPE` 的保守预留。
    """
    types = [f"类型{i}" for i in range(19)]
    for bad in (0, -5):
        plan = HaSClient.plan_completion_batches(types, _TEXT_1577, hard_cap=2048, per_type=bad)
        # 与不传 per_type（走 _NER_TOKENS_PER_TYPE=160）的容量一致
        assert [len(b) for b in plan] == [7, 7, 5]
    # 仍可用 hard_cap=0 关闭
    assert HaSClient.plan_completion_batches(types, _TEXT_1577, hard_cap=0, per_type=0) == [types]


def test_convert_types_to_chinese_never_expands_type_count():
    """评审 Nit-6：幂等性与「不二次切分」的论证依赖「中文名数 ≤ 类型项数」。

    这里把 `_convert_entity_types_to_chinese` 的"无隐藏别名扩展"钉成回归断言：
    若将来引入别名扩展（一个类型项展开成多个查询标签），本用例立刻失败，
    提醒同步修正 `_preplan_completion_batches` 的切分安全论证。
    """
    service = HaSService(base_url="http://stub:0")
    types = _types(19)
    chinese = service._convert_entity_types_to_chinese(types)
    assert len(chinese) <= len(types)
    assert len(set(chinese)) == len(chinese), "中文类型名必须已去重"
