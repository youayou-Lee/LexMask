# Issue#212 供料模式对表格型文档静默零识别：方案与验收标准

- 关联 Issue：https://github.com/youayou-Lee/LexMask/issues/212
- 分支：`fix/issue212-agentmd-v2-table`（自 `origin/preview` 切出，BASE=`686c4c0`）
- 日期：2026-10-29
- 版本：v3（①.5 冷审 round 1 全量、round 2 全量、round 3 窄复核均已处置，处置表见 §7 / §7.2 / §7.3）

## 0. 需求确认（来自 Issue 正文，用户原话结论）

用户原话：**「提Issue,描述清楚这个问题，确保以后不能出现这个问题了」**。

1. 不是"这次修好"，而是**结构性防止再发生**：任何一侧表示（MinerU v1/v2、字段改名、文本层缺失）出问题都必须**可见**，不得静默归零；
2. 用户看到"什么都没识别出来"这种无差别结果必须被拆开：解析出 0 字符 / 识别到 0 实体 / 识别到 N 实体，各是不同状态；
3. 空映射**不得**在用户不知情时出稿（= 交付未脱敏原文）。

## 1. 现象与复现（实测锚定）

真实触发（生产实例，2026-10-29）：80 页纯扫描件「财产查询反馈信息表（某申请人）」**17:04:34** 上传供料模式（`/api/v1/agent-md/upload`），MinerU 3.4.5 任务 `41ddc94f…` 于 **17:11:52** 完成，随后的 NER 请求 `c43152ce…` 只收到 `6 lines, 1 chunks, 143 chars`，HaS/Regex 均 0 实体，Kept 0。同账号同日 3 页文本型文书（16:55 上传）正常识别 52 实体。

同一份 MinerU 产物用生产依赖实测：

| 表示 | segs | 正文字符 | warns |
|---|---|---|---|
| `*_content_list.json`（v1，扁平，表格在 `table_body`） | 33 | **187,119** | 0 |
| `*_content_list_v2.json`（v2，管线实际消费，表格在 `content["html"]`） | 6 | **138** | 8（`unhandled block type: title`） |

v2 块型统计（80 页 173 块）：`table 79 / page_number 80 / paragraph 6 / title 8` ⇒ v2/v1 正文字符比 = **0.0007**。

两个数字口径不同，勿混用：**138** 是 v2 经 `clean_segments` 清洗后的正文（6 段正文，含段落文本），**143** 是后端 NER 请求实际收到的载荷字符数（138 正文 + 分块拼接分隔符，见生产日志 `6 lines, 1 chunks, 143 chars`）。

合成夹具复现（`backend/tests/fixtures/agent_md/v1_table_scan.json` / `v2_table_scan.json`；2 页：标题块 + 两个带 `colspan` 的表 + 段落 + 页码，全部脱敏假数据）：

```
$ python3 -c '...normalize_content_list + clean_segments...'   # 未修代码
v2_table_scan: blocks=6 segs=1 chars=33  warns=['unhandled block type: title']
v1_table_scan: blocks=6 segs=4 chars=258 warns=[]
```

同一内容：**v2 路径只剩 1 段 33 字符**（两个表全丢、标题块丢），**v1 路径 4 段 258 字符**。

## 2. 根因（按符号定位）

1. `agent_md_adapter._V2_TYPE_TO_V1` 未映射 v2 `title` → `text`，`title_content` 走 `unhandled block type` 丢弃（真实案卷里它装着机构名）；
2. `agent_md_adapter._v2_block_to_flat` 只上浮 `sub_type / img_path / table_body`；MinerU 3.4.5 的 v2 表格正文键是 `content["html"]`、存在 `table_caption`/`table_footnote`，没有 `table_body` ⇒ 表格扁平化后无正文；
3. `agent_md_adapter.clean_segments` 对空 `table_body` 既不产 seg 也**不产 warning**（静默丢弃）；
4. `mineru_parse_client._fetch_result` 只解 `*_content_list_v2.json`，zip 内同时存在的 v1 从未被读 ⇒ 没有第二来源对冲字段漂移。

