// Copyright 2026 LexMask Contributors

// Issue#194 Task 11：处理详情卡组件（结果页）+ 三处一致性判据的测试化。
//
// 钉住的不变量：
// 1) 卡内每行与 buildProcessingRows(同 fixture) 输出逐项一致（行数据同源）；
// 2) PlaygroundLoading 的 performance prop 渲染同一行构建器的行（三处一致性的
//    第二消费形态；第三处=任务中心 T12 共用同一纯函数）；
// 3) 旧文件 / 无性能数据 → 不渲染卡（AC3 旧文件条款）；
// 4) 文本件显示「文本直读」注脚、无 OCR 行；
// 5) failed 变体：失败态 + 人话错误信息（结果页不触发，组件支持）；
// 6) PlaygroundProcessingDetails 解析链：provider taskForFile → 草稿 taskJobId →
//    getJob(jobId,{performance:true})；provider 账本清除后卡片凭已取数据留存。
// @vitest-environment jsdom

import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { STORAGE_KEYS } from '@/constants/storage-keys';
import { scopedStorageKey } from '@/lib/storage';
import { useSingleTask, type SingleTaskView } from '@/features/single-task/single-task-context';
import { getJob } from '@/services/jobsApi';
import { t } from '@/i18n';

import {
  buildProcessingRows,
  buildProcessingTotalMs,
  formatDurationMs,
} from '../lib/processing-details';
import { ProcessingDetailsCard, PlaygroundProcessingDetails } from './processing-details-card';
import { PlaygroundLoading } from './playground-loading';

vi.mock('@/features/single-task/single-task-context', () => ({ useSingleTask: vi.fn() }));
vi.mock('@/services/jobsApi', () => ({ getJob: vi.fn() }));

// —— fixture：与 lib 测试同源的三页视觉件（两成一败）——
const FULL_VISION_PERF = {
  queue: { last_task_type: 'recognition', last_wait_ms: 120 },
  recognition: {
    mode: 'vision',
    page_count: 3,
    queue_wait_ms: 120,
    parse_ms: 500,
    duration_ms: 4500,
    pages: {
      '1': {
        page: 1,
        duration_ms: 1500,
        status: 'completed',
        duration_breakdown_ms: {
          ocr_has: 1400,
          visual_features: 300,
          pdf_render_ms: 250,
          'ocr_has.ocr': 900,
          'ocr_has.has_ner': 200,
          'ocr_has.match': 80,
        },
      },
      '2': {
        page: 2,
        duration_ms: 1200,
        status: 'completed',
        duration_breakdown_ms: { ocr_has: 1100, pdf_render_ms: 150, 'ocr_has.ocr': 800, 'ocr_has.has_ner': 150, 'ocr_has.match': 60 },
      },
      '3': { page: 3, duration_ms: 900, status: 'failed', duration_breakdown_ms: { pdf_render_ms: 100 } },
    },
  },
  redaction: { duration_ms: 880, entity_count: 12, bounding_box_count: 34 },
};

const mockedUseSingleTask = vi.mocked(useSingleTask);
const mockedGetJob = vi.mocked(getJob);

beforeEach(() => {
  localStorage.clear();
  vi.clearAllMocks();
  mockedUseSingleTask.mockReturnValue({
    tasks: [],
    taskForFile: () => undefined,
    watch: vi.fn(),
    refresh: vi.fn(async () => undefined),
  });
});

afterEach(() => {
  cleanup();
});

function expectedRowParts(row: ReturnType<typeof buildProcessingRows>[number]): string[] {
  const parts = [t(row.labelKey)];
  if (row.durationMs !== null) parts.push(formatDurationMs(row.durationMs));
  if (row.counts !== null) parts.push(row.counts);
  return parts;
}

