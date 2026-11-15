# Issue#312 扫描件打码模式「映射记录」为空：图片打码链补写已打码映射

- Issue: [#312](https://github.com/youayou-Lee/LexMask/issues/312)
- 分支: `feat/issue312-mask-entity-map`（从 `origin/preview` = `356d901` 切出）
- 关联: #61（文本型 PDF MASK 真打码栅格化路由，本次不改其路由）、#66（预览定位与执行定位同源）、#57（MASK 仅对 PDF/图片有意义）、#275/#276/#278/#280（本轮相邻批次，无耦合）
- 范围: **仅** `backend`；前端零改动（右栏「映射记录」Already 读 `GET /api/v1/files/{id}` 的 `entity_map`）

---

## 1. 背景与现象

用户完成扫描件匿名化后，右侧「映射记录」显示「还没有映射记录」（0 行），但对比预览里能看到整页大面积马赛克。用户判定「预览有结果、记录却为空」自相矛盾。

实证（Issue 正文）：一份 86 页扫描卷 `bounding_boxes` 共 1598 框、全部 `selected=true`、`text` 字段齐全，去重后 **655 个非空原文**；落库快照 `entity_map={}`、`entities=[]`，因此右栏 0 行。

## 2. 根因

图片打码链路与文本替换链路是两条独立链路，但**只有文本链路会写映射**：

| 链路 | 入口 | 是否调用 `RedactionContext.get_replacement` | entity_map |
|---|---|---|---|
| 文本替换（DOCX / TXT / 文本型 PDF 回转 / 文本型 PDF 原位回退） | `text_redactor.py` `_redact_docx` / `_redact_txt` / `_redact_pdf_via_docx` | 是（逐实体，含 coref 复用与公共机构保留） | 原文 → 替换词 |
| 图片打码（`PDF_SCANNED` / `IMAGE`，含文本型 PDF 的 MASK 栅格化路径） | `redaction/image_redactor.py` `_redact_image` | **否**（只做像素遮挡） | 恒为空 |

关键事实：

1. `redact()`（`redactor.py:190-206`）图片分支只把 `selected_boxes + mask_boxes` 交给 `_redact_image`，`context` 未被传入，`context.entity_map` 从头到尾没被写过；
2. `_redact_image`（`redaction/image_redactor.py:170-208`）内部用纯函数 `prepare_image_redaction(boxes, config)`（`image_redactor.py:139-166`）算出**真正参与遮挡的框集合** `safe_boxes`（`selected` 过滤 + 归一化裁剪 + 丢弃零面积），返回 `len(safe_boxes)` 作为 `redacted_count`，然后把 `safe_boxes` 交给 `VisionService.apply_redaction` 做像素操作；
3. `redact()` 末尾无条件 `return {... "entity_map": context.entity_map ...}`（`redactor.py:237-245`），于是图片链路必然返回 `{}`；
4. 编排层三个写点**原样透传**该值（`redaction_orchestrator.py:294` 文件快照、`:305` `redaction_history` 版本条目、`:318` 响应 `RedactionResult`），所以空 map 一路落到底——写点本身没有缺陷，缺的是**产出端**。

即：图片链路的「打码事实」在像素层发生过，但从未被表达成数据记录。

## 3. 方案

### 3.1 落点：图片遮挡完成后，用实际参与遮挡的框文本重建映射

在 `redaction/image_redactor.py` 增加纯函数与常量：

```python
IMAGE_REDACTED_LABEL = "已打码"

def build_image_entity_map(safe_boxes: list[BoundingBox]) -> dict[str, str]:
    """已遮挡框 → 映射{原文: 已打码}。去重由 dict 键天然承担；空白原文跳过。"""
    mapping: dict[str, str] = {}
    for box in safe_boxes:
        text = (box.text or "").strip()
        if not text or not box.selected:
            continue
        mapping[text] = IMAGE_REDACTED_LABEL
    return mapping
```

`_redact_image` 拿到 `safe_boxes` 后（与 `redacted_count` **同源**）写入调用方传入的出参：

```python
async def _redact_image(
    self, file_path, file_type, selected_boxes, output_path, config,
    progress_cb=None, entity_map_out: dict[str, str] | None = None,
) -> int:
    safe_boxes, method, strength, fill_color = prepare_image_redaction(selected_boxes, config)
    if entity_map_out is not None:
        entity_map_out.update(build_image_entity_map(safe_boxes))
    ...
    return len(safe_boxes)          # 语义不变
```

`redact()` 图片分支只多传一个关键字参数：`entity_map_out=context.entity_map`。**返回类型仍为 `int`**，`redacted_count` 语义（= 参与遮挡的框数）逐字节不变。

### 3.2 为什么用出参而不是改返回值/新建数据源

- 改返回值为 `(count, map)` 会打断 `redacted_count` 的既有契约与调用方（编排层 `int(result.get("redacted_count", 0))`），且需多处改造；
- 出参让「map 与 count 同源同一次计算」，不引入第二次 `prepare_image_redaction` 调用（重复裁剪会重复告警、且未来两侧漂移）；
- 不新增前端数据源：`entity_map` 沿用 `GET /api/v1/files/{id}` 快照（`file_management_service` 的 `entity_map` 回退分支只在旧记录缺 `entities`/`bounding_boxes` 时生效，本改动不改变识别项计数）。

### 3.3 覆盖的链路形态（「以实际遮挡框为准」）

| 形态 | 结果 map 的来源 |
|---|---|
| `IMAGE` 直接打码 | `safe_boxes` |
| `PDF_SCANNED`（识别线判定扫描件） | 逐页 `safe_boxes`（跨页合并去重） |
| 文本型 PDF + MASK（实体定位成功 → 栅格化） | `selected_boxes + mask_boxes` 经 `prepare_image_redaction` 过滤后 |
| 文本型 PDF + 替换模式 + 用户拉过框（`bbox>0` → 强制栅格化） | 同上（该路由产物本就是像素遮挡，记录「已打码」是事实口径） |
| MASK 定位失败 → 整卷回退文本链路 | **不进图片链路**，map 仍为文本掩码词（既有行为不变） |
| 公共机构保留（KEEP/ORG） | 图片链路无保留策略：`selected` 框一律遮挡，故一律入表 |

已排除的框（`selected=false`、裁剪后零面积）不入表——与 `redacted_count` 同口径。

### 3.4 安全性（不改动、本单只钉不改的三条）

1. **restore 只读用户上传映射**：`POST /api/v1/vlmd/restore` 的唯一输入是请求体 `mapping`（`api/vlmd.py:41-58` → `normalize_mapping(body.mapping)`），全链不读 `file_store`/`entity_map`；
2. **化名 CSV 只在替代模式出现**：CSV 由前端 `use-playground.ts:1137-1153` 用**执行响应** map 现生成，且受 `confirmedPseudonymMap`（仅替换模式提交成功后置位，`use-playground.ts:135/658`）门控；mask 模式 map 值恒为哨兵 `已打码`，不含任何化名/占位符形态；
3. **不新增明文暴露面**：不写日志、不新增导出字段，落点沿用既有 `file_store.entity_map` 与 `redaction_history`（文本链路本来就落同一字段）。

## 4. 影响面

| 面 | 影响 |
|---|---|
| `RedactionResult.entity_map` | 图片链路从 `{}` 变为 `{原文: "已打码"}`（**唯一用户可见变化**） |
| `file_store` 快照 + `redaction_history[].entity_map` | 同上（同一数据源，无新增字段） |
| `redacted_count` / `replacement_count` / 识别项计数 | 不变 |
| 导出后自检 `_verify_export_residuals` | 不变：`verify_types` 只含文本型 PDF/DOCX/DOC/DOCX/TXT，图片链路本就不进自检 |
| 文本替换链路（DOCX/TXT/文本 PDF 回转） | **零改动**：`text_redactor.py` 一字不动 |
| `POST /redaction/preview-map`（预览口径） | 不变：仍是替换词预览，不在本单范围 |
| 前端 | 零改动 |
| 化名 CSV / `POST /vlmd/restore` | 不受影响（见 3.4，测试钉住） |

## 5. 验收标准

- **AC1 映射非空且去重**：扫描件（`PDF_SCANNED`）与单图（`IMAGE`）打码后 `entity_map` 非空；键 = 参与遮挡框的**去重非空原文**；值恒为 `已打码`；`len(map) ≤ redacted_count`。
- **AC2 空文本过滤**：`text` 为 `None`/`""`/`"   "`（含全角空格）的框不入表。
- **AC3 未参与遮挡的框不入表**：`selected=false` 的框不入表；`redacted_count` 与 map 同口径。
- **AC4 文本链路不变**：替换模式（smart/pseudonym/mask 文本回退）`entity_map` 仍是原文→替换词，`已打码` 不出现；文本链路源码零改动。
- **AC5 落库一致性**：`execute_redaction` 后 `file_store.info["entity_map"]` 与 `info["redaction_history"][-1]["entity_map"]` 与红色器返回值**同一 map**（刷新/任务中心回场一致，不依赖 live 会话内存）。
- **AC6 安全性钉**：mask map 全值 = `已打码`（无化名/占位符形态）；restore 端点只消费请求体 `mapping`（结构钉 + 行为钉），mask map 不参与 restore。
- **AC7 回归**：`tests/` 全量 0 新增失败（`redactor`/`orchestrator`/`image` 相关全文件必跑）。

## 6. 测试计划（TDD：先红后绿）

新增 `backend/tests/test_issue312_mask_entity_map.py`：

| 用例 | 钉什么 | 红性来源（实现前） |
|---|---|---|
| R1 `test_scanned_mask_entity_map_nonempty_and_dedup` | 3 框含 2 处重复文本 → 2 键、值全哨兵、`redacted_count=3` | map 为空 |
| R2 `test_image_mask_entity_map_from_boxes` | `IMAGE` 单图路径 | map 为空 |
| R3 `test_mask_entity_map_skips_blank_text` | `None`/`""`/`"   "`/`"　"` 全跳过 | map 为空 |
| R4 `test_mask_entity_map_excludes_unselected_boxes` | `selected=false` 不入表且不计入 count | map 为空 |
| R5 `test_text_pdf_mask_rasterized_entity_map` | 文本型 PDF MASK 栅格化路径（`mask_boxes` 也入表）+ 原文不在成品文本层 | map 为空 |
| R6 `test_text_path_entity_map_unchanged` | 文本 PDF 替换模式 map = 替换词，逐键断言且不含哨兵 | 已是绿（守不变） |
| R7 `test_redaction_history_carries_same_map`（编排层，真 Redactor + stub store） | 快照与 `redaction_history[-1]` 同一 map | map 为空 |
| R8 `test_restore_does_not_consume_mask_map` | mask map 值无化名形态；`restore_text` 只吃 body.mapping（patch `normalize_mapping` 抓参 + 结构钉） | 已是绿（守不变） |
| R9 `test_mask_pass_does_not_build_pseudonym_csv_shape` | 反向对照：pseudonym 模式 map 值≠哨兵（证明 CSV 门控源未被污染） | 已是绿（守不变） |

命令与门限：
- `cd backend && python -m pytest tests/ -k "redact or entity_map or image"`（本单主闸）
- 受影响全文件：`tests/test_pdf_mode_routing.py tests/test_issue238_progress_reporting.py tests/test_mask_mode_gating.py tests/test_issue312_mask_entity_map.py`
- 全量：`python -m pytest tests/`（零新增失败；失败集合与基线 `356d901` 逐 node-id 对拍）

## 7. 风险与边界

1. **视觉类框的 label 文本入表**（`印章`/`签字`/`指纹`/`official_seal`）：这类框的 `text` 是标签而非被替换原文，入表后右栏会出现「印章 → 已打码」这类行。取舍：Issue 要求「以实际遮挡框为准」且不新增过滤维度（过滤会让 655 行口径对不上），语义解释为「该框已有遮挡」。若后续要按 `source`/`type` 分流，另立 Issue。
2. **页号越界框**（`page > 页数`）：`VisionService._redact_pdf` 只遮挡实际存在的页，越界框从未真正上像素，但**既有** `redacted_count` 一直把它们计入。本单让 map 与 count 同源，故两者口径一致、不为越界框单独收紧（收紧 count 属行为变更，另立 Issue）。
3. **右栏 655 行渲染性能/可读性**：Issue 边界明确「另立 UI 聚合 Issue」，本单不做前端。
4. **历史存量记录**：已落库的 `entity_map={}` 不会自动回填；需重跑执行才会出现映射记录（Issue 验收也以重跑为准）。
5. **落点仍含原文**：`file_store` 与 `redaction_history` 本就在存原文→替换词（文本链路同样），本次不新增字段、不新增日志、不新增导出面。