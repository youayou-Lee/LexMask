// Copyright 2026 LexMask Contributors

// @vitest-environment jsdom

// Issue#351 P1-A（v2 方案，冷审修订后）：挂载恢复 toast 收口到 /single 路由。
//
// 根因：PlaygroundProvider 常驻 Layout（路由切换不重建），usePlayground 顶层挂载
// effect 无条件读草稿+弹「已恢复上次单文件处理现场」——在 /batch、/jobs 等任何
// 页面刷新都弹，是纯噪音。
//
// 修订后契约（冷审 REVISE 处置）：
//  (a) 非 /single 路由：不恢复、不弹（showToast 0 次调用）；
//  (b) /single 路由：照常恢复 + toast 恰好 1 次（既有能力不回归）；
//  (c) 切走再回：/single 恢复 → 切 /jobs 刷新（不弹）→ 回 /single 再恢复（≤1 次/挂载）；
//  (d) ?file_id= 深链优先级：路由闸不影响 resumeFromFile（既有协议先赢）。
//
// 钉法（冷审）：toast 断言用 showToast mock 调用计数（非 /single=0 / /single=1），
// 不靠人眼看页面；负向断言均配正向。

import { act, cleanup, render, waitFor } from '@testing-library/react';
import { useEffect, useRef } from 'react';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { STORAGE_KEYS } from '@/constants/storage-keys';
import { buildDraftSnapshot, serializeDraft, PLAYGROUND_DRAFT_VERSION } from '../lib/playground-draft';
import { scopedStorageKey } from '@/lib/storage';
import { showToast } from '@/components/Toast';
import { SingleTaskProvider } from '@/features/single-task/single-task-context';
import type { FileInfo } from '@/features/playground/types';

import { usePlayground } from './use-playground';
import hookRaw from './use-playground.ts?raw';

vi.mock('@/services/singleTaskApi', () => ({
  submitRecognitionTask: vi.fn(),
  submitRedactionTask: vi.fn(),
  createSubsetPreview: vi.fn(),
  listActiveSingleJobs: vi.fn().mockResolvedValue([]),
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
  return {
    ...actual,
    authFetch: vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ image_base64: '' }),
      blob: async () => new Blob(),
    }),
    downloadFile: vi.fn(),
  };
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

const mockedShowToast = vi.mocked(showToast);

const testFileInfo: FileInfo = {
  file_id: 'f1',
  filename: '卷.pdf',
  file_size: 100,
  file_type: 'pdf',
  page_count: 1,
} as FileInfo;

function seedDraft() {
  // 生产写入路径 setScopedStorageItem(JSON.stringify(value))——草稿值是
  // 「序列化后的字符串」，storage 层再 JSON.stringify 一层 ⇒ 落盘是双编码串。
  const snapshot = serializeDraft(buildDraftSnapshot({
    stage: 'preview',
    fileInfo: testFileInfo,
    content: '正文内容',
    entities: [],
    boundingBoxes: [],
    processingMode: 'mask',
    replacementMode: 'structured',
    watermarkText: '',
    pseudonymMap: {},
    confirmedPseudonymMap: null,
    entityMap: {},
    redactedCount: 0,
    currentPage: 1,
  }));
  localStorage.setItem(
    scopedStorageKey(STORAGE_KEYS.PLAYGROUND_DRAFT, 'tester'),
    JSON.stringify(snapshot),
  );
}

function Probe() {
  const pg = usePlayground();
  const ref = useRef(pg);
  ref.current = pg;
  useEffect(() => {
    harness.pg = ref.current;
  });
  return null;
}

const harness: { pg: ReturnType<typeof usePlayground> | null } = { pg: null };

function renderAt(pathname: string) {
  // usePresets（useQuery）需要 QueryClient——单例即可，queryFn 全走 fetch 失败态
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[pathname]}>
        <SingleTaskProvider>
          <Probe />
        </SingleTaskProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  harness.pg = null;
});

afterEach(() => {
  cleanup();
});

