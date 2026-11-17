// Copyright 2026 LexMask Contributors

// Issue#348：/docs 路由接线契约——把 DOCS_ROUTES 片段直挂 MemoryRouter，钉三件事：
// ① 片段恰含 docs / docs/:slug 两条（不漏挂、不额外挂别的）；
// ② `/docs` 重定向到清单首篇（replace），落点即文档中心组件；
// ③ `docs/:slug` 挂的是 DocsPage（lazy 导入 + LazyPage 包裹）。
// 页面自身的渲染行为在 docs-page.test.tsx 覆盖，此处只钉「路由 → 组件」接线。

// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { DOCS_MANIFEST } from './features/docs/docs-manifest';
import { DOCS_ROUTES } from './router';

// 用 stub 顶替 DocsPage：本文件只验证接线，不重复页面渲染断言（也就不依赖文档内容）。
vi.mock('./features/docs/docs-page', () => ({
  DocsPage: () => <div data-testid="docs-page-stub" />,
}));

const docsRoutes = DOCS_ROUTES.filter((r) => r.path.startsWith('docs'));
/** 清单首篇 = `getAllDocsEntries()` 的数组首位 = 文档页的阅读顺序首篇。 */
const FIRST_SLUG = DOCS_MANIFEST[0].slug;

function LocationProbe() {
  const location = useLocation();
  return <div data-testid="location-probe">{location.pathname}</div>;
}

function renderDocsRoutes(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        {DOCS_ROUTES.map((r) => (
          <Route key={r.path} path={r.path} element={r.element} />
        ))}
        <Route path="*" element={<div data-testid="docs-route-fallback">未命中文档路由</div>} />
      </Routes>
      <LocationProbe />
    </MemoryRouter>,
  );
}

afterEach(() => cleanup());

describe('文档中心路由（#348）', () => {
  it('路由表含 /docs 与 /docs/:slug 且指向文档中心元素', () => {
    expect(docsRoutes.map((r) => r.path).sort()).toEqual(['docs', 'docs/:slug']);
    for (const route of docsRoutes) {
      expect(route.element).toBeTruthy();
    }
  });

  it('/docs 重定向到清单首篇', async () => {
    renderDocsRoutes('/docs');
    expect(await screen.findByTestId('docs-page-stub')).toBeTruthy();
    expect(screen.getByTestId('location-probe').textContent).toBe(`/docs/${FIRST_SLUG}`);
    expect(screen.queryByTestId('docs-route-fallback')).toBeNull();
  });

  it('/docs/:slug 渲染文档中心组件（lazy 导入 DocsPage）', async () => {
    renderDocsRoutes('/docs/some-doc');
    expect(await screen.findByTestId('docs-page-stub')).toBeTruthy();
    expect(screen.getByTestId('location-probe').textContent).toBe('/docs/some-doc');
    expect(screen.queryByTestId('docs-route-fallback')).toBeNull();
  });
});
