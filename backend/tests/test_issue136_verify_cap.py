"""Issue#136 残值自证风暴的上界控制（每页 job 数上限 + 有界工作者池）。

方案与判据：`LexMask-workspace/github-issues/issue136-verify-cap-plan.md` §2/§3。
云实测基线：病态密页单页 `has_text_verify_calls=519`、模型耗时 211.5s。

判据（AC1/AC4/AC6）：
- 上限生效时 `verify_calls + verify_skipped == 待验证 job 数`，且 `verify_calls <= N`；
- `N=0` 表示不限（保持改前行为）；
- 批量与上限同时开启时，上限先作用于 job 数、再按批合并；
- 被截断的值不进入结果（与「自证未确认」同一语义）；
- 指标 `has_text_verify_calls / _skipped / _jobs_before / _jobs_after / _jobs_by_type / _peak_concurrency` 落 stage_status；
- 单值自证异常不污染整批/整池结果。
"""
import asyncio
import threading

import pytest

from app.services.ocr_has_vision_service import OCRTextBlock
from app.services.vision.has_text_analysis import run_has_text_analysis


@pytest.fixture(autouse=True)
def _clean_shared_client_state():
    from app.services.has_client import HaSClient

    cache = getattr(HaSClient, "_SHARED_NER_CACHE", None)
    inflight = getattr(HaSClient, "_SHARED_NER_INFLIGHT", None)
    if cache is not None:
        cache.clear()
    if inflight is not None:
        inflight.clear()
    yield
    if cache is not None:
        cache.clear()
    if inflight is not None:
        inflight.clear()


class _StubHaS:
    """按内容路由的 HaS 替身：主 payload 只召回第 1 个值 ⇒ 触发残值自证。

    内容键控（不按调用序号）——有界池让调用顺序不确定。计数器带锁。
    """

    base_url = "http://stub:0"

    def __init__(self, n: int, raise_on: str | None = None, reverse: bool = False,
                 batch_omit: set[str] | None = None, hallucinate: str | None = None):
        self.n = n
        self.batch_omit = batch_omit or set()
        self.raise_on = raise_on
        self.reverse = reverse
        self.hallucinate = hallucinate
        self.calls = 0
        self.peak = 0
        self.seen: list[str] = []
        self._cur = 0
        self._lock = threading.Lock()

    def ner(self, text, entity_types=None, **_kwargs):
        with self._lock:
            self.calls += 1
            self._cur += 1
            self.peak = max(self.peak, self._cur)
            self.seen.append(text)
        try:
            if self.raise_on and text.strip() == self.raise_on:
                raise RuntimeError("injected verify failure")
            if "款项 USD 1,000.00" in text:  # 主 payload（spaced）：只召回第 1 个
                return {"金额": ["USD1,000.00"]}
            # 残差 payload / verify 单值 / 批量质询（compact）：把文本里出现的值全答出来
            found = [f"USD{i},000.00" for i in range(1, self.n + 1) if f"USD{i},000.00" in text]
            # 「批量质询」形态的调用 = 纯 USD 值列表：在其中模拟模型漏答
            # （批内某个值未被确认），用于校验「∩ 批内值」的确认语义。
            _toks = [x.strip() for x in text.split("\n") if x.strip()]
            if _toks and all(t.startswith("USD") for t in _toks) and self.batch_omit:
                found = [v for v in found if v not in self.batch_omit]
            if self.hallucinate and "款项 USD 1,000.00" not in text:
                # 模型在缩减文本上的拟合产物：原文里根本不存在
                found = found + [self.hallucinate]
            if self.reverse:
                found = list(reversed(found))  # 模拟「模型返回顺序不同」
            return {"金额": found} if found else {}
        finally:
            with self._lock:
                self._cur -= 1


