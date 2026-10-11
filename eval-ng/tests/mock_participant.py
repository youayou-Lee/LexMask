"""CI 用 mock participant：确定性假响应（录制语义），零真端点依赖。

行为可配：entities 直接回放构造时注入的表；延迟固定；鲁棒性条目按 expect_reject 应答。
"""

from __future__ import annotations

from ..engine.protocol import Answer, AnswerForm, ExamItem


class MockParticipant:
    name = "mock"
    answer_form = AnswerForm.ENTITY_TABLE
    supports = None

    def __init__(self, entities_by_item: dict[str, dict] | None = None,
                 latency_sec: float = 0.01):
        self.entities_by_item = entities_by_item or {}
        self.latency_sec = latency_sec

    async def probe(self, item: ExamItem) -> Answer:
        if item.form is AnswerForm.ROBUSTNESS:
            return Answer(form=AnswerForm.ROBUSTNESS,
                          rejected=bool(item.payload.get("expect_reject", True)),
                          latency_sec=self.latency_sec)
        if item.payload.get("mock_answer") is not None:
            return Answer(form=AnswerForm.ENTITY_TABLE,
                          entities=dict(item.payload["mock_answer"]),
                          latency_sec=self.latency_sec)
        entities = self.entities_by_item.get(item.id, dict(item.gt or {}))
        return Answer(form=AnswerForm.ENTITY_TABLE, entities=entities,
                      latency_sec=self.latency_sec)
