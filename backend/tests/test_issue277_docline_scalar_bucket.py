"""Issue#277 专项用例：NER 标量/字典桶的形状治理（解析出口单点 + 补查路由）。

生产实录（2026-11-12，本地容器 file=f5ea1139）：86 页真卷 docline 构建中，
`LEGAL_CASE_ID`（案号）贡献第二个查询标签「文书编号」⇒ docline 实发 **20 个标签**
⇒ 预算 20×160+1990//2=4195 > 完成帽 4096 ⇒ 帽驱动切批 [19, 1]——单类型批恰为
「文书编号」。模型偶发把单值回成**标量桶**（`{"文书编号": 2022}`），帽分批合并循环
`for value in values`（该块位于 `ner_checked` 大 try 之外）迭代 int 炸出
`TypeError: 'int' object is not iterable`，逃逸到 docline 块级 catch ⇒
failed_chunks=1/26 ⇒ 整卷弃权 ⇒ 97s 白烧 + 全卷逐页重算。

修复语义（验收方案 v2，门①评审 REVISE 后定稿）：
- 解析出口 `_sanitize_ner_buckets` 单点剥除非 list 桶——任何形状的坏桶活不过边界，
  下游全部合并循环与 docline 读侧循环同源免疫；
- 被剥掉的**真值标量桶** = 该类型结论「没拿到」（#108 口径）：confirmed=False、
  绝不落缓存键，并路由进**既有 missing-type 补查**换温度轨迹二机会
  （#209 C2「切断桶并入补查」同款先例）；
- 补查不可用（同键重发/禁补查）或仍标量 ⇒ 保持 confirmed=False ⇒ 既有
  「块失败 ⇒ 整卷弃权」语义不变（Issue 验收 2 明文要求保留）；
- 假值标量（0/None/""）≈ 模型答「无」，按 `{}` 口径剥除、不算缺失、不补查。

三条装置纪律（继承 #148 用例）：`HaSClient()` 构造即探测 /models（离线容错）；
`_SHARED_NER_CACHE`/`ner_breaker` 类级共享由 conftest autouse 夹具前后复位；
本文件全部走 `_call_model` 桩（不 MockTransport），`context_tokens` 显式传避免探针。
"""
from __future__ import annotations

import json
import logging

import pytest

from app.core.config import settings
from app.services.has_client import HaSClient
from app.services.text_layer_docline_service import run_document_ner

# 生产形状：19 个普通标签 + 第 20 个「文书编号」（LEGal_CASE_ID 的第二查询标签，
# 落在标签清单末位 ⇒ 帽切批后恰为单类型批）。
_TYPES_19 = [f"类型{i}" for i in range(19)]
_SCALAR_TYPE = "文书编号"
_TYPES_20 = [*_TYPES_19, _SCALAR_TYPE]

# len=1990：desired=20×160+995=4195>4096 触发切批；capacity=(4096-995)//160=19
# ⇒ 恰好 [19, 1]（与生产实录逐字节同形）。
_TEXT_1990 = ("张三2024年向某法院提交编号为2022的文书，" * 85)[:1990]
assert len(_TEXT_1990) == 1990


def _prompt_types(content: str) -> list[str]:
    marker = "Specified types:"
    seg = content.split(marker, 1)[1].split("\n", 1)[0].strip()
    return json.loads(seg)


def _make_client(monkeypatch, respond):
    """`_call_model` 桩：respond(types, scalar_call_index) -> str。"""
    monkeypatch.setattr(settings, "HAS_NER_COMPLETION_HARD_CAP", 4096, raising=False)
    client = HaSClient()
    calls: list[list[str]] = []

    def fake_call_model(messages, *, max_tokens=None, temperature=None):
        types = _prompt_types(messages[0]["content"])
        scalar_index = sum(1 for c in calls if c == [_SCALAR_TYPE])
        calls.append(types)
        return respond(types, scalar_index, temperature)

    client._call_model = fake_call_model
    return client, calls


# ------------------------------------------------------------------ A1① 红绿钉


