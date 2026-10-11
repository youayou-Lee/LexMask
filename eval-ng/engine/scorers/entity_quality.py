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


def error_rows(records: list[dict], limit: int = 200) -> list[dict]:
    """错题清单（诊断用，不影响闸门）：数字丢失/数字字符噪声（原串域）+ 漏检/误检（集合域）。

    口径与旧 run_eval error_rows 同构；打分分级走 nerq.grade_digital 单一源。
    """
    nerq = load_backend_nerq()
    rows: list[dict] = []
    for rec in records:
        gt = rec.get("gt") or {}
        pred = rec.get("pred") or {}
        for etype, gvals in gt.items():
            gset = {str(v) for v in gvals}
            pvals = [str(v) for v in (pred.get(etype) or [])]
            pset = set(pvals)
            if etype in nerq.DIGITAL_GATE_TYPES or etype in nerq.REFERENCE_STRICT_TYPES:
                graded = nerq.grade_digital(sorted(gset), pvals)
                for v in graded["miss"]:
                    rows.append({"page_id": rec["page_id"], "type": etype,
                                 "kind": "数字丢失", "expect": v, "actual": "—"})
                for v, w in graded["near_miss"]:
                    rows.append({"page_id": rec["page_id"], "type": etype,
                                 "kind": "数字字符噪声", "expect": v, "actual": w})
            for v in sorted(gset - pset):
                rows.append({"page_id": rec["page_id"], "type": etype,
                             "kind": "漏检", "expect": v, "actual": "—"})
        for etype, pvals in pred.items():
            pset = {str(v) for v in pvals}
            gset = {str(v) for v in (gt.get(etype) or [])}
            for v in sorted(pset - gset):
                rows.append({"page_id": rec["page_id"], "type": etype,
                             "kind": "误检", "expect": "—", "actual": v})
    return rows[:limit]
