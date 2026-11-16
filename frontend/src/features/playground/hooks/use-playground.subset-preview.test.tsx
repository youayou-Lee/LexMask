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
import { showToast } from '@/components/Toast';
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
      <button
        type="button"
        data-testid="redact-pages-btn"
        onClick={() => void pg.handleRedact([2, 3, 4])}
      >
        redact-pages
      </button>
      <button
        type="button"
        data-testid="redact-stale-btn"
        onClick={() => void pg.handleRedact([DONE_PAGES + 1])}
      >
        redact-stale
      </button>
      {/* 门⑥ r1 M2：显式选择覆盖全部页 ⇒ 仍走子集（用户点的是「生成预览」）。 */}
      <button
        type="button"
        data-testid="redact-all-btn"
        onClick={() =>
          void pg.handleRedact(Array.from({ length: TOTAL_PAGES }, (_, i) => i + 1))
        }
      >
        redact-all
      </button>
      {/* 门⑥ r1 M3：显式空集 ⇒ 不回落全卷（空集是「选了零页」，不是「没传」）。 */}
      <button type="button" data-testid="redact-empty-btn" onClick={() => void pg.handleRedact([])}>
        redact-empty
      </button>
      {/* 面板是直传接线（onClick={handleRedact}），React 会把 MouseEvent 当第一参——
          这里钉住宿主必须把它当「未选页」处理，不能误判成页号列表。 */}
      <button
        type="button"
        data-testid="redact-event-btn"
        onClick={(event) => void pg.handleRedact(event as unknown as number[])}
      >
        redact-event
      </button>
      <button type="button" data-testid="download-btn" onClick={() => pg.handleDownload()}>
        download
      </button>
      <button
        type="button"
        data-testid="set-unselected-boxes"
        onClick={() =>
          pg.setBoundingBoxes([
            {
              id: 'u1',
              page: 2,
              x: 10,
              y: 10,
              width: 20,
              height: 20,
              type: 'custom_ocr_has_name',
              selected: false,
            },
          ])
        }
      >
        set-unselected-boxes
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

  it('(f) 画布有框但读不到服务端快照 ⇒ fail-closed：不发子集请求 + 报错提示（N-2，漏打码红线）', async () => {
    await seedRecognizing();
    // 画布有框（用户手改过）⇒ 走「快照 ∪ 画布」合并分支
    await act(async () => {
      fireEvent.click(screen.getByTestId('set-boxes'));
    });
    // 服务端快照读取失败（5xx）
    vi.mocked(authFetch).mockImplementation(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/files/f1')) {
        return { ok: false, status: 500 } as unknown as Response;
      }
      if (url.endsWith('/files/f1/parse')) {
        return jsonResponse({
          content: '',
          file_type: 'pdf_scanned',
          is_scanned: true,
          page_count: TOTAL_PAGES,
        });
      }
      throw new Error(`unexpected authFetch: ${url}`);
    });

    await act(async () => {
      fireEvent.click(screen.getByTestId('redact-btn'));
    });

    await waitFor(() => expect(showToast).toHaveBeenCalled());
    // 绝不允许「只发画布框」——那会让画布没碰过的页零框进后端
    expect(createSubsetPreview).not.toHaveBeenCalled();
    expect(screen.getByTestId('stage').textContent).toBe('preview');
    expect(showToast).toHaveBeenCalledWith(
      expect.stringContaining('读取识别快照失败'),
      'error',
    );
  });

  it('(g) 同步子集打码期间用户切到另一份文件 ⇒ 旧卷回执作废，不落到新文件上（N-1）', async () => {
    await seedRecognizing();
    let resolveSubset!: (value: unknown) => void;
    vi.mocked(createSubsetPreview).mockImplementation(
      () => new Promise((resolve) => { resolveSubset = resolve; }) as never,
    );

    await act(async () => {
      fireEvent.click(screen.getByTestId('redact-btn'));
    });
    await waitFor(() => expect(createSubsetPreview).toHaveBeenCalledTimes(1));

    // 同步打码还在飞：切到另一份文件（文件切换 ⇒ latestFileIdRef 前移）
    vi.mocked(authFetch).mockImplementation(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/files/f2/parse')) {
        return jsonResponse({
          content: '',
          file_type: 'pdf_scanned',
          is_scanned: true,
          page_count: 3,
        });
      }
      if (url.endsWith('/files/f2')) {
        return jsonResponse({ original_filename: '卷2.pdf', file_size: 13, entities: [] });
      }
      if (url.includes('/preview-image')) {
        return new Response(new Blob(['x']), {
          status: 200,
          headers: { 'Content-Type': 'image/png' },
        });
      }
      throw new Error(`unexpected authFetch: ${url}`);
    });
    await act(async () => {
      await harness.pg!.handleDecrypted('f2', '卷2.pdf', 20);
    });
    await waitFor(() => expect(harness.pg!.fileInfo?.file_id).toBe('f2'));

    // 旧卷回执迟到 ⇒ 必须被守卫丢弃
    await act(async () => {
      resolveSubset({
        file_id: 'f1',
        processed_pages: [1, 2],
        page_map: { '1': 1, '2': 2 },
        page_count: 2,
        redacted_count: 2,
      });
      await Promise.resolve();
    });

    expect(screen.getByTestId('subset-preview').textContent).toBe('no');
    expect(screen.getByTestId('stage').textContent).not.toBe('result');
  });

  it('(h) 「返回编辑」⇒ 子集回执作废（N-7③：回执只服务结果页，滞留会让 awaiting_review 的守卫半失效）', async () => {
    await seedRecognizing();
    await act(async () => {
      fireEvent.click(screen.getByTestId('redact-btn'));
    });
    await waitFor(() => expect(screen.getByTestId('stage').textContent).toBe('result'));
    expect(screen.getByTestId('subset-preview').textContent).toBe('yes');

    await act(async () => {
      harness.pg!.setStage('preview');
    });

    await waitFor(() => expect(screen.getByTestId('subset-preview').textContent).toBe('no'));
  });

  it('(c2) 画布上某页的框被全部取消勾选 ⇒ 该页零框（不回落服务端快照框，R4-2）', async () => {
    await seedRecognizing();

    await act(async () => {
      fireEvent.click(screen.getByTestId('set-unselected-boxes'));
    });

    await act(async () => {
      fireEvent.click(screen.getByTestId('redact-btn'));
    });

    await waitFor(() => expect(createSubsetPreview).toHaveBeenCalledTimes(1));
    const body = vi.mocked(createSubsetPreview).mock.calls[0][2] as {
      bounding_boxes?: Array<{ id: string; page?: number }>;
    };
    const boxes = body.bounding_boxes ?? [];
    // 画布有框 ⇒ 走合并分支；页 2 的框未勾选 ⇒ 既不带画布框，也不回落服务端快照框
    expect(boxes.filter((box) => box.page === 2)).toEqual([]);
    expect(boxes.filter((box) => box.page === 1)).toEqual([
      expect.objectContaining({ id: 's1', page: 1 }),
    ]);
  });

  it('(d2) 非子集口径下载：URL 不得带 preview=true（R4-1 负例：恒带会被后端 400）', async () => {
    await seedRecognizing();
    // 没有子集回执（stage 仍在 preview）⇒ 走全卷口径
    expect(screen.getByTestId('subset-preview').textContent).toBe('no');

    await act(async () => {
      fireEvent.click(screen.getByTestId('download-btn'));
    });

    await waitFor(() => expect(downloadFile).toHaveBeenCalledTimes(1));
    const [url] = vi.mocked(downloadFile).mock.calls[0] as [string, string];
    expect(url).toContain('redacted=true');
    expect(url).not.toContain('preview=true');
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
describe('2026-11-16：手动选页（子集弹窗）', () => {
  it('handleRedact([2,3,4]) ⇒ createSubsetPreview 只收这三页', async () => {
    await seedRecognizing();

    await act(async () => {
      fireEvent.click(screen.getByTestId('redact-pages-btn'));
    });

    await waitFor(() => expect(createSubsetPreview).toHaveBeenCalledTimes(1));
    const [, , body] = vi.mocked(createSubsetPreview).mock.calls[0] as [
      string,
      string,
      { pages: number[] },
    ];
    expect(body.pages).toEqual([2, 3, 4]);
  });

  it('第一参不是数组（React 事件对象）⇒ 视为未选页，走默认全量子集', async () => {
    await seedRecognizing();

    await act(async () => {
      fireEvent.click(screen.getByTestId('redact-event-btn'));
    });

    await waitFor(() => expect(createSubsetPreview).toHaveBeenCalledTimes(1));
    const [, , body] = vi.mocked(createSubsetPreview).mock.calls[0] as [
      string,
      string,
      { pages: number[] },
    ];
    expect(body.pages).toEqual(Array.from({ length: DONE_PAGES }, (_, i) => i + 1));
  });

  it('选择里含未处理页（页账已推进）⇒ fail-closed 不提交，提示重选', async () => {
    await seedRecognizing();

    await act(async () => {
      fireEvent.click(screen.getByTestId('redact-stale-btn'));
    });

    expect(createSubsetPreview).not.toHaveBeenCalled();
    expect(showToast).toHaveBeenCalledWith(
      expect.stringContaining('页账已更新'),
      'error',
    );
    // 解锁 redactionInFlightRef：作废后主按钮仍可再点
    await act(async () => {
      fireEvent.click(screen.getByTestId('redact-btn'));
    });
    await waitFor(() => expect(createSubsetPreview).toHaveBeenCalledTimes(1));
  });

  // 门⑥ r1 M2（评审）：显式选择覆盖全部页时不能静默回落全卷。
  it('显式选择覆盖全部页（86/86）⇒ 仍走子集预览，不启后台全卷打码', async () => {
    await seedRecognizing();
    // 页账刚涨到 86/86，但轮询快照仍是 running（弹窗未关）——用户此时点全选。
    vi.mocked(getItemPageStatus).mockResolvedValue(pageStatus(TOTAL_PAGES, 'running') as never);

    await act(async () => {
      fireEvent.click(screen.getByTestId('redact-all-btn'));
    });

    await waitFor(() => expect(createSubsetPreview).toHaveBeenCalledTimes(1));
    expect(submitRedactionTask).not.toHaveBeenCalled();
    const [, , body] = vi.mocked(createSubsetPreview).mock.calls[0] as [
      string,
      string,
      { pages: number[] },
    ];
    expect(body.pages).toEqual(Array.from({ length: TOTAL_PAGES }, (_, i) => i + 1));
  });

  // 门⑥ r1 M3（评审）：空集不得被当成「未选页」。
  it('显式空集（[]）⇒ fail-closed，既不提交子集也不走全卷', async () => {
    await seedRecognizing();

    await act(async () => {
      fireEvent.click(screen.getByTestId('redact-empty-btn'));
    });

    expect(createSubsetPreview).not.toHaveBeenCalled();
    expect(submitRedactionTask).not.toHaveBeenCalled();
    expect(showToast).toHaveBeenCalledWith(expect.stringContaining('请先选择'), 'error');
  });

  // 门⑥ r1 M4（评审）：页账读取失败 ≠ 选择过期，文案必须分开（否则排障方向被带偏）。
  it('页账查询抛错 ⇒ 报「页账读取失败」而不是「页账已更新」', async () => {
    await seedRecognizing();
    vi.mocked(getItemPageStatus).mockRejectedValue(new Error('network'));

    await act(async () => {
      fireEvent.click(screen.getByTestId('redact-pages-btn'));
    });

    expect(createSubsetPreview).not.toHaveBeenCalled();
    expect(showToast).toHaveBeenCalledWith(expect.stringContaining('页账读取失败'), 'error');
  });
});