describe('ProcessingDetailsCard — 正常态', () => {
  it('卡内每行与 buildProcessingRows(同 fixture) 输出逐项一致 + 总耗时行', () => {
    const rows = buildProcessingRows(FULL_VISION_PERF);
    render(<ProcessingDetailsCard performance={FULL_VISION_PERF} />);

    expect(screen.getByTestId('processing-details-card')).toBeTruthy();
    expect(screen.getByTestId('processing-details-title').textContent).toBe(t('processingDetails.title'));

    for (const row of rows) {
      const el = screen.getByTestId(`processing-row-${row.key}`);
      for (const part of expectedRowParts(row)) {
        expect(el.textContent).toContain(part);
      }
      if (row.tone === 'warn') {
        expect(el.getAttribute('data-tone')).toBe('warn');
      }
    }
    const totalEl = screen.getByTestId('processing-details-total');
    expect(totalEl.textContent).toContain(t('processingDetails.total'));
    expect(totalEl.textContent).toContain(formatDurationMs(buildProcessingTotalMs(FULL_VISION_PERF)!));
  });

  it('文本件：无 OCR 行，显示「文本直读」注脚', () => {
    render(
      <ProcessingDetailsCard performance={{ recognition: { mode: 'text', parse_ms: 20, ner_ms: 40 } }} />,
    );
    expect(screen.queryByTestId('processing-row-ocr')).toBeNull();
    expect(screen.getByTestId('processing-details-text-direct').textContent).toBe(
      t('processingDetails.textDirect'),
    );
  });

  it('filePageCount 组件层合并：文本件解析行补页数', () => {
    render(
      <ProcessingDetailsCard
        performance={{ recognition: { mode: 'text', parse_ms: 20 } }}
        filePageCount={7}
      />,
    );
    expect(screen.getByTestId('processing-row-parse').textContent).toContain('7 页');
  });
});

describe('ProcessingDetailsCard — 失败态与无卡形态', () => {
  it('failed 变体：失败态 + 人话错误信息，不渲染阶段行', () => {
    render(
      <ProcessingDetailsCard performance={FULL_VISION_PERF} failed errorMessage="识别超时，请重试" />,
    );
    expect(screen.getByTestId('processing-details-failed').textContent).toContain(
      t('processingDetails.failed'),
    );
    expect(screen.getByTestId('processing-details-failed').textContent).toContain('识别超时，请重试');
    expect(screen.queryByTestId('processing-row-ocr')).toBeNull();
    expect(screen.queryByTestId('processing-details-total')).toBeNull();
  });

  it('旧文件（performance 全空/null）→ 不渲染卡（AC3 条款）', () => {
    const { container: emptyPerf } = render(<ProcessingDetailsCard performance={{}} />);
    expect(emptyPerf.innerHTML).toBe('');
    const { container: nullPerf } = render(<ProcessingDetailsCard performance={null} />);
    expect(nullPerf.innerHTML).toBe('');
    const { container: undefinedPerf } = render(<ProcessingDetailsCard />);
    expect(undefinedPerf.innerHTML).toBe('');
  });
});

describe('PlaygroundLoading performance prop — 第二消费形态（同一行构建器）', () => {
  it('进行中视图渲染与卡片同源的已产出阶段行', () => {
    render(
      <PlaygroundLoading
        loadingMessage="识别中"
        isImageMode={false}
        performance={FULL_VISION_PERF}
      />,
    );
    const container = screen.getByTestId('playground-processing-rows');
    expect(container).toBeTruthy();
    for (const row of buildProcessingRows(FULL_VISION_PERF)) {
      for (const part of expectedRowParts(row)) {
        expect(screen.getByTestId(`processing-row-${row.key}`).textContent).toContain(part);
      }
    }
    // 进行中视图不渲染卡片专属的标题/总耗时（那是结果页卡的形态）
    expect(screen.queryByTestId('processing-details-total')).toBeNull();
  });

  it('无 performance（旧流式识别）不渲染行区，行为不变', () => {
    render(<PlaygroundLoading loadingMessage="识别中" isImageMode={false} />);
    expect(screen.queryByTestId('playground-processing-rows')).toBeNull();
  });
});

