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

/**
 * 夹具刻意让「数组顺序 ≠ order 升序」：
 * - 数组顺序 = 登记/阅读顺序（`getAllDocsEntries()` 的契约），上下篇必须按它取相邻；
 * - order = 分类内序号，导航树按它排序。
 * 两者不同，才能把「上下篇取数组下标」与「导航树按 order 排」分别钉死。
 */
const entries: DocsEntry[] = [
  { slug: 'doc-b', title: '乙篇', category: '新手必读', order: 2, updated: '2026-11-17' },
  { slug: 'doc-a', title: '甲篇', category: '新手必读', order: 1, updated: '2026-11-17' },
  { slug: 'doc-c', title: '丙篇', category: '操作指南', order: 1, updated: '2026-11-17' },
];

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

/** `loadDocRaw` 的真实契约：返回**含 frontmatter** 的原文（页面负责剥离）。 */
function rawForSlug(slug: string): string {
  const entry = entries.find((e) => e.slug === slug);
  if (!entry) throw new Error(`未注册的文档 slug：${slug}`);
  return [
    '---',
    `title: ${entry.title}`,
    `category: ${entry.category}`,
    `order: ${entry.order}`,
    'updated: 2026-11-17',
    '---',
    '',
    DOC_BODY,
  ].join('\n');
}

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
  mocks.loadDocRaw.mockReset().mockImplementation(async (slug: string) => rawForSlug(slug));
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('resolveAdjacentSlug', () => {
  it('首篇 prev 为 null、next 为数组下一位', () => {
    expect(resolveAdjacentSlug('doc-b', entries)).toEqual({ prev: null, next: 'doc-a' });
  });
  it('中间篇 prev 与 next 同时非空', () => {
    expect(resolveAdjacentSlug('doc-a', entries)).toEqual({ prev: 'doc-b', next: 'doc-c' });
  });
  it('末篇 next 为 null', () => {
    expect(resolveAdjacentSlug('doc-c', entries)).toEqual({ prev: 'doc-a', next: null });
  });
  it('未知 slug 前后均为 null', () => {
    expect(resolveAdjacentSlug('ghost', entries)).toEqual({ prev: null, next: null });
  });
  it('不按 order 重排：数组顺序与 order 顺序不同时仍取数组相邻元素', () => {
    expect(entries.map((e) => e.slug)).toEqual(['doc-b', 'doc-a', 'doc-c']);
    // 乙篇 order=2 > 甲篇 order=1，但数组里乙在甲之前 ⇒ 乙的下一篇是甲（不是丙）。
    expect(resolveAdjacentSlug('doc-b', entries).next).toBe('doc-a');
    expect(resolveAdjacentSlug('doc-a', entries).prev).toBe('doc-b');
    // 若按 order 重排（甲→乙→丙），甲的 prev 会是 null、next 是乙——与此相反。
    expect(resolveAdjacentSlug('doc-a', entries)).toEqual({ prev: 'doc-b', next: 'doc-c' });
  });
});

