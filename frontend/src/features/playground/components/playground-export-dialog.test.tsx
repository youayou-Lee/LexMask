// Copyright 2026 LexMask Contributors

// Issue#313 门⑥ I-2：「单文件导出弹窗」宿主接线判据。
// 之前宿主内联在 playground-page.tsx，全仓没有测试渲染它——删掉 onRequestClose 也全绿。
// 这里真渲染 Radix Dialog 外壳，钉住「点回到编辑 → 父弹窗关闭」。

// @vitest-environment jsdom

import { useState } from 'react';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { ItemPageStatus } from '@/services/jobsApi';

const statusBox = vi.hoisted(() => ({ current: null as ItemPageStatus | null }));

vi.mock('@/features/playground/hooks/use-file-job-item', () => ({
  useFileJobItem: () => ({ jobId: 'job-1', itemId: 'item-1' }),
}));

vi.mock('@/features/batch/hooks/use-batch-page-status', () => ({
  useBatchPageStatus: () => ({
    status: statusBox.current,
    error: null,
    loading: false,
    refresh: vi.fn(),
  }),
}));

vi.mock('@/services/jobsApi', () => ({
  getItemPageStatus: vi.fn(),
  startItemExport: vi.fn(),
  getItemExport: vi.fn(),
  listItemExports: vi.fn().mockResolvedValue({ exports: [], unavailable: [] }),
}));

import { PlaygroundExportDialog } from './playground-export-dialog';

function makeStatus(): ItemPageStatus {
  return {
    item_id: 'item-1',
    job_id: 'job-1',
    file_id: 'file-1',
    paged: true,
    page_count: 3,
    recognition_state: 'done',
    item_status: 'awaiting_review',
    snapshot_at: '2026-11-15T00:00:00+00:00',
    pages: [
      { page: 1, state: 'success' },
      { page: 2, state: 'success' },
      { page: 3, state: 'success' },
    ],
    failed_pages: [],
  };
}

function Harness() {
  const [open, setOpen] = useState(true);
  return (
    <PlaygroundExportDialog
      open={open}
      onOpenChange={setOpen}
      fileId="file-1"
    />
  );
}

beforeEach(() => {
  statusBox.current = makeStatus();
});
afterEach(cleanup);

describe('PlaygroundExportDialog（Issue#313 回到编辑宿主接线）', () => {
  it('打开时弹窗与页格可见', async () => {
    render(<Harness />);
    await act(async () => {
      await Promise.resolve();
    });
    expect(screen.getByTestId('playground-export-pages-dialog')).toBeTruthy();
    expect(screen.getByTestId('page-cell-1')).toBeTruthy();
  });

  it('点「回到编辑」→ onRequestClose → 父弹窗关闭（不是只滚一下背后的画布）', async () => {
    render(<Harness />);
    await act(async () => {
      await Promise.resolve();
    });

    fireEvent.click(screen.getByTestId('export-back-to-edit'));

    await waitFor(() => {
      expect(screen.queryByTestId('playground-export-pages-dialog')).toBeNull();
    });
    // 页格随弹窗一起卸载；选择集由 use-item-export 的卸载 cleanup 落 cache，重开恢复。
    expect(screen.queryByTestId('page-cell-1')).toBeNull();
  });
});