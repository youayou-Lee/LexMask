---
title: 维护规则
category: 项目总览
order: 15
updated: 2026-11-26
status: canonical
canonical_for:
  - maintenance
summary: 一个事实一个家；索引只放摘要；链接与反链；作者规范与自动检查。
---

# 维护规则

## 四条原则

1. **一个事实一个家**：每个事实只在一篇「正本」里定义，正本用 frontmatter 的 `canonical_for` 声明自己拥有哪些事实键；其他篇只摘要，不重写。
2. **索引只放摘要与链接**：[[overview|项目总览]] 是唯一的阅读入口，不承载正文。
3. **摘要必须链回正本**：`status: summary` 的页面必须写明 `canonical: <slug>`，且摘要不超过 3 行。
4. **数字只在正本**：性能与质量数字只出现在 [[evidence|证据与可信度]] 与 [[terminology|术语与口径]]，其他篇引用链接，不复制数值。

## 链接语法

- 正文用 `[[slug|显示名]]` 或 `[[slug]]`；文件名即 slug（如 `product-definition.md`）。
- 正反向都自动维护：你在 A 里链到 B，B 的「被引用」列表会自动出现 A——不需要手动写反链。
- 新增一篇文档后，必须在 [[overview|项目总览]] 里登记一行摘要与链接，否则视为孤儿。

## 作者规范

- 一篇只讲一件事；超过 150 行考虑拆分。
- 标题（H1）与 frontmatter `title` 一致；分类/顺序写在 frontmatter。
- 修改事实先改正本，再改引用它的页面。
- 废弃的页面加 `status: deprecated` 与 `canonical: <新slug>`，不要直接删除历史。

## 自动检查（随文档中心后端实施）

- 链接必须能解析到存在的 slug；
- `canonical_for` 事实键全局唯一；
- `status: summary` 必须有 `canonical`，且摘要长度受限；
- 索引必须覆盖全部正本；
- 跨文档完全重复的句子（≥40 字）判红，防止复制粘贴式腐化。

文档怎么被托管与渲染，见 [[architecture|技术与架构]]；版本与资料入口见 [[meta|版本与索引]]。