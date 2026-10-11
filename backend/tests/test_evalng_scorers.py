"""eval-ng scorers 单测：透传零漂移（AC5）、speed 数值、stability 聚合、cost 占位。"""

import evalng_bootstrap as boot

eq = boot.load("engine/scorers/entity_quality.py")
speed = boot.load("engine/scorers/speed.py")
stability = boot.load("engine/scorers/stability.py")
cost = boot.load("engine/scorers/cost.py")
protocol = boot.load("engine/protocol.py")
nerq = eq.load_backend_nerq()

REPO = boot.REPO


def test_no_scoring_functions_copied():
    """AC5 静态断言：eval-ng 内不得出现打分函数本体定义（防复制双源漂移）。"""
    import re

    for rel in ("engine/scorers/entity_quality.py", "engine/runner.py"):
        path = REPO / "eval-ng" / rel
        if not path.exists():
            continue
        src = path.read_text(encoding="utf-8")
        for fn in ("compute_metrics", "digital_gate_pass", "compare_with_baseline", "grade_digital"):
            assert not re.search(rf"def\s+{fn}\s*\(", src), f"eval-ng 复制了打分函数 {fn}"
            assert re.search(rf"\b{fn}\b", src) or fn in ("grade_digital",), f"未透传 {fn}"


def _rec(page_id, gt, pred, latency=0.1):
    return {"page_id": page_id, "gt": gt, "pred": pred, "latency_sec": latency}


def test_entity_quality_passthrough_identical():
    records = [
        _rec("p1", {"姓名": ["张三"], "身份证号": ["110101199001010011"]},
             {"姓名": ["张三"], "身份证号": ["110101199001010011"]}),
        _rec("p2", {"电话": ["13800138000"]}, {"电话": ["13800138000"]}),
    ]
    ours = eq.score_entity_quality([dict(r) for r in records])
    theirs = nerq.compute_metrics([dict(r) for r in records])
    assert ours == theirs
    # 与自身比：deltas=0 且数字全对 → 三闸门全过
    cmp = eq.compare_baseline(ours, ours)
    assert cmp["gate_pass"] is True


def test_digital_veto_fails_on_near_miss():
    records = [
        _rec("p2", {"电话": ["13800138000"]}, {"电话": ["13800138001"]}),
    ]
    metrics = eq.score_entity_quality(records)
    ok, fails = eq.digital_veto(metrics)
    assert ok is False and fails


def test_score_entity_quality_struct_check():
    try:
        eq.score_entity_quality([{"page_id": "p1"}])
    except ValueError as e:
        assert "gt" in str(e)
    else:
        raise AssertionError("expected ValueError")


def test_speed_numbers():
    detail = [
        {"page": 1, "warmup": True, "wall_s": 10.0},
        {"page": 2, "warmup": False, "wall_s": 2.0},
        {"page": 3, "warmup": False, "wall_s": 4.0},
        {"page": 4, "warmup": False, "wall_s": 6.0},
    ]
    perf = speed.score_speed(detail, warmup_pages=1)
    assert perf["pages_total"] == 4 and perf["warmup_pages"] == 1 and perf["steady_pages"] == 3
    assert perf["wall_s"]["total"] == 12.0
    assert abs(perf["wall_s"]["mean"] - 4.0) < 1e-9
    assert perf["wall_s"]["p50"] == 4.0
    assert abs(perf["wall_s"]["p95"] - 5.8) < 1e-9
    assert abs(perf["throughput_pages_per_min"] - 15.0) < 1e-9


def test_speed_empty():
    perf = speed.score_speed([])
    assert perf["steady_pages"] == 0 and perf["throughput_pages_per_min"] == 0.0


def test_stability_aggregation():
    def run(f1, p, r):
        return {"overall": {"f1": f1, "precision": p, "recall": r}}

    agg = stability.score_stability([run(0.6, 0.7, 0.5), run(0.8, 0.7, 0.9), run(0.7, 0.7, 0.7)])
    assert agg["runs"] == 3
    assert abs(agg["f1"]["mean"] - (0.6 + 0.8 + 0.7) / 3) < 1e-9
    assert agg["f1"]["std"] > 0
    assert len(agg["per_run"]) == 3


def test_stability_single_run_zero_std():
    agg = stability.score_stability([{"overall": {"f1": 0.5, "precision": 0.5, "recall": 0.5}}])
    assert agg["f1"]["std"] == 0.0


def test_cost_placeholder():
    ans = [protocol.Answer(form=protocol.AnswerForm.ENTITY_TABLE, entities={})]
    out = cost.score_cost(ans)
    assert out["tokens_in"] is None and "note" in out
