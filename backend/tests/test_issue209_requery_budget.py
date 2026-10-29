"""Issue#209 成本结构修复③：截断补查沿**类型轴**切小（每请求 ≤2 类型）。

门④实测（真实第四卷 133 页，2026-10-29）：
- 隔离态「2 类型 / 640 帽」补查块**一次收敛**（finish_reason=stop recovered=2/2）；
- 流水线实际发的是「4 类型 / ≈860 帽」块，12 段里反复 `finish_reason=length`
  `confirmed=False` —— 补查自己又被截断，于是 12 段补查烧掉 100s+。

又因类型数已受限，补查不再需要按行切文本（长文本本身不是瓶颈，输出规模由
类型数决定）：`HAS_NER_REQUERY_SINGLE_PASS_MAX_CHARS` 以内整段单次。
"""

from __future__ import annotations

import json

import pytest

from app.core.config import settings
from app.services.has_client import HaSClient

_TYPES = [f"类型{i}" for i in range(6)]
# 长到会被行边界切块的文本（> _REQUERY_CHUNK_THRESHOLD=600）
_LONG = "\n".join(f"第{i}行内容 99112233{i:04d}" for i in range(120))


def _prompt_text(prompt: str) -> str:
    return prompt[prompt.rindex("<text>") + len("<text>") : prompt.rindex("</text>")]


def _prompt_types(prompt: str) -> list[str]:
    line = next(line for line in prompt.splitlines() if line.startswith("Specified types:"))
    return json.loads(line.split("Specified types:", 1)[1])


class _TruncatingOnce(HaSClient):
    """首次作答只回第一个类型 + 半截值（触发 json_repair 截断恢复链），
    后续补查干净作答。"""

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
        if len(types) > 2:
            # 损坏 JSON：只落第一个类型 + 半截值 ⇒ 其余类型成 missing
            return '{"' + types[0] + '":["值-' + types[0] + '","截断残'
        return json.dumps({t: [f"值-{t}"] for t in types}, ensure_ascii=False)


@pytest.fixture(autouse=True)
def _clean_cache():
    HaSClient._SHARED_NER_CACHE.clear()
    yield
    HaSClient._SHARED_NER_CACHE.clear()


def test_requery_splits_by_type_not_by_text():
    """缺 4 类型 + 长文本 ⇒ 补查按类型切成 2 批、且每批都拿**整段**文本。"""
    client = _TruncatingOnce()
    result, _confirmed = client.ner_checked(_LONG, _TYPES, None, context_tokens=8192)

    requery_calls = [c for c in client.calls if len(c["types"]) <= 2]
    main_calls = [c for c in client.calls if len(c["types"]) > 2]
    assert len(main_calls) == 1, f"主查应只 1 次，实际 {len(main_calls)}"
    assert requery_calls, "用例前提：必须触发补查"
    assert all(len(c["types"]) <= 2 for c in requery_calls), (
        f"补查必须按类型切小：{[c['types'] for c in requery_calls]}"
    )
    # 缺失 = 除首类型外的全部（5 个）⇒ ceil(5/2)=3 个类型批；类型数受限后
    # 不再按行切文本 ⇒ 每批 1 次
    assert len(requery_calls) == 3, (
        f"期望 ceil(5/2)=3 次整段补查，实际 {len(requery_calls)}"
    )
    assert all(c["text"] == _LONG for c in requery_calls), "整段单次补查：文本不得被切块"
    assert result["类型0"]  # 主查已落桶


def test_requery_groups_types_and_covers_all_missing(monkeypatch):
    """缺失类型必须**全部**进入某个补查批（不重不漏）。"""
    monkeypatch.setattr(settings, "HAS_NER_REQUERY_MAX_TYPES_PER_CALL", 2, raising=False)
    client = _TruncatingOnce()
    client.ner_checked(_LONG, _TYPES, None, context_tokens=8192)
    requery_types = [t for c in client.calls if len(c["types"]) <= 2 for t in c["types"]]
    assert sorted(requery_types) == sorted(_TYPES[1:]), (
        f"补查类型并集 != 缺失集合：{sorted(requery_types)}"
    )


def test_requery_still_chunks_when_text_exceeds_single_pass_cap(monkeypatch):
    """文本超过整段上限时仍回退行边界切块（防超上下文 / 长程复读）。"""
    monkeypatch.setattr(settings, "HAS_NER_REQUERY_SINGLE_PASS_MAX_CHARS", 300, raising=False)
    client = _TruncatingOnce()
    client.ner_checked(_LONG, _TYPES, None, context_tokens=8192)
    requery_calls = [c for c in client.calls if len(c["types"]) <= 2]
    # 3 个类型批 × N 个文本块（N 由行边界切块决定，此处只钉"逐块都不含整段"）
    assert len(requery_calls) % 3 == 0 and len(requery_calls) > 3, (
        f"期望 3 类型批 × N>1 文本块，实际 {len(requery_calls)}"
    )
    assert all(len(c["text"]) < len(_LONG) for c in requery_calls), "必须按行切块"
    # 单块约为 _REQUERY_CHUNK 目标长度（行边界对齐后可能略超目标，不钉死数字）
    assert max(len(c["text"]) for c in requery_calls) <= 600


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
    requery_calls = [c for c in client.calls if len(c["types"]) <= 2]
    # 6 型 ⇒ 缺失 5 ⇒ 3 个类型批；若发生二层补查，数量会翻倍膨胀
    assert len(requery_calls) <= 3, f"补查不得递归：{len(requery_calls)} 个子请求"