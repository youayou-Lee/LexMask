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
  - 在下一帧滚动到 `#playground-bbox-editor`，并尝试把焦点交给画布（优先 `[role="application"][tabindex]`，兜底 `canvas[tabindex]` / 首个 `tabindex=0` 元素），保证键盘用户也回到编辑。
- 宿主抽成 `PlaygroundExportDialog`（`playground-page.tsx` 使用），由它把 `onRequestClose` 接到 `setExportPagesOpen(false)`；result 阶段（`playground-result.tsx`）的导出块传 `onRequestClose={onBackToEdit}`。
- 选择集：`use-item-export` 原 `selectionCache` 只在 `!enabled` 分支写入，而单文件弹窗整棵子树卸载时 `enabled` 恒 true ⇒ 从不落 cache。修复为 `selectedRef` + 以 `(job,item)` 为 key 的卸载 cleanup 写入；重开按上次选择恢复（含空集语义：空集仍像既有行为一样回落默认预选，CHANGELOG 已如实描述）。
- 修正 zh/en 的 `singleExportHint` / `exportSummary` 为 zip 口径（脱敏 PDF + 映射清单 + 可选 说明.txt）。

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

## 门⑥ 评审修复（2026-11-15 晚）

- **I-1 选择集卸载写入**：`use-item-export.ts` 加 `selectedRef` 与 `useEffect` 卸载 cleanup；`!enabled` 分支同读 ref。用例：`playground-page-export.test.tsx`「取消 1、2 → 卸载 → 重挂仍只选 3、5」。
- **I-2 宿主接线判据**：新增 `playground-export-dialog.tsx`（真 Radix Dialog + `onCloseAutoFocus` 仅回到编辑路径 preventDefault）；`playground-export-dialog.test.tsx` 断言点 `export-back-to-edit` 后内容卸载。
- **I-3 result 阶段判据**：新增 `playground-result-export-wiring.test.tsx`，真渲染 `PlaygroundResult`（mock 重子组件），点导出块的「回到编辑」断言 `onBackToEdit` 调用 1 次；删除 `onRequestClose={onBackToEdit}` 该用例必红（已实测）。
- **M-4 夹具**：`issue278-back-to-edit.test.tsx` 画布夹具改 `div[role=application][tabindex=0]`；**M-5** 改 rAF 等待。
