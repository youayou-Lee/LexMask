# Issue#325 子集预览打码（不触碰 item 生命周期）

## 背景
Issue#321 的 PR#324 被门⑥ 判定阻断：
- **C-1**：识别期 item=PROCESSING，而打码提交要求 AWAITING_REVIEW；放开状态校验又会与识别 worker 并发写同一 file_store。
- **C-2**：强制 `pdf_scanned` 让文本型 PDF 的实体→框定位失效（子集 0 框却 COMPLETED）。
- **C-3**：子集跑完置 COMPLETED，随后不能全卷重跑/补跑失败页；full 导出在页数相等时把子集产物 copyfile 当整卷。

## 方案
新增**独立的子集预览通道**，不改整卷生命周期：

1. `POST /api/v1/jobs/{job_id}/items/{item_id}/subset-preview`
   请求：`pages`（1-based，必须 ⊆ success ∪ manual_verified）、可选 `bounding_boxes`、可选 `entities`。
2. 服务 `subset_preview_service.create_subset_preview`：
   - 校验非空/范围/是否可导出页/文件类型（仅 pdf、pdf_scanned）；
   - `slice_pdf_pages` 切片 → `remap_boxes_to_subset`；
   - 文本型 PDF 保留 `FileType.PDF`（实体→框定位发生在**切片卷**上，门⑥ C-2）；
   - `Redactor().redact` 产预览 PDF，落 `preview_output_path`；
   - 只通过 `file_store.update_fields` 写 `preview_output_path/processed_pages/page_map/preview_entity_map/preview_redacted_count`，**不动 item/job 状态、不动主产物 `output_path`**。
3. 读取：`GET /files/{id}/page-image?redacted=true&preview=true` 与 `GET /files/{id}/download?redacted=true&preview=true` 读预览产物。
4. 全卷导出守卫：`submit_item_export(kind="full")` 发现 `preview_output_path` 且 `processed_pages < page_count` → 400 `subset_preview_incomplete`，不再把子集当整卷。

## 验收
- 86→54：识别期提交 200；预览只 54 页；`page_map` 原始页号正确；主产物/status 不变。
- 文本型 PDF + 仅实体：切片页像素真实变化（实体在切片卷定位）。
- 空集/越界/未识别页/非 PDF → 400（机读码）。
- 子集后 full 导出被拒；重试/全卷重跑路径不受影响。
- 全卷路径回归零变化。

## 边界
- 暂只支持 PDF（pdf/pdf_scanned）；图片/TXT/DOC 不支持。
- 每页是否“可导出”依赖 item performance 的 success/manual_verified 投影。
- 预览产物独立存放，不做自动清理（由 TTL/后续任务处理）。

## 门⑥ 评审修复（2026-11-16）
- **C-1**：预览写回与识别写共用 `get_file_store_lock()`；payload 仅 preview_* 键。
- **C-2**：落库键改 `preview_page_count`，**不再改写共享 `page_count`**；二次预览可跑。
- **C-3**：full 守卫只在**缺主产物**时拦；全卷打码成功清 preview_*；「预览→全卷重跑→full 导出」有回归钉。
- **C-4**：文本型 PDF 在识别完成（awaiting_review/completed）且无页账时按全卷放行准入。
- **Important**：空集 400（服务层）、零框/零实体 400 `subset_no_regions`、缺源 400 `subset_source_unreadable`、page-image 按 `page_map` 反查原卷页号并回原卷 `X-Page-Count`、下载预览文件名 `redacted_preview_*`。
- 仍挂账（另案）：草稿优先级与导出不一致、预览文件 TTL/清理、路由 response_model/audit、文本 NER 整卷页账（#326 关联）。
