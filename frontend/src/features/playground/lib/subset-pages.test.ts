// Copyright 2026 LexMask Contributors
import { describe, expect, it } from 'vitest';
import {
  isRecognitionInFlight,
  mergeSubsetBoxes,
  outputPreviewPageForRequest,
  pickSubsetPages,
  subsetCandidates,
} from './subset-pages';
import type { ItemPageStatus } from '@/services/jobsApi';

function status(
  pages: { page: number; state: 'success' | 'manual_verified' | 'pending' | 'failed' }[],
  total: number,
  recognitionState: ItemPageStatus['recognition_state'] = 'running',
): ItemPageStatus {
  return { page_count: total, pages, recognition_state: recognitionState } as ItemPageStatus;
}

describe('pickSubsetPages (Issue#327)', () => {
  it('只取 success/manual_verified，按页号升序', () => {
    const out = pickSubsetPages(
      status(
        [
          { page: 3, state: 'success' },
          { page: 1, state: 'manual_verified' },
          { page: 2, state: 'pending' },
        ],
        5,
      ),
      5,
    );
    expect(out).toEqual({ pages: [1, 3], total: 5 });
  });

  it('manual_verified 页计入子集（人工补框优先于空识别结果）', () => {
    const out = pickSubsetPages(status([{ page: 7, state: 'manual_verified' }], 9), 9);
    expect(out.pages).toEqual([7]);
  });

  it('失败页不计入子集（即使 failed_pages 与页账同时给）', () => {
    const input = status(
      [
        { page: 1, state: 'success' },
        { page: 2, state: 'success' },
        { page: 3, state: 'failed' },
      ],
      4,
    );
    (input as { failed_pages?: number[] }).failed_pages = [3];
    expect(pickSubsetPages(input, 4).pages).toEqual([1, 2]);
  });

  it('页账总页数缺失/退化时回退 file_info 总页数，且不产生负/零页', () => {
    const out = pickSubsetPages(status([{ page: 0, state: 'success' }], 0), 86);
    expect(out).toEqual({ pages: [], total: 86 });
  });

  it('全卷已完成（success 页数 == 总页数）返回全集——由调用方判「未完成」跳过子集', () => {
    const out = pickSubsetPages(
      status(
        [
          { page: 1, state: 'success' },
          { page: 2, state: 'success' },
        ],
        2,
      ),
      2,
    );
    expect(out).toEqual({ pages: [1, 2], total: 2 });
  });
});

describe('isRecognitionInFlight / subsetCandidates (Issue#327 门⑥ I-1)', () => {
  it('识别在飞（running）才允许子集口径', () => {
    expect(isRecognitionInFlight('running')).toBe(true);
  });

  it.each(['not_started', 'done', 'failed', 'cancelled', null, undefined])(
    '非在飞状态不出现子集入口：%s',
    (state) => {
      expect(isRecognitionInFlight(state)).toBe(false);
    },
  );

  it('识别在飞 + 54/86 → 子集候选 = 已处理页', () => {
    const pages = Array.from({ length: 54 }, (_, i) => ({ page: i + 1, state: 'success' as const }));
    const out = subsetCandidates(status(pages, 86), 86);
    expect(out.pages).toHaveLength(54);
    expect(out.total).toBe(86);
  });

  it('识别已终态（awaiting_review）+ 失败页 → 空候选（走全卷链路，不静默降级）', () => {
    const out = subsetCandidates(
      status([{ page: 1, state: 'success' }], 86, 'done'),
      86,
    );
    expect(out.pages).toEqual([]);
  });

  it('页账缺失（轮询还没回来/失败）→ 空候选，不阻塞识别视图', () => {
    expect(subsetCandidates(null, 86)).toEqual({ pages: [], total: 86 });
  });
});

describe('mergeSubsetBoxes (Issue#327 门⑥ C-2)', () => {
  const snapshot = [
    { id: 'b1', page: 1, x: 0, y: 0, width: 1, height: 1 },
    { id: 'b2', page: 2, x: 0, y: 0, width: 1, height: 1 },
  ];

  it('画布为空 → 整份交回服务端快照', () => {
    expect(mergeSubsetBoxes(snapshot, [])).toEqual(snapshot);
  });

  it('画布碰过的页用画布框（整页覆盖），其余页保留快照框', () => {
    const canvas = [{ id: 'm1', page: 2, x: 5, y: 5, width: 2, height: 2 }];
    const merged = mergeSubsetBoxes(snapshot, canvas);
    expect(merged).toHaveLength(2);
    expect(merged.filter((box) => box.page === 2)).toEqual(canvas);
    expect(merged.filter((box) => box.page === 1)).toEqual([snapshot[0]]);
  });

  it('画布页不在快照里（新页/快照滞后）→ 追加而不是丢弃', () => {
    const canvas = [{ id: 'm1', page: 5, x: 0, y: 0, width: 1, height: 1 }];
    expect(mergeSubsetBoxes(snapshot, canvas)).toEqual([...snapshot, ...canvas]);
  });
});

describe('outputPreviewPageForRequest (Issue#327 门⑥ C-4)', () => {
  it('无 page_map → 原样（前缀子集/非子集预览）', () => {
    expect(outputPreviewPageForRequest(3)).toBe(3);
    expect(outputPreviewPageForRequest(3, null)).toBe(3);
  });

  it('page_map 反查：子集序号 → 原卷页号', () => {
    const pageMap = { '2': 5, '1': 7, '3': 9 };
    expect(outputPreviewPageForRequest(1, pageMap)).toBe(7);
    expect(outputPreviewPageForRequest(3, pageMap)).toBe(9);
  });

  it('page_map 漂移/缺键 → 退回子集序号（不 404 到 0）', () => {
    expect(outputPreviewPageForRequest(4, { '1': 7 })).toBe(4);
    expect(outputPreviewPageForRequest(4, { '4': 0 })).toBe(4);
  });
});