"""HaS 参赛者：与旧 benchmark_t2 HttpEngine 同一调用路径（nerq.call_ner）。

对齐参数钉死（同题同分前提，设计 §3）：
- 类型传法：逐条按当页 GT 类型派生（payload["types"]，runner 填充）
- max_tokens=4000、timeout=120、grouping=off
- temperature/top_p 用 nerq 内置常量（build prompt / call_ner 全走 nerq）
"""

from __future__ import annotations

import httpx

from ..engine.protocol import Answer, AnswerForm, ExamItem
from ..engine.scorers._nerq_source import load_backend_nerq


class HasParticipant:
    name = "has"
    answer_form = AnswerForm.ENTITY_TABLE
    supports = None  # None = 全类型支持（N/A 桶语义沿旧 benchmark_t2）

    def __init__(self, base_url: str, model: str | None = None,
                 max_tokens: int = 4000, timeout: float = 120.0):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.max_tokens = max_tokens      # 对齐参数：4000
        self.timeout = timeout            # 对齐参数：120
        self._client = httpx.AsyncClient()

    async def probe(self, item: ExamItem) -> Answer:
        nerq = load_backend_nerq()
        types = list(item.payload.get("types") or [])
        try:
            entities, latency, warnings = await nerq.call_ner(
                self._client, self.base_url, self.model,
                item.payload.get("text", ""), types, self.max_tokens, self.timeout,
            )
        except Exception as exc:  # noqa: BLE001 — 单条异常不中断（同旧 HttpEngine）
            return Answer(form=self.answer_form, entities={}, latency_sec=0.0,
                          warnings=[f"error: {exc}"])
        return Answer(form=self.answer_form, entities=entities, latency_sec=latency,
                      warnings=list(warnings))

    async def aclose(self):
        await self._client.aclose()
