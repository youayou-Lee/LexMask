// Copyright 2026 LexMask Contributors
// @vitest-environment jsdom

// Issue#257 一期 · 单页重试的 UI 契约（AC2.3 / AC7）；
// Issue#289 拆分修订：红叉页点击只展开详情、不发重试——重试入口收敛到
// 详情面板内的显式按钮（page-detail-retry），格子本身不再触发 retry-pages。
//
// 钉四件事：
//  1) 点红叉页只展开详情面板（不触发重试、不进选择集），重试走面板内显式按钮；
//  2) 识别进行中（running）时面板可开但重试按钮禁用、批量重试按钮也禁用；
//  3) 「重试失败页」一次性提交全部失败页（批量按钮路径不变）；
//  4) 后端 409（item_busy 等）翻成人话而不是裸错误码；终态条目不给重试入口，
//     面板按钮与底部 hint 同口径写明终态原因（Issue#289 F4）。

import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ApiError } from '@/services/api-client';
import type { ItemPageStatus } from '@/services/jobsApi';

const retryItemPages = vi.fn();
const refresh = vi.fn();
const useBatchPageStatus = vi.fn();

// vi.mock 会被提升到文件顶部 ⇒ 工厂里只能 import 原模块，不能引用顶层 const。
vi.mock('@/services/jobsApi', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/services/jobsApi')>();
  return { ...actual, retryItemPages: (...args: unknown[]) => retryItemPages(...args) };
});

vi.mock('../hooks/use-batch-page-status', () => ({
  useBatchPageStatus: (...args: unknown[]) => useBatchPageStatus(...args),
}));

import { BatchRowPages } from './batch-row-pages';

function pageStatus(overrides: Partial<ItemPageStatus> = {}): ItemPageStatus {
  return {
    item_id: 'item-1',
    job_id: 'job-1',
    file_id: 'file-1',
    paged: true,
    page_count: 6,
    recognition_state: 'done',
    item_status: 'awaiting_review',
    snapshot_at: '2026-11-11T01:00:00Z',
    pages: [
      { page: 1, state: 'success' },
      { page: 2, state: 'success' },
      { page: 3, state: 'success' },
      { page: 4, state: 'failed', reason: 'ner boom' },
      { page: 5, state: 'failed', reason: 'page_timeout' },
      { page: 6, state: 'success' },
    ],
    failed_pages: [4, 5],
    ...overrides,
  };
}

function renderRows(status: ItemPageStatus) {
  useBatchPageStatus.mockReturnValue({ status, error: null, loading: false, refresh });
  return render(<BatchRowPages jobId="job-1" itemId="item-1" onExportPages={vi.fn()} />);
}

