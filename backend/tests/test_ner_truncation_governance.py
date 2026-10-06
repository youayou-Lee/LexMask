"""Issue#41 NER 截断重试风暴治理。

背景（#37 WS-2 spike 实证）：模型卡 generation_config.json 写死
max_new_tokens:2048，密集页生产预算公式（类型数×72 + 文本字符/2）算出
~3904 token 被硬顶 → 6/6 finish=length → json_repair 修复 → 整类型补查，
"总是先打一次注定截断的整页调用再补救"。

治理两件套：
(a) HAS_NER_COMPLETION_HARD_CAP——有效完成帽进配置、参与 max_tokens 预算；
(b) 按密度主动分批——预算 > 有效帽时**前置**沿类型轴触发既有分组机制，
    不再发出注定截断的整页调用（豁免：单类型即使独占一批仍超帽的页，
    那是分批无解的页，走既有截断补救）。

外加 stage 观测三键（has_text_ner_finish_reason / has_text_ner_truncation_retries
/ has_text_ner_rebatch_batches），供 S1 量化与 e2e 判定"治理后整页截断调用数=0"。
"""

import json

from app.core.config import settings
from app.services.has_client import (
    HaSClient,
    drain_ner_metrics,
    ner_metrics_scope,
)

# 让 desired = types*72 + chars//2 轻易越过帽的小文本字符数（text_budget=600）
_LONG_TEXT = "字" * 1200
_TYPES_38 = [f"类型{i:02d}" for i in range(38)]


def make_client(responses):
    client = HaSClient()
    calls = []

    def fake_call_model(messages, *, max_tokens=None, temperature=None):
        calls.append({"content": messages[0]["content"], "max_tokens": max_tokens})
        return responses[min(len(calls) - 1, len(responses) - 1)]

    client._call_model = fake_call_model
    return client, calls


def _prompt_types(content: str) -> list[str]:
    marker = "Specified types:"
    seg = content.split(marker, 1)[1].split("\n", 1)[0].strip()
    return json.loads(seg)


# ---------------------------------------------------------------------------
# (b) 主动分批
# ---------------------------------------------------------------------------


def test_budget_over_cap_prebatches_without_doomed_full_call(monkeypatch):
    """预算>帽 → 主动分批：不发整页 payload，逐批预算≤帽，结果合并。"""
    monkeypatch.setattr(settings, "HAS_NER_COMPLETION_HARD_CAP", 2048, raising=False)
    # 38 类型 × text_budget 600：整页 desired = 38*72+600 = 3336 > 2048
    client, calls = make_client([])

    def respond(messages, *, max_tokens=None, temperature=None):
        types = _prompt_types(messages[0]["content"])
        calls.append({"types": types, "max_tokens": max_tokens})
        payload = {t: [f"值-{t}"] for t in types}
        return json.dumps(payload, ensure_ascii=False)

    client._call_model = respond
    with ner_metrics_scope() as scope:
        result = client.ner(_LONG_TEXT, _TYPES_38)

    full = set(_TYPES_38)
    assert calls, "expected batched calls"
    assert not any(set(c["types"]) == full for c in calls), "整页 payload 被发出（注定截断）"
    assert len(calls) >= 2, "密集场景应分 ≥2 批"
    # 每批预算 ≤ 帽：desired(batch) = len(batch)*72 + 600 ≤ 2048
    for c in calls:
        assert len(c["types"]) * 72 + 600 <= 2048
        assert c["max_tokens"] <= 2048
    # 全部类型与值都在合并结果里
    assert set(result) == full
    assert result["类型00"] == ["值-类型00"]
    # 指标：分批批数 ≥2
    metrics = drain_ner_metrics(scope)
    assert metrics["has_text_ner_rebatch_batches"] == len(calls) >= 2


