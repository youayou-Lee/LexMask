"""鲁棒性闸：期望拒绝的题（如加密 PDF）实际拒绝才 PASS；走 gate 不走 scorer（冷评审 C3）。"""

from __future__ import annotations

from ..protocol import Answer, AnswerForm, ExamItem
from .base import GateResult


def gate(answer: Answer, item: ExamItem, env: dict) -> GateResult:
    expect_reject = bool((item.payload or {}).get("expect_reject", True))
    if answer.form is not AnswerForm.ROBUSTNESS or answer.rejected is None:
        return GateResult(
            "robustness", False,
            {"reason": f"鲁棒性条目须答 ROBUSTNESS 形态（实际 {answer.form.value}）"},
        )
    passed = answer.rejected is expect_reject
    return GateResult(
        "robustness", passed,
        {"expect_reject": expect_reject, "actual_rejected": answer.rejected},
    )
