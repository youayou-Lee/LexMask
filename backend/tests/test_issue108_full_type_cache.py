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

⑥ 评审（C1/C2/I3/I4）后追加的用例把「失败」的定义从「抛异常」改成
**「没有解析成功」**：`ner()` 把 HTTP 错/超时/不可解析全吞成 `{}`，`isinstance`
分支永不进入 ⇒ 守卫恒不触发（C1）；三批全吐垃圾时服务层照样把 `{}` 写进全类型键，
读侧 `cached is not None` 判命中 ⇒ 整页 0 实体固化（C2）。判据由
`has_client.ner_checked` 显式给出；I3 则用真实读侧函数补跨链集成与边界用例。
"""

import asyncio
import json
import threading

import pytest

from app.services.has_client import HaSClient
from app.services.has_service import HaSService
from app.services.ocr_has_vision_service import OCRTextBlock
from app.services.vision.has_text_payload import (
    _build_has_text_payload,
    _build_has_text_type_names,
)
from app.services.vision.ocr_cache import _get_cached_has_text_ner

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

    def _maybe_fail(self) -> None:
        with self._lock:
            self.calls += 1
            should_fail = self.calls == self._fail_on
        if should_fail:
            raise RuntimeError("simulated sub-batch failure")

    def ner(self, *args, **kwargs):
        self._maybe_fail()
        return self._inner.ner(*args, **kwargs)

    def ner_checked(self, *args, **kwargs):
        self._maybe_fail()
        return self._inner.ner_checked(*args, **kwargs)


# ---------------------------------------------------------------------------
# ⑥ 评审修复（C1/C2/I3/I4）：判据从「没抛异常」改成「显式解析成功」
# ---------------------------------------------------------------------------


def _real_client_with_echo_model():
    """真实 HaSClient + **按请求类型回话**的假模型。

    `_real_client_with_fake_model` 无论问什么都回 19 型，掩盖了「批间不全」这一
    生产形状（C1 的部分并集）；这里从 prompt 里取回 `Specified types:` 再回话。
    """
    client = HaSClient()
    prompts: list[str] = []

    def fake_call_model(messages, *, max_tokens=None, temperature=None):
        prompt = str(messages[0]["content"])
        prompts.append(prompt)
        marker = "Specified types:"
        start = prompt.index(marker) + len(marker)
        requested = json.loads(prompt[start:prompt.index("\n", start)])
        return json.dumps({t: [f"{t}-值"] for t in requested}, ensure_ascii=False)

    client._call_model = fake_call_model
    client._ner_cache_ttl_sec = 1800.0
    client._ner_cache_max_items = 2048
    return client, prompts


def test_ner_checked_reports_success_for_a_normal_response():
    """`ner_checked` 是服务层写入门唯一可用信号（C1/C2 的同一修法）。"""
    client, _ = _real_client_with_echo_model()

    result, ok = client.ner_checked("文本", ["类型0"])

    assert ok is True
    assert result.get("类型0") == ["类型0-值"]


def test_ner_checked_reports_success_for_a_clean_empty_object():
    """合法空答案（提示词明确「无匹配就回 {}」）算**解析成功**。"""
    client, _ = _real_client_with_fake_model()
    client._call_model = lambda messages, **kwargs: "{}"

    result, ok = client.ner_checked("文本", ["类型0"])

    assert (result, ok) == ({}, True)


def test_ner_checked_reports_parse_failure_for_unparseable_output():
    """非 JSON 垃圾必须 ok=False——不能与「合法空答案」混为一谈（C2 根因）。"""
    client, _ = _real_client_with_fake_model()
    client._call_model = lambda messages, **kwargs: "抱歉，我无法从这段文字里识别出实体。"

    result, ok = client.ner_checked("文本", ["类型0"])

    assert result == {}
    assert ok is False, "不可解析的输出不是「识别结果为空」，而是「这次没结论」"


def test_ner_checked_reports_failure_when_the_model_call_raises():
    """生产失败模式：HTTP 错/超时被 `ner()` 吞成 `{}`，标志位必须把它暴露出来（C1）。"""
    client, _ = _real_client_with_fake_model()

    def boom(messages, **kwargs):
        raise TimeoutError("read timeout")

    client._call_model = boom

    result, ok = client.ner_checked("文本", ["类型0"])

    assert (result, ok) == ({}, False)


def _truncated_then(second_response: str):
    """第一次回**截断** JSON（走 json_repair 修复 + 补查），第二次回指定内容。

    本机 `json_repair` 不可用（`tests/test_has_client_json_parse.py` 的修复用例本就是
    既有环境失败），所以这里直接钉住「修复成功但缺桶」这一支的 `parse_mode`，而不是
    依赖可选依赖；补查那次仍走真实解析。
    """
    client, _ = _real_client_with_fake_model()
    calls = {"n": 0}
    real_parse = HaSClient._try_parse_json_object

    def fake_call_model(messages, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return '{"类型0": ["a"]'  # 截断：缺右括号、且整桶缺 类型1
        return second_response

    def fake_parse(raw: str):
        if calls["n"] == 1:
            return "json_repair:direct", {"类型0": ["a"]}
        return real_parse(raw)

    client._call_model = fake_call_model
    client._try_parse_json_object = staticmethod(fake_parse)
    return client, calls


def test_ner_checked_reports_success_when_truncation_retry_recovers_missing_types():
    """截断修复 + 补查成功 ⇒ ok=True，合并结果可写回（正常路径不得被新判据误伤）。"""
    client, calls = _truncated_then('{"类型1": ["b"]}')

    result, ok = client.ner_checked("文本", ["类型0", "类型1"])

    assert calls["n"] == 2, "缺桶应触发一次补查"
    assert (result, ok) == ({"类型0": ["a"], "类型1": ["b"]}, True)
    assert client.get_cached_ner("文本", ["类型0", "类型1"]) == result


def test_truncated_partial_union_is_not_cached_when_retry_fails():
    """行为红（旧树必红）：补查失败时，修复出的**残缺并集**不得落进全类型键。

    走公开 `ner()`（两棵树都有）⇒ 未改动树上这条断言失败：旧实现在这一支照样
    `_set_cached_ner` + 返回，后续读侧把它当整页结论命中（C1 的同型污染）。
    """
    client, calls = _truncated_then("抱歉，我无法识别。")

    result = client.ner("文本", ["类型0", "类型1"])

    assert calls["n"] == 2, "缺桶应触发一次补查"
    assert result == {"类型0": ["a"]}, "残缺结果仍按尽力而为返回给调用方"
    assert client.get_cached_ner("文本", ["类型0", "类型1"]) is None, (
        "残缺并集不得落进全类型键"
    )


def test_ner_checked_reports_failure_when_truncation_retry_is_not_confirmed():
    """同上路径的显式标志：补查没拿到结论 ⇒ ok=False，服务层据此拒绝写全类型键。"""
    client, calls = _truncated_then("抱歉，我无法识别。")

    result, ok = client.ner_checked("文本", ["类型0", "类型1"])

    assert calls["n"] == 2, "缺桶应触发一次补查"
    assert (result, ok) == ({"类型0": ["a"]}, False)


def test_full_type_key_not_written_when_a_batch_call_raises():
    """C1：被吞掉的批异常不得让部分并集进全类型键（否则缺桶结果固化 TTL = 静默漏检）。"""
    service = HaSService(base_url="http://stub:0")
    client, _ = _real_client_with_echo_model()
    inner_call_model = client._call_model
    calls = {"n": 0}

    def flaky_call_model(messages, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("connection reset by peer")
        return inner_call_model(messages, **kwargs)

    client._call_model = flaky_call_model
    service.client = client

    asyncio.run(service.extract_entities(_TEXT_1577, _types(19)))

    assert calls["n"] == 3, f"三个子批都应被派发，实际 {calls['n']}"
    assert client.get_cached_ner(_TEXT_1577, _full_chinese()) is None, (
        "有子批被吞成 {} 时不得写回全类型键"
    )


def test_full_type_key_not_written_when_every_response_is_unparseable():
    """C2：三批全不可解析时，改前会把 `{}` 写进全类型键并被读侧判「命中」⇒ 整页 0 实体固化。"""
    service = HaSService(base_url="http://stub:0")
    client, prompts = _real_client_with_fake_model()

    def garbage_call_model(messages, *, max_tokens=None, temperature=None):
        prompts.append("garbage")
        return "这不是 JSON。"

    client._call_model = garbage_call_model
    service.client = client

    asyncio.run(service.extract_entities(_TEXT_1577, _types(19)))

    assert len(prompts) == 3, f"三个子批都应被派发，实际 {len(prompts)}"
    assert client.get_cached_ner(_TEXT_1577, _full_chinese()) is None, (
        "解析失败不得写回全类型键（读侧 `cached is not None` 会把它当成命中）"
    )


def test_full_type_key_still_written_when_the_page_genuinely_has_no_entities():
    """正向对照：判据是「解析成功」而非「结果非空」。

    用 `if ner_result:` 兜底会误杀这一类合法空答案（同页每次都得重推一遍），
    所以本用例与上面两条共同钉住判据边界。
    """
    service = HaSService(base_url="http://stub:0")
    client, _ = _real_client_with_fake_model()
    client._call_model = lambda messages, **kwargs: "{}"
    service.client = client

    asyncio.run(service.extract_entities(_TEXT_1577, _types(19)))

    assert client.get_cached_ner(_TEXT_1577, _full_chinese()) == {}


def test_ner_and_set_cached_ner_write_the_very_same_key():
    """I4(c)：`ner()` 自写的键必须 == guidance 空时 `set_cached_ner` 的键（#103 真回归点）。

    旧用例用同一实参 set→get，两端同一入口，测不出漂移；这里比对的是
    `ner()` 内部真实落下的键对象。
    """
    client, _ = _real_client_with_fake_model()
    text, types = "键一致性用文本", ["类型0", "类型1"]

    client.ner(text, types)

    assert len(client._ner_cache) == 1, "ner() 应只落下一条条目"
    ner_key = next(iter(client._ner_cache))
    assert ner_key == client._ner_cache_key_for(text, client._normalize_ner_types(types)), (
        "ner() 写的键与 set_cached_ner 的键公式漂移了"
    )

    client.set_cached_ner(text, types, {"类型0": ["覆盖值"]})

    assert set(client._ner_cache) == {ner_key}, "写回必须落在同一条键上（不得另开一条）"
    assert client.get_cached_ner(text, types) == {"类型0": ["覆盖值"]}


def _text_blocks(*texts: str) -> list[OCRTextBlock]:
    return [
        OCRTextBlock(
            text=text,
            polygon=[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]],
            confidence=0.99,
        )
        for text in texts
    ]


def test_visual_read_side_reads_back_the_full_type_entry():
    """I3：跨链集成——读侧的文本与标签都由**真实读侧函数**产出，而不是测试自己编。

    写侧（`extract_entities` → 全类型写回）与读侧（`_build_has_text_payload` →
    `_get_cached_has_text_ner`，即视觉线 `has_text_analysis` 的探测口）在
    「同一段文本 + 同一套标签」下必须复用同一条缓存。
    """
    types = _types(3)
    payload = _build_has_text_payload(_text_blocks("张三", "13800000000"), max_chars=2048)
    read_side_types = _build_has_text_type_names(types)

    service = HaSService(base_url="http://stub:0")
    client, prompts = _real_client_with_echo_model()
    service.client = client

    asyncio.run(service.extract_entities(payload.content, types))

    assert prompts, "写侧应至少派发一次"
    cached = _get_cached_has_text_ner(client, payload.content, read_side_types)

    assert cached is not None, (
        "同文本同标签时读侧必须读中写侧的全类型条目（键公式跨模块一致）"
    )
    assert cached.get("类型0") == ["类型0-值"]


def test_cross_chain_reuse_requires_byte_identical_text():
    """I3 边界（如实钉住）：收益只成立于「两侧文本逐字相同」，不是「同一条链路」。

    写侧混合链吃的是**文档级语义行切块**（`_build_has_candidate_chunks`：行选择 +
    `\\s+`→单空格归一 + 去重 + 按 `MAX_HAS_TEXT_CHARS` 切块 + `MAX_HAS_CHUNKS` 封顶），
    读侧视觉线吃的是**整页 OCR 块拼接**（`_build_has_text_payload`）。两侧归一规则
    不同，文本摘要就不同；更何况扫描页上混合链根本早退（`is_scanned`），
    压根不会写下这条全类型条目。
    """
    types = _types(3)
    page_payload = _build_has_text_payload(_text_blocks("13800000000", "张三"), max_chars=2048)
    read_side_types = _build_has_text_type_names(types)

    service = HaSService(base_url="http://stub:0")
    client, _ = _real_client_with_echo_model()
    service.client = client

    # 同一页内容，但写侧拿到的是另一种行序（模拟两侧归一/行选择不同）
    asyncio.run(service.extract_entities("张三\n13800000000", types))

    assert client.get_cached_ner("张三\n13800000000", [t.name for t in types]) is not None
    assert _get_cached_has_text_ner(client, page_payload.content, read_side_types) is None, (
        "文本不同 ⇒ 读侧读不中；跨链复用不是本改动的保证"
    )
