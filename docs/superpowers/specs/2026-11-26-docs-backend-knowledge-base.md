# 文档中心：后端托管 + 知识库结构（索引 / 原子文档 / 双向链接）

- 日期：2026-11-26
- 状态：待审阅
- 关联：#368（实施）、#367（内容）、#348（原文档中心）
- 目标读者：实现者、文档作者

## 1. 背景与目标

现状（#348）：文档 Markdown 在 `frontend/src/features/docs-content/`，`docs-manifest.ts` 硬编码 16 篇，构建期打进前端包。问题：

1. 改文档必须重建前端，无法热更新；
2. 内容与前端代码耦合；
3. 单篇文档承担多个主题，同一事实多处描述，知识库随时间腐化。

目标：

- **后端托管**：文档存 `backend/docs-content/`，`DOCS_ROOT` 可配、可挂载卷；改文件即生效。
- **用户文档边界**：只放用户需要的四类——是什么（产品简介）、功能介绍（单文件 / 批量 / 供料 / 还原 / 水印 / 预览 / 导出）、怎么用（快速开始 / 配置）、出问题怎么办（FAQ / 排障）；技术架构、证据、术语、维护规则等项目文档放 `docs/项目总文档/`，不进用户文档中心。
- **知识库结构**：一个事实一个正本（`canonical_for`）；其他页只摘要并链回正本；用户文档不强制索引页（无 index 时落到第一篇）。
- **双向链接**：正文写 `[[slug|标题]]`，后端解析前向链接并自动生成反链。
- **Obsidian 兼容**：文件名即 slug（ASCII），`backend/docs-content` 可直接作为 vault 打开，graph/backlinks 原生可用。

非目标（沿用 #348）：站内搜索、版本切换、多语言、在线编辑、评论反馈。

## 2. 数据模型

### 2.1 文件与 slug

- 每个文档一个 `.md` 文件，**文件名（不含扩展名）= slug**，只允许 `^[a-z0-9][a-z0-9-]*$`。
- 分类用目录表达，目录名可中文（仅影响组织，不进入 URL）。
- URL：`/docs/{slug}`。

### 2.2 Frontmatter

```yaml
title: 单文件处理             # 必填，中文标题
category: 功能介绍            # 必填，导航分类（区域）
section:                      # 可选，分类内分组；用户文档默认不用
order: 1                    # 必填，分类内排序
updated: 2026-11-26         # 必填，ISO 日期
status: canonical           # 必填：index | canonical | summary | draft | deprecated
canonical_for:              # 可选，本页拥有的事实键（全局唯一）
  - definition
canonical: product-definition  # status=summary 时必填，指向正本 slug
summary: 一句话摘要           # 可选，≤ 120 字；索引与导航用
```

校验规则：

- `status=index` 的页面只允许摘要与链接（正文不出现 `##` 以下的实质段落？实现上只做“不得有 canonical_for” + 字数上限）。
- `status=summary` 必须有 `canonical`，且正文 ≤ 3 段/摘要 ≤ 200 字。
- `status=deprecated` 必须有 `canonical`（指向接替页）。
- `canonical_for` 的每个事实键全局只能出现一次。

### 2.3 分类顺序

后端固定配置（不依赖文件系统顺序）：

```
新手必读 → 功能介绍 → 配置与管理 → 常见问题与排障 → 更新日志
```

未登记的分类排在最后，并在 lint 中报警。

## 3. 链接与反链

### 3.1 语法

