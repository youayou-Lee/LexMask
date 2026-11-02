"""Issue#104 —— 本地门③补缺判据：分组派发的**页级接线 / 组级失败 / 并发有界 / 部署链**。

验收方案 v5（私有仓 docs/acceptance/2026-10-15-issue104-payload-context验收方案.md）
的本地判据落点。与 tests/test_issue104_payload_groups.py 的分工：

- 那一份管**纯函数与配置**（分组规划语义 D1①-⑦⑨⑩、估算器锚点 D2b、探测回落 D-fb、
  NER_ERRORS 计数 C5、单组 payload 等价 C6）；
- 本份管**接线的可失败性**——纯函数对但接线错（尾组结果丢、组级失败被洗成成功页、
  外层闸位嵌套死锁、部署链上下文高于服务端）都必须在此被抓红：

- D1⑦b 硬切边界纪律：切点只落在非字母数字边界、植入真值不被拦腰切断
        （补 test_issue104_payload_groups.py::test_d1_7 里那条恒真断言）
- D1⑧    多组页的尾组实体回映：两组各植一个真值 ⇒ 页级合并后每个值都能按
        ``_block_search_text`` 回映到**它自己那一块**；组间 inflight 键不碰撞
- D4      小页零回归：单组、主查实发文本 == 改前 payload、实体不降
- D5      并发安全：GPU 闸位=1 + 多组不嵌套死锁（证明主查外层闸位已删）；
        扇出 = O(组并发窗口)，SERIALIZE_SHARED_GPU_MODELS True/False 都有界
- D-cm    组级失败：任一组失败 ⇒ 页判失败（返回 [] 非半结果）+ 计数落 stage_status；
        重复调用仍判失败（成功组缓存不得把半结果洗成「成功页」）
- D3      部署链静态一致性：各链 max-model-len 真值 + 探测回落值不得高于任一链
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
import time
from pathlib import Path

import httpx
import pytest

from app.core.config import settings
from app.services.has_client import _NER_CONTEXT_RESERVE_TOKENS, HaSClient
from app.services.ocr_has_vision_service import OCRTextBlock
from app.services.vision.has_text_analysis import (
    _run_has_text_ner_groups,
    plan_has_text_group_texts,
    run_has_text_analysis,
)
from app.services.vision.has_text_payload import (
    _build_has_text_payload,
    plan_has_text_groups,
)
from app.services.vision.ocr_cache import _has_text_ner_inflight_key
from app.services.vision.ocr_table_semantics import _block_search_text

_TYPES = ["身份证号", "电话"]
_TYPES_12 = [f"类型{i:02d}" for i in range(12)]

_MARK_HEAD = "370702199001014521"  # 头组真值
_MARK_TAIL = "370702198802024567"  # 尾组真值


# ---------------------------------------------------------------------------
# 公共桩
# ---------------------------------------------------------------------------

def _dense_digits(n: int) -> str:
    # 数字/标点密集（编号、账号、日期串形态）——历史 400 的触发形态。
    return ("370702199001014521," * (n // 19 + 2))[:n]


def _filler_digits(n: int) -> str:
    """与 _MARK_* 不同的密集数字串（否则「植入真值」在填充里到处都是）。"""
    return ("991122334455667788," * (n // 19 + 2))[:n]


def _unique_dense(n: int, seed: str) -> str:
    """去重可辨的密集数字块（编号+18 位数字串），避免 payload 的重复块去重。

    填充值**必须**异于 ``_MARK_*``：否则「植入真值」在填充里随处可见，
    回映断言就会变成恒真。
    """
    unit = f"{seed}、" + ("991122334455667788," * 8)
    return (unit * (n // len(unit) + 1))[:n]


def _estimate_for(types: list[str]):
    def _estimate(group_text: str) -> int:
        return HaSClient._estimate_prompt_tokens(HaSClient._build_ner_prompt(types, "", group_text))

    return _estimate


def _max_tokens_for(types: list[str], context_tokens: int):
    def _max_tokens(group_text: str) -> int:
        return HaSClient.planned_max_tokens(types, group_text, context_tokens=context_tokens)

    return _max_tokens


def _prompt_text(prompt: str) -> str:
    return prompt[prompt.rindex("<text>") + len("<text>") : prompt.rindex("</text>")]


def _prompt_types(prompt: str) -> list[str]:
    line = next(line for line in prompt.splitlines() if line.startswith("Specified types:"))
    return json.loads(line.split("Specified types:", 1)[1])


def _http_400() -> httpx.HTTPStatusError:
    resp = httpx.Response(400, request=httpx.Request("POST", "http://test/v1/chat/completions"))
    return httpx.HTTPStatusError("400", request=resp.request, response=resp)


class _ModelFake(HaSClient):
    """只替换 ``_call_model``：真 ner()（预算/解析/缓存/计数）全部照跑。

    ``reply(text, types)`` 返回 dict（实体桶）或原始字符串；抛异常即模拟模型侧失败。
    """

    def __init__(self, *, context_tokens: int = 8192, reply=None, delay: float = 0.0):
        super().__init__()
        self._ctx = context_tokens
        self.reply = reply or (lambda text, types: {})
        self.delay = delay
        self.calls: list[dict] = []
        self._lock = threading.Lock()
        self.concurrent = 0
        self.peak_concurrent = 0

    def _effective_context_tokens(self) -> int:  # 探测不落网（鸭子类型接缝）
        return self._ctx

    def _call_model(self, messages, *, max_tokens=None, temperature=None):
        prompt = messages[0]["content"]
        text = _prompt_text(prompt)
        types = _prompt_types(prompt)
        with self._lock:
            self.calls.append(
                {"text": text, "types": list(types), "max_tokens": max_tokens, "temperature": temperature}
            )
            self.concurrent += 1
            self.peak_concurrent = max(self.peak_concurrent, self.concurrent)
        try:
            if self.delay:
                time.sleep(self.delay)
            result = self.reply(text, list(types))
        finally:
            with self._lock:
                self.concurrent -= 1
        return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)

    def calls_with(self, marker: str) -> int:
        return sum(1 for call in self.calls if marker in call["text"])

    def exact_calls(self, text: str) -> int:
        return sum(1 for call in self.calls if call["text"] == text)


class _VT:
    """最小 vision_types 桩（id / name / query_labels 三字段即够）。"""

    def __init__(self, id_: str, name: str, labels=()):
        self.id = id_
        self.name = name
        self.query_labels = list(labels)


@pytest.fixture(autouse=True)
def _clean_shared_caches():
    """HaSClient 的 NER 缓存是**类级共享**的：跨用例污染会让页级断言失真。"""
    HaSClient._context_probe_cache.clear()
    HaSClient._SHARED_NER_CACHE.clear()
    yield
    HaSClient._context_probe_cache.clear()
    HaSClient._SHARED_NER_CACHE.clear()


def _blocks(*texts: str) -> list[OCRTextBlock]:
    return [
        OCRTextBlock(
            text=text,
            polygon=[[0.0, 0.0], [100.0, 0.0], [100.0, 10.0], [0.0, 10.0]],
            confidence=1.0,
        )
        for text in texts
    ]


def _planned_groups(page_chars: int, *, context_tokens: int, seed: str = "") -> list[str]:
    """用真规划器把一页密文分成真实组（D5 的组数必须来自规划器，不是手搓）。

    页由**逐行唯一**的密集数字行组成：重复文本会命中组间 inflight/缓存去重，
    让「调用数 == 组数」这条断言假红（那是去重的正确行为，不是扇出失控）。
    """
    lines: list[str] = []
    total = 0
    index = 0
    while total < page_chars:
        index += 1
        line = f"{seed or '页'}第{index:05d}号、" + _filler_digits(140)
        lines.append(line)
        total += len(line) + 1
    payload = _build_has_text_payload(
        _blocks(*lines),
        max_chars=total + 1000,
        max_block_chars=total + 1000,
    )
    return plan_has_text_group_texts(payload.texts, _TYPES, context_tokens=context_tokens)


# ---------------------------------------------------------------------------
# D1⑦b 硬切边界纪律
# ---------------------------------------------------------------------------

def test_d1_7b_hard_split_only_cuts_on_non_alnum_boundaries():
    """装填上限 200 字符（estimate=len、completion=0）下切 60 个 18 位数字串：
    每个切点都必须贴着逗号——切在数字串内部即真值被拦腰切断（D1⑦ 的**可失败**版）。"""
    joined = ",".join(f"{i:018d}" for i in range(60))
    groups = plan_has_text_groups(
        [joined],
        context_tokens=264,  # fill = 264 − 0 − 64 = 200
        reserve_tokens=0,
        estimate_for=len,
        max_tokens_for=lambda text: 0,
    )
    pieces = [piece for group in groups for piece in group]
    assert len(pieces) > 1, "构造未触发硬切，判据失效"
    assert "".join(pieces) == joined  # D1⑤ 不丢字符
    offset = 0
    for piece in pieces[:-1]:
        offset += len(piece)
        assert joined[offset - 1] == "," or joined[offset] == ",", (
            f"切点落在数字串内部：{joined[offset - 25:offset + 25]!r}"
        )


def test_d1_7c_planted_value_survives_real_arithmetic_split():
    """真算术（8192 链）+ 18,020 字符密块：植入的身份证号必须完整落在**恰好一片**里。"""
    id_card = "370702199001014521"
    block = _filler_digits(12000) + "," + id_card + "," + _filler_digits(16000)
    groups = plan_has_text_groups(
        [block],
        context_tokens=8192,
        reserve_tokens=160,
        estimate_for=_estimate_for(_TYPES_12),
        max_tokens_for=_max_tokens_for(_TYPES_12, 8192),
    )
    pieces = [piece for group in groups for piece in group]
    assert len(pieces) > 1, "构造未触发硬切，判据失效"
    assert "".join(pieces) == block
    intact = [piece for piece in pieces if id_card in piece]
    assert len(intact) == 1, f"真值被拦腰切断：{len(intact)} 片含完整身份证"
    offset = 0
    for piece in pieces[:-1]:
        offset += len(piece)
        assert not (block[offset - 1].isdigit() and block[offset].isdigit())


# ---------------------------------------------------------------------------
# D1⑧ 多组页的尾组实体回映
# ---------------------------------------------------------------------------

def _two_group_page() -> tuple[list[str], list[OCRTextBlock], int, int]:
    """构造头/尾真值必然落进**不同组**的页（4096 链）。

    页由 8 个逐块唯一的 800 字符密块组成，第 0 块带 head 真值、第 7 块带 tail 真值。
    返回 (组文本, OCR 块, head 所在组号, tail 所在组号)。
    """
    lines = []
    for i in range(8):
        marker = _MARK_HEAD if i == 0 else (_MARK_TAIL if i == 7 else "")
        line = _unique_dense(800, f"块{i}")
        lines.append(line + ("，" + marker if marker else ""))
    group_texts = plan_has_text_group_texts(lines, _TYPES, context_tokens=4096)
    head_group = [i for i, text in enumerate(group_texts) if _MARK_HEAD in text]
    tail_group = [i for i, text in enumerate(group_texts) if _MARK_TAIL in text]
    assert len(group_texts) >= 2, f"构造未分组（{len(group_texts)}），判据失效"
    assert len(head_group) == 1 and len(tail_group) == 1, (head_group, tail_group)
    assert head_group != tail_group, f"头尾真值落到同一组 {head_group}，判据失效"
    assert head_group[0] < tail_group[0], "真值顺序与组序不一致"
    return group_texts, _blocks(*lines), head_group[0], tail_group[0]


def test_d1_8_each_group_value_maps_back_to_its_own_block():
    group_texts, blocks, head_group, tail_group = _two_group_page()

    def reply(text, types):
        values = [m for m in (_MARK_HEAD, _MARK_TAIL) if m in text]
        return {"身份证号": values} if values and "身份证号" in types else {}

    client = _ModelFake(context_tokens=4096, reply=reply)
    stage: dict = {}
    merged, failed = asyncio.run(
        asyncio.wait_for(_run_has_text_ner_groups(client, stage, group_texts, _TYPES), timeout=30)
    )

    assert failed == 0
    assert set(merged["身份证号"]) == {_MARK_HEAD, _MARK_TAIL}

    # 值 → 块 回映（与生产同源：_block_search_text 是匹配器唯一认的文本）
    for value, expected_block, expected_group in (
        (_MARK_HEAD, 0, head_group),
        (_MARK_TAIL, len(blocks) - 1, tail_group),
    ):
        hits = [i for i, block in enumerate(blocks) if value in _block_search_text(block)]
        assert hits == [expected_block], f"{value} 回映到 {hits}，应为 [{expected_block}]"
        group_index = [i for i, text in enumerate(group_texts) if value in text]
        assert group_index == [expected_group], f"{value} 落在组 {group_index}，应为 [{expected_group}]"

    # 组间 inflight 键不得碰撞（碰撞会让兄弟组的真值互相顶掉）
    keys = {_has_text_ner_inflight_key(client, text, _TYPES) for text in group_texts}
    assert len(keys) == len(group_texts)


def test_d1_8_single_group_key_is_byte_identical_to_the_page_key():
    """单组时组文本 == 整页 payload ⇒ inflight 键也必须逐字等价（C6 的键版）。"""
    block = "张三，身份证 370702199001014521，电话 13800138000"
    payload = _build_has_text_payload(_blocks(block), max_chars=32000, max_block_chars=8000)
    group_texts = plan_has_text_group_texts(payload.texts, _TYPES, context_tokens=8192)
    assert len(group_texts) == 1
    client = HaSClient()
    assert _has_text_ner_inflight_key(client, group_texts[0], _TYPES) == _has_text_ner_inflight_key(
        client, payload.content, _TYPES
    )


# ---------------------------------------------------------------------------
# D4 小页零回归
# ---------------------------------------------------------------------------

def test_d4_small_page_single_group_and_identical_model_payload():
    block = f"张三，身份证 {_MARK_HEAD}，电话 13800138000"
    blocks = _blocks(block)
    payload = _build_has_text_payload(
        blocks,
        max_chars=int(settings.HAS_VISION_MAX_TEXT_CHARS),
        max_block_chars=int(settings.HAS_VISION_MAX_BLOCK_CHARS),
    )

    def reply(text, types):
        return {"身份证号": [_MARK_HEAD]} if _MARK_HEAD in text and "身份证号" in types else {}

    client = _ModelFake(context_tokens=8192, reply=reply)
    stage: dict = {}
    entities = asyncio.run(run_has_text_analysis(blocks, client, [_VT("ID_CARD", "身份证号")], stage))

    assert stage["has_text_payload_group_count"] == 1
    # 改前口径：主查实发文本就是整页 payload、恰一趟（HAS_NER_SELF_CONSIST_SAMPLES=1）
    assert client.exact_calls(payload.content) == int(settings.HAS_NER_SELF_CONSIST_SAMPLES)
    assert any(e["text"] == _MARK_HEAD and e["type"] == "ID_CARD" for e in entities)
    assert stage["has_text_entity_count"] >= 1


# ---------------------------------------------------------------------------
# D5 并发安全
# ---------------------------------------------------------------------------

def _bounded_run(monkeypatch, *, serialize: bool, gate: int, window: int, pages: int, page_chars: int):
    monkeypatch.setattr(settings, "SERIALIZE_SHARED_GPU_MODELS", serialize)
    monkeypatch.setattr(settings, "HAS_NER_GLOBAL_MAX_INFLIGHT", gate)
    monkeypatch.setattr(settings, "HAS_VISION_MAX_GROUPS_INFLIGHT", window)
    monkeypatch.setattr(settings, "HAS_NER_SELF_CONSIST_SAMPLES", 1)

    pages_groups = [
        _planned_groups(page_chars, context_tokens=4096, seed=f"页{p}") for p in range(pages)
    ]
    client = _ModelFake(context_tokens=4096, reply=lambda text, types: {}, delay=0.02)

    async def _go():
        return await asyncio.wait_for(
            asyncio.gather(
                *(_run_has_text_ner_groups(client, {}, group_texts, _TYPES) for group_texts in pages_groups)
            ),
            timeout=60,
        )

    results = asyncio.run(_go())
    return client, pages_groups, results


def test_d5_gate_one_does_not_nest_with_many_groups(monkeypatch):
    """闸位=1 + 多组：组层逐组串行、无重入自锁；调用数 == 组数（无静默丢组）。

    页级「派发期间不持闸位」由 test_d5_page_level_dispatch_holds_no_gpu_slot 负责
    （本用例走组层入口，抓不到页级嵌套）。
    """
    client, pages_groups, results = _bounded_run(
        monkeypatch, serialize=True, gate=1, window=4, pages=1, page_chars=32000
    )
    assert len(pages_groups[0]) >= 4, f"组数太少判据失效：{len(pages_groups[0])}"
    merged, failed = results[0]
    assert failed == 0
    assert client.peak_concurrent == 1  # 闸位是唯一约束
    assert len(client.calls) == len(pages_groups[0])


def test_d5_group_window_bounds_fanout_when_gate_bypassed(monkeypatch):
    """SERIALIZE=False（闸位被旁路）：扇出仍有界 = 组并发窗口，不是 O(页并发×组数)。"""
    client, pages_groups, results = _bounded_run(
        monkeypatch, serialize=False, gate=1, window=2, pages=1, page_chars=32000
    )
    assert len(pages_groups[0]) >= 4
    assert all(failed == 0 for _merged, failed in results)
    assert client.peak_concurrent == 2


def test_d5_three_pages_gate_one_stays_serial_without_deadlock(monkeypatch):
    client, pages_groups, results = _bounded_run(
        monkeypatch, serialize=True, gate=1, window=4, pages=3, page_chars=16000
    )
    assert all(failed == 0 for _merged, failed in results)
    assert client.peak_concurrent == 1
    total_groups = sum(len(groups) for groups in pages_groups)
    assert len(client.calls) == total_groups


def test_d5_page_level_dispatch_holds_no_gpu_slot(monkeypatch):
    """**页级**主查派发期间不得持有 GPU 闸位（C3 的实质）。

    闸位不可重入：改前页级在 ``shared_gpu_inference_slot`` 内逐组派发，闸位=1 时
    内层再取同一信号量 ⇒ 死锁 ⇒ 本判据必超时。本用例必须走**页级入口**
    ``run_has_text_analysis``；只测 ``_run_has_text_ner_groups`` 的用例抓不到这条。
    """
    monkeypatch.setattr(settings, "SERIALIZE_SHARED_GPU_MODELS", True)
    monkeypatch.setattr(settings, "HAS_NER_GLOBAL_MAX_INFLIGHT", 1)
    monkeypatch.setattr(settings, "HAS_NER_SELF_CONSIST_SAMPLES", 1)

    group_texts, blocks, _head_group, _tail_group = _two_group_page()
    assert len(group_texts) >= 2, f"组数太少判据失效：{len(group_texts)}"

    vision_types = [_VT("ID_CARD", "身份证号")]
    client = _ModelFake(context_tokens=4096, reply=lambda text, types: {}, delay=0.01)
    stage: dict = {}

    asyncio.run(
        asyncio.wait_for(
            run_has_text_analysis(blocks, client, vision_types, stage),
            timeout=30,
        )
    )

    assert stage.get("has_text_ner_failed_groups", 0) == 0
    assert stage.get("has_text_payload_group_count", 0) == len(group_texts)
    assert len(client.calls) == len(group_texts)


# ---------------------------------------------------------------------------
# D-cm 组级失败 × 缓存
# ---------------------------------------------------------------------------

def _cm_reply(text, types):
    if _MARK_TAIL in text:
        raise _http_400()
    values = [_MARK_HEAD] if _MARK_HEAD in text else []
    return {"身份证号": values} if values and "身份证号" in types else {}


def test_c5_split_batch_sub_request_failure_is_not_swallowed(monkeypatch):
    """C5（冷审 I1 连带）：类型分批递归里任一子请求 400 也必须上抛。

    12 型 × 长密文会在 `ner_checked` 内走分批路径；递归调用若不透传
    raise_on_error，子批失败只会变成「该类型没拿到」，组级失败被半结果掩盖
    ——正是本 Issue 在修的病。
    """
    monkeypatch.setattr(settings, "HAS_NER_SELF_CONSIST_SAMPLES", 1)
    # Issue#209：默认完成帽 2048 → 4096 后，1200 密字符/12 型预算 2520 装得下
    # 单批 ⇒ 不再分批，本用例的"子请求"前提消失。显式钉回 2048 保住前提。
    monkeypatch.setattr(settings, "HAS_NER_COMPLETION_HARD_CAP", 2048, raising=False)
    seen = {"n": 0}

    def reply(text, types):
        seen["n"] += 1
        if seen["n"] >= 2:
            raise _http_400()
        return {}

    client = _ModelFake(context_tokens=4096, reply=reply)
    # 1200 密字符 + 12 型在 4096 上下文下正好分成 2 批（plan_completion_batches==2）。
    text = ("370702199001014521," * 70)[:1200]
    with pytest.raises(httpx.HTTPStatusError):
        client.ner(text, _TYPES_12, raise_on_error=True, context_tokens=4096)
    assert seen["n"] >= 2, "用例前提：确实触发了分批（多个子请求）"


def test_d1_10_budget_too_small_fails_the_page_explicitly(monkeypatch):
    """D1⑩ 接线：上下文小到放不下一个字符 ⇒ 页判失败 + 专属状态，不抛通用异常。

    规划器对极小上下文显式 `ValueError`；页级调用点必须把它落成「该页失败」
    与可观测状态（budget_error），而不是让它冒到通用兜底被记成 failed。
    """
    monkeypatch.setattr(settings, "HAS_NER_SELF_CONSIST_SAMPLES", 1)
    _group_texts, blocks, _head, _tail = _two_group_page()
    client = _ModelFake(context_tokens=200, reply=lambda text, types: {})

    # `_resolve_effective_context` 走 client 的 `_effective_context_tokens`；
    # 200 token 减去 reserve 与装填余量后为负 ⇒ 规划器 ValueError。
    monkeypatch.setattr(HaSClient, "_NER_CONTEXT_TOKENS_FLOOR", 1, raising=False)
    stage: dict = {}
    vision_types = [_VT("ID_CARD", "身份证号"), _VT("PHONE", "电话")]
    page = asyncio.run(run_has_text_analysis(blocks, client, vision_types, stage))

    assert page == []
    assert stage["has_text_cache_status"] == "budget_error"
    assert client.calls == [], "预算不足时不得发出任何模型请求"


def test_d_cm_group_failure_fails_the_page_and_survives_repeat(monkeypatch):
    monkeypatch.setattr(settings, "HAS_NER_SELF_CONSIST_SAMPLES", 1)
    group_texts, blocks, _head_group, _tail_group = _two_group_page()
    payload = _build_has_text_payload(
        blocks,
        max_chars=int(settings.HAS_VISION_MAX_TEXT_CHARS),
        max_block_chars=int(settings.HAS_VISION_MAX_BLOCK_CHARS),
    )
    # 尾真值所在的**块文本**（页里唯一含它的那一块）——失败注入按它触发
    tail_block_text = next(block.text for block in blocks if _MARK_TAIL in block.text)
    head_block_text = next(block.text for block in blocks if _MARK_HEAD in block.text)
    assert tail_block_text in payload.content

    client = _ModelFake(context_tokens=4096, reply=_cm_reply)
    vision_types = [_VT("ID_CARD", "身份证号"), _VT("PHONE", "电话")]

    stage1: dict = {}
    page1 = asyncio.run(run_has_text_analysis(blocks, client, vision_types, stage1))
    # ① 页判失败：不给半结果（成功组的值必须被丢弃）
    assert page1 == []
    assert stage1["has_text_ner_failed_groups"] == 1
    assert stage1["has_text_cache_status"] == "group_failed"
    assert page1 != [{"type": "ID_CARD", "text": _MARK_HEAD}]

    types_used = client.calls[0]["types"]
    # ② 成功组的缓存确实落盘，但**整页键不得被写成成功**
    hit_group_text = next(
        text for text in group_texts if _MARK_HEAD in text
    )
    assert client.get_cached_ner(hit_group_text, types_used) is not None
    assert client.get_cached_ner(payload.content, types_used) is None

    calls_before = len(client.calls)
    head_calls_before = client.calls_with(_MARK_HEAD)

    stage2: dict = {}
    page2 = asyncio.run(run_has_text_analysis(blocks, client, vision_types, stage2))
    # ③ 重复调用仍判失败：半结果不得因成功组缓存而变成「成功页」
    assert page2 == []
    assert stage2["has_text_ner_failed_groups"] == 1
    assert stage2["has_text_cache_status"] == "group_failed"
    # 成功组走缓存（不重发），失败组重试后再失败
    assert client.calls_with(_MARK_HEAD) == head_calls_before
    assert len(client.calls) > calls_before
    assert head_block_text in payload.content


def test_d_cm_concurrent_sibling_page_sharing_a_failed_group_also_fails(monkeypatch):
    """并发兄弟页共享同一组文本时，**两边都必须判失败**（冷审 Critical）。

    组文本相同的两个并发请求：一个 owner 跑真调用、另一个走 inflight 等待。
    owner 的组失败后，等待方必须同样计入失败——否则 `gather` 拿到的是
    `None`（非 BaseException）⇒ `failed_groups=0` ⇒ 该页被判成功并按**半
    结果**继续，正是本 Issue 要消灭的「静默少实体」。
    """
    monkeypatch.setattr(settings, "HAS_NER_SELF_CONSIST_SAMPLES", 1)
    _group_texts, blocks, _head_group, _tail_group = _two_group_page()
    # delay 撑开 owner 的在飞窗口，让第二个请求稳稳成为 waiter
    client = _ModelFake(context_tokens=4096, reply=_cm_reply, delay=0.3)
    vision_types = [_VT("ID_CARD", "身份证号"), _VT("PHONE", "电话")]

    async def _both():
        stage_a: dict = {}
        stage_b: dict = {}
        page_a, page_b = await asyncio.gather(
            run_has_text_analysis(blocks, client, vision_types, stage_a),
            run_has_text_analysis(blocks, client, vision_types, stage_b),
        )
        return page_a, page_b, stage_a, stage_b

    page_a, page_b, stage_a, stage_b = asyncio.run(_both())

    # ① 两边都不给半结果（等待方最容易被漏掉）
    assert page_a == []
    assert page_b == []
    # ② 两边都必须把「兄弟组失败」计入失败数
    assert stage_a["has_text_ner_failed_groups"] == 1
    assert stage_b["has_text_ner_failed_groups"] == 1
    # ③ 两边都必须标成组级失败，不得落在「成功」语义上
    assert stage_a["has_text_cache_status"] == "group_failed"
    assert stage_b["has_text_cache_status"] == "group_failed"


# ---------------------------------------------------------------------------
# D3 部署链静态一致性
# ---------------------------------------------------------------------------

_CHAIN_FILES = {
    "docker-compose.yml": 4096,
    "cloud-deploy/start_cloud.sh": 4096,
    "cloud-deploy/vllm_ner_25042.sh": 8192,
    "deploy/dual-5090/has_g0.sh": 8192,
    "deploy/dual-5090/has_g1.sh": 8192,
}


def _max_model_len(text: str) -> int:
    match = re.search(r"--max-model-len[\s\S]{0,60}?(\d{3,6})", text)
    assert match, "未找到 --max-model-len 取值"
    return int(match.group(1))


def test_d3_every_deploy_chain_is_at_or_above_the_probe_fallback():
    """回落值是「探测失败时的唯一预算」⇒ 它必须 ≤ 每一条链的服务端上下文，
    否则探测一失败就回到本 Issue 的 400 起点。加一条 2048 链而不降回落值 ⇒ 本用例必红。"""
    root = Path(__file__).resolve().parents[2]
    found = {}
    for rel in _CHAIN_FILES:
        path = root / rel
        assert path.exists(), f"部署链文件缺失：{rel}"
        found[rel] = _max_model_len(path.read_text(encoding="utf-8"))

    assert found == _CHAIN_FILES, f"部署链上下文漂移：{found}"

    fallback = int(settings.HAS_NER_CONTEXT_FALLBACK_TOKENS)
    configured = int(settings.HAS_NER_CONTEXT_TOKENS)
    assert configured == 8192
    assert fallback == 4096
    assert fallback <= min(found.values()), f"回落 {fallback} 高于最窄链 {found}"
    # 生效上下文（探测失败回落）必须装得下一组（真源：生产两个常量，不写死数字）
    from app.services.vision.has_text_payload import _GROUP_FILL_MARGIN_TOKENS

    usable = min(configured, fallback) - int(_NER_CONTEXT_RESERVE_TOKENS) - int(
        _GROUP_FILL_MARGIN_TOKENS
    )
    assert usable > 0, f"生效上下文 {min(configured, fallback)} 装不下一组（可用 {usable}）"

    cfg = (root / "backend/app/core/config.py").read_text(encoding="utf-8")
    assert re.search(r"HAS_NER_CONTEXT_TOKENS:\s*int\s*=\s*8192\b", cfg)
    assert re.search(r"HAS_NER_CONTEXT_FALLBACK_TOKENS:\s*int\s*=\s*4096\b", cfg)


def test_i1_pinned_planning_context_reaches_every_dispatched_request(monkeypatch):
    """规划 pin 的 context 必须透传到每个实发 HaS 请求（不得逐请求重新探测）。

    判据锚在 ``has_text_analysis`` 的派发链上：``_resolve_effective_context`` 只探测一次，
    该值既用于规划分组、也必须作为 ``ner(..., context_tokens=...)`` 传给每个实发请求。
    变异判据：把派发处的 ``context_tokens=context_tokens`` 改成 ``context_tokens=None``
    ⇒ 每个请求各探一次 ⇒ 本用例必须变红（探测次数 > 1）。
    """
    probes: list[int] = []
    client = _ModelFake(context_tokens=8192, reply=lambda text, types: {})
    base_probe = client._effective_context_tokens

    def counting_probe() -> int:
        probes.append(client._ctx)
        return base_probe()

    monkeypatch.setattr(client, "_effective_context_tokens", counting_probe)

    # Issue#209：装填余量与 max_tokens 同源后，8 行恰好压线装进一组（6723+1245
    # == fill），判据「≥2 组」失去前提；加到 12 行让它稳定分成 2 组。
    lines = [_unique_dense(800, f"pin块{i}") for i in range(12)]
    stage: dict = {}
    asyncio.run(run_has_text_analysis(_blocks(*lines), client, [_VT("ID_CARD", "身份证号")], stage))

    groups = int(stage["has_text_payload_group_count"])
    assert groups >= 2, f"判据失效：只规划出 {groups} 组"
    assert len(client.calls) >= 2, f"判据失效：只有 {len(client.calls)} 次实发请求"
    assert probes == [8192], (
        f"规划 pin 的 context 未透传到实发请求：探测 {len(probes)} 次 {probes}（应为 1 次）"
    )
