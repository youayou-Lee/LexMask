"""shared_gpu_inference_slot 并发闸门语义。

背景：原实现是进程级 degree-1 asyncio.Lock，把 vLLM 双实例的 HaS NER
压成全局串行（2026-07 万级批量吞吐优化 P0）。改为 loop-aware 可配置
Semaphore：默认 1 = 原行为；HAS_NER_GLOBAL_MAX_INFLIGHT 放开并发；
SERIALIZE_SHARED_GPU_MODELS=False 完全旁路。
"""

import asyncio

from app.core import gpu_inference_gate as gate
from app.core.config import settings


async def _measure_peak(workers: int, hold_sec: float = 0.02) -> int:
    peak = 0
    current = 0

    async def worker() -> None:
        nonlocal peak, current
        async with gate.shared_gpu_inference_slot("test"):
            current += 1
            peak = max(peak, current)
            await asyncio.sleep(hold_sec)
            current -= 1

    await asyncio.gather(*(worker() for _ in range(workers)))
    return peak


def test_default_serializes(monkeypatch):
    monkeypatch.setattr(settings, "SERIALIZE_SHARED_GPU_MODELS", True)
    monkeypatch.setattr(settings, "HAS_NER_GLOBAL_MAX_INFLIGHT", 1, raising=False)
    assert asyncio.run(_measure_peak(8)) == 1


def test_configured_inflight(monkeypatch):
    monkeypatch.setattr(settings, "SERIALIZE_SHARED_GPU_MODELS", True)
    monkeypatch.setattr(settings, "HAS_NER_GLOBAL_MAX_INFLIGHT", 4, raising=False)
    assert asyncio.run(_measure_peak(8)) == 4


def test_gate_bypass(monkeypatch):
    monkeypatch.setattr(settings, "SERIALIZE_SHARED_GPU_MODELS", False)
    assert asyncio.run(_measure_peak(8)) == 8


def test_new_loop_rebinds_semaphore(monkeypatch):
    """跨 asyncio.run（新事件循环）闸门必须重建，不复用旧 loop 的信号量。"""
    monkeypatch.setattr(settings, "SERIALIZE_SHARED_GPU_MODELS", True)
    monkeypatch.setattr(settings, "HAS_NER_GLOBAL_MAX_INFLIGHT", 2, raising=False)
    assert asyncio.run(_measure_peak(6)) == 2
    assert asyncio.run(_measure_peak(6)) == 2


def test_config_change_rebuilds_gate(monkeypatch):
    monkeypatch.setattr(settings, "SERIALIZE_SHARED_GPU_MODELS", True)
    monkeypatch.setattr(settings, "HAS_NER_GLOBAL_MAX_INFLIGHT", 1, raising=False)
    assert asyncio.run(_measure_peak(6)) == 1
    monkeypatch.setattr(settings, "HAS_NER_GLOBAL_MAX_INFLIGHT", 3, raising=False)
    assert asyncio.run(_measure_peak(6)) == 3


def test_validator_clamps():
    settings_cls = type(settings)
    assert settings_cls(HAS_NER_GLOBAL_MAX_INFLIGHT=99).HAS_NER_GLOBAL_MAX_INFLIGHT == 12
    assert settings_cls(HAS_NER_GLOBAL_MAX_INFLIGHT=0).HAS_NER_GLOBAL_MAX_INFLIGHT == 1


def test_default_inflight_is_eight():
    """Issue#103：默认闸门 4 → 8（同一条 DCU 实测并发曲线；validator 上限即 8）。

    历史：Issue#33 PR-A 把默认从 1 放开到 4。Issue#103 按实测曲线继续放开——
    短文本到 k=16 仍近似线性（13~14×），长 prompt 甜点 k≈8（k=16 起转差）。
    """
    settings_cls = type(settings)
    assert settings_cls.model_fields["HAS_NER_GLOBAL_MAX_INFLIGHT"].default == 8


def test_verify_knobs_defaults_and_clamps():
    """Issue#33 PR-B：验证段并发上限独立于闸门（旁路下仍有界）；批量自证默认关。

    Issue#103：VERIFY_PARALLEL_CAP 默认 4 → 8——生产台账 9845 次带槽位调用里
    9743 次（99%）是 residual verify，扫描件逐页耗时几乎全由本窗口决定。
    """
    settings_cls = type(settings)
    assert settings_cls.model_fields["HAS_NER_VERIFY_PARALLEL_CAP"].default == 8
    assert settings_cls.model_fields["HAS_NER_VERIFY_BATCH_SIZE"].default == 0
    assert settings_cls(HAS_NER_VERIFY_PARALLEL_CAP=99).HAS_NER_VERIFY_PARALLEL_CAP == 12
    assert settings_cls(HAS_NER_VERIFY_PARALLEL_CAP=0).HAS_NER_VERIFY_PARALLEL_CAP == 1
    assert settings_cls(HAS_NER_VERIFY_BATCH_SIZE=-3).HAS_NER_VERIFY_BATCH_SIZE == 0
    assert settings_cls(HAS_NER_VERIFY_BATCH_SIZE=99).HAS_NER_VERIFY_BATCH_SIZE == 32


def test_issue103_knob_matrix_defaults():
    """Issue#103 闸门矩阵：3 个并发闸门 + 2 个缓存项 + 闸门总开关的默认值。

    这是"改后目标值"的单点断言，防止任何一处被回退而没人发现。
    上限（validator 静默钳制，非报错）：MAX_PARALLEL 8 / GLOBAL 12 /
    VERIFY_CAP 12 / CACHE_TTL 3600 / CACHE_MAX_ITEMS 4096。
    """
    settings_cls = type(settings)
    fields = settings_cls.model_fields

    assert fields["HAS_NER_MAX_PARALLEL_REQUESTS"].default == 8
    assert fields["HAS_NER_GLOBAL_MAX_INFLIGHT"].default == 8
    assert fields["HAS_NER_VERIFY_PARALLEL_CAP"].default == 8
    assert fields["HAS_NER_CACHE_TTL_SEC"].default == 1800.0
    assert fields["HAS_NER_CACHE_MAX_ITEMS"].default == 2048
    # 闸门被旁路时上述并发全部失效（验收必须显式断言其为 True）
    assert fields["SERIALIZE_SHARED_GPU_MODELS"].default is True


def test_issue103_knob_clamps_are_silent():
    """超上限是**静默钳制**而非报错——验收要按钳制后的值断言，不是按 env 写的值。"""
    settings_cls = type(settings)
    assert settings_cls(HAS_NER_MAX_PARALLEL_REQUESTS=99).HAS_NER_MAX_PARALLEL_REQUESTS == 8
    assert settings_cls(HAS_NER_MAX_PARALLEL_REQUESTS=-1).HAS_NER_MAX_PARALLEL_REQUESTS == 1
    assert settings_cls(HAS_NER_GLOBAL_MAX_INFLIGHT=99).HAS_NER_GLOBAL_MAX_INFLIGHT == 12
    assert settings_cls(HAS_NER_VERIFY_PARALLEL_CAP=99).HAS_NER_VERIFY_PARALLEL_CAP == 12
