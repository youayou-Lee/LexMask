// Copyright 2026 LexMask Contributors

// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { DocsPage, resolveAdjacentSlug } from './docs-page';
import type { DocsEntry } from './docs-manifest';

// 页面本身从 `./docs-manifest` 取清单与原文；测试里把这两个异步入口换成夹具，
// 保持 `buildDocsNav`（纯函数、真实实现）与真实模块其余部分不变。
const mocks = vi.hoisted(() => ({
  getAllDocsEntries: vi.fn(),
  loadDocRaw: vi.fn(),
}));

vi.mock('./docs-manifest', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./docs-manifest')>();
  return {
    ...actual,
    getAllDocsEntries: mocks.getAllDocsEntries,
    loadDocRaw: mocks.loadDocRaw,
  };
});

const entries: DocsEntry[] = [
  { slug: 'test-doc', title: '测试文档', category: '新手必读', order: 1, updated: '2026-11-17' },
  { slug: 'other-doc', title: '另一篇', category: '操作指南', order: 2, updated: '2026-11-17' },
];

const DOC_RAW = [
  '---',
  'title: 测试文档',
  'category: 新手必读',
  'order: 1',
  'updated: 2026-11-17',
  '---',
  '',
  '# 你好',
  '',
  '这是 **测试** 内容。',
].join('\n');

function LocationProbe() {
  const location = useLocation();
  return <div data-testid="location-probe">{location.pathname}</div>;
}

/** 同时挂 `/docs` 与 `/docs/:slug`（真实路由由 Task 4 提供），便于钉住重定向。 */
function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/docs" element={<DocsPage />} />
        <Route path="/docs/:slug" element={<DocsPage />} />
      </Routes>
      <LocationProbe />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  mocks.getAllDocsEntries.mockReset().mockResolvedValue(entries);
  mocks.loadDocRaw.mockReset().mockResolvedValue(DOC_RAW);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('resolveAdjacentSlug', () => {
  it('中间篇返回前后', () => {
    expect(resolveAdjacentSlug('test-doc', entries)).toEqual({ prev: null, next: 'other-doc' });
  });
  it('末篇 next 为 null', () => {
    expect(resolveAdjacentSlug('other-doc', entries)).toEqual({ prev: 'test-doc', next: null });
  });
  it('未知 slug 前后均为 null', () => {
    expect(resolveAdjacentSlug('ghost', entries)).toEqual({ prev: null, next: null });
  });
});

describe('DocsPage', () => {
  it('渲染导航树（分类 + 标题）与正文（react-markdown 渲染标题）', async () => {
    renderAt('/docs/test-doc');
    // 正文：frontmatter 已剥离，`# 你好` 由 react-markdown 渲染为 h1。
    expect(await screen.findByRole('heading', { name: '你好', level: 1 })).toBeTruthy();
    // 导航树：分类标签 + 两篇标题 + 当前篇高亮链接。
    expect(screen.getByText('新手必读')).toBeTruthy();
    expect(screen.getByText('操作指南')).toBeTruthy();
    expect(screen.getByRole('link', { name: '另一篇' }).getAttribute('href')).toBe(
      '/docs/other-doc',
    );
    expect(screen.getAllByRole('link', { name: '测试文档' }).length).toBeGreaterThan(0);
    expect(mocks.loadDocRaw).toHaveBeenCalledWith('test-doc');
  });

  it('无 slug（/docs）时重定向到第一篇', async () => {
    renderAt('/docs');
    expect(await screen.findByRole('heading', { name: '你好', level: 1 })).toBeTruthy();
    expect(screen.getByTestId('location-probe').textContent).toBe('/docs/test-doc');
  });

  it('上/下篇按清单顺序渲染并链到相邻文档', async () => {
    const first = renderAt('/docs/test-doc');
    expect(await screen.findByRole('heading', { name: '你好', level: 1 })).toBeTruthy();
    expect(screen.getByRole('link', { name: /下一篇/ }).getAttribute('href')).toBe(
      '/docs/other-doc',
    );
    expect(screen.queryByRole('link', { name: /上一篇/ })).toBeNull();
    first.unmount();

    renderAt('/docs/other-doc');
    expect(await screen.findByRole('link', { name: /上一篇/ })).toBeTruthy();
    expect(screen.getByRole('link', { name: /上一篇/ }).getAttribute('href')).toBe(
      '/docs/test-doc',
    );
    expect(screen.queryByRole('link', { name: /下一篇/ })).toBeNull();
  });

  it('加载失败时展示错误提示而不是空白', async () => {
    mocks.loadDocRaw.mockRejectedValue(new Error('网络异常'));
    renderAt('/docs/test-doc');
    expect(await screen.findByText(/文档加载失败：网络异常/)).toBeTruthy();
  });
});
