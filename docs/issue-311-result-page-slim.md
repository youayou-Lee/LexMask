# Issue #311：结果页默认视图瘦身（设计）

- Issue：<https://github.com/youayou-Lee/LexMask/issues/311>
- 分支：`feat/issue311-result-slim`（基线 `origin/preview` @ `356d901`）
- 范围：仅前端结果页展示层（`features/playground`），不改后端、不引新依赖、不动任务中心与识别中加载态。

## 1. 背景

匿名化完成后的结果页当前从上到下依次渲染：

1. 「匿名化完成」深色横幅（图标、标题、已处理计数、水印 chip、步骤条、hint）；
2. 质量报告折叠条（展开后一整块指标卡）；
3. 处理详情卡（默认展开，含阶段 rows / 总耗时 / 失败页）；
4. 逐页结果 / 选页导出块（默认展开，86 页卷的页格几乎吃掉首屏）；
5. 对照 / 成品切换条；
6. 文件对比预览。

横幅与两块诊断信息长期占据首屏，用户真正要看的对比预览被挤到首屏之下。Issue #311 要求默认视图只保留「动作条 + 逐页/导出块 + 预览」，把诊断信息收进按需打开的「详情」弹窗，同时保证失败页不会被藏起来（未处理数仍在详情报表面板展示）。

## 2. 目标与非目标

### 目标

- 删除深色完成横幅整块，只保留右上角一行动作按钮。
- 水印 chip（如「已摘除 3 页」）降级为动作行内的小 badge，保持可见（合规证据）。
- 质量报告与处理详情默认不进入 DOM；合并进一个「详情」弹窗（上半质量报告、下半处理详情，复用 `ProcessingRowsList`）。
- 失败页 / 任务失败时，「详情」按钮带琥珀色告警点（未处理数不算告警：其含用户主动取消勾选）。
- 结果页容器保持 `flex-1 min-h-0`，默认视图给对比预览留出主要视口。
- zh/en 文案同步；弹窗默认关闭，ESC / 遮罩可关，键盘可达。

### 非目标

- 不改后端接口、报告字段与数据获取逻辑。
- 不改变任务中心、识别中加载态的卡片形态。
- 不重构 `PlaygroundPageExport` 的选页/导出能力，只增加「默认收起」入口。
- 不新增第三方依赖（弹窗复用现有 Radix `dialog` 基元）。

## 3. 现状与约束（代码事实）

| 位置 | 事实 |
| --- | --- |
| `frontend/src/features/playground/components/playground-result.tsx` | 根容器 `flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden`，顺序渲染横幅 → 报告 → 处理详情 → 导出块 → 切换条 → 预览；`reportOpen` 由宿主 `playground-page.tsx` 传入，`use-playground.ts:93` 默认 `false` |
| `playground-result-action-bar.tsx` | 深色横幅 + `RedactionReportSection` + 报告指标/分布 helpers；右侧动作按钮与 `data-testid` 均在此 |
| `processing-details-card.tsx` | `ProcessingDetailsCard` / `ProcessingRowsList` / `PlaygroundProcessingDetails`（任务 job → performance 解析壳）；空数据返回 `null` |
| `playground-page-export.tsx` | `useState(!compact)` ⇒ 结果页默认展开；`compact` 同时收窄页格高度并隐藏导出记录；新增 `defaultCollapsed` 后结果页默认收起 |
| `src/components/ui/dialog.tsx` | Radix 基元：焦点陷阱、ESC、遮罩关闭均为内置行为 |
| `src/i18n/{zh,en}.ts` | 扁平键表，`i18n-parity.test.ts` 双向校验键集合 |

数据侧（只读）：`GET /api/v1/redaction/{fileId}/report` 返回 `total_entities / redacted_entities / entity_type_distribution / confidence_distribution / source_distribution / coverage_rate / redaction_mode`；未处理项数按 `max(total_entities - redacted_entities, 0)` 计算。处理详情的失败页来自任务 `performance.failed_pages`。

## 4. 决策

### D1 横幅删除，动作行右对齐

`PlaygroundResultActionBar` 不再渲染 `Card`、图标、标题、已处理计数、步骤条、hint 与 `resultActionFlowCopy`。改为一行：

```
[水印 badge] [详情 •] [返回编辑] [处理新文件] [下载化名对照表] [下载文件]
```

- 保留既有 `data-testid`：`playground-back-edit`、`playground-download`、`playground-watermark-chip`。
- 新增 `playground-details-button`、`playground-details-alert`、`playground-new-file`。
- 水印 chip 由深色背景上的胶囊改为行内 `Badge`（中性描边/浅底），保留完整文案与可见性。
- `redactedCount` 不再参与横幅展示；动作条不再接收该 prop。

