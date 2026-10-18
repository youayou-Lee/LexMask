"""Issue#194 单文件识别任务的请求/响应 schema。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class RecognitionTaskBody(BaseModel):
    """POST /files/{file_id}/recognition-task 请求体。

    entity_type_ids 可为空：空列表交给识别管道按默认实体类型处理。
    """

    entity_type_ids: list[str] = Field(default_factory=list)


class SingleTaskRef(BaseModel):
    """受理回执：后台 job / item / file 三元组。"""

    job_id: str
    item_id: str
    file_id: str
