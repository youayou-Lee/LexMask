"""Issue#209 门⑥（独立评审 2026-10-29）REVISE 的逐条回归钉。

覆盖评审给出的四条**会静默少打码**的缺陷（F2/F3/F5/F6）与两处判据硬化（F1 已
并入 ``test_issue209_near_duplicate_collapse.py``、F9 在本文件）：

- **F2**（Important）C2 的并入闸原按「类型集合基数」近似「会不会同 payload 整段
  重发」。改动③之后长文本**恒按行边界切块**，切出的块 key 必与原文不同 ⇒ 自连接
  死锁前提消失。原判据恰好挡住「主查只回了被切断的那一个桶」这一**正是 C2 要修**
  的形态（实测 19 类型 / 74 行单号：编号只剩 2 个值，且 18 个缺失类型照旧补查）。
- **F3**（Important）被切断的桶存在但没能并入补查集合时，结果**已知残缺**，
  却仍 `confirmed=True` ⇒ `_set_cached_ner` 把残缺结果写进共享缓存，下次同键零
  调用命中并报 ok=True（评审注入该改动的 M13 全绿 ⇒ 此前零判据）。
- **F5**（Important）页级 NER 死线原先在「首次 NER 调用」才建立，OCR 耗时不计入
  预算（OCR 120s + NER 200s = 320s 仍先撞 300s 页墙）⇒ 死线必须在**页入口**建立。
- **F6**（Important）`max(_NER_MAX_TOKENS_FLOOR, context_room)` 在
  `context_room ∈ (0, 64]` 时会把 `estimate + max_tokens` 顶到装填上限之上最多
  64 token；本文件用**扫长度找带内样本**的方式钉住 (fill-64, fill] 这一段。
- **F9**（Minor）`_truncated_tail_bucket` 原先只看「后面有没有 ]」，值字符串里
  含 `]`（地址 `A[1]`）会假阴性 ⇒ C2 静默失效。
"""

from __future__ import annotations

import json
import time

import pytest

from app.core.config import settings
from app.services.has_client import (
    HaSClient,
    _NER_CONTEXT_RESERVE_TOKENS,
    _NER_FILL_SAFETY_TOKENS,
    _NER_MAX_TOKENS_FLOOR,
)
from app.services.vision.has_text_analysis import (
    has_text_page_deadline,
    reset_page_ner_deadline,
)

# 长到必然被行边界切块的文本（与 test_issue209_requery_budget.py 同款夹具）
_LONG = "\n".join(f"第{i}行内容 99112233{i:04d}" for i in range(120))


def _prompt_text(prompt: str) -> str:
    return prompt[prompt.rindex("<text>") + len("<text>") : prompt.rindex("</text>")]


def _prompt_types(prompt: str) -> list[str]:
    line = next(line for line in prompt.splitlines() if line.startswith("Specified types:"))
    return json.loads(line.split("Specified types:", 1)[1])


class _Recorder(HaSClient):
    """记录每次实发的 (text, types)；主查由子类覆写 ``_main_reply`` 决定。"""

    main_reply = "{}"

    def __init__(self):
        super().__init__()
        self.calls: list[dict] = []

    def _effective_context_tokens(self) -> int:
        return 8192

    def _main_reply_for(self, types: list[str]) -> str:
        return self.main_reply

    def _call_model(self, messages, *, max_tokens=None, temperature=None):
        prompt = messages[0]["content"]
        text = _prompt_text(prompt)
        types = _prompt_types(prompt)
        self.calls.append({"text": text, "types": list(types)})
        if not self.calls[1:]:  # 第一次调用 = 主查
            return self._main_reply_for(types)
        return json.dumps({t: [f"值-{t}"] for t in types}, ensure_ascii=False)


@pytest.fixture(autouse=True)
def _clean_cache():
    HaSClient._SHARED_NER_CACHE.clear()
    yield
    HaSClient._SHARED_NER_CACHE.clear()


# ---------------------------------------------------------------------------
# F2：只剩「被切断的那个桶」× 长文本 ⇒ 必须并入补查
# ---------------------------------------------------------------------------

class _CutBucketOnly(_Recorder):
    """主查只回了**一个**桶（=本次批的最后一个请求类型），且尾值被切断。"""

    def _main_reply_for(self, types):
        return '{"' + types[-1] + '":["*721721", "截断残'


def test_cut_bucket_on_chunked_text_is_requeried():
    # 真实页 133 的形态：主查只回了被切断的那一个桶，其余类型全缺（74 行真实
    # 报关单号所在桶只剩 2 个值）。6 类型/2409 字符 ⇒ 主查一批装得下全类型。
    types = ["编号", *[f"类型{i}" for i in range(5)]]
    client = _CutBucketOnly()

    client.ner_checked(_LONG, types, None, context_tokens=8192)

    main, requery = client.calls[0], client.calls[1:]
    assert sorted(main["types"]) == sorted(types), "主查是整页全类型"
    assert len(client._split_requery_chunks(_LONG)) > 1, "用例前提：文本会被切块"
    requeried = {t for call in requery for t in call["types"]}
    assert "编号" in requeried, (
        "被切断的桶必须进补查集合（否则它永远补不上、静默少打码）："
        f"实际补查类型={sorted(requeried)}"
    )


