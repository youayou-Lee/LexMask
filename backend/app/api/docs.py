"""文档中心 API —— 只读 Markdown 知识库。

内容由后端托管（`DOCS_ROOT`），前端运行时读取；改文件即生效。
鉴权由 main.py 的 include_router 依赖统一施加。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.models.docs_schemas import DocsDetailResponse, DocsListResponse
from app.services import docs_service

router = APIRouter()


@router.get("/docs", response_model=DocsListResponse)
async def list_docs() -> DocsListResponse:
    """分类树 + 索引页 slug；不返回已废弃文档。"""
    return DocsListResponse(**docs_service.list_docs())


@router.get("/docs/{slug}", response_model=DocsDetailResponse)
async def get_doc(slug: str) -> DocsDetailResponse:
    """单篇文档：正文（链接已解析）+ 前向链接 + 反链。"""
    if not docs_service.SLUG_RE.fullmatch(slug):
        raise HTTPException(status_code=404, detail="文档不存在")
    payload = docs_service.get_doc(slug)
    if payload is None:
        raise HTTPException(status_code=404, detail="文档不存在")
    return DocsDetailResponse(**payload)
