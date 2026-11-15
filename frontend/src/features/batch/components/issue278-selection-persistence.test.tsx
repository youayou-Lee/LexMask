// Copyright 2026 LexMask Contributors

// Issue#278「回到编辑保留选择集」（§3.3/§3.7）：
//  - 关弹窗不清空选择（写模块级 cache），重开时预填；
//  - 预填/剪枝与最新页状态取交集：失败页被摘出；
//  - 剪枝谓词 = success ∪ manual_verified（r2 F4：success-only 会把 manual 页
//    静默摘出选择集，与「manual 页默认包含」裁定冲突）；
//  - cache 无条目 → 缺省全选可导出页。
//
// @vitest-environment jsdom

import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { ItemPageStatus } from '@/services/jobsApi';

const statusBox = vi.hoisted(() => ({ current: null as ItemPageStatus | null }));

vi.mock('../hooks/use-batch-page-status', () => ({
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
  listItemExports: vi.fn(),
}));

import { listItemExports, startItemExport } from '@/services/jobsApi';
import { PartialExportDialog } from './partial-export-dialog';

const mockedStart = vi.mocked(startItemExport);
const mockedList = vi.mocked(listItemExports);

function makeStatus(overrides: Partial<ItemPageStatus> = {}): ItemPageStatus {
  return {
    item_id: 'item-1',
    job_id: 'job-1',
    file_id: 'file-1',
    paged: true,
    page_count: 6,
    recognition_state: 'done',
    item_status: 'awaiting_review',
    snapshot_at: '2026-11-15T00:00:00+00:00',
    pages: [
      { page: 1, state: 'success' },
      { page: 2, state: 'success' },
      { page: 3, state: 'success' },
      { page: 4, state: 'failed', reason: 'page_timeout' },
      { page: 5, state: 'success' },
      { page: 6, state: 'pending' },
    ],
    failed_pages: [4],
    ...overrides,
  };
}

beforeEach(() => {
  statusBox.current = makeStatus();
  mockedStart.mockReset();
  mockedList.mockReset();
  mockedList.mockResolvedValue({ exports: [], unavailable: [] });
});

afterEach(cleanup);

/** cache 是模块级：每个用例都换 item 维度隔离，避免用例间串台。 */
let seq = 0;

let lastView: ReturnType<typeof render> | null = null;

/** 同一用例内多轮挂载：先卸载上一棵树，避免 testid 撞多份。 */
async function renderDialog(props: {
  open: boolean;
  itemId?: string;
  initialSelected?: number[] | null;
  onClose?: () => void;
}) {
  const previous = lastView;
  if (previous) {
    act(() => previous.unmount());
    lastView = null;
  }
  const view = render(
    <PartialExportDialog
      open={props.open}
      jobId="job-1"
      itemId={props.itemId ?? `item-${seq}`}
      filename="测试卷.pdf"
      initialSelected={props.initialSelected ?? null}
      onClose={props.onClose ?? (() => {})}
    />,
  );
  lastView = view;
  await act(async () => {
    await Promise.resolve();
  });
  return view;
}

function selectedPagesFromCalls(): number[] {
  expect(mockedStart).toHaveBeenCalled();
  const request = mockedStart.mock.calls[0][2];
  return [...(request.pages ?? [])].sort((a, b) => a - b);
}

describe('选择集持久化与剪枝（Issue#278）', () => {
  it('关弹窗再开：已选页保留（cache 预填），改框回来不丢选择', async () => {
    seq += 1;
    const itemId = `item-${seq}`;
    await renderDialog({ open: true, itemId });

    // 取消页 1：选择集变成 [2,3,5]
    fireEvent.click(screen.getByTestId('page-cell-1'));
    fireEvent.click(screen.getByTestId('partial-export-submit'));
    await waitFor(() => expect(mockedStart).toHaveBeenCalledTimes(1));
    expect(selectedPagesFromCalls()).toEqual([2, 3, 5]);

    // 关弹窗（cache 写入）→ 重开 → 选择仍是 [2,3,5]，不是缺省全集。
    await renderDialog({ open: false, itemId });
    await renderDialog({ open: true, itemId });
    fireEvent.click(screen.getByTestId('partial-export-submit'));
    await waitFor(() => expect(mockedStart).toHaveBeenCalledTimes(2));
    expect(selectedPagesFromCalls()).toEqual([2, 3, 5]);
  });

  it('cache 无条目 → 缺省全选可导出页（失败/未跑页不进）', async () => {
    seq += 1;
    const itemId = `item-${seq}`;
    await renderDialog({ open: true, itemId });
    fireEvent.click(screen.getByTestId('partial-export-submit'));
    await waitFor(() => expect(mockedStart).toHaveBeenCalledTimes(1));
    expect(selectedPagesFromCalls()).toEqual([1, 2, 3, 5]);
  });

  it('剪枝谓词含 manual_verified：manual 页在剪枝后保留（r2 F4）', async () => {
    seq += 1;
    const itemId = `item-${seq}`;
    statusBox.current = makeStatus({
      pages: [
        { page: 1, state: 'success' },
        { page: 2, state: 'manual_verified' as ItemPageStatus['pages'][number]['state'] },
        { page: 3, state: 'success' },
        { page: 4, state: 'failed', reason: 'x' },
      ],
      failed_pages: [4],
      page_count: 4,
    });

    await renderDialog({ open: true, itemId, initialSelected: [1, 2, 3] });

    // 触发一次剪枝路径：点击已选页之外的可选页再取消（同一提交集不变）。
    // 直接提交：initialSelected 里 1/2/3 都在 exportable 谓词（success ∪ manual_verified）内。
    fireEvent.click(screen.getByTestId('partial-export-submit'));
    await waitFor(() => expect(mockedStart).toHaveBeenCalledTimes(1));
    expect(selectedPagesFromCalls()).toEqual([1, 2, 3]);
  });

  it('已选页一旦转失败立刻被剪出（状态轮询后的交集剪枝）', async () => {
    seq += 1;
    const itemId = `item-${seq}`;
    await renderDialog({ open: true, itemId, initialSelected: [1, 2, 3, 5] });

    // 页 5 转失败（重跑后仍失败的页不得留在选择里）
    statusBox.current = makeStatus({
      pages: [
        { page: 1, state: 'success' },
        { page: 2, state: 'success' },
        { page: 3, state: 'success' },
        { page: 4, state: 'failed', reason: 'x' },
        { page: 5, state: 'failed', reason: 'regressed' },
      ],
      failed_pages: [4, 5],
    });

    // 重开弹窗：重新初始化走 cache 预填 + 与最新页状态交集（页 5 摘出）。
    await renderDialog({ open: false, itemId });
    await renderDialog({ open: true, itemId });

    fireEvent.click(screen.getByTestId('partial-export-submit'));
    await waitFor(() => expect(mockedStart).toHaveBeenCalledTimes(1));
    expect(selectedPagesFromCalls()).toEqual([1, 2, 3]);
  });
});
