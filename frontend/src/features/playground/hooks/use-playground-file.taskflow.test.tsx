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
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ApiError, authFetch } from '@/services/api-client';
import { getJob, cancelJob, type JobDetail, type JobSummary } from '@/services/jobsApi';
import {
  listActiveSingleJobs,
  resolveAwaitingReviewTask,
  submitRecognitionTask,
} from '@/services/singleTaskApi';
import { SingleTaskProvider, useSingleTask } from '@/features/single-task/single-task-context';
import { showToast } from '@/components/Toast';

import { overlayModeFor, shouldUsePassiveOverlay } from '../lib/progressive-render';
import { PlaygroundLoading } from '../components/playground-loading';
import { usePlaygroundFile, type UsePlaygroundFileOptions } from './use-playground-file';
import type { BoundingBox, Entity } from '../types';

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
  getJobsBatch: vi.fn(),
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

function makeItem(overrides: Partial<JobDetail['items'][number]> = {}): JobDetail['items'][number] {
  return {
    id: 'it1',
    job_id: 'j1',
    file_id: 'f1',
    sort_order: 0,
    status: 'awaiting_review',
    progress_stage: 'parse',
    progress_current: 0,
    progress_total: 0,
    progress_message: null,
    error_message: null,
    created_at: CREATED,
    updated_at: CREATED,
    ...overrides,
  } as JobDetail['items'][number];
}

