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
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ApiError, authFetch } from '@/services/api-client';
import { showToast } from '@/components/Toast';
import { STORAGE_KEYS } from '@/constants/storage-keys';
import { getScopedStorageItem, setScopedStorageItem } from '@/lib/storage';
import { cancelJob, getJob, type JobDetail, type JobSummary } from '@/services/jobsApi';
import {
  listActiveSingleJobs,
  resolveAwaitingReviewTask,
  submitRecognitionTask,
  submitRedactionTask,
} from '@/services/singleTaskApi';
import { SingleTaskProvider, useSingleTask } from '@/features/single-task/single-task-context';

import { buildDraftSnapshot, serializeDraft, PLAYGROUND_DRAFT_VERSION } from '../lib/playground-draft';
import { usePlayground } from './use-playground';

vi.mock('@/services/singleTaskApi', () => ({
  submitRecognitionTask: vi.fn(),
  submitRedactionTask: vi.fn(),
  listActiveSingleJobs: vi.fn(),
  resolveAwaitingReviewTask: vi.fn(),
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
  // Issue#194 终审 D：provider 发通知要 navigate（useNavigate）⇒ 必须在 Router 上下文内
  return render(
    <MemoryRouter>
      <SingleTaskProvider>
        <Probe />
        <TaskProbe />
      </SingleTaskProvider>
    </MemoryRouter>,
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

// --- Issue#194 Bug#2：刷新后 awaiting_review 态点打码 ---
//
// 真实场景：识别完成停在 awaiting_review 后用户刷新——provider 全新挂载，账本
// 只恢复 active 任务（listActiveSingleJobs），watch 未注册 ⇒ taskForFile miss。
// playground 会话由草稿快照恢复（draft restore 是挂载路径，与真实刷新同构）：
// 预置 localStorage 草稿后直接渲染，不经过识别提交。

/** 与真实刷新同构的现场：预置草稿 → 新 provider 树恢复 preview 会话，账本无该任务。 */
async function seedRefreshedPreview() {
  const snapshot = {
    version: PLAYGROUND_DRAFT_VERSION,
    savedAt: '2026-10-18T00:00:00.000Z',
    stage: 'preview' as const,
    fileInfo: {
      file_id: 'f1',
      filename: 'a.pdf',
      file_size: 13,
      file_type: 'pdf',
      is_scanned: false,
      page_count: 1,
    },
    content: '合同内容',
    entities: [
      { id: 'e1', text: '张三', type: 'NAME', start: 0, end: 2, selected: true, source: 'llm' as const },
      { id: 'e2', text: '李四', type: 'NAME', start: 4, end: 6, selected: true, source: 'llm' as const },
    ],
    boundingBoxes: [],
    processingMode: 'mask' as const,
    replacementMode: 'structured' as const,
    watermarkText: '',
    pseudonymMap: {},
    pseudonymUserEditedKeys: [] as string[],
    confirmedPseudonymMap: null,
    entityMap: {},
    redactedCount: 0,
    currentPage: 0,
    visionStream: null,
  };
  // 与 hook 写入侧同一 API（serializeDraft 产出 JSON 字符串，setScopedStorageItem
  // 再包一层 JSON）——直接 setItem(对象) 会因存储层双重序列化对不上，恢复必 miss。
  setScopedStorageItem(
    STORAGE_KEYS.PLAYGROUND_DRAFT,
    serializeDraft(buildDraftSnapshot(snapshot)) ?? '',
    'tester',
  );
  // Issue#199（门⑥ 二段 F8 起）：打码前必查 performance 判定失败页，而该查询失败是
  // fail-closed（中止本次提交）⇒ 未铺「查询成功」的用例会被中止；这里铺无失败页现场。
  vi.mocked(getJob).mockResolvedValue(makeDetail('awaiting_review', 'awaiting_review'));
  renderHarness();
  await waitFor(() => expect(screen.getByTestId('stage').textContent).toBe('preview'));
  await waitFor(() => expect(harness.pg!.entities.length).toBe(2));
  // 刷新模拟成立的判据：账本里没有该文件的任务（watch 未注册、active 恢复为空）
  expect(harness.task!.taskForFile('f1')).toBeUndefined();
}

describe('Issue#194 Bug#2：刷新后 awaiting_review 态打码兜底解析', () => {
  it('provider 无该任务：handleRedact 走 awaiting_review 兜底解析成功提交，并把任务注册回账本', async () => {
    await seedRefreshedPreview();

    vi.mocked(resolveAwaitingReviewTask).mockResolvedValue({
      job_id: 'j9',
      item_id: 'it9',
      file_id: 'f1',
    });

    await act(async () => {
      fireEvent.click(screen.getByTestId('redact-btn'));
    });

    await waitFor(() => expect(submitRedactionTask).toHaveBeenCalledTimes(1));
    expect(resolveAwaitingReviewTask).toHaveBeenCalledWith('f1');
    expect(submitRedactionTask).toHaveBeenCalledWith(
      'j9',
      'it9',
      expect.objectContaining({
        config: expect.objectContaining({ replacement_mode: 'mask' }),
      }),
    );
    // 兜底命中后任务注册回账本：完成推进与正常路径同轨（provider 轮询可接手）
    expect(harness.task!.taskForFile('f1')?.jobId).toBe('j9');
    // passive 提示照旧，停留 preview 等任务推进
    expect(showToast).toHaveBeenCalledWith('后台打码中…', 'info');
    expect(screen.getByTestId('stage').textContent).toBe('preview');
  });

  it('兜底解析也查无任务 → 报「匿名化失败。」且不提交', async () => {
    await seedRefreshedPreview();

    vi.mocked(resolveAwaitingReviewTask).mockResolvedValue(null);

    await act(async () => {
      fireEvent.click(screen.getByTestId('redact-btn'));
    });

    await waitFor(() => expect(showToast).toHaveBeenCalledWith('匿名化失败。', 'error'));
    expect(submitRedactionTask).not.toHaveBeenCalled();
    expect(screen.getByTestId('stage').textContent).toBe('preview');
  });
});

// --- Issue#194 终审 C（AC1 破口）：打码在途刷新后「结果页」恢复 ---
//
// 真实场景：点「开始匿名化」后任务在后台打码（stage=preview），此时刷新/关浏览器——
// 收尾守门 lastRedactSubmit 是纯内存，草稿里若不带打码 job id，恢复分支永不触发
// ⇒ 成品下载入口只在结果页 ⇒ UI 孤儿化；再点「开始匿名化」吃后端 400。
// 修法（对称识别段 taskJobId/reattachTask）：草稿带 pendingRedactJobId，恢复时重挂
// provider 账本；任务在刷新期间已 completed ⇒ 轮询报 done ⇒ 订阅 effect 照常
// 拉快照+report/versions 收尾进结果页。

/** 打码在途的现场：stage=preview 草稿 + pendingRedactJobId（写侧契约见下一条用例）。 */
async function seedRefreshedRedacting() {
  const snapshot = {
    version: PLAYGROUND_DRAFT_VERSION,
    savedAt: '2026-10-18T00:00:00.000Z',
    stage: 'preview' as const,
    fileInfo: {
      file_id: 'f1',
      filename: 'a.pdf',
      file_size: 13,
      file_type: 'pdf',
      is_scanned: false,
      page_count: 1,
    },
    content: '合同内容',
    entities: [
      { id: 'e1', text: '张三', type: 'NAME', start: 0, end: 2, selected: true, source: 'llm' as const },
      { id: 'e2', text: '李四', type: 'NAME', start: 4, end: 6, selected: true, source: 'llm' as const },
    ],
    boundingBoxes: [],
    processingMode: 'mask' as const,
    replacementMode: 'structured' as const,
    watermarkText: '',
    pseudonymMap: {},
    pseudonymUserEditedKeys: [] as string[],
    confirmedPseudonymMap: null,
    entityMap: {},
    redactedCount: 0,
    currentPage: 0,
    visionStream: null,
    // 刷新期间打码已完成的判定前提：任务不在 active 清单（listActiveSingleJobs=[]），
    // 只能靠草稿里的 job id 重挂账本。
    pendingRedactJobId: 'jr1',
  };
  setScopedStorageItem(
    STORAGE_KEYS.PLAYGROUND_DRAFT,
    serializeDraft(buildDraftSnapshot(snapshot)) ?? '',
    'tester',
  );
}

describe('Issue#194 终审 C：打码在途刷新后结果页恢复', () => {
  it('(读侧) 草稿带 pendingRedactJobId：恢复后任务已 completed ⇒ 自动收尾进结果页（快照+report+versions）', async () => {
    await seedRefreshedRedacting();
    // 打码任务刷新期间已完成：不在 active 恢复清单；getJob 直报 completed
    vi.mocked(listActiveSingleJobs).mockResolvedValue([]);
    vi.mocked(getJob).mockResolvedValue(makeDetail('completed', 'completed', { id: 'jr1' }));
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
        return jsonResponse({ file_id: 'f1', redacted_entities: 2 });
      }
      if (url.endsWith('/redaction/f1/versions')) {
        return jsonResponse({ file_id: 'f1', versions: [{ version: 1 }], total: 1 });
      }
      throw new Error(`unexpected authFetch: ${url}`);
    });

    renderHarness();
    // 重挂账本：草稿恢复把 jr1 注册回 provider（不等首轮轮询即可命中）
    await waitFor(() => expect(harness.task!.taskForFile('f1')?.jobId).toBe('jr1'));

    // 手动驱动一轮轮询（真实定时器 1.5s 一拍，手动 refresh 确定性更快）
    await act(async () => {
      await harness.task!.refresh();
    });

    // AC1 判据：结果页可达（改前 stage 永远停在 preview ⇒ 此处超时判红）
    await waitFor(() => expect(screen.getByTestId('stage').textContent).toBe('result'));
    await waitFor(() =>
      expect(screen.getByTestId('entity-map').textContent).toBe('张三=张＊;李四=李＊'),
    );
    expect(screen.getByTestId('redacted-count').textContent).toBe('2');
    const fetchUrls = fetchMock.mock.calls.map((call) => String(call[0]));
    expect(fetchUrls.some((url) => url.endsWith('/redaction/f1/report'))).toBe(true);
    expect(fetchUrls.some((url) => url.endsWith('/redaction/f1/versions'))).toBe(true);
  });

  it('(写侧) 打码提交后落盘的草稿带 pendingRedactJobId（刷新可恢复的前提）', async () => {
    await seedPreview();

    await act(async () => {
      fireEvent.click(screen.getByTestId('redact-btn'));
    });
    await waitFor(() => expect(submitRedactionTask).toHaveBeenCalledTimes(1));

    // 草稿落盘有 400ms 防抖：提交引发的 pendingRedact 状态变化触发重写
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 600));
    });
    const raw = getScopedStorageItem<string | null>(
      STORAGE_KEYS.PLAYGROUND_DRAFT,
      null,
      'tester',
    );
    expect(raw).toContain('"pendingRedactJobId":"j1"');
  });

  it('(k) 已完成打码的单文件回场：直接恢复结果页，不重提识别', async () => {
    // 用户验收②根源场景：账号打码成功后从任务中心/历史再进文件，旧行为落
    // rerun 自动重跑识别（扫描件无实体被 cached 分支误判未识别），成品反而
    // 看不到 ⇒ 用户误以为匿名化失败。改后 output_path 在 ⇒ 直接恢复结果页。
    renderHarness();
    const fetchMock = vi.mocked(authFetch);
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/files/f1/parse')) {
        return jsonResponse({ content: '', file_type: 'pdf', is_scanned: true, page_count: 3 });
      }
      if (url.endsWith('/files/f1')) {
        return jsonResponse({
          original_filename: 'a.pdf',
          entities: [],
          bounding_boxes: {
            '1': [{ id: 'b1', x: 0, y: 0, width: 1, height: 1, type: 'NAME' }],
          },
          output_path: 'out/result.pdf',
          entity_map: { 张三: '张某某' },
          redacted_count: 5,
        });
      }
      if (url.endsWith('/redaction/f1/report')) {
        return jsonResponse({ total_entities: 5, redacted_entities: 5 });
      }
      if (url.endsWith('/redaction/f1/versions')) {
        return jsonResponse({ versions: [] });
      }
      return jsonResponse({});
    });

    await act(async () => {
      await harness.pg!.resumeFromFile('f1');
    });

    await waitFor(() => expect(screen.getByTestId('stage').textContent).toBe('result'));
    expect(submitRecognitionTask).not.toHaveBeenCalled();
    expect(screen.getByTestId('entity-map').textContent).toContain('张三=张某某');
    expect(screen.getByTestId('redacted-count').textContent).toBe('5');
    const fetchUrls = fetchMock.mock.calls.map((call) => String(call[0]));
    expect(fetchUrls.some((url) => url.endsWith('/redaction/f1/report'))).toBe(true);
    expect(fetchUrls.some((url) => url.endsWith('/redaction/f1/versions'))).toBe(true);
  });

  it('(l) 打码已完成后再点打码（400 not awaiting_review）：自动恢复结果页而非失败弹窗', async () => {
    // sulan 场景：打码成功后回到确认页又点了一次打码，后端 400 拒绝——
    // 不应弹「匿名化失败。」误导用户，而应查到成品在、直接带去结果页。
    await seedPreview();
    vi.mocked(submitRedactionTask).mockRejectedValue(
      new ApiError('item not awaiting_review: completed', 400),
    );
    const fetchMock = vi.mocked(authFetch);
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/files/f1')) {
        return jsonResponse({
          original_filename: 'a.pdf',
          entities: [],
          output_path: 'out/result.pdf',
          entity_map: { 张三: '张某某' },
          redacted_count: 5,
        });
      }
      if (url.endsWith('/redaction/f1/report')) {
        return jsonResponse({ total_entities: 5, redacted_entities: 5 });
      }
      if (url.endsWith('/redaction/f1/versions')) {
        return jsonResponse({ versions: [] });
      }
      return jsonResponse({});
    });

    await act(async () => {
      fireEvent.click(screen.getByTestId('redact-btn'));
    });

    await waitFor(() => expect(screen.getByTestId('stage').textContent).toBe('result'));
    expect(screen.getByTestId('entity-map').textContent).toContain('张三=张某某');
    // 失败文案不出现：全程没有 error toast（完成提示是 info 级）
    expect(vi.mocked(showToast).mock.calls.some(([, kind]) => kind === 'error')).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// Issue#199 门⑥ 二段 F8：打码前失败页知情确认必须 fail-closed
