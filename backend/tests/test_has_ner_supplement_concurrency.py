"""补查链并发化（Issue#216 收口 / 单文件提速试点）。

背景：补查循环是「文本块 × 类型批」双重串行 for（has_client.ner_checked 内），
每段实测 6~14s，9~12 段接龙能把页预算吃光（#209 页 133 老病）。#216 挂账
「补查并行化 + 单请求死线」。本文件钉：

1. HAS_NER_SUPPLEMENT_CONCURRENCY>1 且任务数 >1 时补查任务真并发派发
   （Barrier 证明：串行路径必然超时拿 marker）；
2. 默认 1 = 与旧串行逐字节等价（同响应序列 → 同结果）；
3. 并发结果与串行结果一致（提交序合并，乱序完成不影响）；
4. 观测面：has_text_ner_supplement_jobs 进 NerMetricsScope/drain；
5. raise_on_error=True 时段内异常照旧上抛（页判失败语义不因并发改变）。
"""

import json
import threading
import time

import pytest

from app.core.config import settings
from app.services.has_client import HaSClient, drain_ner_metrics, ner_metrics_scope

# 长文本多行：>600 阈值 → _split_requery_chunks 按行切成多块（每块 ≤450 字符）
LINES = [
    f"第{i}行 当事人{i}于某地签署某合同并发生某往来事项，涉及若干款项与凭证，"
    f"另有多份补充材料与往来记录需要一并核对归档，具体金额与日期以原始凭证为准。"
    for i in range(10)
]
LONG_TEXT = "\n".join(LINES)

TYPES = ["姓名", "日期", "薪酬", "年龄（岁）"]

# 主查响应：截断形态（direct 解析必败 → json_repair 恢复）——补查链只在
# json_repair 路径触发。恢复后 姓名/日期 两桶在、薪酬/年龄（岁）整桶缺失，
# 末桶残值 "2" 被 trim → 触发补查（与既有 FLOOD_TRUNCATED 形态同源）。
MAIN_RESPONSE = '{"姓名":["焦先生"],"日期":["2016.04 - 至今","2012.07 - 2013.07","2'
REQUERY_RESPONSE = '{"薪酬":["10K-15K/月"],"年龄（岁）":["36岁"]}'


def _types_in_prompt(prompt: str) -> list[str]:
    """从 prompt 里剥出 Specified types 的 JSON 数组（数组后还有指令文本与正文）。"""
    segment = prompt.split("Specified types:", 1)[1]
    start = segment.find("[")
    end = segment.find("]", start)
    return json.loads(segment[start : end + 1])


def make_client(main_response=MAIN_RESPONSE, requery_response=REQUERY_RESPONSE):
    """fake _call_model：主查（4 型）回 main_response，补查（≤2 型）回 requery_response。"""
    client = HaSClient()
    calls: list[str] = []

    def fake_call_model(messages, *, max_tokens=None, temperature=None):
        prompt = messages[0]["content"]
        calls.append(prompt)
        requested = _types_in_prompt(prompt)
        if len(requested) == len(TYPES):
            return main_response
        return requery_response

    client._call_model = fake_call_model
    return client, calls


def _requery_job_count(text: str) -> int:
    chunks = HaSClient._split_requery_chunks(text)
    # 2 个缺失类型、默认预算（2×160 + 短文本余量）≤ cap → 单类型批 ⇒ 任务数 = 块数
    return len(chunks)


def test_requery_fixture_produces_multiple_jobs():
    """夹具自检：长文本确实切出 >1 个补查块（任务数 >1 是并发化的前提）。"""
    assert _requery_job_count(LONG_TEXT) > 1


def test_supplement_concurrency_default_serial_is_unchanged():
    """默认（窗口=1）行为零变化：任务数、合并结果与旧串行一致。"""
    client, calls = make_client()
    result = client.ner(LONG_TEXT, TYPES, type_guidance=[])
    chunks = _requery_job_count(LONG_TEXT)
    # 1 次主查 + N 次补查（单类型批 × 每块一次）
    assert len(calls) == 1 + chunks
    assert result.get("薪酬") == ["10K-15K/月"]
    assert result.get("年龄（岁）") == ["36岁"]
    assert result.get("姓名") == ["焦先生"]
    # 截断残值 '2' 被 trim，日期桶保留完整值（补查按块重枚举可能并入更多）
    assert "2" not in result.get("日期", [])
    assert "2016.04 - 至今" in result.get("日期", [])


