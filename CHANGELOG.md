# CHANGELOG

## 2026-10-15

- perf(config): 内嵌文本层文档级链改为**默认开启**（`TEXT_LAYER_DOCLINE_ENABLED` 默认 `True`）——#115 合入时该链默认关闭以保零行为变更；云实例在同一台机器、同一次会话、同一份 60 页真实案卷上对照：页线 403.31s（6.72s/页、OCR 边车 58 次、HaS 调用 394）→ **文档级线 181.53s（3.03s/页、OCR 边车 0 次、HaS 调用 191）= 2.22×**，召回与页线持平（10 页 GT canary 101/121，类型完全一致 86→91），189 页整卷建卷 231s、零 400/OOM、`/health` 全程可用（#129）。
  - 默认开启后的代价已写入配置注释与 `.env.example`：**任一页首次请求须等整卷分块建流**（60 页约 115s、189 页约 231s，memo 命中后逐页秒级）；同一实体每个出现位置都出框（产物变重，用户已明确暂不收敛）；无可用文本层的页 / 卷级门不达标 / 建卷异常一律逐页回落旧视觉线，不 5xx。
  - 新增 1 条单测钉住默认值（改动前该断言必失败）。

- perf(perception): 扫描件内嵌文本层改走文档级链路，同 k=8 下 60 页真实卷 3.65×–3.87×（页线 679.40s/719.41s → **186.08s**，OCR 侧车调用 60 → **0**）——根因是 `backend/app/services/file_parser.py` 把「整页图覆盖 ≥0.9」的页直接判为扫描件并**清空已抽好的文本层**，`vision_service._detect_with_pdf_text_layer` 更主动抛 `scanned page with embedded OCR text layer`（真实案卷可判定子集中 89% 自带干净内嵌层）；新增 `text_layer_quality`（三档分类器）、`text_layer_docline_service`（约 2000 字不跨词分块 + 文档级 HaS + NFKC 归一后回映到 (页号, 框)）与 `text_layer_doc_source`（per-file memo 单飞构建、按 key 加锁），**默认关闭**（`TEXT_LAYER_DOCLINE_ENABLED=false`），分类器不合格/建卷异常/截断超阈一律回落旧视觉线；10 页 GT canary 召回与页线持平（101/121，共同漏的 20 条均为 `人民币XXX.00元` 金额形态），类型完全一致 91 vs 86；同实体每个出现位置都出框（88→318 框/页）系隐私侧更完整、产物更重的既定取舍（#115，PR #127）。

- perf(vision): 扫描件单文件识别提速 2.28×（10 页扫描件 canary，改前 198.13s → 改后 86.74s，真实前端代码路径 + 真实后端计时）——删除 `runVisionDetectionPages` 中「勾选视觉特征即把页间并发强制压成 1」的特化（`frontend/src/features/playground/utils.ts`），页面并发回到 `PLAYGROUND_VISION_PAGE_CONCURRENCY=3`；同轮 HaS 模型调用数 40→40、逐页识别框在 ±1 框架噪声带内（并发可见性另由后端日志证得改前区间重叠 1 / 改后重叠 3）；并同步补失败/取消语义（`stopRequested` + `onPageComplete` 的 abort 守卫）与请求体转发断言（#106，PR #113）。

- perf(ner): 单文件文本型识别提速 1.64×（78 页/45408 字卷，三轮中位 53.23s→32.42s）——HaS 三闸门默认 4→8（`HAS_NER_MAX_PARALLEL_REQUESTS` / `HAS_NER_GLOBAL_MAX_INFLIGHT` / `HAS_NER_VERIFY_PARALLEL_CAP`），NER 结果缓存 300s/256→1800s/2048；帽容量分批决策从 `HaSClient.ner` 内部（同步串行、整串占同一 GPU 闸位）上移到 `has_service` 派发前（`plan_completion_batches` 单一真源），低块数短文档另获 1.95×（15.42s→7.90s）；实体数与 HaS 调用数逐轮不变（36 次调用 / 1468 实体），实体集差异限于臂内噪声带；部署链三闸门口径统一到 8 或显式钉住既有等效值（#103，PR #107）。

