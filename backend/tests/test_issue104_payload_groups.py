"""Issue#104 —— HaS token 估算低估 3.2× 致 400 → 主查静默空结果。

验收方案 v5（私有仓 docs/acceptance/2026-10-15-issue104-payload-context验收方案.md）
的本地门③判据落点：

- D1①-⑩  分组规划纯函数语义（预算+1 拆组 / 单块硬切 / 空输入 / 单组守恒 /
          硬切不丢字符 / 跨组去重 / 硬切不切真值 / 尾组值可回映 / partition
          性质 / 极小 context 显式报错）
- D2      实发预算口径（8192 与 4096 各一遍；planned 与 ner() 实算同源同值；
          打桩 4096 组数必须变多；residual 逐组满足装填上限、bridge 恒单组）
- D-fb    本地半：探测失败矩阵 → 回落 min(配置, 4096) + WARNING + 不拒服；
          探测纪律（N 次调用探测 1 次，缓存生效）
- D-cm    组级失败：任一组失败 ⇒ 页判失败；重复调用仍失败；成功组缓存不得
          让半结果按「成功页」返回
- C5      不再静默：HTTP 400 ⇒ NER_ERRORS{error_type=http_400} 计数 + 日志带
          估算/长度
- C6      单组路径与现状逐字等价（同 payload、同调用数、同 max_tokens）
"""

from __future__ import annotations

import json
import logging
import re

import httpx
import pytest

from app.core.config import settings
from app.services.has_client import HaSClient
from app.services.vision.has_text_payload import (
    _build_has_text_payload,
    plan_has_text_groups,
)

_TYPES_12 = [f"类型{i:02d}" for i in range(12)]


def _estimate_for(types: list[str]):
    def _estimate(group_text: str) -> int:
        return HaSClient._estimate_prompt_tokens(HaSClient._build_ner_prompt(types, "", group_text))

    return _estimate


def _max_tokens_for(types: list[str], context_tokens: int):
    def _max_tokens(group_text: str) -> int:
        return HaSClient.planned_max_tokens(types, group_text, context_tokens=context_tokens)

    return _max_tokens