# ---------------------------------------------------------------------------
# F3：被切断的桶存在但没能并入 ⇒ confirmed 必须为 False（不落缓存）
# ---------------------------------------------------------------------------

class _CutBucketBlocked(_Recorder):
    """3 类型、短文本（不会切块）⇒ C2 的保守闸生效：被切断桶无法并入。"""

    def _main_reply_for(self, types):
        return '{"甲":["值-甲","截断残'


def test_unrequeried_cut_bucket_is_not_confirmed():
    types = ["甲", "乙", "丙"]
    client = _CutBucketBlocked()

    result, confirmed = client.ner_checked("短文本一行 99112233", types, None, context_tokens=8192)

    assert "甲" in result
    assert confirmed is False, "已知残缺的结果不得声称确认（否则会写进共享缓存）"
    outer_keys = [
        key for key in HaSClient._SHARED_NER_CACHE if set(key[1]) == set(types)
    ]
    assert outer_keys == [], f"残缺结果不得以本次请求的类型集合落键：{outer_keys}"


# ---------------------------------------------------------------------------
# F5：页级死线在**页入口**建立，同页复用，reset 重新起算，关闸为 None
# ---------------------------------------------------------------------------

def test_page_deadline_is_established_at_page_entry(monkeypatch):
    monkeypatch.setattr(settings, "HAS_NER_PAGE_BUDGET_SEC", 200.0, raising=False)

    reset_page_ner_deadline()
    deadline = has_text_page_deadline()
    remaining = deadline - time.monotonic()

    assert deadline is not None
    assert 190.0 < remaining <= 200.5, f"页入口即起算 200s 预算，实际余 {remaining:.1f}s"
    assert has_text_page_deadline() == deadline, "同页多次 NER 必须复用同一条死线"

    reset_page_ner_deadline()
    assert has_text_page_deadline() > deadline, "新页必须重新起算"


def test_page_deadline_none_when_budget_disabled(monkeypatch):
    monkeypatch.setattr(settings, "HAS_NER_PAGE_BUDGET_SEC", 0.0, raising=False)
    reset_page_ner_deadline()
    assert has_text_page_deadline() is None


# ---------------------------------------------------------------------------
# F6：(fill-64, fill] 带内也不许超装填
# ---------------------------------------------------------------------------

def test_fill_invariant_holds_when_context_room_is_tiny():
    context_tokens = 8192
    fill = context_tokens - _NER_CONTEXT_RESERVE_TOKENS - _NER_FILL_SAFETY_TOKENS
    band_hit = False
    violations: list[tuple[int, int, int, int]] = []

    for length in range(6900, 7900):
        text = "3" * length
        _prompt, estimate, max_tokens = HaSClient._resolve_ner_budget(
            ["编号"], text, context_tokens=context_tokens
        )
        room = context_tokens - estimate - _NER_CONTEXT_RESERVE_TOKENS - _NER_FILL_SAFETY_TOKENS
        if 0 < room <= _NER_MAX_TOKENS_FLOOR:
            band_hit = True
        # room <= 0 = 提示词本身已装满填限额（单条请求无法自救，必须由分组器
        # 切分）；不变式只对 room > 0 成立。
        if room > 0 and estimate + max_tokens > fill:
            violations.append((length, estimate, max_tokens, room))

    assert band_hit, "用例前提：必须扫到 context_room ∈ (0, 64] 的带内样本"
    assert violations == [], f"装填不变式被违反：{violations[:5]}"


# ---------------------------------------------------------------------------
# F9：值字符串里的 ] 不得让「桶未闭合」判定假阴性
# ---------------------------------------------------------------------------

def test_truncated_tail_bucket_ignores_brackets_inside_strings():
    raw = '{"地址": ["北京市朝阳区 A[1] 号楼", "北京市朝阳区 A[2] 号楼"], "编号": ["721721'
    assert HaSClient._truncated_tail_bucket(raw, ["地址", "编号"]) == "编号"
    closed = '{"地址": ["北京市朝阳区 A[1] 号楼"], "编号": ["721721"]}'
    assert HaSClient._truncated_tail_bucket(closed, ["地址", "编号"]) is None


# ---------------------------------------------------------------------------
# F8 加固：strict 预算失败必须**页级失败**（不得降级成 completed 的部分扫描）
# ---------------------------------------------------------------------------

def test_strict_budget_exception_is_page_fatal(monkeypatch):
    from app.services.vision.has_text_analysis import HasTextNerPageBudgetExceeded
    from app.services.vision_service import is_page_fatal_stage_error

    monkeypatch.setattr(settings, "HAS_NER_PAGE_BUDGET_STRICT_FAIL", True, raising=False)
    assert is_page_fatal_stage_error(HasTextNerPageBudgetExceeded("budget")) is True
    # 非预算类阶段异常维持既有「降级 + warning」语义
    assert is_page_fatal_stage_error(RuntimeError("OCR service down")) is False
    assert is_page_fatal_stage_error(None) is False

    monkeypatch.setattr(settings, "HAS_NER_PAGE_BUDGET_STRICT_FAIL", False, raising=False)
    assert is_page_fatal_stage_error(HasTextNerPageBudgetExceeded("budget")) is False