后果：以表格为主的文档（银行流水、财产查询反馈、对账单、协执清单）必然 0 实体零提示；空映射可「确认」出稿 = 未脱敏原文。#197 `agent_batch` 复用同一适配器/客户端，合并后自动继承。

## 3. 改动点

### 3.1 适配层 `backend/app/services/agent_md_adapter.py`

- `_V2_TYPE_TO_V1` 增 `title → text`；
- `_v2_block_to_flat`：`type == "table"` 且缺 `table_body` 时从 `content["html"]` 上浮；`table_caption` / `table_footnote`（v2 为 `[{type:text,content:…}]` 数组）拼接成字符串上浮为同名扁平键（已存在则不覆写）；
- `clean_segments` 返回**三段组** `(segs, warns, stats)`，`stats` 为新增 `@dataclass CleanStats`：
  - `empty_table_blocks: int`、`dropped_block_types: dict[str, int]`；
  - 表格正文 = `table_to_text(table_body)`，再把非空 caption / footnote 各追加一行（`" | "` 拼接风格保持与 body 一致）；
  - 空表格块计入 `stats.empty_table_blocks` 并按 `empty table block: page=N` 进 warns；未知 type 计入 `stats.dropped_block_types[type]` 并保留 `unhandled block type: X` warn；
  - 不改变 v1 `table_body` 既有行为（既有测试必须零改动通过，除非仅签名解包调整）。

### 3.2 解析客户端 `backend/app/services/mineru_parse_client.py`

- `MineruResult` 增 `content_list_v1: list[dict]`（默认空，向后兼容）；
- `_fetch_result` 同时解 `*_content_list.json`（先判 v2 后缀再判 v1，避免 v1 匹配吞掉 v2）；缺 v1 只 `logger.warning`，不失败。

### 3.3 管线双表示守卫 `backend/app/services/agent_md_pipeline_service.py`

`_run_stage1` 中两路都算：

```python
# stats 是 _choose_representation 内部新建的 out 参数对象，不是 clean_segments 的返回值
v2_stats = CleanStats(); v1_stats = CleanStats()
v2_segs, _ = clean_segments(normalize_content_list(result.content_list_v2), stats=v2_stats)
v1_segs, _ = clean_segments(normalize_content_list(result.content_list_v1), stats=v1_stats)
# clean_segments 返回 (segs, warns)，计数写在传入的 stats 上
v2_chars, v1_chars = 正文（text/table）字符数；GUARD_RATIO = 0.95
```

判据（按顺序，先命中者生效；`short = v2_chars < GUARD_RATIO * v1_chars`）：

| # | 条件 | 选择 | 告警码 |
|---|---|---|---|
| 1 | `v1` 缺失（`content_list_v1` 为空） | v2 | `dual_representation_unavailable` |
| 2 | `v1_chars == 0 且 v2_chars == 0` | **无**（任务 FAILED） | 无（`message` 带 `parse_no_text`） |
| 3 | `v1_chars == 0`（v1 来了但无正文） | v2 | `shortfall winner=content_list_v2 v1=0 v2=<m>` |
| 4 | `v2_chars == 0`（v2 整体归零/v2 缺失） | **v1** | `shortfall winner=content_list_v1 v1=<n> v2=0` |
| 5 | `short` 且 `v2_stats.empty_table_blocks > 0`（v2 丢表） | **v1** | `shortfall winner=content_list_v1 v1=<n> v2=<m>` |
| 6 | `short` 且 v2 无空表（v1 被水印/重复放大撑大） | v2 | `shortfall winner=content_list_v2 v1=<n> v2=<m>` |
| 7 | `v1_chars < GUARD_RATIO * v2_chars`（v1 侧偏少） | v2 | `shortfall winner=content_list_v2 v1=<n> v2=<m>` |
| 8 | 其余（两路接近） | v2 | 无 |

- 两路皆 0 ⇒ `task.state = FAILED`、`task.message = "parse_no_text: …"`，**不得** MAPPING_READY（禁"0 映射静默就绪"）；
- `task.dropped_blocks`（新字段）= **v2 主表示的丢弃计数** + 命中来源：
  `{"empty_table": v2_stats.empty_table_blocks, "unhandled": dict(v2_stats.dropped_block_types), "source": "content_list_v1"|"content_list_v2"}`
  （口径说明：v2 是本次事故的产出侧，也是字段漂移的观测面；即使因守卫回落 v1，`empty_table` 仍报 v2 的 79，配合 `source` 让"丢表并已回退"在 API 上一眼可见）；
