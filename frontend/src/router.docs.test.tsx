// Copyright 2026 LexMask Contributors

// Issue#348 / #368：/docs 路由接线契约——把 DOCS_ROUTES 片段直挂 MemoryRouter，钉四件事：
// ① 片段恰含 docs / docs/:slug 两条（不漏挂、不额外挂别的）；
// ② `/docs` 渲染文档中心组件本身（落点由页面按后端索引页决定，不再前端硬编码）；
// ③ `docs/:slug` 挂的是 DocsPage（lazy 导入 + LazyPage 包裹）；
// ④ 这两条真的被 `createBrowserRouter` 展开进 `/`（RequireAuth + Layout）的 children。
// 页面自身的渲染行为在 docs-page.test.tsx 覆盖，此处只钉「路由 → 组件」接线。

// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { DOCS_ROUTES, router } from './router';

// 用 stub 顶替 DocsPage：本文件只验证接线，不重复页面渲染断言（也就不依赖文档内容）。
vi.mock('./features/docs/docs-page', () => ({
  DocsPage: () => <div data-testid="docs-page-stub" />,
}));

const docsRoutes = DOCS_ROUTES.filter((r) => r.path.startsWith('docs'));

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

describe('文档中心路由（#348 / #368）', () => {
  it('路由表含 /docs 与 /docs/:slug 且指向文档中心元素', () => {
    expect(docsRoutes.map((r) => r.path).sort()).toEqual(['docs', 'docs/:slug']);
    for (const route of docsRoutes) {
      expect(route.element).toBeTruthy();
    }
  });

  // 接线钉：片段必须真的被 `createBrowserRouter` 展开——只导出不挂的话上面几条仍全绿
  // （它们直挂片段自身），删掉 router.tsx 里的 `...DOCS_ROUTES,` 时靠这条变红。
  it('createBrowserRouter 的 / 路由 children 里含 docs 片段', () => {
    const root = router.routes.find((r) => r.path === '/');
    const childPaths = root?.children?.map((c) => c.path);
    expect(childPaths).toContain('docs');
    expect(childPaths).toContain('docs/:slug');
  });

  it('/docs 渲染文档中心组件（落点由页面决定）', async () => {
    renderDocsRoutes('/docs');
    expect(await screen.findByTestId('docs-page-stub')).toBeTruthy();
    expect(screen.getByTestId('location-probe').textContent).toBe('/docs');
    expect(screen.queryByTestId('docs-route-fallback')).toBeNull();
  });

  it('/docs/:slug 渲染文档中心组件（lazy 导入 DocsPage）', async () => {
    renderDocsRoutes('/docs/some-doc');
    expect(await screen.findByTestId('docs-page-stub')).toBeTruthy();
    expect(screen.getByTestId('location-probe').textContent).toBe('/docs/some-doc');
    expect(screen.queryByTestId('docs-route-fallback')).toBeNull();
  });
});