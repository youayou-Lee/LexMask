// Copyright 2026 LexMask Contributors

// Issue#278 统一导出对话框（前端接线判据 §3.7）：
//  - 模式段切换（完整导出 / 选页导出）与缺省模式（宿主下发 defaultKind）；
//  - 说明.txt 开关默认关，勾选后请求带 include_summary；
//  - 错误码路由到专门文案（exportNotReady 等），未知码回落通用；
//  - 记录摘要双口径：新 kind 用包内 page_count、legacy partial_redaction 保留 +1 补偿（r2 F7）；
//  - 进度卡诚实口径：只报服务端真实 stage/current/total（#238）；
//  - legacy excluded int[] 升格不崩（normalizer 双形状兼容）。
//
// @vitest-environment jsdom

import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { ItemExportTask, ItemPageStatus } from '@/services/jobsApi';

const statusBox = vi.hoisted(() => ({ current: null as ItemPageStatus | null }));
const errorBox = vi.hoisted(() => ({ current: null as string | null }));

vi.mock('@/hooks/use-service-health', () => ({
  useServiceHealth: () => ({ health: null, refresh: vi.fn() }),
}));

vi.mock('../hooks/use-batch-page-status', () => ({
  useBatchPageStatus: () => ({
    status: statusBox.current,
    error: errorBox.current,
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
import { ApiError } from '@/services/api-client';
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

function makeTask(overrides: Partial<ItemExportTask> = {}): ItemExportTask {
  return {
    export_id: 'exp-1',
    kind: 'partial',
    title: '测试卷-部分导出(第1、3、5页)',
    status: 'queued',
    error: null,
    progress: { stage: 'queued', current: 0, total: 0 },
    volumes: [],
    total_bytes: 0,
    file_count: 1,
    created_at: '2026-11-15T00:00:00+00:00',
    finished_at: null,
    ...overrides,
  };
}

beforeEach(() => {
  statusBox.current = makeStatus();
  errorBox.current = null;
  mockedStart.mockReset();
  mockedList.mockReset();
  mockedList.mockResolvedValue({ exports: [], unavailable: [] });
  mockedStart.mockResolvedValue(makeTask());
});

afterEach(cleanup);

async function renderDialog(props: {
  open?: boolean;
  jobId?: string;
  itemId?: string;
  filename?: string;
  defaultKind?: 'full' | 'partial';
  initialSelected?: number[] | null;
  onBackToEdit?: () => void;
  onClose?: () => void;
}) {
  await act(async () => {
    render(
      <PartialExportDialog
        open={props.open ?? true}
        jobId={props.jobId ?? 'job-1'}
        itemId={props.itemId ?? 'item-1'}
        filename={props.filename ?? '测试卷.pdf'}
        defaultKind={props.defaultKind}
        initialSelected={props.initialSelected ?? null}
        onBackToEdit={props.onBackToEdit}
        onClose={props.onClose ?? (() => {})}
      />,
    );
  });
  await act(async () => {
    await Promise.resolve();
  });
}

describe('统一导出对话框（Issue#278）', () => {
  it('模式段缺省 = 选页导出；切到完整导出后提交不发 pages', async () => {
    await renderDialog({});

    // 选页导出模式下页格在场
    expect(screen.getByTestId('page-matrix-summary')).toBeTruthy();
    fireEvent.click(screen.getByTestId('export-mode-full'));

    const submit = screen.getByTestId('partial-export-submit') as HTMLButtonElement;
    expect(submit.disabled).toBe(false);
    fireEvent.click(submit);
    await waitFor(() => expect(mockedStart).toHaveBeenCalledTimes(1));
    expect(mockedStart).toHaveBeenCalledWith('job-1', 'item-1', {
      kind: 'full',
      pages: undefined,
      includeSummary: false,
    });
  });

  it('宿主下发 defaultKind="full" 时缺省即完整导出（step5 单文件入口）', async () => {
    await renderDialog({ defaultKind: 'full' });
    expect(screen.getByTestId('export-mode-full').getAttribute('aria-pressed') ?? '')
      .not.toContain('false');
    const submit = screen.getByTestId('partial-export-submit');
    expect(submit.textContent).not.toContain('选中页');
  });

  it('说明.txt 开关默认关；勾选后请求带 include_summary', async () => {
    await renderDialog({});
    const checkbox = screen.getByTestId('export-include-summary') as HTMLInputElement;
    expect(checkbox.checked).toBe(false);

    fireEvent.click(checkbox);
    expect(checkbox.checked).toBe(true);

    fireEvent.click(screen.getByTestId('partial-export-submit'));
    await waitFor(() => expect(mockedStart).toHaveBeenCalledTimes(1));
    expect(mockedStart).toHaveBeenCalledWith('job-1', 'item-1', {
      kind: 'partial',
      pages: [1, 2, 3, 5],
      includeSummary: true,
    });
  });

  it('已知错误码路由专门文案（export_not_ready），未知码回落通用', async () => {
    mockedStart.mockRejectedValue(
      new ApiError('not ready', 409, 'unknown', 'export_not_ready'),
    );
    await renderDialog({});
    fireEvent.click(screen.getByTestId('partial-export-submit'));
    const errorNode = await screen.findByTestId('partial-export-error');
    expect(errorNode.textContent).toContain('主产物');

    mockedStart.mockReset();
    mockedStart.mockRejectedValue(
      new ApiError('mystery', 400, 'unknown', 'totally_new_code'),
    );
    fireEvent.click(screen.getByTestId('partial-export-submit'));
    const errorNode2 = await screen.findByTestId('partial-export-error');
    expect(errorNode2.textContent).toContain('totally_new_code');
  });

  it('进度卡诚实口径：只渲染服务端真实 stage（rendering 显示 current/total）', async () => {
    mockedStart.mockReset();
    mockedStart.mockResolvedValueOnce(
      makeTask({ status: 'running', progress: { stage: 'rendering', current: 2, total: 3 } }),
    );
    await renderDialog({});
    fireEvent.click(screen.getByTestId('partial-export-submit'));
    const progress = await screen.findByTestId('export-progress');
    expect(progress.textContent).toContain('2');
    expect(progress.textContent).toContain('3');
    expect(progress.textContent).not.toContain('%');
  });

  it('completed 后展示每文件 sha256 尾 8 位', async () => {
    mockedStart.mockResolvedValueOnce(
      makeTask({
        status: 'completed',
        volumes: [{ name: 'part001.zip', size_bytes: 10 }],
        files: [{ name: '测试卷-脱敏.pdf', bytes: 9, sha256: 'a'.repeat(56) + '01234567' }],
      }),
    );
    await renderDialog({});
    fireEvent.click(screen.getByTestId('partial-export-submit'));
    const sha = await screen.findByTestId('export-sha256-测试卷-脱敏.pdf');
    expect(sha.textContent).toContain('01234567');
  });

  it('记录摘要双口径：新 kind 用包内页数、legacy partial_redaction 保留 +1 补偿', async () => {
    mockedList.mockResolvedValue({
      exports: [
        {
          export_id: 'newkind1',
          kind: 'partial',
          title: '测试卷-部分导出(第1、3、5页)',
          status: 'completed',
          progress: { stage: 'completed', current: 3, total: 3 },
          volumes: [{ name: 'part001.zip', size_bytes: 10 }],
          pages: [1, 3, 5],
          page_count: 3,
          excluded_failed_pages: [{ page: 4, reason_code: 'x' }],
          created_at: '2026-11-15T00:00:00+00:00',
        },
        {
          export_id: 'legacy2',
          kind: 'partial_redaction',
          title: '测试卷-部分导出(第1、3页)',
          status: 'completed',
          progress: { stage: 'completed', current: 2, total: 2 },
          volumes: [{ name: 'part001.zip', size_bytes: 10 }],
          pages: [1, 3],
          page_count: 12,
          // legacy 记录线上是 int[]，类型面是对象式：以 unknown 中转模拟旧形状。
          excluded_failed_pages: [4] as unknown as ItemExportTask['excluded_failed_pages'],
          created_at: '2026-11-14T00:00:00+00:00',
        },
      ],
      unavailable: [],
    });

    await renderDialog({});

    const fresh = await screen.findByTestId('partial-export-record-meta-newkind1');
    expect(fresh.textContent).toContain('本包 3 页'), '新 kind：page_count 即包内页数，无 +1';
    const legacy = await screen.findByTestId('partial-export-record-meta-legacy2');
    expect(legacy.textContent).toContain('本包 3 页'), 'legacy：pages.length(2) + 1 说明页 = 3';
    // kind 徽标分流
    expect(screen.getByTestId('partial-export-record-kind-newkind1').textContent).not.toBe(
      screen.getByTestId('partial-export-record-kind-legacy2').textContent,
    );
  });

  it('legacy excluded int[] 升格不崩且计数正确', async () => {
    mockedList.mockResolvedValue({
      exports: [
        {
          export_id: 'legacy3',
          kind: 'partial_redaction',
          title: '旧记录',
          status: 'completed',
          progress: { stage: 'completed', current: 1, total: 1 },
          volumes: [{ name: 'part001.zip', size_bytes: 10 }],
          pages: [1],
          page_count: 6,
          excluded_failed_pages: [4, 5] as unknown as ItemExportTask['excluded_failed_pages'],
          created_at: '2026-11-14T00:00:00+00:00',
        },
      ],
      unavailable: [],
    });
    await renderDialog({});
    const meta = await screen.findByTestId('partial-export-record-meta-legacy3');
    expect(meta.textContent).toContain('排除失败页 2 页');
  });
});

describe('选页导出场景：点页只改选择，不弹技术详情（Issue#314）', () => {
  it('点成功页只切换选择：DOM 无「第 N 页发生了什么」、无 route chip', async () => {
    statusBox.current = makeStatus({
      route_summary: { verdict: 'page_pipeline', fallback_reason_code: 'no_text_layer' },
    });
    await renderDialog({ defaultKind: 'partial' });

    const cell = screen.getByTestId('page-cell-1');
    const before = cell.getAttribute('aria-pressed');
    fireEvent.click(cell);

    // 选择真的变了（导出点页的主语义），但技术面板整块不出现。
    expect(cell.getAttribute('aria-pressed')).not.toBe(before);
    expect(screen.queryByTestId('page-detail-panel')).toBeNull();
    expect(screen.queryByTestId('page-route-chip')).toBeNull();
    expect(screen.queryByText(/第 1 页发生了什么/)).toBeNull();
  });

  it('点失败页不弹技术面板，失败页仍不可选', async () => {
    await renderDialog({ defaultKind: 'partial' });

    const failed = screen.getByTestId('page-cell-4');
    expect(failed.getAttribute('aria-pressed')).toBe('false');
    fireEvent.click(failed);
    expect(failed.getAttribute('aria-pressed')).toBe('false');
    expect(screen.queryByTestId('page-detail-panel')).toBeNull();
    // 错误状态仍可见（title 带失败原因），只是不展开诊断面板。
    expect(failed.getAttribute('title')).toContain('失败');
  });
});
