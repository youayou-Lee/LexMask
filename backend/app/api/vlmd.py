"""还原工具 API(Issue#70/#50 T7):POST /api/v1/vlmd/restore。

出入参均为真实姓名级敏感数据,挂 require_auth(评审 A9,与既有敏感路由一致)。
"""

import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.core.auth import require_auth
from app.services.restore_service import VALID_POLICIES, Mapping, normalize_mapping, restore

logger = logging.getLogger(__name__)

router = APIRouter()


class RestoreBody(BaseModel):
    text: str = Field(..., description="含占位符/化名的脱敏文本(Agent 结论等任意文本)")
    mapping: dict = Field(..., description="映射表 JSON(#66 产物,兼容新旧/退化格式)")
    policy: str = Field(default="safe", description="一对多策略:safe(默认,保留+候选)/first(取首条)")


class AmbiguousItem(BaseModel):
    key: str
    candidates: list[str]
    reason: str | None = None


class RestoreResponse(BaseModel):
    restored_text: str
    restored_count: int
    ambiguous: list[AmbiguousItem]
    unknown: list[str]
    hits: dict
    parse_warnings: list[str]


@router.post("/vlmd/restore", response_model=RestoreResponse)
async def restore_text(body: RestoreBody, owner_id: str = Depends(require_auth)):
    mapping: Mapping = normalize_mapping(body.mapping)
    if body.policy not in VALID_POLICIES:
        from fastapi import HTTPException

        raise HTTPException(status_code=400, detail=f"policy 必须是 {VALID_POLICIES} 之一")
    result = restore(body.text, mapping, policy=body.policy)
    return RestoreResponse(
        restored_text=result.restored_text,
        restored_count=result.restored_count,
        ambiguous=[AmbiguousItem(**a) for a in result.ambiguous],
        unknown=result.unknown,
        hits=result.hits,
        parse_warnings=mapping.parse_warnings,
    )
