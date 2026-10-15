# Issue#131 设计：eval-ng 速度测量模式（端到端案卷速率模块）

| 项 | 值 |
|---|---|
| Issue | youayou-Lee/LexMask#131 |
| 基线 | `origin/preview` @ `643d581`（含 #127 文档级链 / #129 默认开启 / #130） |
| 分支 | `feat/issue131-speed-module` |
| 状态 | **v3**，已逐条处置门①.5 两轮评审（第一轮 REVISE / 第二轮 REVISE），处置表见 §11 |
| 评审原文 | 私有仓 `LexMask-workspace/docs/acceptance/2026-10-15-issue131-①.5评审-{第一轮,第二轮}.md` |
| 日期 | 2026-10-15 |

## §0 结论摘要

把「测速度」做成 `eval-ng/cli.py` 的第三个子命令 **`speed`**，与质量评测解耦：引擎出**单臂单次**的
可复现报告（逐环拆解 + 逐页 + 冷温标注 + 环境脚注 + **可反推的配置快照**），**臂编排留在引擎外的薄脚本**里。
本期（P1）**零后端改动、零新增埋点**——后端 `duration_ms` / `pipeline_status.stage_duration_ms`
已含所需的全部环节，工作是**接线 + 聚合 + 报告纪律**。

**判据的立心之变（v2）**：不再以「死磕历史绝对秒数」为最强判据——历史锚点出自**前端 vitest 探针**
（真 `runVisionDetectionPages` + 真后端），而本模块的 `single` 线是 **API 级 Python 驱动**，两者是**不同仪器**。
故 AC1 改为**同实例同窗口的双仪器对照**，绝对带降级为**观测参考带（P4）**：出带只记录 `out_of_band:true` + 一次
re-anchor 记录，**不参与任何判定**（无逃生舱；第三轮 W1）。

**v3 的四处加固（第二轮评审 S1–S9）**：

1. **请求载荷与对照装置写死**（S3）：`single` 线的请求体不再留白——类型集、`force`、`include_result_image`
   全部写死，**默认载荷 = 历史锚点装置同款**（`OCR19` + `official_seal`/`signature`），否则
   `has_request=False` 会静默回落默认类型集（`backend/app/api/redaction.py:189,195-197`），两仪器根本不可比。
2. **两个时钟同粒度**（S1）：闭合校验只在**同粒度**上做（`Σ_pages` vs `Σ_pages`）；文件级墙钟交给
   `parallel_efficiency`，不再拿它跟后端逐页之和比 2%（那在 k=3 下恒 FAIL）。
3. **AC 表补齐 Issue 明文要求**（S8）：新增 **AC21 键归位 / AC22 单线键路径来源 / AC23 跨仪器装置 /
   AC24 载荷同一性 / AC25 文件墙钟语义 / AC26 质量旁证结构化内置**。
4. **判据可执行性**（S6/S7）：泄密检查换成**自建报告扫描器**（`leak_check.py` 只认文档+对照表 CSV，扫不了 JSON）；
   中断清理语义改为**可满足**的写法（kill 驱动 vs kill 后端两条分开判）。

## §1 定位与分期判据

| 期 | 交付 | 完成判据（可判） | 本期 |
|---|---|---|---|
| **P1 出数** | `eval-ng/cli.py speed --exam <exam.json> --driver single\|batch --arm <arm.json> --out <dir>` | §8 AC1–AC26 | ✅ 验收 |
| **P2 双臂** | 同卷 × 两臂 → 逐环 delta + 漂移带；侧车 `elapsed` / vLLM 服务端耗时上抛 | 需后端改动，**另行立项** | ⛔ **本期不建、不判** |
| **P3 门禁** | `--baseline` + 容差 + `--fail-on-regression` | 劣化超容差 → 非 0 退出 | ⛔ **本期不建、不判** |

**P1 的验收明确不含对比**：对比需要冷温纪律（§5）与臂编排（§6）就位，且跨臂数字必须可比，否则假红。
P2/P3 的「不判」是**显式范围声明**，不是漏写（第一轮 I10/D8）。

## §2 底座现状与缺口（实证）

### 2.1 可复用的现成件

| 件 | 位置 | 复用点 |
|---|---|---|
| 子命令骨架与环境强制 | `eval-ng/cli.py:248 _run_e2e` | 环境组装（`--env-label/--target-label/--hardware/--topology/--stack-version`）→ 加载考卷 → 建 API → 跑 → `strip_heavy` → 落 `{ts}-{exam_id}-e2e.json` → 退出码 |
| 硬件自动探测 | `eval-ng/engine/env_detect.py:26 detect_hardware` | 环境脚注的 hardware 项（探测失败即退出码 2，硬校验） |
| **既有速度聚合器** | `eval-ng/engine/scorers/speed.py:22 score_speed`（被 `engine/runner.py:15` 导入、`:101` 调用；测试 `backend/tests/test_evalng_scorers.py:6`） | **复用**：`{pages_total,warmup_pages,steady_pages,wall_s{total,mean,p50,p95},throughput_pages_per_min}` 汇总口径；**新包改名 `speedkit` 以避开同名**（第二轮 S5） |
| `PERF_ONLY` 形态 | `eval-ng/engine/protocol.py:21,59` | `gt=None` 的条目走纯耗时路径；`Answer.latency_sec` 全形态必报（`:64,71`） |
| per-file 速度返回结构 | `eval-ng/engine/e2e.py:272-282` | `perf{...}` 结构可沿用 |
| level 级吞吐（**已存在**） | `eval-ng/engine/e2e.py:324`（聚合）、`eval-ng/engine/report.py:85`（md 渲染） | 吞吐口径可沿用 |
| 逐环聚合雏形 | `eval-ng/engine/e2e.py:311-327` | 质量路径已把逐页 `duration_ms` 按 key 聚成 `{mean,p95}`——可提为公共函数 |
| 逐页计时抽取 | `eval-ng/engine/e2e.py` `iter_pages_with_timing` | 单文件线逐页 `wall_s` + `duration_ms` + `pipeline_status` |
| 基线闸 | `eval-ng/engine/gates/baseline_gate.py:10` | P3 复用 |
| 成绩单渲染 | `eval-ng/engine/report.py:37 render_report` | json/md 渲染骨架（但**不认识 e2e 结果**，见缺口 5） |
| 单测 | `backend/tests/test_evalng_{cli,e2e_gold,gates,manifest,participants,protocol,runner,scorers}.py` | 回归面 |

> **v2 订正（第一轮 D7）**：v1 与 Issue#131 正文曾写「`throughput_pages_per_min` 只进
> `e2e_exec.metrics` 未进 `summary.metrics`（真 bug）」。**该表述是错的**：全仓 `e2e_exec` 零命中，
> 现树为 `eval-ng/engine/e2e.py:282`（per-file）/`:324`（level）产出、`report.py:85` 渲染。
> 该条**撤销**，Issue#131 正文已同步订正。真实缺口改写为缺口 7。

### 2.2 必须补的缺口

| # | 缺口 | 证据 |
|---|---|---|
| 1 | **无运行级速度聚合**：`perf_only` 文件不带 `overall`，被排除在 `run_e2e_level` 聚合外；`strip_heavy` 还会删 `pages_detail` | `eval-ng/engine/e2e.py:344-372,384` |
| 2 | **只有单文件线**：无批量线（smart_batch）驱动 | `eval-ng/engine/e2e.py` 仅 `iter_pages_with_timing` / `parse_and_hybrid_ner` |
| 3 | **无冷温/缓存纪律**：`--warmup-pages` 语义是「丢弃前 N 页」，不是「暖机轮次」；无重启、无清缓存 | `eval-ng/cli.py:107` |
| 4 | **无臂概念**；`run-e2e` 的 `--baseline` **悬空未接线** | `eval-ng/cli.py:108` 定义，`_run_e2e`（`:248-292`）未引用 |
| 5 | **e2e 结果渲染不出 md**：`render_report` 期望 `{participant,exam_id,runs,aggregated,perf,gates,errors,env}`，全文零处引用 `per_file` | `eval-ng/engine/report.py:37-100` |
| 6 | **真实考卷清单缺失**：只有 `manifest.private.example.json` | `eval/datasets/` |
| 7 | **`run-e2e` 产物无结构化脚注契约**：env 是普通 dict、**只查 `hardware` 非空**（`cli.py:261-265`），**绕过** `report.py:11-27` 的 `_assert_env` 硬校验 | `eval-ng/cli.py:261-265`、`report.py:11-27` |
| 8 | **level 级产物不可反推配置**：现有 `config` 仅 `{api_base, warmup_pages, exam}` | `eval-ng/cli.py:281-283` |

