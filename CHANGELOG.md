# CHANGELOG

## 2026-10-16

- feat(eval-ng): 新增**速度测量模块**（`eval-ng/cli.py speed`）——把「端到端案卷速率」从一次性脚本变成可复跑、可出数、可机检的评测底座（#131，PR #133）。
  - **两条测量线**：`--driver single`（Python 驱动逐页 `/redaction/{id}/vision`，页间并发由运行时 k 控制）与 `--driver batch`（建 `smart_batch` 作业 → 上传 → 提交 → 轮询，逐页仪表取自作业详情），两侧都出**页线**（页数/分钟、逐页墙钟 p50/p95/IQR）与**卷线**（提交→末页完成的 `file_wall_s`）两组数字。
  - **判定面（四态 PASS/UNVERIFIED/BLOCKED/FAIL）**：D2 并发自证（驱动声明 k + 在飞峰值实测 k 双证）、D3 闭合校验（环和 vs 页总**同钟** ≤2%、残差双侧 0≤·≤10%、两序列**非同源**且比值 ∈[1.00,1.25]、键路径压平后必含五个环叶子）、D4 自足（`file_wall_s` 与客户端墙钟互证 ≤5%、md 与 JSON 逐项一致）、D5 脱敏（八项环境脚注硬校验、实例指纹四键 + sha256、数值字段白名单**路径级**表、质量子树判 FALSE）、D6 边界（非法轮排除、载荷同一 `force=true&include_result_image=false`、批作业经网关 ≥8 MiB 单文件）、D8 旁证（计数类只累加、字符串状态不丢、补丁生效断言必须真在文件里）。
  - **名实不一即 FAIL**：`D8.patch_effect` 要求臂声明的 `patches[].to`/`expect[].contains` 真出现在目标文件里；`D5.numeric_whitelist` 按**字段路径**放行（`summary.agg_value` 这种换名会被点名），质量字段换名也走不脱。
  - **新增公开树扫描器** `eval-ng/scripts/check_public_tree.py`（与报告脱敏扫描器同族，含**阳性对照自检**）：公网 IP / 绝对路径 / Windows 盘符路径 / 凭据形态 / `--denylist` 词表五类；默认只扫 `--base` 以来改动过的文件，行内 `public-tree-scan: ignore-line` 可显式放过（扫描器自己的阳性对照夹具用）。
  - **单测 128 条**（`backend/tests/test_evalng_speed.py` 120 + `backend/tests/test_evalng_cli.py` 8；eval-ng 测试文件合计 **183** 条（`--collect-only` 实测）；零网络：`SpeedApi` 构造在单测里被替换成抛错版本，AC24 请求捕获用 `httpx.MockTransport`）；④ 云门实测又钉掉三处死角：`_hardware_capture` 取设备身份行而非 `hy-smi` 表头、`dedupe_checks` 同 id 按四态取最严、k 双证按 `min(声明 k, 该轮页数)`（页数足够仍达不到声明值依旧 FAIL）；eval-ng 全量绿；本机全量 `pytest` 失败名集合与 clean base 逐条一致（无回归）。
  - ④ 云实例端到端实测（10 页扫描 canary / 3 页真实案卷切片 / 8.3 MiB 单文件（8,703,181 B 的 1 页填充分片）过网关；实例：云 DCU 单机 + `SINGLE` 起 uvicorn，后端 `68f0e7ba`，仪器 `de197b74`–`f1f249c`）：
    - **D1 锚点复现**：`single` 线冷跑各 4 轮（弃第 1 轮取 2–4 中位）——k=3 **92.594s**（离散 10.1%，锚点 86.65s ⇒ +6.9%）、k=1 **207.468s**（离散 2.9%，锚点 198.13s ⇒ +4.7%），**`D1.anchor_ratio` = 2.241× ≥ 1.8× PASS**；两臂闭合/残差/跨钟/键路径/冷跑证据（pid 与 `start_time` 三轮互异）/并发双证全 PASS，判定 UNVERIFIED（无 FAIL；未项只有 env 臂的 `D8.patch_effect` 与无基准的 `D7.parity`）。
    - **D6④ 网关**：实例装不上真 nginx（无包网 + SCNet 禁 Docker）、本地 registry 不可达 ⇒ 用 stdlib **L4 原字节转发代理**（`equiv_proxy_l4.py`，只窥视每连接首个请求头做上限与记账）并声明偏差；两侧证据——**A 不限体量：8,703,181 B 的单文件经网关 `POST /files/upload` 200**（请求体 8,703,363 B，multipart 开销 182 B；0.68s）、**B 上限 1 MiB：同一请求 413 且不转发上游**（`{"detail":"请求体 8703363 字节超过反向代理上限 1048576 字节"}`，记账 `rejected_413=1`）。
    - **D9 真实案卷（67 页证据卷前 3 页切片、纯扫描、文本层 0 字符）**：`single` k=3 cold —— 逐页 p50 **19.09s**、`file_wall_s` p50 **24.538s**、闭合 0.001556 / 残差 0.001556 / 跨钟 1.041956 全在阈内、无质量数值、脱敏扫描 clean 且案名自检 0 命中；抽数后真卷报告已删（不留私有产物）。
    - **D7 与旧基准脚本对照 → PASS（原 BLOCKED 已闭环）**：批线曾因 `smart_batch` 在飞入队后 worker 不取件（提交后长期 `queued`、`processing recognition` 0 次，**只有重启恢复路径**能处理）而拿不到数——已另立 **Issue#137**（根因：Python 3.10 下 `asyncio.TimeoutError` 不是内建 `TimeoutError` 的子类 ⇒ `_worker_loop` 的 `except TimeoutError` 捕不到首个 2s 空闲超时 ⇒ worker 静默退出），修复 PR#138 经三轮独立冷评审后合入 `bf38eaad`。修复后两侧**均冷跑**、10 页合成扫描卷、同窗口对照：`pages_per_min` 相对差 **7.9%**（4.226 vs 4.59）、页 p50 相对差 **1.4%**（3489.5 vs 3540 ms），均 ≤20% ⇒ **`D7.parity` PASS**。（更早一次 33.8% 的 FAIL 系基准侧未冷跑、两侧温冷态不同，属对照装置不公平，不作判据。）⚠️ 云侧产物（instrument rev `f4b6acba`、`scnet-main`）本机两仓不可解析；口径与私有验收方案 §9.12 一致。
  - **批线账本根同作用域订正（④ 云门 D7 期实测发现，2026-10-16 补）**：批线逐页 `duration_ms`（外层）含槽位等待/装配等账外墙钟，与各管线 `<prefix>.total` 不同作用域 ⇒ 页根改取 `duration_breakdown_ms["total"]`（外层墙钟单列 `page_wall_ms`、跨页累积账单列 `other_breakdown_ms`，两者**只记录不入和**）；新增机判 `D3.root_scope`（作用域不对 ⇒ FAIL 并点名后果）。**阈值一处未动**：真实两页账目上改前闭合差 3.66%（>2% 必 FAIL）、改后 1.84% PASS；5 条新单测共存，把根换回外层墙钟即 4 红（改前红自证）。
  - **③ 第三轮冷评审处置**（裁决 REVISE；代码层无阻塞缺陷、无恒真判据，阻塞面在验收闭包）：新增 **`D1.corpus_premise`**——AC2/D1④ 从「只记录」变成**机判**：声明 `scanned_pdf`/`text_pdf` 必须与**本地 `pdftotext` 非空白计数**及**后端 `/parse` 的 `is_scanned`** 两侧一致，任一侧与声明不符 ⇒ FAIL，取不到某一侧 ⇒ UNVERIFIED（不猜 PASS）；夹具里的实例目录名字面量换成中性示例路径（不再靠 `ignore-line` 放行真实实例路径）；单测口径订正为 **128 条**（speed 120 + cli 8；eval-ng 测试文件合计 **183** 条，均 `--collect-only` 实测——含 R3/⑤ 补的扫描器判据与装配级接线用例，以及 ⑥ 补的**复合键 / 值形状**两组扫描器判据）。
    - **门③ 第六轮处置（凭据面）**：公开树扫描器的口令识别从「键名 `\b` 正则」改为「键名按 `_`/`-`/camelCase 切词元查词表 + 值形状 + 语境豁免」，从而覆盖 `api_secret` / `db_password` / `clientSecret` 这类**凭据词作后缀**的复合键（旧 `\b` 在 `_` 前后不成边界，整类漏网）；同时收紧值形状（分隔符 / JSON 片段 / CJK 文案 / CamelCase 类型名 / ID 形态一律不算口令），整树噪声由 18 处降到 **1 处预存示例**；口令面改为 `check_public_tree.credential_findings()` 单一真源，报表扫描器同源导入；补两条 pytest 判据（复合键必中 + 元数据/值形状必净），变异 M-S2/S3/S4/S5/S6 全 KILLED。
    - **口令外置的迁移声明（公开树零口令，G4）**：`--api-pass` 默认值不再内置任何口令字面量，改读环境变量 `EVAL_API_PASS`（`eval-ng/cli.py` 两处 + 范围外既有 `eval/scripts/{leak_check,run_eval,build_pseudonym_set}.py` 四处）；未导出该变量时这些入口的 `--api-pass` 为 `None` —— 旧 `eval/scripts/*` 的既有调用方须显式导出 `EVAL_API_PASS` 或继续传 `--api-pass`（既有行为只在「依赖内置默认值」这一点上改变，故单列声明）；整树扫描余 2 处**预存**凭据类痕迹另立 Issue#140。
    - 小数据跑法是用户 2026-10-16 的明确指示（「只验可行性，不做整卷稳定性」）：**只改跑法，阈值一处未放宽**；轮次少 ⇒ `D2.timing_rounds` 落 UNVERIFIED 属设计使然。

