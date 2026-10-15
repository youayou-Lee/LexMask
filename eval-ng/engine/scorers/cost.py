"""成本 scorer：本期占位（token 用量未采集，YAGNI——设计 §7 只留协议口）。"""

from __future__ import annotations

from ..protocol import Answer


def score_cost(answers: list[Answer]) -> dict:
    return {
        "tokens_in": None,
        "tokens_out": None,
        "note": "token 用量采集未启用（协议留口）；latency 已由 speed scorer 承接",
    }
