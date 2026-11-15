// Copyright 2026 LexMask Contributors

// @vitest-environment jsdom

// Issue#327（PR#329 门⑥ C-1~C-4 返工）：识别在飞时的「子集预览」链路。
//
// 需求原文（唯一口径）：只要有已经处理好的页，就可以开始匿名化；结果预览只包含这批页；
// 要有返回编辑按钮支持重新选页；手动拉框后直接导出也必须包含手改结果。
//
// 覆盖（评审 4 Critical + 1 Important 的返工判据）：
// (a) 识别在飞 + 54/86 已处理 ⇒ createSubsetPreview 收到 pages=[1..54]，
//     且**不发画布框**（画布为空 ⇒ 让后端吃 file_store 快照，否则扫描件必 400）；
//     不走 submitRedactionTask，stage 直接进 result；子集入口候选 54/86（C-1 的前置）。
// (b) 识别**已终态** + 失败页 ⇒ 绝不静默降级为子集，走既有全卷链路（I-1）。
// (c) 画布有手拉框 ⇒ 请求体 = 服务端快照框 ∪ 手拉框（手拉框所在页整页覆盖，C-2）。
// (d) 结果页下载 ⇒ URL 带 preview=true（C-3：闭包依赖必须含 subsetPreview）。

import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { useEffect, useRef } from 'react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { authFetch, downloadFile } from '@/services/api-client';
import { getJob, type JobDetail, type JobSummary, getItemPageStatus } from '@/services/jobsApi';
import {
  createSubsetPreview,
  listActiveSingleJobs,
  resolveAwaitingReviewTask,
  submitRecognitionTask,
  submitRedactionTask,
} from '@/services/singleTaskApi';
import { SingleTaskProvider, useSingleTask } from '@/features/single-task/single-task-context';

import { usePlayground } from './use-playground';

vi.mock('@/services/singleTaskApi', () => ({
  submitRecognitionTask: vi.fn(),
  submitRedactionTask: vi.fn(),
  createSubsetPreview: vi.fn(),
  listActiveSingleJobs: vi.fn(),
  resolveAwaitingReviewTask: vi.fn(),
}));
vi.mock('@/services/jobsApi', () => ({
  getJob: vi.fn(),
  getItemPageStatus: vi.fn(),
  cancelJob: vi.fn(),
  listJobs: vi.fn(),
}));
vi.mock('@/services/api-client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/services/api-client')>();
  return { ...actual, authFetch: vi.fn(), downloadFile: vi.fn() };
});
vi.mock('@/components/Toast', () => ({ showToast: vi.fn() }));
vi.mock('@/lib/notifications', () => ({ notifyDone: vi.fn() }));
vi.mock('@/features/auth/auth-context', () => ({
  useAuth: () => ({
    status: { authenticated: true, username: 'tester', multi_user: false },
    loading: false,
    error: null,
    refresh: async () => null,
    login: async () => {},
    setup: async () => {},
    register: async () => {},
    logout: async () => {},
  }),
}));
vi.mock('@/hooks/use-service-health', () => ({
  useServiceHealth: () => ({ health: null, checking: false }),
}));
vi.mock('@/services/hooks/use-presets', () => {
  const stablePresets: never[] = [];
  return {
    usePresets: () => ({ data: stablePresets }),
    useInvalidatePresets: () => async () => {},
  };
});

const CREATED = '2026-01-01T00:00:00Z';

/** 86 页扫描件卷（验收场景）：识别跑到 54 页时暂停。 */
const TOTAL_PAGES = 86;
const DONE_PAGES = 54;

function makeSummary(overrides: Partial<JobSummary> = {}): JobSummary {
  return {
    id: 'j1',
    job_type: 'single',
    title: '卷.pdf',
    status: 'processing',
    skip_item_review: false,
    config: {},
    error_message: null,
    created_at: CREATED,
    updated_at: CREATED,
    progress: {
      total_items: 1,
      pending: 0,
      processing: 1,
      queued: 0,
      parsing: 0,
      ner: 0,
      vision: 1,
      awaiting_review: 0,
      review_approved: 0,
      redacting: 0,
      completed: 0,
      failed: 0,
    },
    ...overrides,
  };
}

