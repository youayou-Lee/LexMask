"""Issue#194 单文件识别任务的请求/响应 schema。"""

from __future__ import annotations

from typing import Any

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


class SingleRedactBody(BaseModel):
    """POST /jobs/{job_id}/items/{item_id}/redact 请求体。

    entities / bounding_boxes 为前端确认后的实体 seed（扁平 list，
    打码工人自适配 per-page dict）；config merge 进 job config_json，
    打码工人按 replacement_mode / custom_replacements / watermark_text 等
    既有键读取。pseudonym 在单文件允许（``_reject_batch_pseudonym`` 是批量守卫）。
    """

    entities: list[dict[str, Any]] = Field(default_factory=list)
    bounding_boxes: list[dict[str, Any]] = Field(default_factory=list)
    config: dict[str, Any] = Field(default_factory=dict)


class SingleRedactResult(BaseModel):
    """打码受理回执：item 已推进并入队打码。"""

    job_id: str
    item_id: str
    status: str
