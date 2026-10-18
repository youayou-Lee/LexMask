// Copyright 2026 LexMask Contributors

// @vitest-environment jsdom

// Issue#194 Task 10：打码改任务流第二段——「点完打码即可离开」（D3）。
//
// 四用例（brief Step 1）：
// (a) 提交体逐字段：submitRedactionTask(jobId, itemId, {entities, bounding_boxes, config})
//     且不再直调旧同步端点 /redaction/execute；提交成功后 passive 提示「打码中，可离开」；
// (b) provider 报 done：stage 切 result、entity_map/redacted_count 来自 GET /files/{id}
//     快照、report/versions 照旧拉取；
// (c) 409（同账号其他任务在跑）：toast 后端文案、停留 preview 不切 stage，
//     且在途守卫释放（可再次提交）；
// (d) 打码中无 interaction lock（body.overflow 非 hidden、无 inert）——可离开。
//
// provider 用真实 SingleTaskProvider（mock API 层），识别段沿用 T9 流程铺现场。

import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { useEffect, useRef } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ApiError, authFetch } from '@/services/api-client';
import { showToast } from '@/components/Toast';
import { cancelJob, getJob, type JobDetail, type JobSummary } from '@/services/jobsApi';
import { listActiveSingleJobs, submitRecognitionTask, submitRedactionTask } from '@/services/singleTaskApi';
import { SingleTaskProvider, useSingleTask } from '@/features/single-task/single-task-context';

import { usePlayground } from './use-playground';

vi.mock('@/services/singleTaskApi', () => ({
  submitRecognitionTask: vi.fn(),
  submitRedactionTask: vi.fn(),
  listActiveSingleJobs: vi.fn(),
}));
vi.mock('@/services/jobsApi', () => ({
  getJob: vi.fn(),
  cancelJob: vi.fn(),
  listJobs: vi.fn(),
}));
vi.mock('@/services/api-client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/services/api-client')>();
  return { ...actual, authFetch: vi.fn() };
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
  // 稳定引用：use-playground-recognition 有 setPlaygroundPresets(query.data ?? []) 接线，
  // data 每渲染新数组 → 无限重渲染（真实 react-query 有缓存稳定性，mock 必须对齐）
  const stablePresets: never[] = [];
  return {
    usePresets: () => ({ data: stablePresets }),
    useInvalidatePresets: () => async () => {},
  };
});

const CREATED = '2026-01-01T00:00:00Z';

function makeSummary(overrides: Partial<JobSummary> = {}): JobSummary {
  return {
    id: 'j1',
    job_type: 'single',
    title: 'a.pdf',
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
      parsing: 1,
      ner: 0,
      vision: 0,
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
        progress_stage: 'parse',
        progress_current: 0,
        progress_total: 0,
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

// --- 探针：完整 usePlayground + 真实 SingleTaskProvider ---

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
      <span data-testid="redacted-count">{pg.redactedCount}</span>
      <span data-testid="entity-map">
        {Object.entries(pg.entityMap)
          .map(([k, v]) => `${k}=${v}`)
          .join(';')}
      </span>
      <button type="button" data-testid="redact-btn" onClick={() => void pg.handleRedact()}>
        redact
      </button>
    </div>
  );
}

/** provider 句柄：测试里手动 refresh 推进任务阶段（T9 同款）。 */
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
    <SingleTaskProvider>
      <Probe />
      <TaskProbe />
    </SingleTaskProvider>,
  );
}

/** 铺现场：parse（加密 PDF 续跑入口，与拖拽同走 applyParsedFile）→ 识别任务提交 →
 * awaiting_review（实体回填、stage=preview）。识别段是 T9 契约，仅作脚手架。 */