describe('DocsPage', () => {
  it('渲染导航树（分类 + 标题）与正文（react-markdown 渲染标题）', async () => {
    renderAt('/docs/doc-b');
    // 正文：frontmatter 已剥离，`# 你好` 由 react-markdown 渲染为 h1。
    expect(await screen.findByRole('heading', { name: '你好', level: 1 })).toBeTruthy();
    const nav = screen.getByRole('navigation', { name: '文档目录' });
    // 分类内按 order 升序（甲 order=1 → 乙 order=2），分类间「新手必读」在前。
    expect(
      within(nav)
        .getAllByRole('link')
        .map((a) => a.textContent),
    ).toEqual(['甲篇', '乙篇', '丙篇']);
    expect(within(nav).getByRole('link', { name: '丙篇' }).getAttribute('href')).toBe(
      '/docs/doc-c',
    );
    expect(screen.getAllByRole('link', { name: '乙篇' }).length).toBeGreaterThan(0);
    expect(mocks.loadDocRaw).toHaveBeenCalledWith('doc-b');
  });

  it('正文不渲染 frontmatter（原文含 frontmatter，渲染前剥离）', async () => {
    renderAt('/docs/doc-b');
    expect(await screen.findByRole('heading', { name: '你好', level: 1 })).toBeTruthy();
    expect(screen.queryByText(/title:/)).toBeNull();
    expect(screen.queryByText(/category:/)).toBeNull();
    expect(screen.queryByText(/updated:/)).toBeNull();
    expect(screen.queryByText(/^---$/)).toBeNull();
  });

  it('无 slug（/docs）时重定向到第一篇', async () => {
    renderAt('/docs');
    expect(await screen.findByRole('heading', { name: '你好', level: 1 })).toBeTruthy();
    expect(screen.getByTestId('location-probe').textContent).toBe('/docs/doc-b');
  });

  // I-1：未知 slug 不能渲染空正文/错误页——回文档中心首页（真实路由里 /docs 再重定向到首篇）。
  it('未知 slug 重定向回文档中心首页（不渲染空正文、不落加载失败）', async () => {
    renderAt('/docs/ghost-slug');
    // 重定向链落地到清单首篇，正文正常渲染。
    expect(await screen.findByRole('heading', { name: '你好', level: 1 })).toBeTruthy();
    expect(screen.getByTestId('location-probe').textContent).toBe('/docs/doc-b');
    // 关键：不再拿未知 slug 去请求原文（否则错误态会盖住重定向）。
    expect(mocks.loadDocRaw).not.toHaveBeenCalledWith('ghost-slug');
    expect(screen.queryByText(/文档加载失败/)).toBeNull();
  });

  it('上/下篇按清单数组顺序渲染并链到相邻文档', async () => {
    const first = renderAt('/docs/doc-b');
    expect(await screen.findByRole('heading', { name: '你好', level: 1 })).toBeTruthy();
    expect(screen.getByRole('link', { name: /下一篇/ }).getAttribute('href')).toBe('/docs/doc-a');
    expect(screen.queryByRole('link', { name: /上一篇/ })).toBeNull();
    first.unmount();

    const middle = renderAt('/docs/doc-a');
    expect(await screen.findByRole('link', { name: /上一篇/ })).toBeTruthy();
    expect(screen.getByRole('link', { name: /上一篇/ }).getAttribute('href')).toBe('/docs/doc-b');
    expect(screen.getByRole('link', { name: /下一篇/ }).getAttribute('href')).toBe('/docs/doc-c');
    middle.unmount();

    renderAt('/docs/doc-c');
    expect(await screen.findByRole('link', { name: /上一篇/ })).toBeTruthy();
    expect(screen.getByRole('link', { name: /上一篇/ }).getAttribute('href')).toBe('/docs/doc-a');
    expect(screen.queryByRole('link', { name: /下一篇/ })).toBeNull();
  });

  it('本页目录：列出正文 h2/h3、锚点与标题一一对应、点击滚动到对应标题', async () => {
    renderAt('/docs/doc-b');
    expect(await screen.findByRole('heading', { name: '你好', level: 1 })).toBeTruthy();
    const toc = screen.getByRole('complementary', { name: '本页目录' });
    const links = within(toc).getAllByRole('link');
    // 只取 h2/h3（正文 h1「你好」不进目录），按出现顺序。
    expect(links.map((a) => a.textContent)).toEqual(['快速开始', '安装依赖', '常见问题']);
    expect(links[0].getAttribute('href')).toBe('#快速开始');
    // 每个锚点都能在正文里找到 id 对应、文本相同的标题（锚点 id 与渲染文本同源）。
    for (const link of links) {
      const id = (link.getAttribute('href') ?? '').replace(/^#/, '');
      const heading = document.getElementById(id);
      expect(heading?.tagName).toMatch(/^H[23]$/);
      expect(heading?.textContent).toBe(link.textContent);
    }
    // 点击锚点滚动到对应标题。
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

    fireEvent.click(toggle);
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
    expect(screen.queryByRole('navigation', { name: '文档目录（窄屏）' })).toBeNull();
  });

  // M-3：切换文档时抽屉必须收起——展开着的抽屉会挡住新一篇的正文。
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
    mocks.loadDocRaw.mockRejectedValue(new Error('网络异常'));
    renderAt('/docs/doc-b');
    expect(await screen.findByText(/文档加载失败：网络异常/)).toBeTruthy();
  });
});