## 2026-10-15

- perf(config): 内嵌文本层文档级链改为**默认开启**（`TEXT_LAYER_DOCLINE_ENABLED` 默认 `True`）——#115 合入时该链默认关闭以保零行为变更；云实例在同一台机器、同一次会话、同一份 60 页真实案卷上对照：页线 403.31s（6.72s/页、OCR 边车 58 次、HaS 调用 394）→ **文档级线 181.53s（3.03s/页、OCR 边车 0 次、HaS 调用 191）= 2.22×**，召回与页线持平（10 页 GT canary 101/121，类型完全一致 86→91），189 页整卷建卷 231s、零 400/OOM、`/health` 全程可用（#129）。
  - 默认开启后的代价已写入配置注释与 `.env.example`：**任一页首次请求须等整卷分块建流**（60 页约 115–171s、189 页约 231s，memo 命中后逐页秒级）；同一实体每个出现位置都出框（产物变重，用户已明确暂不收敛）；无可用文本层的页 / 卷级门不达标 / 建卷异常一律逐页回落旧视觉线，不 5xx。
  - **建流预算闸**（门⑥ 冷评审 Important I1）：整卷建流在首个请求里同步跑完，页数过多会超过网关 `proxy_read_timeout` 而 **504**（且该路径不经过任何回落）——新增 `TEXT_LAYER_DOCLINE_MAX_BUILD_SEC`（默认 600s ≈ 375 页，超预算的卷**直接不接管**、逐页回落旧线）与估算速率 `TEXT_LAYER_DOCLINE_BUILD_SEC_PER_PAGE`，并把 `frontend/nginx.conf.template` 的 `proxy_read_timeout/send_timeout` 由 400s 抬到 **900s**（原值按 189 页 231s + 余量已逼近 330 页卷的临界）；`deploy/RUNBOOK.md` 新增第 6 节记录开关、首屏代价、日志特征与排查顺序。
  - 新增 2 条单测：钉住字段默认值（断言 `model_fields[...].default`，避免被存量 `.env` 影响）、预算闸短路（超预算时模型一次都不被调用）。

