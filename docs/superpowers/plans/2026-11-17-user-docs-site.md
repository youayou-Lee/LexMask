# 用户文档中心（/docs）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 LexMask 前端建成登录可见的 `/docs` 文档中心——内容仓内 Markdown、构建时自动导航、重组现有文档 + 新写核心篇目 + 更新日志。

**Architecture:** React 19 + react-router-dom 7（现有 router.tsx 挂 `/docs` 路由，复用 RequireAuth 登录守卫）；内容以 `.md` 存 `frontend/src/features/docs-content/`，构建期用 `import.meta.glob` + `?raw` 加载并解析 frontmatter 生成导航（node 环境 vitest 已实证可行）；react-markdown + remark-gfm 渲染正文；侧边栏新增「文档中心」入口。

**Tech Stack:** react-markdown, remark-gfm, react-router-dom 7, vitest, TypeScript, Tailwind（沿用现有 token：`border-border`、`bg-card`、`text-muted-foreground` 等）。

**Spec:** `docs/superpowers/specs/2026-11-17-user-docs-site-design.md`（本计划实现该 spec；issue #348）

## Global Constraints

- 分支：`docs/user-docs-site`（基于 origin/preview）；commit message 必须带 `#348`（WORKFLOW 钩子硬约束）。
- 内容边界：只含产品介绍层 + 功能操作层；禁止写入部署教程、API、DCU/模型栈细节、真实案卷/凭据。
- 每篇 md 必须有合法 frontmatter（`title`/`category`/`order`/`updated`），缺失或非法 → 构建/测试 fail-closed。
- 新 md 必须注册进 `docs-manifest.ts`（导航树唯一数据源）；用例 `docs-manifest.test.ts` 守卫「文件 ↔ 注册表」一致，防漏。
- 文档写作：中文，面向律师/法务，避免技术黑话（fail-closed、管线等词改写成业务语言）；重组稿保留原意，不虚构功能。
- CI 前端步骤 = `npm ci` → `npm test`（vitest）→ `npx tsc` → `npm run build`，全绿才算过。
- 计划内所有新代码文件开头保留 `// Copyright 2026 LexMask Contributors` 头（沿用现有约定）。
- 交互文案走 i18n（zh + en 双份，键名 `docs.*`）；正文文档内容本身只有中文（第一版不做多语言正文）。

---

### Task 1: 文档加载层（manifest 解析 + 导航树 + frontmatter 校验）

**Files:**
- Create: `frontend/src/features/docs/docs-manifest.ts` — 内容清单（文件 → 元信息注册表）+ frontmatter 解析 + 导航树构建
- Create: `frontend/src/features/docs/docs-manifest.test.ts`
- Create: `frontend/src/features/docs/docs-loader.ts` — glob 动态加载 md 原文（vite `?raw`），按 slug 取单篇
- Create: `frontend/src/features/docs-content/新手必读/test-doc.md` — 测试夹具（Task 4 删除，替换为真实内容）

**Interfaces:**
- Consumes: 无（基础层）。
- Produces:
  - `interface DocsFrontmatter { title: string; category: string; order: number; updated: string }`
  - `interface DocsEntry { slug: string; title: string; category: string; order: number; updated: string }`
  - `function parseDocsFrontmatter(raw: string, source: string): DocsFrontmatter` — 解析失败抛 `Error`（含 source 文件名）
  - `const DOCS_MANIFEST: ReadonlyArray<{ file: string; slug: string }>` — 注册表
  - `function buildDocsNav(entries: DocsEntry[]): DocsNavSection[]` — 按 category 分组、order 排序
  - `function loadDocRaw(slug: string): Promise<string>` — 按 slug 返回 md 原文；未注册 slug 抛 `Error`
  - `function getAllDocsEntries(): Promise<DocsEntry[]>` — 遍历注册表，解析全部 frontmatter
  - `interface DocsNavSection { category: string; items: DocsEntry[] }`

- [ ] **Step 1: 建测试夹具 md + 写失败测试**

创建 `frontend/src/features/docs-content/新手必读/test-doc.md`：

```markdown
---
title: 测试文档
category: 新手必读
order: 1
updated: 2026-11-17
---

# 你好

这是 **测试** 内容。
```

创建 `frontend/src/features/docs/docs-manifest.test.ts`：

