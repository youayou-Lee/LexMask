// Copyright 2026 LexMask Contributors

// @vitest-environment jsdom

// Issue#194 Task 9：识别页手术——「浏览器里自己扛请求」改「提交任务 + 听进度」。
//
// 五用例（brief Step 1）：
// (a) 文本件提交：submitRecognitionTask 被调、provider 收到 watch、页面无 blocking 遮罩
//     （useInteractionLock 不激活 ⇒ document.body.style.overflow !== 'hidden'）；
// (b) 任务到 awaiting_review：实体来自 GET /files/{id} 快照、stage==='preview'；
// (c) loadExistingFile 命中活跃任务：不再发 ner/hybrid、vision 请求（零调用），
//     且不重复提交任务（T8 交接：先问一次，miss 则 refresh 后再问）；
// (d) 提交遇 409：recognitionIssue 显示后端 detail 文案、不跳 stage；
// (e) 提交遇 PDF_ENCRYPTED_NEEDS_PASSWORD：encryptedPrompt 被置。
//
// provider 用真实 SingleTaskProvider（mock API 层），不造假 provider——T8 的契约原样生效。

import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import { useEffect } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ApiError, authFetch } from '@/services/api-client';
import { getJob, cancelJob, type JobDetail, type JobSummary } from '@/services/jobsApi';
import { listActiveSingleJobs, submitRecognitionTask } from '@/services/singleTaskApi';
import { SingleTaskProvider, useSingleTask } from '@/features/single-task/single-task-context';

import { overlayModeFor, shouldUsePassiveOverlay } from '../lib/progressive-render';
import { PlaygroundLoading } from '../components/playground-loading';
import { usePlaygroundFile, type UsePlaygroundFileOptions } from './use-playground-file';
import type { Entity } from '../types';

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

const CREATED = '2026-01-01T00:00:00Z';

function makeSummary(overrides: Partial<JobSummary> = {}): JobSummary {
  return {
    id: 'j1',
    job_type: 'single',
    title: 'a.txt',
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

// --- 探针：真实 usePlaygroundFile + 真实 SingleTaskProvider + 页面同款遮罩接线 ---

type FileApi = ReturnType<typeof usePlaygroundFile>;
type TaskApi = ReturnType<typeof useSingleTask>;

let harness: { file: FileApi | null; task: TaskApi | null } = { file: null, task: null };
let entities: Entity[] = [];

const baseOptions: UsePlaygroundFileOptions = {
  latestOcrHasTypesRef: { current: [] },
  latestVisualFeatureTypesRef: { current: [] },
  latestSelectedTypesRef: { current: ['NAME'] },
  resetEntityHistory: () => {},
  resetImageHistory: () => {},
  setEntities: (value) => {
    entities = typeof value === 'function' ? value(entities) : value;
  },
  setSelectedTypes: () => {},
  setBoundingBoxes: () => {},
};

function Probe({ onReady }: { onReady: (file: FileApi, task: TaskApi) => void }) {
  const file = usePlaygroundFile(baseOptions);
  const task = useSingleTask();
  useEffect(() => {
    onReady(file, task);
  }, [file, task, onReady]);
  // playground-page.tsx 的同款接线：任务驱动识别 ⇒ passive 遮罩 + 真进度。
  const overlay = overlayModeFor(
    shouldUsePassiveOverlay(file.isLoading, file.visionStream, file.taskRecognition !== null),
  );
  return (
    <div>
      <span data-testid="stage">{file.stage}</span>
      <span data-testid="issue">{file.recognitionIssue ?? ''}</span>
      <span data-testid="loading-message">{file.loadingMessage}</span>
      {file.isLoading && (
        <PlaygroundLoading
          loadingMessage={file.loadingMessage}
          isImageMode={false}
          overlay={overlay}
          progress={file.visionStream}
          onCancel={() => file.cancelProcessing()}
        />
      )}
      {file.encryptedPrompt && (
        <div data-testid="encrypted-prompt">
          {file.encryptedPrompt.fileId}:{file.encryptedPrompt.filename}
        </div>
      )}
    </div>
  );
}

function renderHarness() {
  // 在 React 外创建一次，组件每次渲染拿到的都是同一引用（等效 useCallback，react-hooks/globals 干净）。
  const onReady = (file: FileApi, task: TaskApi) => {
    harness.file = file;
    harness.task = task;
  };
  return render(
    <SingleTaskProvider>
      <Probe onReady={onReady} />
    </SingleTaskProvider>,
  );
}

const textFile = () => new File(['contract body'], 'a.txt', { type: 'text/plain' });

async function dropTextFile() {
  await act(async () => {
    await harness.file!.handleFileDrop([textFile()]);
  });
}

beforeEach(() => {
  vi.clearAllMocks();
  entities = [];
  harness = { file: null, task: null };
  vi.mocked(submitRecognitionTask).mockResolvedValue({
    job_id: 'j1',
    item_id: 'it1',
    file_id: 'f1',
  });
  vi.mocked(cancelJob).mockResolvedValue(makeSummary({ status: 'cancelled' }));
  vi.mocked(getJob).mockRejectedValue(new Error('job fetch disabled'));
  vi.mocked(listActiveSingleJobs).mockResolvedValue([]);
  const fetchMock = vi.mocked(authFetch);
  fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.endsWith('/files/upload')) {
      return jsonResponse({ file_id: 'f1', filename: 'a.txt', file_size: 13, file_type: 'pdf' });
    }
    if (url.endsWith('/files/f1/parse')) {
      return jsonResponse({ content: '合同内容', file_type: 'pdf', is_scanned: false, page_count: 1 });
    }
    if (url.endsWith('/files/f1')) {
      return jsonResponse({
        original_filename: 'a.txt',
        file_size: 13,
        entities: [
          { id: 'e1', text: '张三', type: 'NAME', start: 0, end: 2 },
          { id: 'e2', text: '李四', type: 'NAME', start: 4, end: 6 },
        ],
        recognition_config: { entity_type_ids: ['NAME'] },
      });
    }
    throw new Error(`unexpected authFetch: ${url}`);
  });
});