### 2.3 后端仪表（不需改动，只需抽取）

`POST /redaction/{file_id}/vision`（`backend/app/api/redaction.py:176`）返回的 `VisionResult`
（`backend/app/models/vision_schemas.py:17-28`：`bounding_boxes` / `warnings` / `pipeline_status` /
`duration_ms` / `cache_status` / `result_image`）中，`duration_ms` 是**逐页逐环字典**，HaS 子拆分直接写在
这个字典里（`backend/app/services/vision/has_text_analysis.py:286-294` 等），经 `ocr_has_vision_service.py:382`
注入 —— 即**单文件线自带 `has_text_slot_wait_ms` / `has_text_model_ms` / `has_text_duplicate_wait_ms` /
`has_text_cache_status`**。

作业线（`GET /jobs/{id}`）另有 `performance.recognition.pages[p]`（含 `page_concurrency` /
`active_pages_at_start|end`）与压平后的 `duration_breakdown_ms`、`recognition_parallelism_ratio`、
`queue_wait_ms`（`backend/app/services/task_queue_metrics.py:175-191`、`app/api/jobs.py:118-134`）。

**两条线的键路径来源不同（写死，v3 新增 AC22）**：

| 线 | 逐页环数据来源 | 是否有 `duration_breakdown_ms` |
|---|---|---|
| `single` | `VisionResult.duration_ms`（根：`{"ocr_has":..,"visual_features":..}`，`backend/app/services/vision_service.py:213`；`stage_duration_ms` 挂到 `pipeline_status["ocr_has"]`，同文件 `:414-420`；根 `total` 同文件 `:532`） | **没有**（该键是 jobs 线专有） |
| `batch` | `jobs.performance.recognition.pages[p].duration_breakdown_ms`（`<pipeline>.<stage>` 前缀合成，`task_queue_metrics.py:180-190`） | 有 |

⇒ 压平函数**两线统一**，`single` 线由驱动自行按同一套键规则合成，不得两套口径。

**压平后的键路径（写死，防歧义）**：`single` 线 = `ocr_has.<stage>` / `visual_features.<stage>` /
`pdf_render_ms` / `pdf_render_cache_hit` / `pdf_text_layer_ms` 等；`batch` 线 = `<pipeline_name>.<stage_key>`。
已核实的真实键：

| 管线标签 | 阶段键 | 证据 |
|---|---|---|
| `ocr_has` | `prepare` / `ocr` / `has_ner` / `match` / `draw` | `backend/app/services/ocr_has_vision_service.py:358,383,396,407,444-446,496,530,568` |
| `ocr_has` | `has_text_slot_wait_ms` / `has_text_model_ms` / `has_text_duplicate_wait_ms` / `has_text_cache_status` / `has_text_type_count` / `has_text_total_ms` | `backend/app/services/vision/has_text_analysis.py:286-294` 等 |
| `visual_features` | 逐 stage 键（`stage_duration_ms` 快照） | `backend/app/services/vision_service.py:422-424` |
| `text_layer_doc` | 文档级链阶段键 | `backend/app/services/redaction_orchestrator.py:591` |

⚠ **第一轮 D15 成立**：v1 写「必含 `ocr_has.*` 与 `has_text_*` 两环」在语义上重叠（`has_text_*` 本就挂在
`ocr_has` 前缀下），故改为**逐条列举完整键路径**（AC7）。既有 `benchmark_throughput.py:105-111` 按
`endswith(suffix)` 匹配，会把不同管线的同名后缀混在一起——新模块**不得**沿用（AC21 守卫）。

## §3 考卷契约

- **驱动入口**：读 `make_exam_from_manifest.py` 产物（`{items:[{id,carrier,gt_pages?...}]}`），与 `run-e2e` 同格式。
- **合成卷（公开）**：`eval/datasets/manifest.json`（16 文件 / 15 卷）——不新增清单。
- **真实卷（私有）**：补 `manifest.private.json`（当前仅 `.example.json`）。**清单与卷路径落私有仓**；
  公开仓只留 example 与契约说明（`eval-ng/datasets/manifest-private-schema.md`）。真卷无 `gt_pages` ⇒ `perf_only`。
  **`--suite real` 且清单缺失 → 非 0 退出 + 明确错误**（AC19）。
- **`no_gt` 强制**：真卷报告 `no_gt=true` 且**数值字段白名单外一律禁数值**（键路径级白名单；`0` 与 `0.0` 同样禁，`null`/`"not_applicable"` 允许）；
  第三轮 X5：原「禁 P/R/F1」是黑名单，**换个字段名（`score`/`quality_index`）即可绕过**，故改白名单 + 名称黑名单兜底；
  合成卷**条件式**：若本次驱动未走质量路径（`speed` 默认不评分，见 §7.1），则允许不带 F1，但**必须**带
  `quality_eval:"not_run"` 显式标记；若走了质量路径，F1 必须为数值型（AC11，v3 按第二轮 S9 改条件式）。
- **`--driver` 与 carrier**：`single` 只支持页级 carrier（`pdf`/图像）；`batch` 支持全部。

## §4 驱动与逐环拆分

### 4.1 两条驱动线（**链路与载荷写死**，第一轮 R2 + 第二轮 S3）

| | `single`（单文件视觉线） | `batch`（smart_batch 作业线） |
|---|---|---|
| 路径 | Python 驱动：上传 → 逐页 POST（见下方**载荷写死**） | `POST /jobs`(smart_batch) → 上传 → submit → 轮询 `GET /jobs/{id}` |
| 页并发 | **`--page-concurrency`（运行时入参）**，在飞页数上限由驱动自身控制 | 后端 `BATCH_RECOGNITION_PAGE_CONCURRENCY`；**无开关可绕，driver 只观测** |
| 与前端关系 | **不改前端、不改常量**；前端常量只作默认 k 的来源与对照基准（AC23） | 无关 |
| 独有字段 | 逐页原始 `duration_ms` + `pipeline_status` + `cache_status`；无队列噪声 | `queue_wait_ms`、`page_concurrency`、`active_pages_at_start/end`、`recognition_parallelism_ratio` |
| 用途 | 主判据：单卷速率与逐环拆解 | 旁证：真实批量吞吐与队列/闸位行为 |

**`single` 线请求（写死，v3）**：

```
POST /redaction/{file_id}/vision?page=N&force=true&include_result_image=false
Authorization: Bearer <token>
Content-Type: application/json

{"selected_ocr_has_types": ["CASE_NUMBER","BIRTH_DATE","ADDRESS","PHONE","WORK_UNIT",
  "COMPANY_NAME","PASSPORT","INSTITUTION_NAME","GOVERNMENT_AGENCY","NATIVE_PLACE",
  "BANK_NAME","AGE","ID_CARD","PERSON","BANK_CARD","EMAIL","LEGAL_CASE_ID",
  "LICENSE_PLATE","DATE"],
 "selected_visual_feature_types": ["official_seal","signature"]}
```

- **默认载荷 = 历史锚点装置同款**（`OCR19` 十九类 + 印章/签名；原文见私有仓
  `LexMask-workspace/scripts/issue106-d3/d3-probe.test.ts:17-21,47-48`）。
