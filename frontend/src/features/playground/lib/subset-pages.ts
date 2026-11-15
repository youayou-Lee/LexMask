// Copyright 2026 LexMask Contributors

import type { ItemPageStatus } from '@/services/jobsApi';

export interface SubsetSelection {
  pages: number[];
  total: number;
}

/** Issue#327：识别未完成时，取「已落定页」作为子集预览范围；全卷完成返回空。 */
export function pickSubsetPages(
  status: ItemPageStatus | null,
  fallbackTotal: number,
): SubsetSelection {
  const total = status?.page_count || fallbackTotal || 0;
  const pages = (status?.pages ?? [])
    .filter((cell) => cell.state === 'success' || cell.state === 'manual_verified')
    .map((cell) => cell.page)
    .sort((a, b) => a - b);
  if (total > 0 && pages.length > 0 && pages.length < total) return { pages, total };
  return { pages: [], total };
}
