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
    # 38 类型 × text_budget 600：整页 desired = 38*160+600 = 6680 > 2048
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
    # 每批预算 ≤ 帽：desired(batch) = len(batch)*160 + 600 ≤ 2048（容量 9 型/批）
    for c in calls:
        assert len(c["types"]) * 160 + 600 <= 2048
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
    """预算=帽 → 不分批（边界）。desired = n*160+600 ≤ 2048 → n=9（2040）。"""
    monkeypatch.setattr(settings, "HAS_NER_COMPLETION_HARD_CAP", 2048, raising=False)
    types = _TYPES_38[:9]
    clean = json.dumps({t: [f"值-{t}"] for t in types}, ensure_ascii=False)
    client, calls = make_client([clean])
    client.ner(_LONG_TEXT + " eq-cap", types)
    assert len(calls) == 1, "预算恰在帽内不应分批"


def test_budget_one_over_cap_splits(monkeypatch):
    monkeypatch.setattr(settings, "HAS_NER_COMPLETION_HARD_CAP", 2048, raising=False)
    types = _TYPES_38[:10]  # 10*160+600 = 2200 > 2048（9 型=2040 恰在帽内）
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


def test_budget_over_cap_batches_also_with_type_guidance(monkeypatch):
    """guidance 路径同样前置分批（生产 guidance=None，此用例钉住不回归）。"""
    monkeypatch.setattr(settings, "HAS_NER_COMPLETION_HARD_CAP", 2048, raising=False)

    def respond(messages, *, max_tokens=None, temperature=None):
        batch = _prompt_types(messages[0]["content"])
        return json.dumps({t: [f"值-{t}"] for t in batch}, ensure_ascii=False)

    client = HaSClient()
    calls = []

    def respond_count(messages, *, max_tokens=None, temperature=None):
        calls.append(_prompt_types(messages[0]["content"]))
        return respond(messages, max_tokens=max_tokens, temperature=temperature)

    client._call_model = respond_count
    guidance = [{"type": t, "description": "测试引导"} for t in _TYPES_38]
    with ner_metrics_scope() as scope:
        result = client.ner(_LONG_TEXT + " with-guidance", _TYPES_38, guidance)
    full = set(_TYPES_38)
    assert not any(set(c) == full for c in calls), "guidance 路径整页 payload 被发出"
    assert set(result) == full
    metrics = drain_ner_metrics(scope)
    assert metrics.get("has_text_ner_rebatch_batches", 0) >= 2


def test_cap_takes_over_legacy_prebatch_when_budget_fits(monkeypatch):
    """帽>0 且预算装得下 → 整页单发，legacy 预分批（12 型/1600 字符规则）被接管。

    区分性入参：15 型 × 2000 字符——legacy 规则会拆（>12 型且>1600 字符），
    帽规则单发（15×160+1000=3400 ≤ 8192）。帽=0 时同样入参走 legacy 拆批。"""
    text = "字" * 2000
    types = _TYPES_38[:15]

    def make(calls):
        def respond(messages, *, max_tokens=None, temperature=None):
            batch = _prompt_types(messages[0]["content"])
            calls.append(batch)
            return json.dumps({t: [f"值-{t}"] for t in batch}, ensure_ascii=False)
        return respond

    client_on = HaSClient()
    calls_on = []
    client_on._call_model = make(calls_on)
    monkeypatch.setattr(settings, "HAS_NER_COMPLETION_HARD_CAP", 8192, raising=False)
    result_on = client_on.ner(text + " on", types)
    assert len(calls_on) == 1 and set(calls_on[0]) == set(types), "帽>0 预算装得下必须整页单发"
    assert set(result_on) == set(types)

    client_off = HaSClient()
    calls_off = []
    client_off._call_model = make(calls_off)
    monkeypatch.setattr(settings, "HAS_NER_COMPLETION_HARD_CAP", 0, raising=False)
    client_off.ner(text + " off", types)
    assert len(calls_off) >= 2, "帽=0 同入参应走 legacy 拆批（对照）"


def test_page_level_exempt_when_single_type_over_cap_by_text_length(monkeypatch):
    """页长到单类型都超帽（capacity<1）→ 页面级豁免整页单发，不拆 N 个仍截断的串行调用。"""
    monkeypatch.setattr(settings, "HAS_NER_COMPLETION_HARD_CAP", 2048, raising=False)
    huge = "字" * 5000  # text_budget=2500 > 2048 → capacity<1
    types = _TYPES_38[:10]
    calls = []

    def respond(messages, *, max_tokens=None, temperature=None):
        calls.append(_prompt_types(messages[0]["content"]))
        return json.dumps({t: [f"值-{t}"] for t in calls[-1]}, ensure_ascii=False)

    client = HaSClient()
    client._call_model = respond
    result = client.ner(huge + " cliff", types)
    assert len(calls) == 1 and set(calls[0]) == set(types), "capacity<1 必须整页单发豁免"
    assert set(result) == set(types)


