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