def test_budget_within_cap_is_semantically_equivalent(monkeypatch):
    """预算≤帽 → 行为与现状一致：单次调用，不产新路径指标键。"""
    monkeypatch.setattr(settings, "HAS_NER_COMPLETION_HARD_CAP", 2048, raising=False)
    clean = json.dumps({"姓名": ["张三"]}, ensure_ascii=False)
    client, calls = make_client([clean])
    with ner_metrics_scope() as scope:
        result = client.ner("张三 联系电话 13800000000", ["姓名", "电话"])

    assert len(calls) == 1
    assert result == {"姓名": ["张三"]}
    metrics = drain_ner_metrics(scope)
    # 新键仅新路径产出：干净路径无分批/补查键
    assert "has_text_ner_rebatch_batches" not in metrics
    assert "has_text_ner_truncation_retries" not in metrics
    # fake _call_model 不写 finish_reason 通道 → 不产 finish_reason 键
    assert "has_text_ner_finish_reason" not in metrics


def test_budget_equals_cap_keeps_single_call(monkeypatch):
    """预算=帽 → 不分批（边界）。desired = n*72+600 = 2048 → n=20.11 → 20 类型时=2040。"""
    monkeypatch.setattr(settings, "HAS_NER_COMPLETION_HARD_CAP", 2048, raising=False)
    types = _TYPES_38[:20]
    clean = json.dumps({t: [f"值-{t}"] for t in types}, ensure_ascii=False)
    client, calls = make_client([clean])
    client.ner(_LONG_TEXT + " eq-cap", types)
    assert len(calls) == 1, "预算恰在帽内不应分批"


def test_budget_one_over_cap_splits(monkeypatch):
    monkeypatch.setattr(settings, "HAS_NER_COMPLETION_HARD_CAP", 2048, raising=False)
    types = _TYPES_38[:21]  # 21*72+600 = 2112 > 2048
    payload = {t: [f"值-{t}"] for t in types}

    def respond(messages, *, max_tokens=None, temperature=None):
        batch = _prompt_types(messages[0]["content"])
        return json.dumps({t: payload[t] for t in batch}, ensure_ascii=False)

    client = HaSClient()
    client._call_model = respond
    result = client.ner(_LONG_TEXT + " one-over", types)
    assert set(result) == set(types)


def test_single_type_over_cap_page_is_exempt(monkeypatch):
    """单类型独占一批仍超帽 → 豁免：不无限分，单调用发出（走既有截断补救）。"""
    monkeypatch.setattr(settings, "HAS_NER_COMPLETION_HARD_CAP", 128, raising=False)
    clean = json.dumps({"姓名": ["张三"]}, ensure_ascii=False)
    client, calls = make_client([clean])
    with ner_metrics_scope() as scope:
        result = client.ner(_LONG_TEXT + " exempt", ["姓名"])
    assert len(calls) == 1
    assert calls[0]["max_tokens"] <= 128, "帽配置仍须约束 max_tokens"
    assert result == {"姓名": ["张三"]}
    metrics = drain_ner_metrics(scope)
    assert "has_text_ner_rebatch_batches" not in metrics


def test_hard_cap_disabled_restores_current_behavior(monkeypatch):
    """帽≤0 → 关闭：超预算也不分批、不设 max_tokens 帽（现状路径）。"""
    monkeypatch.setattr(settings, "HAS_NER_COMPLETION_HARD_CAP", 0, raising=False)
    clean = json.dumps({"姓名": ["张三"]}, ensure_ascii=False)
    client, calls = make_client([clean])
    client.ner(_LONG_TEXT + " disabled", _TYPES_38)
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# 观测三键
# ---------------------------------------------------------------------------


def test_truncation_retry_counted(monkeypatch):
    monkeypatch.setattr(settings, "HAS_NER_COMPLETION_HARD_CAP", 2048, raising=False)
    from tests.test_has_ner_truncation_retry import CLEAN_TAIL, FLOOD_TRUNCATED, TEXT, TYPES

    client, calls = make_client([FLOOD_TRUNCATED, CLEAN_TAIL])
    with ner_metrics_scope() as scope:
        client.ner(TEXT + " gov1", TYPES, type_guidance=[])
    metrics = drain_ner_metrics(scope)
    assert metrics["has_text_ner_truncation_retries"] >= 1