### D2 质量报告与处理详情合并进「详情」弹窗

新增 `playground-result-details-dialog.tsx`：

- 复用 Radix `Dialog`，`open` 直接绑定既有 `reportOpen` / `setReportOpen`，默认 `false`，不新增宿主状态。
- 上半部：质量报告内容（从 action bar 抽出），保留 `data-testid="playground-quality-report"` 与四项指标 `ReportMetric`（总数/已处理/未处理/覆盖率）及三类分布块。
- 下半部：处理详情内容，复用 `ProcessingRowsList`，保留 `processing-details-title` / `processing-details-total` / `processing-details-rows` / `processing-details-failed-pages` / `processing-details-text-direct` 及失败变体 `processing-details-failed`。
- 无任何可展示内容时渲染占位文案 `playground.detailsEmpty`。
- 弹窗默认不挂载内容（Radix `DialogContent` 仅在 open 时渲染），因此默认 DOM 不含报告/详情节点。

### D3 处理详情数据抽取为 hook

从 `processing-details-card.tsx` 抽出：

- `usePlaygroundProcessingDetails(fileId, filePageCount)`：沿用现有 provider → sticky → 草稿 → `getJob(jobId, { performance: true })` 解析链，返回 `{ jobId, performance, mergedPerf, rows, totalMs, textDirect, failedPages, hasData, failed, errorMessage }`。
- `ProcessingDetailsBody`：`ProcessingDetailsCard` 的纯展示体（保留全部 testid）。
- `ProcessingDetailsCard` 改为薄壳调用 `ProcessingDetailsBody`；`PlaygroundProcessingDetails` 保留导出以兼容既有测试，但结果页不再渲染它，改由 `PlaygroundResult` 调用 hook 提前拿到 `failedPages` 供告警点与弹窗使用。

收益：告警点与弹窗共用同一份取数，避免重复请求；默认视图仍不发多余 DOM。

### D4 告警点条件

`alert = failedPages.length > 0 || task.failed`。

- 告警点：`span` 8px 圆点，`bg-amber-500` + `ring-2 ring-background`，绝对定位在按钮右上角；`aria-hidden="true"`，按钮 `aria-label` 追加 `playground.detailsAlert` 文案，保证读屏可达。
- 未处理数由 `redactionReport` 计算：`Math.max(total_entities - redacted_entities, 0)`，仅作为报告指标展示；`total` 含 `selected=false`，不作为告警条件（门⑥ I3）。
- 信息不可被隐藏：只要有失败页/任务失败，告警点即出现，且点击详情可见完整清单与未处理数。

### D5 布局：预览占据主要视口

- 动作行与「导出块 + 对照/成品切换」各压成一行；动作行 `mb-2`，行高约 36px。
- `PlaygroundPageExport` 新增 `defaultCollapsed?: boolean`（默认 `false`，不影响其他调用方），结果页传 `true`：默认只显示一行摘要，仍可展开选页/导出。
- 「导出摘要 + 对照/成品切换」同一 flex 行；结果页容器保持 `flex-1 min-h-0`，预览区保持 `flex-1 min-h-0`。
- 估算：内容区顶部 12px padding + 动作行 36 + 间距 8 + 导出行 36 + 间距 8 ≈ 100px ≤ 120px；1440×900 内容高 824px，预览约 724px ≈ 80% 视口高（≥ 60%）。

### D6 文案

新增 zh/en 同步键（保留旧键，避免 parity 失败）：

- `playground.details`：详情 / Details
- `playground.detailsDialogTitle`：质量报告与处理详情 / Quality report & processing details
- `playground.detailsDialogDesc`：质量报告与逐阶段处理过程集中在这里，默认视图不占用预览空间。/ Quality metrics and per-stage processing live here, out of the default preview view.
- `playground.detailsEmpty`：暂无质量报告或处理详情。/ No quality report or processing details yet.
- `playground.detailsAlert`：有失败页或任务失败 / Some pages failed or the task failed

## 5. 改动文件

| 文件 | 改动 |
| --- | --- |
| `frontend/src/features/playground/components/playground-result.tsx` | 移除横幅/报告/详情卡直渲染；接入 hook、详情弹窗、动作行与「导出 + 切换」行 |
| `frontend/src/features/playground/components/playground-result-action-bar.tsx` | 删除深色横幅与报告 section；新增详情按钮/告警点/水印 badge |
| `frontend/src/features/playground/components/playground-result-details-dialog.tsx` | 新增：质量报告 + 处理详情弹窗 |
| `frontend/src/features/playground/components/processing-details-card.tsx` | 抽出 `ProcessingDetailsBody` 与 `usePlaygroundProcessingDetails`，保留兼容壳 |
| `frontend/src/features/playground/components/playground-page-export.tsx` | 新增 `defaultCollapsed` 可选 prop |
| `frontend/src/i18n/zh.ts`、`en.ts` | 新增详情弹窗文案 |
| `frontend/src/features/playground/components/playground-result.test.tsx` | 新增：默认隐藏、弹窗、告警点、动作行、布局类名 |
| `frontend/src/features/playground/components/playground-result-details-dialog.test.tsx` | 新增：指标渲染、空态、ESC/遮罩关闭 |
| `docs/issue-311-result-page-slim.md` | 本设计 |

