"""稳定性 scorer：runs=N 聚合（本期 N=3，报告层必须输出 runs 数）。"""

from __future__ import annotations

import statistics


def score_stability(per_run_metrics: list[dict]) -> dict:
    """per_run_metrics：每轮 compute_metrics 输出。聚合 F1/P/R 的 mean/std 与逐轮值。"""
    if not per_run_metrics:
        return {"runs": 0, "f1_mean": None, "f1_std": None, "per_run": []}
    f1s = [m["overall"]["f1"] for m in per_run_metrics]
    ps = [m["overall"]["precision"] for m in per_run_metrics]
    rs = [m["overall"]["recall"] for m in per_run_metrics]

    def agg(vals: list[float]) -> dict:
        return {
            "mean": statistics.fmean(vals),
            "std": statistics.pstdev(vals) if len(vals) > 1 else 0.0,
        }

    return {
        "runs": len(per_run_metrics),
        "f1": agg(f1s),
        "precision": agg(ps),
        "recall": agg(rs),
        "per_run": [{"f1": f1, "precision": p, "recall": r} for f1, p, r in zip(f1s, ps, rs)],
    }