- **body 不可省**：`request: VisionDetectRequest | None = None`，`has_request = request is not None`
  （`backend/app/api/redaction.py:189,195-197`）；省略 body ⇒ 回落 `default_ocr_has_types` /
  `default_visual_feature_types`（`redaction_orchestrator.py:443-444,465-481`）⇒ 类型集与锚点装置不同 ⇒ 不可比。
- `include_result_image=false`：默认值 `True`（`redaction.py:180`）会把结果图 base64 塞进每个响应，
  污染墙钟与传输量（第二轮 S1 指出的 2% 无依据之因）。
- 可用 `--payload-file <json>` 覆盖；覆盖后报告记录载荷 sha256（AC24）。
- **不做**兜底的「自动探测默认类型」：宁可显式失败，也不静默换类型集。

**前端线的角色（v3 行号订正，第二轮 S3）**：历史锚点仪器是前端 vitest 探针
`frontend/src/features/playground/__d3_issue106_tmp.test.ts`（跑真 `runVisionDetectionPages`），
已归档私有仓 `LexMask-workspace/scripts/issue106-d3/d3-probe.test.ts`；vitest **即时编译 TS**，无需 `npm run build`。
**臂切换**：`frontend/src/constants/timing.ts:57 export const PLAYGROUND_VISION_PAGE_CONCURRENCY = 3;`
sed 改该行（→`1` 为串行臂），**不是**旧文档写的 `utils.ts:318`（现树 `:318` 已是注释；`:310` 是默认参数、
`:321 const effectiveConcurrency = concurrency;`，"勾选视觉特征压成 1" 的 `? 1 :` 已随 #106 修复删除）。

**已实测的并发上限**：扫描件 k=3 为上限（LA 单请求 p50 @k=3 为串行 1.36×，k=4 达 2.04× 出包络 ⇒ #113）。
`speed` 默认 `--page-concurrency 3`，允许 k=1 与 k=4 用于**判别力验证**（AC5），不承诺 k>3 为生产配置。

### 4.2 逐环字段清单（报告必须落全）

```
wall_s
├─ file_wall_s            （文件级墙钟：单文件的"提交→末页完成"总耗时）★AC25 专条断言
│   └─ mean / p50 / p95   （n 轮）
├─ page_wall_s            （逐页，steady 与 warmup 分列）
├─ page_concurrency       （逐页在飞页数；驱动自报 + 后端日志区间反推双证）
├─ upload_s  /  job_queue_wait_ms（batch 独有）
├─ recognize_s
│  ├─ total_backend_ms    ← 逐页 VisionResult.duration_ms["total"] 之和（single）/ pages[].duration_ms 之和（batch）
│  ├─ ocr_has.prepare / ocr / has_ner / match / draw     ← 完整键路径
│  │   ├─ ocr_has.has_text_slot_wait_ms        ← 等 HaS 闸位
│  │   ├─ ocr_has.has_text_model_ms            ← 真模型调用
│  │   ├─ ocr_has.has_text_duplicate_wait_ms   ← in-flight 去重等待
│  │   └─ ocr_has.has_text_cache_status / has_text_type_count / has_text_total_ms
│  ├─ visual_features.*    （LocateAnything 段，逐 stage）
│  ├─ text_layer_doc.*     （文档级链，若启用）
│  ├─ pdf_render_ms / pdf_render_cache_hit / pdf_text_layer_ms   （顶层 duration_ms 键）
│  └─ unclassified_ms      ← total_backend_ms − Σ已知环（**反推值，须带 unclassified_source 标注**）
└─ 质量旁证（全部取自响应，禁止读日志，AC26）：
   single → len(bounding_boxes)、warnings、
            pipeline_status.*.has_text_ner_finish_reason / has_text_ner_truncation_retries /
            has_text_ner_rebatch_batches、cache_status
   batch  → jobs.performance 里的 entity_count / bounding_box_count
```

### 4.3 三处真缺口的处置

| 缺口 | 处置 |
|---|---|
| 侧车响应体已回传 `elapsed`，后端只写日志未上抛（`backend/app/services/ocr_service.py`、`app/services/vision/locate_grounding.py`） | **P2**：上抛后可拆「服务端净推理 vs 后端等待/网络」 |
| vLLM 服务端耗时不可见（`has_client.py` 仅日志） | **P2**（同上，属后端改动） |
| 装配/落盘未单列 | **P1 用反推**：`unclassified_ms` + 诚实标注 + **注入可证伪**（AC6） |

⇒ **P1 硬边界：不改后端、不改侧车、不新增埋点。**
⇒ **反向守卫（第一轮 D12）**：P2 未做前，报告**不得**出现 `vllm_server_ms` / `sidecar_elapsed_ms` 之类凭空造的环键（AC14）。

## §5 冷温与缓存纪律

**核心事实**：`_SHARED_NER_CACHE` 是**进程级**、按页文本 sha256 键控、TTL 1800s、**换 file_id 也不能清**
（#106 D3 实证；`before2` 轮 45.36s 就是紧跟基准轮未重启的**缓存假快**，已作废）。

0. **缓存策略是显式入参**（v2 订正，第一轮 D13）：`--cache-policy cold|warm`，默认 **cold**。
   - `cold`：**每一计时轮前重启后端**；报告逐页校验命中页数 = 0，否则标 `cache_contaminated=true` 且该轮不得用于跨臂/锚点对比。
   - `warm`：同臂同进程第 2 轮起测量（测稳态）；报告标 `warm` 并**不**参与 AC1 的绝对/比值判据。
1. **清 == 重启**：`--restart-cmd` 由调用方给（引擎不内建对 `start_all_dcu.sh` 的依赖）。
2. **等就绪**：轮询 `/health` 200 后再静置（历史 rig 用 `sleep 3`），`--ready-timeout` 默认 90×2s；
   **报告记录实测的等待与静置值**（AC12 第 8 项，第一轮 R9）。
3. **命中定义**（第一轮 I5）：**仅 `has_text_cache_status == "hit_before_slot"` 计命中**；
   `shared_inflight` 是**同文本并发去重**（冷跑也可能出现），**单列不计命中**；
   `skipped_*`（`skipped_empty_ocr` / `skipped_too_short` / `skipped_visual_only_types`）说明该页**根本没走 HaS**
   ——此时 `has_text_model_ms` / `has_text_slot_wait_ms` 恒为 0 是**正确**的，不得据此判 FAIL。
   报告须给**逐页 cache_status 直方图**（七态：`not_started` / `skipped_empty_ocr` / `skipped_too_short` /
   `skipped_visual_only_types` / `hit_before_slot` / `miss` / `shared_inflight`）。
4. **暖机（warmup）**：正式测量前用**不在考卷内**的文件跑 N 次（默认 3）丢弃；判据=末两次 `wall_s` 差 ≤10%。
   与既有 `--warmup-pages`（丢弃考卷前 N 页）语义**不同**，两者共存但报告中分别标注。

## §6 臂与薄编排（引擎只出单臂）

**臂定义**（`eval-ng/arms/*.json`，公开只放通用示例）。v2 订正：v1 的示例
`{"env":{"PLAYGROUND_VISION_PAGE_CONCURRENCY":"3"}}` **无效**——该名只存在于前端 TS 常量，后端无此环境变量
（已核实：`frontend/src/constants/timing.ts:57`、`frontend/src/features/playground/utils.ts:310`；
后端 `config.py` 无同名项）。改为**真实可用的开关**：

```json
{"name":"hasgate8", "kind":"env", "env":{"HAS_NER_GLOBAL_MAX_INFLIGHT":"8"},
 "needs_restart":true, "notes":"闸位实验臂（#106 §7）"}
{"name":"docline-on", "kind":"env", "env":{"TEXT_LAYER_DOCLINE_ENABLED":"true"},
 "needs_restart":true, "notes":"#127 文档级链开关（默认 false）"}
{"name":"verify-batch8", "kind":"env", "env":{"HAS_NER_VERIFY_BATCH_SIZE":"8"},
 "needs_restart":true, "notes":"#106 §7.2 请求数实验臂；对墙钟无收益，仅旁证"}
{"name":"probe-serial", "kind":"patch", "patch_file":"arms/probe-serial.patch",
 "restore_paths":["frontend/src/constants/timing.ts"], "needs_restart":false,
 "probe":"vitest", "notes":"仅用于 AC1/AC23 同 rig 对照：sed timing.ts:57 的 3→1（无 build）"}
```

