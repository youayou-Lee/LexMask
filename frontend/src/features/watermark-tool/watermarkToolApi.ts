// Copyright 2026 LexMask Contributors

// Issue#244：水印工具 API 层（base /api/v1，Bearer/cookie 由 api-client 统一带）。
// 后端契约（backend/app/api/files.py）：GET watermark-status（只读检测报告）/
// POST watermark-strip（摘除并产出净化副本）/ GET watermark-clean（净化件 PDF 流）。

import { get, post } from '@/services/api-client';

/** GET /files/{id}/watermark-status（字段守卫读取走 lib/watermark-tool 纯函数）。 */
export interface WatermarkStatusReport {
  file_id?: string;
  code: string;
  pages_probed?: number;
  pages_hit?: number;
  fonts?: string[];
  size_spread?: number;
  alpha_spread?: number;
  top_repeats?: string[];
  elapsed_ms?: number;
  error?: string | null;
}

/** POST /files/{id}/watermark-strip（clean_available 由 API 层补齐，见后端 files.py）。 */
export interface WatermarkStripResult {
  file_id?: string;
  code: string;
  source?: string;
  pages_probed?: number;
  pages_hit?: number;
  pages_stripped?: number;
  pages_skipped?: number;
  pages_guard_refused?: number;
  duration_ms?: number;
  error?: string | null;
  clean_available?: boolean;
}

export function getWatermarkStatus(fileId: string): Promise<WatermarkStatusReport> {
  return get<WatermarkStatusReport>(
    `/files/${encodeURIComponent(fileId)}/watermark-status`,
  );
}

export function stripWatermark(fileId: string): Promise<WatermarkStripResult> {
  return post<WatermarkStripResult>(`/files/${encodeURIComponent(fileId)}/watermark-strip`);
}

/** 净化件下载：downloadFile/authFetch 走原始路径（不走 axios baseURL），带 /api/v1 前缀。 */
export function watermarkCleanDownloadUrl(fileId: string): string {
  return `/api/v1/files/${encodeURIComponent(fileId)}/watermark-clean`;
}