async function seedPreview() {
  renderHarness();
  await act(async () => {
    await harness.pg!.handleDecrypted('f1', 'a.pdf', 13);
  });
  await waitFor(() => expect(submitRecognitionTask).toHaveBeenCalledTimes(1));
  vi.mocked(getJob).mockResolvedValue(makeDetail('awaiting_review', 'awaiting_review'));
  await act(async () => {
    await harness.task!.refresh();
  });
  await waitFor(() => expect(screen.getByTestId('stage').textContent).toBe('preview'));
  await waitFor(() => expect(harness.pg!.isLoading).toBe(false));
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
  vi.mocked(cancelJob).mockResolvedValue(makeSummary({ status: 'cancelled' }));
  vi.mocked(getJob).mockRejectedValue(new Error('job fetch disabled'));
  vi.mocked(listActiveSingleJobs).mockResolvedValue([]);
  const fetchMock = vi.mocked(authFetch);
  fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.endsWith('/files/upload')) {
      return jsonResponse({ file_id: 'f1', filename: 'a.pdf', file_size: 13, file_type: 'pdf' });
    }
    if (url.endsWith('/files/f1/parse')) {
      return jsonResponse({ content: '合同内容', file_type: 'pdf', is_scanned: false, page_count: 1 });
    }
    if (url.endsWith('/files/f1')) {
      return jsonResponse({
        original_filename: 'a.pdf',
        file_size: 13,
        entities: [
          { id: 'e1', text: '张三', type: 'NAME', start: 0, end: 2, selected: true },
          { id: 'e2', text: '李四', type: 'NAME', start: 4, end: 6, selected: true },
        ],
        recognition_config: { entity_type_ids: ['NAME'] },
      });
    }
    if (url.includes('/locate-entities')) {
      return jsonResponse({ boxes: [], missed: [] });
    }
    if (url.endsWith('/redaction/f1/report')) {
      return jsonResponse({
        file_id: 'f1',
        filename: 'a.pdf',
        total_entities: 2,
        redacted_entities: 2,
        entity_type_distribution: {},
        confidence_distribution: {},
        source_distribution: {},
        coverage_rate: 1,
        redaction_mode: 'mask',
        created_at: CREATED,
      });
    }
    if (url.endsWith('/redaction/f1/versions')) {
      return jsonResponse({ file_id: 'f1', versions: [], total: 0 });
    }
    throw new Error(`unexpected authFetch: ${url}`);
  });
});

afterEach(() => {
  cleanup();
});