## 6. 验收标准

1. **动作行**：默认视图不再出现「匿名化完成」深色横幅、图标、标题、已处理计数、步骤条、hint；右上角一行存在返回编辑、处理新文件、下载文件（有化名表时第四个）与详情按钮，`playground-back-edit` / `playground-download` 保留可点。
2. **水印 badge**：存在水印摘除信息时 `playground-watermark-chip` 在动作行内可见，文案包含页数。
3. **默认无诊断 DOM**：初始渲染不含 `playground-quality-report`、`processing-details-card` / `processing-details-title` / `processing-details-rows`。
4. **详情弹窗**：点击 `playground-details-button` 后弹窗出现，含质量报告四项指标、分布块、总耗时、阶段 rows、失败页清单；ESC 与遮罩可关闭，焦点可达；关闭后 DOM 回到默认。
5. **告警点**：存在失败页或任务失败时 `playground-details-alert` 出现；仅有未处理数（用户取消勾选）时不出现。
6. **布局**：结果页根容器保留 `flex-1 min-h-0`，预览容器保留 `flex-1 min-h-0`；1440×900、86 页完成态下，结果页内容区顶部到预览区顶部 chrome ≤ 120px，预览高度 ≥ 60% 视口；本仓无 Playwright 工程，门④用浏览器 DevTools 量取并截图留档（不含全局 76px 顶栏）。
7. **门禁**：`npm run build`、`npm run lint`、`npx tsc -p tsconfig.test.json --noEmit`、`npx vitest run` 全绿；zh/en parity 通过。

## 7. 测试计划（TDD）

先写失败测试，再实现：

1. `playground-result.test.tsx`
   - 默认渲染：无深色横幅文案/`playground-quality-report`/`processing-details-*`；动作行与三个保留 testid 存在；水印 badge 可见。
   - 告警点：`failedPages` 非空或 `task.failed` → 出现；仅有未处理数（含 `selected=false`）→ 不出现。
   - 点击详情 → `playground-quality-report` 与 `processing-details-title` / rows / total 出现；指标数值与 report 一致。
   - ESC 关闭弹窗；关闭后诊断节点消失。
   - 容器类名断言 `flex-1`、`min-h-0`；导出块默认收起（展开内容不在 DOM）。
2. `playground-result-details-dialog.test.tsx`
   - open 时渲染报告指标 + 处理详情行；空数据渲染 `playground.detailsEmpty`；失败变体显示 `processing-details-failed`。
3. 既有测试：`processing-details-card.test.tsx`、`processing-details-card.failed-pages.test.tsx` 保持通过；如断言旧横幅结构则同步更新判据，不删除。
4. 全量 `npx vitest run`；若耗时过长，至少跑 playground 目录 + i18n + 相关 feature 并说明。

## 8. 风险与待确认

1. **测量基准**：全局顶栏 `--page-shell-offset: 4.75rem`（76px）属于应用 chrome。若验收把全局顶栏计入「页面顶部到预览区顶部」，任何只改结果页的方案都无法 ≤ 120px。本设计按「结果页内容区顶部（page-shell 内顶）到预览区顶部」测量，需门④按此口径截图并注明。
2. **导出块收起属默认值变更**：`defaultCollapsed` 只影响结果页，用户仍可展开；若产品坚持默认展开，则第 6 条布局指标在 86 页卷下无法同时满足，需要另行拍板（例如把导出块移到预览下方）。
3. **弹窗复用 `reportOpen`**：与既有宿主/上下文解耦，但语义从「报告折叠」变为「详情弹窗」；`use-playground.ts` 的状态名保留，避免扩大改动面。
4. **处理详情 hook 抽取**：既有 `ProcessingDetailsCard` 测试与草稿回落逻辑必须保持字节级行为；先跑旧测试再改实现，避免解析链回归。
5. **无 Playwright 工程**：仓库当前没有 Playwright 测试配置，门④用 DevTools 量取 + 截图留档（内容区口径，不含全局 76px 顶栏），RTL 覆盖交互与 DOM 判据。

## 9. 实施顺序

1. 设计稿提交（本文件）。
2. 新增/调整测试并确认红。
3. 实现 hook 抽取 → 弹窗 → 动作行 → 结果页布局 → i18n。
4. 全量门禁与报告。