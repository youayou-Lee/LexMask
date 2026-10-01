"""Issue#41 NER 截断重试风暴治理：预算感知预分批 + stats 指标缝。

风暴机理（spike 实证）：模型卡 max_new_tokens=2048 硬帽下，
①短文本(≤1600)+类型数>17：单发注定截断（should_single_pass 不看预算）；
②长文本(>2368)：12 类型预分批子批预算 864+text//2 超帽。
修法=预分批触发与批大小预算感知（_ner_pre_batch_chunk）+ stats 指标外传。
"""

import asyncio
import json
import threading

import httpx
import pytest

from app.services.has_client import HaSClient, _ner_pre_batch_chunk


@pytest.fixture(autouse=True)
def _clean_shared_client_state():
    for attr in ("_SHARED_NER_CACHE", "_SHARED_NER_INFLIGHT"):
        obj = getattr(HaSClient, attr, None)
        if obj is not None:
            obj.clear()
    yield
    for attr in ("_SHARED_NER_CACHE", "_SHARED_NER_INFLIGHT"):
        obj = getattr(HaSClient, attr, None)
        if obj is not None:
            obj.clear()


def _resp(content: str, finish_reason: str = "stop"):
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": finish_reason}],
            "usage": {"completion_tokens": 10},
        },
    )


def _client(monkeypatch, responder):
    """responder(payload, call_index) -> httpx.Response；记录每次 payload。"""
    c = HaSClient(base_url="http://stub:0")
    calls: list[dict] = []

    def _fake(base, payload):
        calls.append(payload)
        return responder(payload, len(calls))

    monkeypatch.setattr(c, "_do_chat_request", _fake)
    return c, calls


def _types_from_prompt(payload) -> list[str]:
    content = payload["messages"][0]["content"]
    line = next(ln for ln in content.splitlines() if ln.startswith("Specified types:"))
    return json.loads(line[len("Specified types:"):])


def _answer_for_types(types: list[str]) -> str:
    return json.dumps({t: [f"值-{t}"] for t in types}, ensure_ascii=False)


# ---------- 纯函数：预分批决策 ----------

def test_chunk_no_batching_when_budget_fits():
    need, chunk = _ner_pre_batch_chunk(
        types_count=5, text_chars=500, desired_full=610,
        configured_batch=12, target_batch=24, hard_cap=2048,
    )
    assert need is False


def test_chunk_boundary_budget_eq_cap_single_call():
    # 预算 == 帽 → 单发（不分批）
    need, _ = _ner_pre_batch_chunk(
        types_count=20, text_chars=1216, desired_full=2048,
        configured_batch=12, target_batch=24, hard_cap=2048,
    )
    assert need is False


def test_chunk_boundary_budget_eq_cap_plus_one_batched():
    need, chunk = _ner_pre_batch_chunk(
        types_count=20, text_chars=1219, desired_full=2051,
        configured_batch=12, target_batch=24, hard_cap=2048,
    )
    assert need is True
    # 每批预算 ≤ 帽：chunk*72 + text//2 ≤ 2048
    assert chunk * 72 + 1219 // 2 <= 2048


def test_chunk_single_type_never_batches():
    need, chunk = _ner_pre_batch_chunk(
        types_count=1, text_chars=8000, desired_full=9999,
        configured_batch=12, target_batch=24, hard_cap=2048,
    )
    assert need is False  # 单类型无可分（走既有补救路径）


def test_chunk_oversized_text_per_type_fallback():
    # 文本长到单类型也超帽 → chunk=1 尽力分（json_repair 兜底）
    need, chunk = _ner_pre_batch_chunk(
        types_count=38, text_chars=8000, desired_full=9999,
        configured_batch=12, target_batch=24, hard_cap=2048,
    )
    assert need is True
    assert chunk == 1


# ---------- 集成：stats 缝与预分批行为 ----------

def test_stats_records_finish_reason_stop(monkeypatch):
    c, calls = _client(monkeypatch, lambda p, i: _resp('{"人名": ["张三"]}'))
    stats: dict = {}
    out = c.ner("张三来了", ["人名"], stats=stats)
    assert out == {"人名": ["张三"]}
    assert len(calls) == 1
    assert stats["ner_finish_reason"] == "stop"
    assert stats.get("ner_truncated_calls", 0) == 0


def test_stats_counts_truncation_retry(monkeypatch):
    seq = [
        _resp('{"人名": ["张三", "李四"', finish_reason="length"),  # 截断（可修复）
        _resp('{"人名": ["张三", "李四"], "案号": ["(2026)桂1"]}'),
    ]

    def responder(payload, i):
        return seq[min(i - 1, len(seq) - 1)]

    c, calls = _client(monkeypatch, responder)
    stats: dict = {}
    out = c.ner("张三和李四的案号", ["人名", "案号"], stats=stats)
    assert "人名" in out
    assert stats["ner_truncated_calls"] == 1
    assert stats["ner_truncation_retries"] >= 1


