// Copyright 2026 LexMask Contributors

import React from 'react';
import { Link, Navigate, useParams } from 'react-router-dom';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';

import { buildDocsNav, getAllDocsEntries, loadDocRaw } from './docs-manifest';
import type { DocsEntry, DocsNavSection } from './docs-manifest';

/**
 * 相邻文档（上一篇/下一篇）。
 *
 * 顺序口径 = `entries` 数组顺序（`getAllDocsEntries()` 返回的登记顺序，即阅读顺序
 * 新手必读 → 操作指南 → 配置与管理 → 常见问题与排障），不在此处重新排序。
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

  const current = entries.find((e) => e.slug === slug);
  const { prev, next } = resolveAdjacentSlug(slug, entries);
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