def test_scalar_bucket_in_cap_split_recovers_via_requery(monkeypatch):
    """单类型批回标量桶：修复前 TypeError 逃逸（红）；修复后补查恢复并完整落缓存（绿）。

    红形态 = 生产日志逐字：`docline NER chunk failed: 'int' object is not iterable`
    （异常从 ner_checked 逃逸到调用方，本测试即调用方）。
    """

    def respond(types, scalar_index, temperature):
        if types == _TYPES_19:
            return json.dumps({t: [f"值-{t}"] for t in types}, ensure_ascii=False)
        if types == [_SCALAR_TYPE]:
            if scalar_index == 0:
                # 生产实录的坏形状：单值回成裸 int
                return json.dumps({_SCALAR_TYPE: 12345}, ensure_ascii=False)
            # 补查（温度升轨、按行切块）恢复为列表
            return json.dumps({_SCALAR_TYPE: ["X号"]}, ensure_ascii=False)
        return json.dumps({t: [] for t in types}, ensure_ascii=False)

    client, calls = _make_client(monkeypatch, respond)
    result, ok = client.ner_checked(_TEXT_1990, _TYPES_20, context_tokens=4096)

    assert ok is True, "标量桶经补查恢复后必须 confirmed=True"
    assert result[_SCALAR_TYPE] == ["X号"], "补查恢复的值必须并入结果"
    for t in _TYPES_19:
        assert result[t] == [f"值-{t}"], "其余 19 批的值必须原样并入"
    # 原 2 次调用（19 批 + 标量批）+ ≥5 次补查块（1990 字按行界切 450）
    assert len(calls) >= 7, f"标量桶必须触发补查（实发 {len(calls)} 次调用）"
    # 完整结论才允许落全类型键（#108 C1/C2：部分并集不得写缓存）
    cached = client.get_cached_ner(_TEXT_1990, _TYPES_20)
    assert cached is not None, "补查恢复后的完整并集必须落缓存"
    assert cached.get(_SCALAR_TYPE) == ["X号"]


def test_scalar_bucket_unrecoverable_stays_unconfirmed_and_skips_cache(monkeypatch):
    """补查也回标量：confirmed=False、缓存键绝不落盘、19 批值仍尽力返回。

    这是「宁可失败可见，不可静默少扫」的护栏钉：部分并集（缺文书编号）
    不得被 confirmed=True 洗白进 TTL 缓存（#108 C1/C2 / #263 同族 fail-open）。
    """

    def respond(types, scalar_index, temperature):
        if types == _TYPES_19:
            return json.dumps({t: [f"值-{t}"] for t in types}, ensure_ascii=False)
        if types == [_SCALAR_TYPE]:
            return json.dumps({_SCALAR_TYPE: 12345}, ensure_ascii=False)  # 永远标量
        return json.dumps({t: [] for t in types}, ensure_ascii=False)

    client, calls = _make_client(monkeypatch, respond)
    result, ok = client.ner_checked(_TEXT_1990, _TYPES_20, context_tokens=4096)

    assert ok is False, "补查也无法恢复的标量桶必须保持 confirmed=False"
    assert _SCALAR_TYPE not in result
    assert result[_TYPES_19[0]] == [f"值-{_TYPES_19[0]}"], "未受影响批次的值仍尽力返回"
    assert len(calls) >= 7, "补查必须真的发起过（不可恢复 ≠ 不尝试）"
    # 全类型键与单类型键都不得落缓存
    assert client.get_cached_ner(_TEXT_1990, _TYPES_20) is None, (
        "缺类型的部分并集不得写缓存"
    )
    assert client.get_cached_ner(_TEXT_1990, [_SCALAR_TYPE]) is None


# ------------------------------------------------------------------ A1② 形状矩阵


@pytest.mark.parametrize(
    ("raw", "expect_result", "expect_missing"),
    [
        # 合法列表桶：逐字节保留（含重复值/混合类型项——净化不做值级变更）
        ({"a": ["x", "x"]}, {"a": ["x", "x"]}, []),
        ({"a": [1, None, ""]}, {"a": [1, None, ""]}, []),
        # 空列表桶：合法空答案，保留键
        ({"a": []}, {"a": []}, []),
        # 真值标量桶：剥除 + 按缺失上报（int / str / dict 各一条）
        ({"a": 5}, {}, ["a"]),
        ({"a": "文本"}, {}, ["a"]),
        ({"a": {"k": 1}}, {}, ["a"]),
        # 假值标量 ≈ 空答案：剥除但不算缺失、不补查（与 {"a": []} 同口径）
        ({"a": 0, "b": None, "c": "", "d": False}, {}, []),
        # 混合形状：合法桶保留、坏桶上报
        ({"a": ["x"], "b": 5, "c": [], "d": 0}, {"a": ["x"], "c": []}, ["b"]),
    ],
)
def test_sanitize_shape_matrix(raw, expect_result, expect_missing):
    clean, missing = HaSClient._sanitize_ner_buckets(raw)
    assert clean == expect_result
    assert missing == expect_missing


