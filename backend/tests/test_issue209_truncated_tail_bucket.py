"""Issue#209 C2：截断时**正在枚举的那个桶**也必须进补查集合。

回归背景（2026-10-29 门④实测，真实第四卷第 133 页）：该页是「水印碎片 + 密集
报关单表格」，模型对 `编号` 桶一行一行枚举真实报关单号（74 行），在 max_tokens=2048
处被切断。截断恢复原逻辑只补查 `missing = [t for t in types if t not in result]`——
被切断的桶**仍然存在**（只剩切点之前的几个值），所以永远不在补查集合里 ⇒ 该桶
尾值静默丢失，而 `confirmed` 还能是 True（cap=4096 实测真值：编号 83 个值，
切断后只剩 ~4 个）。密集页上这就是"少打码"。

修复：从截断原文里定位最后一个顶层桶开口（`"<类型>": [`），若它是本次请求类型
之一，就把它并进补查集合（补查按行边界分块 + 温度下限，各块内能枚举完整，
合并时按值去重），召回补齐。
"""

import json
import re

from app.services.has_client import HaSClient


def prompt_types(content: str) -> list[str]:
    """从提示词里取**请求类型清单**（`Specified types:[...]` 那一行）。

    只能这样取：正文里也可能出现类型名字面量（页 133 文本里就有「编号」列），
    直接 `"编号" in prompt` 会假阳性。
    """
    match = re.search(r"Specified types:(\[.*?\])", content, re.S)
    assert match is not None, f"no type list in prompt: {content[:120]!r}"
    return json.loads(match.group(1))

# 截断形态：末桶（编号）正在枚举真实报关单号时被 max_tokens 切断
TRUNCATED_ENUMERATING = (
    '{"姓名":["焦先生"],"编号":["*721720240174000394",'
    '"*721720240174000395","*7217202401740'
)

CLEAN_TAIL = json.dumps(
    {
        "编号": ["*721720240174000396", "*721720240174000397"],
        "薪酬": ["10K-15K/月"],
    },
    ensure_ascii=False,
)

TEXT = "焦先生 单号 *721720240174000394 报酬 10K-15K/月 表格列枚举"
TYPES = ["姓名", "编号", "薪酬"]


def make_client(responses):
    client = HaSClient()
    calls = []

    def fake_call_model(messages, *, max_tokens=None, temperature=None):
        calls.append(messages[0]["content"])
        return responses[min(len(calls) - 1, len(responses) - 1)]

    client._call_model = fake_call_model
    return client, calls


# ---------------------------------------------------------------- 纯函数：定位被切断的桶


def test_tail_bucket_returns_last_open_request_type():
    raw = '{"姓名":["焦先生"],"编号":["*721720240174000394","*7217202401740'
    assert HaSClient._truncated_tail_bucket(raw, TYPES) == "编号"


def test_tail_bucket_ignores_non_requested_keys():
    raw = '{"姓名":["焦先生"],"自定义项":["x","y'
    # 末桶不是本次请求的类型 ⇒ 不猜（返回 None）
    assert HaSClient._truncated_tail_bucket(raw, TYPES) is None


def test_tail_bucket_none_when_no_bucket_open():
    assert HaSClient._truncated_tail_bucket('{"姓名":["焦先生"]}', TYPES) is None
    assert HaSClient._truncated_tail_bucket("", TYPES) is None
    assert HaSClient._truncated_tail_bucket("not json at all", TYPES) is None


# ---------------------------------------------------------------- 集成：补查集合含被切断的类型


def test_truncated_enumerating_bucket_is_requeried_and_merged():
    client, calls = make_client([TRUNCATED_ENUMERATING, CLEAN_TAIL])
    result = client.ner(TEXT + " t1", TYPES, type_guidance=[])

    assert len(calls) == 2, f"expected exactly one retry, got {len(calls)} calls"
    # 关键判据：补查的类型清单含被切断的「编号」（C2）+ 整桶丢失的「薪酬」
    retry_types = prompt_types(calls[1])
    assert "编号" in retry_types, "cut bucket must be included in the re-query"
    assert "薪酬" in retry_types, "missing bucket still re-queried"
    assert "姓名" not in retry_types, "already-populated bucket must not be re-queried"
    # 切点之前已落地的值保留，补查命中的新值合并进来（按值去重）
    assert "*721720240174000394" in result.get("编号", [])
    assert "*721720240174000395" in result.get("编号", [])
    assert "*721720240174000396" in result.get("编号", [])
    assert result.get("薪酬") == ["10K-15K/月"]
    # 截断残值（前缀）仍被丢弃，不因 C2 复活
    assert "*7217202401740" not in result.get("编号", [])


def test_no_cut_bucket_keeps_legacy_requery_set():
    """末桶不是请求类型时，补查集合与改前一致（只含 missing）。"""
    raw = '{"姓名":["焦先生"],"薪酬":["10K"],"其它":["a","b'
    client, calls = make_client([raw, CLEAN_TAIL])
    client.ner(TEXT + " t2", TYPES, type_guidance=[])

    assert len(calls) == 2
    retry_types = prompt_types(calls[1])
    assert "编号" in retry_types
    # 已存在的桶（姓名/薪酬）不因"末尾有非请求桶"被拉进补查
    assert "姓名" not in retry_types


def test_truncation_retry_still_capped_at_one():
    """C2 不引入额外层：补查自己再被切断时不再递归（_allow_truncation_retry=False）。"""
    client, calls = make_client([TRUNCATED_ENUMERATING, '{"编号":["*72172024'])
    client.ner(TEXT + " t3", TYPES, type_guidance=[])

    assert len(calls) == 2, f"retry must be capped at one, got {len(calls)} calls"

def test_cut_bucket_not_added_when_it_would_resend_full_payload():
    """并入被切断的桶若会让补查集合==全类型，则**不并入**（防自连接死锁）。

    同 payload / 同预算整段重发必然同结局，且补查 key 与本次 inflight 相同会
    白等到超时（历史 e2e：批调用 3×120s 挂起）。这里只钉「不并入」这一点，
    不钉重度行为（缺失类型仍补查一次）。
    """
    # 只修复出「编号」（即被切断的那个桶）一个桶
    raw = '{"编号":["*721720240174000394","*7217202401740'
    client, calls = make_client([raw, CLEAN_TAIL])
    client.ner(TEXT + " t4", TYPES, type_guidance=[])

    assert len(calls) == 2, "must still re-query the missing types exactly once"
    retry_types = prompt_types(calls[1])
    assert "编号" not in retry_types, (
        "full-payload resend would deadlock on its own inflight"
    )
    assert set(retry_types) == {"姓名", "薪酬"}