def test_supplement_concurrency_dispatches_in_parallel():
    """窗口=4：全部补查任务同时在场（Barrier parties=任务数）。

    串行实现下第一个补查调用必然等满 barrier 超时 → 回 SERIAL-MARKER；
    并发实现下所有任务同时在场 → barrier 放行 → 无 marker。
    """
    jobs = _requery_job_count(LONG_TEXT)
    barrier = threading.Barrier(jobs, timeout=10)
    client = HaSClient()
    calls: list[str] = []

    def fake_call_model(messages, *, max_tokens=None, temperature=None):
        prompt = messages[0]["content"]
        calls.append(prompt)
        requested = _types_in_prompt(prompt)
        if len(requested) == len(TYPES):
            return MAIN_RESPONSE
        try:
            barrier.wait(timeout=10)
        except threading.BrokenBarrierError:
            return '{"薪酬":["SERIAL-MARKER"],"年龄（岁）":["SERIAL-MARKER"]}'
        return REQUERY_RESPONSE

    client._call_model = fake_call_model
    monkey_window = pytest.MonkeyPatch()
    monkey_window.setattr(settings, "HAS_NER_SUPPLEMENT_CONCURRENCY", 4, raising=False)
    try:
        result = client.ner(LONG_TEXT, TYPES, type_guidance=[])
    finally:
        monkey_window.undo()
    assert len(calls) == 1 + jobs
    assert result.get("薪酬") == ["10K-15K/月"], result
    assert result.get("年龄（岁）") == ["36岁"], result
    assert "SERIAL-MARKER" not in json.dumps(result, ensure_ascii=False)
    assert "2016.04 - 至今" in result.get("日期", []), result


def test_supplement_parallel_result_equals_serial():
    """同响应序列下，窗口=1 与窗口=4 的合并结果逐值一致（提交序合并钉死）。

    注意：NER 结果缓存是**类级共享**（conftest 只在测试间清），同一测试内
    串行腿会污染并行腿的主查键——并行腿前必须清缓存，否则并行腿直接命中
    缓存、并发链根本没跑（变异假存活的根源）。
    """
    serial_client, _ = make_client()
    serial_result = serial_client.ner(LONG_TEXT, TYPES, type_guidance=[])

    HaSClient._SHARED_NER_CACHE.clear()
    parallel_client, _ = make_client()
    patcher = pytest.MonkeyPatch()
    patcher.setattr(settings, "HAS_NER_SUPPLEMENT_CONCURRENCY", 4, raising=False)
    try:
        parallel_result = parallel_client.ner(LONG_TEXT, TYPES, type_guidance=[])
    finally:
        patcher.undo()

    assert parallel_result == serial_result