- `task.chars`（新字段）= 选中表示正文（text/table）字符数；`task.entity_types_used` = 本次解析出的实体类型列表（零实体闸的豁免依据）。

### 3.4 接口 `backend/app/api/agent_md.py`

- `status` 增 `warnings`（≤20 条原文）、`warnings_total`、`dropped_blocks`（恒存在三键：`empty_table`/`unhandled`/`source`）、`chars`；
- **owner 校验**：`status/mapping/confirm/artifacts` 一律比对 `task.owner_id == owner_id`，不符按 404 `TASK_NOT_FOUND`（现状完全不校验；新增 warnings/dropped_blocks 含图片路径等内部信息，泄露面扩大）；
- `confirm`：当 `not task.mapping` **且** `task.entity_types_used` 非空（用户停用全部实体类型时豁免，避免误报）且请求体未带 `acknowledge_zero_entities: true` ⇒ `409 ZERO_ENTITIES_NOT_ACKNOWLEDGED`，**不产出任何产物**；带 ack 则放行并 `audit_log("agent_md_confirm_zero_entities", "agent_md_task", task_id, user=owner_id, detail={pages_total, chars, entity_types_used, warnings_total})`（审计写失败只告警不阻断）。

### 3.5 前端 `frontend/src/features/agent-md/`

- `api.ts`：`StatusDto` 增 `warnings?: string[]`、`warnings_total?: number`、`dropped_blocks?: {empty_table?: number; unhandled?: Record<string, number>; source?: string}`；
- `lib/agent-md-flow.ts`：新增纯函数 `needsZeroEntityAck(items) => items.length === 0`；
- `step-review.tsx`：空映射时置顶告警条（i18n zh/en 两条文案），「确认出稿」在 `needsZeroEntityAck(items)` 为真时 disabled，需勾选「我已知晓本次未识别到任何敏感实体」后才可点，提交带 `acknowledge_zero_entities: true`；
- `step-processing.tsx` / `step-result.tsx`：展示 `warnings`（折叠）+ `dropped_blocks` 聚合；
- i18n：`agentMd.zeroEntitiesWarning` / `zeroEntitiesAck` / `droppedBlocksHint`（zh+en）。

### 3.6 文档

`CHANGELOG.md` 当天条目（fixed 段）；本文档随 PR 入库；Issue#212 正文互链。

## 4. 验收标准（Definition of Done）

判据原则：每条都能被"不修代码"证伪；期望值用**字面常量**（不由被测实现产出）；阈值给数值与来源；环境不可达的条目单列并请用户接受（§4.1）。

**AC0 红性先证（本地 pytest）**
新增用例在**未修代码**上必须判红（红跑输出贴 PR）：
- 适配层：`v2_table_scan.json` → 正文含 `6222020200012345678`、`张某某`、`某市某区人民法院财产查询反馈信息表`；未修实测 `segs=1/chars=33` ⇒ 必红；
- 管线：fake mineru 返回同内容 v2+v1 → `task.segs` 正文含上述串；未修实测 33 字符 ⇒ 必红。

**AC1 字面 golden（v2 路径正文，`v2_table_scan.json`）**
逐段等于字面常量（`GOLDEN_SEGS`，抄自 v1 路径未修实测输出，v1 分支零改动 ⇒ 不能靠"改坏 v1 对齐 v2"通过）：
`["某市某区人民法院财产查询反馈信息表", <表1 全文>, "上述财产查询结果由协执单位于 2026 年 10 月 1 日反馈。", <表2 全文>]`，`chars == 258`；warns 中**不含** `unhandled block type: title`；`stats.dropped_block_types == {}`。
（AC2 用**独立夹具** `v2_table_caption.json`，不混入本夹具，避免 258 与用例互相打架。）