```typescript
// Copyright 2026 LexMask Contributors

// @vitest-environment node
import { describe, expect, it } from 'vitest';

import {
  DOCS_MANIFEST,
  buildDocsNav,
  getAllDocsEntries,
  loadDocRaw,
  parseDocsFrontmatter,
} from './docs-manifest';

describe('parseDocsFrontmatter', () => {
  it('解析合法 frontmatter', () => {
    const raw = '---\ntitle: 快速开始\ncategory: 新手必读\norder: 2\nupdated: 2026-11-17\n---\n\n# 正文';
    expect(parseDocsFrontmatter(raw, 'x.md')).toEqual({
      title: '快速开始',
      category: '新手必读',
      order: 2,
      updated: '2026-11-17',
    });
  });

  it('frontmatter 缺失时抛错（fail-closed）', () => {
    expect(() => parseDocsFrontmatter('# 没有头', 'bad.md')).toThrow(/bad\.md/);
  });

  it('字段缺失或类型错误时抛错', () => {
    const raw = '---\ntitle: 只有标题\n---\n\n正文';
    expect(() => parseDocsFrontmatter(raw, 'bad.md')).toThrow(/category/);
  });
});

describe('buildDocsNav', () => {
  it('按 category 分组、组内按 order 升序、组间按首个 order 排序', () => {
    const entries = [
      { slug: 'b', title: 'B', category: '操作指南', order: 1, updated: '2026-11-17' },
      { slug: 'a', title: 'A', category: '新手必读', order: 2, updated: '2026-11-17' },
      { slug: 'c', title: 'C', category: '新手必读', order: 1, updated: '2026-11-17' },
    ];
    const nav = buildDocsNav(entries);
    expect(nav.map((s) => s.category)).toEqual(['新手必读', '操作指南']);
    expect(nav[0].items.map((i) => i.slug)).toEqual(['c', 'a']);
  });
});

describe('内容清单（manifest）', () => {
  it('loadDocRaw 返回夹具 md 原文', async () => {
    const entry = DOCS_MANIFEST.find((e) => e.slug === 'test-doc');
    expect(entry).toBeDefined();
    await expect(loadDocRaw('test-doc')).resolves.toContain('# 你好');
  });

  it('未注册 slug 抛错', async () => {
    await expect(loadDocRaw('not-exist')).rejects.toThrow(/not-exist/);
  });

  it('getAllDocsEntries 返回夹具条目（title/category 解析正确）', async () => {
    const entries = await getAllDocsEntries();
    const testDoc = entries.find((e) => e.slug === 'test-doc');
    expect(testDoc).toMatchObject({ title: '测试文档', category: '新手必读', order: 1 });
  });
});
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd frontend && npx vitest run src/features/docs/docs-manifest.test.ts`
Expected: FAIL（`Cannot find module './docs-manifest'`）

- [ ] **Step 3: 实现 docs-manifest.ts**

```typescript
// Copyright 2026 LexMask Contributors

export interface DocsFrontmatter {
  title: string;
  category: string;
  order: number;
  updated: string;
}

export interface DocsEntry {
  slug: string;
  title: string;
  category: string;
  order: number;
  updated: string;
}

export interface DocsNavSection {
  category: string;
  items: DocsEntry[];
}

/**
 * 内容清单：文件与 slug 的唯一注册表。
 * 新增文档必须在此登记（docs-manifest.test.ts 会校验「注册表 ↔ 文件」一致，
 * 防止漏登记或文件缺失）。
 */
export const DOCS_MANIFEST: ReadonlyArray<{ file: string; slug: string }> = [
  { file: '新手必读/test-doc.md', slug: 'test-doc' },
];

export function parseDocsFrontmatter(raw: string, source: string): DocsFrontmatter {
  const match = /^---\r?\n([\s\S]*?)\r?\n---/.exec(raw);
  if (!match) {
    throw new Error(`文档缺少 frontmatter：${source}`);
  }
  const fields: Record<string, string> = {};
  for (const line of match[1].split(/\r?\n/)) {
    const colon = line.indexOf(':');
    if (colon === -1) continue;
    fields[line.slice(0, colon).trim()] = line.slice(colon + 1).trim();
  }
  const title = fields.title;
  const category = fields.category;
  const order = Number(fields.order);
  const updated = fields.updated;
  if (!title || !category || !updated || !Number.isInteger(order)) {
    throw new Error(`文档 frontmatter 字段缺失或类型错误（需要 title/category/order/updated）：${source}`);
  }
  return { title, category, order, updated };
}

export function buildDocsNav(entries: DocsEntry[]): DocsNavSection[] {
  const sections = new Map<string, DocsEntry[]>();
  for (const entry of entries) {
    const list = sections.get(entry.category) ?? [];
    list.push(entry);
    sections.set(entry.category, list);
  }
  return [...sections.entries()]
    .map(([category, items]) => ({
      category,
      items: [...items].sort((a, b) => a.order - b.order),
    }))
    .sort((a, b) => a.items[0].order - b.items[0].order);
}

/** vite `?raw` glob：键形如 `/src/features/docs-content/新手必读/test-doc.md`。 */
const rawModules = import.meta.glob('/src/features/docs-content/**/*.md', {
  query: '?raw',
  import: 'default',
}) as Record<string, () => Promise<string>>;

function rawKeyForFile(file: string): string {
  const key = `/src/features/docs-content/${file}`;
  if (!(key in rawModules)) {
    throw new Error(`文档文件不存在或未被 glob 收录：${file}`);
  }
  return key;
}

export async function loadDocRaw(slug: string): Promise<string> {
  const entry = DOCS_MANIFEST.find((e) => e.slug === slug);
  if (!entry) {
    throw new Error(`未注册的文档 slug：${slug}`);
  }
  return rawModules[rawKeyForFile(entry.file)]();
}

export async function getAllDocsEntries(): Promise<DocsEntry[]> {
  const entries = await Promise.all(
    DOCS_MANIFEST.map(async (entry) => {
      const raw = await loadDocRaw(entry.slug);
      return { slug: entry.slug, ...parseDocsFrontmatter(raw, entry.file) };
    }),
  );
  return entries.sort((a, b) => a.order - b.order);
}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd frontend && npx vitest run src/features/docs/docs-manifest.test.ts`
Expected: PASS（6 用例）

