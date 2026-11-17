# Issue#347 设计方案：扫描页 ±90° 方向归一 + 金额紧裁 None 防御

> Issue: [#347 扫描对账单打码覆盖不全](https://github.com/youayou-Lee/LexMask/issues/347)
> 设计评论（含完整取证）：[issuecomment-6953841345](https://github.com/youayou-Lee/LexMask/issues/347#issuecomment-6953841345)
> 验收方案（两段式 + 冷审修订）：[issuecomment-6954180101](https://github.com/youayou-Lee/LexMask/issues/347#issuecomment-6954180101)

## 问题形态（真卷 80 页巡检）

| 形态 | 根因 |
|---|---|
| 少数页整页 0 框（户名/证件号/账号全裸） | 根因①：金额紧裁 `len(None)` 崩溃 → stage 异常 fail-open |
| 偶数页表头整行漏打（同页正文有框） | 根因②：±90° 躺倒页 OCR 退化 |
| 证件号只遮前段、尾段 9 位可见（奇偶页皆有） | 根因②：竖排文本行在横排行字符框假设下被截断 |

## 根因①：`_crop_amount_block_region` None 防御缺失

`ocr_entity_match._crop_amount_block_region`（#308 同签名金额紧裁）把
`_entity_char_box_line_rects(...)` 的返回值直接 `len(rects)`。该函数契约允许返回
`None`（跨行/零宽 rect 收不出可证明跨度时），同文件其余 5 个调用点全部有
None 守卫，唯此处漏。旋转页的竖排行字符几何触发返回 None →
`TypeError: object of type 'NoneType' has no len()` → 异常逃出 `detect_and_draw`
→ vision_service 把 ocr_has stage 异常降级为 warning（fail-open）→ 整页 0 框
照常落库、页照常 completed。

**修复**：`rects is None` ⇒ 按既有契约回退整块罩（never-uncover 语义不变）。

## 根因②：页面 ±90° 交替旋转 + OCR 无方向归一

- 双面扫描正反面进纸方向相反：偶数页内容顺时针躺倒、奇数页逆时针躺倒；PDF 无
  `/Rotate`（全部 rotate=0），渲染出的页图内容横躺。
- OCR 服务端三个方向分类全关（PPStructureV3 与字符 word 引擎
  `use_doc_orientation_classify / use_textline_orientation / use_doc_unwarping=False`）。
- 横躺输入下 PaddleOCR-VL 退化（0 框 / 单个巨型表格 blob / 乱码），表头字段
  （户名/证件号码/账号）读不出或读碎 → NER 无锚点 → 整行漏打。
- 判别信号（10 组真卷纯 OCR 块统计）：横躺页 `vertical_share`（竖排行块占比）
  **0.703–0.733**，转正页 **0.0**；错向（180°）居中；近空白页无旋转信号。

## 修法：OCR 前方向归一 try-and-pick（后端单侧，零新模型）

```
渲染页图（原样）──OCR──► blocks ──评分──┬─ 无旋转签名 ─► 原样继续（好页零开销）
                                        └─ 有旋转签名 ─► rot90/rot270 各一次纯 OCR
                                                          └─ 比较制择优（1.2× 边际）
NER/匹配/正则兜底（胜者空间，只跑一次）
    │
_detect_with_ocr_has 出口：归一化框单点映射回原页坐标
    │
下游（file_store 落库 / 马赛克渲染 / 预览 / 人工框合并）零改动
```

- 新模块 `app/services/vision/ocr_orientation.py`：`score_ocr_blocks`（评分）、
  `choose_orientation`（1.2× 边际择优）、`rotate_image_for_ocr`（PIL 转置与页面
  渲染语义对齐）、`map_normalized_box_to_page/_to_upright`（归一化框 90°/270°
  互逆映射，round-trip 钉死）、`map_bounding_box_to_page`（BoundingBox 出口映射）。
- `ocr_has_vision_service.detect_and_draw`：OCR 段后插 `_normalize_ocr_orientation`
  ——重试只跑纯 OCR（坏页不浪费 NER）；候选 OCR 失败弃权不致命；
  `duration_ms["ocr_orientation"]` = {chosen, retried, scores, signals} 自动流入
  pipeline_status 观测面。
- `vision_service._detect_with_ocr_has`：读 `last_ocr_orientation`；ink 检查改用
  胜者图；归一化框出口映射回原页。

### 方向语义（防呆）

`angle` 与 PyMuPDF `page.set_rotation` 渲染语义对齐：90 = 原页顺时针转 90°
（偶数页转正方向），270 = 逆时针（奇数页）。PIL 的 `ROTATE_*` 是逆时针计数，
故 90° 顺时针 = `Image.Transpose.ROTATE_270`——单测用不对称像素钉死。
归一化框映射（y 向下、origin 左上）：

- 90°：`x_p = y_u；y_p = 1 − x_u − w_u`，宽高互换
- 270°：`x_p = 1 − y_u − h_u；y_p = x_u`，宽高互换

### 被否备选

- **paddlex `use_doc_orientation_classify=True`**：ocr 容器无该模型权重（需镜像
  烘焙 + ModelScope 下载流程）、VL/structure 双引擎回映射行为未验证。留作
  180° 形态成为实证问题时的候选。
- **OCR 容器侧旋转**：碰 GPU 栈部署面，验收成本不值。

### 已知边界（明示不做）

- 180° 倒置页/镜像页：本卷无实证样本；180° 页文字仍水平、不触发本修的重试闸，
  行为与现状一致。
- stage 异常的通用 fail-open 政策不动（消除已知崩溃点即可；政策面属 #263 同族
  风险，另行讨论）。

## 测试

- `tests/test_issue347_ocr_orientation.py`（23 例）：评分器四类夹具（横向正常/
  旋转/巨型 blob/近空白）、映射 round-trip（含真卷数值锚）、PIL 方向语义钉、
  择优与边际回退、`detect_and_draw` 接线钉（好页零开销断言）、出口映射正反向。
- `tests/test_issue347_amount_crop_none_guard.py`（3 例）：None 契约回退、零宽
  字符框几何真实形态、正常单游程紧裁行为不回归（#308 行为保护）。
