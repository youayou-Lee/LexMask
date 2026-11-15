// Issue#322：选页导出请求必须携带画布框快照（partial），full 忽略。
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { BoundingBox } from '@/features/playground/types';

vi.mock('./api-client', () => ({
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
  del: vi.fn(),
}));

import { post } from './api-client';
import { startItemExport } from './jobsApi';

const box = {
  id: 'b1',
  x: 0.1,
  y: 0.1,
  width: 0.2,
  height: 0.1,
  page: 1,
  type: 'PERSON',
  selected: true,
} as unknown as BoundingBox;

describe('startItemExport 请求体（Issue#322）', () => {
  beforeEach(() => {
    vi.mocked(post).mockReset();
    vi.mocked(post).mockResolvedValue({});
  });

  it('partial + boundingBoxes → 携带 bounding_boxes', async () => {
    await startItemExport('job-1', 'item-1', {
      kind: 'partial',
      pages: [1, 2],
      boundingBoxes: [box],
    });
    expect(vi.mocked(post)).toHaveBeenCalledWith(
      '/jobs/job-1/items/item-1/export',
      expect.objectContaining({ pages: [1, 2], bounding_boxes: [box] }),
    );
  });

  it('空框快照 → 不带 bounding_boxes（避免覆盖成空框账）', async () => {
    await startItemExport('job-1', 'item-1', { kind: 'partial', pages: [1], boundingBoxes: [] });
    const body = vi.mocked(post).mock.calls[0][1] as Record<string, unknown>;
    expect(body).not.toHaveProperty('bounding_boxes');
  });

  it('full → 不携带 pages / bounding_boxes', async () => {
    await startItemExport('job-1', 'item-1', {
      kind: 'full',
      pages: [1],
      boundingBoxes: [box],
    });
    const body = vi.mocked(post).mock.calls[0][1] as Record<string, unknown>;
    expect(body).not.toHaveProperty('pages');
    expect(body).not.toHaveProperty('bounding_boxes');
  });
});