- `env` 型：只改进程环境 + 重启（实例 `.env` 无 `HAS_NER_*` 覆盖 ⇒ 走进程环境注入）。
- `patch` 型：必须改代码的臂——用 patch 文件，**跑完强制还原**
  （`git apply --reverse` + `git checkout -- <restore_paths>`），报告记录还原后 `git status --short` 残留检查；
  **跑前 dirty 即中止**（AC14）。
- **`single` 的 k 不是臂**：它是 `--page-concurrency`（运行时入参，AC5）；臂只描述**进程/代码配置**。

**薄编排**（私有仓 `LexMask-workspace/scripts/speed/`，不进引擎）：
`读臂列表 → 逐臂 [应用 → 重启 → 等就绪 → 静置 → 引擎单臂 n 轮 → 落 json] → 汇总 compare.md（中位/漂移带/逐环 delta）`。
臂启动失败只记 log 跳过，**不中断整批**。

## §7 报告与门禁

### 7.1 指标口径（写死，防漂移）

| 指标 | 定义 | 用途 |
|---|---|---|
| `wall_sec_per_page` / `wall_sec_per_file` | 页级墙钟（驱动侧 `perf_counter`）；**文件级**=提交→末页完成（AC25） | 主判据 |
| `pages_per_min` | steady 页数 × 60 ÷ Σ墙钟（沿用既有口径） | 吞吐 |
| `stage_share_%` | 各环 ÷ `total_backend_ms`（含 `unclassified_ms`） | 定位瓶颈 |
| `parallel_efficiency` | **公式写死**：`Σ(page_wall_s) ÷ file_wall_s`（产品线 alias = 后端 `recognition_parallelism_ratio`） | 判「加并发还涨不涨」：=1 串行，>1 才生效 |
| `has_slot_wait_share_%` | `ocr_has.has_text_slot_wait_ms ÷ Σ page_wall_s` | 判闸位是否瓶颈 |
| `box_count` / `entity_count` | 逐页与合计（**只取自响应**） | 质量旁证（**不作门禁判据**） |

**两个时钟必须分清且同粒度**（第一轮 I1 + 第二轮 S1）：`total_backend_ms` = 后端自报（逐页 `duration_ms["total"]` 之和），
`wall_harness` = 驱动侧逐页 `perf_counter` 之和。**两者只在同为 `Σ_pages` 时比较**（AC6）；
**文件级墙钟不参与该比较**（它含并发折叠，交 `parallel_efficiency`）。

### 7.2 环境脚注（强制，缺项即 `incomplete`）

| # | 项 | 来源 | 判别性要求（v3） |
|---|---|---|---|
| 1 | 实例标识 | `--env-label` / `--target-label`（沿用旧惯例，必填） | 须与实例实测指纹交叉（hostname / `detect_hardware` 输出），**禁 N/A** |
| 2 | 硬件 | `detect_hardware`（或 `--hardware`；探测失败退 2） | 自动探测优先 |
| 3 | 镜像与 DTK 版本 | `--stack-version k=v`（可多次） | 至少一项可由实例探测交叉（`/opt/dtk` 版本文件），**禁 N/A** |
| 4 | 代码 rev | `git rev-parse HEAD`（自动；dirty 附 `--dirty`） | 自动采集，不可手填 |
| 5 | 开关集快照 | 生效的相关 env/config（arm 声明的键 + 默认值） | 由驱动回读生效值 |
| 6 | 考卷清单 hash | 考卷 JSON 与各卷文件 sha256 | 自动计算 |
| 7 | 时间 | 起止时间戳（含时区） | 自动 |
| 8 | 等待/静置实测 | `/health` 就绪耗时 + 静置秒数（第一轮 R9） | 实测值，非声明值 |

依据：#116 撤回教训——性能数字必须带环境脚注；其中 4/5/6/8 与 1/3 的交叉核验是**新增校验**
（现有 `report.py:11-27` 只硬校验 hardware/stack_versions/topology 三项）。

### 7.3 门禁语义（P3，本期不建）

`--baseline <report.json>` + `--tolerance <相对 %>`（默认 15%，依据历史漂移 7.0% 的 2 倍余量）；
默认只报警，`--fail-on-regression` 才非 0 退出。
**门禁判据只用时间与阶段占比，禁止用 `entity_count`**——同一配置两次串行跑，密集页计数实测可差 **80%**。

## §8 验收方案（AC1–AC26，每条含判据与判别性来源）

