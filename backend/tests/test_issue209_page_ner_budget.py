"""Issue#209 —— 页级 HaS NER 预算（方案 C）：超预算停发剩余阶段 + 显式记账 + strict 落页级失败。

真案卷证据（第四卷.pdf 第 133 页，149 页扫描件）：

- 页内 OCR 文本被防泄漏水印的「姓名碎片+17 位编号」行污染：HaS 提示词 323 行里
  293 行（≈90%）是只差数字的近似重复行（``映峰1450120`` / ``术映峰1450120141``…），
  精确去重杀不掉（每段编号独立）。
- 该页 OCR 只花 20s（687 blocks / 5952 chars），但 HaS NER 独立跑 >420s 不收敛：
  主查 ``types=20 text_chars=5372 max_tokens=2048`` 单请求 65.5–83.4s 撞完成帽
  （枚举式复读——#202 的流式签名掐断匹配不到"同结构只差数字"），json_repair 后按
  缺失类型切 9–12 段补查，外层又整页重发 ⇒ 循环到 300s 页墙整页清零。
- 页墙（``BATCH_RECOGNITION_PAGE_TIMEOUT``）看管的是一整页（含 OCR/视觉/打码），
  NER 自己的病态不受约束；本件给它一条自己的墙。

语义（验收判据）：

- 预算耗尽 ⇒ **不再开新组**（在飞组跑完）、**不再进残差**、**不再进逐值自证**；
  用已聚合的部分结果收尾。
- 记账：``has_text_ner_budget_exceeded``、``has_text_ner_budget_skipped_groups``、
  ``has_text_ner_budget_elapsed_ms`` + 一条 WARNING。
- ``HAS_NER_PAGE_BUDGET_STRICT_FAIL=True``（默认）⇒ 抛
  ``HasTextNerPageBudgetExceeded``，由页级 stage 落进 ``recognition.failed_pages``
  （与页超时同一条交付拦截链：界面可查、有失败页时导出拒绝）；
  ``False`` ⇒ 部分结果收尾，仍记账、不判失败。
- ``HAS_NER_PAGE_BUDGET_SEC=0`` ⇒ 关闭（退回"只受页门限看管"，与改前逐字等价）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from typing import Any

import pytest

from app.core.config import settings
from app.services.has_client import HaSClient
from app.services.ocr_has_vision_service import OCRTextBlock
from app.services.vision.has_text_analysis import (
    HasTextNerPageBudgetExceeded,
    _run_has_text_ner_groups,
    plan_has_text_group_texts,
    run_has_text_analysis,
)

_TYPES = ["身份证号", "电话"]
_MARK_HEAD = "370702199001014521"
_MARK_TAIL = "370702198802024567"


# ---------------------------------------------------------------------------
# 桩
# ---------------------------------------------------------------------------


class _FakeClock:
    """受控单调钟：模型请求每次推进 ``step_sec``，让预算在确定的位置耗尽。

    ``has_text_analysis`` 用的是模块级 ``import time`` 的 ``time.monotonic()`` /
    ``time.perf_counter()``——把该模块的 ``time`` 换成本桩，预算判据就完全确定
    （不依赖机器快慢，也不 sleep 真时间）。
    """

    def __init__(self, *, start: float = 1000.0, step_sec: float = 10.0):
        self.now = float(start)
        self.step_sec = float(step_sec)

    def monotonic(self) -> float:
        return self.now

    def perf_counter(self) -> float:
        return self.now

    def advance(self) -> None:
        self.now += self.step_sec


class _ModelFake(HaSClient):
    """只替换 ``_call_model``：真 ner()（预算/解析/缓存/补查/计数）全部照跑。"""

    def __init__(self, *, context_tokens: int = 4096, reply=None, clock: _FakeClock | None = None):
        super().__init__()
        self._ctx = context_tokens
        self.reply = reply or (lambda text, types: {})
        self.clock = clock
        self.calls: list[dict] = []
        self._lock = threading.Lock()

    def _effective_context_tokens(self) -> int:  # 探测不落网（鸭子类型接缝）
        return self._ctx

    def _call_model(self, messages, *, max_tokens=None, temperature=None):
        prompt = messages[0]["content"]
        text = prompt[prompt.rindex("<text>") + len("<text>") : prompt.rindex("</text>")]
        types = json.loads(
            next(line for line in prompt.splitlines() if line.startswith("Specified types:"))
            .split("Specified types:", 1)[1]
        )
        with self._lock:
            self.calls.append(
                {"text": text, "types": list(types), "max_tokens": max_tokens, "temperature": temperature}
            )
        if self.clock is not None:
            self.clock.advance()
        result = self.reply(text, list(types))
        return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)


class _VT:
    """最小 vision_types 桩（id / name / query_labels 三字段即够）。"""

    def __init__(self, id_: str, name: str, labels=()):
        self.id = id_
        self.name = name
        self.query_labels = list(labels)


@pytest.fixture(autouse=True)
def _clean_shared_caches():
    """HaSClient 的 NER 缓存是类级共享的：跨用例污染会让页级断言失真。"""
    HaSClient._context_probe_cache.clear()
    HaSClient._SHARED_NER_CACHE.clear()
    yield
    HaSClient._context_probe_cache.clear()
    HaSClient._SHARED_NER_CACHE.clear()


# ---------------------------------------------------------------------------
# 构造：多组页（真规划器分组，不是手搓）
# ---------------------------------------------------------------------------


def _unique_dense(n: int, seed: str) -> str:
    """逐块唯一的密集数字串（避免 payload 精确去重把整块吃掉）。"""
    unit = f"{seed}、" + ("991122334455667788," * 8)
    return (unit * (n // len(unit) + 1))[:n]


def _blocks(*texts: str) -> list[OCRTextBlock]:
    return [
        OCRTextBlock(
            text=text,
            polygon=[[0.0, 0.0], [100.0, 0.0], [100.0, 10.0], [0.0, 10.0]],
            confidence=1.0,
        )
        for text in texts
    ]


def _multi_group_page() -> tuple[list[str], list[OCRTextBlock], int, int]:
    """头/尾真值必然落进**不同组**的多组页（4096 链）。

    页由 8 个逐块唯一的 800 字符密块组成，第 0 块带 head 真值、第 7 块带 tail 真值。
    返回 (组文本, OCR 块, head 组号, tail 组号)。
    """
    lines = []
    for i in range(8):
        marker = _MARK_HEAD if i == 0 else (_MARK_TAIL if i == 7 else "")
        line = _unique_dense(800, f"块{i}")
        lines.append(line + ("，" + marker if marker else ""))
    group_texts = plan_has_text_group_texts(lines, _TYPES, context_tokens=4096)
    head_groups = [i for i, text in enumerate(group_texts) if _MARK_HEAD in text]
    tail_groups = [i for i, text in enumerate(group_texts) if _MARK_TAIL in text]
    assert len(group_texts) >= 2, f"构造未分组（{len(group_texts)}），判据失效"
    assert len(head_groups) == 1 and len(tail_groups) == 1, (head_groups, tail_groups)
    assert head_groups[0] != tail_groups[0], f"头尾真值落到同一组 {head_groups}，判据失效"
    return group_texts, _blocks(*lines), head_groups[0], tail_groups[0]


def _mark_reply(text: str, types: list[str]) -> dict[str, list[str]]:
    values = [m for m in (_MARK_HEAD, _MARK_TAIL) if m in text]
    return {"身份证号": values} if values and "身份证号" in types else {}


def _run_page(blocks, client, *, stage: dict[str, Any] | None = None):
    return asyncio.run(
        run_has_text_analysis(
            blocks, client, [_VT("ID_CARD", "身份证号"), _VT("PHONE", "电话")], stage if stage is not None else {}
        )
    )


# ---------------------------------------------------------------------------
# 组级：deadline 语义
# ---------------------------------------------------------------------------


def test_group_deadline_in_the_past_skips_every_group_and_records():
    """死线已过 ⇒ 一组都不发（不是「发了再丢」），跳过数可读。"""
    client = _ModelFake(reply=lambda text, types: {"身份证号": [_MARK_HEAD]})
    stage: dict = {}

    merged, failed = asyncio.run(
        _run_has_text_ner_groups(client, stage, ["g1", "g2", "g3"], _TYPES, deadline=time.monotonic() - 1.0)
    )

    assert merged == {}
    assert failed == 0, "预算跳过不是「组失败」语义（后者会把整页判失败）"
    assert stage["has_text_ner_budget_exceeded"] == 1
    assert stage["has_text_ner_budget_skipped_groups"] == 3
    assert client.calls == [], "预算已耗尽却仍对模型发了请求"


def test_group_deadline_in_the_future_runs_every_group_without_budget_keys():
    client = _ModelFake(reply=lambda text, types: {})
    stage: dict = {}

    _merged, failed = asyncio.run(
        _run_has_text_ner_groups(client, stage, ["g1", "g2", "g3"], _TYPES, deadline=time.monotonic() + 30.0)
    )

    assert failed == 0
    assert len(client.calls) == 3
    assert "has_text_ner_budget_exceeded" not in stage
    assert "has_text_ner_budget_skipped_groups" not in stage


def test_group_deadline_none_is_legacy_path():
    """不传 deadline（=历史签名）⇒ 无预算键、全组照发（零回归钉子）。"""
    client = _ModelFake(reply=lambda text, types: {})
    stage: dict = {}

    _merged, failed = asyncio.run(_run_has_text_ner_groups(client, stage, ["g1", "g2"], _TYPES))

    assert failed == 0
    assert len(client.calls) == 2
    assert "has_text_ner_budget_exceeded" not in stage


# ---------------------------------------------------------------------------
# 页级：strict / 非 strict / 关闭 / 零回归
# ---------------------------------------------------------------------------


def test_page_budget_strict_fails_the_page_with_dedicated_exception(monkeypatch, caplog):
    """strict（默认）⇒ 抛专属异常 + 记账；页面只跑了一部分，不得当完成页。"""
    clock = _FakeClock(step_sec=10.0)
    group_texts, blocks, _head, _tail = _multi_group_page()
    monkeypatch.setattr(settings, "HAS_NER_PAGE_BUDGET_SEC", 5.0, raising=False)
    monkeypatch.setattr(settings, "HAS_NER_PAGE_BUDGET_STRICT_FAIL", True, raising=False)
    monkeypatch.setattr("app.services.vision.has_text_analysis.time", clock)
    monkeypatch.setattr(settings, "HAS_VISION_MAX_GROUPS_INFLIGHT", 1, raising=False)
    client = _ModelFake(clock=clock)

    stage: dict = {}
    with caplog.at_level(logging.WARNING, logger="app.services.vision.has_text_analysis"):
        with pytest.raises(HasTextNerPageBudgetExceeded) as excinfo:
            _run_page(blocks, client, stage=stage)

    assert "budget" in str(excinfo.value).lower()
    assert stage["has_text_ner_budget_exceeded"] == 1
    assert stage["has_text_ner_budget_elapsed_ms"] >= 0
    assert stage.get("has_text_ner_budget_skipped_groups", 0) >= 1
    assert any("budget exceeded" in record.getMessage() for record in caplog.records), caplog.text


def test_page_budget_non_strict_keeps_partial_and_is_a_strict_subset(monkeypatch):
    """非 strict ⇒ 部分结果收尾（头组值在、尾组未跑），但必须记账且比全量少。"""
    group_texts, blocks, _head, _tail = _multi_group_page()
    monkeypatch.setattr(settings, "HAS_NER_PAGE_BUDGET_SEC", 5.0, raising=False)
    monkeypatch.setattr(settings, "HAS_NER_PAGE_BUDGET_STRICT_FAIL", False, raising=False)

    clock = _FakeClock(step_sec=10.0)
    monkeypatch.setattr("app.services.vision.has_text_analysis.time", clock)
    monkeypatch.setattr(settings, "HAS_VISION_MAX_GROUPS_INFLIGHT", 1, raising=False)
    client = _ModelFake(reply=_mark_reply, clock=clock)

    stage: dict = {}
    page = _run_page(blocks, client, stage=stage)

    assert stage["has_text_ner_budget_exceeded"] == 1
    assert 0 < len(client.calls) < len(group_texts), (
        f"预算应在组间耗尽：calls={len(client.calls)} groups={len(group_texts)}"
    )
    assert {e["text"] for e in page} == {_MARK_HEAD}, "只跑了的组的值才可采纳（尾组未跑）"

    # 全量参照（关闭预算 + 全新时钟/客户端）：部分结果必须严格小
    monkeypatch.setattr("app.services.vision.has_text_analysis.time", _FakeClock(step_sec=0.0))
    full = _run_page(blocks, _ModelFake(reply=_mark_reply))
    assert {e["text"] for e in full} == {_MARK_HEAD, _MARK_TAIL}
    assert {e["text"] for e in page} < {e["text"] for e in full}


def test_page_budget_is_a_hard_wall_around_one_long_call(monkeypatch, caplog):
    """预算必须能截断**单次** ner() 内部的长链（真实时间，非受控钟）。

    门④ 实测（门禁④ 云实例第二轮，2026-10-29 06:01）：预算是 240s，但页 133 仍然
    跑满 300s 被页墙杀掉、预算一次都没触发——因为一次 ``has_client.ner()`` 内部会走
    「主查撞完成帽 → 按缺失类型切 9~12 段补查 → 残差重发」的级联，阶段边界的复检
    进不去。这里用一个**真睡**的模型验证：wait_for 用剩余预算兜住整条链，页面在
    预算处收尾，而不是等到模型自然结束。
    """
    group_texts, blocks, _head, _tail = _multi_group_page()
    monkeypatch.setattr(settings, "HAS_NER_PAGE_BUDGET_SEC", 0.2, raising=False)
    monkeypatch.setattr(settings, "HAS_NER_PAGE_BUDGET_STRICT_FAIL", False, raising=False)
    monkeypatch.setattr(settings, "HAS_VISION_MAX_GROUPS_INFLIGHT", 1, raising=False)

    class _SlowModel(_ModelFake):
        def _call_model(self, messages, *, max_tokens=None, temperature=None):
            time.sleep(1.5)  # 远长于预算：自然结束要到 1.5s
            return super()._call_model(messages, max_tokens=max_tokens, temperature=temperature)

    client = _SlowModel()
    stage: dict = {}

    async def _timed():
        # 只量 await 本身的耗时：``asyncio.run`` 收尾会 join 默认线程池（to_thread
        # 的 1.5s 睡线程），那部分不算页面阻塞——服务端不关事件循环，页面早已返回。
        started = time.monotonic()
        result = await run_has_text_analysis(
            blocks, client, [_VT("ID_CARD", "身份证号"), _VT("PHONE", "电话")], stage
        )
        return result, time.monotonic() - started

    with caplog.at_level(logging.WARNING, logger="app.services.vision.has_text_analysis"):
        merged, elapsed = asyncio.run(_timed())
    assert elapsed < 1.0, f"预算没兜住长请求（elapsed={elapsed:.2f}s）"
    assert merged == [], "被硬墙截断的组不得当成功值合并"
    assert stage.get("has_text_ner_budget_exceeded") == 1
    assert any("budget exceeded" in r.getMessage() for r in caplog.records), caplog.text


def test_page_budget_zero_disables_the_wall(monkeypatch):
    """``HAS_NER_PAGE_BUDGET_SEC=0`` ⇒ 关闭：不记账、不抛，全组照跑。"""
    group_texts, blocks, _head, _tail = _multi_group_page()
    monkeypatch.setattr(settings, "HAS_NER_PAGE_BUDGET_SEC", 0.0, raising=False)
    monkeypatch.setattr(settings, "HAS_NER_PAGE_BUDGET_STRICT_FAIL", True, raising=False)
    monkeypatch.setattr("app.services.vision.has_text_analysis.time", _FakeClock(step_sec=99.0))
    client = _ModelFake(reply=_mark_reply)

    stage: dict = {}
    page = _run_page(blocks, client, stage=stage)

    assert {e["text"] for e in page} == {_MARK_HEAD, _MARK_TAIL}
    assert "has_text_ner_budget_exceeded" not in stage
    assert "has_text_ner_budget_skipped_groups" not in stage


def test_default_budget_fast_page_is_byte_identical_to_wall_disabled(monkeypatch):
    """默认 150s 预算 + 快页 ⇒ 与"关预算"逐字等价（零回归钉子）。"""
    _group_texts, blocks, _head, _tail = _multi_group_page()

    monkeypatch.setattr(settings, "HAS_NER_PAGE_BUDGET_SEC", 150.0, raising=False)
    monkeypatch.setattr(settings, "HAS_NER_PAGE_BUDGET_STRICT_FAIL", True, raising=False)
    monkeypatch.setattr("app.services.vision.has_text_analysis.time", _FakeClock(step_sec=10.0))
    stage_on: dict = {}
    page_with_wall = _run_page(blocks, _ModelFake(reply=_mark_reply), stage=stage_on)

    monkeypatch.setattr(settings, "HAS_NER_PAGE_BUDGET_SEC", 0.0, raising=False)
    monkeypatch.setattr("app.services.vision.has_text_analysis.time", _FakeClock(step_sec=10.0))
    stage_off: dict = {}
    page_without_wall = _run_page(blocks, _ModelFake(reply=_mark_reply), stage=stage_off)

    assert "has_text_ner_budget_exceeded" not in stage_on
    assert page_with_wall == page_without_wall
    assert page_with_wall, "钉子页必须真出实体，否则等价断言恒真"
    assert stage_off.get("has_text_entity_count") == len(page_with_wall)