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
  { file: '配置与管理/律师场景配置指南.md', slug: 'lawyer-guide' },
  { file: '常见问题与排障/常见问题.md', slug: 'faq' },
  { file: '常见问题与排障/识别效果不佳怎么办.md', slug: 'poor-results' },
  { file: '常见问题与排障/遇到报错怎么办.md', slug: 'errors' },
  { file: '更新日志/版本更新记录.md', slug: 'changelog' },
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
    throw new Error(
      `文档 frontmatter 字段缺失或类型错误（需要 title/category/order/updated）：${source}`,
    );
  }
  return { title, category, order, updated };
}

/**
 * 按 category 分组、组内按 order 升序、组间按**分类首现顺序**。
 *
 * 首现顺序 = `entries` 数组顺序（= `getAllDocsEntries()` 返回的注册表顺序，
 * 即阅读顺序 新手必读 → 操作指南 → 配置与管理 → 常见问题与排障 → 更新日志）。用 Map 的插入序
 * 天然实现：第一次遇到某分类时插入，之后只往数组追加，分类位置不再变化。
 *
 * 不按「组内最小 order」或「篇数」排：各分类都从 order 1 起，前者全部并列；
 * 篇数降序会把 5 篇的「操作指南」排到 3 篇的「新手必读」之前，与 spec §3 的
 * 阅读顺序相悖（2026-11-17 主控裁决 D）。
 */
export function buildDocsNav(entries: DocsEntry[]): DocsNavSection[] {
  const sections = new Map<string, DocsEntry[]>();
  for (const entry of entries) {
    const list = sections.get(entry.category);
    if (list) {
      list.push(entry);
    } else {
      sections.set(entry.category, [entry]);
    }
  }
  return [...sections.values()].map((items) => ({
    category: items[0].category,
    items: [...items].sort((a, b) => a.order - b.order),
  }));
}

/** vite `?raw` glob：键形如 `/src/features/docs-content/新手必读/产品简介.md`。 */
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
  // 返回顺序 = DOCS_MANIFEST 登记顺序（= 阅读顺序）。order 是「分类内序号」，
  // 全局按 order 排序会打乱分类（各分类都从 1 起 ⇒ 变成跨分类交错）。
  return entries;
}