describe('PlaygroundProcessingDetails — fileId→jobId 解析链', () => {
  function mockProviderTask(overrides: Record<string, unknown> = {}) {
    mockedUseSingleTask.mockReturnValue({
      tasks: [],
      taskForFile: (fileId: string) =>
        fileId === 'f1'
          ? ({
              jobId: 'j1',
              itemId: 'it1',
              fileId: 'f1',
              filename: 'a.pdf',
              phase: 'done',
              current: 1,
              total: 1,
              message: null,
              error: null,
              performance: null,
              ...overrides,
            } as SingleTaskView)
          : undefined,
      watch: vi.fn(),
      refresh: vi.fn(async () => undefined),
    });
  }

  it('provider taskForFile 命中 → getJob(performance) 取 item.performance 渲染卡片', async () => {
    mockProviderTask();
    mockedGetJob.mockResolvedValue({
      items: [{ file_id: 'f1', performance: FULL_VISION_PERF }],
    } as unknown as Awaited<ReturnType<typeof getJob>>);

    render(<PlaygroundProcessingDetails fileId="f1" filePageCount={null} />);

    await waitFor(() => expect(screen.getByTestId('processing-details-card')).toBeTruthy());
    expect(mockedGetJob).toHaveBeenCalledWith('j1', { performance: true });
    expect(screen.getByTestId('processing-row-redaction').textContent).toContain('不足 1 秒');
  });

  it('旧文件（provider 未命中且无草稿）→ 不渲染、不发请求', () => {
    render(<PlaygroundProcessingDetails fileId="f-old" filePageCount={null} />);
    expect(document.body.querySelector('[data-testid="processing-details-card"]')).toBeNull();
    expect(mockedGetJob).not.toHaveBeenCalled();
  });

  it('provider 账本清除后（终态短留到期），卡片凭已取数据留存', async () => {
    mockProviderTask();
    mockedGetJob.mockResolvedValue({
      items: [{ file_id: 'f1', performance: FULL_VISION_PERF }],
    } as unknown as Awaited<ReturnType<typeof getJob>>);

    const { rerender } = render(<PlaygroundProcessingDetails fileId="f1" filePageCount={null} />);
    await waitFor(() => expect(screen.getByTestId('processing-details-card')).toBeTruthy());

    // provider 10s 后清除条目：taskForFile 变为未命中
    mockedUseSingleTask.mockReturnValue({
      tasks: [],
      taskForFile: () => undefined,
      watch: vi.fn(),
      refresh: vi.fn(async () => undefined),
    });
    rerender(<PlaygroundProcessingDetails fileId="f1" filePageCount={null} />);
    expect(screen.getByTestId('processing-details-card')).toBeTruthy();
    // 不重复发请求（sticky jobId）
    expect(mockedGetJob).toHaveBeenCalledTimes(1);
  });

  it('provider 未命中时回落草稿 taskJobId（同文件才认）', async () => {
    mockedGetJob.mockResolvedValue({
      items: [{ file_id: 'f1', performance: FULL_VISION_PERF }],
    } as unknown as Awaited<ReturnType<typeof getJob>>);
    localStorage.setItem(
      scopedStorageKey(STORAGE_KEYS.PLAYGROUND_DRAFT),
      JSON.stringify({
        version: 1,
        savedAt: '2026-10-18T00:00:00Z',
        stage: 'result',
        fileInfo: { file_id: 'f1', filename: 'a.pdf', file_size: 1 },
        content: 'x',
        entities: [],
        boundingBoxes: [],
        taskJobId: 'j-draft',
      }),
    );

    render(<PlaygroundProcessingDetails fileId="f1" filePageCount={null} />);

    await waitFor(() => expect(screen.getByTestId('processing-details-card')).toBeTruthy());
    expect(mockedGetJob).toHaveBeenCalledWith('j-draft', { performance: true });
  });

  it('provider 报 failed → 卡切失败态并透出任务错误', async () => {
    mockProviderTask({ phase: 'failed', error: '识别超时' });
    mockedGetJob.mockResolvedValue({
      items: [{ file_id: 'f1', performance: null }],
    } as unknown as Awaited<ReturnType<typeof getJob>>);

    render(<PlaygroundProcessingDetails fileId="f1" filePageCount={null} />);
    await waitFor(() => expect(screen.getByTestId('processing-details-failed')).toBeTruthy());
    expect(screen.getByTestId('processing-details-failed').textContent).toContain('识别超时');
  });
});
