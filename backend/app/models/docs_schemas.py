"""文档中心响应模型（/api/v1/docs）。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class DocsNavItem(BaseModel):
    slug: str
    title: str
    section: str = ""
    summary: str = ""
    status: str
    order: int
    updated: str = ""


class DocsNavSection(BaseModel):
    category: str
    items: list[DocsNavItem]


class DocsListResponse(BaseModel):
    index: str | None = None
    sections: list[DocsNavSection] = Field(default_factory=list)


class DocsLink(BaseModel):
    slug: str
    title: str


class DocsDetailResponse(BaseModel):
    slug: str
    title: str
    category: str
    status: str
    updated: str = ""
    summary: str = ""
    content: str
    links: list[DocsLink] = Field(default_factory=list)
    backlinks: list[DocsLink] = Field(default_factory=list)
    canonical: str | None = None
