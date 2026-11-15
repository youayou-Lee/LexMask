// Copyright 2026 LexMask Contributors
import { describe, expect, it } from 'vitest';
import { pickSubsetPages } from './subset-pages';
import type { ItemPageStatus } from '@/services/jobsApi';

function status(pages: { page: number; state: 'success' | 'manual_verified' | 'pending' | 'failed' }[], total: number): ItemPageStatus {
  return { page_count: total, pages } as ItemPageStatus;
}

describe('pickSubsetPages (Issue#327)', () => {
  it('只取 success/manual_verified，按页号升序', () => {
    const out = pickSubsetPages(status([{ page: 3, state: 'success' }, { page: 1, state: 'manual_verified' }, { page: 2, state: 'pending' }], 5), 5);
    expect(out).toEqual({ pages: [1, 3], total: 5 });
  });

  it('全卷完成（已处理页=总页数）不算子集', () => {
    expect(pickSubsetPages(status([{ page: 1, state: 'success' }, { page: 2, state: 'success' }], 2), 2)).toEqual({ pages: [], total: 2 });
  });

  it('状态未知时回退文件页数，且不产生子集', () => {
    expect(pickSubsetPages(null, 86)).toEqual({ pages: [], total: 86 });
  });

  it('失败页不计入子集', () => {
    expect(pickSubsetPages(status([{ page: 1, state: 'success' }, { page: 2, state: 'failed' }], 3), 3)).toEqual({ pages: [1], total: 3 });
  });
});
