# Issue#313 单文件结果页「回到编辑」失效

## 背景

单文件任务在复核阶段打开「逐页结果与选页导出」弹窗后，选择页码并点「回到编辑」，界面没有任何变化：弹窗不关、画布不进入可操作状态，用户只剩「导出选中页」，无法回去 review、手动拉框。批量任务（`PartialExportDialog`）的同类按钮工作正常。

## 根因

1. `frontend/src/features/playground/components/playground-page-export.tsx:52` 以 `useState(!compact)` 维护展开态，`:89` 的 `expanded = bare || open` 让 `bare`（弹窗内）形态恒展开；点击「回到编辑」只执行 `setOpen(false)`，对 `bare` 无效。
2. 组件没有「关闭父 Dialog」的回调。宿主 `frontend/src/features/playground/playground-page.tsx:664-668` 的 Dialog 由 `exportPagesOpen` 控制，点击只能把背后的编辑画布 `scrollIntoView`，弹窗纹丝不动。
3. 临时单测（已删除）复现：点按钮后提交按钮仍在、组件没有收起。

附带问题：底栏仍显示旧产物文案「产物 = 说明页 1 页 + N 页」（`zh.ts:750` / `en.ts:787`），而 #278 已改为 zip 产物、说明页不再插入 PDF。

## 方案

- `PlaygroundPageExport` 新增可选 `onRequestClose`：
  - 点击「回到编辑」→ `setOpen(false)`（inline 形态收起导出块）→ 调用 `onRequestClose?.()`（弹窗形态由宿主关闭 Dialog）；
  - 在下一帧滚动到 `#playground-bbox-editor`，并尝试把焦点交给画布（`canvas[tabindex]` / 首个 `tabindex=0` 元素），保证键盘用户也回到编辑。
- 宿主 `playground-page.tsx` 传入 `onRequestClose={() => setExportPagesOpen(false)}`。
- 选择集仍由 `use-item-export` 的模块级 `selectionCache` 保留，无需额外处理。
- 修正 zh/en 的 `batchWizard.pages.exportSummary` 为 zip 口径（脱敏 PDF + 映射清单 + 可选 说明.txt）。

## 影响面

- 单文件弹窗形态（修复目标）；
- 识别中 `compact` 内联形态（保持收起 + 锚点聚焦，行为不变）；
- 批量 `PartialExportDialog` 不经过该组件（不受影响）。

## 验收标准

1. 单文件结果页选 3 页 → 回到编辑 → 弹窗关闭、画布可见且可拉框。
2. 再次打开弹窗，选择集保持；点「导出选中页」仍返回 202 并进入轮询。
3. `bare` 形态单测：点击触发 `onRequestClose`、下一帧滚动/聚焦编辑画布。
4. inline 形态单测不回归。
5. zh/en 文案一致，不再出现「说明页 1 页」。

## 测试计划

- 扩展 `frontend/src/features/batch/components/issue278-back-to-edit.test.tsx`：新增 bare 契约用例，更新 inline 用例以等待下一帧。
- 运行 `npx vitest run src/features/batch src/features/playground`，再跑全量 `npm test`；`npm run build`、`npm run lint`、`tsc -p tsconfig.test.json --noEmit`。

## 风险

- 关闭弹窗后滚动/聚焦依赖宿主 DOM id `#playground-bbox-editor`（既有契约，测试中已钉）。
- Radix Dialog 关闭后的滚动锁释放时点：用 `requestAnimationFrame` 延后一帧，兜底 `setTimeout`。
