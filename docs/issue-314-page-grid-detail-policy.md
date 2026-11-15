# Issue#314 页格点击语义：选择与诊断分离

> 关联 Issue：<https://github.com/youayou-Lee/LexMask/issues/314>
> 状态：设计 + 实现 + 测试完成（分支 `feat/issue314-page-grid-detail`），待评审/合并。

## 背景

「逐页结果与选页导出」的页格矩阵由 `frontend/src/features/batch/components/batch-page-matrix.tsx` 一处实现，被三个宿主复用：

- 批量导出弹窗 `partial-export-dialog.tsx`（选页导出）；
- 单文件导出块 `playground-page-export.tsx`（单文件选页导出）；
- 批量审阅行 `batch-row-pages.tsx`（识别完成后逐页审阅）。

Issue#279 给页格加了「第 N 页发生了什么」技术面板（路由 verdict、fallback 原因码、`stage_ms`、`error_type`、`attempts`）与卷头 route chip；Issue#289 又把点击语义收敛为「点击任何格子只展开/收起详情，重试走面板内按钮」。这两次改动都默认「每个宿主都需要诊断面」。

用户反馈（2026-11-15）：在导出场景里点页码是为了选页，每次点击都弹出技术面板，成功页的 `stage_ms`/`route_verdict` 对导出没有价值，而且面板插在页格下方会把选页操作顶走。导出需要的是「点页 = 选中/取消」，审阅才需要「问题页 = 看诊断」。

## 根因

`batch-page-matrix.tsx:187-194` 的 `activate` 把两件事耦合在一个无条件分支里：

```ts
const activate = (page: number, state: PageCellState, shiftKey = false) => {
  if (!shiftKey) setExpandedPage((prev) => (prev === page ? null : page));
  if (state === 'failed') return;
  toggle(page, state, shiftKey);
};
```

- 展开详情不区分宿主、不区分页状态；导出宿主即使不传 `onRetryPage`/`onManualAnnotate`，仍会渲染只读技术面板与 route chip。
- `BatchPageMatrix` 没有「宿主场景」参数，三个宿主只能继承同一套交互，无法表达「导出不要诊断、审阅只要问题页诊断」。
- 卷头 route chip 的渲染（`routeChip` IIFE，`batch-page-matrix.tsx:227-251`）同样没有场景门。

## 方案

给 `BatchPageMatrix` 增加 `detailPolicy?: 'always' | 'issues' | 'none'`（默认 `always`，兼容尚未迁移的宿主）：

| 策略 | 点击成功/pending 页 | 点击 failed 页 | 点击 manual_verified 页 | 详情面板 | route chip |
|---|---|---|---|---|---|
| `always`（默认） | 展开/收起详情 + 切换选择 | 展开/收起详情，不进选择集 | 展开/收起详情 + 切换选择 | 渲染 | 渲染 |
| `issues` | 只切换选择 | 展开/收起详情，不进选择集 | 展开/收起详情 + 切换选择 | 仅问题页展开时渲染 | 渲染 |
| `none` | 只切换选择 | 无操作（不可选，错误状态仍由格色/title 可见） | 只切换选择 | 不渲染 | 不渲染 |

落地要点：

1. `activate` 增加策略门：`none` 永不写 `expandedPage`；`issues` 只在 `state` 为 `failed`/`manual_verified` 时写；`always` 保持现行为。选择分支完全复用既有 `toggle`，Shift 连选、锚点、`#199` 失败页守卫不动。
2. route chip 与详情面板的渲染增加 `detailPolicy !== 'none'` 门（面板另外仍受 `expandedCell` 约束，双保险）。
3. 重试（`page-detail-retry`）、人工补框（`page-detail-manual-annotate`）、撤销（`page-detail-manual-revoke`）入口仍只在面板内渲染：`issues` 下失败页/人工页可展开，因此入口保留且可用；`none` 下整块不出现。
4. 失败页仍然不可进入选择集（`#199` 红线，`toggle` 内 `selectableState` 守卫不变）；`manual_verified` 与 success 同口径可选。
5. 根节点加 `data-detail-policy` 供测试与排障读取；`none` 下不可选格子的 cursor 由 `cursor-pointer` 改为 `cursor-not-allowed`（只改视觉诚实性，不加 `disabled` 属性，避免误导 a11y）。
6. 范围输入、全选成功页、反选、清空按钮、汇总行、失败页提示、视觉列缺失提示均与策略无关（不属于「技术面板」）。