def test_doomed_single_call_now_batched(monkeypatch):
    """短文本+20 类型：现状单发预算 2240>帽注定截断；治理后预算感知预分批。"""
    types = [f"类型{i}" for i in range(20)]
    text = "字" * 1600

    def responder(payload, i):
        return _resp(_answer_for_types(_types_from_prompt(payload)))

    c, calls = _client(monkeypatch, responder)
    stats: dict = {}
    c.ner(text, types, stats=stats)
    assert len(calls) >= 2, "预算>帽必须预分批"
    for p in calls:
        assert p["max_tokens"] <= 2048, "每批预算必须 ≤ 帽"
    assert stats["ner_batches"] == len(calls)
    merged_types = set()
    for p in calls:
        merged_types.update(_types_from_prompt(p))
    assert merged_types == set(types), "类型轴分批不得丢类型"


def test_budget_fits_single_call_unchanged(monkeypatch):
    """预算≤帽：单发，行为与现状一致（语义等价）。"""
    c, calls = _client(monkeypatch, lambda p, i: _resp(_answer_for_types(_types_from_prompt(p))))
    stats: dict = {}
    out = c.ner("张三来了", ["人名", "案号", "金额"], stats=stats)
    assert len(calls) == 1
    assert out == {"人名": ["值-人名"], "案号": ["值-案号"], "金额": ["值-金额"]}
    assert stats.get("ner_batches", 0) == 0


def test_cap_config_effective(monkeypatch):
    """帽配置生效：帽调大 → 同一输入从分批退回单发。"""
    from app.core.config import settings
    types = [f"类型{i}" for i in range(20)]
    text = "字" * 1600

    def responder(payload, i):
        return _resp(_answer_for_types(_types_from_prompt(payload)))

    c, calls = _client(monkeypatch, responder)
    monkeypatch.setattr(settings, "HAS_NER_COMPLETION_HARD_CAP", 8192, raising=False)
    stats: dict = {}
    c.ner(text, types, stats=stats)
    assert len(calls) == 1, "帽=8192 时预算 2240≤帽，应单发"


def test_oversized_text_per_type_calls(monkeypatch):
    """超长文本（单类型也超帽）：按单类型尽力分，全部调用带各自类型。"""
    types = ["人名", "案号", "金额"]
    text = "字" * 6000  # text//2=3000 > 帽

    def responder(payload, i):
        return _resp(_answer_for_types(_types_from_prompt(payload)))

    c, calls = _client(monkeypatch, responder)
    stats: dict = {}
    c.ner(text, types, stats=stats)
    assert stats.get("ner_budget_unbatchable", 0) == 1
    assert len(calls) == len(types)
    for p in calls:
        assert len(_types_from_prompt(p)) == 1


def test_batch_subcall_retry_capped_once(monkeypatch):
    """分批路径下补查仍封顶 1 次：截断批→补查一次→不再递归失控。"""
    types = [f"类型{i}" for i in range(20)]
    text = "字" * 1600
    state = {"n": 0}

    def responder(payload, i):
        state["n"] += 1
        asked = _types_from_prompt(payload)
        if state["n"] == 1:
            return _resp("{\"类型0\": [\"值", finish_reason="length")
        return _resp(_answer_for_types(asked))

    c, calls = _client(monkeypatch, responder)
    stats: dict = {}
    c.ner(text, types, stats=stats)
    assert state["n"] <= len(types) + 2, "补查不得递归失控"


def test_run_has_text_analysis_merges_stats(monkeypatch):
    """stats→stage_status 合并：has_text_ner_finish_reason 进指标。"""
    from app.services.vision.has_text_analysis import run_has_text_analysis
    from app.services.ocr_has_vision_service import OCRTextBlock

    class _FakeWithStats:
        base_url = "http://stub:0"

        def ner(self, text, entity_types=None, stats=None, **_kw):
            if stats is not None:
                stats["ner_finish_reason"] = "stop"
            return {"人名": ["张三"]}

    blocks = [OCRTextBlock(text="张三来了", polygon=[[0, 0], [1, 0], [1, 1], [0, 1]], confidence=0.9)]
    stage: dict = {}
    asyncio.run(run_has_text_analysis(blocks, _FakeWithStats(), vision_types=None, stage_status=stage))
    assert stage.get("has_text_ner_finish_reason") == "stop"