function makeDetail(
  jobStatus: string,
  itemStatus: string,
  overrides: Partial<JobDetail> = {},
): JobDetail {
  return {
    ...makeSummary({ status: jobStatus }),
    items: [makeItem({ status: itemStatus })],
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
let boxes: BoundingBox[] = [];

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
  // 用户验收①：记录画布框写入（迁移/回场路径是否把快照 bounding_boxes 装进画布）
  setBoundingBoxes: (value) => {
    boxes = typeof value === 'function' ? value(boxes) : value;
  },
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
    // Issue#194 终审 D：provider 发通知要 navigate（useNavigate）⇒ 必须在 Router 上下文内
    <MemoryRouter>
      <SingleTaskProvider>
        <Probe onReady={onReady} />
      </SingleTaskProvider>
    </MemoryRouter>,
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
  boxes = [];
  harness = { file: null, task: null };
  vi.mocked(submitRecognitionTask).mockResolvedValue({
    job_id: 'j1',
    item_id: 'it1',
    file_id: 'f1',
  });
  vi.mocked(cancelJob).mockResolvedValue(makeSummary({ status: 'cancelled' }));
  vi.mocked(getJob).mockRejectedValue(new Error('job fetch disabled'));
  vi.mocked(listActiveSingleJobs).mockResolvedValue([]);
  // 终审 A：默认查无待确认任务（(f) 用例各自覆盖）
  vi.mocked(resolveAwaitingReviewTask).mockResolvedValue(null);
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
        // 用户验收①负样本：文本件快照即便带框也不许装进画布（框只在视觉件语义里）
        bounding_boxes: { '1': [{ id: 'bX', x: 0, y: 0, width: 1, height: 1, type: 'NAME' }] },
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
    // 文本件：第三参 undefined（视觉要素选择不携带，终审 B 的 isVisionFile 分支）
    expect(submitRecognitionTask).toHaveBeenCalledWith('f1', ['NAME'], undefined);
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
    // 用户验收①负样本：文本件不受影响——快照即便带 bounding_boxes 也不装进画布
    expect(boxes).toEqual([]);
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

  it('(i) 用户验收①：扫描件 awaiting_review 迁移——快照 bounding_boxes 装进画布（改前红：画布空空）', async () => {
    // 扫描件：parse 出 is_scanned，快照 entities 恒空、框按页 dict 存在 bounding_boxes
    const fetchMock = vi.mocked(authFetch);
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/files/upload')) {
        return jsonResponse({ file_id: 'f1', filename: 'scan.pdf', file_size: 99, file_type: 'pdf' });
      }
      if (url.endsWith('/files/f1/parse')) {
        return jsonResponse({ content: '', file_type: 'pdf_scanned', is_scanned: true, page_count: 2 });
      }
      if (url.endsWith('/files/f1')) {
        return jsonResponse({
          original_filename: 'scan.pdf',
          file_size: 99,
          entities: [],
          bounding_boxes: {
            '1': [{ id: 'b1', x: 10, y: 20, width: 30, height: 40, type: 'NAME', text: '张三' }],
            '2': [{ x: 1, y: 2, width: 3, height: 4, type: 'SEAL' }],
          },
        });
      }
      throw new Error(`unexpected authFetch: ${url}`);
    });

    renderHarness();
    await dropTextFile();
    await waitFor(() => expect(harness.task!.taskForFile('f1')).toBeDefined());

    vi.mocked(getJob).mockResolvedValue(makeDetail('awaiting_review', 'awaiting_review'));
    await act(async () => {
      await harness.task!.refresh();
    });

    await waitFor(() => expect(screen.getByTestId('stage').textContent).toBe('preview'));
    // 判据：拍平的框数组进画布——页号来自 dict 键、缺 id 走扁平序号兜底、默认选中
    await waitFor(() => expect(boxes).toHaveLength(2));
    expect(boxes[0]).toMatchObject({ id: 'b1', page: 1, type: 'NAME', selected: true });
    expect(boxes[1]).toMatchObject({ id: 'bbox_1', page: 2, type: 'SEAL', selected: true });
  });

  it('(j) 用户验收③：识别中视图显示服务端段名——第二段显示「视觉要素」，不再像重新开始', async () => {
    renderHarness();
    await dropTextFile();
    await waitFor(() => expect(harness.task!.taskForFile('f1')).toBeDefined());

    // 视觉第二段（merge pass）：progress_message 带 stage_label + 页码
    vi.mocked(getJob).mockResolvedValue(
      makeDetail('processing', 'vision', {
        items: [
          makeItem({
            status: 'vision',
            progress_stage: 'vision',
            progress_current: 5,
            progress_total: 20,
            progress_message: '视觉特征识别 page 5/20',
          }),
        ],
      }),
    );
    await act(async () => {
      await harness.task!.refresh();
    });

    // 判据：段名人话 + 数字保留（改前红：只显示「后台识别中 5/20」，与第一段一模一样）
    await waitFor(() =>
      expect(screen.getByTestId('loading-message').textContent).toBe('视觉要素 5/20'),
    );

    // 第一段（OCR+HaS）→ 文字识别
    vi.mocked(getJob).mockResolvedValue(
      makeDetail('processing', 'vision', {
        items: [
          makeItem({
            status: 'vision',
            progress_stage: 'vision',
            progress_current: 3,
            progress_total: 20,
            progress_message: 'OCR+HaS识别 page 3/20',
          }),
        ],
      }),
    );
    await act(async () => {
      await harness.task!.refresh();
    });
    await waitFor(() =>
      expect(screen.getByTestId('loading-message').textContent).toBe('文字识别 3/20'),
    );

    // 无段名消息（vision 排队）→ 回落既有「后台识别中」模板（负样本：不秀英文原文）
    vi.mocked(getJob).mockResolvedValue(
      makeDetail('processing', 'vision', {
        items: [
          makeItem({
            status: 'vision',
            progress_stage: 'vision',
            progress_current: 0,
            progress_total: 20,
            progress_message: 'Vision recognition queued for 20 page(s)',
          }),
        ],
      }),
    );
    await act(async () => {
      await harness.task!.refresh();
    });
    await waitFor(() =>
      expect(screen.getByTestId('loading-message').textContent).toBe('后台识别中 0/20…'),
    );
  });
});

// --- Issue#194 终审 A：扫描件停待确认后经任务中心/历史回场 ⇒ 不得重复提交识别任务 ---
//
// 机制：扫描件 file 记录 entities 恒空（planServerCachedResume 必 miss）→ 旧路径
// 落 rerun 自动 submitRecognitionTask，而旧任务仍停在 awaiting_review ⇒ 双任务孤儿。
// 修法：loadExistingFile 的 rerun 分支前按 fileId 直查 awaiting_review 清单
// （resolveAwaitingReviewTask），命中即恢复确认视图、不自动重提。

