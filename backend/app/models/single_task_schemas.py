"""Issue#194 单文件识别任务的请求/响应 schema。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class RecognitionTaskBody(BaseModel):
    """POST /files/{file_id}/recognition-task 请求体。

    entity_type_ids 可为空：空列表交给识别管道按默认实体类型处理。
    ocr_has_types / visual_feature_types（Issue#194 终审 B）：扫描件/图片的视觉
    要素选择，原样写进 job config（识别工人视觉段按 resolve_optional_type_list
    读取）；None=未提供 ⇒ 不写键 ⇒ 编排器默认全类型集（与旧同步流
    VisionDetectRequest 的 selected_ocr_has_types/selected_visual_feature_types
    缺省语义一致）。空列表=用户显式清空该管道，原样保留。
    """

    entity_type_ids: list[str] = Field(default_factory=list)
    ocr_has_types: list[str] | None = None
    visual_feature_types: list[str] | None = None


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