function makeDetail(
  jobStatus: string,
  itemStatus: string,
  overrides: Partial<JobDetail> = {},
): JobDetail {
  return {
    ...makeSummary({ status: jobStatus }),
    items: [
      {
        id: 'it1',
        job_id: 'j1',
        file_id: 'f1',
        sort_order: 0,
        status: itemStatus,
        progress_stage: 'vision',
        progress_current: DONE_PAGES,
        progress_total: TOTAL_PAGES,
        progress_message: null,
        error_message: null,
        created_at: CREATED,
        updated_at: CREATED,
      },
    ],
    ...overrides,
  } as JobDetail;
}

function jsonResponse(body: unknown): Response {
  return { ok: true, status: 200, json: async () => body } as unknown as Response;
}

/** 页账：前 N 页 success，其余 pending；recognition_state 由用例指定。 */
function pageStatus(
  done: number,
  recognitionState: 'running' | 'done' | 'failed' | 'cancelled',
  failedPages: number[] = [],
) {
  return {
    item_id: 'it1',
    job_id: 'j1',
    file_id: 'f1',
    paged: true,
    page_count: TOTAL_PAGES,
    recognition_state: recognitionState,
    item_status: recognitionState === 'done' ? 'awaiting_review' : 'processing',
    snapshot_at: CREATED,
    pages: Array.from({ length: TOTAL_PAGES }, (_, index) => ({
      page: index + 1,
      state: index < done ? 'success' : 'pending',
    })),
    failed_pages: failedPages,
  };
}

type PlaygroundApi = ReturnType<typeof usePlayground>;
type TaskApi = ReturnType<typeof useSingleTask>;

let harness: { pg: PlaygroundApi | null; task: TaskApi | null } = { pg: null, task: null };

function Probe() {
  const pg = usePlayground();
  const pgRef = useRef<PlaygroundApi | null>(null);
  pgRef.current = pg;
  useEffect(() => {
    harness.pg = pgRef.current;
  });
  return (
    <div>
      <span data-testid="stage">{pg.stage}</span>
      <span data-testid="subset-ready">{pg.subsetReadyCount}</span>
      <span data-testid="subset-total">{pg.subsetTotalPages}</span>
      <span data-testid="subset-preview">{pg.subsetPreview ? 'yes' : 'no'}</span>
      <button type="button" data-testid="redact-btn" onClick={() => void pg.handleRedact()}>
        redact
      </button>
      <button type="button" data-testid="download-btn" onClick={() => pg.handleDownload()}>
        download
      </button>
      <button
        type="button"
        data-testid="set-boxes"
        onClick={() =>
          pg.setBoundingBoxes([
            {
              id: 'm1',
              page: 2,
              x: 10,
              y: 10,
              width: 20,
              height: 20,
              type: 'custom_ocr_has_name',
              selected: true,
            },
          ])
        }
      >
        set-boxes
      </button>
    </div>
  );
}

function TaskProbe() {
  const task = useSingleTask();
  const taskRef = useRef<TaskApi | null>(null);
  taskRef.current = task;
  useEffect(() => {
    harness.task = taskRef.current;
  });
  return null;
}

function renderHarness() {
  return render(
    <MemoryRouter>
      <SingleTaskProvider>
        <Probe />
        <TaskProbe />
      </SingleTaskProvider>
    </MemoryRouter>,
  );
}

