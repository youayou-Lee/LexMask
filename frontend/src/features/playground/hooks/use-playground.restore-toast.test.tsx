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

import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { useEffect, useRef } from 'react';
import { MemoryRouter, Route, Routes, useNavigate } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { STORAGE_KEYS } from '@/constants/storage-keys';
import { buildDraftSnapshot, serializeDraft, PLAYGROUND_DRAFT_VERSION } from '../lib/playground-draft';
import { scopedStorageKey } from '@/lib/storage';
import { showToast } from '@/components/Toast';
import { SingleTaskProvider } from '@/features/single-task/single-task-context';
import type { FileInfo } from '@/features/playground/types';

import { usePlayground } from './use-playground';

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
    // 状态未被恢复：fileInfo 为空（恢复会把 file_id=f1 灌进来）。
    // 挂载证明（门⑥ Minor-5）：pg 非 null，否则本用例在 provider 抛错时也恒绿。
    expect(harness.pg).not.toBeNull();
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

  it('(c2) 生产拓扑回归（门⑥ Critical-1）：/jobs 起步不烧 ref → 应用内切 /single 仍恢复', async () => {
    // Provider 常驻、不随路由重建——冷审用探针证实旧实现在此拓扑下草稿永不恢复。
    // 用 Routes+Navigate 探针在**同一挂载**内切路由，复刻生产拓扑。
    seedDraft();
    function NavProbe() {
      const nav = useNavigate();
      return (
        <button
          type="button"
          data-testid="nav-to-single"
          onClick={() => nav('/single')}
        >
          go
        </button>
      );
    }
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false, gcTime: 0 } },
    });
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={['/jobs']}>
          <Routes>
            <Route path="/jobs" element={<NavProbe />} />
            <Route path="/single" element={<SingleTaskProvider><Probe /></SingleTaskProvider>} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );
    // /jobs 挂载：不恢复不弹
    await act(async () => {
      await Promise.resolve();
    });
    expect(mockedShowToast).not.toHaveBeenCalled();
    // 应用内切到 /single（同 Provider 实例，不 unmount）
    fireEvent.click(screen.getByTestId('nav-to-single'));
    await waitFor(() => expect(harness.pg?.fileInfo?.file_id).toBe('f1'));
    expect(mockedShowToast).toHaveBeenCalledTimes(1);
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

  it('(d) ?file_id= 深链：挂载恢复被跳过（真行为断言，history.replaceState 改 search）', async () => {
    // 门⑥ Minor-3：jsdom 支持同源 history.replaceState ⇒ window.location.search 可写，
    // 不需要 ?raw 表达式契约钉——直接断言组合行为。
    seedDraft();
    window.history.replaceState({}, '', '/single?file_id=f9');
    try {
      renderAt('/single');
      await act(async () => {
        await Promise.resolve();
      });
      // 深链协议接管：挂载恢复不弹「已恢复」（负向）
      expect(mockedShowToast).not.toHaveBeenCalledWith('已恢复上次单文件处理现场', 'info');
      // 正向对照：草稿未被挂载恢复（fileInfo 空）——闸生效的直接证据
      expect(harness.pg).not.toBeNull();
      expect(harness.pg?.fileInfo?.file_id ?? null).toBeNull();
    } finally {
      window.history.replaceState({}, '', '/single');
    }
  });
});
