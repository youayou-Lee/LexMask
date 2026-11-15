// Copyright 2026 LexMask Contributors

// Issue#257 需求重对齐（2026-11-11）：单文件任务（/single）的「逐页结果与选页导出」块。
//
// 钉住用户点名的三件事：
//  - 单文件也有页格（不再只挂在批量任务上），非分页文件整块不出现；
//  - 识别进行中就能看到失败页，且失败页不可选；
//  - 没有「我已了解失败页」勾选框：区间内失败页自动排除后可直接导出。
//
// @vitest-environment jsdom

import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { ItemPageStatus } from '@/services/jobsApi';

const statusBox = vi.hoisted(() => ({ current: null as ItemPageStatus | null }));
// 选择集 cache 是模块级的：给每个用例独立 item，避免用例间互相污染。
const idsBox = vi.hoisted(() => ({ jobId: 'job-1', itemId: 'item-1' }));

vi.mock('../hooks/use-file-job-item', () => ({
  useFileJobItem: () => ({ jobId: idsBox.jobId, itemId: idsBox.itemId }),
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
  listItemExports: vi.fn(),
}));

import { listItemExports, startItemExport } from '@/services/jobsApi';
import { PlaygroundPageExport } from './playground-page-export';

const mockedStart = vi.mocked(startItemExport);
const mockedList = vi.mocked(listItemExports);

function makeStatus(overrides: Partial<ItemPageStatus> = {}): ItemPageStatus {
  return {
    item_id: 'item-1',
    job_id: 'job-1',
    file_id: 'file-1',
    paged: true,
    page_count: 6,
    recognition_state: 'running',
    item_status: 'processing',
    snapshot_at: '2026-11-11T00:00:00+00:00',
    pages: [
      { page: 1, state: 'success' },
      { page: 2, state: 'success' },
      { page: 3, state: 'success' },
      { page: 4, state: 'failed', reason: '视觉识别 page timeout after 6s' },
      { page: 5, state: 'success' },
      { page: 6, state: 'pending' },
    ],
    failed_pages: [4],
    ...overrides,
  };
}

beforeEach(() => {
  idsBox.jobId = 'job-1';
  idsBox.itemId = 'item-1';
  statusBox.current = makeStatus();
  mockedStart.mockReset();
  mockedList.mockReset();
  mockedList.mockResolvedValue({ exports: [], unavailable: [] });
});

afterEach(cleanup);

/** 记录加载是异步的：等一拍，避免 act 警告掩盖真实断言。 */
async function renderSettled(ui: React.ReactElement) {
  const view = render(ui);
  await act(async () => {
    await Promise.resolve();
  });
  return view;
}

describe('单文件任务的逐页结果与选页导出（Issue#257 需求重对齐）', () => {
  it('识别进行中也渲染页格：三态计数可见、失败页要不要确认看一眼就知道', async () => {
    await renderSettled(<PlaygroundPageExport fileId="file-1" />);

    const counts = screen.getByTestId('playground-page-export-counts').textContent ?? '';
    expect(counts).toContain('4'); // 成功 4
    expect(counts).toContain('1'); // 失败 1
    // 失败页不可选（#199 红线在选择层就挡住）。
    expect(screen.getByTestId('page-cell-4').getAttribute('aria-pressed')).toBe('false');
    expect(screen.queryByTestId('partial-export-ack')).toBeNull();
  });

  it('没有勾选框：区间含失败页也可直接提交，请求只带升序页号', async () => {
    await renderSettled(<PlaygroundPageExport fileId="file-1" />);

    const submit = screen.getByTestId('playground-page-export-submit') as HTMLButtonElement;
    expect(submit.disabled).toBe(false);
    fireEvent.click(submit);

    await waitFor(() => expect(mockedStart).toHaveBeenCalledTimes(1));
    expect(mockedStart).toHaveBeenCalledWith('job-1', 'item-1', { kind: 'partial', pages: [1, 2, 3, 5], includeSummary: false });
    // 说明页会逐条列出被排除的页，底栏只给一行汇总。
    expect(screen.getByTestId('playground-page-export-excluded').textContent).toContain('4');
  });

  it('底栏汇总不留占位符，且产物口径是 zip（Issue#313 去掉旧说明页文案）', async () => {
    await renderSettled(<PlaygroundPageExport fileId="file-1" />);

    const summary = screen.getByTestId('playground-page-export-summary').textContent ?? '';
    // 真实 UI 事故：只 replace 首发时会漏出 `{count}`；这里同时钉住占位符全替换。
    expect(summary).not.toContain('{');
    expect(summary).not.toContain('}');
    // #278 起说明页不再插入 PDF，产物是 zip；旧「说明页 1 页」文案必须消失。
    expect(summary).not.toContain('说明页 1 页');
    expect(summary).toContain('zip');
    expect(summary).toContain('4');
  });

  it('非分页文件（docx/txt 之类）不渲染整块', async () => {
    statusBox.current = makeStatus({ paged: false, pages: [], failed_pages: [] });
    await renderSettled(<PlaygroundPageExport fileId="file-1" />);
    expect(screen.queryByTestId('playground-page-export')).toBeNull();
  });

  it('defaultCollapsed（结果页瘦身）默认收起，仍可展开；未传时保持展开', async () => {
    const collapsed = await renderSettled(<PlaygroundPageExport fileId="file-1" defaultCollapsed />);
    const toggle = screen.getByTestId('playground-page-export-toggle');
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
    expect(screen.queryByTestId('page-matrix')).toBeNull();
    fireEvent.click(toggle);
    expect(screen.getByTestId('page-matrix')).toBeTruthy();
    collapsed.unmount();

    await renderSettled(<PlaygroundPageExport fileId="file-1" />);
    expect(screen.getByTestId('playground-page-export-toggle').getAttribute('aria-expanded')).toBe(
      'true',
    );
  });

  it('compact（识别浮层）默认收起，点标题展开', async () => {
    await renderSettled(<PlaygroundPageExport fileId="file-1" compact />);

    const toggle = screen.getByTestId('playground-page-export-toggle');
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
    expect(screen.queryByTestId('page-matrix')).toBeNull();

    fireEvent.click(toggle);
    expect(toggle.getAttribute('aria-expanded')).toBe('true');
    expect(screen.getByTestId('page-matrix')).toBeTruthy();
  });
});

