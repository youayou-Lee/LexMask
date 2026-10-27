"""Issue#198：截断补查换采样温度（逃逸复读）+ 补查逃逸观测面。

背景（真实案卷 134 页卷第 34 页两次复现）：temp=0 贪心解码走进复读循环，
主请求烧满 max_tokens（finish_reason=length）→ json_repair 修复后缺失类型
补查**继承温度 0** → 同贪心轨迹必然复发（实测 84s+76s 双循环）。修复：补查
温度 = max(调用方温度, HAS_NER_TRUNCATION_RETRY_TEMPERATURE=0.7)，只升不
降（自洽趟不被拉低）；补查嵌套 ner_metrics_scope 记录其自身 finish_reason/
confirmed/recovered，日志 "HaS NER re-query finished: …" 为门④「补查逃逸」
判据的观测面。

变异靶：has_client.py 补查调用处 `temperature=retry_temperature`——还原为
`temperature=temperature` 应令本文件变红（跑整个文件，禁 -k 限域；变异前清
__pycache__）。
"""

import json
import logging

from app.core.config import settings
from app.services.has_client import LAST_FINISH_REASON, HaSClient

FLOOD_TRUNCATED = (
    '{"姓名":["焦先生"],"日期":["2016.04 - 至今（8年1个月）","2012.07 - 2013.07 (1年)",'
    '"2016.04 - 至今（8年1个月）","2012.07 - 2013.07 (1年)","2'
)

CLEAN_TAIL = '{"薪酬":["10K-15K/月","2.8万-3.5万/月"],"年龄（岁）":["36岁"]}'

TRUNCATED_AGAIN = '{"薪酬":["10K'

TEXT = "焦先生 男 36岁 (1988年4月) 咨询项目经理 2.8万-3.5万/月 10K-15K/月 2016.04 - 至今（8年1个月） 2012.07 - 2013.07 (1年)"

TYPES = ["姓名", "日期", "薪酬", "年龄（岁）"]