def test_explicit_tokens_per_type_respected_everywhere(monkeypatch):
    """显式 HAS_NER_TOKENS_PER_TYPE 一律尊重（评审 Important-2/3）：预算与容量同源。"""
    monkeypatch.setattr(settings, "HAS_NER_TOKENS_PER_TYPE", 72, raising=False)
    client = HaSClient()
    budget = client._ner_completion_budget(12, "字" * 90, client._effective_tokens_per_type(settings))
    assert budget == 12 * 72 + 45, "显式 72 必须生效（≈preview 预算）"
    # 容量反推同源：settings=320 时容量按 320 反推，批预算必 ≤ 帽
    monkeypatch.setattr(settings, "HAS_NER_TOKENS_PER_TYPE", 320, raising=False)
    monkeypatch.setattr(settings, "HAS_NER_COMPLETION_HARD_CAP", 2048, raising=False)
    text = "字" * 1200  # text_budget=600
    calls = []

    def respond(messages, *, max_tokens=None, temperature=None):
        batch = _prompt_types(messages[0]["content"])
        calls.append(batch)
        return json.dumps({t: [f"值-{t}"] for t in batch}, ensure_ascii=False)

    client2 = HaSClient()
    client2._call_model = respond
    client2.ner(text + " eff", _TYPES_38[:20])
    for batch in calls:
        assert len(batch) * 320 + 600 <= 2048, f"批 {len(batch)} 型按 320 反推超帽"


def test_empty_text_and_zero_entity_and_many_entities_single_type(monkeypatch):
    """边界三件套（评审 Important-5）：空文本页 / 零实体页 / 单类型超多实体页。"""
    monkeypatch.setattr(settings, "HAS_NER_COMPLETION_HARD_CAP", 2048, raising=False)
    clean = json.dumps({"姓名": []}, ensure_ascii=False)

    # 空文本：不炸、单发
    client, calls = make_client([clean])
    r = client.ner("", ["姓名", "电话"])
    assert len(calls) == 1 and r == {"姓名": []}

    # 零实体页（模型返回 {}）：单发、结果空
    client2, calls2 = make_client(["{}"])
    r2 = client2.ner("普通文本", ["姓名"])
    assert calls2 and r2 == {}

    # 单类型超多实体：guidance=None 下多类型批不受影响；单类型照样单发
    big = json.dumps({"姓名": [f"人名{i}" for i in range(200)]}, ensure_ascii=False)
    client3, calls3 = make_client([big])
    r3 = client3.ner("一页超多人名 " + "字" * 300, ["姓名"])
    assert len(calls3) == 1 and len(r3.get("姓名", [])) == 200


def test_batch_path_requery_capped_once(monkeypatch):
    """分批路径下补查仍封顶 1 次/批（v2⑤）：每批截断修复后至多 1 次重查。

    首答=可修复损坏 JSON（只含批首类型）→ json_repair 恢复 → 缺其余类型 →
    重查 1 次（响应干净全量）。重查自身再截断也不会二次重查
    （_allow_truncation_retry=False）。"""
    monkeypatch.setattr(settings, "HAS_NER_COMPLETION_HARD_CAP", 2048, raising=False)

    def respond(messages, *, max_tokens=None, temperature=None):
        batch = _prompt_types(messages[0]["content"])
        if len(batch) >= 2:
            # 损坏但可修复：只含批首类型 → 触发一次补查
            return '{"' + batch[0] + '":["值-' + batch[0] + '","截断残'
        return json.dumps({t: [f"值-{t}"] for t in batch}, ensure_ascii=False)

    client = HaSClient()
    calls = []

    def counting(messages, *, max_tokens=None, temperature=None):
        calls.append(_prompt_types(messages[0]["content"]))
        return respond(messages, max_tokens=max_tokens, temperature=temperature)

    client._call_model = counting
    with ner_metrics_scope() as scope:
        result = client.ner(_LONG_TEXT + " batchcap", _TYPES_38[:20], type_guidance=[])
    # 20 型 → 容量 9 → 3 批；每批 1 主调用 + 1 补查 = 6 次
    assert len(calls) == 6, f"期望 3 批×(1 主+1 补查)=6，实际 {len(calls)}"
    assert len(result) >= 3
    assert drain_ner_metrics(scope).get("has_text_ner_truncation_retries") == 3


def test_recovered_result_lacking_all_types_skips_self_retry(monkeypatch):
    """修复结果不含任何请求类型 → 跳过自重查（防 inflight 自连接死等 120s×N）。"""
    monkeypatch.setattr(settings, "HAS_NER_COMPLETION_HARD_CAP", 2048, raising=False)
    import time as _t

    # 返回"完全不含请求类型"的可修复 JSON（姓名 不在请求清单里）
    bad = '{"姓名":["焦先生"],"日期":["2016.04"],"截'
    client, calls = make_client([bad])
    t0 = _t.perf_counter()
    with ner_metrics_scope() as scope:
        client.ner("文本 " + "字" * 100, ["薪酬", "年龄（岁）"], type_guidance=[])
    dt = _t.perf_counter() - t0
    assert len(calls) == 1, "不得自重查（同 key 自连接）"
    assert dt < 10, f"自连接死等复现：{dt:.0f}s"
    assert "has_text_ner_truncation_retries" not in drain_ner_metrics(scope)
