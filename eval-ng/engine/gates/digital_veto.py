"""数字一票否决闸：四类数字 exact_rate 必须 = 1.0（口径透传 nerq）。"""

from __future__ import annotations

from ..protocol import Answer, AnswerForm, ExamItem
from ..scorers import entity_quality
from .base import GateResult


def gate(answer: Answer, item: ExamItem, env: dict) -> GateResult:
    if answer.form is not AnswerForm.ENTITY_TABLE:
        return GateResult("digital_veto", True, {"skipped": f"form={answer.form.value}"})
    if answer.entities is None:
        return GateResult("digital_veto", False, {"reason": "entity_table 答案缺 entities"})
    metrics = entity_quality.score_entity_quality(
        [{"page_id": item.id, "gt": item.gt or {}, "pred": answer.entities,
          "latency_sec": answer.latency_sec or 0.0}]
    )
    ok, failures = entity_quality.digital_veto(metrics)
    return GateResult(
        "digital_veto", ok, {"failures": failures, "metrics_digital": metrics.get("digital", {})}
    )