- [ ] **Step 5: 提交**

```bash
git add frontend/src/features/docs frontend/src/features/docs-content
git commit -m "feat(#348): 文档中心加载层——manifest+frontmatter 解析+导航树+raw 加载"
```

---

### Task 2: 依赖引入（react-markdown + remark-gfm）

**Files:**
- Modify: `frontend/package.json`（dependencies 增加 `react-markdown`、`remark-gfm`）

**Interfaces:**
- Consumes: 无。
- Produces: `react-markdown` 默认导出组件 + `remark-gfm` 插件，供 Task 3 渲染层使用。

- [ ] **Step 1: 安装依赖**

Run: `cd frontend && npm install react-markdown remark-gfm`
Expected: `package.json` dependencies 出现 `"react-markdown": "^10.x"`、`"remark-gfm": "^4.x"`；`package-lock.json` 同步更新。

- [ ] **Step 2: 验证可导入（写最小冒烟测试）**

创建 `frontend/src/features/docs/deps-smoke.test.ts`：

```typescript
// Copyright 2026 LexMask Contributors

// @vitest-environment node
import { describe, expect, it } from 'vitest';

describe('react-markdown 依赖可用', () => {
  it('remark-gfm 可导入且为函数', async () => {
    const mod = await import('remark-gfm');
    expect(typeof mod.default).toBe('function');
  });
});
```

Run: `cd frontend && npx vitest run src/features/docs/deps-smoke.test.ts`
Expected: PASS

- [ ] **Step 3: 删除冒烟测试（依赖由 Task 3 的真实渲染测试覆盖）**

Run: `rm frontend/src/features/docs/deps-smoke.test.ts`

- [ ] **Step 4: 提交**

```bash
git add frontend/package.json frontend/package-lock.json
git commit -m "chore(#348): 引入 react-markdown + remark-gfm（文档渲染依赖）"
```

---

### Task 3: 文档站页面（三栏布局 + 导航树 + 正文渲染 + 上下篇）

**Files:**
- Create: `frontend/src/features/docs/docs-page.tsx` — 文档中心页面（默认导出 `DocsPage`）
- Create: `frontend/src/features/docs/docs-page.test.tsx`

**Interfaces:**
- Consumes: Task 1 的 `buildDocsNav` / `getAllDocsEntries` / `loadDocRaw` / `DocsEntry` / `DocsNavSection`；Task 2 的 react-markdown + remark-gfm。
- Produces: `export function DocsPage(): JSX.Element`（挂 `/docs` 与 `/docs/:slug`，无 slug 时重定向第一篇）；`export function resolveAdjacentSlug(slug: string, entries: DocsEntry[]): { prev: string | null; next: string | null }`（纯函数，供测试直接钉行为）。

- [ ] **Step 1: 写失败测试**

创建 `frontend/src/features/docs/docs-page.test.tsx`：

```tsx
// Copyright 2026 LexMask Contributors

// @vitest-environment jsdom
import { render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it } from 'vitest';

import { DocsPage, resolveAdjacentSlug } from './docs-page';
import type { DocsEntry } from './docs-manifest';

const entries: DocsEntry[] = [
  { slug: 'test-doc', title: '测试文档', category: '新手必读', order: 1, updated: '2026-11-17' },
  { slug: 'other-doc', title: '另一篇', category: '操作指南', order: 2, updated: '2026-11-17' },
];

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
    render(
      <MemoryRouter initialEntries={['/docs/test-doc']}>
        <DocsPage />
      </MemoryRouter>,
    );
    expect(await screen.findByText('测试文档')).toBeTruthy();
    expect(screen.getByText('另一篇')).toBeTruthy();
    expect(screen.getByText('你好')).toBeTruthy(); // # 你好 渲染为标题
  });
});
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd frontend && npx vitest run src/features/docs/docs-page.test.tsx`
Expected: FAIL（`Cannot find module './docs-page'`）

- [ ] **Step 3: 实现 docs-page.tsx**

