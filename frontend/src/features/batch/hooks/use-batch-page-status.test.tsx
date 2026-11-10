// Copyright 2026 LexMask Contributors

// Issue#257 一期：页级状态轮询契约（评审 Important-2 回归）。
//
// 钉住一件事：手动 refresh() 拿到「还在跑」的快照后必须**重新武装轮询**，
// 否则单页重试后行会永远停在 running、按钮全灰，只能折叠再展开才刷新。
//
// @vitest-environment jsdom

import { act, cleanup, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { ItemPageStatus } from '@/services/jobsApi';

const mockedGet = vi.hoisted(() => vi.fn());

vi.mock('@/services/jobsApi', () => ({
  getItemPageStatus: (...args: unknown[]) => mockedGet(...args),
}));

import { PAGE_STATUS_POLL_MS, useBatchPageStatus } from './use-batch-page-status';

function makeStatus(state: ItemPageStatus['recognition_state']): ItemPageStatus {
  return {
    item_id: 'item-1',
    job_id: 'job-1',
    file_id: 'file-1',
    paged: true,
    page_count: 3,
    recognition_state: state,
    item_status: state === 'running' ? 'processing' : 'awaiting_review',
    snapshot_at: '2026-11-11T00:00:00+00:00',
    pages: [
      { page: 1, state: 'success' },
      { page: 2, state: 'failed', reason: 'ocr' },
      { page: 3, state: 'pending' },
    ],
    failed_pages: [2],
  };
}

let refreshRef: (() => Promise<void>) | null = null;

function Probe() {
  const { status, refresh } = useBatchPageStatus({ jobId: 'job-1', itemId: 'item-1' });
  refreshRef = refresh;
  return <span data-testid="state">{status?.recognition_state ?? 'none'}</span>;
}

async function flush() {
  await act(async () => {
    await Promise.resolve();
  });
}

beforeEach(() => {
  vi.useFakeTimers();
  mockedGet.mockReset();
  refreshRef = null;
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe('useBatchPageStatus（Issue#257）', () => {
  it('终态后手动 refresh 拿到 running 会重新武装轮询', async () => {
    mockedGet.mockResolvedValueOnce(makeStatus('done'));
    await act(async () => {
      render(<Probe />);
    });
    await flush();
    expect(mockedGet).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId('state').textContent).toBe('done');

    // 终态下不会自己再轮询
    await act(async () => {
      await vi.advanceTimersByTimeAsync(PAGE_STATUS_POLL_MS * 2);
    });
    expect(mockedGet).toHaveBeenCalledTimes(1);

    // 点「重试」后 refresh 拿到 running
    mockedGet.mockResolvedValue(makeStatus('running'));
    await act(async () => {
      await refreshRef?.();
    });
    await flush();
    expect(screen.getByTestId('state').textContent).toBe('running');
    const afterRefresh = mockedGet.mock.calls.length;
    expect(afterRefresh).toBeGreaterThanOrEqual(2);

    // 继续跑：再过一轮轮询应有新请求（修复前这里恒为 afterRefresh）
    await act(async () => {
      await vi.advanceTimersByTimeAsync(PAGE_STATUS_POLL_MS);
    });
    await flush();
    expect(mockedGet.mock.calls.length).toBeGreaterThan(afterRefresh);
  });
});
