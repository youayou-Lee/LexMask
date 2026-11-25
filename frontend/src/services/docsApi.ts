// Copyright 2026 LexMask Contributors

import { get } from './api-client';

/** 文档中心 API（后端托管 Markdown；前端运行时读取）。 */

export interface DocsNavItem {
  slug: string;
  title: string;
  summary: string;
  status: 'index' | 'canonical' | 'summary' | 'draft' | 'deprecated' | string;
  order: number;
  updated: string;
}

export interface DocsNavSection {
  category: string;
  items: DocsNavItem[];
}

export interface DocsListResponse {
  index: string | null;
  sections: DocsNavSection[];
}

export interface DocsLink {
  slug: string;
  title: string;
}

export interface DocsDetail {
  slug: string;
  title: string;
  category: string;
  status: string;
  updated: string;
  summary: string;
  /** 正文（[[slug]] 已由后端解析为标准 Markdown 链接）。 */
  content: string;
  links: DocsLink[];
  backlinks: DocsLink[];
  canonical: string | null;
}

export function fetchDocsList(): Promise<DocsListResponse> {
  return get<DocsListResponse>('/docs');
}

export function fetchDoc(slug: string): Promise<DocsDetail> {
  return get<DocsDetail>(`/docs/${encodeURIComponent(slug)}`);
}