**AC2 caption/footnote 上浮（v2，独立夹具 `v2_table_caption.json`）**
渲染文本必须同时包含 `表1 财产查询结果`、`数据来源：协执单位`，且顺序 = 单元格文本 → caption → footnote。未修代码不产出这两串 ⇒ 必红。

**AC3 双表示守卫（管线单测，fake 依赖）**
a) v2 丢表（表格无 html）+ v1 完整 ⇒ 选中 v1 正文（含 `6222020200012345678`），warns 含 `dual_representation_shortfall` 且 `winner=content_list_v1`；`dropped_blocks == {"empty_table": 1, "unhandled": {}, "source": "content_list_v1"}`；
b) 水印放大：v1 正文 = `重复水印文字`（6 字）×500、v2 完整无空表 ⇒ 正文来自 v2（`task.chars < 200`，且不含 `重复水印文字`），warns 含**字面**串 `dual_representation_shortfall: winner=content_list_v2 v1=3033 v2=33`（v1 = 表格 33 + 水印 3000；不用 `\d+` 正则——那近乎恒真）；
c) v1 缺失 ⇒ 用 v2，warns 含 `dual_representation_unavailable`；
d) 两路皆空（都有块、表格正文为空）⇒ `state == FAILED` 且 `message.startswith("parse_no_text")`，`mapping == []`；
e) **v2 缺失、v1 完整**（`content_list_v2=[]`）⇒ 选中 v1 正文，warns 含 `winner=content_list_v1 ... v2=0`；
f) 两路接近（v1=v2 正文）⇒ 选 v2、**不产生** shortfall/unavailable 告警（防"永远告警"）；
g) **第 3 行**：v1 存在但正文全空（`table_body=""`）、v2 完整 ⇒ 选 v2，warns 含字面 `... winner=content_list_v2 v1=0 v2=33`，且**不得**出现 `dual_representation_unavailable`（空正文 ≠ 缺失）；
h) **第 7 行**：v1 明显少于 v2（v1 仅 2 字文本段，v2 33 字）⇒ 选 v2，warns 含字面 `... winner=content_list_v2 v1=2 v2=33`；
i) **并发不串味**：两个任务在不同线程各自跑 1 个 service（一丢表、一正常）⇒ 各自 `dropped_blocks` 与 `warnings` 相互独立（`empty_table` 分别为 1 / 0，source 分别为 v1 / v2），后完成的那个不得覆写前者。
（未修代码：a/d/e 正文或状态不对 ⇒ 红；b/c/f/g/h/i 缺该码/多该码 ⇒ 红。）

**AC4 丢弃可观测（结构化计数，两向断言）**
- 管线：80 页同形状夹具（79 表/8 title/80 页码，测试内生成，形状取自实测）缺 html + v1 完整 ⇒ `task.dropped_blocks["empty_table"] == 79`、`source == "content_list_v1"`；
- 管线：同形状带 html ⇒ `dropped_blocks == {"empty_table": 0, "unhandled": {}, "source": "content_list_v2"}`；
- API：把上述 79 的 task 经 fake 管线返回 ⇒ `GET status` 的 `dropped_blocks.empty_table == 79`（API 级**非零**例，堵"常量归一蒙过"）；未实现时该键缺失 ⇒ 红；
- API：无丢弃的 task ⇒ 显式 `{"empty_table": 0, "unhandled": {}, "source": ...}`（不是键缺失）；
- API：21 条 warnings ⇒ `warnings_total == 21`、`len(warnings) == 20`。

**AC5 零实体可见（安全侧）**
- **前置** `state == mapping_ready`，`mapping == []`，`entity_types_used` 非空、未带 ack ⇒ 409 且 `error_code == "ZERO_ENTITIES_NOT_ACKNOWLEDGED"`（不用状态码单独判：`_require_ready` 自身也回 409）⇒ `outputs` 目录无新增文件、`artifacts/{kind}` 404、`confirm_calls == 0`；
- 带 ack ⇒ 200 出稿，审计一条 `agent_md_confirm_zero_entities`，`detail.chars`/`pages_total` 与任务一致、`user` = 请求者；
- 豁免：`entity_types_used == []`（停用全部类型）且 `mapping == []`、无 ack ⇒ 200（不误报）；
- **正向对照**：`mapping` 非空、无 ack ⇒ 200 且 `output_file_id` 有值（防"所有 confirm 都要 ack"也能过的假绿）；
- owner：他人 owner 访问 status/mapping/confirm/artifacts ⇒ 404 且 `confirm_calls == 0`；
- 前端：`step-review` 渲染测试（items=[] ⇒ 告警条可见 + 确认按钮 disabled；勾选后 enabled）+ 提交 payload 含 `acknowledge_zero_entities: true`（组件级，非仅 helper 单测）。

