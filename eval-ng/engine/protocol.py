"""eval-ng 参赛者申报制协议。

四元组模型（考卷×参赛者×指标组×闸门）中的参赛者层定义。
设计正本：LexMask-workspace/docs/plans/2026-10-11-eval-ng-redesign.md §3。
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from enum import Enum


class AnswerForm(str, Enum):
    """参赛者答案形态枚举（冷评审 C3 采纳）。"""

    ENTITY_TABLE = "entity_table"      # NER 引擎：类型→实体串列表
    TRANSCRIPT = "transcript"          # 解析引擎：转录文本（协议预留，GT 补齐后点亮）
    ARTIFACT_FILE = "artifact_file"    # 完整管线：成品文件路径
    ROBUSTNESS = "robustness"          # 鲁棒性：拒绝或接受
    PERF_ONLY = "perf_only"            # 纯耗时记录（gt=null 的速度条目）


@dataclass
class EnvReport:
    """环境自报。report 层硬校验（AC4）：hardware/stack_versions/topology 任一空即报错。"""

    hardware: str = ""
    stack_versions: dict[str, str] = field(default_factory=dict)
    topology: str = ""
    extra: dict = field(default_factory=dict)

    def missing_fields(self) -> list[str]:
        missing = []
        if not self.hardware or not self.hardware.strip():
            missing.append("hardware")
        if not self.stack_versions:
            missing.append("stack_versions")
        if not self.topology or not self.topology.strip():
            missing.append("topology")
        return missing


@dataclass
class ExamItem:
    """考卷一题（页/文件）。payload 结构随 form 不同：

    - ENTITY_TABLE: {"text": str, "types": list[str]}  types 由 manifest gt 派生，runner 填充
    - TRANSCRIPT:   {"text": str}
    - ARTIFACT_FILE:{"path": str, "sidecars": dict}
    - ROBUSTNESS:   {"path": str, "expect_reject": bool}
    - PERF_ONLY:    {"path": str}（或 {"text": str}）
    """

    id: str
    exam_id: str
    form: AnswerForm
    payload: dict = field(default_factory=dict)
    gt: dict | None = None  # {类型: [实体串]}；None = perf_only / 无 GT 阶段


@dataclass
class Answer:
    """参赛者答案制品。form 决定哪个字段有效；latency_sec 全形态必报。"""

    form: AnswerForm
    entities: dict | None = None      # ENTITY_TABLE: {类型: [串]}
    text: str | None = None           # TRANSCRIPT
    artifact_path: str | None = None  # ARTIFACT_FILE
    rejected: bool | None = None      # ROBUSTNESS
    latency_sec: float | None = None
    env: EnvReport | None = None      # 参赛者环境自报（可空，报告以 runner 传入的为准）
    warnings: list[str] = field(default_factory=list)


class ParticipantProtocolError(TypeError):
    """参赛者未满足申报制协议。"""


def validate_participant(participant) -> None:
    """鸭子类型校验：name / answer_form / supports / probe 均须存在。"""
    required_attrs = ["name", "answer_form", "supports"]
    for attr in required_attrs:
        if not hasattr(participant, attr):
            raise ParticipantProtocolError(
                f"participant {participant!r} 缺少属性 {attr!r}"
            )
    probe = getattr(participant, "probe", None)
    if not callable(probe):
        raise ParticipantProtocolError(f"participant {participant!r} 缺少可调用 probe()")
    sig = inspect.signature(probe)
    if len(sig.parameters) < 1:
        raise ParticipantProtocolError(
            f"participant {participant!r} 的 probe() 须接受 ExamItem 参数"
        )
