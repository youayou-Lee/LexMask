"""Issue#209 成本结构修复③：截断补查的**成本轴**——默认切文本，超预算才切类型。

门④两轮实测（真实第四卷 133 页，2026-10-29）：
- 一轮：流水线发「4 类型 / ≈860 帽」的补查块，12 段反复 `finish_reason=length`
  `confirmed=False`（补查自己又被截断）；隔离态「2 类型 / 640 帽」一次收敛。
- 二轮（本文件的定稿依据）：把整段 3924 字符 × 2 类型单请求发出去 ⇒ 生成
  **72.6s**（模型要把整页重读一遍再枚举该类型全部值），比 450 字符块慢一个
  数量级 —— 「切类型」不是省时的轴，**切文本**才是。

因此最终策略（与 #103/#104 的容量算术同源）：
1. **默认始终按行边界切文本块**（`HAS_NER_REQUERY_SINGLE_PASS_MAX_CHARS=0`）；
2. 类型轴只在「整批真装不下完成帽」时才切（`HAS_NER_REQUERY_MAX_TYPES_PER_CALL`
   作上限）；装得下就单批，不做无谓切分；
3. 补查一律 `_allow_truncation_retry=False` ⇒ 绝不二次补查（封顶语义不变）。
"""

from __future__ import annotations

import json
import math

import pytest

from app.core.config import settings
from app.services.has_client import HaSClient

_TYPES = [f"类型{i}" for i in range(6)]
# 长到会被行边界切块的文本（2409 字符）
_LONG = "\n".join(f"第{i}行内容 99112233{i:04d}" for i in range(120))


def _prompt_text(prompt: str) -> str:
    return prompt[prompt.rindex("<text>") + len("<text>") : prompt.rindex("</text>")]


def _prompt_types(prompt: str) -> list[str]:
    line = next(line for line in prompt.splitlines() if line.startswith("Specified types:"))
    return json.loads(line.split("Specified types:", 1)[1])


class _TruncatingOnce(HaSClient):
    """主查只回第一个类型 + 半截值（触发 json_repair 截断恢复链），后续补查干净
    作答。``calls[0]`` 是主查，之后都是补查。"""

    def __init__(self):
        super().__init__()
        self.calls: list[dict] = []

    def _effective_context_tokens(self) -> int:
        return 8192

    def _call_model(self, messages, *, max_tokens=None, temperature=None):
        prompt = messages[0]["content"]
        text = _prompt_text(prompt)
        types = _prompt_types(prompt)
        self.calls.append(
            {"text": text, "types": list(types), "max_tokens": max_tokens, "temperature": temperature}
        )
        if not self.calls[1:]:  # 第一次调用 = 主查
            return '{"' + types[0] + '":["值-' + types[0] + '","截断残'
        return json.dumps({t: [f"值-{t}"] for t in types}, ensure_ascii=False)


@pytest.fixture(autouse=True)
def _clean_cache():
    HaSClient._SHARED_NER_CACHE.clear()
    yield
    HaSClient._SHARED_NER_CACHE.clear()


def test_requery_chunks_text_and_keeps_all_missing_types_together():
    """默认：按文本块切、缺失类型**整批**问（不切类型轴）。"""
    client = _TruncatingOnce()
    client.ner_checked(_LONG, _TYPES, None, context_tokens=8192)

    main, requery = client.calls[0], client.calls[1:]
    assert requery, "用例前提：必须触发补查"
    chunks = client._split_requery_chunks(_LONG)
    assert len(chunks) > 1, "用例前提：该文本会被切块"
    assert len(requery) == len(chunks), f"每块 1 次，实际 {len(requery)} 次"
    # 门⑥ F2：假模型的主查只回了 ``类型0`` 且尾值被切断 ⇒ 该桶既"存在"又被
    # 切断，按 C2 必须并入补查集合（长文本恒切块 ⇒ 无自连接重发风险）⇒ 缺失集
    # 合是**全部**类型，而不是「除类型0 以外」。
    missing = sorted(_TYPES)
    for call in requery:
        assert sorted(call["types"]) == missing, (
            f"默认应整批问缺失类型 {missing}，实际 {sorted(call['types'])}"
        )
        assert len(call["text"]) < len(_LONG), "必须按行切块（整段单次实测慢 10 倍）"


def test_requery_type_batching_only_when_budget_truly_exceeds_cap(monkeypatch):
    """整批装不下完成帽时才沿类型轴切（容量算术，不是固定 2）。"""
    monkeypatch.setattr(settings, "HAS_NER_COMPLETION_HARD_CAP", 1024, raising=False)
    monkeypatch.setattr(settings, "HAS_NER_REQUERY_MAX_TYPES_PER_CALL", 2, raising=False)
    client = _TruncatingOnce()
    client.ner_checked(_LONG, _TYPES, None, context_tokens=8192)

    main, requery = client.calls[0], client.calls[1:]
    missing = sorted(_TYPES)  # 同 F2：被切断的 ``类型0`` 也进补查集合
    per_call = settings.HAS_NER_REQUERY_MAX_TYPES_PER_CALL
    assert all(len(c["types"]) <= per_call for c in requery), (
        f"超预算时必须按类型切小：{[c['types'] for c in requery]}"
    )
    # 类型并集不重不漏
    covered = sorted({t for c in requery for t in c["types"]})
    assert covered == missing, f"补查类型并集 != 缺失集合：{covered}"
    # 每块 × 每个类型批
    chunks = client._split_requery_chunks(_LONG)
    assert len(requery) == len(chunks) * math.ceil(len(missing) / per_call)


def test_requery_never_recurses(monkeypatch):
    """补查自身再截断也不二次补查（_allow_truncation_retry=False 语义不变）。"""

    class _AlwaysTruncating(_TruncatingOnce):
        def _call_model(self, messages, *, max_tokens=None, temperature=None):
            prompt = messages[0]["content"]
            types = _prompt_types(prompt)
            self.calls.append(
                {
                    "text": _prompt_text(prompt),
                    "types": list(types),
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                }
            )
            return '{"' + types[0] + '":["值-' + types[0] + '","截断残'

    client = _AlwaysTruncating()
    client.ner_checked(_LONG, _TYPES, None, context_tokens=8192)
    chunks = client._split_requery_chunks(_LONG)
    # 一轮 = 主查 + 每块 1 次；若发生二层补查，数量会按块数翻倍
    assert len(client.calls) - 1 <= len(chunks), (
        f"补查不得递归：{len(client.calls) - 1} 个补查请求 / {len(chunks)} 块"
    )