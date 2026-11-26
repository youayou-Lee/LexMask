// Copyright 2026 LexMask Contributors

import React from 'react';
import { Link, Navigate, useParams } from 'react-router-dom';
import ReactMarkdown from 'react-markdown';
import type { Components } from 'react-markdown';
import remarkGfm from 'remark-gfm';

import { fetchDoc, fetchDocsList } from '@/services/docsApi';
import type { DocsDetail, DocsNavItem, DocsNavSection } from '@/services/docsApi';

/**
 * 相邻文档（上一篇/下一篇）。
 *
 * 顺序口径 = 扁平化后的导航顺序（分类顺序 → 分类内 order → slug），
 * 与左侧目录一致；不在此处重新排序。
 */
export function resolveAdjacentSlug(
  slug: string,
  entries: DocsNavItem[],
): { prev: string | null; next: string | null } {
  const index = entries.findIndex((e) => e.slug === slug);
  if (index === -1) return { prev: null, next: null };
  return {
    prev: index > 0 ? entries[index - 1].slug : null,
    next: index < entries.length - 1 ? entries[index + 1].slug : null,
  };
}

function flattenEntries(sections: DocsNavSection[]): DocsNavItem[] {
  return sections.flatMap((section) => section.items);
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
// 站内文档链接（后端已解析为 /docs/{slug}）用路由跳转，不整页刷新。
const markdownComponents: Components = {
  h2: ({ children }) => <h2 id={slugifyHeading(flattenText(children))}>{children}</h2>,
  h3: ({ children }) => <h3 id={slugifyHeading(flattenText(children))}>{children}</h3>,
  a: ({ href, children }) => {
    if (href && href.startsWith('/docs/')) {
      return <Link to={href}>{children}</Link>;
    }
    if (href && href.startsWith('#')) {
      return <a href={href}>{children}</a>;
    }
    return (
      <a href={href} target="_blank" rel="noreferrer">
        {children}
      </a>
    );
  },
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

function groupBySection(
  items: DocsNavItem[],
): Array<{ section: string; items: DocsNavItem[] }> {
  const groups: Array<{ section: string; items: DocsNavItem[] }> = [];
  for (const item of items) {
    const section = item.section ?? '';
    const last = groups[groups.length - 1];
    if (last && last.section === section) {
      last.items.push(item);
    } else {
      groups.push({ section, items: [item] });
    }
  }
  return groups;
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
          {groupBySection(section.items).map((group, index) => (
            <div key={`${group.section}-${index}`}>
              {group.section ? (
                <p className="mb-0.5 mt-2 px-2 text-[11px] font-medium text-muted-foreground/80">
                  {group.section}
                </p>
              ) : null}
              <ul className="space-y-0.5">
                {group.items.map((item) => (
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
        </div>
      ))}
    </>
  );
}

export function DocsPage() {
  const { slug } = useParams<{ slug: string }>();
  const [sections, setSections] = React.useState<DocsNavSection[] | null>(null);
  const [indexSlug, setIndexSlug] = React.useState<string | null>(null);
  const [detail, setDetail] = React.useState<DocsDetail | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [navOpen, setNavOpen] = React.useState(false);

  React.useEffect(() => {
    let cancelled = false;
    fetchDocsList()
      .then((payload) => {
        if (cancelled) return;
        setSections(payload.sections);
        setIndexSlug(payload.index);
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const entries = React.useMemo(
    () => (sections ? flattenEntries(sections) : []),
    [sections],
  );
  const fallbackSlug = indexSlug ?? entries[0]?.slug ?? null;
  const currentSlug = slug ?? fallbackSlug;

  // 切换文档时收起窄屏抽屉——抽屉展开着会挡住新一篇的正文。
  React.useEffect(() => {
    setNavOpen(false);
  }, [currentSlug]);

  React.useEffect(() => {
    // 清单未到位不取正文：未知 slug 在清单到位后由渲染层重定向，若先按 slug 发一次请求，
    // 失败写入的 error 会盖住重定向（表现为「文档加载失败」而不是回到目录）。
    if (!sections || !currentSlug) return;
    if (!entries.some((entry) => entry.slug === currentSlug)) return;
    let cancelled = false;
    setDetail(null);
    fetchDoc(currentSlug)
      .then((payload) => {
        if (!cancelled) setDetail(payload);
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      });
    return () => {
      cancelled = true;
    };
  }, [currentSlug, sections, entries]);

  if (error) {
    return (
      <div className="flex h-full items-center justify-center px-6">
        <p className="text-sm text-muted-foreground">文档加载失败：{error}</p>
      </div>
    );
  }
  if (!sections) {
    return (
      <div className="flex h-full items-center justify-center">
        <div className="h-7 w-7 animate-spin rounded-full border-2 border-border border-t-foreground" />
      </div>
    );
  }
  if (!slug) {
    if (!fallbackSlug) {
      return (
        <div className="flex h-full items-center justify-center px-6">
          <p className="text-sm text-muted-foreground">文档库为空</p>
        </div>
      );
    }
    return <Navigate to={`/docs/${fallbackSlug}`} replace />;
  }

  const current = entries.find((e) => e.slug === slug);
  // 未知 slug（手输错字 / 旧链接 / 文档改名）回文档中心首页——渲染空正文或错误页都不如
  // 把人送回目录树。真实路由里 `/docs` 本身会落到索引页。
  if (!current) {
    return <Navigate to="/docs" replace />;
  }
  const currentCategory =
    sections.find((section) => section.items.some((item) => item.slug === slug))?.category ?? '';
  const { prev, next } = resolveAdjacentSlug(slug, entries);
  const body = detail?.slug === slug ? detail.content : '';
  const toc = extractToc(body);
  const canonicalTitle = detail?.canonical
    ? entries.find((e) => e.slug === detail.canonical)?.title
    : undefined;

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
            <DocsNavList sections={sections} currentSlug={currentSlug} />
          </nav>
        ) : null}
      </div>
      {/* 宽屏（≥lg）：常驻左侧导航树。 */}
      <nav
        aria-label="文档目录"
        className="hidden w-64 shrink-0 overflow-y-auto border-r border-border/70 px-3 py-4 lg:block"
      >
        <DocsNavList sections={sections} currentSlug={currentSlug} />
      </nav>
      <article className="min-h-0 min-w-0 flex-1 overflow-y-auto px-6 py-6 lg:px-10">
        <div className="mx-auto max-w-3xl">
          <p className="mb-1 text-xs text-muted-foreground">文档中心 / {currentCategory}</p>
          <h1 className="mb-4 text-2xl font-semibold tracking-tight text-foreground">
            {detail?.title ?? current.title}
          </h1>
          {detail?.status === 'summary' && detail.canonical ? (
            <p className="mb-4 rounded-lg border border-border/70 bg-muted/40 px-3 py-2 text-xs text-muted-foreground">
              本文为摘要，正本见{' '}
              <Link to={`/docs/${detail.canonical}`} className="underline underline-offset-2">
                {canonicalTitle ?? detail.canonical}
              </Link>
              。
            </p>
          ) : null}
          {body ? (
            <div className="docs-prose text-sm leading-7 text-foreground">
              <ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownComponents}>
                {body}
              </ReactMarkdown>
            </div>
          ) : (
            <div className="flex h-32 items-center justify-center">
              <div className="h-5 w-5 animate-spin rounded-full border-2 border-border border-t-foreground" />
            </div>
          )}
          {detail && detail.backlinks.length > 0 ? (
            <section
              aria-label="被引用"
              className="mt-8 rounded-lg border border-border/70 px-4 py-3"
            >
              <p className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
                本文被以下文档引用
              </p>
              <ul className="space-y-1 text-sm">
                {detail.backlinks.map((link) => (
                  <li key={link.slug}>
                    <Link
                      to={`/docs/${link.slug}`}
                      className="text-muted-foreground hover:text-foreground"
                    >
                      {link.title}
                    </Link>
                  </li>
                ))}
              </ul>
            </section>
          ) : null}
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