| AC | 判据（可判） | 环境 | 判别性来源 |
|---|---|---|---|
| **AC1 锚点复现（同 rig 双仪器）** | ① 模块 `single` k=3/k=1 **中位比值 ≥1.8×**（数字出自 `speed --out`）；② **同实例同窗口**跑归档 D3 探针（前置机检见 AC23，载荷同一见 AC24），两仪器的**比值差 ≤20%**；**判定归属（第三轮 E1/E2）**：装置/输入**不可得** ⇒ **BLOCKED**（须附命令 + 原始报错，**不得**计为 PASS）；装置**可得但未按协议执行** ⇒ **FAIL**（禁静默 SKIP）；③ 绝对带 `198.13s`（改前）/ **`86.65s`**（改后，n=3 计时轮中位，r1 弃）±20% 降为**观测参考带（P4）**：出带只记 `out_of_band:true` 并允许一次 re-anchor 记录，**不改变 ①/② 判定** | 云 DCU | #106 D3 实测（n=3 中位，漂移 7.0%）。**前提机检**：canary 无文本层（AC2） |
| **AC2 锚点前提机检** | `canary-issue106-scan-10p.pdf`：`pdftotext` 输出为空 **且** `/files/{id}/parse` 报 `is_scanned=True`、`file_type=pdf_scanned`；**反例** `canary-issue115-textlayer-10p.pdf` 必须**不**满足。Lead 已预机检：10 页 / `pdftotext` = **0 字符**；反例 8666 字符；`canary-issue115-mixed-10p.pdf` 5178 字符 | 本地 + 云 | 「纯扫描」可机检；防换样本静默改语义 |
| **AC3 数字归属** | **模块侧**数字必须出自 `speed --out` 的 json（含 arm/k/冷温标签/卷 sha256/时间戳可追溯）；**探针侧**数字出自探针产物 json（实例内路径见私有验收方案，不写入公开文档）。手工装置产出的数字不得充当模块侧证据 | 云 DCU | 防「不改代码也能过」（第一轮 I4） |
| **AC4 CLI 面** | `speed` 子命令存在；`--exam/--driver/--arm/--out` 缺一即非 0 退出；未知 `--driver`、未知 arm、`--out` 不可写、`--page-concurrency 0` 或 > 页数 → 非 0。**当前树必然 FAIL**（`cli.py:79,93` 只有 `run`/`run-e2e`） | 本地 | 交付物存在性 |
| **AC5 k 生效** | `--page-concurrency 2` → 报告 `effective_k=2` 且逐页 `page_concurrency` 峰值 = 2（**双证**：驱动自报 + 后端响应回吐反推，见 #106 D4 方法）；**单轮页数少于 k 时期望取 `min(k, 页数)`**（页数足够却仍达不到声明值依然 FAIL——判别力不被「小语料豁免」稀释，④云门 D6④/D7 备料实测后订正）；且墙钟位于 k=1 与 k=3 之间（单调性） | 云 DCU | k 是核心入参；防「参数被忽略也能过」 |
| **AC6 逐环闭合（同粒度）+ 残差可证伪** | ①a **同源自闭合**：`|Σ_backend_pages − Σ_rings| ÷ Σ_backend_pages ≤ 2%`（两者**同一时钟同一口径**；文件级墙钟不参与此式，交 AC8/AC25）；①b **跨时钟分列 + 非同源断言（第三轮 X1）**：报告并列 `sum_harness_pages_ms` 与 `sum_backend_pages_ms`，断言两序列**不得逐元素全等**（全等 ⇒ 判驱动抄后端数字），且 `1.00 ≤ sum_harness ÷ sum_backend ≤ 1.25`；② `unclassified_ms = total_backend_ms − Σ_known`（同一时钟）、**`0 ≤ unclassified_ms ≤ 10% × total_backend_ms`（双侧，负残差 ⇒ FAIL；第三轮 W2）**、报告带 `unclassified_source:"derived_from_residual"`；③ **单测两组注入**：**两组互不成比例**的 `(base_total, δ)`（如 (1000,37) 与 (2500,91)）⇒ 残差变化 ≈ δ（±1ms），否则残差是常数噪声（第三轮 X2 封「单点 δ 可被 fixture 常数抵消」） | 本地单测 + 云复核 | v1 公式在反推定义下**恒真**（第一轮 I1）；k=3 下拿文件级墙钟比逐页之和会**恒 FAIL**（第二轮 S1） |
| **AC7 逐环键路径守卫（分路径）** | ① **键存在性**：`single` 报告必含 `ocr_has.prepare/ocr/has_ner/match/draw`（缺任一 → FAIL）；② **非零性按实际路径限定**：对 `cache_status=="miss"` 且非 `skipped_*` 的页——**扫描路径页**（AC2 机检 `is_scanned=True`）须 `ocr_has.prepare > 0` 且 `ocr_has.ocr > 0`；**文本层路径页**须 `ocr_has.has_ner > 0`（该路径 `prepare`/`ocr` 恒 0 是**正确**的，`ocr_has_vision_service.py:358` 全程不更新）；③ **`draw` 只断言键存在，不作非零断言**（文本层路径 `draw` 恒 0，同文件 `:407`） | 本地 + 云 | 第二轮 S2：v2 的「prepare 必非零」对文本层卷恒 FAIL；键路径已对代码核实（§2.3） |
| **AC8 `parallel_efficiency` 口径** | 公式写死（§7.1）；**注入式 fixture 单测**验证（人工构造 `pages_detail` 的 duration/concurrency）；系统级比值另由 AC1 判 | 本地单测 | v1 的「k=1 ∈[0.95,1.05]」近乎恒真（第一轮 I2） |
| **AC9 冷温可判** | `--cache-policy cold`：逐页命中页数（仅 `hit_before_slot`）= 0，报告带逐页 `cache_status` 直方图；`warm` 第二轮命中 > 0；冷跑若命中 >0 → `cache_contaminated=true` 且该轮被排除。**不得**把 `skipped_*` 页判 FAIL | 云 DCU | 反向对照两跑；命中定义写死（第一轮 I5） |
| **AC10 n≥3 与失败轮** | 每配置 ≥3 计时轮，报中位 + IQR；**人为 kill backend 一轮** → 该轮标 `invalid` 并**排除**，不得静默按 2 轮取中位；报告列出被排除轮次 | 云 DCU | 历史 rig 弃首轮；防「静默降级」（第一轮 R4） |
| **AC11 质量字段配对（条件式）** | 真卷报告：**数值字段白名单（键路径级）之外一律禁数值**（`0`/`0.0` 均违规；`null`/`"not_applicable"` 允许）**且**名称黑名单（`(f1|precision|recall|score|quality|accuracy)` 或 `quality`/`metrics` 子树下任何数值）⇒ 违规；+ `no_gt:true`；合成卷：`speed` 默认不评分 ⇒ 必须带 `quality_eval:"not_run"`；若走了质量路径则 F1 必须数值型 | 本地 | 配对断言堵「两不写」（第一轮 I8）；第二轮 S9 改**条件式**；**第三轮 X5 改白名单**（黑名单可换名绕过） |
| **AC12 环境脚注 8 项硬校验** | §7.2 八项；**逐一删除任一项** → 渲染器报 `incomplete` **拒出报告**（非 warning）；第 1/3 项须与实测指纹交叉、**禁 N/A**；**所有数值 AC 的前置机检**：脚注未含指定云实例 + 镜像/DTK 版本 → 数值 AC 判 **FAIL**（不是 N/A，不是跳过） | 云 DCU | 现有只硬校验三项；「云门④」作为 AC 恒真 ⇒ 改前置机检（第一轮 I3/R7；第二轮 S9 加强判别力） |
| **AC13 报告自足（可反推配置）** | 仅凭 `--out` 的 json 可重建：arm 名与声明、`effective_k`、warmup 轮数、计时轮数、缓存策略、卷 sha256、载荷 sha256、代码 rev、时间；**删除任一项** → 校验器报 `incomplete` | 本地静态 | Issue P1 明文（第一轮 D4/R3） |
| **AC14 臂纪律** | `patch` 臂：跑完 `git status --porcelain` 为空；**跑前 dirty 即中止**（非 0）；故意让还原失败 → 必须报错而非静默。`env` 臂只改进程环境 + 重启。报告**不得出现未实现的环键**（`vllm_server_ms` 等） | 云 DCU | 第一轮 D9/D12/R8 |
| **AC15 两线差异（行为断言）** | `batch` 报告出现队列等待（`job_queue_wait_ms`）且**在 ≥5 页卷上**页并发汇总 > 1；`single` 报告出现逐页 1..N 序号且**无**作业级队列字段。以**行为差异**断言，不以「键的有无」断言 | 本地 + 云 | v1 缺席断言可能让正确实现 FAIL（第一轮 I7）；页数限定见第二轮 S9 |
| **AC16 单测与回归** | `pytest backend/tests/test_evalng_speed*.py`：①**新增测试文件必须存在**且②**点名反向用例名出现在 pytest 输出**：缺脚注→`incomplete`、真卷→无数值质量、空报告→FAIL、残差注入可证伪、残差来源标注、CLI 缺参非 0、k 非法非 0、`manifest.private.json` 缺失非 0；③ **无回归判据 = 改动前后失败用例名集合相等**（本地既有 18–19 条环境性失败，非全绿基线） | 本地 | v1「全绿」bar 太低（第一轮 I6）；第二轮 S9 补文件/用例名存在性 |
| **AC17 泄密守卫（自建扫描器）** | 新增 `eval-ng/scripts/check_report_redaction.py`：扫报告 json/md 的 **IP 正则 / 绝对路径前缀 / `--denylist`（私有文件列真卷名·实例名）**，命中即退 1；单测含正例+反例。公开仓不得出现真实卷路径/实例 IP/真实秒数；`manifest.private.json` 契约须写明「读哪个清单、缺失与非法行为」 | 本地 | 第一轮 D11/R13；第二轮 S6：`eval/scripts/leak_check.py` 只认「文档 + 对照表 CSV」，`extract_text` 对 `.json` 抛 `ValueError` ⇒ **不能**充当本用途工具（能力边界写入 AC17 说明） |
| **AC18 旧仪器口径收敛** | **必须**与 `backend/scripts/eval/benchmark_throughput.py` 在**小合成语料**同口径 parity（`pages_per_min` / 页 p50 差 ≤20%，该脚本零测试引用）；**不得**以「文档声明弃用」替代 parity（该恒真分支已删除）。新模块是**单一产出口**；且须清点复用关系：`eval-ng/engine/scorers/speed.py:22 score_speed` 被复用做汇总裁（导入路径 `eval-ng.engine.scorers.speed`），新包 `eval-ng/engine/speedkit/` **不同名**、无导入歧义 | 本地 + 云 | 第二轮 S5 ①恒真出口 ②漏清点 `scorers/speed.py` |
| **AC19 异常输入** | manifest 缺失/schema 非法/空 `items`/0 页 PDF 引用/k 非法/`--out` 不可写 → 明确报错且非 0，**不得产出「零页全绿」报告**；加密 PDF 在 speed 考卷里的行为与 `expect_reject` 语义对齐 | 本地 | 防「空报告全绿」另一路径（第一轮 R10） |
| **AC20 大卷、中断清理与部署链路** | ① 67 页真实卷跑通：逐页行数 = 67、逐环闭合成立；② **中断语义（两条分开判，v3 重写）**：**(a)** SIGINT 驱动进程 → 驱动 `finally` 生效 ⇒ 后端**零残留**（机检 `GET /files` 无新增，或新 file_id 已删）；**(b)** `kill -9` 驱动 → 后端**可能有残留**（诚实断言：报告标注 `orphan_possible=true`）且模块提供 `--cleanup` 子命令，在后端恢复后扫描并删除本模块上传的残留文件（清理后机检 0 残留）；**(c)** kill 后端 → 驱动**必须**记录 `invalid` 轮次并提供同一 `--cleanup` 路径；③ 三条链路各 smoke：本地 compose 直连 8000 / docker nginx / 云 DCU，**batch 线必须单独过 nginx**（大卷请求体上限前科 #28/#29），报告记录实际 `api_base` | 云 DCU + 本地 | 历史 #125 锚点卷；第二轮 S7：`finally: api.delete_file`（`e2e.py:242-243,265-266`）在**驱动侧**，kill 后端时驱动发不出 DELETE ⇒ 原「必被清理」不可满足 |
| **AC21 键归位守卫**（新增，第二轮 S8） | 喂 canned 响应**同时**含 `text_layer_doc.ocr` 与 `ocr_has.ocr` → 断言语平后落在**两个独立桶**，不得合并；并断言实现**未使用 `endswith` 后缀匹配**（跨管线同名后缀会混，`benchmark_throughput.py:105-111` 即此坑）；另喂含 `has_text_*` 的页断言其归在 `ocr_has` 前缀下 | 本地单测 | §2.3 明令禁用后缀匹配，但 v2 无 AC 守卫 |
| **AC22 `single` 线键路径来源写死**（新增，第二轮 S8/评审事实核对） | 单测断言 `single` 聚合的输入来源 = `VisionResult.duration_ms`（根 `total` / `ocr_has` / `visual_features`）+ `pipeline_status[*].stage_duration_ms`；**断言实现不依赖 `duration_breakdown_ms`**（该键仅 jobs 线产出，`task_queue_metrics.py:180-190`）；两线共用同一压平函数（同输入→同输出 fixture 比对） | 本地单测 | single 线无 `duration_breakdown_ms`；不写死则两线会出现两套口径 |
| **AC23 跨仪器装置写死**（新增，第二轮 S3） | AC1② 的前置**全部机检**（**七项**，与 Issue 正文/验收方案同序），任一不满足 → AC1② 判 **FAIL（禁 SKIP）**：① 实例有 node（绝对路径 `$HOME/.local/node-v22/bin/node` v22.14.0——非交互 ssh 的 PATH 里**没有** node）；② `frontend/node_modules` 在；③ 探针文件从私有仓 `LexMask-workspace/scripts/issue106-d3/d3-probe.test.ts` scp 到位并拷入 `frontend/src/features/playground/`（实例上跑完即删，现树**不在**，**私有仓未同步到实例**，scp 源只能是本地——这是必做步骤）；④ 臂切换 = sed `frontend/src/constants/timing.ts:57`（`3`→`1`），**非** `utils.ts:318`——该处 `? 1 :` 已由 #106 删除，按旧行号 patch 会**静默不改行为**，故必须**断言两臂 `page_concurrency` 分别为 3 与 1**（否则两条同值序列也会「像跑过了」）；⑤ 每轮重启后端且逐轮校验 `cache_hits=0`；⑥ 实例上的凭证与 file_id 文件就位（路径见私有验收方案）；⑦ 实例上不存在私有装置仓库。**收尾**：跑后删探针 + `git checkout --` 还原，实例 `git status` 无新增脏项 | 云 DCU | 第二轮 S3；Lead 已核实例工具链在（node v22.14.0 / `node_modules` 在）、私有仓未同步、实例仓无残留探针 |
| **AC24 请求载荷同一性**（新增，第二轮 S3④） | ① 单测用 mock transport **捕获** `single` 线发出的请求：断言 body 的 `selected_ocr_has_types`/`selected_visual_feature_types` **非空**且等于 `--payload-file`（默认 `OCR19` + 印章/签名）；② 断言 query 含 `force=true&include_result_image=false`；③ 云侧旁证：报告记录载荷 sha256，且 `pipeline_status.ocr_has.stage_duration_ms.has_text_type_count` 与载荷类型数一致（不一致 → FAIL 并查明）；④ 显式断言**未走** `has_request=False` 回落路径 | 本地单测 + 云 | 不传 body ⇒ `has_request=False` ⇒ 回落默认类型集（`redaction.py:195-197`）⇒ 与锚点装置不可比 |
| **AC25 文件墙钟语义**（新增，第二轮 §11 核验 D3 假处置） | 报告**必含** `file_wall_s`（`mean`/`p50`/`p95`，n 轮），语义 = **提交→末页完成**；注入式 fixture（人工构造页间隔与提交时刻）断言其等于构造跨度 ±1%；**删除该字段 → `incomplete`** | 本地单测 | v2 只在 §4.2 定义字段、无任何 AC 断言其存在与语义 ⇒ D3 实为假处置 |
| **AC26 质量旁证结构化内置**（新增，第二轮 S8） | Issue 验收清单第 7 条：`box_count` / `entity_count` / `has_text_ner_finish_reason` / `has_text_ner_truncation_retries` / `has_text_ner_rebatch_batches` / 缓存命中 **全部从响应取**：① 单测断言报告字段值等于 mock 响应值；② **静态断言** driver/collect 实现内无日志文件读取（不出现读 `.log` 的代码路径）；③ 报告出现「日志 grep」痕迹即 FAIL | 本地 + 云 | Issue 明文要求；v2 零覆盖 |