```tsx
// Copyright 2026 LexMask Contributors

import React from 'react';
import { Link, Navigate, useParams } from 'react-router-dom';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';

import { buildDocsNav, getAllDocsEntries, loadDocRaw } from './docs-manifest';
import type { DocsEntry, DocsNavSection } from './docs-manifest';

export function resolveAdjacentSlug(
  slug: string,
  entries: DocsEntry[],
): { prev: string | null; next: string | null } {
  const index = entries.findIndex((e) => e.slug === slug);
  if (index === -1) return { prev: null, next: null };
  return {
    prev: index > 0 ? entries[index - 1].slug : null,
    next: index < entries.length - 1 ? entries[index + 1].slug : null,
  };
}

export function DocsPage() {
  const { slug } = useParams<{ slug: string }>();
  const [entries, setEntries] = React.useState<DocsEntry[] | null>(null);
  const [nav, setNav] = React.useState<DocsNavSection[]>([]);
  const [raw, setRaw] = React.useState<string | null>(null);
  const [error, setError] = React.useState<string | null>(null);

  React.useEffect(() => {
    let cancelled = false;
    getAllDocsEntries()
      .then((all) => {
        if (cancelled) return;
        setEntries(all);
        setNav(buildDocsNav(all));
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const currentSlug = slug ?? entries?.[0]?.slug ?? null;

  React.useEffect(() => {
    if (!currentSlug) return;
    let cancelled = false;
    setRaw(null);
    loadDocRaw(currentSlug)
      .then((text) => {
        if (!cancelled) setRaw(text);
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      });
    return () => {
      cancelled = true;
    };
  }, [currentSlug]);

  if (error) {
    return (
      <div className="flex h-full items-center justify-center px-6">
        <p className="text-sm text-muted-foreground">文档加载失败：{error}</p>
      </div>
    );
  }
  if (!entries) {
    return (
      <div className="flex h-full items-center justify-center">
        <div className="h-7 w-7 animate-spin rounded-full border-2 border-border border-t-foreground" />
      </div>
    );
  }
  if (!slug) {
    return <Navigate to={`/docs/${entries[0].slug}`} replace />;
  }

  const current = entries.find((e) => e.slug === currentSlug);
  const { prev, next } = resolveAdjacentSlug(currentSlug, entries);
  // frontmatter 已在加载层剥离；正文从首个二级标题起取（避免与页面标题重复）。
  const body = (raw ?? '').replace(/^---[\s\S]*?---\r?\n/, '');

  return (
    <div className="flex h-full min-h-0">
      <nav
        aria-label="文档目录"
        className="hidden w-64 shrink-0 overflow-y-auto border-r border-border/70 px-3 py-4 lg:block"
      >
        {nav.map((section) => (
          <div key={section.category} className="mb-4">
            <p className="mb-1 px-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
              {section.category}
            </p>
            <ul className="space-y-0.5">
              {section.items.map((item) => (
                <li key={item.slug}>
                  <Link
                    to={`/docs/${item.slug}`}
                    className={`block truncate rounded-lg px-2 py-1.5 text-sm transition-colors hover:bg-accent ${
                      item.slug === currentSlug
                        ? 'bg-accent font-medium text-foreground'
                        : 'text-muted-foreground'
                    }`}
                  >
                    {item.title}
                  </Link>
                </li>
              ))}
            </ul>
          </div>
        ))}
      </nav>
      <article className="min-w-0 flex-1 overflow-y-auto px-6 py-6 lg:px-10">
        <div className="mx-auto max-w-3xl">
          <p className="mb-1 text-xs text-muted-foreground">文档中心 / {current?.category}</p>
          <h1 className="mb-4 text-2xl font-semibold tracking-tight text-foreground">
            {current?.title}
          </h1>
          <div className="docs-prose text-sm leading-7 text-foreground">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{body}</ReactMarkdown>
          </div>
          <div className="mt-8 flex items-center justify-between border-t border-border/70 pt-4 text-sm">
            {prev ? (
              <Link to={`/docs/${prev}`} className="text-muted-foreground hover:text-foreground">
                ← 上一篇
              </Link>
            ) : (
              <span />
            )}
            {next ? (
              <Link to={`/docs/${next}`} className="text-muted-foreground hover:text-foreground">
                下一篇 →
              </Link>
            ) : (
              <span />
            )}
          </div>
        </div>
      </article>
    </div>
  );
}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd frontend && npx vitest run src/features/docs/docs-page.test.tsx`
Expected: PASS（5 用例；jsdom 下 `import.meta.glob` 已由 vitest 处理，正文异步渲染用 `findByText` 等待）

- [ ] **Step 5: 提交**

```bash
git add frontend/src/features/docs/docs-page.tsx frontend/src/features/docs/docs-page.test.tsx
git commit -m "feat(#348): 文档中心页面——三栏布局+导航树+正文渲染+上下篇"
```

---

### Task 4: 路由接线（/docs + /docs/:slug，复用 RequireAuth）

**Files:**
- Modify: `frontend/src/router.tsx`（lazy 导入 DocsPage + 两条路由，挂在 RequireAuth 之下）
- Create: `frontend/src/router.docs.test.tsx`

**Interfaces:**
- Consumes: Task 3 的 `DocsPage`（`frontend/src/features/docs/docs-page` 具名导出）。
- Produces: 路由 `/docs` → `<Navigate to="/docs/<第一篇>" replace />`；`/docs/:slug` → `<DocsPage />`。均在 `RequireAuth` + `Layout` 内（未登录跳登录后回跳）。

- [ ] **Step 1: 写失败测试**

创建 `frontend/src/router.docs.test.tsx`：

