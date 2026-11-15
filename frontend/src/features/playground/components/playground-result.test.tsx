// Copyright 2026 LexMask Contributors

// Issue#311：结果页默认视图瘦身。
//
// 钉住用户拍板的默认视图口径：
// 1) 深色「匿名化完成」横幅整块消失，右上角一行动作按钮 + 行内水印 badge；
// 2) 质量报告与处理详情默认不进 DOM（数据可以取，但不渲染）；
// 3) 「详情」按钮打开弹窗后，质量报告指标 + 处理详情行/总耗时/失败页齐全；
// 4) 失败页 / 未处理项存在时详情按钮带琥珀色告警点；
// 5) 预览容器吃掉主要高度（flex-1 min-h-0）。
// @vitest-environment jsdom

import { useState, type ComponentProps } from 'react';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { useSingleTask, type SingleTaskView } from '@/features/single-task/single-task-context';
import { getJob } from '@/services/jobsApi';
import { t } from '@/i18n';

import type { FileInfo } from '../types';
import { PlaygroundResult } from './playground-result';

vi.mock('@/features/single-task/single-task-context', () => ({ useSingleTask: vi.fn() }));
vi.mock('@/services/jobsApi', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/services/jobsApi')>()),
  getJob: vi.fn(),
}));
// 结果页的逐页导出块不在本文件的判据里，用轻量桩断言接线（默认收起由
// playground-page-export.test.tsx 自己钉）。
vi.mock('./playground-page-export', () => ({
  PlaygroundPageExport: ({
    fileId,
    defaultCollapsed,
  }: {
    fileId: string | null;
    defaultCollapsed?: boolean;
  }) => (
    <div
      data-testid="playground-page-export"
      data-file-id={fileId ?? ''}
      data-default-collapsed={defaultCollapsed ? 'true' : 'false'}
    />
  ),
}));

const mockedUseSingleTask = vi.mocked(useSingleTask);
const mockedGetJob = vi.mocked(getJob);

const FILE: FileInfo = {
  file_id: 'f1',
  filename: '第四卷.pdf',
  file_size: 1024,
  file_type: 'pdf',
  page_count: 86,
  vision_quality: {
    '1': {
      pipeline_status: {
        watermark_overlay: {
          code: 'hit',
          source: 'copy',
          pages_stripped: 3,
        },
      },
    },
  },
};

const REPORT = {
  total_entities: 10,
  redacted_entities: 7,
  coverage_rate: 70,
  entity_type_distribution: { PERSON: 6 },
  confidence_distribution: { high: 7, medium: 3 },
  source_distribution: { has: 6, regex: 4 },
  redaction_mode: 'mask',
};

/** 两成一败三页视觉件：failed_pages=[3]。 */
const PERFORMANCE = {
  recognition: {
    mode: 'vision',
    page_count: 3,
    duration_ms: 900,
    // 失败页清单是后端无条件写的独立键（Issue#199 契约），不是从 pages[*].status 推的。
    failed_pages: { '3': { reason: 'timeout' } },
    pages: {
      '1': { page: 1, status: 'completed', duration_ms: 400 },
      '2': { page: 2, status: 'completed', duration_ms: 300 },
      '3': { page: 3, status: 'failed', duration_ms: 200 },
    },
  },
  redaction: { duration_ms: 120, entity_count: 7, bounding_box_count: 9 },
};

function mockProvider(overrides: Partial<SingleTaskView> = {}) {
  const task = {
    jobId: 'j1',
    itemId: 'it1',
    fileId: 'f1',
    filename: '第四卷.pdf',
    phase: 'done',
    current: 1,
    total: 1,
    message: null,
    error: null,
    performance: null,
    ...overrides,
  } as SingleTaskView;
  mockedUseSingleTask.mockReturnValue({
    tasks: [],
    taskForFile: (fileId: string) => (fileId === 'f1' ? task : undefined),
    watch: vi.fn(),
    refresh: vi.fn(async () => undefined),
  });
}