afterEach(() => {
  cleanup();
});

describe('Issue#194 识别页任务流（T9）', () => {
  it('(a) 文本件提交：submitRecognitionTask 被调、provider 收到 watch、遮罩为 passive', async () => {
    renderHarness();
    await dropTextFile();

    await waitFor(() => expect(submitRecognitionTask).toHaveBeenCalledTimes(1));
    expect(submitRecognitionTask).toHaveBeenCalledWith('f1', ['NAME']);
    // 不再直调 ner/hybrid
    const fetchUrls = vi.mocked(authFetch).mock.calls.map((call) => String(call[0]));
    expect(fetchUrls.some((url) => url.includes('ner/hybrid'))).toBe(false);

    // provider 收到 watch：taskForFile 同步命中（占位视图，不等首轮轮询）
    await waitFor(() => expect(harness.task!.taskForFile('f1')).toBeDefined());
    expect(harness.task!.taskForFile('f1')?.phase).toBe('recognizing');

    // 识别中视图：不 blocking（useInteractionLock 不激活）
    await waitFor(() => expect(harness.file!.taskRecognition).not.toBeNull());
    expect(document.body.style.overflow).not.toBe('hidden');
    expect(document.documentElement.getAttribute('inert')).toBeNull();
  });

  it('(b) 任务到 awaiting_review：实体来自 GET /files/{id} 快照、stage 切 preview、loading 退出', async () => {
    renderHarness();
    await dropTextFile();
    await waitFor(() => expect(harness.task!.taskForFile('f1')).toBeDefined());

    vi.mocked(getJob).mockResolvedValue(makeDetail('awaiting_review', 'awaiting_review'));
    await act(async () => {
      await harness.task!.refresh();
    });

    await waitFor(() => expect(screen.getByTestId('stage').textContent).toBe('preview'));
    expect(entities.map((entity) => entity.text)).toEqual(['张三', '李四']);
    expect(entities.every((entity) => entity.selected === true)).toBe(true);
    // 快照拉的是文件记录（planServerCachedResume 同源），不是 ner 端点
    const fetchUrls = vi.mocked(authFetch).mock.calls.map((call) => String(call[0]));
    expect(fetchUrls.some((url) => url.endsWith('/files/f1'))).toBe(true);
    expect(fetchUrls.some((url) => url.includes('ner/hybrid'))).toBe(false);
    await waitFor(() => expect(harness.file!.isLoading).toBe(false));
    // 迁移只执行一次：tasks 再变化也不重复拉快照
    const snapshotCalls = vi
      .mocked(authFetch)
      .mock.calls.filter((call) => String(call[0]).endsWith('/files/f1')).length;
    await act(async () => {
      await harness.task!.refresh();
    });
    await waitFor(() => expect(harness.task!.taskForFile('f1')?.phase).toBe('awaiting_review'));
    const snapshotCallsAfter = vi
      .mocked(authFetch)
      .mock.calls.filter((call) => String(call[0]).endsWith('/files/f1')).length;
    expect(snapshotCallsAfter).toBe(snapshotCalls);
  });

  it('(c) loadExistingFile 命中活跃任务：零 ner/vision 调用、不重复提交、进入识别中视图', async () => {
    vi.mocked(listActiveSingleJobs).mockResolvedValue([
      makeSummary({ id: 'j9', title: 'a.txt' }),
    ]);
    vi.mocked(getJob).mockResolvedValue(
      makeDetail('processing', 'parsing', {
        id: 'j9',
        items: [
          {
            id: 'it9',
            job_id: 'j9',
            file_id: 'f1',
            sort_order: 0,
            status: 'parsing',
            progress_stage: 'parse',
            progress_current: 0,
            progress_total: 0,
            progress_message: null,
            error_message: null,
            created_at: CREATED,
            updated_at: CREATED,
          },
        ],
      }) as JobDetail,
    );

    renderHarness();
    // provider 挂载恢复把 j9 认领进来（fileId 待首轮轮询回填 → 先问 miss，refresh 后再问）
    await waitFor(() => expect(harness.task!.tasks.length).toBe(1));
    await act(async () => {
      await harness.file!.loadExistingFile('f1');
    });

    await waitFor(() => expect(harness.file!.taskRecognition?.jobId).toBe('j9'));
    // 不发识别请求、不重复提交任务
    const fetchUrls = vi.mocked(authFetch).mock.calls.map((call) => String(call[0]));
    expect(fetchUrls.some((url) => /ner\/hybrid|vision/.test(url))).toBe(false);
    expect(submitRecognitionTask).not.toHaveBeenCalled();
    // 识别中视图：loading 保持、遮罩 passive、阶段人话（解析中）
    expect(harness.file!.isLoading).toBe(true);
    await waitFor(() => expect(screen.getByTestId('loading-message').textContent).toContain('后台'));
    expect(document.body.style.overflow).not.toBe('hidden');
  });

  it('(d) 提交遇 409：recognitionIssue 显示后端文案、不跳 stage', async () => {
    vi.mocked(submitRecognitionTask).mockRejectedValue(
      new ApiError('有文件正在处理：a.txt，请等它完成', 409, 'server'),
    );
    renderHarness();
    await dropTextFile();

    await waitFor(() =>
      expect(screen.getByTestId('issue').textContent).toBe('有文件正在处理：a.txt，请等它完成'),
    );
    expect(screen.getByTestId('stage').textContent).toBe('upload');
    expect(harness.file!.isLoading).toBe(false);
    expect(document.body.style.overflow).not.toBe('hidden');
  });

  it('(e) 提交遇 PDF_ENCRYPTED_NEEDS_PASSWORD：encryptedPrompt 被置', async () => {
    vi.mocked(submitRecognitionTask).mockRejectedValue(
      new ApiError('PDF 已加密，需要密码后才能识别', 400, 'server', 'PDF_ENCRYPTED_NEEDS_PASSWORD'),
    );
    renderHarness();
    await dropTextFile();

    await waitFor(() => expect(screen.getByTestId('encrypted-prompt').textContent).toBe('f1:a.txt'));
    expect(screen.getByTestId('stage').textContent).toBe('upload');
    expect(harness.file!.isLoading).toBe(false);
  });
});