**反「恒真」纪律**：每条阈值的来源在表中列明；未列来源的阈值不得写进代码。
**P2/P3 条款不进本表**（本期不判），改动只需保证 §2.2 缺口 4 的 `--baseline` 悬空状态**不被新代码加剧**。

## §9 落地计划

文件清单（分支 `feat/issue131-speed-module`）：

- `eval-ng/cli.py`：新增 `speed` 子命令（复用共享参数与环境组装，照 `_run_e2e` 模板）
- `eval-ng/engine/speedkit/__init__.py`（**包名 `speedkit`，避开既有 `engine/scorers/speed.py`**）
- `eval-ng/engine/speedkit/driver.py`：`SpeedApi`（vision 带 body + jobs 面）+ `measure_single_round` / `measure_batch_round`（`--page-concurrency`、`--cache-policy`、`--round-restart-cmd`、`--round-evidence`、`--payload-file`、`--cleanup`）
- `eval-ng/engine/speedkit/collect.py`：**逐条键路径**抽取 / 两线统一压平 / `unclassified_ms` 反推 / 闭合校验 / 两时钟同粒度校验
- `eval-ng/engine/speedkit/report.py`：speed md/json 渲染 + 八项脚注硬校验 + 配置快照自足性校验；汇总裁**复用** `eval-ng/engine/scorers/speed.py:22 score_speed`
- `eval-ng/engine/speedkit/arms.py`：臂 schema + `env`/`patch` 应用与**还原保证**（跑前 dirty 中止）
- `eval-ng/arms/*.json`：公开示例臂（**真实开关名**，见 §6）
- `eval-ng/datasets/manifest-private-schema.md`：真卷清单契约（公开）
- `eval-ng/scripts/check_report_redaction.py`：报告泄密扫描器（AC17 / D5④）
- `eval-ng/scripts/check_public_tree.py`：公开树红线扫描器（G4；公网 IP / 绝对路径 / Windows 盘符 / 凭据形态 / `--denylist`，含阳性对照自检）
- `backend/tests/test_evalng_speed.py`（mock API + 点名反向用例；**零网络**：夹具把 `SpeedApi` 构造替换成抛错版本，AC24 用 `httpx.MockTransport` 抓原始请求）
- 私有：`LexMask-workspace/scripts/speed/*`（编排 + 真卷清单 + 实例臂 + 探针 payload）