## 宿主矩阵

| 宿主 | 场景 | `detailPolicy` | 理由 |
|---|---|---|---|
| `partial-export-dialog.tsx:221` | 批量任务选页导出 | `none` | 导出点页只选页；诊断去审阅/记录看 |
| `playground-page-export.tsx:135` | 单文件选页导出 | `none` | 同上（单文件识别进行中也可选页，减少干扰） |
| `batch-row-pages.tsx:147` | 批量审阅行内页格 | `issues` | 成功页只勾选；失败/人工页展开看原因、重试、撤销人工框 |
| 其他（测试/未迁移宿主） | — | 缺省 `always` | 现行为不变，逐个宿主迁移 |

## 验收标准

- **AC1（导出）**：导出弹窗（单文件 + 批量）点任意页码只切换选择；DOM 不出现 `page-detail-panel`（「第 N 页发生了什么」），不出现 `page-route-chip`；失败页不可选但错误状态（格色/title/失败提示行）仍可见。
- **AC2（审阅）**：批量审阅行内页格点成功页只切换选择、不展开；点失败页展开详情，`page-detail-retry` 可达且可用；点人工页展开详情，撤销入口可达。
- **AC3（兼容）**：缺省 `detailPolicy` 仍等价于 `always`，Issue#279/#289 既有诊断行为零变化；`issues` 下 route chip 仍渲染（审阅卷头诊断面不丢）。
- **AC4（选择回归）**：Shift 连选、范围输入、全选成功页/反选/清空、`#199` 失败页不可选、手动页可选行为不回归。

## 测试计划

1. 新增 `frontend/src/features/batch/components/issue314-page-detail-policy.test.tsx`：
   - `none`：点成功页只 onChange，无 panel、无 route chip（即使 `route_summary` 有值）；点失败页无 onChange 无 panel；Shift 连选与范围输入照常；`data-detail-policy="none"` 可读。
   - `issues`：点成功页只 onChange 无 panel；点失败页展开 panel 且 `page-detail-retry` 可达、不 onChange；点 manual 页展开 panel + onChange + 撤销入口可达；route chip 仍渲染。
   - `always`（缺省）：与现行为一致（成功页展开 + 选择）。
2. 宿主级交互：
   - `issue278-export-dialog.test.tsx`：批量导出弹窗点页 → 选择变化、无详情标题；点失败页无面板。
   - `playground-page-export.test.tsx`：单文件导出点页 → 只选择、DOM 无「第 N 页发生了什么」。
   - `issue257-page-retry.test.tsx`：标注批量审阅（`BatchRowPages`）成功页点击不展开；失败页展开 + 重试按钮可达（既有用例补断言）。
3. 更新受影响用例：`issue279-page-detail.test.tsx` 中「导出挂载点只读」用例改为 `detailPolicy="none"` 语义（导出不再有面板）；`issue276-page-detail-visual.test.tsx` 补 `none` 下视觉行不出现在面板外、卷头视觉提示仍在。
4. 命令：`cd frontend && npx vitest run src/features/batch src/features/playground`；收尾 `npm test`、`npm run build`、`npm run lint`、`npx tsc -p tsconfig.test.json --noEmit`。

## 风险

- **与 Issue#313 冲突**：`playground-page-export.tsx` 同时被 #313 增加 `onRequestClose`。本支只改页格调用处一行 props，不改文件结构与相邻代码，冲突面最小；跨支冲突由 Lead 处理。
- **诊断面可发现性**：导出场景隐藏 route chip 后，用户在导出弹窗内看不到卷级路由摘要。诊断仍可通过批量审阅页（`issues`）、导出记录/页事件日志查看，属预期取舍；若后续要求导出弹窗显式入口，另案。
- **漏迁移宿主**：缺省 `always` 会让未迁移宿主保持旧交互，这是有意兼容；三个已识别宿主全部显式传参，测试钉住。
- **交互可点性**：`none` 下失败格点击无响应，只能靠 title/格色/失败提示行表达错误状态；不引入 `disabled` 属性以免读屏器把整格从 tab 序移除（失败页仍需可聚焦查看 title）。
- **测试判据**：更新 `issue279` 用例时只改「导出挂载点」前提，不删除既有 `#199`、`#289`、手动页、视觉列判据。