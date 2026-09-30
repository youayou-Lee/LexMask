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


def test_default_inflight_is_four():
    """Issue#33 PR-A：默认闸门从 1 放开到 4（多并发是闸门注释明示的预期用法）。"""
    settings_cls = type(settings)
    assert settings_cls.model_fields["HAS_NER_GLOBAL_MAX_INFLIGHT"].default == 4


def test_verify_knobs_defaults_and_clamps():
    """Issue#33 PR-B：验证段并发上限独立于闸门（旁路下仍有界）；批量自证默认关。"""
    settings_cls = type(settings)
    assert settings_cls.model_fields["HAS_NER_VERIFY_PARALLEL_CAP"].default == 4
    assert settings_cls.model_fields["HAS_NER_VERIFY_BATCH_SIZE"].default == 0
    assert settings_cls(HAS_NER_VERIFY_PARALLEL_CAP=99).HAS_NER_VERIFY_PARALLEL_CAP == 12
    assert settings_cls(HAS_NER_VERIFY_PARALLEL_CAP=0).HAS_NER_VERIFY_PARALLEL_CAP == 1
    assert settings_cls(HAS_NER_VERIFY_BATCH_SIZE=-3).HAS_NER_VERIFY_BATCH_SIZE == 0
    assert settings_cls(HAS_NER_VERIFY_BATCH_SIZE=99).HAS_NER_VERIFY_BATCH_SIZE == 32


def test_incremental_filter_flag_default_off():
    """Issue#37 WS-1：增量识别默认关——关闭时行为必须与现状逐字节等价。"""
    settings_cls = type(settings)
    assert settings_cls.model_fields["HAS_VISION_INCREMENTAL_KNOWN_FILTER"].default is False
