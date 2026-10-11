"""闸门层公共结构。gate 签名 = gate(answer, item, env) -> GateResult（冷评审 C3）。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class GateResult:
    name: str
    passed: bool
    details: dict = field(default_factory=dict)
