// Copyright 2026 LexMask Contributors

// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import {
  MemoryRouter,
  Route,
  RouterProvider,
  Routes,
  createMemoryRouter,
  useLocation,
} from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { DocsPage, resolveAdjacentSlug } from './docs-page';
import type { DocsDetail, DocsNavItem, DocsListResponse } from '@/services/docsApi';

// 页面从 `@/services/docsApi` 取清单与正文；测试只替换这两个网络入口。
const mocks = vi.hoisted(() => ({
  fetchDocsList: vi.fn(),
  fetchDoc: vi.fn(),
}));

vi.mock('@/services/docsApi', () => ({
  fetchDocsList: mocks.fetchDocsList,
  fetchDoc: mocks.fetchDoc,
}));

const entries: DocsNavItem[] = [
  {
    slug: 'doc-a',
    title: '甲篇',
    section: '第一章 · 甲',
    summary: '',
    status: 'canonical',
    order: 1,
    updated: '2026-11-17',
  },
  {
    slug: 'doc-b',
    title: '乙篇',
    section: '第一章 · 甲',
    summary: '',
    status: 'canonical',
    order: 2,
    updated: '2026-11-17',
  },
  {
    slug: 'doc-c',
    title: '丙篇',
    section: '第二章 · 乙',
    summary: '',
    status: 'canonical',
    order: 1,
    updated: '2026-11-17',
  },
];

const list: DocsListResponse = {
  index: 'doc-b',
  sections: [
    { category: '新手必读', items: entries.slice(0, 2) },
    { category: '操作指南', items: entries.slice(2) },
  ],
};

const DOC_BODY = [
  '# 你好',
  '',
  '这是 **测试** 内容。',
  '',
  '## 快速开始',
  '',
  '第一段。',
  '',
  '### 安装依赖',
  '',
  '第二段。',
  '',
  '## 常见问题',
  '',
  '第三段。',
].join('\n');

function detailForSlug(slug: string): DocsDetail {
  const entry = entries.find((e) => e.slug === slug);
  if (!entry) throw new Error(`未注册的文档 slug：${slug}`);
  const category =
    list.sections.find((section) => section.items.some((item) => item.slug === slug))
      ?.category ?? '';
  return {
    slug,
    title: entry.title,
    category,
    status: entry.status,
    updated: entry.updated,
    summary: entry.summary,
    content: DOC_BODY,
    links: [],
    backlinks: [],
    canonical: null,
  };
}

function LocationProbe() {
  const location = useLocation();
  return <div data-testid="location-probe">{location.pathname}</div>;
}

/** 同时挂 `/docs` 与 `/docs/:slug`，便于钉住重定向。 */
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
  mocks.fetchDocsList.mockReset().mockResolvedValue(list);
  mocks.fetchDoc.mockReset().mockImplementation(async (slug: string) => detailForSlug(slug));
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('resolveAdjacentSlug', () => {
  it('首篇 prev 为 null、next 为下一位', () => {
    expect(resolveAdjacentSlug('doc-a', entries)).toEqual({ prev: null, next: 'doc-b' });
  });
  it('中间篇 prev 与 next 同时非空', () => {
    expect(resolveAdjacentSlug('doc-b', entries)).toEqual({ prev: 'doc-a', next: 'doc-c' });
  });
  it('末篇 next 为 null', () => {
    expect(resolveAdjacentSlug('doc-c', entries)).toEqual({ prev: 'doc-b', next: null });
  });
  it('未知 slug 前后均为 null', () => {
    expect(resolveAdjacentSlug('ghost', entries)).toEqual({ prev: null, next: null });
  });
  it('按传入数组顺序取相邻（后端已按分类/order 排好）', () => {
    expect(resolveAdjacentSlug('doc-b', entries).next).toBe('doc-c');
  });
});