def test_supplement_merge_follows_submission_order_not_completion_order():
    """合并按**提交序**（= 旧串行序），不按完成序。

    各块返回互不相同、可区分的值，并给靠后的块更短延迟（完成序≈提交序的
    逆序）。若实现按完成序合并，薪酬桶顺序会颠倒——此测试即红。
    """
    lines = LONG_TEXT.split("\n")

    def chunk_index(prompt: str) -> int:
        for i, line in enumerate(lines):
            if line in prompt:
                return i
        return -1

    def make_delayed_client():
        client = HaSClient()
        calls: list[str] = []

        def fake_call_model(messages, *, max_tokens=None, temperature=None):
            prompt = messages[0]["content"]
            requested = _types_in_prompt(prompt)
            if len(requested) == len(TYPES):
                calls.append(prompt)
                return MAIN_RESPONSE
            idx = chunk_index(prompt)
            time.sleep(0.02 * (10 - idx))  # 前面的块睡更久 → 完成序是逆序
            calls.append(prompt)
            return json.dumps(
                {"薪酬": [f"值-{idx}"], "年龄（岁）": [f"龄-{idx}"]},
                ensure_ascii=False,
            )

        client._call_model = fake_call_model
        return client, calls

    serial_client, serial_calls = make_delayed_client()
    serial_result = serial_client.ner(LONG_TEXT, TYPES, type_guidance=[])
    expected = serial_result["薪酬"]
    # 块 0（提交序第一）睡最久、完成序必然最后——串行桶首元素必须是块 0 的值
    assert expected[0] == "值-0", expected
    assert len(set(expected)) == len(expected), expected

    parallel_client, _ = make_delayed_client()
    HaSClient._SHARED_NER_CACHE.clear()  # 类级共享缓存：串行腿已写入主查键
    patcher = pytest.MonkeyPatch()
    patcher.setattr(settings, "HAS_NER_SUPPLEMENT_CONCURRENCY", 8, raising=False)
    try:
        parallel_result = parallel_client.ner(LONG_TEXT, TYPES, type_guidance=[])
    finally:
        patcher.undo()

    assert parallel_result["薪酬"] == expected, (
        "补查合并必须按提交序（旧串行序），完成序颠倒不得改变桶内顺序"
    )
    assert parallel_result["年龄（岁）"] == serial_result["年龄（岁）"]


def test_supplement_jobs_metric_recorded():
    """观测面：并发补查时 has_text_ner_supplement_jobs=任务数进 drain。"""
    jobs = _requery_job_count(LONG_TEXT)
    client, _ = make_client()
    with ner_metrics_scope() as scope:
        patcher = pytest.MonkeyPatch()
        patcher.setattr(settings, "HAS_NER_SUPPLEMENT_CONCURRENCY", 4, raising=False)
        try:
            client.ner(LONG_TEXT, TYPES, type_guidance=[])
        finally:
            patcher.undo()
    metrics = drain_ner_metrics(scope)
    assert metrics.get("has_text_ner_supplement_jobs") == jobs


def test_supplement_window_clamped_to_gate_width():
    """窗口运行时钳到全局闸宽：闸=1 的部署上补查必须退化为串行。"""
    jobs = _requery_job_count(LONG_TEXT)
    barrier = threading.Barrier(jobs, timeout=2)
    client = HaSClient()

    def fake_call_model(messages, *, max_tokens=None, temperature=None):
        requested = _types_in_prompt(messages[0]["content"])
        if len(requested) == len(TYPES):
            return MAIN_RESPONSE
        try:
            barrier.wait(timeout=2)
        except threading.BrokenBarrierError:
            return '{"薪酬":["SERIAL-MARKER"],"年龄（岁）":["SERIAL-MARKER"]}'
        return REQUERY_RESPONSE

    client._call_model = fake_call_model
    patcher = pytest.MonkeyPatch()
    patcher.setattr(settings, "HAS_NER_SUPPLEMENT_CONCURRENCY", 4, raising=False)
    patcher.setattr(settings, "HAS_NER_GLOBAL_MAX_INFLIGHT", 1, raising=False)
    try:
        result = client.ner(LONG_TEXT, TYPES, type_guidance=[])
    finally:
        patcher.undo()
    assert "SERIAL-MARKER" in json.dumps(result, ensure_ascii=False)


def test_supplement_parallel_raise_on_error_propagates():
    """raise_on_error=True：补查段内异常照旧上抛（组失败 → 页判失败语义不变）。"""
    client = HaSClient()

    def fake_call_model(messages, *, max_tokens=None, temperature=None):
        requested = _types_in_prompt(messages[0]["content"])
        if len(requested) == len(TYPES):
            return MAIN_RESPONSE
        raise RuntimeError("boom")

    client._call_model = fake_call_model
    patcher = pytest.MonkeyPatch()
    patcher.setattr(settings, "HAS_NER_SUPPLEMENT_CONCURRENCY", 4, raising=False)
    try:
        with pytest.raises(RuntimeError, match="boom"):
            client.ner(LONG_TEXT, TYPES, type_guidance=[], raise_on_error=True)
    finally:
        patcher.undo()