// ---------------------------------------------------------------------------
describe('Issue#199 门⑥ 二段 F8：失败页知情确认 fail-closed', () => {
  it('(a) performance 查询失败 → 中止提交并提示重试（旧实现静默放行）', async () => {
    await seedPreview();
    vi.mocked(getJob).mockRejectedValue(new Error('perf lookup down'));

    await act(async () => {
      fireEvent.click(screen.getByTestId('redact-btn'));
    });

    await waitFor(() =>
      expect(showToast).toHaveBeenCalledWith(expect.stringContaining('未能确认'), 'error'),
    );
    expect(submitRedactionTask).not.toHaveBeenCalled();
    expect(screen.getByTestId('stage').textContent).toBe('preview');
  });

  it('(b) 存在失败页 → 弹确认；用户取消则不提交', async () => {
    await seedPreview();
    const detail = makeDetail('awaiting_review', 'awaiting_review');
    detail.items[0].performance = { recognition: { failed_pages: { '3': 'timeout' } } };
    vi.mocked(getJob).mockResolvedValue(detail);
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false);

    await act(async () => {
      fireEvent.click(screen.getByTestId('redact-btn'));
    });

    await waitFor(() => expect(confirmSpy).toHaveBeenCalledTimes(1));
    expect(String(confirmSpy.mock.calls[0][0])).toContain('3');
    expect(submitRedactionTask).not.toHaveBeenCalled();
    confirmSpy.mockRestore();
  });

  it('(c) 无失败页 → 不弹确认、照常提交（既有成功路径零变化）', async () => {
    await seedPreview();
    const confirmSpy = vi.spyOn(window, 'confirm');

    await act(async () => {
      fireEvent.click(screen.getByTestId('redact-btn'));
    });

    await waitFor(() => expect(submitRedactionTask).toHaveBeenCalledTimes(1));
    expect(confirmSpy).not.toHaveBeenCalled();
    confirmSpy.mockRestore();
  });
});