- perf(perception): 扫描件内嵌文本层改走文档级链路，同 k=8 下 60 页真实卷 3.65×–3.87×（页线 679.40s/719.41s → **186.08s**，OCR 侧车调用 60 → **0**）——根因是 `backend/app/services/file_parser.py` 把「整页图覆盖 ≥0.9」的页直接判为扫描件并**清空已抽好的文本层**，`vision_service._detect_with_pdf_text_layer` 更主动抛 `scanned page with embedded OCR text layer`（真实案卷可判定子集中 89% 自带干净内嵌层）；新增 `text_layer_quality`（三档分类器）、`text_layer_docline_service`（约 2000 字不跨词分块 + 文档级 HaS + NFKC 归一后回映到 (页号, 框)）与 `text_layer_doc_source`（per-file memo 单飞构建、按 key 加锁），**默认关闭**（`TEXT_LAYER_DOCLINE_ENABLED=false`），分类器不合格/建卷异常/截断超阈一律回落旧视觉线；10 页 GT canary 召回与页线持平（101/121，共同漏的 20 条均为 `人民币XXX.00元` 金额形态），类型完全一致 91 vs 86；同实体每个出现位置都出框（88→318 框/页）系隐私侧更完整、产物更重的既定取舍（#115，PR #127）。

- docs(perf): 撤回 v1.0 无实测支撑的性能数字并给出可核验替代口径（#116，PR #117）——白皮书 v1.0→v1.1：撤回「29.2 文件/分钟」「1–3 秒/份」「1.5–3 秒/页」「双卡 RTX 5090 生产拓扑」等读数（硬件与管线双变量，在任一实例均不可复现），删 `docs/测试报告-v1.0.0.docx` 孪生件；替代口径如实录入云 DCU K100 实测——单文件文本型 78 页/45,408 字 **32.42s**（36 次 HaS 调用 / 1,468 实体）、短文本 **7.90s**、扫描件单页中位 **27s**；单页阶段分解 `ocr_has` 均值 **72.05s**（OCR **4.21s** 占 5.9% + HaS NER **67.52s** 占 94.1%）+ 视觉特征 **6.29s**（759 页真实案卷台账）；整卷页并发 k=1→3 使 67 页证据卷 **1442.0s→485.0s（2.97×）**、10 页扫描 canary **198.13s→86.74s（2.28×）**、3 页 **61.93s→27.42s（2.26×）**（单页 reported 成本三臂 21.5/22.4/21.6s/页、均值 21.8s，收益来自页间重叠）；端到端真实案卷 **1.9 页/分钟**（35 稳态页 1,114.24s ≈ **31.83s/页**）、合成样张卷 **3.9–4.6 页/分钟**；删不可复现的「单页约 23 秒」。新增 `MAX_HAS_CHUNKS = 12` 覆盖限警示（78 页卷仅处理约 42% 文本，解帽改造立项 #115）。逐格出处落 `eval/reports/2026-10-15-dcu-perf-evidence.md` 与 `eval/reports/2026-09-15-format-matrix.md`（原私有矩阵报告去标识入公仓）；白皮书 §3 增「各小节测量时点不同」标注——§3.4 为 2026-09-15 早期构建、§3.5 为 2026-09-14 `705870d`，§3.1–§3.3 为 2026-10-15/16 `bdc1f23` 线，四者**不得**表述为「同一批实测」；规矩：对外性能数字必须带环境脚注（实例 + 版本 + 口径）。

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