步骤顺序（先无争议的，后碰网络/进程的）：

1. `collect.py` + 单测（canned 响应 → 逐环表/两线统一/闭合同粒度/unclassified/**注入可证伪**/AC21 键归位/AC22 来源）——**零网络**
2. `report.py` + 八项脚注校验 + 自足性 + `file_wall_s` 语义（AC25）+ 反向用例
3. `cli.py speed` 接线 + `arms.py`（含还原保证）+ `check_report_redaction.py`
4. `driver.py` single 线（Python 运行时 k；**载荷写死** AC24；不经前端）
5. `driver.py` batch 线 + nginx 链路 smoke
6. 云门④：AC1/AC2/AC5/AC9/AC10/AC12/AC14/AC20/AC23
7. AC18 旧仪器 parity（必做）
8. P2/P3 另开

## §10 风险与不做

**风险**：
1. **canary 可比性**——若 canary 含内嵌文本层，`TEXT_LAYER_DOCLINE_ENABLED=true`（#129）会让 OCR 环归零，
   与历史锚点不可比 ⇒ AC2 把该前提**机器化**并在报告中记录（Lead 已预检：锚点卷 0 字符）。
2. **跨仪器误差**——模块 API 级驱动 vs 前端 `runVisionDetectionPages`，即使载荷同一，重试/abort/页序实现差仍可能
   让比值偏出 ⇒ AC1② 给 **≤20%** 带（不再用 10%），且**前置不满足即 FAIL**，避免「悄悄不跑也不能算过」。
3. **实例风暴**——臂编排反复重启后端，须限制并发（一次只跑一臂）并与既有服务错峰。
4. **真卷敏感**——真卷数字与路径不得进公开仓；报告落公开仓前先过 AC17 扫描器。
5. **锚点环境漂移**——历史锚点未记录镜像/DTK/code rev；若现实例不同，只触发 **P4 观测量**记录（`out_of_band` + 一次
   re-anchor），**不影响任何判定**（第三轮 W1：v1 曾把它当判据级逃生舱，已封）。

**不做**：
- 不改后端/侧车、不新增埋点（P1 边界）；不改前端、不改 `timing.ts` 常量
- 不把臂编排塞进引擎
- 不做前端可视化报告（只出 json + md）
- 不承诺多用户叠加（属 #111 边界）
- 不在 P1 建 P2/P3（双臂 delta / 门禁）
- 不替代 `benchmark_throughput.py`（保留为旁证，按 AC18 **parity 必做**收敛）

## §11 门①.5 评审处置表（v1 → v2 → v3）

**前两轮（设计稿）结论均为 REVISE**；**第三轮改审验收方案**（评审输入 = Issue 正文 + 方案全文）结论同为 **REVISE**，
其 W1/W3/E1/E2 四项已回灌本文 AC1（参考带脱离判定、四态判定、冷跑须留机器证据）；三轮原文均已归档私有仓
（`LexMask-workspace/docs/acceptance/2026-10-15-issue131-①.5评审-第一轮.md`、`-第二轮.md`、`2026-10-16-…-第三轮.md`），使本表**可独立核验**（第二轮曾指出「R1–R15 是位置列表而非逐条映射，无法核验」——本轮补齐为
`R_i → AC_j → 判据句`）。

### 11.1 第一轮：分歧点 D1–D16

| 项 | 处置 | AC | 判据句（锚） |
|---|---|---|---|
| D1 CLI 面未绑定 | 采纳 | AC4 | 「`--exam/--driver/--arm/--out` 缺一即非 0 退出」 |
| D2 k 未绑定交付物 | 采纳（并订正 Issue 硬约束 3 的错误措辞） | AC5 / §4.1 | 「`--page-concurrency 2` → `effective_k=2` 且逐页并发峰值 = 2（双证）」 |
| D3 文件墙钟无 AC | **本轮真处置**（v2 曾假处置） | **AC25** | 「报告必含 `file_wall_s`（提交→末页完成），fixture 断言 ±1%；删除 → `incomplete`」 |
| D4 报告不能反推配置 | 采纳 | AC13 | 「仅凭 json 可重建配置；删任一项 → `incomplete`」 |
| D5 残差无来源标注 | 采纳 | AC6② | 「报告带 `unclassified_source:"derived_from_residual"`」 |
| D6 n≥3 波动塞表外 | 采纳 | AC10 | 「≥3 轮报中位 + IQR；人为失败轮标 `invalid` 并排除」 |
| D7 `e2e_exec` 不可操作 | 采纳：表述错误，撤销并订正 Issue 正文 | §2.1 订正块 / AC18 | 「`e2e_exec` 全仓零命中」 |
| D8 P2 范围未声明 | 采纳 | §1 / §8 表尾 | 「P2/P3 不进本表，本期不判」 |
| D9 patch 臂还原无 AC | 采纳 | AC14 | 「跑完 `git status --porcelain` 为空；跑前 dirty 即中止」 |
| D10 旧仪器无处置 | **本轮真处置**（删恒真分支 + 清点 `scorers/speed.py`） | AC18 | 「**必须**小语料 parity ≤20%，不得以文档弃用替代 parity」 |
| D11 泄密无 AC | **本轮真处置**（换自建扫描器） | AC17 | 「`check_report_redaction.py` 扫 IP/路径/denylist，命中退 1」 |
| D12 无反向守卫 | 采纳 | §4.3 / AC14 | 「报告不得出现未实现的环键（`vllm_server_ms` 等）」 |
| D13 冷温自相矛盾 | 采纳 | §5.0 / AC9 | 「`--cache-policy cold` 每轮重启且命中 = 0」 |
| D14 锚点字段不可判 | 采纳 + **勘误** | §7.1 / AC1③ | 字段写死为 `wall_sec_per_file` 中位；**勘误**：改后 n=3 中位应为 **86.65 s**（r2–r4 = 86.65/86.74/84.42），v2 写的 86.74 是算术滑笔；改前 198.13 s ✓（证据 `2026-10-16-issue106-scan-page-concurrency-evidence.md §12.2`；对 2.28×→2.29× 无影响） |
| D15 环名重叠 | 采纳 | §2.3 / AC7 / AC21 | 「逐条列举完整键路径；`has_text_*` 归 `ocr_has` 前缀」 |
| D16 无回归无判据 | 采纳 | AC16③ | 「失败用例名集合相等」 |

### 11.2 第一轮：无效标准 I1–I10

| 项 | 处置 | AC | 判据句（锚） |
|---|---|---|---|
| I1 闭合恒真 | 采纳（拆两时钟 + 注入可证伪） | AC6 | 「`Σ_backend_pages` vs `Σ_harness_pages` ≤2%（同粒度）+ 注入 δ → 残差变化 ≈ δ」 |
| I2 k=1 效率恒真 | 采纳（注入式单测 + 系统比值另判） | AC8 / AC1 | 「fixture 验证公式；系统级比值由 AC1 判」 |
| I3 云门④作为 AC 恒真 | 采纳（改前置机检，FAIL 非 N/A） | AC12 | 「脚注未含指定实例 + 镜像/DTK → 数值 AC 判 FAIL」 |
| I4 AC1 可被现状满足 | 采纳（数字必须出自模块产物） | AC3 | 「模块侧数字出自 `speed --out` json」 |
| I5 AC5 是既有行为且前提冲突 | 采纳（定义命中集合 + 认识 `skipped_*`） | §5.3 / AC9 | 「仅 `hit_before_slot` 计命中；`skipped_*` 不得判 FAIL」 |
| I6 AC9 bar 太低 | 采纳（点名反向用例 + 失败名集合相等） | AC16 | 「点名用例名出现在 pytest 输出；失败名集合相等」 |
| I7 缺席断言可能误杀 | 采纳（改行为断言） | AC15 | 「以行为差异断言，不以键的有无断言」 |
| I8 AC6 过严 / 可两不写 | 采纳（`null`/`not_applicable` 允许 + 配对） | AC11 | 「真卷无数值质量 + `no_gt:true`；合成卷条件式 F1」 |
| I9 阈值需可证伪 | 采纳（注入实验） | AC6③ | 「注入 δ → 残差变化 ≈ δ（±1ms）」 |
| I10 AC11 不判且虚增覆盖 | 采纳（P2/P3 移出表） | §8 表尾 | 「P2/P3 条款不进本表」 |