/** 铺现场：上传→解析（86 页扫描件）→ 自动识别任务提交（识别在飞，stage=preview）。 */
async function seedRecognizing() {
  renderHarness();
  await act(async () => {
    await harness.pg!.handleDecrypted('f1', '卷.pdf', 13);
  });
  await waitFor(() => expect(submitRecognitionTask).toHaveBeenCalledTimes(1));
  vi.mocked(getJob).mockResolvedValue(makeDetail('processing', 'processing'));
  // 手动跑一轮 provider 轮询：回填 itemId（placeholder 视图的 itemId 为空）
  await act(async () => {
    await harness.task!.refresh();
  });
  await waitFor(() => expect(harness.pg!.subsetReadyCount).toBe(DONE_PAGES));
}

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  harness = { pg: null, task: null };
  vi.mocked(submitRecognitionTask).mockResolvedValue({
    job_id: 'j1',
    item_id: 'it1',
    file_id: 'f1',
  });
  vi.mocked(submitRedactionTask).mockResolvedValue({
    job_id: 'j1',
    item_id: 'it1',
    status: 'queued',
  });
  vi.mocked(createSubsetPreview).mockResolvedValue({
    file_id: 'f1',
    processed_pages: Array.from({ length: DONE_PAGES }, (_, i) => i + 1),
    page_map: { '1': 1, '54': 54 },
    page_count: DONE_PAGES,
    redacted_count: 42,
  });
  vi.mocked(getJob).mockResolvedValue(makeDetail('processing', 'processing'));
  vi.mocked(getItemPageStatus).mockResolvedValue(pageStatus(DONE_PAGES, 'running') as never);
  vi.mocked(listActiveSingleJobs).mockResolvedValue([]);
  vi.mocked(resolveAwaitingReviewTask).mockResolvedValue(null);
  vi.mocked(downloadFile).mockResolvedValue(undefined);
  vi.spyOn(window, 'confirm').mockReturnValue(true);

  vi.mocked(authFetch).mockImplementation(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.endsWith('/files/f1/parse')) {
      return jsonResponse({
        content: '',
        file_type: 'pdf_scanned',
        is_scanned: true,
        page_count: TOTAL_PAGES,
      });
    }
    if (url.endsWith('/files/f1')) {
      return jsonResponse({
        original_filename: '卷.pdf',
        file_size: 13,
        entities: [],
        bounding_boxes: {
          1: [{ id: 's1', x: 1, y: 1, width: 5, height: 5, type: 'name', selected: true }],
          2: [{ id: 's2', x: 2, y: 2, width: 5, height: 5, type: 'name', selected: true }],
          3: [{ id: 's3', x: 3, y: 3, width: 5, height: 5, type: 'name', selected: true }],
        },
      });
    }
    if (url.includes('/preview-image')) {
      return new Response(new Blob(['x']), {
        status: 200,
        headers: { 'Content-Type': 'image/png' },
      });
    }
    throw new Error(`unexpected authFetch: ${url}`);
  });
});

afterEach(() => {
  cleanup();
});