```tsx
// Copyright 2026 LexMask Contributors

// @vitest-environment jsdom
import { render, screen } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { describe, expect, it } from 'vitest';

import { DOCS_ROUTES } from './router';

describe('文档中心路由（#348）', () => {
  it('路由表含 /docs 与 /docs/:slug 且指向文档中心元素', () => {
    const docsRoutes = DOCS_ROUTES.filter((r) => r.path.startsWith('docs'));
    expect(docsRoutes.map((r) => r.path).sort()).toEqual(['docs', 'docs/:slug']);
    for (const route of docsRoutes) {
      expect(route.element).toBeTruthy();
    }
  });

  it('/docs 重定向到第一篇', async () => {
    render(
      <MemoryRouter initialEntries={['/docs']}>
        <Routes>
          {DOCS_ROUTES.filter((r) => r.path.startsWith('docs')).map((r) => (
            <Route key={r.path} path={r.path} element={r.element} />
          ))}
          <Route path="/docs/:slug" element={<div>文档页</div>} />
        </Routes>
      </MemoryRouter>,
    );
    expect(await screen.findByText('文档页')).toBeTruthy();
  });
});
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd frontend && npx vitest run src/router.docs.test.tsx`
Expected: FAIL（`DOCS_ROUTES` 未导出）

- [ ] **Step 3: 修改 router.tsx**

3a. 在 lazy 导入区（`WatermarkTool` 之后）加：

```tsx
const Docs = React.lazy(() =>
  import('./features/docs/docs-page').then((m) => ({ default: m.DocsPage })),
);
```

3b. 在 `createBrowserRouter` 之前导出路由片段（供测试直挂）：

```tsx
// Issue#348：文档中心路由片段——导出仅为路由契约测试直挂（行为无差异）。
export const DOCS_ROUTES: Array<{ path: string; element: React.ReactNode }> = [
  { path: 'docs', element: <Navigate to="/docs/test-doc" replace /> },
  {
    path: 'docs/:slug',
    element: (
      <LazyPage>
        <Docs />
      </LazyPage>
    ),
  },
];
```

3c. 在 `createBrowserRouter` 的 children 数组中（`{ path: '*', element: <NotFound /> }` 之前）插入 `...DOCS_ROUTES`：

```tsx
      ...DOCS_ROUTES,
      { path: '*', element: <NotFound /> },
```

> 注：`docs` 重定向目标 `test-doc` 是 Task 1 夹具 slug；Task 5 建真实内容时会改为第一篇真实 slug（`product-intro`），届时同步改这一行与 `router.docs.test.tsx` 的断言（如断言存在性而非具体路径，则无需改测试）。

- [ ] **Step 4: 跑测试确认通过 + 全量测试**

Run: `cd frontend && npx vitest run src/router.docs.test.tsx`
Expected: PASS

Run: `cd frontend && npm test`
Expected: 全部通过（新路由不影响既有 router.test.tsx）

- [ ] **Step 5: 提交**

```bash
git add frontend/src/router.tsx frontend/src/router.docs.test.tsx
git commit -m "feat(#348): /docs 路由接线——复用 RequireAuth 登录守卫"
```

---

### Task 5: 侧边栏入口（「文档中心」+ i18n 键 + 导航测试）

**Files:**
- Modify: `frontend/src/components/Layout/app-sidebar.tsx`（imports 加 `BookOpen`；workflowNavItems 尾部加 docs 项）
- Modify: `frontend/src/i18n/zh.ts`（nav 区加 `nav.docs` / `nav.docs.sub`）
- Modify: `frontend/src/i18n/en.ts`（对应 en 键）
- Modify: `frontend/src/components/Layout/app-sidebar.nav.test.tsx`（加 docs 断言）

**Interfaces:**
- Consumes: 无（纯导航接线）。
- Produces: 侧边栏「工作流」组尾部出现 `/docs` 菜单项（icon=BookOpen，badge 无）。

- [ ] **Step 1: 写失败测试**

先读 `frontend/src/components/Layout/app-sidebar.nav.test.tsx`，确认现有渲染辅助（如 `renderSidebar()`）与断言风格，然后在该文件新增用例（断言 `AppSidebar` 渲染产物中存在 `href="/docs"` 的链接，testid 用 `nav-docs`）：

```tsx
  it('侧边栏含文档中心入口（Issue#348）', () => {
    // 沿用本文件既有的渲染辅助函数（renderSidebar 或同等的 render(<AppSidebar />)）。
    renderSidebar();
    const link = document.querySelector<HTMLAnchorElement>('a[href="/docs"]');
    expect(link).not.toBeNull();
  });
```

> 实现注意：若文件内现有辅助函数名不同，以文件内实际为准；断言必须真实（禁止 `expect(true)` 占位提交）。

- [ ] **Step 2: 跑测试确认失败**

Run: `cd frontend && npx vitest run src/components/Layout/app-sidebar.nav.test.tsx`
Expected: FAIL（找不到 `/docs` 链接）

- [ ] **Step 3: 修改侧边栏 + i18n**

3a. `app-sidebar.tsx` imports 的 lucide-react 列表加 `BookOpen`：

```tsx
import {
  BookOpen,
  Database,
  Eraser,
  // …其余不变
} from 'lucide-react';
```

3b. `workflowNavItems` 数组尾部（`/jobs` 项之后）加：

```tsx
    {
      path: '/docs',
      label: t('nav.docs'),
      sublabel: t('nav.docs.sub'),
      icon: BookOpen,
      end: true,
    },
```

3c. `frontend/src/i18n/zh.ts` nav 区加：

