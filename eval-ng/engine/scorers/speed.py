"""速度 scorer：与旧 run_eval perf 字段同构（AC5 离线对照口径）。

字段结构（对齐 eval/scripts/run_eval.py perf 块）：
{pages_total, warmup_pages, steady_pages, wall_s:{total,mean,p50,p95},
 throughput_pages_per_min, pages_detail}
"""

from __future__ import annotations


def _percentile(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    idx = q * (len(sorted_vals) - 1)
    lo, hi = int(idx), min(int(idx) + 1, len(sorted_vals) - 1)
    frac = idx - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


def score_speed(pages_detail: list[dict], warmup_pages: int = 1) -> dict:
    """pages_detail = [{page, warmup, wall_s, ...}]（同旧 run_eval iter_pages_with_timing 输出）。"""
    steady = [p for p in pages_detail if not p.get("warmup", False)]
    walls = sorted(float(p["wall_s"]) for p in steady)
    total = sum(walls)
    return {
        "pages_total": len(pages_detail),
        "warmup_pages": len(pages_detail) - len(steady),
        "steady_pages": len(steady),
        "wall_s": {
            "total": total,
            "mean": total / len(walls) if walls else 0.0,
            "p50": _percentile(walls, 0.50),
            "p95": _percentile(walls, 0.95),
        },
        "throughput_pages_per_min": (len(walls) / total * 60.0) if total > 0 else 0.0,
        "pages_detail": pages_detail,
    }
