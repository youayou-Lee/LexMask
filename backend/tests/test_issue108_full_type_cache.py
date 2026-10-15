"""Issue#108：预切分批后按**完整类型表**写回 HaS NER 缓存（红先测试）。

背景：Issue#103 把「帽容量分批」从 `has_client.ner`（内部同步 `for`）上移到
`has_service._preplan_completion_batches` + `asyncio.gather`。副作用是 `ner()`
此后只会被以**子批**类型表调用，于是只写子批缓存键、不再写全类型键；而
`has_client.get_cached_ner`（视觉线 `has_text_analysis` 的探测口）是按**完整
类型表**查的 ⇒ 同一段文字混合链算过一遍后，视觉线仍然读不中、再推一遍。

本文件钉三件事：
1. 客户端写回口 `set_cached_ner` 与读口 `get_cached_ner` 的键**必须一致**
   （键公式两处漂移的表现是「写进去了却永远读不中」，不报错、不告警）；
2. 服务层预切成 3 个子批后，**完整 19 型**的键必须可读（未改动树为 red：None）；
3. 只要有子批失败，**不得**写回全类型键——`gather(return_exceptions=True)`
   吞掉的失败批若被合并后写进缓存，该键在 TTL 内会一直返回偏少结果 = 永久漏检。
"""

import asyncio
import json
import threading

import pytest

from app.services.has_client import HaSClient
from app.services.has_service import HaSService

# 19 型 × 160 token/型 + 1577 字 ⇒ capacity=(2048-788)//160=7 ⇒ 7+7+5 三个子批
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


def _full_chinese(count: int = 19) -> list[str]:
    return [f"类型{i}" for i in range(count)]


@pytest.fixture(autouse=True)
def _clean_shared_cache():
    """HaS 缓存是**类级共享**的（`_SHARED_NER_CACHE`），用例之间必须隔离。"""
    HaSClient._SHARED_NER_CACHE.clear()
    yield
    HaSClient._SHARED_NER_CACHE.clear()


def _real_client_with_fake_model():
    """真实 HaSClient，只把唯一网络出口 `_call_model` 换成返回固定 JSON。"""
    client = HaSClient()
    prompts: list[str] = []

    def fake_call_model(messages, *, max_tokens=None, temperature=None):
        prompts.append(json.dumps(messages, ensure_ascii=False))
        return json.dumps({t: [f"{t}-值"] for t in _full_chinese()}, ensure_ascii=False)

    client._call_model = fake_call_model
    # 显式钉住缓存开关，避免外部环境把 TTL/容量设成 0 时用例假失败
    client._ner_cache_ttl_sec = 1800.0
    client._ner_cache_max_items = 2048
    return client, prompts


def test_set_and_get_cached_ner_share_one_key_formula():
    """写回后必须能按同一 (text, 类型表) 读中——两处键公式不得漂移。"""
    client, _ = _real_client_with_fake_model()
    types = ["姓名", "公司名称"]

    client.set_cached_ner("缓存键一致性用文本", types, {"姓名": ["张三"]})

    assert client.get_cached_ner("缓存键一致性用文本", types) == {"姓名": ["张三"]}
    # 类型表不同 ⇒ 键不同（不得串味）
    assert client.get_cached_ner("缓存键一致性用文本", ["姓名"]) is None


def test_guided_write_stays_separate_from_unguided_probe():
    """guidance 是键的一部分：带 guidance 的写回不得被无 guidance 的探测读中。

    读侧 `get_cached_ner(text, entity_types)` 没有 guidance 参数、键里摘要恒为空串，
    所以服务层的写回也必须走**无 guidance** 的键（见 has_service 写回处注释）。
    """
    client, _ = _real_client_with_fake_model()
    guidance = [{"type": "姓名", "description": "人物姓名"}]

    client.set_cached_ner("带引导文本", ["姓名"], {"姓名": ["李四"]}, guidance)

    assert client.get_cached_ner("带引导文本", ["姓名"]) is None
    # 而同一份结果按读侧的键（无 guidance）写回后必须读得中
    client.set_cached_ner("带引导文本", ["姓名"], {"姓名": ["李四"]})
    assert client.get_cached_ner("带引导文本", ["姓名"]) == {"姓名": ["李四"]}


def test_full_type_key_is_written_after_preplanned_split():
    """核心红先断言：预切 3 子批合并成功后，完整 19 型的键必须可读。

    未改动树：`ner()` 只写了 7/7/5 三个子批键 ⇒ 本断言得 None（red）。
    """
    service = HaSService(base_url="http://stub:0")
    client, prompts = _real_client_with_fake_model()
    service.client = client

    asyncio.run(service.extract_entities(_TEXT_1577, _types(19)))

    assert len(prompts) == 3, f"19 型 / 1577 字应派发 3 个子批，实际 {len(prompts)}"
    cached = client.get_cached_ner(_TEXT_1577, _full_chinese())
    assert cached is not None, "合并成功后必须按完整类型表写回缓存（Issue#108）"
    assert cached.get("类型0") == ["类型0-值"]


def test_full_type_key_not_written_when_any_sub_batch_fails():
    """⚠️ 失败批不得合并进缓存：那会把「一次失败」放大成 TTL 内的永久漏检。"""
    service = HaSService(base_url="http://stub:0")
    client, _ = _real_client_with_fake_model()
    flaky = _FlakyOnNthNer(client, fail_on=2)
    service.client = flaky

    asyncio.run(service.extract_entities(_TEXT_1577, _types(19)))

    assert flaky.calls == 3, f"三个子批都应被派发，实际 {flaky.calls}"
    assert client.get_cached_ner(_TEXT_1577, _full_chinese()) is None, (
        "有子批失败时不得写回全类型键"
    )


class _FlakyOnNthNer:
    """第 N 次 `ner()` 抛错；其余属性透传（`plan_completion_batches`/`set_cached_ner`）。"""

    def __init__(self, inner: HaSClient, fail_on: int = 2):
        self._inner = inner
        self._fail_on = fail_on
        self._lock = threading.Lock()
        self.calls = 0

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def ner(self, *args, **kwargs):
        with self._lock:
            self.calls += 1
            should_fail = self.calls == self._fail_on
        if should_fail:
            raise RuntimeError("simulated sub-batch failure")
        return self._inner.ner(*args, **kwargs)