function renderResult(props: Partial<ComponentProps<typeof PlaygroundResult>> = {}) {
  return render(
    <PlaygroundResult
      fileInfo={FILE}
      content="张三 借款 100 元"
      entities={[]}
      entityMap={{}}
      redactedCount={7}
      redactionReport={REPORT}
      reportOpen={false}
      setReportOpen={() => undefined}
      versionHistory={[]}
      versionHistoryOpen={false}
      setVersionHistoryOpen={() => undefined}
      isImageMode={false}
      imageUrl=""
      currentPage={1}
      totalPages={1}
      onPageChange={() => undefined}
      visibleBoxes={[]}
      visionTypes={[]}
      getVisionTypeConfig={() => ({ name: '', color: '' })}
      onBackToEdit={() => undefined}
      onReset={() => undefined}
      onDownload={() => undefined}
      {...props}
    />,
  );
}

/** 受控开关：验证弹窗真的能被 ESC 关闭。 */
function ControlledResult() {
  const [open, setOpen] = useState(false);
  return (
    <PlaygroundResult
      fileInfo={FILE}
      content="张三 借款 100 元"
      entities={[]}
      entityMap={{}}
      redactedCount={7}
      redactionReport={REPORT}
      reportOpen={open}
      setReportOpen={setOpen}
      versionHistory={[]}
      versionHistoryOpen={false}
      setVersionHistoryOpen={() => undefined}
      isImageMode={false}
      imageUrl=""
      currentPage={1}
      totalPages={1}
      onPageChange={() => undefined}
      visibleBoxes={[]}
      visionTypes={[]}
      getVisionTypeConfig={() => ({ name: '', color: '' })}
      onBackToEdit={() => undefined}
      onReset={() => undefined}
      onDownload={() => undefined}
    />
  );
}

beforeEach(() => {
  localStorage.clear();
  vi.clearAllMocks();
  mockProvider();
  mockedGetJob.mockResolvedValue({
    items: [{ file_id: 'f1', performance: PERFORMANCE }],
  } as unknown as Awaited<ReturnType<typeof getJob>>);
});

afterEach(cleanup);

describe('Issue#311 结果页默认视图', () => {
  it('深色完成横幅整块消失，右上角动作行保留三个既有入口 + 详情按钮', () => {
    renderResult();

    expect(screen.queryByText(t('playground.redactComplete'))).toBeNull();
    const actionBar = screen.getByTestId('playground-result-action-bar');
    expect(actionBar.className).toContain('justify-end');
    expect(actionBar.className).not.toContain('bg-foreground');
    expect(screen.getByTestId('playground-back-edit')).toBeTruthy();
    expect(screen.getByTestId('playground-new-file')).toBeTruthy();
    expect(screen.getByTestId('playground-download')).toBeTruthy();
    expect(screen.getByTestId('playground-details-button')).toBeTruthy();
  });

  it('水印 chip 降级为行内 badge 但仍可见（合规证据）', () => {
    renderResult();
    const chip = screen.getByTestId('playground-watermark-chip');
    expect(chip.textContent).toContain(t('batchWizard.step4.pipelineWatermarkOverlay'));
    expect(chip.textContent).toContain('3');
    // 不再是深色横幅里的浅字胶囊：落在动作行内、中性底色。
    expect(screen.getByTestId('playground-result-action-bar').contains(chip)).toBe(true);
  });

  it('默认 DOM 不渲染质量报告与处理详情（即使数据已取到）', async () => {
    renderResult();

    await waitFor(() => expect(mockedGetJob).toHaveBeenCalledWith('j1', { performance: true }));
    expect(screen.queryByTestId('playground-quality-report')).toBeNull();
    expect(screen.queryByTestId('processing-details-card')).toBeNull();
    expect(screen.queryByTestId('processing-details-title')).toBeNull();
    expect(screen.queryByTestId('processing-details-rows')).toBeNull();
    expect(screen.queryByTestId('processing-details-failed-pages')).toBeNull();
  });

  it('结果页与预览容器保持 flex-1 min-h-0，导出块默认收起且与视图切换同一行', () => {
    renderResult();
    const root = screen.getByTestId('playground-result');
    expect(root.className).toContain('flex-1');
    expect(root.className).toContain('min-h-0');
    const preview = screen.getByTestId('playground-result-preview');
    expect(preview.className).toContain('flex-1');
    expect(preview.className).toContain('min-h-0');
    expect(screen.getByTestId('playground-page-export').getAttribute('data-default-collapsed')).toBe(
      'true',
    );
    expect(screen.getByTestId('playground-result-view-toggle')).toBeTruthy();
  });
});