describe('Issue#194 打码走任务流第二段（T10）', () => {
  it('(a) 提交体逐字段：submitRedactionTask 收到 jobId/itemId/entities/bounding_boxes/config，且不再直调 /redaction/execute；提交后 passive 提示', async () => {
    await seedPreview();

    await act(async () => {
      fireEvent.click(screen.getByTestId('redact-btn'));
    });

    await waitFor(() => expect(submitRedactionTask).toHaveBeenCalledTimes(1));
    expect(submitRedactionTask).toHaveBeenCalledWith(
      'j1',
      'it1',
      expect.objectContaining({
        entities: [
          expect.objectContaining({ text: '张三', selected: true }),
          expect.objectContaining({ text: '李四', selected: true }),
        ],
        bounding_boxes: [],
        config: {
          replacement_mode: 'mask',
          custom_replacements: {},
          watermark_text: undefined,
        },
      }),
    );
    // 旧同步端点从打码主路径移除
    const fetchUrls = vi.mocked(authFetch).mock.calls.map((call) => String(call[0]));
    expect(fetchUrls.some((url) => url.includes('/redaction/execute'))).toBe(false);
    // passive 提示「打码中，可离开」，停留在 preview 等任务推进
    expect(showToast).toHaveBeenCalledWith('后台打码中…', 'info');
    expect(screen.getByTestId('stage').textContent).toBe('preview');
  });

  it('(b) provider 报 done：stage 切 result，entity_map/redacted_count 来自文件快照，report/versions 照旧拉取', async () => {
    await seedPreview();

    await act(async () => {
      fireEvent.click(screen.getByTestId('redact-btn'));
    });
    await waitFor(() => expect(submitRedactionTask).toHaveBeenCalledTimes(1));

    // 打码工人完成：job/item 双 completed；文件记录被工人写入 entity_map + redacted_count
    const fetchMock = vi.mocked(authFetch);
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/files/f1')) {
        return jsonResponse({
          original_filename: 'a.pdf',
          file_size: 13,
          entity_map: { 张三: '张＊', 李四: '李＊' },
          redacted_count: 2,
        });
      }
      if (url.endsWith('/redaction/f1/report')) {
        return jsonResponse({
          file_id: 'f1',
          filename: 'a.pdf',
          total_entities: 2,
          redacted_entities: 2,
          entity_type_distribution: {},
          confidence_distribution: {},
          source_distribution: {},
          coverage_rate: 1,
          redaction_mode: 'mask',
          created_at: CREATED,
        });
      }
      if (url.endsWith('/redaction/f1/versions')) {
        return jsonResponse({ file_id: 'f1', versions: [{ version: 1 }], total: 1 });
      }
      throw new Error(`unexpected authFetch: ${url}`);
    });
    vi.mocked(getJob).mockResolvedValue(makeDetail('completed', 'completed'));
    await act(async () => {
      await harness.task!.refresh();
    });
    await waitFor(() => expect(screen.getByTestId('stage').textContent).toBe('result'));

    // entity_map / redacted_count 数据源 = GET /files/{id} 快照（工人落库的真实结果）
    await waitFor(() =>
      expect(screen.getByTestId('entity-map').textContent).toBe('张三=张＊;李四=李＊'),
    );
    expect(screen.getByTestId('redacted-count').textContent).toBe('2');
    const fetchUrls = fetchMock.mock.calls.map((call) => String(call[0]));
    expect(fetchUrls.some((url) => url.endsWith('/redaction/f1/report'))).toBe(true);
    expect(fetchUrls.some((url) => url.endsWith('/redaction/f1/versions'))).toBe(true);
  });

  it('(c) 409（其他任务在跑）：toast 后端文案、停留 preview、守卫释放可再提交', async () => {
    await seedPreview();

    vi.mocked(submitRedactionTask).mockRejectedValue(
      new ApiError('有文件正在处理：b.pdf，请等它完成', 409, 'server'),
    );
    await act(async () => {
      fireEvent.click(screen.getByTestId('redact-btn'));
    });
    await waitFor(() =>
      expect(showToast).toHaveBeenCalledWith('有文件正在处理：b.pdf，请等它完成', 'error'),
    );
    expect(screen.getByTestId('stage').textContent).toBe('preview');

    // 守卫已释放：再次点击会再次提交（而不是被 in-flight 静默吞掉）
    vi.mocked(submitRedactionTask).mockResolvedValue({
      job_id: 'j1',
      item_id: 'it1',
      status: 'queued',
    });
    await act(async () => {
      fireEvent.click(screen.getByTestId('redact-btn'));
    });
    await waitFor(() => expect(submitRedactionTask).toHaveBeenCalledTimes(2));
    expect(screen.getByTestId('stage').textContent).toBe('preview');
  });

  it('(d) 打码中无 interaction lock：全程不设 loading、body.overflow 非 hidden、无 inert（点完即可离开）', async () => {
    await seedPreview();

    let resolveSubmit: (value: { job_id: string; item_id: string; status: string }) => void =
      () => {};
    vi.mocked(submitRedactionTask).mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveSubmit = resolve;
        }),
    );
    await act(async () => {
      fireEvent.click(screen.getByTestId('redact-btn'));
    });
    // 提交在途：D3 判据（评审 Important-1）——打码全程不设 loading。
    // 这是 hook 级真判据：页面级 lock（body.overflow/inert）由 PlaygroundLoading 的
    // useInteractionLock 设置，而 lock 只在 isLoading 时渲染——isLoading=false
    // 即锁必然不激活。若 handleRedact 加回 setIsLoading(true)，此处立即判红。
    expect(harness.pg!.isLoading).toBe(false);
    expect(document.body.style.overflow).not.toBe('hidden');
    expect(document.documentElement.getAttribute('inert')).toBeNull();
    expect(screen.getByTestId('stage').textContent).toBe('preview');

    await act(async () => {
      resolveSubmit({ job_id: 'j1', item_id: 'it1', status: 'queued' });
    });
    // 提交完成（任务后台打码中）：依然无 loading/无 lock，用户可离开
    expect(harness.pg!.isLoading).toBe(false);
    expect(document.body.style.overflow).not.toBe('hidden');
    expect(document.documentElement.getAttribute('inert')).toBeNull();
    expect(screen.getByTestId('stage').textContent).toBe('preview');
  });

  it('(e) 负样本：账本内其他 job 到 done 不收口本会话（stage 不变、不发 result 拉取）', async () => {
    await seedPreview();

    await act(async () => {
      fireEvent.click(screen.getByTestId('redact-btn'));
    });
    await waitFor(() => expect(submitRedactionTask).toHaveBeenCalledTimes(1));

    // 注册另一 job j2（同账本）；j1 仍在打码段（pending 在途），j2 先完成
    await act(async () => {
      harness.task!.watch('j2', 'f1', 'a.pdf');
    });
    vi.mocked(getJob).mockImplementation(async (jobId: string) =>
      jobId === 'j2'
        ? makeDetail('completed', 'completed', { id: 'j2' })
        : makeDetail('processing', 'redacting'),
    );
    await act(async () => {
      await harness.task!.refresh();
    });

    await waitFor(() => expect(harness.task!.taskForFile('f1')?.phase).toBe('redacting'));
    // j2 已 done，但本会话 pending 的是 j1：不切 result、不发 result 侧拉取
    expect(screen.getByTestId('stage').textContent).toBe('preview');
    const fetchUrls = vi.mocked(authFetch).mock.calls.map((call) => String(call[0]));
    expect(fetchUrls.some((url) => url.endsWith('/redaction/f1/report'))).toBe(false);
    expect(fetchUrls.some((url) => url.endsWith('/redaction/f1/versions'))).toBe(false);
  });
});