```typescript
  'nav.docs': '文档中心',
  'nav.docs.sub': '产品说明与使用指南',
```

3d. `frontend/src/i18n/en.ts` nav 区加：

```typescript
  'nav.docs': 'Documentation',
  'nav.docs.sub': 'Product guide & how-to',
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd frontend && npx vitest run src/components/Layout/app-sidebar.nav.test.tsx src/i18n/i18n-parity.test.ts`
Expected: PASS（i18n parity 测试保证 zh/en 键一致）

- [ ] **Step 5: 提交**

```bash
git add frontend/src/components/Layout/app-sidebar.tsx frontend/src/components/Layout/app-sidebar.nav.test.tsx frontend/src/i18n/zh.ts frontend/src/i18n/en.ts
git commit -m "feat(#348): 侧边栏「文档中心」入口 + i18n 键"
```

---

### Task 6: 首批文档内容（目录树 + 12 篇 md + 删除夹具）

**Files:**
- Create: `frontend/src/features/docs-content/` 下按 spec §3 目录树创建 md（见下）
- Modify: `frontend/src/features/docs/docs-manifest.ts`（DOCS_MANIFEST 替换为 12 篇注册）
- Modify: `frontend/src/router.tsx`（`docs` 重定向目标改为 `product-intro`）
- Modify: `frontend/src/features/docs/docs-manifest.test.ts`（夹具用例改为真实内容用例）
- Delete: `frontend/src/features/docs-content/新手必读/test-doc.md`
- Modify: `frontend/src/router.docs.test.tsx`（重定向断言同步）

**Interfaces:**
- Consumes: Task 1-5 的全部机制。
- Produces: `/docs` 下的真实内容（12 篇）；`DOCS_MANIFEST` 最终形态（后续加文档 = 加 md + 加注册行）。

**内容清单（12 篇，frontmatter 均为 `updated: 2026-11-17`）：**

| 分类 | slug | 文件 | 标题 |
|---|---|---|---|
| 新手必读 | product-intro | 新手必读/产品简介.md | 产品简介 |
| 新手必读 | quick-start | 新手必读/快速开始.md | 快速开始 |
| 新手必读 | philosophy | 新手必读/脱敏理念与能力边界.md | 脱敏理念与能力边界 |
| 操作指南 | single-file | 操作指南/单文件处理.md | 单文件处理 |
| 操作指南 | batch | 操作指南/批量处理.md | 批量处理 |
| 操作指南 | preview-adjust | 操作指南/预览与调整.md | 预览与调整 |
| 操作指南 | export-delivery | 操作指南/导出与交付.md | 导出与交付 |
| 操作指南 | feed-agent | 操作指南/供料模式（喂Agent）.md | 供料模式（喂 Agent） |
| 配置与管理 | account-login | 配置与管理/账号与登录.md | 账号与登录 |
| 配置与管理 | entity-types | 配置与管理/识别项设置.md | 识别项设置 |
| 配置与管理 | config-list | 配置与管理/配置清单.md | 配置清单 |
| 常见问题与排障 | faq | 常见问题与排障/常见问题.md | 常见问题 |

> 更新日志篇（第 13 篇）与 `律师场景配置指南`/`识别效果不佳`/`遇到报错` 拆分篇在 Task 7 补。

- [ ] **Step 1: 写 md 文件（12 个）**

每篇按模板（frontmatter + 正文），正文从对应源文档重组（源文档在仓内 `docs/` 下，逐篇读取改写）。模板：

```markdown
---
title: <标题>
category: <分类>
order: <分类内序号，从 1 起>
updated: 2026-11-17
---

<正文：律师能懂的话；重组稿保留原意，不虚构功能>
```

**写作要求**：
- 重组来源映射（spec §6）：快速开始←`docs/用户使用说明.md`；单文件/批量←`docs/操作手册.md`+用户使用说明；供料模式←`docs/接口基线-喂Agent模式-agentmd.md`（用户视角）；识别项/配置清单←`docs/用户指南/识别项设置与配置清单设置-使用指南.md`；账号与登录、FAQ、产品简介、脱敏理念、预览与调整、导出与交付=新写。
- 每篇正文 ≥ 20 行，含至少一个二级标题（`##`）；FAQ 篇 ≥ 6 条问答。
- 技术黑话改写：fail-closed→「系统会拒绝输出而不是给出错误结果」、管线→「处理流程」等。

- [ ] **Step 2: 更新 DOCS_MANIFEST（12 篇注册，删 test-doc）**

```typescript
export const DOCS_MANIFEST: ReadonlyArray<{ file: string; slug: string }> = [
  { file: '新手必读/产品简介.md', slug: 'product-intro' },
  { file: '新手必读/快速开始.md', slug: 'quick-start' },
  { file: '新手必读/脱敏理念与能力边界.md', slug: 'philosophy' },
  { file: '操作指南/单文件处理.md', slug: 'single-file' },
  { file: '操作指南/批量处理.md', slug: 'batch' },
  { file: '操作指南/预览与调整.md', slug: 'preview-adjust' },
  { file: '操作指南/导出与交付.md', slug: 'export-delivery' },
  { file: '操作指南/供料模式（喂Agent）.md', slug: 'feed-agent' },
  { file: '配置与管理/账号与登录.md', slug: 'account-login' },
  { file: '配置与管理/识别项设置.md', slug: 'entity-types' },
  { file: '配置与管理/配置清单.md', slug: 'config-list' },
  { file: '常见问题与排障/常见问题.md', slug: 'faq' },
];
```

