// Copyright 2026 LexMask Contributors

// Issue#257 一期：页级勾选格 + 部分导出对话框（前端接线判据）。
//
// 钉住三件产品行为：
//  - 页格三态可见，失败页不可选（#199 红线在「选择层」就挡住，不靠后端兜底）；
//  - 所选页范围里含失败页时必须显式确认（ack），未确认提交按钮禁用、点击不发请求；
//  - 导出记录从后端列表渲染并给出可下载链接（跨重启仍在，见后端扫盘口径）。
//
// @vitest-environment jsdom

import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { ItemPageStatus } from '@/services/jobsApi';

const hookState = vi.hoisted(() => ({
  status: null as ItemPageStatus | null,
  error: null as string | null,
}));

vi.mock('../hooks/use-batch-page-status', () => ({
  useBatchPageStatus: () => ({
    status: hookState.status,
    error: hookState.error,
    loading: false,
    refresh: vi.fn(),
  }),
}));

vi.mock('@/hooks/use-service-health', () => ({
  useServiceHealth: () => ({ health: null, refresh: vi.fn() }),
}));

vi.mock('@/services/jobsApi', () => ({
  getItemPageStatus: vi.fn(),
  startPartialExport: vi.fn(),
  getPartialExport: vi.fn(),
  listItemExports: vi.fn(),
}));

import { listItemExports, startPartialExport } from '@/services/jobsApi';
import { FileType } from '@/types';
import type { BatchRow } from '../types';
import { BatchPageMatrix } from './batch-page-matrix';
import { BatchStep3Recognize } from './batch-step3-recognize';
import { BatchRowPages } from './batch-row-pages';
import { PartialExportDialog } from './partial-export-dialog';

const mockedStart = vi.mocked(startPartialExport);
const mockedList = vi.mocked(listItemExports);

function makeStatus(overrides: Partial<ItemPageStatus> = {}): ItemPageStatus {
  return {
    item_id: 'item-1',
    job_id: 'job-1',
    file_id: 'file-1',
    paged: true,
    page_count: 6,
    recognition_state: 'running',
    snapshot_at: '2026-11-11T00:00:00+00:00',
    pages: [
      { page: 1, state: 'success', duration_ms: 1200, cache_hit: false },
      { page: 2, state: 'success', duration_ms: 900, cache_hit: false },
      { page: 3, state: 'success', duration_ms: 800, cache_hit: true },
      { page: 4, state: 'failed', reason: 'page_timeout' },
      { page: 5, state: 'success', duration_ms: 1000, cache_hit: false },
      { page: 6, state: 'pending' },
    ],
    failed_pages: [4],
    ...overrides,
  };
}

beforeEach(() => {
  hookState.status = makeStatus();
  hookState.error = null;
  mockedStart.mockReset();
  mockedList.mockReset();
  mockedList.mockResolvedValue({ exports: [], unavailable: [] });
  mockedStart.mockResolvedValue({
    export_id: 'exp-1',
    kind: 'partial_redaction',
    title: '测试卷-部分导出(第1-5页)',
    status: 'queued',
    error: null,
    progress: { stage: '', current: 0, total: 0 },
    volumes: [],
    total_bytes: 0,
    file_count: 1,
    created_at: '2026-11-11T00:00:00+00:00',
    finished_at: null,
  });
});

afterEach(() => cleanup());

/** 对话框打开时会立刻拉一次导出记录；把它放进 act 里渲染，避免异步 setState 警告。 */
async function renderDialog(props: {
  open: boolean;
  jobId: string;
  itemId: string;
  filename: string;
  initialSelected?: number[] | null;
  onClose: () => void;
}) {
  await act(async () => {
    render(<PartialExportDialog {...props} />);
  });
}