def _dense_digits(n: int) -> str:
    # 数字/标点密集（编号、账号、日期串形态）——历史 400 的触发形态。
    return ("370702199001014521," * (n // 19 + 2))[:n]


def _filler_digits(n: int) -> str:
    # 与植入真值**不同形**的密集填充（用于「真值没有被切」类断言，避免真值满页皆是）。
    return ("991122334455667788," * (n // 19 + 2))[:n]


def make_client(responses=None):
    client = HaSClient()
    calls = []

    def fake_call_model(messages, *, max_tokens=None, temperature=None):
        calls.append({"content": messages[0]["content"], "max_tokens": max_tokens})
        if responses:
            return responses[min(len(calls) - 1, len(responses) - 1)]
        return "{}"

    client._call_model = fake_call_model
    return client, calls


# ---------------------------------------------------------------------------
# D1 分组规划纯函数
# ---------------------------------------------------------------------------

def _fill(context_tokens: int) -> int:
    return context_tokens - 160 - 64


def test_d1_1_budget_plus_one_makes_two_groups():
    # 用线性可控的桩闭包直接钉「预算+1」的装填语义：estimate=字符数、
    # completion=常数。真实算术的耦合由 D2 系列覆盖。
    blocks = ["a" * 50, "b" * 50]
    # 双块足迹 = 101(含换行) + 10 = 111；单块足迹 = 60。fill=110 ⇒ 单块装得下、
    # 双块恰好装不下（预算-1 → 溢出）。
    context_tokens = 160 + 64 + 10 + 101 - 1
    groups = plan_has_text_groups(
        blocks,
        context_tokens=context_tokens,
        reserve_tokens=160,
        estimate_for=len,
        max_tokens_for=lambda text: 10,
    )
    assert len(groups) == 2
    fill = context_tokens - 160 - 64
    for group in groups:
        joined = "\n".join(group)
        assert len(joined) + 10 <= fill


def test_d1_2_single_oversized_block_is_hard_split():
    block = _dense_digits(20000)
    estimate = _estimate_for(_TYPES_12)
    context = 8192
    groups = plan_has_text_groups(
        [block],
        context_tokens=context,
        reserve_tokens=160,
        estimate_for=estimate,
        max_tokens_for=_max_tokens_for(_TYPES_12, context),
    )
    assert len(groups) > 1
    fill = _fill(context)
    for group in groups:
        joined = "\n".join(group)
        assert estimate(joined) + HaSClient.planned_max_tokens(_TYPES_12, joined, context_tokens=context) <= fill


def test_d1_3_empty_and_whitespace_yield_no_groups():
    assert (
        plan_has_text_groups(
            [],
            context_tokens=8192,
            reserve_tokens=160,
            estimate_for=_estimate_for(_TYPES_12),
            max_tokens_for=_max_tokens_for(_TYPES_12, 8192),
        )
        == []
    )
    assert (
        plan_has_text_groups(
            ["", "  ", "\n\t"],
            context_tokens=8192,
            reserve_tokens=160,
            estimate_for=_estimate_for(_TYPES_12),
            max_tokens_for=_max_tokens_for(_TYPES_12, 8192),
        )
        == []
    )


def test_d1_4_single_group_conserves_input_verbatim():
    blocks = ["张三，身份证 370702199001014521", "电话 0536-1234567", "银行卡 6222020200112233445"]
    groups = plan_has_text_groups(
        blocks,
        context_tokens=8192,
        reserve_tokens=160,
        estimate_for=_estimate_for(_TYPES_12),
        max_tokens_for=_max_tokens_for(_TYPES_12, 8192),
    )
    assert len(groups) == 1
    assert "\n".join(groups[0]) == "\n".join(blocks)


def test_d1_5_hard_split_loses_no_characters():
    block = _dense_digits(20000)
    groups = plan_has_text_groups(
        [block],
        context_tokens=8192,
        reserve_tokens=160,
        estimate_for=_estimate_for(_TYPES_12),
        max_tokens_for=_max_tokens_for(_TYPES_12, 8192),
    )
    pieces = [piece for group in groups for piece in group]
    assert "".join(pieces) == block


def test_d1_7_hard_split_never_cuts_inside_a_planted_value():
    # 植入 18 位身份证（真值）：切点不得落在其内部。
    # 填充值必须与真值**形态不同**（`_dense_digits` 本身就是身份证串的重复），
    # 否则「植入值」满页皆是、断言失去判别力（冷审 I3：原断言 `a not in p or
    # a in p` 对任意字符串恒真）。
    id_card = "370702199001014521"
    # 真值刻意**跨在中点**上：若切点退化为「中点对半」（冷审 M2 变异形态），
    # 中点必然落在真值内部 ⇒ 该判据必须变红；而正常实现会退到真值左侧的逗号
    # 边界（切点 == 真值起点，允许），故本用例对新旧切法有判别力。
    filler = _filler_digits(9001)
    assert id_card not in filler
    block = filler + "," + id_card + "," + _filler_digits(8999)
    value_start = block.index(id_card)
    assert value_start < len(block) // 2 < value_start + len(id_card)
    groups = plan_has_text_groups(
        [block],
        context_tokens=8192,
        reserve_tokens=160,
        estimate_for=_estimate_for(_TYPES_12),
        max_tokens_for=_max_tokens_for(_TYPES_12, 8192),
    )
    assert len(groups) > 1
    pieces = [piece for group in groups for piece in group]
    assert "".join(pieces) == block  # 不丢
    assert id_card in "".join(pieces)
    _assert_no_boundary_inside(block, pieces, id_card)


def test_d1_7b_midpoint_cut_is_caught_by_the_same_guard():
    """判别力自查：把切点钉在中点（冷审 M2 变异形态）必须被同一判据抓到。"""
    id_card = "370702199001014521"
    block = _filler_digits(300) + "," + id_card + "," + _filler_digits(300)
    value_start = block.index(id_card)
    midpoint = len(block) // 2
    assert value_start < midpoint < value_start + len(id_card)  # 真值恰好跨中点
    with pytest.raises(AssertionError):
        _assert_no_boundary_inside(block, [block[:midpoint], block[midpoint:]], id_card)


def _assert_no_boundary_inside(block: str, pieces: list[str], value: str) -> None:
    """切片边界不得落在 ``value`` 的字符区间内部（非恒真，可被变异触发）。

    注意判据方向：切片**包含**整个真值是允许的（此时边界在区间之外），
    只有边界落在 ``(start, end)`` 开区间内部才算把真值拦腰切断。
    """
    start = block.index(value)
    end = start + len(value)
    offset = 0
    for piece in pieces:
        piece_start = offset
        offset += len(piece)
        assert not (start < piece_start < end), f"切片起始边界落在真值内部: {piece_start}"
        assert not (start < offset < end), f"切片结束边界落在真值内部: {offset}"
        if piece_start <= start and end <= offset:
            assert value in piece  # 覆盖该区间的切片必须完整包含真值


def test_d1_9_partition_property_groups_cover_input_in_order():
    blocks = [_dense_digits(900) for _ in range(12)]
    groups = plan_has_text_groups(
        blocks,
        context_tokens=8192,
        reserve_tokens=160,
        estimate_for=_estimate_for(_TYPES_12),
        max_tokens_for=_max_tokens_for(_TYPES_12, 8192),
    )
    # 各组按序覆盖全部输入块、不重不漏：摊平后必须仍是原块序列的顺序拼接。
    flattened = [piece for group in groups for piece in group]
    assert "".join(flattened) == "".join(blocks)
    # 块级边界（无硬切时）：每组内部的块都必须来自输入、且全局顺序单调。
    iterator = iter(blocks)
    for group in groups:
        for piece in group:
            if len(groups) == 1:
                continue
            matched = next((b for b in iterator if b == piece), None)
            assert matched == piece


def test_d1_10_tiny_context_reports_instead_of_hanging():
    with pytest.raises(ValueError):
        plan_has_text_groups(
            [_dense_digits(500)],
            context_tokens=512,
            reserve_tokens=160,
            estimate_for=_estimate_for(_TYPES_12),
            max_tokens_for=_max_tokens_for(_TYPES_12, 512),
        )


def test_d1_6_cross_group_merge_dedupes_type_value_pairs():
    from app.services.vision.has_text_analysis import merge_group_ner_results

    merged = merge_group_ner_results(
        [
            {"身份证": ["370702199001014521"], "电话": ["0536-1234567"]},
            {"身份证": ["370702199001014521", "370781198802023311"], "电话": []},
        ]
    )
    assert merged["身份证"] == ["370702199001014521", "370781198802023311"]
    assert merged["电话"] == ["0536-1234567"]


# ---------------------------------------------------------------------------
# D2 实发预算口径（8192 与 4096 各一遍；与 ner() 实算同源）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("context_tokens", [8192, 4096])
def test_d2_groups_fill_limit_matches_planned_budget(context_tokens):
    blocks = ["张三，身份证 370702199001014521，电话 13800138000"] * 40
    payload = _build_has_text_payload(
        [type("B", (), {"text": t, "polygon": [], "confidence": 1.0})() for t in blocks],
        max_chars=32000,
    )
    estimate = _estimate_for(_TYPES_12)
    groups = plan_has_text_groups(
        payload.texts,
        context_tokens=context_tokens,
        reserve_tokens=160,
        estimate_for=estimate,
        max_tokens_for=_max_tokens_for(_TYPES_12, context_tokens),
    )
    assert groups
    fill = context_tokens - 160 - 64
    for group in groups:
        joined = "\n".join(group)
        used = estimate(joined) + HaSClient.planned_max_tokens(_TYPES_12, joined, context_tokens=context_tokens)
        assert used <= fill


def test_d2_planned_equals_ner_actual_max_tokens():
    client, calls = make_client(responses=["{}"])
    text = "张三，身份证 370702199001014521，电话 13800138000，银行卡 6222020200112233445"
    client.ner(text, _TYPES_12)
    assert len(calls) == 1
    actual = calls[0]["max_tokens"]
    planned = HaSClient.planned_max_tokens(_TYPES_12, text, context_tokens=8192)
    assert actual == planned


def test_i1_planned_max_tokens_default_context_uses_configured_value():
    """冷审 I1：`context_tokens` 缺省时必须取配置值而不是 NameError。

    CI 的 ruff 曾抓出该分支引用未导入的 `settings`（F821）——本用例把该缺省
    路径纳入回归（缺省值应与显式传 `HAS_NER_CONTEXT_TOKENS` 完全一致）。
    """
    text = _dense_digits(400)
    default_planned = HaSClient.planned_max_tokens(["身份证号"], text)
    explicit_planned = HaSClient.planned_max_tokens(
        ["身份证号"], text, context_tokens=settings.HAS_NER_CONTEXT_TOKENS
    )
    assert default_planned == explicit_planned > 0


@pytest.mark.parametrize("context_tokens", [4096, 8192, 32768])
@pytest.mark.parametrize("n_types", [1, 6, 12])
def test_d2_every_actual_request_fits_the_fill_budget(context_tokens, n_types):
    """D2（冷审 I1 改写为「观测实发请求」口径）。

    逐条检查**真正发出去的**每个请求：`estimate(实发 prompt) + 实发
    max_tokens <= context - reserve - 64`。类型分批（一个组内可能发多个请求）
    也必须逐条满足——这比「planned == actual 单点相等」更强：单点相等只在
    未分批时成立（分批后每批的 desired 预算更小），而装填上限对每条请求都
    必须成立。
    """
    types = _TYPES_12[:n_types]
    corpus = {
        "dense_digits": _dense_digits(1200),
        "chinese_prose": "甲方李四，电话13800138000，住址北京市朝阳区建国路88号。" * 20,
        "latin_mixed": "Contract No. 2026-1016, Party A Li Si, Tel 13800138000. " * 30,
    }
    fill = context_tokens - 160 - 64
    for label, text in corpus.items():
        # 类级共享缓存会跨用例命中（键不含上下文），命中就一条请求都不发——
        # 对「观测实发请求」的断言必须先清缓存。
        HaSClient._SHARED_NER_CACHE.clear()
        client, calls = make_client(responses=["{}"])
        client._effective_context_tokens = lambda ctx=context_tokens: ctx
        client.ner(text, types)
        assert calls, f"{label}: 未发出任何请求（缓存命中会掩盖实发口径）"
        for call in calls:
            estimate = HaSClient._estimate_prompt_tokens(call["content"])
            assert estimate + call["max_tokens"] <= fill, (
                f"{label}: ctx={context_tokens} types={n_types} "
                f"est={estimate} + max_tokens={call['max_tokens']} > fill={fill}"
            )


def _types_in_prompt(content: str) -> list[str]:
    match = re.search(r"Specified types:(\[.*?\])\n", content)
    assert match, f"实发 prompt 里没有类型清单: {content[:200]!r}"
    return json.loads(match.group(1))


def test_i1_pinned_context_propagates_to_every_request():
    """冷审 I1：调用方钉住的上下文必须贯穿**每个**实发请求（含分批递归）。

    旧实现里 ner() 自行 `_effective_context_tokens()`——探测缓存 TTL 到期后，
    规划用的上下文与实发用的上下文会漂移，装填上限失效。这里把探测值钉成
    一个刻意不同的 32768：若任何一层递归漏传钉住值，它就会按 32768 算预算，
    与「按 4096 重算」不符。
    """
    client, calls = make_client(responses=["{}"])
    probes: list[int] = []

    def _probe() -> int:
        probes.append(1)
        return 32768

    client._effective_context_tokens = _probe
    text = _dense_digits(400)
    client.ner(text, _TYPES_12, context_tokens=4096)
    assert probes == [], "钉住上下文时不得再探测（规划/实发同源）"
    assert calls, "应有实发请求"
    for call in calls:
        batch = _types_in_prompt(call["content"])
        expected = HaSClient._resolve_ner_budget(batch, text, context_tokens=4096)[2]
        assert call["max_tokens"] == expected, (
            f"类型批 {batch} 实发 {call['max_tokens']} != 按钉住上下文 4096 重算 {expected}"
        )


def test_d2_stubbing_smaller_context_increases_group_count():
    blocks = [_dense_digits(400) for _ in range(20)]
    # 去重会合并相同块文本 —— 让每块唯一。
    blocks = [f"块{i}，{_dense_digits(400)}" for i in range(20)]
    estimate = _estimate_for(_TYPES_12)
    kwargs = dict(reserve_tokens=160, estimate_for=estimate)
    wide = plan_has_text_groups(blocks, context_tokens=8192, max_tokens_for=_max_tokens_for(_TYPES_12, 8192), **kwargs)
    narrow = plan_has_text_groups(blocks, context_tokens=4096, max_tokens_for=_max_tokens_for(_TYPES_12, 4096), **kwargs)
    assert len(narrow) > len(wide) >= 1


def test_d2_residual_groups_fit_but_bridge_stays_single():
    # residual 与主查同一条规划路径；bridge(1200 字符上限)永远单组。
    residual_blocks = ["未解释墨迹 " + _dense_digits(3000)]
    payload = _build_has_text_payload(
        [type("B", (), {"text": t, "polygon": [], "confidence": 1.0})() for t in residual_blocks],
        max_chars=32000,
    )
    groups = plan_has_text_groups(
        payload.texts,
        context_tokens=8192,
        reserve_tokens=160,
        estimate_for=_estimate_for(_TYPES_12),
        max_tokens_for=_max_tokens_for(_TYPES_12, 8192),
    )
    assert groups
    fill = _fill(8192)
    for group in groups:
        joined = "\n".join(group)
        assert len(joined) <= 32000
        assert (
            _estimate_for(_TYPES_12)(joined)
            + HaSClient.planned_max_tokens(_TYPES_12, joined, context_tokens=8192)
            <= fill
        )
    # bridge：1200 字符上限的 payload 在 8192/4096 链都必须单组。
    bridge_payload = _build_has_text_payload(
        [type("B", (), {"text": "甲" * 1000, "polygon": [], "confidence": 1.0})()],
        max_chars=1200,
    )
    for context in (8192, 4096):
        bridge_groups = plan_has_text_groups(
            bridge_payload.texts,
            context_tokens=context,
            reserve_tokens=160,
            estimate_for=_estimate_for(_TYPES_12),
            max_tokens_for=_max_tokens_for(_TYPES_12, context),
        )
        assert len(bridge_groups) == 1


# ---------------------------------------------------------------------------
# 估算器回归（本地已知真值点；云端 P1/D2b 用真实 usage 再钉）
# ---------------------------------------------------------------------------

def test_estimator_never_underestimates_known_payloads():
    """已知真值锚点(云 D2b 回归表,2026-10-15/16 SCNet 实测)。"""
    estimate = HaSClient._estimate_prompt_tokens
    # 历史 400 现场：4,455 字符 ASCII/数字密集 → 服务端 prompt_tokens=4090。
    dense = _dense_digits(4455)
    assert estimate(HaSClient._build_ner_prompt(_TYPES_12, "", dense)) >= 4090
    # 纯中文同长度：服务端 prompt_tokens=3073（0.69 token/字符）。
    chinese = "张" * 4455
    est = estimate(HaSClient._build_ner_prompt(_TYPES_12, "", chinese))
    assert est >= 3073
    assert est <= 3073 * 1.6


def test_estimator_astral_and_latin_classes():
    """D2b 首轮回归抓到的两类低估,系数上调后的锚点:
    拉丁交替大小写 2000 字符 actual=1494(0.75/字符);emoji/CJK 扩展 B ≈ 3~4 token/字符。"""
    estimate = HaSClient._estimate_prompt_tokens
    latin = ("AbCdEfGhIj " * 200)[:2000]
    assert estimate(HaSClient._build_ner_prompt(_TYPES_12, "", latin)) >= 1494
    emoji = "😀😁😂🤣😃😄😅😆" * 25  # 200 个星平面字符,云实测 actual=821
    assert estimate(HaSClient._build_ner_prompt(_TYPES_12, "", emoji)) >= 821
    extb = "𠀀𠀁𠀂𠀃𠀄𠀅𠀆𠀇" * 25
    assert estimate(HaSClient._build_ner_prompt(_TYPES_12, "", extb)) >= 821
    # 长数字串(云实测 6000 字符 actual=6117,1.0 系数差 0.5%)
    assert estimate(HaSClient._build_ner_prompt(_TYPES_12, "", "1234567890" * 600)) >= 6117


# ---------------------------------------------------------------------------
# D-fb 本地半：上下文探测失败矩阵 + 探测纪律
# ---------------------------------------------------------------------------

class _ProbeClient(HaSClient):
    def __init__(self, probe_results):
        super().__init__()
        self._probe_results = list(probe_results)
        self.probe_calls = 0

    def _probe_server_context(self, base_url: str):
        self.probe_calls += 1
        result = self._probe_results[min(self.probe_calls - 1, len(self._probe_results) - 1)]
        if isinstance(result, Exception):
            return None  # 与真实现契约一致：任何失败 → None（由调用方回落）
        return result


@pytest.fixture(autouse=True)
def _clear_probe_cache():
    HaSClient._context_probe_cache.clear()
    yield
    HaSClient._context_probe_cache.clear()


def test_dfb_probe_success_is_capped_by_config():
    client = _ProbeClient([8192])
    assert client._effective_context_tokens() == 8192
    client._probe_results = [99999]
    assert client._effective_context_tokens() == min(8192, settings.HAS_NER_CONTEXT_TOKENS)


def test_dfb_probe_failure_falls_back_to_safe_small_value(monkeypatch, caplog):
    settings.HAS_NER_CONTEXT_FALLBACK_TOKENS = 4096
    settings.HAS_NER_CONTEXT_TOKENS = 8192
    client = _ProbeClient([httpx.ConnectError("boom")])
    with caplog.at_level(logging.WARNING, logger="app.services.has_client"):
        effective = client._effective_context_tokens()
    assert effective == 4096
    assert any("4096" in record.message for record in caplog.records)


@pytest.mark.parametrize("bad", [None, 0, -5, "not-a-number"])
def test_dfb_probe_invalid_values_fall_back(bad, monkeypatch):
    settings.HAS_NER_CONTEXT_FALLBACK_TOKENS = 4096
    client = _ProbeClient([bad])
    assert client._effective_context_tokens() == 4096


def test_dfb_probe_called_once_across_many_calls():
    client = _ProbeClient([8192])
    for _ in range(10):
        assert client._effective_context_tokens() == 8192
    assert client.probe_calls == 1


def test_dfb_probe_success_below_fallback_is_respected():
    settings.HAS_NER_CONTEXT_FALLBACK_TOKENS = 4096
    client = _ProbeClient([2048])
    assert client._effective_context_tokens() == 2048


def test_dfb_probe_extract_max_model_len_from_models_payload(monkeypatch):
    """_probe_server_context 解析 /v1/models 载荷并按 HAS_TEXT_MODEL_NAME 选条目。"""
    client = HaSClient()

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "data": [
                    {"id": "other-model", "max_model_len": 100},
                    {"id": settings.HAS_TEXT_MODEL_NAME or "HaS_4.0_0.6B", "max_model_len": 8192},
                ]
            }

    monkeypatch.setattr(
        client, "_http_client", type("C", (), {"get": lambda self, url, timeout=None: _Resp()})()
    )
    monkeypatch.setattr(settings, "HAS_TEXT_MODEL_NAME", "HaS_4.0_0.6B", raising=False)
    assert client._probe_server_context("http://127.0.0.1:8080/v1") == 8192


# ---------------------------------------------------------------------------
# C5 不再静默 + raise_on_error
# ---------------------------------------------------------------------------

def test_c5_http_400_increments_ner_errors_counter():
    from app.core.metrics import NER_ERRORS

    client, _ = make_client()
    before = NER_ERRORS.labels(error_type="http_400")._value.get()

    def raise_400(messages, *, max_tokens=None, temperature=None):
        resp = httpx.Response(400, request=httpx.Request("POST", "http://test/v1/chat/completions"))
        raise httpx.HTTPStatusError("400", request=resp.request, response=resp)

    client._call_model = raise_400
    assert client.ner("张三身份证 370702199001014521", _TYPES_12) == {}
    after = NER_ERRORS.labels(error_type="http_400")._value.get()
    assert after == before + 1


def test_c5_raise_on_error_propagates_after_counting():
    from app.core.metrics import NER_ERRORS

    client, _ = make_client()
    before = NER_ERRORS.labels(error_type="http_400")._value.get()

    def raise_400(messages, *, max_tokens=None, temperature=None):
        resp = httpx.Response(400, request=httpx.Request("POST", "http://test/v1/chat/completions"))
        raise httpx.HTTPStatusError("400", request=resp.request, response=resp)

    client._call_model = raise_400
    with pytest.raises(httpx.HTTPStatusError):
        client.ner("张三身份证 370702199001014521", _TYPES_12, raise_on_error=True)
    assert NER_ERRORS.labels(error_type="http_400")._value.get() == before + 1


# ---------------------------------------------------------------------------
# C6 单组路径与现状逐字等价
# ---------------------------------------------------------------------------

def test_c6_small_page_stays_single_group_with_identical_payload():
    blocks = [f"张三，身份证 37070219900101452{i}，电话 1380013800{i}" for i in range(5)]
    payload = _build_has_text_payload(
        [type("B", (), {"text": t, "polygon": [], "confidence": 1.0})() for t in blocks],
        max_chars=32000,
    )
    groups = plan_has_text_groups(
        payload.texts,
        context_tokens=8192,
        reserve_tokens=160,
        estimate_for=_estimate_for(_TYPES_12),
        max_tokens_for=_max_tokens_for(_TYPES_12, 8192),
    )
    assert len(groups) == 1
    assert "\n".join(groups[0]) == payload.content


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

def test_config_fallback_and_group_sem_defaults():
    assert settings.HAS_NER_CONTEXT_FALLBACK_TOKENS == 4096
    assert settings.HAS_VISION_MAX_GROUPS_INFLIGHT == 4


def test_config_fallback_clamped():
    from app.core.config import Settings

    s = Settings(HAS_NER_CONTEXT_FALLBACK_TOKENS=1)
    assert s.HAS_NER_CONTEXT_FALLBACK_TOKENS == 512
    s2 = Settings(HAS_NER_CONTEXT_FALLBACK_TOKENS=99999)
    assert s2.HAS_NER_CONTEXT_FALLBACK_TOKENS == 8192