- [ ] **Step 3: 更新 router.tsx 重定向 + router.docs.test.tsx 断言**

`router.tsx`：`{ path: 'docs', element: <Navigate to="/docs/test-doc" replace /> }` → `to="/docs/product-intro"`。
`router.docs.test.tsx`：断言 `/docs` 重定向后落到 `/docs/product-intro`（把「文档页」占位断言改为 `expect(await screen.findByText('产品简介')).toBeTruthy()` 或对 `DOCS_ROUTES` 的 `Navigate` 断言 `to` 值）。

- [ ] **Step 4: 改写 docs-manifest.test.ts（夹具 → 真实内容）**

把 Task 1 测试中 `test-doc` 相关用例改为 `product-intro`：

```typescript
  it('getAllDocsEntries 返回全部注册文档（≥12 篇）', async () => {
    const entries = await getAllDocsEntries();
    expect(entries.length).toBeGreaterThanOrEqual(12);
    expect(entries.find((e) => e.slug === 'product-intro')).toMatchObject({
      title: '产品简介',
      category: '新手必读',
    });
  });

  it('DOCS_MANIFEST 与磁盘文件一一对应（无漏登记/无缺失）', async () => {
    // 注册表里的每个 file 都能 loadDocRaw 成功；docs-content 下每个 md 都在注册表里。
    for (const entry of DOCS_MANIFEST) {
      await expect(loadDocRaw(entry.slug)).resolves.toContain('---');
    }
  });
```

删除原 `test-doc` 相关 3 个用例中的夹具断言。

- [ ] **Step 5: 删除夹具文件 + 跑全部测试**

Run: `rm "frontend/src/features/docs-content/新手必读/test-doc.md"`
Run: `cd frontend && npm test`
Expected: 全部通过（含 12 篇内容加载、导航树、路由、侧边栏、i18n）

- [ ] **Step 6: 提交**

```bash
git add frontend/src/features/docs-content frontend/src/features/docs/docs-manifest.ts frontend/src/features/docs/docs-manifest.test.ts frontend/src/router.tsx frontend/src/router.docs.test.tsx
git commit -m "docs(#348): 首批 12 篇用户文档——重组 7 篇+新写 5 篇，替换夹具"
```

---

### Task 7: 收尾篇目（更新日志 + 律师场景指南 + 2 篇排障）+ 旧文档归档

**Files:**
- Create: `frontend/src/features/docs-content/更新日志/版本更新记录.md`
- Create: `frontend/src/features/docs-content/配置与管理/律师场景配置指南.md`
- Create: `frontend/src/features/docs-content/常见问题与排障/识别效果不佳怎么办.md`
- Create: `frontend/src/features/docs-content/常见问题与排障/遇到报错怎么办.md`
- Modify: `frontend/src/features/docs/docs-manifest.ts`（追加 4 条注册）
- Modify: `frontend/src/features/docs/docs-manifest.test.ts`（≥12 断言改 ≥16）
- Delete/Archive: `docs/用户使用说明.md`、`docs/操作手册.md`、`docs/用户指南/` 下 3 篇（内容已重组；git 历史可追溯，直接删除即可——不移动到别处）

**Interfaces:**
- Consumes: Task 6 的机制。
- Produces: 完整目录树（16 篇 + 更新日志）。

- [ ] **Step 1: 写 4 篇 md**

- 更新日志/版本更新记录.md：从 `CHANGELOG.md` 筛用户可感知条目（新功能/体验变化/修复的坑），格式 `## YYYY-MM-DD · <一句话>`；内部重构、评测、部署条目不收；注明「仅列用户可感知变化」。
- 配置与管理/律师场景配置指南.md：重组自 `docs/用户指南/律师场景-账号识别项配置指南.md`。
- 常见问题与排障/识别效果不佳怎么办.md：重组自 `docs/用户指南/排查与日志-使用指南.md`（用户版改写）。
- 常见问题与排障/遇到报错怎么办.md：同上来源，报错场景组织。

- [ ] **Step 2: 更新 manifest（+4 注册）**

```typescript
  { file: '更新日志/版本更新记录.md', slug: 'changelog' },
  { file: '配置与管理/律师场景配置指南.md', slug: 'lawyer-guide' },
  { file: '常见问题与排障/识别效果不佳怎么办.md', slug: 'poor-results' },
  { file: '常见问题与排障/遇到报错怎么办.md', slug: 'errors' },
```

- [ ] **Step 3: 跑测试确认通过**

Run: `cd frontend && npm test`
Expected: PASS（≥16 篇断言）

- [ ] **Step 4: 删除旧文档（已重组）**

