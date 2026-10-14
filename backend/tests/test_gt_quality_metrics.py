# Issue#105 G5 —— 质量三数 quality_metrics.py 的离线单测（全部合成数据）。
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "eval"))
from gt import quality_metrics as qm  # noqa: E402


# ---- 自动消化率 ------------------------------------------------------------

def test_auto_digest_rate():
    """分母统一=实体总数（拿残留数当分母会出现 >1 的荒谬比率）。"""
    adjudications = [
        {"verdict": "auto_closed"}, {"verdict": "auto_closed"},
        {"verdict": "human_review"}, {"verdict": "human_review"},
        {"verdict": "human_review"},
    ]
    m = qm.auto_digest_rate(adjudications, upstream_closed=6, total_entities=10)
    # 上游 6/10=60%；软层 2/10=20%；全链 8/10=80%
    assert m["upstream"] == pytest.approx(0.6)
    assert m["soft_layer"] == pytest.approx(0.2)
    assert m["overall"] == pytest.approx(0.8)


def test_auto_digest_rate_zero_division_safe():
    m = qm.auto_digest_rate([], upstream_closed=0, total_entities=0)
    assert m["upstream"] == m["soft_layer"] == m["overall"] == 0.0


# ---- Agent 错误率（抽检） ---------------------------------------------------

def test_agent_error_rate():
    spot = [{"confirmed": True}] * 19 + [{"confirmed": False}]
    m = qm.agent_error_rate(spot)
    assert m["sampled"] == 20
    assert m["errors"] == 1
    assert m["error_rate"] == pytest.approx(0.05)


def test_agent_error_rate_empty():
    m = qm.agent_error_rate([])
    assert m == {"sampled": 0, "errors": 0, "error_rate": 0.0, "available": False}


# ---- 同源一致率（同模型两跑结论比对） ---------------------------------------

def test_same_source_agreement():
    run1 = [{"page": "a", "entities": [{"text": "张三", "type": "姓名"}]},
            {"page": "b", "entities": []}]
    run2 = [{"page": "a", "entities": [{"text": "张三", "type": "姓名"}]},
            {"page": "b", "entities": []}]
    m = qm.same_source_agreement(run1, run2)
    assert m["pages_compared"] == 2 and m["agree"] == 2
    assert m["agreement"] == 1.0


def test_same_source_agreement_disagreement():
    """页级严格一致（实体集合完全相等）：少一个实体=该页不一致。"""
    run1 = [{"page": "a", "entities": [{"text": "张三", "type": "姓名"},
                                        {"text": "李四", "type": "姓名"}]}]
    run2 = [{"page": "a", "entities": [{"text": "张三", "type": "姓名"}]}]
    m = qm.same_source_agreement(run1, run2)
    assert m["pages_compared"] == 1 and m["agree"] == 0
    assert m["agreement"] == 0.0


def test_same_source_agreement_needs_both_runs():
    assert qm.same_source_agreement(None, None)["available"] is False


# ---- 熔断阈值 ---------------------------------------------------------------

def test_circuit_breaker():
    assert qm.circuit_breaker(error_rate=0.11, threshold=0.10) is True
    assert qm.circuit_breaker(error_rate=0.05, threshold=0.10) is False
    assert qm.circuit_breaker(error_rate=0.0, threshold=0.10, sampled=0) is False  # 无样本不熔断


# ---- 汇总报告 ---------------------------------------------------------------

def test_build_quality_report():
    rep = qm.build_quality_report(
        adjudications=[{"verdict": "auto_closed"}],
        upstream_closed=3, upstream_total=4,  # 实体总数 4：上游 3/4 + 软层 1/4 = 全链 100%
        spot_check=[{"confirmed": True}] * 9 + [{"confirmed": False}],
        same_source=(None, None),
        thresholds={"agent_error": 0.10},
    )
    assert rep["digest"]["overall"] == pytest.approx(1.0)
    assert rep["agent_error"]["error_rate"] == pytest.approx(0.1)
    assert rep["circuit_break"]["tripped"] is False
    assert rep["same_source"]["available"] is False