describe('Issue#351 P1-A：恢复 toast 收口 /single 路由', () => {
  it('(a) 非 /single 路由（/jobs）：草稿不恢复、toast 零调用（负向）；草稿仍在 localStorage', async () => {
    seedDraft();
    renderAt('/jobs');
    await act(async () => {
      await Promise.resolve();
    });
    // 负向：showToast 从未被调用
    expect(mockedShowToast).not.toHaveBeenCalled();
    // 正向对照：草稿仍在（收口不是删除——下次进 /single 照常恢复）
    const still = localStorage.getItem(scopedStorageKey(STORAGE_KEYS.PLAYGROUND_DRAFT, 'tester'));
    expect(still).not.toBeNull();
    expect(JSON.parse(JSON.parse(still as string)).version).toBe(PLAYGROUND_DRAFT_VERSION);
    // 状态未被恢复：fileInfo 为空（恢复会把 file_id=f1 灌进来）
    expect(harness.pg?.fileInfo?.file_id ?? null).toBeNull();
  });

  it('(b) /single 路由：草稿照常恢复 + toast 恰好一次（正向，既有能力不回归）', async () => {
    seedDraft();
    renderAt('/single');
    await act(async () => {
      await Promise.resolve();
    });
    await waitFor(() => expect(harness.pg?.fileInfo?.file_id).toBe('f1'));
    // 正向：toast 恰好 1 次且文案正确
    expect(mockedShowToast).toHaveBeenCalledTimes(1);
    expect(mockedShowToast.mock.calls[0][0]).toBe('已恢复上次单文件处理现场');
    expect(mockedShowToast.mock.calls[0][1]).toBe('info');
  });

  it('(c) 切走再回：/single 恢复 → 非 single 不弹 → 回 /single 再恢复（每挂载≤1 次）', async () => {
    seedDraft();
    const first = renderAt('/single');
    await waitFor(() => expect(harness.pg?.fileInfo?.file_id).toBe('f1'));
    expect(mockedShowToast).toHaveBeenCalledTimes(1);
    first.unmount();

    // 切到 /jobs（新挂载）：不弹
    renderAt('/jobs');
    await act(async () => {
      await Promise.resolve();
    });
    expect(mockedShowToast).toHaveBeenCalledTimes(1);

    // 回 /single（新挂载）：再恢复、再弹一次（累计 2 = 每挂载至多一次）
    renderAt('/single');
    await waitFor(() => expect(mockedShowToast).toHaveBeenCalledTimes(2));
  });

  it('(d) ?file_id= 深链：挂载恢复的 file_id 闸语义钉（jsdom 无真 URL，钉表达式契约）', async () => {
    // 背景：挂载 effect 的 file_id 闸读 window.location.search——jsdom 下该值恒空
    // （MemoryRouter 不改 window.location），直接测深链行为会假绿/假红。生产语义
    // =「URL 带 ?file_id= 时跳过挂载恢复（走 resumeFromFile 协议）」由代码走查钉：
    // 这里钉闸的表达式本身——含 file_id 参数即 return，防止闸被顺手删除。
    const hookSource = hookRaw;
    // 闸仍在：file_id 查询参数 → return（挂载恢复被跳过）
    expect(hookSource).toMatch(
      /if \(new URLSearchParams\(window\.location\.search\)\.get\('file_id'\)\) return;/,
    );
    // 正向对照：/single 闸在 file_id 之后（顺序钉——file_id 先判，路由闸后判）
    const fileIdIdx = hookSource.indexOf("get('file_id')");
    const routeIdx = hookSource.indexOf("location.pathname.startsWith('/single')");
    expect(fileIdIdx).toBeGreaterThan(-1);
    expect(routeIdx).toBeGreaterThan(fileIdIdx);
    // 行为对照（同文件既有协议测试已覆盖 resumeFromFile 分流）：本用例渲染不炸
    seedDraft();
    renderAt('/single?file_id=f9');
    await act(async () => {
      await Promise.resolve();
    });
    expect(harness.pg).not.toBeNull();
  });
});