### 11.3 第一轮：建议补充 R1–R15（**逐条映射，v3 补齐**）

| 项 | R 原文要点 | AC | 判据句（锚） |
|---|---|---|---|
| R1 CLI 面 | `speed` 子命令存在，缺参非 0 | **AC4** | 「`--exam/--driver/--arm/--out` 缺一即非 0；未知 driver/arm 非 0」 |
| R2 k 生效 + 链路写明 | `--k 2` 逐页并发回显；写明走哪条链路 | **AC5 + §4.1 + AC23 + AC24** | 「`effective_k=2` 且并发峰值 = 2（双证）；链路与请求载荷写死」 |
| R3 报告自足性 | 仅凭 json 重建配置；删项 → `incomplete` | **AC13** | 「arm/k/warmup/rounds/缓存策略/卷 hash/载荷 hash/code rev/时间；删任一项 → `incomplete`」 |
| R4 n≥3 与失败轮 | 3 轮中位 + IQR；失败轮排除 | **AC10** | 「人为 kill backend 一轮 → 标 `invalid` 并排除，不得静默取 2 轮中位」 |
| R5 残差可证伪 | 注入 δ → 残差 ≈ δ；带来源标注 | **AC6②③** | 「`unclassified_source:"derived_from_residual"`；注入变化 ≈ δ（±1ms）」 |
| R6 no_gt 强约束 | 真卷无数值 F1/P/R；合成卷有 F1 | **AC11** | 「真卷全树无数值质量 + `no_gt:true`；合成卷条件式（`quality_eval:"not_run"` 或数值 F1）」 |
| R7 环境脚注（六项→八项） | 六项逐一删除报 `incomplete` | **AC12** | 「八项逐一删除 → `incomplete` 且拒出报告；第 1/3 项禁 N/A」 |
| R8 patch 臂还原 | 跑后 `git status` 空；跑前 dirty 中止 | **AC14** | 「还原失败必须报错而非静默」 |
| R9 健康等待/重启归属 | 记录实测等待与静置 | **AC12 第 8 项 + §5.2** | 「报告记录 `/health` 就绪耗时与静置秒数实测值」 |
| R10 异常输入 | 缺清单/schema/空 items/k 非法/`--out` 不可写 → 非 0 | **AC19** | 「不得产出『零页全绿』报告；加密卷与 `expect_reject` 对齐」 |
| R11 极端规模与清理 | 67 页卷；中断后清理上传文件 | **AC20①②** | 「逐页行数 = 67；SIGINT → 零残留；SIGKILL/kill backend → 标 `orphan_possible` + `--cleanup` 清到 0」 |
| R12 部署链路三条 | compose 直连 / nginx / 云 DCU 各 smoke | **AC20③** | 「batch 线必须单独过 nginx；报告记录实际 `api_base`」 |
| R13 泄密 | 新增产物不泄真卷路径/IP；真卷清单留私有 | **AC17** | 「自建扫描器（IP/绝对路径/denylist）命中退 1」 |
| R14 口径收敛 | 与 `benchmark_throughput.py` parity 或明弃用+指针 | **AC18** | 「**必须** parity ≤20%（恒真弃用分支已删）；清点 `scorers/speed.py` 复用」 |
| R15 前置机检 | canary 无文本层机器断言 + 反例卷 | **AC2** | 「锚点卷 `pdftotext` 为空且 `is_scanned=True`；反例卷必须不满足」 |

### 11.4 第二轮：S1–S9 处置

| 项 | 要点 | 处置 | AC |
|---|---|---|---|
| S1 | AC6 两时钟粒度未写死（k=3 恒 FAIL）+ `total_backend` 来源键未写死 + `include_result_image` 污染 | **采纳**：同粒度 `Σ_pages` vs `Σ_pages`；来源写死为根 `duration_ms["total"]`（`vision_service.py:532`）；`include_result_image=false` | AC6 / §4.1 / §4.2 |
| S2 | AC7「prepare 必非零」对文本层卷恒 FAIL | **采纳**：非零性按实际路径限定；`draw` 只断言键存在 | AC7 |
| S3 | AC1②/AC23 跨仪器装置未写死（`utils.ts:318` 不存在、探针不在树、载荷未写死、`include_result_image` 未写死） | **采纳**：装置写成 AC23 六项机检；载荷写死为锚点装置同款并加 AC24 同一性断言；行号订正为 `timing.ts:57` | AC23 / AC24 / §4.1 |
| S4 | AC3 与 AC1② 自相矛盾（探针数字按定义不出自 `speed --out`） | **采纳**：模块侧/探针侧数字分列归属 | AC3 |
| S5 | AC18 恒真出口 + 漏清点 `scorers/speed.py` | **采纳**：删「或弃用」分支；新增复用清点与包名避让（`speedkit`） | AC18 / §2.1 / §9 |
| S6 | AC17 判据不可执行（`leak_check.py` 扫不了 JSON） | **采纳**：改自建 `check_report_redaction.py`；把能力边界写入 AC | AC17 |
| S7 | AC20② 不可满足（kill 后端时驱动发不出 DELETE） | **采纳**：三条中断路径分开判（SIGINT 零残留 / SIGKILL 标 `orphan_possible` + `--cleanup` / kill 后端标 `invalid`） | AC20② |
| S8 | 缺 AC：质量旁证结构化内置（Issue 清单第 7 条）、键归位守卫；另建议 AC22 | **采纳**：新增 AC26 / AC21 / AC22 | AC21 / AC22 / AC26 |
| S9 | AC15 加最小页数；AC16 加测试文件/用例名存在性；AC12 加强判别力；AC11 改条件式 | **采纳** | AC15 / AC16 / AC12 / AC11 |

### 11.5 记录在案

- **技术性反驳（第一轮 R2 的前提）成立**：Issue 初稿硬约束 3 要求把 `timing.ts:57` 编译期常量变 CLI 参数 ——
  Python CLI 运行时改不了前端编译期量；**该措辞是我方 Issue 的错误，已订正**（现正文为「并发由本模块驱动实现，
  不改前端」）。故 `single` 线 k 由本模块驱动实现，前端线只作**对照仪器**。
- **第二轮评审的两处「未核实」**：① 第一轮 D/I/R 原文未随材料给出（本轮已归档原文并补齐逐条映射）；
  ② `default_ocr_has_types` 与探针 19 类是否同集 —— **本轮以「写死载荷 + AC24 同一性断言」消解**，
  不再依赖「默认集恰好相同」。

**已核实的事实基线**（本轮修订逐条在代码/记忆中确认）：
`e2e_exec` 全仓零命中；`report.py:11-27` 仅硬校验三项；`cli.py:79,93` 只有 `run`/`run-e2e`；
`utils.ts:310/321`（`:318` 为注释，`? 1 :` 已删）；`timing.ts:57 = 3`；
`ocr_has_vision_service.py:358`（文本层 `prepare`/`ocr` 恒 0）与 `:407`（`draw` 恒 0）；
`redaction.py:179-180,189,195-197`；`e2e.py:242-243,265-266`；
`has_text_cache_status` 七态；锚点卷 `pdftotext` = 0 字符 / 反例卷 8666 字符；
实例 node v22.14.0 + `frontend/node_modules` 在、私有仓**未同步**到实例、实例仓无残留探针；
两轮评审原文已归档私有仓 `LexMask-workspace/docs/acceptance/`。