beforeEach(() => {
  retryItemPages.mockReset();
  refresh.mockReset();
  retryItemPages.mockResolvedValue({ queued_pages: [4] });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('单页重试（Issue#289 拆分后：格子只展开，重试在面板内按钮）', () => {
  it('点红叉页只展开详情面板，不触发重试', async () => {
    renderRows(pageStatus());

    const cell = screen.getByTestId('page-cell-4');
    expect(cell.getAttribute('data-state')).toBe('failed');
    expect(cell.getAttribute('data-retryable')).toBe('true');

    await act(async () => {
      fireEvent.click(cell);
    });

    // Issue#289：「看」与「重试」拆分——点击失败页不发重试请求。
    expect(retryItemPages).not.toHaveBeenCalled();
    // #199 红线（集成层代理）：失败页不进选择集（mock 级 onChange 断言在 issue289/issue279 文件）。
    expect(screen.getByTestId('page-cell-4').getAttribute('data-selected')).toBe('false');
    // 详情面板照常展开。
    const panel = screen.getByTestId('page-detail-panel');
    expect(panel.getAttribute('data-page')).toBe('4');
  });

  it('重试走详情面板内的显式按钮：提交该页并刷新', async () => {
    renderRows(pageStatus());

    await act(async () => {
      fireEvent.click(screen.getByTestId('page-cell-4'));
    });
    const button = screen.getByTestId('page-detail-retry');
    expect(button.hasAttribute('disabled')).toBe(false);

    await act(async () => {
      fireEvent.click(button);
    });

    await waitFor(() => expect(retryItemPages).toHaveBeenCalledWith('job-1', 'item-1', [4]));
    await waitFor(() => expect(refresh).toHaveBeenCalled());
  });

  it('点成功页只改选择，不触发重试', async () => {
    renderRows(pageStatus());

    await act(async () => {
      fireEvent.click(screen.getByTestId('page-cell-1'));
    });

    expect(retryItemPages).not.toHaveBeenCalled();
    expect(screen.getByTestId('page-cell-1').getAttribute('data-selected')).toBe('false');
  });

  it('识别进行中时红叉页面板可开但重试按钮禁用、批量按钮禁用', async () => {
    renderRows(pageStatus({ recognition_state: 'running', item_status: 'processing' }));

    const cell = screen.getByTestId('page-cell-4');
    expect(cell.getAttribute('data-retryable')).toBe('false');
    await act(async () => {
      fireEvent.click(cell);
    });
    // 看原因永远合法：面板照常展开；重试按钮禁用并写明忙态原因。
    expect(screen.getByTestId('page-detail-panel').getAttribute('data-page')).toBe('4');
    expect(screen.getByTestId('page-detail-retry').hasAttribute('disabled')).toBe(true);
    expect(screen.getByTestId('page-detail-retry-unavailable').textContent).toContain('需先完成识别');
    expect(retryItemPages).not.toHaveBeenCalled();

    expect(screen.getByTestId('page-matrix-retry-failed').hasAttribute('disabled')).toBe(true);
  });

  it('重试失败页一次提交全部失败页', async () => {
    renderRows(pageStatus());

    await act(async () => {
      fireEvent.click(screen.getByTestId('page-matrix-retry-failed'));
    });

    await waitFor(() => expect(retryItemPages).toHaveBeenCalledWith('job-1', 'item-1', [4, 5]));
  });

  it('409 item_busy 翻成中文提示', async () => {
    retryItemPages.mockRejectedValueOnce(
      new ApiError('busy', 409, 'server', 'item_busy', { error_code: 'item_busy' }),
    );
    renderRows(pageStatus());

    await act(async () => {
      fireEvent.click(screen.getByTestId('page-matrix-retry-failed'));
    });

    const error = await screen.findByTestId('page-retry-error');
    expect(error.textContent).toContain('正在识别或重试中');
    expect(error.textContent).not.toContain('item_busy');
  });

  it('未知错误码保留原文，不吞掉', async () => {
    retryItemPages.mockRejectedValueOnce(
      new ApiError('上游炸了', 500, 'server', 'boom_code'),
    );
    renderRows(pageStatus());

    await act(async () => {
      fireEvent.click(screen.getByTestId('page-matrix-retry-failed'));
    });

    const error = await screen.findByTestId('page-retry-error');
    expect(error.textContent).toContain('上游炸了');
  });

  it('已确认/已完成的条目不给重试入口（状态机没有回边，点了必 409）', async () => {
    renderRows(pageStatus({ item_status: 'completed' }));

    const cell = screen.getByTestId('page-cell-4');
    expect(cell.getAttribute('data-retryable')).toBe('false');
    await act(async () => {
      fireEvent.click(cell);
    });
    // 面板内按钮禁用，且终态原因与「识别中」的忙态提示区分（Issue#289 F4：底部 hint 同口径）。
    expect(screen.getByTestId('page-detail-retry').hasAttribute('disabled')).toBe(true);
    expect(screen.getByTestId('page-detail-retry-unavailable').textContent).toContain('终态');
    const hint = screen.getByTestId('page-matrix-failed-hint');
    expect(hint.textContent).toContain('终态');
    expect(hint.textContent).not.toContain('需先完成识别');
    const bulk = screen.getByTestId('page-matrix-retry-failed') as HTMLButtonElement;
    expect(bulk.disabled).toBe(true);

    await act(async () => {
      fireEvent.click(screen.getByTestId('page-detail-retry'));
      fireEvent.click(bulk);
    });
    expect(retryItemPages).not.toHaveBeenCalled();
  });
});