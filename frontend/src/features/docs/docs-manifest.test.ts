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

/** 磁盘上的全部文档 md（与 docs-manifest.ts 内部 glob 同一口径，用于守卫注册表 ↔ 文件一致）。 */
const diskDocs = import.meta.glob('/src/features/docs-content/**/*.md', {
  query: '?raw',
  import: 'default',
}) as Record<string, () => Promise<string>>;

describe('parseDocsFrontmatter', () => {
  it('解析合法 frontmatter', () => {
    const raw =
      '---\ntitle: 快速开始\ncategory: 新手必读\norder: 2\nupdated: 2026-11-17\n---\n\n# 正文';
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

  it('order 非整数（类型错误）时抛错', () => {
    const raw =
      '---\ntitle: 标题\ncategory: 新手必读\norder: abc\nupdated: 2026-11-17\n---\n\n正文';
    expect(() => parseDocsFrontmatter(raw, 'bad.md')).toThrow(/order/);
  });
});

describe('buildDocsNav', () => {
  // 组间顺序 = 分类在注册表里的**首现顺序**（Map 插入序），不是组内最小 order、
  // 更不是篇数——篇数降序会把 5 篇的「操作指南」排到 3 篇的「新手必读」之前，
  // 与 spec §3 的阅读顺序（新手必读 → 操作指南 → 配置与管理 → 常见问题）相悖。
  it('按 category 分组、组内按 order 升序、组间按分类首现顺序', () => {
    const entries = [
      { slug: 'c', title: 'C', category: '新手必读', order: 1, updated: '2026-11-17' },
      { slug: 'b', title: 'B', category: '操作指南', order: 1, updated: '2026-11-17' },
      { slug: 'a', title: 'A', category: '新手必读', order: 2, updated: '2026-11-17' },
    ];
    const nav = buildDocsNav(entries);
    expect(nav.map((s) => s.category)).toEqual(['新手必读', '操作指南']);
    expect(nav[0].items.map((i) => i.slug)).toEqual(['c', 'a']);
  });

  // 反例：篇数多的分类**后登记**时不得插到前面；首现分类的组内最小 order 更大
  // （9 > 1）也不得因此让位。旧规则（篇数降序 / 首篇 order）在这两条上都会排错。
  it('组间顺序只看首现：篇数多的后登记不提前，首现分类 order 更大也不让位', () => {
    const entries = [
      { slug: 'solo', title: '单篇', category: '常见问题与排障', order: 9, updated: '2026-11-17' },
      { slug: 'b1', title: 'B1', category: '批量处理', order: 1, updated: '2026-11-17' },
      { slug: 'b2', title: 'B2', category: '批量处理', order: 2, updated: '2026-11-17' },
    ];
    const nav = buildDocsNav(entries);
    expect(nav.map((s) => s.category)).toEqual(['常见问题与排障', '批量处理']);
    expect(nav[1].items.map((i) => i.slug)).toEqual(['b1', 'b2']);
  });
});

describe('内容清单（manifest）', () => {
  it('loadDocRaw 返回真实文档原文（含 frontmatter；正文不再重复 H1）', async () => {
    const entry = DOCS_MANIFEST.find((e) => e.slug === 'product-intro');
    expect(entry).toBeDefined();
    const raw = await loadDocRaw('product-intro');
    // 页面用 frontmatter 的 title 渲染唯一 H1，正文再从 `##` 起（H1 不重复）。
    expect(raw).toContain('## 墨隐是什么');
    expect(raw.startsWith('---\ntitle: 产品简介\n')).toBe(true);
    expect(raw).not.toContain('\n# 产品简介\n');
  });

  it('未注册 slug 抛错', async () => {
    await expect(loadDocRaw('not-exist')).rejects.toThrow(/not-exist/);
  });

  // 真实清单的组间顺序 = 注册表顺序（= spec §3 的阅读顺序）。这条是「篇数降序」
  // 旧规则的红灯用例：12 篇里「操作指南」最多（5 篇），旧规则会把它顶到最前。
  it('真实清单的组间顺序 = 注册表顺序（新手必读 → 操作指南 → 配置与管理 → 常见问题）', async () => {
    const nav = buildDocsNav(await getAllDocsEntries());
    expect(nav.map((s) => s.category)).toEqual([
      '新手必读',
      '操作指南',
      '配置与管理',
      '常见问题与排障',
    ]);
  });

  it('getAllDocsEntries 返回全部注册文档（≥12 篇，frontmatter 解析正确）', async () => {
    const entries = await getAllDocsEntries();
    expect(entries.length).toBeGreaterThanOrEqual(12);
    expect(entries.find((e) => e.slug === 'product-intro')).toMatchObject({
      title: '产品简介',
      category: '新手必读',
      order: 1,
    });
  });

  // order 是「分类内序号」，全局按 order 排序会打乱分类（product-intro → single-file →
  // account-login → faq → quick-start…），而下游（默认页 / 上一篇下一篇）要的是阅读顺序。
  it('getAllDocsEntries 返回顺序 = DOCS_MANIFEST 登记顺序（不按 order 全局排序）', async () => {
    const entries = await getAllDocsEntries();
    expect(entries.map((e) => e.slug)).toEqual(DOCS_MANIFEST.map((entry) => entry.slug));
  });

  it('DOCS_MANIFEST 与磁盘文件一一对应（无漏登记 / 无缺失）', async () => {
    const diskFiles = Object.keys(diskDocs)
      .map((key) => key.replace('/src/features/docs-content/', ''))
      .sort();
    expect(diskFiles).toEqual(DOCS_MANIFEST.map((entry) => entry.file).sort());
    for (const entry of DOCS_MANIFEST) {
      await expect(loadDocRaw(entry.slug)).resolves.toContain('---');
    }
  });
});