def make_client(responses):
    """responses: [(响应体, finish_reason), ...]；桩捕获每次调用的 temperature
    kwarg（payload 级证据，不读模块属性回显），并模拟真实 _call_model 的
    LAST_FINISH_REASON 写入通道。"""
    client = HaSClient()
    calls = []

    def fake_call_model(messages, *, max_tokens=None, temperature=None):
        idx = len(calls)
        body, finish_reason = responses[min(idx, len(responses) - 1)]
        if finish_reason:
            LAST_FINISH_REASON.set(finish_reason)
        calls.append(
            {
                "content": messages[0]["content"],
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
        )
        return body

    client._call_model = fake_call_model
    return client, calls


def test_truncated_requery_escalates_temperature_and_merges():
    client, calls = make_client([(FLOOD_TRUNCATED, "length"), (CLEAN_TAIL, "stop")])
    result = client.ner(TEXT + " t1", TYPES, type_guidance=[], temperature=0.0)

    assert len(calls) == 2, f"expected exactly one retry, got {len(calls)} calls"
    # 种子趟保持调用方温度；补查趟升到设置默认 0.7
    assert calls[0]["temperature"] == 0.0
    assert calls[1]["temperature"] == 0.7
    # 补查 prompt 只带缺失类型
    assert "薪酬" in calls[1]["content"] and "年龄（岁）" in calls[1]["content"]
    assert "姓名" not in calls[1]["content"]
    # 合并结果闭合全部缺失桶
    assert result.get("薪酬") == ["10K-15K/月", "2.8万-3.5万/月"]
    assert result.get("年龄（岁）") == ["36岁"]


def test_requery_temperature_follows_setting_and_none_seed(monkeypatch):
    monkeypatch.setattr(settings, "HAS_NER_TRUNCATION_RETRY_TEMPERATURE", 0.9)
    client, calls = make_client([(FLOOD_TRUNCATED, "length"), (CLEAN_TAIL, "stop")])
    # temperature=None（_call_model 内部折算 0.0 的真实路径）也要走 max 语义
    client.ner(TEXT + " t2", TYPES, type_guidance=[])

    assert len(calls) == 2
    assert calls[1]["temperature"] == 0.9, "接线钉：设置必须真正到达补查调用"


def test_requery_temperature_never_downgrades_caller():
    client, calls = make_client([(FLOOD_TRUNCATED, "length"), (CLEAN_TAIL, "stop")])
    client.ner(TEXT + " t3", TYPES, type_guidance=[], temperature=1.2)

    assert len(calls) == 2
    assert calls[1]["temperature"] == 1.2, "max 语义：调用方已更高的温度不被拉低"


def test_clean_response_keeps_single_call_and_caller_temperature(caplog):
    clean = json.dumps(
        {"姓名": ["焦先生"], "日期": ["2016.04 - 至今（8年1个月）"]}, ensure_ascii=False
    )
    client, calls = make_client([(clean, "stop")])
    with caplog.at_level(logging.WARNING, logger="app.services.has_client"):
        client.ner(TEXT + " t4", TYPES, type_guidance=[], temperature=0.0)

    assert len(calls) == 1
    assert calls[0]["temperature"] == 0.0
    assert "re-query finished" not in caplog.text, "无截断不得触发补查观测日志"


def test_requery_retruncated_capped_confirmed_false(caplog):
    client, calls = make_client(
        [(FLOOD_TRUNCATED, "length"), (TRUNCATED_AGAIN, "length")]
    )
    with caplog.at_level(logging.WARNING, logger="app.services.has_client"):
        _, confirmed = client.ner_checked(TEXT + " t5", TYPES, type_guidance=[])

    assert len(calls) == 2, "补查封顶语义不变：至多一次"
    assert calls[1]["temperature"] == 0.7
    assert confirmed is False
    # 观测面在坏状态下如实报 length/False——判据②据此区分「逃逸」与「被封顶」
    assert "re-query finished: finish_reason=length confirmed=False" in caplog.text


def test_rebatch_recursion_inherits_caller_temperature_characterization():
    """Issue#200 特征化钉（冷审 I3b）：解析失败分批递归现状 = 子批继承调用方
    温度。25 类型 > _NER_REBATCH_TYPE_THRESHOLD(24) → batch_size=24 → 首调用
    不可解析后递归 2 个子批。#200 后续修改本行为时此钉可见。"""
    types = [f"类型{i}" for i in range(25)]
    bodies = [
        ("this is not json at all", None),
        (json.dumps({"类型0": ["甲"]}, ensure_ascii=False), "stop"),
        (json.dumps({"类型24": ["乙"]}, ensure_ascii=False), "stop"),
    ]
    client, calls = make_client(bodies)
    merged = client.ner(TEXT + " t6", types, type_guidance=[], temperature=0.0)

    assert len(calls) == 3, "首调用 + 2 个子批（24+1）"
    assert calls[1]["temperature"] == 0.0, "现状：递归子批继承调用方温度（未升级）"
    assert calls[2]["temperature"] == 0.0
    assert merged.get("类型0") == ["甲"]
    assert merged.get("类型24") == ["乙"]


# ---- Issue#198 追加：补查分块（温度逃逸被门④证伪后的第二层） ----


def _long_multiline_text(lines: int = 9, chars_per_line: int = 100) -> str:
    body = "购货记录 行内容 数字 1234567890 姓名 林辉 证件号码 452428198706122537"
    # 行带序号：内容互不相同 ⇒ 各块文本互异，不触发 NER 缓存去重（缓存命中
    # 会让同文本块不发模型调用，调用计数断言会失真）。
    return "\n".join(f"行{i} " + (body * 3)[: chars_per_line - 4] for i in range(lines))


def test_split_requery_chunks_unit():
    fn = HaSClient._split_requery_chunks
    short = "短文本"
    assert fn(short) == [short]
    long_text = _long_multiline_text()
    chunks = fn(long_text)
    assert len(chunks) >= 2, "900+ 字符必须切块"
    assert all(len(c) <= 450 + 120 for c in chunks), "每块不超过目标+单行余量"
    assert "".join(chunks).replace("\n", "") == long_text.replace("\n", ""), "切分不丢字符"
    no_newline = "A" * 1000
    assert fn(no_newline) == [no_newline[i : i + 450] for i in range(0, 1000, 450)]


def test_truncated_requery_long_text_chunked_and_merged(caplog):
    text = _long_multiline_text()
    clean = json.dumps({"薪酬": ["10K-15K/月"], "年龄（岁）": ["36岁"]}, ensure_ascii=False)
    client, calls = make_client(
        [(FLOOD_TRUNCATED, "length"), (clean, "stop"), (clean, "stop")]
    )
    with caplog.at_level(logging.WARNING, logger="app.services.has_client"):
        result = client.ner(text, TYPES, type_guidance=[], temperature=0.0)

    assert len(calls) == 4, f"主查 1 次 + 3 块补查（100 字符行按 450 目标打包成 3 块），got {len(calls)}"
    assert all(c["temperature"] == 0.7 for c in calls[1:]), "每块补查都带升温"
    assert "re-query split into 3" in caplog.text
    assert "re-query finished: finish_reason=stop confirmed=True" in caplog.text
    assert result.get("薪酬") == ["10K-15K/月"]
    assert result.get("年龄（岁）") == ["36岁"]


def test_chunk_retruncated_confirmed_false():
    text = _long_multiline_text()
    client, calls = make_client(
        [(FLOOD_TRUNCATED, "length"), (CLEAN_TAIL, "stop"), (TRUNCATED_AGAIN, "length")]
    )
    _, confirmed = client.ner_checked(text, TYPES, type_guidance=[])

    assert len(calls) == 4, "主查 1 次 + 3 块补查"
    assert confirmed is False, "任一块再截断 ⇒ 整体 confirmed=False（不写缓存）"