**AC6 云端端到端（门④，真实案卷输入）**
云实例**隔离端口**（不动 8000 生产进程）跑本分支代码：
- 修复后 80 页真件：`state == mapping_ready`，`items` 数 ≥ **绝对下限**（取实测：v1 直取同文件跑出的实体数记为 N0，PR 中写死 N0 与 ≤±15% 容差；同时要求 ≥ 0.9×N0），映射项覆盖 `PERSON / ID_CARD / BANK_CARD` 各 ≥1；
- **探针独立性与口径**：探针脚本只读 v1 产物（`clean_segments` 的 v1 路径 + 同一 NER，允许重跑 3 次取中位数），实体按 `(text, type)` 去重后计数；它和被修路径共享 NER 抖动，因此另设 N0 绝对下限兜底（"探针被一起改坏"时下限仍红）；
- **红性对照（改为可达形态）**：preview 代码无 `dropped_blocks` 字段，故不写"status 读到 79"；改为 preview 适配器对**同一 v1/v2 产物离线回放**，断言 `v2 = 6 segs / 138 chars`（修前事故形态），并附生产日志 `6 lines, 1 chunks, 143 chars` + `Kept 0 entities` 作旁证；
- 真件上云方式：仅测试期 `scp` 到实例临时目录 + 隔离端口测试数据目录，跑完 `shred`/删除，不写生产 `uploads/`、不入库、不进 commit（步骤写进私有验收文档）；
- 限时：`T` = **受测 run 之前**在云隔离端口对同一文件独立预跑一次的实测耗时（不得取受测 run 自身耗时，否则自指恒真），PR 写死 `T` 的值；判据 `≤ 1.5 × T`（可判定）。
- 并发/串味：本地已由 AC3i 覆盖（引擎 `_lock` 全局串行，per-run stats）；云端不重复造并发。

**AC6 实测回填（2026-10-29，云实例隔离端口 8100，HEAD `6e65486`）**
- `T = 733.1s`（预跑 run1：上传→`mapping_ready`，task `70c23624…`；判决 run2 要求 ≤ **1099.7s**）；
- `N0 = 495`（事故原始产物 zip 复放：v1 直取 + 同一 NER，3 次跑均 `4093` 原始 / **`495`** 去重；明细 `CASE_NUMBER 261 / ORG 141 / BIRTH_DATE 44 / BANK_CARD 14 / PERSON 10 / ID_CARD 9 / ADDRESS 9 / PHONE 5 / LICENSE_PLATE 2`）；
- 判决容差：`items` ∈ **[446, 570]**（≥0.9×N0 且 ±15%）；
- run1 实测 `items=494`、`PERSON 10 / ID_CARD 9 / BANK_CARD 14` ①三条类别均 ≥1 ②落在容差带内；
- 红性对照实测：preview 未修代码对同一 v2 产物 → **6 segs / 138 chars**（对齐生产 `6 lines/1 chunks/143 chars`，143=138+分块分隔符）；分支代码对同一产物 → `v2 = 187,119 chars`（`empty_table=60`、`unhandled={}`，title 已归位）、`v1 = 33 segs / 187,119 chars`，守卫留在 v2 且不告警（两路一致）；
- 残留面（如实记账）：事故产物 79 张表里有 **60 张 MinerU 只给截图无 html**，修复只保证"有 html 的表格不再丢"+"空表逐张可见告警"，**不能凭空恢复截图表格的文字**；该残留经 `dropped_blocks.empty_table` 与 `empty table block: page=N` 告警在 API/前端可见，不在本 Issue 静默。

