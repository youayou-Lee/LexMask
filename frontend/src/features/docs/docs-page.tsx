// Copyright 2026 LexMask Contributors

import React from 'react';
import { Link, Navigate, useParams } from 'react-router-dom';
import ReactMarkdown from 'react-markdown';
import type { Components } from 'react-markdown';
import remarkGfm from 'remark-gfm';

import { buildDocsNav, getAllDocsEntries, loadDocRaw } from './docs-manifest';
import type { DocsEntry, DocsNavSection } from './docs-manifest';

/**
 * 相邻文档（上一篇/下一篇）。
 *
 * 顺序口径 = `entries` 数组顺序（`getAllDocsEntries()` 返回的登记顺序，即阅读顺序
 * 新手必读 → 操作指南 → 配置与管理 → 常见问题与排障 → 更新日志），不在此处重新排序
 * （`order` 是分类内序号，与阅读顺序不同）。
 * 纯函数：供测试直接钉行为。
 */
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

/** 标题文本 → 锚点 id（中文保留，空白转 `-`，标点剔除）。 */
function slugifyHeading(text: string): string {
  const slug = text
    .trim()
    .toLowerCase()
    .replace(/\s+/g, '-')
    .replace(/[^\w\u4e00-\u9fa5-]/g, '');
  return slug || 'section';
}

/** react-markdown 的 heading children 已是 React 节点，拍平成纯文本以生成同源 id。 */
function flattenText(node: React.ReactNode): string {
  if (typeof node === 'string' || typeof node === 'number') return String(node);
  if (Array.isArray(node)) return node.map(flattenText).join('');
  if (React.isValidElement(node)) {
    return flattenText((node.props as { children?: React.ReactNode }).children);
  }
  return '';
}

// react-markdown v10 默认不给 heading 加 id —— 用 components 覆盖 h2/h3，
// id 由「渲染出的标题文本」生成，与本页目录同源，保证锚点能对上。
const markdownComponents: Components = {
  h2: ({ children }) => <h2 id={slugifyHeading(flattenText(children))}>{children}</h2>,
  h3: ({ children }) => <h3 id={slugifyHeading(flattenText(children))}>{children}</h3>,
};

interface TocItem {
  level: 2 | 3;
  text: string;
  id: string;
}

/** 去掉行内 markdown 记号，让目录文本与渲染后的标题文本一致。 */
function stripInlineMarkdown(text: string): string {
  return text
    .replace(/`([^`]*)`/g, '$1')
    .replace(/\[([^\]]*)\]\([^)]*\)/g, '$1')
    .replace(/[*_~]/g, '');
}

/** 从 markdown 正文提取 h2/h3 标题，作为「本页目录」锚点。 */
function extractToc(markdown: string): TocItem[] {
  const items: TocItem[] = [];
  for (const line of markdown.split(/\r?\n/)) {
    const match = /^(#{2,3})\s+(.+?)\s*#*\s*$/.exec(line);
    if (!match) continue;
    const text = stripInlineMarkdown(match[2]).trim();
    if (!text) continue;
    items.push({ level: match[1].length === 2 ? 2 : 3, text, id: slugifyHeading(text) });
  }
  return items;
}

function DocsNavList({
  sections,
  currentSlug,
}: {
  sections: DocsNavSection[];
  currentSlug: string | null;
}) {
  return (
    <>
      {sections.map((section) => (
        <div key={section.category} className="mb-4 last:mb-0">
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
    </>
  );
}

export function DocsPage() {
  const { slug } = useParams<{ slug: string }>();
  const [entries, setEntries] = React.useState<DocsEntry[] | null>(null);
  const [nav, setNav] = React.useState<DocsNavSection[]>([]);
  const [raw, setRaw] = React.useState<string | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [navOpen, setNavOpen] = React.useState(false);

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

  const current = entries.find((e) => e.slug === slug);
  const { prev, next } = resolveAdjacentSlug(slug, entries);
  // `loadDocRaw` 返回的是**含 frontmatter 的原文**，下面的 replace 负责把 frontmatter
  // 剥掉（承重代码，勿删）；正文里的 `# 你好` 交给 react-markdown 渲染。
  const body = (raw ?? '').replace(/^---[\s\S]*?---\r?\n/, '');
  const toc = extractToc(body);

  return (
    <div className="flex h-full min-h-0 flex-col lg:flex-row">
      {/* 窄屏（<lg）：目录收进可折叠抽屉，保证窄屏也能切换文档。 */}
      <div className="border-b border-border/70 lg:hidden">
        <button
          type="button"
          aria-expanded={navOpen}
          aria-controls={navOpen ? 'docs-nav-mobile' : undefined}
          onClick={() => setNavOpen((open) => !open)}
          className="flex w-full items-center justify-between px-4 py-3 text-sm font-medium text-foreground"
        >
          文档目录
          <span aria-hidden="true">{navOpen ? '▴' : '▾'}</span>
        </button>
        {navOpen ? (
          <nav
            id="docs-nav-mobile"
            aria-label="文档目录（窄屏）"
            className="max-h-72 overflow-y-auto px-3 pb-3"
          >
            <DocsNavList sections={nav} currentSlug={currentSlug} />
          </nav>
        ) : null}
      </div>
      {/* 宽屏（≥lg）：常驻左侧导航树。 */}
      <nav
        aria-label="文档目录"
        className="hidden w-64 shrink-0 overflow-y-auto border-r border-border/70 px-3 py-4 lg:block"
      >
        <DocsNavList sections={nav} currentSlug={currentSlug} />
      </nav>
      <article className="min-h-0 min-w-0 flex-1 overflow-y-auto px-6 py-6 lg:px-10">
        <div className="mx-auto max-w-3xl">
          <p className="mb-1 text-xs text-muted-foreground">文档中心 / {current?.category}</p>
          <h1 className="mb-4 text-2xl font-semibold tracking-tight text-foreground">
            {current?.title}
          </h1>
          <div className="docs-prose text-sm leading-7 text-foreground">
            <ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownComponents}>
              {body}
            </ReactMarkdown>
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
      {/* 本页目录（≥xl）：正文右侧锚点栏。 */}
      {toc.length > 0 ? (
        <aside
          aria-label="本页目录"
          className="hidden w-56 shrink-0 overflow-y-auto border-l border-border/70 px-3 py-6 xl:block"
        >
          <p className="mb-2 px-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            本页目录
          </p>
          <ul className="space-y-1">
            {toc.map((item, index) => (
              <li key={`${item.id}-${index}`} className={item.level === 3 ? 'pl-4' : undefined}>
                <a
                  href={`#${item.id}`}
                  onClick={(event) => {
                    event.preventDefault();
                    document.getElementById(item.id)?.scrollIntoView({ block: 'start' });
                  }}
                  className="block truncate rounded px-2 py-1 text-sm text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
                >
                  {item.text}
                </a>
              </li>
            ))}
          </ul>
        </aside>
      ) : null}
    </div>
  );
}