describe('Issue#194 终审 A：待确认扫描件回场不重复提交', () => {
  function mockScannedFileWithEmptyEntities() {
    const fetchMock = vi.mocked(authFetch);
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/files/f1/parse')) {
        return jsonResponse({
          content: '',
          file_type: 'pdf_scanned',
          is_scanned: true,
          page_count: 3,
        });
      }
      if (url.endsWith('/files/f1')) {
        // 扫描件：识别完成也不写 entities（实体在 vision 框/确认流呈现）
        return jsonResponse({ original_filename: 'scan.pdf', file_size: 99, entities: [] });
      }
      throw new Error(`unexpected authFetch: ${url}`);
    });
  }

  it('(f) rerun 前命中 awaiting_review：恢复确认视图（stage=preview），不重复提交识别任务', async () => {
    mockScannedFileWithEmptyEntities();
    // awaiting_review 任务不在 active 恢复清单（provider 账本双 miss 的前提）；
    // resolveAwaitingReviewTask 按 fileId 直查命中（j7 停待确认）
    vi.mocked(listActiveSingleJobs).mockResolvedValue([]);
    vi.mocked(resolveAwaitingReviewTask).mockResolvedValue({
      job_id: 'j7',
      item_id: 'it7',
      file_id: 'f1',
    });

    renderHarness();
    await act(async () => {
      await harness.file!.loadExistingFile('f1');
    });

    // 判据（改前红：旧路径会自动 submitRecognitionTask 建第二个识别任务）
    expect(submitRecognitionTask).not.toHaveBeenCalled();
    expect(resolveAwaitingReviewTask).toHaveBeenCalledWith('f1');
    expect(screen.getByTestId('stage').textContent).toBe('preview');
    expect(harness.file!.isLoading).toBe(false);
    // 提示该文件识别完成待确认（i18n 新键），实体列表允许为空（不新造 UI）
    expect(showToast).toHaveBeenCalledWith('该文件识别已完成，请确认识别结果后开始匿名化', 'info');
  });

  it('(g) rerun 前未命中 awaiting_review（真未识别过）：照旧自动重提', async () => {
    mockScannedFileWithEmptyEntities();
    vi.mocked(listActiveSingleJobs).mockResolvedValue([]);
    vi.mocked(resolveAwaitingReviewTask).mockResolvedValue(null);

    renderHarness();
    await act(async () => {
      await harness.file!.loadExistingFile('f1');
    });

    // 查无待确认任务 → rerun 语义保留：自动提交识别任务（负样本，防一刀切）
    await waitFor(() => expect(submitRecognitionTask).toHaveBeenCalledTimes(1));
  });

  it('(h) Issue#194 终审 B：扫描件任务化识别透传视觉要素选择（第三参）', async () => {
    // 扫描件：识别工人视觉段读 job config 的 ocr_has_types/visual_feature_types，
    // 不透传则编排器按默认全类型集跑（用户勾选无效的回归）
    const fetchMock = vi.mocked(authFetch);
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/files/upload')) {
        return jsonResponse({ file_id: 'f1', filename: 'scan.pdf', file_size: 99, file_type: 'pdf' });
      }
      if (url.endsWith('/files/f1/parse')) {
        return jsonResponse({ content: '', file_type: 'pdf_scanned', is_scanned: true, page_count: 2 });
      }
      throw new Error(`unexpected authFetch: ${url}`);
    });
    baseOptions.latestOcrHasTypesRef.current = ['ocr_seal'];
    baseOptions.latestVisualFeatureTypesRef.current = ['visual_stamp'];

    try {
      renderHarness();
      await dropTextFile();
      await waitFor(() => expect(submitRecognitionTask).toHaveBeenCalledTimes(1));
      expect(submitRecognitionTask).toHaveBeenCalledWith('f1', ['NAME'], {
        ocrHasTypes: ['ocr_seal'],
        visualFeatureTypes: ['visual_stamp'],
      });
    } finally {
      baseOptions.latestOcrHasTypesRef.current = [];
      baseOptions.latestVisualFeatureTypesRef.current = [];
    }
  });
});
