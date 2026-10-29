"""Issue#209 成本结构修复①：完成帽按实体值需求放开 + 装填不变式同源。

根因（真实第四卷第 133 页，2026-10-29 云实测）：该页真实实体值 ≈200 个 ⇒ 一次问全
需 ≥2048 token 输出。帽=2048 时主查**必然**截断（实测单请求 70–124s 烧满帽），
截断恢复（分块补查 + 残差整页重发 + 逐值自证）把同一页排成 2 次整页 + 20 余次
子请求 = 300–427s > 300s 页墙。隔离实测帽=4096 ⇒ 85.3s / 154 值 / **一次收敛**。

本文件钉两件事：
- 默认完成帽 = 4096（放开到"一次问全"）；
- 放开后**装填不变式**仍成立：每条实发请求 `prompt + max_tokens` 必须装进
  `context - reserve - safety`。旧算术只扣 reserve，帽一抬就溢出 64 token
  （D2 实测破裂），故 `context_room` 已与派发侧同源扣 safety。
"""

from __future__ import annotations

import pytest

from app.core.config import settings
from app.services.has_client import (
    _NER_CONTEXT_RESERVE_TOKENS,
    _NER_FILL_SAFETY_TOKENS,
    HaSClient,
)

# 页 133 的真实形状：323 行 / 3928 字符 / 密集数字表格
_DENSE_3928 = ("370702199001014521," * (3928 // 19 + 2))[:3928]
_TYPES_20 = [f"类型{i}" for i in range(20)]


def test_default_completion_cap_allows_one_pass_emission():
    """默认帽必须容得下"这一页一次问全"的预算（≥2048 输出）。"""
    assert settings.HAS_NER_COMPLETION_HARD_CAP == 4096

    _, _, max_tokens = HaSClient._resolve_ner_budget(
        _TYPES_20[0:1], _DENSE_3928, context_tokens=8192
    )
    # 单类型也已高于旧的 2048 帽（文本份额本身就 >2048）
    assert max_tokens > 1500
    # 多类型时预算必须能越过 2048（旧帽下这是不可能事件）
    _, estimate, max_tokens_multi = HaSClient._resolve_ner_budget(
        _TYPES_20, _DENSE_3928, context_tokens=8192
    )
    assert estimate + max_tokens_multi <= 8192 - _NER_CONTEXT_RESERVE_TOKENS - _NER_FILL_SAFETY_TOKENS
    assert max_tokens_multi >= 2048, (
        f"帽放开后仍装不下一次问全的输出预算：max_tokens={max_tokens_multi}"
    )


@pytest.mark.parametrize("context_tokens", [4096, 8192, 16384])
def test_fill_invariant_holds_for_dense_page(context_tokens):
    """装填不变式：estimate + max_tokens ≤ context - reserve - safety（任意上下文）。"""
    for n_types in (1, 5, 20):
        _, estimate, max_tokens = HaSClient._resolve_ner_budget(
            [f"类型{i}" for i in range(n_types)], _DENSE_3928, context_tokens=context_tokens
        )
        fill = context_tokens - _NER_CONTEXT_RESERVE_TOKENS - _NER_FILL_SAFETY_TOKENS
        if estimate > fill:
            # 该上下文连提示词本身都装不下（如 4096 上下文 × 3928 字符密文）：
            # 这不是预算问题，是**分组器**必须切页的情形（#104 已钉）。
            continue
        assert estimate + max_tokens <= fill, (
            f"装填超限 context={context_tokens} types={n_types}: "
            f"{estimate}+{max_tokens} > {fill}"
        )


def test_planner_no_longer_splits_a_page_that_fits_one_pass(monkeypatch):
    """帽放开后，原本被"预算>帽"逼着切的页回到整页单次（少一次往返）。"""
    monkeypatch.setattr(settings, "HAS_NER_COMPLETION_HARD_CAP", 4096, raising=False)
    text = "字" * 1577
    plan = HaSClient.plan_completion_batches([f"类型{i}" for i in range(19)], text)
    assert [len(batch) for batch in plan] == [19], (
        "预算 19×160+788=3828 已装进 4096 帽，不应再切批"
    )


def test_planner_still_splits_when_budget_truly_exceeds_cap(monkeypatch):
    """容量算术没有被放开压坏：预算真超帽时照样切，且批并集无丢失。"""
    monkeypatch.setattr(settings, "HAS_NER_COMPLETION_HARD_CAP", 4096, raising=False)
    text = "字" * 1577
    source = [f"类型{i}" for i in range(30)]
    plan = HaSClient.plan_completion_batches(source, text)
    assert len(plan) > 1
    assert [item for batch in plan for item in batch] == source