def test_sanitize_non_requested_scalar_not_requeried(monkeypatch):
    """模型答了请求类型之外的垃圾标量桶：剥除、不补查、confirmed 走 #108 I-4 口径。"""

    def respond(types, scalar_index, temperature):
        # 请求 [a, b]，模型只回了垃圾键的标量桶
        return json.dumps({"垃圾键": 5}, ensure_ascii=False)

    client, calls = _make_client(monkeypatch, respond)
    result, ok = client.ner_checked(_TEXT_1990, ["类型a", "类型b"], context_tokens=4096)
    # 原始答案非空却不含任何请求类型 ⇒ 契约违背 ⇒ ok=False（#108 ⑥ I-4，用原始结果判定）
    assert ok is False
    assert result == {}
    assert client.get_cached_ner(_TEXT_1990, ["类型a", "类型b"]) is None


def test_scalar_bucket_with_retry_disabled_returns_unconfirmed(monkeypatch):
    """禁补查层（_allow_truncation_retry=False，即补查的补查）遇标量：False + 不落缓存。"""

    def respond(types, scalar_index, temperature):
        return json.dumps({_SCALAR_TYPE: 12345}, ensure_ascii=False)

    client, calls = _make_client(monkeypatch, respond)
    result, ok = client.ner_checked(
        _TEXT_1990[:400],
        [_SCALAR_TYPE],
        context_tokens=4096,
        _allow_truncation_retry=False,
    )
    assert ok is False
    assert result == {}
    assert client.get_cached_ner(_TEXT_1990[:400], [_SCALAR_TYPE]) is None


def test_scalar_bucket_short_text_no_self_requery(monkeypatch):
    """短文本（补查块=原文同键）遇标量：不发起自连接补查，直接 confirmed=False。

    同键补查 = 加入自身 inflight 白等满 read 超时（#108 e2e 复现的挂起形态），
    宁可立即 False 走块失败，也不挂 120s。
    """

    def respond(types, scalar_index, temperature):
        return json.dumps({_SCALAR_TYPE: 12345}, ensure_ascii=False)

    client, calls = _make_client(monkeypatch, respond)
    short = "张三提交文书。"  # ≤600 → 补查块=原文 → resend_same_text=True
    result, ok = client.ner_checked(short, [_SCALAR_TYPE], context_tokens=4096)
    assert ok is False
    assert result == {}
    # 只有 1 次模型调用（无补查）
    assert len(calls) == 1


# ------------------------------------------------------------------ A2 块级 catch 可观测


class _BoomClient:
    """非标量异常桩：修复后标量桶不再走到 docline 块级 catch，A2 须独立触发。"""

    def ner_checked(self, text, types, *args, **kwargs):
        raise RuntimeError("boom-12345")


def test_docline_chunk_failure_logs_full_traceback(monkeypatch, caplog):
    """块级 catch 的 warning 必须带完整 traceback（Issue 验收 2）。

    反事实：2026-11-12 生产实录只有一行消息、无栈，定位花了整晚——
    本钉保证下次任何逃逸异常都自带行号。
    """
    from app.services.text_layer_docline_service import TextLayerPage, TextLayerWord

    tl_page = TextLayerPage(
        no=1,
        width=595,
        height=842,
        words=[
            TextLayerWord(text="张三", x0=72, y0=70, x1=100, y1=84),
            TextLayerWord(text="文书编号2022号", x0=110, y0=70, x1=200, y1=84),
        ],
    )
    with caplog.at_level(logging.WARNING, logger="app.services.text_layer_docline_service"):
        result = asyncio_run_docline(tl_page)

    assert result.failed_chunks >= 1, "桩异常必须以块失败记账（语义不变）"
    records = [r for r in caplog.records if "docline NER chunk failed" in r.getMessage()]
    assert records, "块失败 warning 必须落日志"
    rec = records[0]
    assert rec.exc_info is not None, "必须带 exc_info（完整 traceback）"
    assert "RuntimeError" in (rec.exc_text or ""), "traceback 必须含异常类型"
    assert "boom-12345" in (rec.exc_text or ""), "traceback 必须含异常消息"


def asyncio_run_docline(tl_page):
    import asyncio

    async def _run():
        return await run_document_ner(
            [tl_page],
            [_SCALAR_TYPE],
            has_client=_BoomClient(),
            residual_pass=False,
            concurrency=1,
        )

    return asyncio.run(_run())