**AC7 回归与门禁**
- 后端 `pytest tests/`：failed 集合与基线（preview 同机同命令）**完全相同**（`comm -3` 对比名单）；
- 前端 `npm run build && npm test && npm run lint` 全绿；
- 变异自证（各自回退必须变红，留档）：①去掉 `content["html"]` 上浮 → AC0/AC1 红；②去掉 `title→text` → AC1 红；③去掉 caption/footnote 拼接 → AC2 红；④守卫恒取 v2 → AC3a/e 红；⑤去掉 `parse_no_text` 失败态 → AC3d 红；⑥去掉 confirm 的 ack 闸 → AC5 红；⑦去掉 v1 分支（只算 v2）→ AC3b/c/f 红（b 会误报 winner=v1）。

### 4.1 不可达环境条目（PR 显式声明，需用户接受）

1. `docker nginx` 静态托管链与云实例只差前端镜像构建产物，本次未实测；替代证据 = 前端 build/lint 绿 + 组件渲染测试；
2. 浏览器级端到端（真点勾选再 confirm）未覆盖；替代证据 = 组件渲染测试 + 云隔离端口 API 走查；
3. 本地无 MinerU/GPU，真件 e2e 仅云隔离端口可达；
4. AC6 的 `T` 与 `N0` **已回填**（`T=733.1s` 预跑 run1、`N0=495` v1 直取探针 3 次取中位，见 AC6 末段）；判决 run2 只需与预跑不同一次、且满足时限与容差。

## 5. 验收方案（谁验收）

**人工验收**（从严）：端到端输入是真实案卷，且"识别结果是否正确"含业务判断，按门禁 §4 归人工验收类。
- AI 先跑完 ③④ 并贴证据到 PR；
- 操作文档：`LexMask-workspace/docs/acceptance/手动验收-issue212-供料模式表格识别.md`（不入库：SSH 隧道命令、账号、逐步操作与预期结果）；
- 用户验收通过并放行 → 进 ⑥ 独立评审；无 Critical/Important → 汇报后合并（merge 前用户二次确认）。

## 6. 非目标

- 不改 MinerU 服务本身（版本/参数）；
- 不做表格结构语义化（合并单元格/坐标）；
- 不动 `agent_batch`（#197 未合并；本分支只在适配器/客户端做向后兼容的增字段与新增分支，PR 显式声明「批量通道复用同一适配器，合并即继承本修复」，不写"若已合并则验证"的悬空条件）。

## 7. ①.5 冷审 round 1 处置（留痕）

| # | 冷审意见 | 处置 |
|---|---|---|
| 1 | caption/footnote 缺判据 | 采纳 → §3.1 + AC2 |
| 2 | 未断言 `unhandled block type: title` 消失 | 采纳 → AC1 |
| 3 | v1 缺失也应告警（原 AC2③ 方向错且恒真） | 采纳 → `dual_representation_unavailable` + AC3c |
| 4 | Issue AC2 要求 姓名/身份证号/银行账号 三类覆盖 | 采纳 → AC6（默认实体类型含 PERSON/ID_CARD/BANK_CARD，已核） |
| 5 | 阈值无实测来源；0.9 vs Issue 的 <5% | 采纳 → 统一 `GUARD_RATIO=0.95`，来源=修前实测 0.0007/0.128 与修后目标 1.0 |
| 6 | 缺 docker nginx / 本地 compose 链 | 部分采纳 → §4「部署链路声明」显式划边界，不伪造未实测结论 |
| 7 | 锚点漂移 16:04 → 17:04:34 | 采纳 → §1 已改 |
| 8 | AC5「warnings 不存在」类恒真 | 采纳 → AC4 改结构化 `dropped_blocks`（键恒存在、显式 0） |
| 9 | 红性对照 items==0 是假红 | 采纳 → AC6 增加 `state==mapping_ready` 且 `empty_table==79` |
| 10 | AC1「两路互比」同源自证 | 采纳 → 改字面 golden，v1 分支零改动 |
| 11 | 前端只测 helper 无接线判据 | 采纳 → AC5 组件级渲染 + payload 断言 |
| 12 | items≥20 弱判据 | 采纳 → 改「≥0.9 × v1 直取探针实体数」 |
| 13 | 冻结真实 v2 产物夹具 | 部分采纳 → 用**合成**同形状夹具（79/8/80）替代真件（PII 不得入库），形状数字取自实测 |
| 14 | 水印放大夹具 | 采纳 → AC3b |
| 15 | 整缺 v2 需显式失败 | 采纳（等价形态）→ 双路皆空 `parse_no_text` 失败态（AC3d）；仅缺 v2 时由 v1 守卫兜底 |
| 16 | 零实体闸需区分"停用全部类型"；审计含 actor | 采纳 → §3.4 + AC5 |
| 17 | status/mapping 未校验 owner（新增 warnings 会泄露） | 采纳 → §3.4 owner 校验 + AC5 |
| 18 | 加密 PDF / 并发 / 大文件 / 空 content_list | 部分采纳 → 加密 PDF 走既有 converter 测试（AC7 回归）；**并发串味采纳为 AC3i**（两线程两 service 各报各的 `dropped_blocks`）；空 content_list → AC3d；187k 字符规模 → AC6 限时（`T` 独立预跑） |

