"""Issue#75 喂Agent模式：类型契约（状态机/文本段/映射项/任务上下文）。"""
from dataclasses import dataclass, field
from enum import Enum

from app.models.schemas import Entity


class TaskState(str, Enum):
    UPLOADED = "uploaded"
    PARSING = "parsing"
    NER_RUNNING = "ner_running"
    MAPPING_READY = "mapping_ready"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class Seg:
    """适配器输出的清洁文本段。source: text|table|img_ocr|sentinel。"""

    text: str
    page_idx: int
    source: str
    img_path: str = ""


@dataclass
class MappingItem:
    """映射表一行：原文 → 占位符。excluded=用户选择保留原文。"""

    id: str
    original_text: str
    entity_type: str
    replacement: str
    excluded: bool = False


@dataclass
class Chunk:
    """NER 分块：全局文本中的一段，base_offset 用于 Entity.start/end 平移。"""

    base_offset: int
    text: str
    page_idx: int


@dataclass
class AgentMdTask:
    """一个喂Agent任务的全部内存上下文（含密码不落盘：不存密码字段）。"""

    task_id: str
    state: TaskState
    stage: str = ""
    pages_done: int = 0
    pages_total: int = 0
    message: str = ""
    file_path: str = ""
    filename: str = ""
    segs: list[Seg] = field(default_factory=list)
    entities: list[Entity] = field(default_factory=list)
    mapping: list[MappingItem] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    output_file_id: str | None = None
    owner_id: str = "local_user"
