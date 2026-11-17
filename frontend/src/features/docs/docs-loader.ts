// Copyright 2026 LexMask Contributors

/**
 * 文档原文加载层（`docs-loader`）。
 *
 * 实现落在 `docs-manifest.ts`：vite `?raw` glob 与 frontmatter 解析共用同一份
 * 内容清单（`DOCS_MANIFEST`），拆成两个文件会形成循环依赖（glob 需要清单、
 * 清单需要 glob），故此处只做稳定的消费入口——下游（页面/路由）从
 * `./docs-loader` 取原文，从 `./docs-manifest` 取元信息与类型。
 */
export { getAllDocsEntries, loadDocRaw } from './docs-manifest';