describe('Issue#327：识别在飞时的子集预览链路', () => {
  it('(a) 54/86：入口候选可算（54/86）、createSubsetPreview 收到 pages=[1..54] 且不发画布框、不走全卷打码、stage=result', async () => {
    await seedRecognizing();
    expect(screen.getByTestId('subset-ready').textContent).toBe(String(DONE_PAGES));
    expect(screen.getByTestId('subset-total').textContent).toBe(String(TOTAL_PAGES));

    await act(async () => {
      fireEvent.click(screen.getByTestId('redact-btn'));
    });

    await waitFor(() => expect(createSubsetPreview).toHaveBeenCalledTimes(1));
    const [jobId, itemId, body] = vi.mocked(createSubsetPreview).mock.calls[0] as [
      string,
      string,
      { pages: number[]; bounding_boxes?: unknown[]; entities?: unknown[] },
    ];
    expect(jobId).toBe('j1');
    expect(itemId).toBe('it1');
    expect(body.pages).toEqual(Array.from({ length: DONE_PAGES }, (_, i) => i + 1));
    // 画布为空 ⇒ 不发框（后端从 file_store 快照兜底；发 [] 会让扫描件 subset_no_regions 400）
    expect(body.bounding_boxes).toBeUndefined();
    expect(body.entities).toBeUndefined();

    expect(submitRedactionTask).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.getByTestId('stage').textContent).toBe('result'));
    expect(screen.getByTestId('subset-preview').textContent).toBe('yes');
  });

  it('(b) 识别已终态 + 失败页：不静默降级为子集，走既有全卷打码链路（I-1）', async () => {
    await seedRecognizing();
    // 识别收尾（awaiting_review）+ 页账终态：子集入口必须消失
    vi.mocked(getItemPageStatus).mockResolvedValue(
      pageStatus(TOTAL_PAGES, 'done', [3]) as never,
    );
    vi.mocked(getJob).mockResolvedValue(makeDetail('awaiting_review', 'awaiting_review'));
    await act(async () => {
      await harness.task!.refresh();
    });
    await waitFor(() => expect(screen.getByTestId('stage').textContent).toBe('preview'));
    await waitFor(() => expect(screen.getByTestId('subset-ready').textContent).toBe('0'));

    await act(async () => {
      fireEvent.click(screen.getByTestId('redact-btn'));
    });

    await waitFor(() => expect(submitRedactionTask).toHaveBeenCalledTimes(1));
    expect(createSubsetPreview).not.toHaveBeenCalled();
  });

  it('(c) 画布有手拉框：请求体 = 服务端快照框 ∪ 手拉框（手拉框所在页整页覆盖）', async () => {
    await seedRecognizing();

    await act(async () => {
      fireEvent.click(screen.getByTestId('set-boxes'));
    });

    await act(async () => {
      fireEvent.click(screen.getByTestId('redact-btn'));
    });

    await waitFor(() => expect(createSubsetPreview).toHaveBeenCalledTimes(1));
    const body = vi.mocked(createSubsetPreview).mock.calls[0][2] as {
      pages: number[];
      bounding_boxes?: Array<{ id: string; page?: number }>;
    };
    const boxes = body.bounding_boxes ?? [];
    // 页 2 用画布框（整页覆盖），页 1/3 保留服务端快照框
    expect(boxes.filter((box) => box.page === 2)).toEqual([
      expect.objectContaining({ id: 'm1', page: 2 }),
    ]);
    expect(boxes.filter((box) => box.page === 1)).toEqual([
      expect.objectContaining({ id: 's1', page: 1 }),
    ]);
    expect(boxes.filter((box) => box.page === 3)).toEqual([
      expect.objectContaining({ id: 's3', page: 3 }),
    ]);
  });

  it('(e) 识别在「用户正看子集结果页」时收尾：stage 留在 result（不踢回确认页），画布仍装全卷框', async () => {
    await seedRecognizing();
    await act(async () => {
      fireEvent.click(screen.getByTestId('redact-btn'));
    });
    await waitFor(() => expect(createSubsetPreview).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.getByTestId('stage').textContent).toBe('result'));

    // 识别收尾（awaiting_review）：快照带 3 页框
    vi.mocked(getJob).mockResolvedValue(makeDetail('awaiting_review', 'awaiting_review'));
    await act(async () => {
      await harness.task!.refresh();
    });

    // 不抢焦点：stage 仍是 result（旧实现会 setStage('preview') 把用户踢回确认页）
    expect(screen.getByTestId('stage').textContent).toBe('result');
    // 画布照常装全卷框（他「返回编辑」时需要完整画布，否则是空画布）
    await waitFor(() => expect(harness.pg!.boundingBoxes.length).toBe(3));
    expect(harness.pg!.boundingBoxes.map((box) => box.id).sort()).toEqual(['s1', 's2', 's3']);
  });

  it('(d) 结果页下载带 preview=true（C-3：闭包依赖含 subsetPreview）', async () => {
    await seedRecognizing();
    await act(async () => {
      fireEvent.click(screen.getByTestId('redact-btn'));
    });
    await waitFor(() => expect(createSubsetPreview).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.getByTestId('stage').textContent).toBe('result'));

    await act(async () => {
      fireEvent.click(screen.getByTestId('download-btn'));
    });

    await waitFor(() => expect(downloadFile).toHaveBeenCalledTimes(1));
    const [url] = vi.mocked(downloadFile).mock.calls[0] as [string, string];
    expect(url).toContain('redacted=true');
    expect(url).toContain('preview=true');
  });
});