def _blocks(n: int):
    lines = [f"发票号 {i:04d} 款项 USD {i},000.00 经手人 张三{i}" for i in range(1, n + 1)]
    return [
        OCRTextBlock(
            text=t,
            polygon=[[100, 40 * i], [600, 40 * i], [600, 40 * i + 30], [100, 40 * i + 30]],
            confidence=0.98,
        )
        for i, t in enumerate(lines)
    ]


def _run(n: int, client, monkeypatch, *, cap=200, batch=0, parallel=4):
    from app.core.config import settings

    monkeypatch.setattr(settings, "SERIALIZE_SHARED_GPU_MODELS", False, raising=False)
    monkeypatch.setattr(settings, "HAS_NER_GLOBAL_MAX_INFLIGHT", parallel, raising=False)
    monkeypatch.setattr(settings, "HAS_NER_VERIFY_PARALLEL_CAP", parallel, raising=False)
    monkeypatch.setattr(settings, "HAS_NER_VERIFY_BATCH_SIZE", batch, raising=False)
    monkeypatch.setattr(settings, "HAS_NER_VERIFY_MAX_JOBS_PER_PAGE", cap, raising=False)
    stage: dict = {}
    entities = asyncio.run(
        run_has_text_analysis(_blocks(n), client, vision_types=None, stage_status=stage)
    )
    return entities, stage


def _amounts(entities):
    return {e["text"] for e in entities if e["type"] == "AMOUNT"}


def test_cap_truncates_jobs_and_records_skipped(monkeypatch):
    """AC1：N=2 时 jobs_before=4 → jobs_after=2、calls=2、skipped=2；按原文首现位置序取前 2（USD2/USD3）。"""
    client = _StubHaS(5)
    entities, stage = _run(5, client, monkeypatch, cap=2)
    assert stage["has_text_verify_jobs_before"] == 4
    assert stage["has_text_verify_jobs_after"] == 2
    assert stage["has_text_verify_calls"] == 2
    assert stage["has_text_verify_skipped"] == 2
    assert stage["has_text_verify_jobs_before"] == stage["has_text_verify_jobs_after"] + stage["has_text_verify_skipped"]
    assert stage["has_text_verify_jobs_by_type"] == {"金额": 2}
    assert sum(stage["has_text_verify_jobs_by_type"].values()) == stage["has_text_verify_jobs_after"]
    amounts = _amounts(entities)
    assert {"USD2,000.00", "USD3,000.00"} <= amounts  # 前 2 条被自证
    assert "USD4,000.00" not in amounts and "USD5,000.00" not in amounts  # 截断者不进入结果
    assert "USD1,000.00" in amounts  # 主召回不受影响


def test_cap_emits_warning_with_counts(monkeypatch, caplog):
    """AC4：触顶必须发 **WARNING**（级别本身是判据），并带 before/after/skipped 与按类型分布。

    变异 M12（把 `logger.warning` 降级为 `logger.debug`）不得存活。
    """
    import logging

    client = _StubHaS(5)
    with caplog.at_level(logging.WARNING):
        _entities, _stage = _run(5, client, monkeypatch, cap=2)
    records = [r for r in caplog.records if r.levelno == logging.WARNING]
    hit = next(
        (r for r in records if "residual verify capped" in r.getMessage()), None
    )
    assert hit is not None, caplog.text
    msg = hit.getMessage()
    assert "4 job(s) -> 2" in msg
    assert "skipped 2" in msg
    assert "金额" in msg


def test_verify_ms_metric_present_and_non_negative(monkeypatch):
    """AC1(d)：自证阶段**模型墙钟**指标随 stage_status 发出（同路径量测，不受 OCR 抖动影响）。"""
    client = _StubHaS(5)
    _entities, stage = _run(5, client, monkeypatch, cap=2)
    assert "has_text_verify_ms" in stage
    assert isinstance(stage["has_text_verify_ms"], int)
    assert stage["has_text_verify_ms"] >= 0