- `[[slug]]` → 显示目标 `title`；
- `[[slug|显示名]]` → 显示指定文字；
- 代码块（```）与行内代码（`...`）内的 `[[...]]` **不解析**（用于文档中举例）。

### 3.2 解析

后端在读取文档时：

1. 用正则 `\[\[([a-z0-9][a-z0-9-]*)(?:\|([^\]]+))?\]\]` 提取链接，跳过代码区段；
2. 查 slug 表：
   - 命中 → 输出标准 Markdown 链接 `[显示名](/docs/{slug})`，并记录一条前向边 `A → B`；
   - 未命中 → 保持原文并记 warning（运行时），lint 时判红。
3. 构建全局图：`forward[A] = {B,...}`，`backlinks[B] = {A,...}`。

### 3.3 反链

`GET /api/v1/docs/{slug}` 返回 `backlinks: [{slug,title}]`。前端在正文末尾渲染「本文被以下文档引用」。反链无需手写。

## 4. API 契约

所有接口鉴权：复用现有登录校验；未授权 401。

### 4.1 清单

`GET /api/v1/docs`

```json
{
  "index": null,
  "sections": [
    {
      "category": "新手必读",
      "items": [
        {"slug":"product-intro","title":"产品简介","summary":"…","status":"canonical","order":1,"updated":"2026-11-26"}
      ]
    }
  ]
}
```

- 只返回 `status != deprecated` 的页面（deprecated 仅供链接跳转与历史查询）；
- `index` 为 `status=index` 的页面 slug（若多个取 order 最小）。

### 4.2 详情

`GET /api/v1/docs/{slug}`

```json
{
  "slug": "product-intro",
  "title": "产品简介",
  "category": "新手必读",
  "status": "canonical",
  "updated": "2026-11-26",
  "content": "# 产品简介\n\n…（[[…]] 已解析为标准链接）",
  "links": [{"slug":"quick-start","title":"快速开始"}],
  "backlinks": [{"slug":"product-definition","title":"产品定义"}]
}
```

- `404`：slug 不存在或文件缺失；
- 正文不返回 frontmatter。

## 5. 存储与热更新

- `DOCS_ROOT` 环境变量，默认 `backend/docs-content`；Docker Compose 挂载为卷。
- 启动时全量扫描并建索引；生产运行期按目录 mtime 做 TTL 检查（默认 5s）或每次请求 stat 根目录，变更则重建（文档量级 < 百篇，重建成本可忽略）。
- 内存缓存内容与索引；写入方只改文件，不调 API。

## 6. 前端改造

1. 删除 `frontend/src/features/docs-content/` 与 `docs-manifest.ts`（及其测试）；前端包不再含文档正文。
2. `/docs`：fetch `/api/v1/docs`，渲染分类树；默认打开 `index` 指向的页面。
3. `/docs/:slug`：fetch 详情；`react-markdown` + `remark-gfm` 渲染；内部链接用路由跳转（不整页刷新）。
4. 文末「被引用」面板：`backlinks` 为空则不显示。
5. 状态：加载骨架、404、接口错误（含重试）；沿用现有登录守卫。

## 7. 治理脚本 `docs_lint.py`

CI 与本地均可跑；失败返回非 0：

1. frontmatter 必填与类型；
2. slug 唯一、文件名合法；
3. `canonical_for` 事实键全局唯一；
4. 链接可解析（跳过代码区段）、无自链、无孤儿（每篇至少被索引或另一篇引用）；
5. 索引覆盖全部 `status=canonical`；
6. `summary` 页有 `canonical` 且长度受限；
7. **完全重复句子（≥40 字）跨文档判红**；近似重复（shingle Jaccard ≥ 0.8）报警；
8. 未登记分类报警。

## 8. 迁移步骤

1. `git mv frontend/src/features/docs-content/* backend/docs-content/`，目录结构保留；
2. 文件名改为 slug（如 `新手必读/产品简介.md` → `新手必读/product-intro.md`），frontmatter 补 `status`/`canonical_for`/`summary`；
3. 删除 `docs-manifest.ts` 与相关测试；
4. 项目总文档（技术 / 证据 / 术语 / 维护规则）放 `docs/项目总文档/`，**不进** `backend/docs-content/`——用户文档中心只放用户内容；
5. `产品简介`、`脱敏理念与能力边界` 改为自包含的用户页（不链项目文档），正文只留用户需要的内容；
6. 「功能介绍」区覆盖：单文件处理、批量处理、预览与调整、导出与交付、供料模式、还原工具、水印检测；
7. 后端路由、前端 fetch、测试同 PR。

## 9. 安全

- 鉴权：所有 docs API 需登录；未授权 401。
- 路径穿越：slug 正则白名单；`realpath` 必须位于 `DOCS_ROOT` 内；只读 `.md`。
- XSS：内容来自仓内文件，不接受用户输入；react-markdown 默认不执行 HTML（如需禁用 raw HTML，显式配置）。
- 体积：单篇上限（如 1 MB），超出拒绝。

## 10. 测试

- 后端：清单/详情/404/未授权/穿越/热更新（改文件后重建）/反链图/重复 slug；
- 前端：fetch 渲染、路由跳转、404、错误态、反链面板；
- 契约：`docs_lint.py` 红/绿各一组（断链、重复 canonical_for、重复句子、孤儿）；
- 人工：Obsidian 打开 vault，`[[slug]]` 跳转与 graph 连线。

## 11. 风险与回滚

- 风险：迁移期间前端内置内容与后端内容并存 → 以“同 PR 删除前端内容”避免双份；
- 风险：slug 变化导致外链失效 → 迁移时 slug 保持不变（见 §8）；
- 回滚：保留 #348 的前端实现于 git 历史，必要时 revert 本 PR。

## 12. 验收映射

| AC | 对应 |
|---|---|
| AC1 热更新 | §5 |
| AC2 不含正文 | §6.1 |
| AC3 鉴权 | §4 / §9 |
| AC4 链接与反链 | §3 / §4.2 |
| AC5 路径穿越 | §9 |
| AC6 lint 红绿 | §7 / §10 |
| AC7 Obsidian | §2.1 / §10 |
| AC8 测试绿 | §10 |