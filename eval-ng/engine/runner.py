"""eval-ng 唯一执行路径：四元组（考卷×参赛者×指标组×闸门）runner。

跑前打印成本预估（考卷×参赛者×runs）；环境标注缺项硬报错（AC4）；
任一闸门 FAIL = 评测 FAIL。--ci 档跳过 baseline_gate（设计 §5）。
"""

from __future__ import annotations

import asyncio
import importlib
from dataclasses import dataclass, field

from .gates.base import GateResult
from .protocol import Answer, AnswerForm, EnvReport, ExamItem, validate_participant
from .scorers import entity_quality, speed, stability


class MissingEnvAnnotation(Exception):
    """AC4：环境标注缺硬件/版本/拓扑任一项。"""


@dataclass
class MetricGroup:
    name: str
    scorers: list[str] = field(default_factory=lambda: ["entity_quality", "speed", "stability"])
    gates: list[str] = field(default_factory=lambda: ["digital_veto"])
    runs: int = 3


def _gate_module(name: str):
    return importlib.import_module(f"{__package__}.gates.{name}")


async def run_exam(
    exam: list[ExamItem],
    participant,
    group: MetricGroup,
    env_report: EnvReport,
    *,
    ci_mode: bool = False,
    gate_env: dict | None = None,
    exam_id: str | None = None,
) -> dict:
    """执行一次评测。gate_env：闸门所需句柄/基线（baseline_metrics、mapping 所在 env 等）。"""
    validate_participant(participant)
    missing = env_report.missing_fields()
    if missing:
        raise MissingEnvAnnotation(
            f"环境标注缺项 {missing}（AC4：硬件/版本/拓扑任一缺失即拒绝评测）"
        )

    exam_id = exam_id or (exam[0].exam_id if exam else "unknown")
    n_items = len(exam)
    print(
        f"[eval-ng] 成本预估：考卷 {exam_id} 题数 {n_items} × 参赛者 {participant.name} "
        f"× runs={group.runs} = {n_items * group.runs} 次调用"
    )
    gate_env = gate_env or {}

    per_run_metrics: list[dict] = []
    last_records: list[dict] = []
    pages_detail: list[dict] = []
    gate_results: list = []
    first_run_pairs: list[tuple[ExamItem, Answer]] | None = None

    for run_idx in range(group.runs):
        pairs: list[tuple[ExamItem, Answer]] = []
        n_items = len(exam)
        for idx, item in enumerate(exam, 1):
            answer = await participant.probe(item)
            if answer.latency_sec is None:
                answer.latency_sec = 0.0
            # 逐题进度日志（用户反馈：长桶不再静默）
            print(f"  [{run_idx + 1}/{group.runs} {idx}/{n_items}] "
                  f"{item.id} {answer.latency_sec:.2f}s"
                  + (f" ⚠{answer.warnings[0][:60]}" if answer.warnings else ""),
                  flush=True)
            pairs.append((item, answer))
        if first_run_pairs is None:
            first_run_pairs = pairs

        records = [
            {"page_id": item.id, "gt": item.gt, "pred": answer.entities or {},
             "latency_sec": answer.latency_sec or 0.0}
            for item, answer in pairs
            if item.form is AnswerForm.ENTITY_TABLE and item.gt is not None
        ]
        if records:
            per_run_metrics.append(entity_quality.score_entity_quality(records))
            last_records = records

        if run_idx == 0:
            pages_detail = [
                {"page": item.id, "warmup": False, "wall_s": answer.latency_sec or 0.0}
                for item, answer in pairs
                if item.form in (AnswerForm.ENTITY_TABLE, AnswerForm.PERF_ONLY,
                                 AnswerForm.ARTIFACT_FILE)
            ]

    aggregated = stability.score_stability(per_run_metrics) if per_run_metrics else {"runs": 0}
    if per_run_metrics:
        aggregated["last_run_full"] = per_run_metrics[-1]
    perf = speed.score_speed(pages_detail, warmup_pages=1)

    # 闸门装配
    gate_names = [g for g in group.gates if not (ci_mode and g == "baseline_gate")]
    for gname in gate_names:
        gmod = _gate_module(gname)
        if gname == "digital_veto":
            if per_run_metrics:
                ok, failures = entity_quality.digital_veto(per_run_metrics[-1])
                gate_results.append(GateResult("digital_veto", ok, {"failures": failures}))
        elif gname == "robustness":
            for item, answer in (first_run_pairs or []):
                if item.form is AnswerForm.ROBUSTNESS:
                    gate_results.append(gmod.gate(answer, item, gate_env))
        elif gname == "leak_free":
            for item, answer in (first_run_pairs or []):
                if item.form is AnswerForm.ARTIFACT_FILE:
                    gate_results.append(gmod.gate(answer, item, gate_env))
        elif gname == "baseline_gate":
            gate_results.append(gmod.gate(None, None, gate_env))
        else:
            raise ValueError(f"未知闸门 {gname!r}")

    gate_pass = all(gr.passed for gr in gate_results) if gate_results else True
    return {
        "participant": participant.name,
        "exam_id": exam_id,
        "runs": group.runs,
        "per_run_metrics": per_run_metrics,
        "aggregated": aggregated,
        "perf": perf,
        "gates": gate_results,
        "gate_pass": gate_pass,
        "env": env_report,
        "errors": entity_quality.error_rows(last_records),
        "answers": [ans for _, ans in (first_run_pairs or [])],
    }


def run_exam_sync(*args, **kwargs) -> dict:
    return asyncio.run(run_exam(*args, **kwargs))