describe('DocsPage', () => {
  it('渲染导航树（分类 + 标题）与正文（react-markdown 渲染标题）', async () => {
    renderAt('/docs/doc-b');
    expect(await screen.findByRole('heading', { name: '你好', level: 1 })).toBeTruthy();
    const nav = screen.getByRole('navigation', { name: '文档目录' });
    expect(
      within(nav)
        .getAllByRole('link')
        .map((a) => a.textContent),
    ).toEqual(['甲篇', '乙篇', '丙篇']);
    expect(within(nav).getByRole('link', { name: '丙篇' }).getAttribute('href')).toBe(
      '/docs/doc-c',
    );
    // 章节层级：同一 section 的文档归在一组，组标题可见。
    expect(within(nav).getByText('第一章 · 甲')).toBeTruthy();
    expect(within(nav).getByText('第二章 · 乙')).toBeTruthy();
    expect(mocks.fetchDoc).toHaveBeenCalledWith('doc-b');
  });

  it('正文由后端返回（不含 frontmatter），页面原样渲染', async () => {
    renderAt('/docs/doc-b');
    expect(await screen.findByRole('heading', { name: '你好', level: 1 })).toBeTruthy();
    expect(screen.queryByText(/title:/)).toBeNull();
    expect(screen.queryByText(/category:/)).toBeNull();
    expect(screen.queryByText(/^---$/)).toBeNull();
  });

  it('无 slug（/docs）时重定向到后端返回的索引页', async () => {
    renderAt('/docs');
    expect(await screen.findByRole('heading', { name: '你好', level: 1 })).toBeTruthy();
    expect(screen.getByTestId('location-probe').textContent).toBe('/docs/doc-b');
  });

  it('未知 slug 重定向回文档中心首页，不请求该 slug', async () => {
    renderAt('/docs/ghost-slug');
    expect(await screen.findByRole('heading', { name: '你好', level: 1 })).toBeTruthy();
    expect(screen.getByTestId('location-probe').textContent).toBe('/docs/doc-b');
    expect(mocks.fetchDoc).not.toHaveBeenCalledWith('ghost-slug');
    expect(screen.queryByText(/文档加载失败/)).toBeNull();
  });

  it('上/下篇按清单顺序渲染并链到相邻文档', async () => {
    const first = renderAt('/docs/doc-a');
    expect(await screen.findByRole('heading', { name: '你好', level: 1 })).toBeTruthy();
    expect(screen.getByRole('link', { name: /下一篇/ }).getAttribute('href')).toBe('/docs/doc-b');
    expect(screen.queryByRole('link', { name: /上一篇/ })).toBeNull();
    first.unmount();

    const middle = renderAt('/docs/doc-b');
    expect(await screen.findByRole('link', { name: /上一篇/ })).toBeTruthy();
    expect(screen.getByRole('link', { name: /上一篇/ }).getAttribute('href')).toBe('/docs/doc-a');
    expect(screen.getByRole('link', { name: /下一篇/ }).getAttribute('href')).toBe('/docs/doc-c');
    middle.unmount();

    renderAt('/docs/doc-c');
    expect(await screen.findByRole('link', { name: /上一篇/ })).toBeTruthy();
    expect(screen.getByRole('link', { name: /上一篇/ }).getAttribute('href')).toBe('/docs/doc-b');
    expect(screen.queryByRole('link', { name: /下一篇/ })).toBeNull();
  });

  it('本页目录：列出正文 h2/h3、锚点与标题一一对应、点击滚动到对应标题', async () => {
    renderAt('/docs/doc-b');
    expect(await screen.findByRole('heading', { name: '你好', level: 1 })).toBeTruthy();
    const toc = screen.getByRole('complementary', { name: '本页目录' });
    const links = within(toc).getAllByRole('link');
    expect(links.map((a) => a.textContent)).toEqual(['快速开始', '安装依赖', '常见问题']);
    expect(links[0].getAttribute('href')).toBe('#快速开始');
    for (const link of links) {
      const id = (link.getAttribute('href') ?? '').replace(/^#/, '');
      const heading = document.getElementById(id);
      expect(heading?.tagName).toMatch(/^H[23]$/);
      expect(heading?.textContent).toBe(link.textContent);
    }
    const scrollIntoView = vi.fn();
    const original = Element.prototype.scrollIntoView;
    Element.prototype.scrollIntoView = scrollIntoView;
    try {
      fireEvent.click(links[1]);
    } finally {
      Element.prototype.scrollIntoView = original;
    }
    expect(scrollIntoView).toHaveBeenCalledTimes(1);
    expect(scrollIntoView.mock.instances[0]).toBe(document.getElementById('安装依赖'));
  });

  it('窄屏目录默认收起，展开后可见导航项（抽屉）', async () => {
    renderAt('/docs/doc-b');
    expect(await screen.findByRole('heading', { name: '你好', level: 1 })).toBeTruthy();
    const toggle = screen.getByRole('button', { name: /文档目录/ });
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
    expect(screen.queryByRole('navigation', { name: '文档目录（窄屏）' })).toBeNull();

    fireEvent.click(toggle);
    expect(toggle.getAttribute('aria-expanded')).toBe('true');
    const drawer = screen.getByRole('navigation', { name: '文档目录（窄屏）' });
    expect(
      within(drawer)
        .getAllByRole('link')
        .map((a) => a.textContent),
    ).toEqual(['甲篇', '乙篇', '丙篇']);
  });

  it('切换文档时窄屏抽屉自动收起', async () => {
    const router = createMemoryRouter(
      [
        { path: '/docs', element: <DocsPage /> },
        { path: '/docs/:slug', element: <DocsPage /> },
      ],
      { initialEntries: ['/docs/doc-b'] },
    );
    render(<RouterProvider router={router} />);
    expect(await screen.findByRole('heading', { name: '你好', level: 1 })).toBeTruthy();

    const toggle = screen.getByRole('button', { name: /文档目录/ });
    fireEvent.click(toggle);
    expect(toggle.getAttribute('aria-expanded')).toBe('true');

    await act(async () => {
      await router.navigate('/docs/doc-a');
    });
    await waitFor(() => expect(toggle.getAttribute('aria-expanded')).toBe('false'));
    expect(screen.queryByRole('navigation', { name: '文档目录（窄屏）' })).toBeNull();
  });

  it('加载失败时展示错误提示而不是空白', async () => {
    mocks.fetchDoc.mockRejectedValue(new Error('网络异常'));
    renderAt('/docs/doc-b');
    expect(await screen.findByText(/文档加载失败：网络异常/)).toBeTruthy();
  });

  it('反链面板：展示被引用文档并链回', async () => {
    mocks.fetchDoc.mockImplementation(async (slug: string) => {
      const detail = detailForSlug(slug);
      return slug === 'doc-b'
        ? { ...detail, backlinks: [{ slug: 'doc-a', title: '甲篇' }] }
        : detail;
    });
    renderAt('/docs/doc-b');
    expect(await screen.findByRole('heading', { name: '你好', level: 1 })).toBeTruthy();
    const backlinks = screen.getByRole('region', { name: '被引用' });
    expect(within(backlinks).getByRole('link', { name: '甲篇' }).getAttribute('href')).toBe(
      '/docs/doc-a',
    );
  });

  it('摘要页展示「正本」提示并链到 canonical', async () => {
    mocks.fetchDoc.mockImplementation(async (slug: string) => {
      const detail = detailForSlug(slug);
      return slug === 'doc-b'
        ? { ...detail, status: 'summary', canonical: 'doc-a' }
        : detail;
    });
    renderAt('/docs/doc-b');
    expect(await screen.findByText(/本文为摘要/)).toBeTruthy();
    const banner = screen.getByText(/本文为摘要/).closest('p');
    expect(banner).not.toBeNull();
    expect(
      within(banner as HTMLElement).getByRole('link', { name: '甲篇' }).getAttribute('href'),
    ).toBe('/docs/doc-a');
  });
});