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
    throw new Error(
      `文档 frontmatter 字段缺失或类型错误（需要 title/category/order/updated）：${source}`,
    );
  }
  return { title, category, order, updated };
}

/**
 * 按 category 分组、组内按 order 升序、组间按「组内首篇 order」升序。
 *
 * 并列规则（各分类首篇 order 相同时，例如所有分类都从 order 1 起）：
 * 篇数多的分类在前，仍相同则按分类名升序——保证顺序确定、可复现，
 * 不依赖 entries 的传入顺序。
 */
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
    .sort(
      (a, b) =>
        a.items[0].order - b.items[0].order ||
        b.items.length - a.items.length ||
        a.category.localeCompare(b.category, 'zh-Hans-CN'),
    );
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
  // 返回顺序 = DOCS_MANIFEST 登记顺序（= 阅读顺序）。order 是「分类内序号」，
  // 全局按 order 排序会打乱分类（各分类都从 1 起 ⇒ 变成跨分类交错）。
  return entries;
}