## 2026-10-06

- feat(gt): eval/ 新增 GT 预标引擎（Issue#56 M1/M2）——统一转录接口（云 v6/VL+本地 vl-md）、归一化双面 span、正则+NER 实体通道、R1-R7 仲裁、GT schema 校验、逐页流水线 CLI、A1/A2 合成集验证闸、选卷矩阵脚本。

## 2026-09-30

- docs(workflow): 开发流程修订——七道门改八道门，新增 ①.5 验收方案独立评审门（独立 AI 只拿 Issue 正文+验收方案全文冷审，负样本检验打回恒真断言，修复"设计者=出题人导致验收方案永远通过"）；Step 1 立项改为构建 Issue（吸收 ⓪ 需求澄清）；⑥ 独立评审 skill 化（requesting-code-review 派发 + receiving-code-review 处理 + 回归副作用清单）；合并放行改预授权制（人工验收类保留二次确认，AI 自验收类汇报后即合）（PR #34）。

## 2026-09-29

- fix(upload): 请求体上限中间件跟随 `MAX_FILE_SIZE`（0=非 JSON 不拦截，配置时 +10MB multipart 开销）——原 `MaxBodySizeMiddleware` 硬编码 60MB 与配置脱钩，是 #10 清理时漏网的第六处限制（不经 settings、grep 不可见），用户以 100MB 实测单文件整包上传秒 413 暴露；JSON 1MB 上限不变，断点续传 5MB 分块路径本就不受影响。实例实测：本机回环整包 100MB→200(0.75s)、隧道分块 100MB 全链路→complete 200；新增中间件三分支单测（#27）。
- docs(workflow): 恢复 preview 分支线（#20 方案 B，用户拍板）——§7 分支模型改为 功能分支 base=preview → squash 合入 → 晋级 main 打 tag；Step 3/5/6/7、§5 命令、门禁规范 §0⑥/§5 措辞同步；AGENTS.md、协同开发规范对齐；ci.yml base-guard 即按此模型执行，无需改动。preview 分支已自 main(2655be7) 建立（#20，PR #22）。
- docs(workflow): 设计阶段(Step 2)新增「验收方案(谁来验收:AI 自验收/人工)」「影响面/环境计划(只起影响面内服务+端口段规划)」必填项;门禁规范 §0/§1/§4/§5 配套修订验收人分级——AI 自验收(浏览器/API 实测逐条留证)通过后直接进独立 review,人工验收流程与 merge 放行权不变,未写方案按人工从严(#16,PR #19)。
- feat(upload): 去除单文件 50MB 上传限制——`MAX_FILE_SIZE` 默认 0=不限制（环境变量可恢复上限），整包/断点续传/inbox/SFTP/DICOM 各校验点改为「配置上限才校验」（顺修 DICOM `max(1,0)` 把不限量卡成 1 字节的陷阱），前端 playground/batch 两处 dropzone `maxSize` 与 50MB 文案移除，断点续传单测改「默认不限+可配上限」双例，用户文档与 .env 注释同步（#10，PR #11）。
- fix(cloud-deploy): bootstrap.sh 在拉起 frontend 前补 dist 构建守卫——同步部署（bundle clone，镜像保存点无 dist）后 vite preview 对空目录起服务，静态页整站 404 而 /health 走代理仍 200，健康检查全绿掩盖问题（#7，PR #8）。实例实测：构建后 / 由 404 恢复 200；构建日志落 `$LOG/frontend-build.log`。DCU 实例持久卷 `start_all_dcu.sh` 同步打同款守卫（私有运维件）。
- fix(cloud-deploy): 清除 4 个脚本的行首 `\x01` 脏字符（start_cloud.sh #14/PR #15；setup_cloud.sh、setup_dtk.sh、start_dtk.sh #17/PR #18）——行首 `\x01` 使 `#` 失去注释作用，运行时报 command not found。全目录扩展口径控制符扫描（C0/DEL/C1/零宽/双向/BOM/NBSP）零命中。
