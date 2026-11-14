// Copyright 2026 LexMask Contributors
// @vitest-environment jsdom

// Issue#280 · F3：部分导出选页白名单把 manual_verified 页与 success 页同口径。
//
// 钉住：
//  1) manual 页进预填（初始选择集 = success ∪ manual）；
//  2) manual 页不被剪枝（剪枝谓词只保留 success+manual，failed/pending 摘除）；
//  3) failed 页依旧排除（#199 红线在选择层就挡住）。

import { renderHook, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { ItemPageStatus } from '@/services/jobsApi';

const hookState = vi.hoisted(() => ({
  status: null as ItemPageStatus | null,
  error: null as string | null,
}));

vi.mock('./use-batch-page-status', () => ({
  useBatchPageStatus: () => ({
    status: hookState.status,
    error: hookState.error,
    loading: false,
    refresh: vi.fn(),
  }),
}));

vi.mock('@/services/jobsApi', () => ({
  getPartialExport: vi.fn(),
  listItemExports: vi.fn().mockResolvedValue({ exports: [], unavailable: [] }),
  startPartialExport: vi.fn(),
}));

import { usePartialExportSelection } from './use-partial-export-selection';

afterEach(() => {
  hookState.status = null;
  hookState.error = null;
  vi.clearAllMocks();
});

function makeStatus(): ItemPageStatus {
  return {
    item_id: 'item-280',
    job_id: 'job-280',
    file_id: 'file-280',
    paged: true,
    page_count: 4,
    recognition_state: 'done',
    item_status: 'awaiting_review',
    snapshot_at: '2026-11-15T00:00:00+00:00',
    pages: [
      { page: 1, state: 'success', cache_hit: false },
      { page: 2, state: 'manual_verified', cache_hit: false },
      { page: 3, state: 'failed', reason: 'boom', cache_hit: false },
      { page: 4, state: 'pending' },
    ],
    failed_pages: [3],
    manual_verified_pages: [
      { page: 2, box_count: 1, verified_at: 't', operator: 'op' },
    ],
    route_summary: null,
  };
}

describe('部分导出选页白名单含 manual_verified 页（Issue#280）', () => {
  it('预填选择集 = success ∪ manual；failed/pending 不进', async () => {
    hookState.status = makeStatus();
    const { result } = renderHook(() =>
      usePartialExportSelection({ jobId: 'job-280', itemId: 'item-280' }),
    );
    await waitFor(() => {
      expect(result.current.selected.size).toBe(2);
    });
    expect(result.current.selected.has(1)).toBe(true);
    expect(result.current.selected.has(2)).toBe(true);
    expect(result.current.selected.has(3)).toBe(false);
  });

  it('剪枝谓词同口径：已选 manual 页不被摘掉', async () => {
    hookState.status = makeStatus();
    const { result } = renderHook(() =>
      usePartialExportSelection({
        jobId: 'job-280',
        itemId: 'item-280',
        initialSelected: [1, 2, 3],
      }),
    );
    await waitFor(() => {
      expect(result.current.selected.size).toBe(2);
    });
    // manual 页 2 留在选中集；failed 页 3 被剪掉
    expect(result.current.selected.has(2)).toBe(true);
    expect(result.current.selected.has(3)).toBe(false);
    // orderedSelected 升序输出（提交面）
    expect(result.current.orderedSelected).toEqual([1, 2]);
  });
});