def test_truncation_order_is_input_deterministic_not_model_order(monkeypatch):
    """AC1（C4）：模型返回顺序反转时，截断仍按「值在原文首次出现位置」取前 2。

    旧实现按 residual_new 的插入序（= 模型 JSON key/数组顺序）取前 N ⇒ 该顺序
    换模型/模板即变，「哪些值被丢掉」不可复现。本用例把模型返回顺序反转：
    按模型序截断会保留 USD5/USD4，按原文首现位置序则保留 USD2/USD3。
    """
    client = _StubHaS(5, reverse=True)
    entities, stage = _run(5, client, monkeypatch, cap=2)
    assert stage["has_text_verify_calls"] == 2
    amounts = _amounts(entities)
    assert {"USD2,000.00", "USD3,000.00"} <= amounts
    assert "USD4,000.00" not in amounts and "USD5,000.00" not in amounts


def test_cap_zero_is_unlimited_and_matches_baseline(monkeypatch):
    """AC1：N=0 = 不限（保持改前行为），skipped 恒 0。"""
    client = _StubHaS(5)
    entities, stage = _run(5, client, monkeypatch, cap=0)
    assert stage["has_text_verify_jobs_before"] == stage["has_text_verify_jobs_after"] == 4
    assert stage["has_text_verify_calls"] == 4
    assert stage["has_text_verify_skipped"] == 0
    assert all(f"USD{i},000.00" in _amounts(entities) for i in range(1, 6))


def test_cap_above_jobs_is_noop(monkeypatch):
    """AC6 边界：N > jobs ⇒ 不截断、skipped=0。"""
    client = _StubHaS(5)
    _, stage = _run(5, client, monkeypatch, cap=99)
    assert stage["has_text_verify_calls"] == 4
    assert stage["has_text_verify_skipped"] == 0


def test_cap_equal_to_jobs_is_noop(monkeypatch, caplog):
    """AC6 边界：N == jobs ⇒ 不截断、skipped=0，且不应打「capped」告警。"""
    client = _StubHaS(5)
    with caplog.at_level("WARNING"):
        _, stage = _run(5, client, monkeypatch, cap=4)
    assert stage["has_text_verify_calls"] == 4
    assert stage["has_text_verify_skipped"] == 0
    assert "residual verify capped" not in caplog.text


def test_default_value_and_validator_bounds():
    """AC3 前置：默认档位是被冻结的预注册值（改动须改本用例），取值域 0..4096。

    默认 N 是**召回上限的杠杆**，不是普通常量：无云证据前不允许随手改。
    """
    from app.core.config import Settings, settings

    assert settings.HAS_NER_VERIFY_MAX_JOBS_PER_PAGE == 200
    validator = Settings._validate_has_ner_verify_max_jobs_per_page
    assert validator(-5) == 0
    assert validator(0) == 0
    assert validator(200) == 200
    assert validator(10**9) == 4096


def test_cap_one(monkeypatch):
    """AC6 边界：N=1 ⇒ 只自证 1 条。"""
    client = _StubHaS(5)
    _, stage = _run(5, client, monkeypatch, cap=1)
    assert stage["has_text_verify_calls"] == 1
    assert stage["has_text_verify_skipped"] == 3


def test_batch_path_reduces_calls(monkeypatch):
    """AC6：批量自证 = ceil(jobs/B) 次调用（同一语义：批内确认集 ∩）。"""
    client = _StubHaS(5)
    entities, stage = _run(5, client, monkeypatch, cap=0, batch=2)
    assert stage["has_text_verify_jobs_before"] == stage["has_text_verify_jobs_after"] == 4
    assert stage["has_text_verify_calls"] == 2
    assert stage["has_text_verify_skipped"] == 0
    assert all(f"USD{i},000.00" in _amounts(entities) for i in range(1, 6))