describe('页格矩阵（Issue#257）', () => {
  it('渲染三态计数，失败页不可点', () => {
    const onChange = vi.fn();
    render(
      <BatchPageMatrix status={makeStatus()} selected={new Set([1, 2, 3, 5])} onChange={onChange} />,
    );

    const summary = screen.getByTestId('page-matrix-summary');
    expect(summary.textContent).toContain('4');
    expect(summary.textContent).toContain('1');
    expect(summary.textContent).toContain('1');

    expect(screen.getByTestId('page-cell-1').getAttribute('data-state')).toBe('success');
    expect(screen.getByTestId('page-cell-4').getAttribute('data-state')).toBe('failed');
    expect(screen.getByTestId('page-cell-6').getAttribute('data-state')).toBe('pending');

    // 失败页点击不进选择：离线红叉不能被勾进导出。
    fireEvent.click(screen.getByTestId('page-cell-4'));
    expect(onChange).not.toHaveBeenCalled();

    // 失败页的 hover 原因（title 属性承载，指针设备可读）。
    expect(screen.getByTestId('page-cell-4').getAttribute('title')).toContain('page_timeout');
  });

  it('全选只选成功页，不会把失败/未跑页塞进选择', () => {
    const onChange = vi.fn();
    render(<BatchPageMatrix status={makeStatus()} selected={new Set()} onChange={onChange} />);

    fireEvent.click(screen.getByTestId('page-matrix-select-all'));
    expect(onChange).toHaveBeenCalledTimes(1);
    const next = onChange.mock.calls[0][0] as Set<number>;
    expect([...next].sort((a, b) => a - b)).toEqual([1, 2, 3, 5]);
  });
});

describe('部分导出对话框（Issue#257）', () => {
  it('默认全选成功页；区间含失败页时未勾选确认按钮禁用且不发请求', async () => {
    await renderDialog({
      open: true,
      jobId: 'job-1',
      itemId: 'item-1',
      filename: '测试卷.pdf',
      onClose: () => {},
    });

    expect(screen.getByTestId('partial-export-ack')).toBeTruthy();
    const submit = screen.getByTestId('partial-export-submit') as HTMLButtonElement;
    expect(submit.disabled).toBe(true);

    fireEvent.click(submit);
    expect(mockedStart).not.toHaveBeenCalled();
  });

  it('勾选确认后可提交，请求带升序页号与 ack=true', async () => {
    await renderDialog({
      open: true,
      jobId: 'job-1',
      itemId: 'item-1',
      filename: '测试卷.pdf',
      initialSelected: [5, 1, 2, 3],
      onClose: () => {},
    });

    fireEvent.click(screen.getByTestId('partial-export-ack'));
    const submit = screen.getByTestId('partial-export-submit') as HTMLButtonElement;
    expect(submit.disabled).toBe(false);

    fireEvent.click(submit);
    await waitFor(() => expect(mockedStart).toHaveBeenCalledTimes(1));
    expect(mockedStart).toHaveBeenCalledWith('job-1', 'item-1', [1, 2, 3, 5], true);
  });

  it('导出记录渲染下载链接（跨重启记录来自后端列表）', async () => {
    mockedList.mockResolvedValue({
      exports: [
        {
          export_id: 'abc123',
          kind: 'partial_redaction',
          title: '测试卷-部分导出(第1-5页)',
          status: 'completed',
          error: null,
          progress: { stage: '', current: 0, total: 0 },
          volumes: [{ name: 'part001.zip', size_bytes: 1024 }],
          total_bytes: 1024,
          file_count: 1,
          created_at: '2026-11-11T00:00:00+00:00',
          finished_at: '2026-11-11T00:00:30+00:00',
        },
      ],
      unavailable: [],
    });

    await renderDialog({
      open: true,
      jobId: 'job-1',
      itemId: 'item-1',
      filename: '测试卷.pdf',
      onClose: () => {},
    });

    const record = await screen.findByTestId('partial-export-record-abc123');
    const link = record.querySelector('a');
    expect(link).toBeTruthy();
    expect(link?.getAttribute('href')).toContain('/files/batch/export/abc123/volumes/part001.zip');
  });

  it('不可分页类型给出明确不支持文案而不是空页格', async () => {
    hookState.status = makeStatus({ paged: false, pages: [], failed_pages: [] });

    await renderDialog({
      open: true,
      jobId: 'job-1',
      itemId: 'item-1',
      filename: '笔录.docx',
      onClose: () => {},
    });

    expect(screen.getByTestId('partial-export-unsupported')).toBeTruthy();
    expect(screen.queryByTestId('page-matrix')).toBeNull();
  });
});