def test_finish_reason_length_recorded(monkeypatch):
    """真 _call_model 会把 finish_reason 写进上下文通道（此处模拟其行为）。"""
    from app.services import has_client as hc

    monkeypatch.setattr(settings, "HAS_NER_COMPLETION_HARD_CAP", 2048, raising=False)
    client = HaSClient()

    def fake_call_model(messages, *, max_tokens=None, temperature=None):
        hc.LAST_FINISH_REASON.set("length")
        return json.dumps({"姓名": ["张三"]}, ensure_ascii=False)

    client._call_model = fake_call_model
    with ner_metrics_scope() as scope:
        client.ner("张三", ["姓名"])
    metrics = drain_ner_metrics(scope)
    assert metrics["has_text_ner_finish_reason"] == "length"


def test_finish_reason_stop_recorded_via_real_call_model(monkeypatch):
    """_call_model 本体把 finish_reason 写入通道；stop 场景记 stop。"""

    client = HaSClient()

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"finish_reason": "stop", "message": {"content": "{}"}}]}

    def fake_post(url, json=None, **kwargs):
        return _Resp()

    client._http_client.post = fake_post
    with ner_metrics_scope() as scope:
        client.ner("无敏感内容文本", ["姓名"])
    metrics = drain_ner_metrics(scope)
    assert metrics["has_text_ner_finish_reason"] == "stop"


def test_scope_nested_and_thread_isolated():
    """scope 遵循 contextvar 语义：嵌套叠加、线程外不可见（不串页）。"""
    import threading

    from app.services import has_client as hc

    with ner_metrics_scope() as outer:
        with ner_metrics_scope():
            hc.LAST_FINISH_REASON.set("length")
            hc._record_ner_metrics(finish_reason="length", truncation_retries=1)
        assert drain_ner_metrics(outer)["has_text_ner_truncation_retries"] >= 1

    seen = {}

    def worker():
        with ner_metrics_scope() as scope:
            hc._record_ner_metrics(truncation_retries=5)
            seen["v"] = drain_ner_metrics(scope)["has_text_ner_truncation_retries"]

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert seen["v"] == 5


# ---------------------------------------------------------------------------
# stage 接线：_run_has_text_analysis 把 scope 指标写进 stage_status
# ---------------------------------------------------------------------------


def test_stage_wiring_writes_metrics_into_stage_status(monkeypatch):
    import asyncio
    import json as _json

    from app.services.has_client import HaSClient
    from app.services.ocr_has_vision_service import OcrHasVisionService, OCRTextBlock

    service = OcrHasVisionService.__new__(OcrHasVisionService)
    service._has_client = None  # 触发 lazy re-init 前先手动注入
    client = HaSClient()
    service._has_client = client

    def fake_call_model(messages, *, max_tokens=None, temperature=None):
        from app.services import has_client as hc
        hc.LAST_FINISH_REASON.set("stop")
        return _json.dumps({"姓名": ["张三"]}, ensure_ascii=False)

    client._call_model = fake_call_model

    class _Person:
        id = "PERSON"
        name = "姓名"

    blocks = [
        OCRTextBlock(text=f"张三 联系电话 13800000000 第{i}行文字内容足够长",
                     polygon=[[0, 40 * i], [500, 40 * i], [500, 40 * i + 30], [0, 40 * i + 30]])
        for i in range(6)
    ]
    stage_status: dict = {}
    result = asyncio.run(
        service._run_has_text_analysis(blocks, [_Person()], stage_status=stage_status)
    )
    assert result and result[0]["type"]
    assert stage_status.get("has_text_ner_finish_reason") == "stop"
    assert "has_text_ner_truncation_retries" not in stage_status
