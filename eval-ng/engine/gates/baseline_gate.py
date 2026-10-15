"""基线三闸门（不进 CI）：透传 nerq.compare_with_baseline，附加环境一致性检查。"""

from __future__ import annotations

from ..protocol import Answer, ExamItem
from ..scorers import entity_quality
from .base import GateResult


def gate(answer: Answer, item: ExamItem, env: dict) -> GateResult:
    candidate = env.get("candidate_metrics")
    baseline = env.get("baseline_metrics")
    if not candidate or not baseline:
        return GateResult("baseline_gate", False, {"reason": "缺 candidate/baseline metrics"})
    comparison = entity_quality.compare_baseline(candidate, baseline)
    details = dict(comparison)
    baseline_env = baseline.get("env_label") if isinstance(baseline, dict) else None
    current_env = env.get("env_label")
    if baseline_env and current_env and baseline_env != current_env:
        details["env_mismatch"] = {"baseline": baseline_env, "current": current_env}
    return GateResult("baseline_gate", bool(comparison.get("gate_pass")), details)