describe('Issue#311 详情入口与告警点', () => {
  it('无失败页、无未处理项 → 不出现告警点', async () => {
    mockedGetJob.mockResolvedValue({
      items: [
        {
          file_id: 'f1',
          performance: {
            recognition: {
              mode: 'vision',
              pages: { '1': { page: 1, status: 'completed', duration_ms: 10 } },
            },
          },
        },
      ],
    } as unknown as Awaited<ReturnType<typeof getJob>>);

    renderResult({
      redactionReport: { ...REPORT, redacted_entities: 10 },
    });
    await waitFor(() => expect(mockedGetJob).toHaveBeenCalled());

    expect(screen.queryByTestId('playground-details-alert')).toBeNull();
  });

  it('存在未处理项 → 详情按钮带琥珀色告警点', async () => {
    renderResult(); // REPORT: total 10 / redacted 7 → 3 个未处理
    await waitFor(() => expect(screen.getByTestId('playground-details-alert')).toBeTruthy());
    expect(screen.getByTestId('playground-details-alert').className).toContain('bg-amber-500');
  });

  it('存在失败页 → 详情按钮带告警点（即使报告全部处理完）', async () => {
    renderResult({ redactionReport: { ...REPORT, redacted_entities: 10 } });
    await waitFor(() => expect(screen.getByTestId('playground-details-alert')).toBeTruthy());
    // 信息不被隐藏：按钮 aria-label 明确告警含义。
    expect(screen.getByTestId('playground-details-button').getAttribute('aria-label')).toContain(
      t('playground.detailsAlert'),
    );
  });
});

describe('Issue#311 详情弹窗', () => {
  it('点击详情打开弹窗：质量报告指标 + 处理详情行/总耗时/失败页齐全', async () => {
    render(<ControlledResult />);

    fireEvent.click(screen.getByTestId('playground-details-button'));

    const report = screen.getByTestId('playground-quality-report');
    expect(report.textContent).toContain(t('playground.totalEntities'));
    expect(report.textContent).toContain('10');
    expect(report.textContent).toContain(t('playground.redactedEntities'));
    expect(report.textContent).toContain('7');
    expect(report.textContent).toContain(t('playground.unprocessedEntities'));
    expect(report.textContent).toContain('3');

    await waitFor(() => expect(screen.getByTestId('processing-details-title')).toBeTruthy());
    expect(screen.getByTestId('processing-details-total').textContent).toContain(
      t('processingDetails.total'),
    );
    expect(screen.getByTestId('processing-details-rows')).toBeTruthy();
    expect(screen.getByTestId('processing-row-ocr')).toBeTruthy();
    expect(screen.getByTestId('processing-details-failed-pages').textContent).toContain('3');
  });

  it('ESC 关闭弹窗，诊断内容重新从 DOM 消失', async () => {
    render(<ControlledResult />);
    fireEvent.click(screen.getByTestId('playground-details-button'));
    expect(screen.getByTestId('playground-details-dialog')).toBeTruthy();

    fireEvent.keyDown(document, { key: 'Escape', code: 'Escape' });

    await waitFor(() => expect(screen.queryByTestId('playground-details-dialog')).toBeNull());
    expect(screen.queryByTestId('playground-quality-report')).toBeNull();
    expect(screen.queryByTestId('processing-details-title')).toBeNull();
  });
});