## 7.2 ①.5 冷审 round 2 处置（全量，留痕）

| # | 冷审意见 | 处置 |
|---|---|---|
| R2-1 | caption/footnote 无判据 | 采纳 → AC2 独立夹具（`v2_table_caption.json`）+ 顺序断言 |
| R2-2 | 未断言 `unhandled block type: title` 消失 | 采纳 → AC1 断言不含该 warn + `dropped_block_types == {}` |
| R2-3 | "v1 缺失时不出现告警"方向错 | 采纳 → AC3c 反向断言（缺失必须告警） |
| R2-4 | 无姓名/身份证/银行账号类别覆盖 | 采纳 → AC6 按 `PERSON/ID_CARD/BANK_CARD` 各 ≥1 |
| R2-5 | 阈值数值无推导 | 采纳 → `GUARD_RATIO=0.95` 写死，来源=修前实测 0.0007 与修后目标 1.0 |
| R2-6 | 缺 docker/部署链 | 部分采纳 → §4.1 显式声明不可达 |
| R2-8 | AC4 无非零例（常量归一即过） | 采纳 → API 级 79 字面例 + 0 例两向 |
| R2-9 | 红性 items==0 假红 | 采纳 → AC6 改 preview 离线回放 v2=6segs/138chars |
| R2-12 | 探针与受测同源、被一起改坏仍绿 | 采纳 → N0 字面绝对下限 + `(text,type)` 去重 + 3 次取中位 |
| R2-14 | §3.3 第 3 行方向/AC3b 不一致；variant 表缺 3b/3c | 采纳 → 表已修；AC3b/g/h + 变异 ⑦ 覆盖；数值改字面 |
| R2-15 | 缺"v2 缺失、v1 完整"用例 | 采纳 → AC3e |
| R2-18 | 并发无判据、25min 不可判 | 采纳 → AC3i 并发判据；`T` 改受测前独立预跑回填 |

## 7.3 ①.5 冷审 round 3 窄复核处置（留痕）

| # | 意见 | 处置 |
|---|---|---|
| R3-1 | AC3b 用 `\d+` 正则近乎恒真 | 采纳 → 改字面 `v1=3033 v2=33`（夹具=6 字×500） |
| R3-2 | §3.3 第 3、7 行无 AC 覆盖 | 采纳 → 新增 AC3g（第 3 行）/ AC3h（第 7 行），均字面数值 |
| R3-3 | 并发只靠既有路径带过 | 采纳 → AC3i（两线程两 service 独立性判据） |
| R3-4 | `T` 来源自指风险 | 采纳 → §4 写明"受测前独立预跑回填，PR 写死" |
| R3-5 | 版本号仍写 v2、round2 无留痕 | 采纳 → 版本改 v3 + §7.2/§7.3 |
| R3-6 | 138 与 143 并列无说明 | 采纳 → §1 补口径说明（clean 后正文 vs NER 载荷） |
| R3-7 | §3.3 伪码 `clean_segments(..., stats=v2_stats)` 自引用 | 采纳 → 伪码改为先建 `CleanStats()` 再 out 传参 |