def test_batch_confirms_only_model_confirmed_values(monkeypatch):
    """AC6：批量自证确认集 = 模型返回值 ∩ 批内值 —— 模型未确认的批内值必须落空。

    变异靶子：把 `v in confirmed_batch` 改成恒 True ⇒ 本用例红。
    """
    client = _StubHaS(5, batch_omit={"USD3,000.00"})
    entities, stage = _run(5, client, monkeypatch, cap=0, batch=2)
    amounts = _amounts(entities)
    assert "USD3,000.00" not in amounts  # 模型在批里明确未答 ⇒ 不采纳
    assert {"USD2,000.00", "USD4,000.00", "USD5,000.00"} <= amounts
    assert stage["has_text_verify_calls"] == 2


def test_cap_applies_before_batching(monkeypatch):
    """AC1+AC6：上限先作用于 job 数、再按批合并 ⇒ calls=ceil(2/2)=1。"""
    client = _StubHaS(5)
    _, stage = _run(5, client, monkeypatch, cap=2, batch=2)
    assert stage["has_text_verify_jobs_before"] == 4
    assert stage["has_text_verify_jobs_after"] == 2
    assert stage["has_text_verify_calls"] == 1
    assert stage["has_text_verify_skipped"] == 2


def test_bounded_pool_keeps_result_set_and_peak(monkeypatch):
    """AC6：有界池不改变确认结果集；峰值 ≤ 窗口。"""
    client = _StubHaS(5)
    entities, stage = _run(5, client, monkeypatch, cap=0, parallel=2)
    assert 1 <= stage["has_text_verify_peak_concurrency"] <= 2
    assert all(f"USD{i},000.00" in _amounts(entities) for i in range(1, 6))


def test_single_verify_error_isolated(monkeypatch):
    """AC6：单个自证失败 ⇒ errors 计数 +1，其余值照常确认。"""
    client = _StubHaS(5, raise_on="USD3,000.00")
    entities, stage = _run(5, client, monkeypatch, cap=0)
    assert stage["has_text_verify_errors"] == 1
    assert "USD3,000.00" not in _amounts(entities)
    assert all(f"USD{i},000.00" in _amounts(entities) for i in (2, 4, 5))


def test_metrics_present_even_when_no_cap_hit(monkeypatch):
    """AC4：三个指标恒落盘（含 skipped=0 的情形），便于云端观测。"""
    client = _StubHaS(5)
    _, stage = _run(5, client, monkeypatch, cap=200)
    for key in ("has_text_verify_calls", "has_text_verify_skipped",
                "has_text_verify_jobs_before", "has_text_verify_jobs_after",
                "has_text_verify_jobs_by_type"):
        assert key in stage


def test_truncation_never_preempts_real_values_with_hallucinations(monkeypatch, caplog):
    """AC2/AC6：原文中找不到的值（模型在缩减文本上的拟合产物）排最后 ⇒ 上限截断先砍它们。

    判别力：把 `_verify_order` 的 found-flag 丢掉（①.5 r4 自造变异 NM-1）后，幻觉值的
    position 被记成 0、会抢占第一个名额 ⇒ 本用例判红（calls 里出现 USD9,000.00）。
    """
    import logging

    hall = "USD9,000.00"
    client = _StubHaS(3, hallucinate=hall)
    with caplog.at_level(logging.WARNING):
        entities, stage = _run(3, client, monkeypatch, cap=2)
    # 主 payload 只召回 USD1；残值 = USD2/USD3 + 幻觉值 = 3 条，cap=2 ⇒ 砍掉幻觉值
    assert hall not in _amounts(entities)
    assert _amounts(entities) == {"USD1,000.00", "USD2,000.00", "USD3,000.00"}
    assert hall not in client.seen, "幻觉值不得进入任何自证调用"
    assert stage["has_text_verify_jobs_before"] == 3
    assert stage["has_text_verify_jobs_after"] == 2
    assert stage["has_text_verify_skipped"] == 1
    assert "residual verify capped" in caplog.text