describe('step3 行内展开（Issue#257）', () => {
  it('展开后挂载页格与导出入口', () => {
    const onExportPages = vi.fn();
    render(<BatchRowPages jobId="job-1" itemId="item-1" onExportPages={onExportPages} />);

    expect(screen.getByTestId('page-matrix-block')).toBeTruthy();
    fireEvent.click(screen.getByTestId('page-matrix-export'));
    expect(onExportPages).toHaveBeenCalledTimes(1);
    expect(onExportPages.mock.calls[0][0]).toEqual([1, 2, 3, 5]);
  });
});
describe('step3 接线（Issue#257）', () => {
  function row(overrides: Partial<BatchRow>): BatchRow {
    return {
      file_id: 'file-1',
      original_filename: '测试卷.pdf',
      file_size: 1024,
      file_type: FileType.PDF,
      has_output: false,
      entity_count: 0,
      analyzeStatus: 'ready',
      item_id: 'item-1',
      job_id: 'job-1',
      ...overrides,
    } as BatchRow;
  }

  function renderStep3(rows: BatchRow[]) {
    return render(
      <BatchStep3Recognize
        rows={rows}
        activeJobId="job-1"
        failedRows={[]}
        goStep={vi.fn()}
        submitQueueToWorker={vi.fn(async () => {})}
        requeueFailedItems={vi.fn(async () => {})}
      />,
    );
  }

  it('终局文案：识别完成但有失败页时给「完成(148/149，第 133 页失败)」', () => {
    renderStep3([
      row({
        analyzeStatus: 'awaiting_review',
        page_count: 149,
        recognitionFailedPages: [133],
      }),
    ]);

    const cell = screen.getByTestId('row-progress-file-1');
    expect(cell.textContent).toContain('完成(148/149');
    expect(cell.textContent).toContain('第 133 页失败');
  });

  it('无失败页/页数未知时不冒充终局文案', () => {
    renderStep3([
      row({ file_id: 'file-clean', analyzeStatus: 'awaiting_review', page_count: 149 }),
      row({
        file_id: 'file-unknown',
        item_id: 'item-2',
        analyzeStatus: 'awaiting_review',
        page_count: 0,
        recognitionFailedPages: [7],
      }),
    ]);

    expect(screen.getByTestId('row-progress-file-clean').textContent).not.toContain('完成(');
    expect(screen.getByTestId('row-progress-file-unknown').textContent).not.toContain('完成(');
  });

  it('可分页行有逐页入口，点击后挂载页格；docx 行没有', async () => {
    renderStep3([row({}), row({ file_id: 'file-2', file_type: FileType.DOCX, item_id: 'item-2' })]);

    expect(screen.getByTestId('page-matrix-toggle-file-1')).toBeTruthy();
    expect(screen.queryByTestId('page-matrix-toggle-file-2')).toBeNull();
    expect(screen.queryByTestId('page-matrix-block')).toBeNull();

    await act(async () => {
      fireEvent.click(screen.getByTestId('page-matrix-toggle-file-1'));
    });
    expect(screen.getByTestId('page-matrix-block')).toBeTruthy();

    await act(async () => {
      fireEvent.click(screen.getByTestId('page-matrix-toggle-file-1'));
    });
    expect(screen.queryByTestId('page-matrix-block')).toBeNull();
  });
});