describe('单文件导出场景：点页只改选择（Issue#314）', () => {
  it('点成功页只切换选择，且不弹「第 N 页发生了什么」/route chip', async () => {
    statusBox.current = makeStatus({
      route_summary: { verdict: 'page_pipeline', fallback_reason_code: 'no_text_layer' },
    });
    await renderSettled(<PlaygroundPageExport fileId="file-1" />);

    const cell = screen.getByTestId('page-cell-1');
    const before = cell.getAttribute('aria-pressed');
    fireEvent.click(cell);

    expect(cell.getAttribute('aria-pressed')).not.toBe(before);
    expect(screen.queryByTestId('page-detail-panel')).toBeNull();
    expect(screen.queryByTestId('page-route-chip')).toBeNull();
    expect(screen.queryByText(/第 1 页发生了什么/)).toBeNull();
  });

  it('点失败页不弹技术面板；失败页仍不可选', async () => {
    await renderSettled(<PlaygroundPageExport fileId="file-1" />);

    const failed = screen.getByTestId('page-cell-4');
    expect(failed.getAttribute('aria-pressed')).toBe('false');
    fireEvent.click(failed);
    expect(failed.getAttribute('aria-pressed')).toBe('false');
    expect(screen.queryByTestId('page-detail-panel')).toBeNull();
    expect(failed.getAttribute('title')).toContain('失败');
  });
});

describe('选择集恢复（Issue#313 门⑥ I-1）', () => {
  it('回到编辑再重开：选择集按上次勾选恢复，不回落全选（Issue#313 门⑥ I-1）', async () => {
    idsBox.itemId = 'item-cache';
    const first = await renderSettled(<PlaygroundPageExport fileId="file-1" />);
    // 默认全选 4 页可导出页（1、2、3、5）；取消 1、2 → 只剩 3、5。
    fireEvent.click(screen.getByTestId('page-cell-1'));
    fireEvent.click(screen.getByTestId('page-cell-2'));
    expect(screen.getByTestId('page-cell-1').getAttribute('aria-pressed')).toBe('false');
    expect(screen.getByTestId('page-cell-2').getAttribute('aria-pressed')).toBe('false');
    expect(screen.getByTestId('page-cell-3').getAttribute('aria-pressed')).toBe('true');
    first.unmount();

    // 模拟 Dialog 关闭即卸载、再打开重新挂载。
    await renderSettled(<PlaygroundPageExport fileId="file-1" />);
    expect(screen.getByTestId('page-cell-1').getAttribute('aria-pressed')).toBe('false');
    expect(screen.getByTestId('page-cell-2').getAttribute('aria-pressed')).toBe('false');
    expect(screen.getByTestId('page-cell-3').getAttribute('aria-pressed')).toBe('true');
    expect(screen.getByTestId('page-cell-5').getAttribute('aria-pressed')).toBe('true');
  });
});
