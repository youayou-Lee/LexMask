"""实体质量 scorer：透传 backend eval_ner_quality（单一 import 源，零打分逻辑）。"""

from __future__ import annotations

from ._nerq_source import load_backend_nerq


def score_entity_quality(records: list[dict]) -> dict:
    """records = [{page_id, gt, pred, latency_sec}]；直接透传 nerq.compute_metrics。

    仅做结构校验，不含任何打分逻辑（AC5 零漂移）。
    """
    for i, rec in enumerate(records):
        for key in ("page_id", "gt", "pred"):
            if key not in rec:
                raise ValueError(f"records[{i}] 缺字段 {key!r}")
    return load_backend_nerq().compute_metrics(records)


def digital_veto(metrics: dict) -> tuple[bool, list[str]]:
    """数字一票否决：四类数字 exact_rate 必须 = 1.0。透传 nerq.digital_gate_pass。"""
    return load_backend_nerq().digital_gate_pass(metrics)


def compare_baseline(candidate: dict, baseline: dict) -> dict:
    """基线三闸门（R≥基线−1pp / P≥基线 / 数字 exact 100%）。透传 nerq.compare_with_baseline。"""
    return load_backend_nerq().compare_with_baseline(candidate, baseline)