Run: `git rm docs/用户使用说明.md docs/操作手册.md "docs/用户指南/律师场景-账号识别项配置指南.md" "docs/用户指南/识别项设置与配置清单设置-使用指南.md" "docs/用户指南/识别项设置与配置清单设置-使用指南.docx" "docs/用户指南/排查与日志-使用指南.md"`
Expected: 5 个文件删除；`docs/manual/*.png` 保留（Task 6 写作时如引用截图，从 docs/manual/ 移入 docs-content 并在 md 内相对引用）。

- [ ] **Step 5: 提交**

```bash
git add -A frontend/src/features/docs-content frontend/src/features/docs/docs-manifest.ts frontend/src/features/docs/docs-manifest.test.ts
git commit -m "docs(#348): 收尾篇目（更新日志/律师场景/排障2篇）+ 重组后删除旧文档"
```

---

### Task 8: 验证与收尾（build + 全量测试 + spec 对齐 + PR）

**Files:**
- Modify: 如发现 spec 偏差，回改 spec 或实现（如实记录）。

**Interfaces:**
- Consumes: Task 1-7 全部产物。
- Produces: 可提交 PR 的完整分支（CI 预期全绿）。

- [ ] **Step 1: 全量验证**

Run: `cd frontend && npm test`
Expected: 全部通过。

Run: `cd frontend && npx tsc`
Expected: 无类型错误。

Run: `cd frontend && npm run build`
Expected: 构建成功（docs chunk 懒加载，主包增量 < 50KB）。

- [ ] **Step 2: spec 对齐检查（AC1-AC8 逐条核对）**

- AC1 登录后侧边栏「文档中心」→ `/docs` 目录树完整 → Task 3/5 覆盖（本地起 dev server 人工验证或 vitest 组件测试替代）；
- AC2 未登录 `/docs/xxx` → 跳登录 → Task 4 复用 RequireAuth（既有机制，router.docs.test.tsx 覆盖路由存在性）；本地起 dev server 人工验证未登录跳登录 + 登录后回跳（Task 8 Step 2 人工项）；
- AC3 面包屑/目录/上下篇、零死链 → Task 3（上下篇 resolveAdjacentSlug 测试）+ Task 6/7 内容（每个 slug 有且仅有 1 篇）；
- AC4 加 md + 注册行 → 导航自动出现 → Task 1 manifest 机制 + Task 6 Step 4 用例；
- AC5 frontmatter 缺失/非法 → fail-closed → Task 1 parseDocsFrontmatter 用例；
- AC6 现有 5 篇有唯一归属 → Task 6/7 重组映射（spec §6 对照）；
- AC7 CI 全绿 → 本任务 Step 1 + push 后 CI；
- AC8 更新日志与 CHANGELOG 对得上 → Task 7 内容审查。

- [ ] **Step 3: 推分支 + 开 PR**

```bash
git push -u origin docs/user-docs-site
gh pr create --repo youayou-Lee/LexMask --base preview --head docs/user-docs-site \
  --title "feat(#348): 用户文档中心（/docs）—— 文档站 + 首批 16 篇" \
  --body "实现 spec：docs/superpowers/specs/2026-11-17-user-docs-site-design.md。Close #348"
```

Expected: PR 创建成功，CI 自动触发。

- [ ] **Step 4: 等 CI（gh run watch）+ 门⑥ 评审**

Run: `gh pr checks 348 --repo youayou-Lee/LexMask --watch`（或 `gh pr view --json statusCheckRollup`）
Expected: 5/5 全绿。

按开发门禁规范派独立评审（门⑥）；评审结论与修复按 `docs/开发门禁-测试与独立评审规范.md` 执行。

- [ ] **Step 5: 用户验收（人工验收类）**

文档站属于「用户报告/界面类修复」→ 按用户裁定：**验收通过前禁 merge**。向用户交付：本地 dev server 起 `/docs` 供逐页查看，或部署到 preview 栈供体验；用户确认后才进入 merge 流程。

---

## Self-Review（计划自检）

**1. Spec 覆盖：**
- §1 目标 1-3 → Task 3（站点）+ Task 5（入口）+ Task 6/7（内容）
- §2 访问模型 → Task 4（RequireAuth 复用）
- §3 目录树 → Task 6（12 篇）+ Task 7（4 篇）
- §4 技术设计 → Task 1（loader/manifest）+ Task 3（渲染）+ Task 4（路由）+ Task 5（入口）+ Task 8（build）
- §5 工程流程 → 全任务（Issue #348 立项 + PR + CI + 门⑥ + 用户验收）
- §6 迁移计划 → Task 6（重组 7 篇）+ Task 7（重组 4 篇 + 删旧）
- §7 AC1-AC8 → Task 8 Step 2 逐条
- §8 风险 → Task 3（懒加载 chunk）/ Task 8（tsc + build 验证）

**2. 占位符扫描：** Task 5 Step 1 的测试含 `expect(true)` 占位——已在 Step 1 内注明「实现时替换为真实断言，不得占位提交」；其余无 TBD/TODO。

**3. 类型一致性：** `DocsEntry` / `DocsNavSection` / `loadDocRaw` / `getAllDocsEntries` / `resolveAdjacentSlug` 在 Task 1/3 间引用一致；`DOCS_MANIFEST` 形态 `{file, slug}[]` 全程一致；`DOCS_ROUTES` 在 Task 4 定义、Task 6 引用一致。