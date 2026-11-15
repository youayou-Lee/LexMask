// Copyright 2026 LexMask Contributors

// @vitest-environment jsdom

// Issue#327 门⑥ C-4 返工：子集预览结果页的逐页取图必须发**原卷页号**。
//
// 缺陷实证（评审 C-4）：`page-image?preview=true` 的 `page` 是原卷页号（后端用
// `page_map` 反查），而结果页翻页器走的是子集序号 1..k。直接发子集序号 ⇒
// 非前缀子集（如第 20-73 页）每一页都 404 `preview_page_not_covered`。
//
// 本文件钉「子集序号 → 原卷页号」的整链：props.previewPageMap → useOutputPagePreview
// → 请求 URL。用**非恒等** page_map（子集序号 1 → 原卷页号 20），恒等映射会让方向
// 写反也照样绿（这正是第一版实现能骗过自己的原因）。

import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { authFetch } from '@/services/api-client';
import { useSingleTask, type SingleTaskView } from '@/features/single-task/single-task-context';

import type { FileInfo } from '../types';
import { PlaygroundResult } from './playground-result';

vi.mock('@/features/single-task/single-task-context', () => ({ useSingleTask: vi.fn() }));
vi.mock('@/services/jobsApi', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/services/jobsApi')>()),
  getJob: vi.fn(async () => ({ items: [], performance: null })),
}));
vi.mock('@/services/api-client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/services/api-client')>();
  return { ...actual, authFetch: vi.fn() };
});
vi.mock('./playground-page-export', () => ({
  PlaygroundPageExport: () => <div data-testid="playground-page-export" />,
}));

const mockedUseSingleTask = vi.mocked(useSingleTask);
const mockedAuthFetch = vi.mocked(authFetch);

/** jsdom 无 ResizeObserver（结果页图片视口的 useImageViewport 需要）——哑实现。 */
class FakeResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}

const FILE: FileInfo = {
  file_id: 'f1',
  filename: '卷.pdf',
  file_size: 2048,
  file_type: 'pdf',
  page_count: 86,
};

/** 子集 = 原卷第 20-73 页（54 页）：page_map 是「子集序号 → 原卷页号」。 */
const PAGE_MAP: Record<string, number> = { '1': 20, '2': 21, '54': 73 };

function mockProvider() {
  const task = {
    jobId: 'j1',
    itemId: 'it1',
    fileId: 'f1',
    filename: '卷.pdf',
    phase: 'recognizing',
    current: 54,
    total: 86,
    message: null,
    error: null,
    performance: null,
  } as SingleTaskView;
  mockedUseSingleTask.mockReturnValue({
    tasks: [],
    taskForFile: (fileId: string) => (fileId === 'f1' ? task : undefined),
    watch: vi.fn(),
    refresh: vi.fn(async () => undefined),
  });
}

function renderResult(props: Partial<React.ComponentProps<typeof PlaygroundResult>> = {}) {
  return render(
    <PlaygroundResult
      fileInfo={FILE}
      content=""
      entities={[]}
      entityMap={{}}
      redactedCount={54}
      redactionReport={null}
      reportOpen={false}
      setReportOpen={() => undefined}
      versionHistory={[]}
      versionHistoryOpen={false}
      setVersionHistoryOpen={() => undefined}
      isImageMode={false}
      imageUrl=""
      currentPage={1}
      totalPages={86}
      onPageChange={() => undefined}
      visibleBoxes={[]}
      boundingBoxes={[]}
      visionTypes={[]}
      getVisionTypeConfig={() => ({ name: '', color: '' })}
      onBackToEdit={() => undefined}
      onReset={() => undefined}
      onDownload={() => undefined}
      {...props}
    />,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.stubGlobal('ResizeObserver', FakeResizeObserver);
  mockProvider();
  // 页图请求返回一个极小的 PNG 响应（内容不解析，只看 URL）
  mockedAuthFetch.mockResolvedValue({
    ok: true,
    status: 200,
    headers: new Headers({ 'Content-Type': 'image/png', 'X-Page-Count': '54' }),
    blob: async () => new Blob(['x'], { type: 'image/png' }),
  } as unknown as Response);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('Issue#327：子集结果页取图发原卷页号（C-4）', () => {
  it('preview + page_map：子集第 1 页 → page-image?page=20&preview=true', async () => {
    renderResult({
      preview: true,
      previewPageCount: 54,
      previewPageMap: PAGE_MAP,
    });

    await waitFor(() =>
      expect(
        mockedAuthFetch.mock.calls.some(([input]) =>
          String(input).includes('/api/v1/files/f1/page-image?page=20&redacted=true&preview=true'),
        ),
      ).toBe(true),
    );
  });

  it('无 page_map（前缀子集/未给映射）：原样发子集序号，不 404 到 0', async () => {
    renderResult({ preview: true, previewPageCount: 54 });

    await waitFor(() =>
      expect(
        mockedAuthFetch.mock.calls.some(([input]) =>
          String(input).includes('/api/v1/files/f1/page-image?page=1&redacted=true&preview=true'),
        ),
      ).toBe(true),
    );
  });

  it('子集口径下不渲染「对照｜成品」切换（识别在飞没有可对照的框）', () => {
    renderResult({ preview: true, previewPageCount: 54, previewPageMap: PAGE_MAP });
    expect(screen.queryByTestId('playground-result-view-toggle')).toBeNull();
  });
});