# CHANGELOG


## 2026-11-12

### 修正（Issue#281）供料产物两处替换缺陷：跨页切开的实体漏替换 + 标签叠字

- **为什么**：本地真卷冒烟发现两处「能读但会误导」的缺陷。①实体原文被页边界（页脚数字 + `<!-- page N of M -->`）切成两半时，产物侧不存在连续形态，字节级 replace 与空白柔性正则都匹配不到；更隐蔽的是同一事实的 OCR 空白变体项（更长的 flex 项）先替换掉唯一连续形态后，被切开的项既「不缺席」（别处有连续形态）也「不残留」（切开形态不是逐字原文），前向覆盖闸与零残留闸双假绿，真实案号随产物出厂。②OCR 原文自带类型短名（「案号（2023）…号」）而占位符是「案号N」，替换后成「案号案号N」叠字，污染下游字段。
- **改了什么**（`backend/app/services/agent_md_replace.py`，纯逻辑）：
  - `_apply_page_split`：对每个非 excluded 映射项枚举原文被页注释切开的全部切分点，构造「head 字符序列 + 页边界噪声 + tail 字符序列」正则（字符间 `\s*`，与 flex 同口径），替换时**把页边界噪声原样回插**——页脚数字与页注释必须保留，否则结构对账判页标记序列变化；覆盖闸/残留闸同步纳入该形态（已落地项不再按「原文连续出现」判缺席或残留）。
  - `_collapse_label_duplication`：replacement 形如「短名+序号」且叠字真实存在时收口为单个标签，映射表 replacement 不变（审阅口径不漂移）；legacy 拍平重建路径同样生效。
- **测试**：新增 `backend/tests/test_issue281_pagesplit_label.py` 6 例（跨页切开 + 空白变体互吃 + 仅切开形态 + 页脚/页注释保留 + 结构化/legacy 两路叠字收口 + excluded 不动）；后端全量 2922 passed / 18 failed（与 preview 基线同集合，容器信号量权限问题）。
- **本地真卷验证**：同一 6 页扫描件（同一映射 30 条）用修复后代码重跑——跨页切开的真案号落地为「案号4」（修复前 0 次、原样残留）、「案号案号5」收口为「案号5」、页标记 6/6 且结构对账通过；映射表与保留清单与修复前逐字节相同（只改产物 md）。

### 修正（Issue#281 续）供料产物：Markdown 转义/全角失配整卷拒产 + 短数字项吃掉页标记

- **为什么**：五卷真卷验收（6/6/2/78/86 页）暴露两处「表示失配 / 高频命中」缺陷，均导致整卷拿不到交付。①官方结构化 md 对检测面原文做了 Markdown 转义（`wxid_fixture0000001` → `wxid\_fixture0000001`）并把全角 `：`/`）` 半角化，字面与空白柔性双失配 ⇒ 前向覆盖闸判 `representation_mismatch`、整卷 fail-closed；②扫描卷 NER 把页码片段误判成 `CASE_NUMBER='10'/'11'`，字节级 `replace` 命中 `<!-- page 10 of 86 -->` 的页号 ⇒ 页标记序列 86→84、结构对账失败。
- **改了什么**（`backend/app/services/` 纯逻辑）：
  - `agent_md_replace.py` **折叠视图**：`_fold_md` 生成「去 `\`+ASCII 标点转义 + 全角 ASCII 折半角」副本并返回折叠下标→原文下标（前置转义符一并回退），`_fold_flex_pattern` / `_fold_spans` / `_fold_matches` / `_replace_fold` 在折叠副本上匹配、回原文按区间倒序替换；覆盖闸、残留闸、legacy `render_outputs` 兜底同步接入。
  - `agent_md_ner.py` 默认 excluded 扩为「单字符或 ≤3 位纯数字」原文（映射表可见、律师可改）；渲染层 `degenerate_original` 硬闸同步扩容。
  - `agent_md_replace.py` **页标记护甲**：替换与闸门期间把页标记换成私用区哨兵（`\ue000`+字母序号+`\ue001`，不含数字），覆盖闸/主循环/叠字收口/残留闸只认正文，返回前逐字节还原；覆盖与残留闸因此不再把页标记里的同形文本当证据。
- **测试**：`backend/tests/test_issue281_pagesplit_label.py` 追加 5 例（转义态、全角态、转义+空白叠加、裸 ID、真失配仍 fail-closed）与 5 例（短数字排除、渲染层硬闸、护甲往返、页标记内同形原文不被替换、正文邻页标记替换），共 16 例；后端全量 2932 passed / 18 failed（与 preview 基线同集合，容器信号量权限问题）。
- **模型召回兜底**：`agent_md_ner.py` 新增 `regex_pii_entities` / `merge_regex_entities`——身份证号 / 手机号 / 6222 银行卡号 / 微信 ID 四类正则补层（与 vl_md 管线同口径），仅当 NER 有「会真正打码」的实体完整包住命中时才丢弃（真卷 1 字符 `CASE_NUMBER:'4'` 压在身份证号首位的场景已补回归测试）；`SOCIAL_ACCOUNT` 占位短名为「社交账号」。
- **本地真卷验证**：五卷（6 页文本 / 6 页扫描 / 2 页 / 78 页文本 / 86 页扫描）全部 completed；页标记 6/6、6/6、2/2、78/78、86/86；映射残留均为 0；78 页卷有 2 处微信 ID OCR 变体未被 NER 召回（检测侧召回边界，非替换层缺陷，另案）。

### 增强（Issue#257 一期之三·需求重对齐）单文件任务也能选页导出：页格嵌在文件处理页 + 范围输入 + 去掉失败页勾选

- **为什么**：一期上线后用户实测反馈三点——①选页导出只挂在批量任务上，单文件流程（`/single`）根本没有入口，而用户的真实用法就是「先跑一个文件」；②失败页要先勾一个「我已了解」才能导出，一个红勾比失败页本身还刺眼；③200 页里有 150 页要导出，只能一格一格点。重对齐后的需求：单文件就有、运行中就能看到哪一页有问题且不可选、不要勾选框、必须支持页码范围。
- **改了什么**：
  - **单文件页格**：新增 `PlaygroundPageExport`（逐页结果 + 三态计数 + 选页导出 + 导出记录），挂三处——识别中浮层（`compact` 默认收起）、复核阶段「逐页结果与选页导出」弹窗、结果页「处理详情」下方。解析链 `useFileJobItem`：provider 账本 → 会话内 sticky → 草稿 `taskJobId` → **新端点 `GET /api/jobs/items/by-file/{file_id}`**（`job_store.find_item_ref_by_file_id`，按 `job_items.file_id` 反查最近一条、`owner_id` 越权即 404）——前三者都会在识别完成后被清空，没有这条兜底刷新页面页格就永久消失（旧 `ProcessingDetailsCard` 同样受此影响）。
  - **去勾选**：`failed_pages_ack_required` 门控删除——区间内失败页**自动排除**（`excluded_failed_pages` 随 202 返回并在说明页逐条列出页号与原因码），`acknowledge_failed_excluded` 参数仅为旧客户端兼容保留、不再影响判定；前端 ack 复选框换成一行提示「所选范围内有 N 页失败页（第 … 页）会被自动排除」。
  - **范围选择**：新增 `features/batch/lib/page-range.ts`——`parsePageRangeInput`（认 `100-150`/`100~150`/`100到150`/`1-20, 88`/全角逗号，越界夹取，`MAX_RANGE_SPAN` 防撑爆）、`extendSelection`（Shift 连选，反向同样成立）、`pruneSelection`（页转失败时从选择集摘除，不变时返回原实例）。页格工具栏加范围输入 + 「应用」+「反选」。
  - **导出记录摘要**：改为报包内页数（含说明页），标题跳页时逐页列举（修掉「等 9 页」被再套一层「页」的「等 9 页页」）。
  - 抽取 `usePartialExportSelection`（选择/提交/记录/任务轮询），批量对话框与单文件面板共用一套口径。
- **测试**：后端 +9 例（自动排除失败页两处、按文件反查五项契约、标题跳页/连续/单页三形态）；前端 +19 例（`page-range` 8 例含全角与反向 Shift、页格范围/Shift/prune 3 例、单文件面板 5 例含「无勾选框直接提交」与「底栏汇总不留占位符」、既有测试改口径）。全量后端 2916 passed / 18 failed（与 preview 基线同集合，容器信号量权限问题）；前端 498 passed。
- **本地实机验证**（`/single`，acc257 登录）：识别中浮层显示「成功 0 · 失败 0 · 未跑 10」并逐页转绿；复核弹窗范围输入 `3-6` → 选中 3、4、5、6，导出产物 5 页（说明页 + 4 页，说明页逐字列出「原卷第 3、4、5、6 页 / 共 4 页 / 原卷总页数：10」）；注入失败页后页格第 4 页红叉不可选、「已选 9 页（1-10）· 将排除 1 页失败页」、无勾选框可直接导出 9 页。

## 2026-11-11

### 修正（Issue#256）本地栈扫描线整卷误记全页失败：队列路径 LA 不可用整段跳过视觉识别 + AppError str 可读

- **为什么**：本地栈（无 LocateAnything）扫描件识别整卷误报「all N page(s) failed recognition」而 OCR 框/NER 实体实际全部产出（canary 10 页 22s 复现）。根因有二：①PR#235「LA 不可用不拦截识别」只在 playground 前端提交侧执行，API/队列载荷无此保护——队列两段式第二段（视觉特征识别）逐页进 `detect_vision`，每页撞 Issue#217 的 LA fail-closed 预检，`AppError(visual_service_unavailable)` 使全部页记入 `failed_pages`；②`AppError.__init__` 从未调 `super().__init__`，kwargs 构造（生产全部预检类抛点同款）时 `args=()` ⇒ `str(exc)==""`，页失败 reason 只剩「AppError: 」，错误码与指引全丢、排障全盲（「双账本分裂」表象 = deep-merge 账面残留第一段 completed 字段）。
- **改了什么**：①`_run_vision` 调度前探测一次 LA（勾选视觉要素时）：不可用 ⇒ 剔除视觉要素（空数组=显式不跑该段）+ `recognition.visual_detection_skipped` 结构化落账 + WARNING + 进度消息 `visual_detection_skipped_la_unavailable`，OCR+HaS 成果照常交付；预检自身意外异常不吞（fail-closed 不降级）；LA 在场（云端）行为零变化。②`AppError` 补 `super().__init__(message)`，`str(exc)` 从此携带 message（kwargs/位置参数两形态同口径）。
- **测试**：后端 +7 例（AppError str 两形态、两段式跳过全流程到 awaiting_review 且零失败摘要、单页剔除视觉要素、LA 可用两段式契约钉、预检意外异常穿透、LA 恢复重跑清陈旧跳过账——判据读 deep-merge 合并视图）；共享桩 `_vision_stubs` 默认 LA 可用（既有两段式用例不受新探测影响）。前端 +3 例（step3 识别消息映射抽纯函数 `lib/step3-recognition-message.ts` 后的契约钉，含视觉跳过终态与未知消息回落）。后端全量 2792 passed 零回归、前端 381 passed。

### 增强（Issue#257 一期之二）失败页单页重试 + NER 连接类错误改可重试瞬时

- **为什么**：大卷里单页失败（OCR/NER 超时、模型侧闪断）现在只能整卷重识别——200 页真卷一次十几分钟，用户实际动作是「先把红叉页补上再交」。另一条是事故教训：NEO001 网络闪断抛 `httpx.ReadError/RemoteProtocolError`，旧重试集合只认 `ConnectError/Timeout`，一次闪断就被判成确定性失败、进而整卷熔断。
- **改了什么**：
  - 新增 `page_retry_service`：`validate_page_retry` 把准入钉成一张表——仅 `awaiting_review`、仅视觉类（pdf/pdf_scanned/image）、仅失败清单内的页，识别中或已有在飞标记一律 409 `item_busy`；`submit_page_retry` 写序为 **状态→标记→投递**（先标记会造「标记在、状态未变」死锁；先状态则崩溃后回落整卷重识别，慢但正确）。
  - `task_queue` 增 `page_retry` 任务类型 + `RecognitionPipelineMixin._run_page_retry`：逐页 `vision_detect(..., merge_existing=True, force=True)`（**force 必须**，否则吃页级缓存假成功），页级超时/异常只记该页，成功后整体替换页记录并从 `failed_pages` 消账（`replace_paths` 保证旧 `error`/`stage_duration_ms` 清零），收尾清标记、重算失败摘要、回 `awaiting_review`，并把该页新框并入既有审阅草稿（旧框按页剔除）。
  - 重启补偿：`repair_stale_processing_items` 按 `recognition.page_retry_pages` 非空判 `page_retry`，不再把中断的单页重试当成整卷重识别。
  - NER 瞬时重试：`RETRYABLE_HTTPX` 增 `ReadError/WriteError/RemoteProtocolError/PoolTimeout`，**不**把 `TransportError` 父类拉进来（`ProxyError`/`UnsupportedProtocol` 是本地配置写错，重试只会掩盖真因）；`_classify_ner_error` 增 `read_error/write_error/remote_protocol` 观测桶。
  - 端点：`POST /api/jobs/{job_id}/items/{item_id}/retry-pages`（202；`item_busy`/`retry_requires_awaiting_review` 走 409，参数错 400，越权 404）。
  - 前端：红叉页可直接点击重试 + 「重试失败页(n)」批量按钮（仅识别完成且无重试在飞时可用），409 错误码翻成人话（`item_busy` / 已完成提示走二期断点续跑 / 页不在失败清单等）。
  - 完整评审返修（C1/C2/C3/I1/I3/I5/I6 + Minor）：重启恢复也走 `page_retry` 并带页清单（`redispatch_task_type_for` 复用标记，`main` 投递带 meta），整卷识别起跑清标记，「忙」只认处理中状态——三条合起来消灭「重试中途重启 ⇒ 标记永不清 ⇒ 永久 409」；重试页**人工框**保留、自动框换新（与实体同口径，交付 seed 整份覆盖时不再丢手框）；整卷识别的失败清单与页记录改**整体替换**（`replace_paths`），重跑成功不再留旧 `failed_pages` 卡死交付闸；提交前重读状态兜并发双提交；空页清单的空转任务把状态落回 `awaiting_review`；前端重试入口只认 `awaiting_review`（`page_status` 新增原始 `item_status`），`completed/已确认` 不再给必错按钮；轮询 `refresh` 改为只重排队、不再立刻重复取一次。
  - 门⑥ 返修（PR#261 评审）：重启补偿的 `page_retry` 任务不带 meta 时从 `recognition.page_retry_pages` 回落页清单（否则清标记后空转、item 永久停在 pending）；重试页的**实体**也按页并入审阅草稿（自动来源整页换新、`manual` 保留），避免交付 seed 整份覆盖时丢掉新 NER 实体；前端 `refresh()` 拿到 running 快照后重新武装轮询（否则重试后行永远 running）；补写入顺序契约测试（状态→标记→投递）。
- **测试**：后端 +41 例（完整评审返修 7 例：重启带页清单重派/标记不清不再永久 busy/人工框保留/整卷重跑清失败账/页记录整体替换/并发双提交只过一笔/空清单落回审阅 + 服务层准入全枚举与写入顺序、真库+真队列任务全链含页记录与其余页逐字节不变、草稿框与实体按页合并、无 meta 重启补偿回落、重启补偿派发三态、NER 重试计数恰 3 与 400/ProxyError 只 1 次、端点契约 8 例）；全量 2903 passed，18 failed 与 preview 基线同集合（容器信号量权限问题）。前端 +8 例（红叉页点单页 / running 态禁用 / 批量提交 / 409 文案化 / 未知码不吞 / 手动 refresh 重新武装轮询 / 已完成条目不给重试入口），全量 402 passed，`tsc` 双配置与 `vite build` 通过。

### 增强（Issue#257 一期之一）大案卷页级勾选格 + 部分导出（只导选中页 + 说明页 + 导出记录）

- **为什么**：200 页的真卷识别要跑很久，律师等不到结束就要先用已完成的页——一页亮一勾、跑到 100 页能先导出前 100 页、跑完再导完整版；同时大卷里单页离线是常态（#199 已做页级隔离），但交付面只有「整卷导出」，用户拿不到「已完成的部分」。这条能力把 #199 的逐页账本变成产品面。
- **改了什么**：
  - 新增 `page_status_service`（页状态投影，单一真源）：`recognition_state` 以 item 状态机为权威，页三态 = `completed`→success / `failed|timeout`→failed / 缺记录→pending，失败原因优先取 `recognition.failed_pages` 码、回落页记录 `error`；只对 pdf/pdf_scanned/image 提供（`paged`）。
  - 新增 `partial_export_service`：锁内取快照 → 解析交付源（去水印副本，避免二次解析）→ `insert_pdf` 切页（原卷升序）→ 框页号重映射到子集页序 → 直接调 `Redactor.redact`（**不写 file_store**，主产物逐字节不变）→ 中文说明页插首（页范围/共 N 页/原卷总页数/排除的失败页及原因码/快照时间/生成时间/导出 ID/免责句，不出现任何案卷文本）→ `write_export_volumes` 打包。
  - `ExportTask` 增 `meta`（job_id/item_id/pages/excluded_failed_pages/page_count/snapshot_at/recognized_pages_at_export），`get()/volume_path()` 增**磁盘回落**（重启后记录仍可查可下）；`GET .../exports` 扫 `export-status.json` 列出该 item 全部导出。
  - 端点：`GET /api/jobs/{job_id}/items/{item_id}/page-status`（轻量投影）、`POST .../partial-export`（202，异步任务）、`GET .../exports`。失败页永不进包：选区间内含失败页必须 `acknowledge_failed_excluded=true`，否则 409 `failed_pages_ack_required`。
  - F1（前置缺陷）：`update_item_performance` 增 `replace_paths`，路径命中整体替换——修复深合并「空 dict 不删键」导致失败页清单清不掉、页记录重写残留旧 `error`；默认参数保持旧行为。
  - 前端：step3 文件行展开逐页格（成功/失败 hover 原因/未跑），失败页在**选择层**就不可勾；部分导出对话框（默认全选成功页、失败页知情确认、202 后轮询到终态、导出记录列表可直接下载）；识别进行中也允许导出（导出侧另取快照并在说明页写明快照时间）。
- **测试**：后端 +65 例（F1 真 `JobStore` 红性/页投影三态与计数/`failed_pages` 压过页记录的双账本分叉/切页与逐页图像流 sha256 对齐全量参照件/端点契约与错误码/框落不到本页时 fail-closed/真 `ExportTaskManager` 全链含重启后下载与跨 owner 同名目录不可见/说明页失败清理/无框页拒收）；全量 2866 passed，18 failed 与 preview 基线（同 revision 2802 passed）**同集合**（容器 `ProcessPoolExecutor` 信号量权限问题，非本 PR 引入）。前端 +13 例（页格三态与失败页不可选/ack 门控不发请求/记录下载链接与页数摘要/step3 接线与终局文案/200+ 页分块 50 格「显示更多」/图片行不给导出入口），全量 395 passed；i18n 双语 parity 2 例。

### 增强（Issue#262）法律默认档前端信息架构改造

- 法律默认档（Issue#262）：侧边栏 8→6、任务中心+处理结果合并单入口、/single 识别前法律预设 chip+高级抽屉、复核态右栏精简、配置三页页签化进高级设置、开始页移除（登录落单次处理）、产品名定稿墨隐。
- 清扫（同 PR 收尾，Task 8）：i18n 死键 zh/en 双侧对称删除——`start.*` 全组（仅保留 OnboardingGuide 仍引用的 `start.entry.playground.cta`）、`page.start.*`、`nav.recognitionSettings(.sub)`、`page.history.sub`、`page.redactionList(.title/.sub)`（header /settings/redaction 死分支删除后零引用）；app-header `/settings/redaction` 死分支删除；router prefetch 删除冗余的 features/history 预取（jobs chunk 已静态引入 HistoryView）；AuthRoute `shouldUseHome` 更名 `isRegisterLanding`（开始页已删，行为等价）；补 `/history` 重定向渲染钉（router.test.tsx）+ tasks done 页签「无计数」断言收紧为无计数 span 元素；删 app-sidebar.test.ts 与渲染钉信息冗余的 fabricated-input 全列用例（保留 filterHiddenNavItems 三个机制钉）。
- 测试：前端全量 55 文件 / 400 例全绿 + tsc + build 绿（本机）；e2e 路由冒烟 14 路由全过 + /history→/jobs?view=done 真机重定向验证（worktree dev server × 本地栈 backend）；门④云真机走查待用户。
- 二期 Task 9b（1926d73）：上传页重皮对齐界面稿——页头紧凑化（kicker/title/desc/configDesc 长文案键删除）、上传大卡+摘要卡标签墙（选中类型实时镜像 playground-selected-tags）、告警人话化（视觉特征离线走专属文案且仅当选中含视觉要素时显示，黑话细节默认折叠 playground-offline-hint-details）。
- 二期 Task 10b（4423584）：T6「高级 · 按类型微调」内联抽屉删除，上传页改开识别范围三区弹窗（RecognitionScopeDialog）——ner/ocr/la 三区并排（<1100px 纵叠）、每区 8 类/页翻页+区内全选/全不选、即点即存直写 rec 状态、服务 offline 整区暗化+区内红条、纯文本文件 OCR/LA 两区「不适用」（na≠离线）；方案三态 select 派生（法律卷宗/全部/我的方案/手动改勾落 custom），「保存为方案」复用 createPreset(kind:'full')+invalidatePresets+自动套用；上传页旧 PresetSelectors/PresetSaveDialog 摘除（playground-upload-presets.tsx 删除，法律预设 chip 与识别范围入口保留，请求层 submitRecognitionTask 透传契约不动）。
- 二期评审 fix（05c0797）：方案下拉一致性——tracking 判定收紧为「文本/视觉两维 tracking 指向同一 full 方案且当前三清单与该方案按目录过滤后期望形态一致」（单维 tracking 在跨区手改后不再假绿显示方案名）+「不适用」优先于「离线」（纯文本文件无论服务状态都显示不适用，服务徽标仍如实显示离线）+ i18n 死键清扫（preset.saveText.prompt / preset.saveVision.prompt）。
- 二期测试：recognition-scope-dialog.test.tsx 694 行、playground-upload.reskin.test.tsx 325 行（两期提交累计）；前端全量 62 文件 / 469 例全绿 + tsc + build 绿（本机；换基并入 preview #257/#259/#261 后复跑口径）。e2e：golden_single 选择器路径逐点核对全存续、无需改动（其点击的 playground-advanced-toggle 是复核态实体面板高级抽屉 testid，并非二期删除的上传页 playground-advanced-types-toggle，泄漏断言原样）；smoke_routes 16 路由全过 + /single 识别范围三区弹窗真机探针 PASS（vite preview 新 dist × 本地栈 backend :8000，compose :3000 为旧 preview 构建不承载二期验证）；e2e 公共件 common.py 补登录后关闭引导浮卡（OnboardingGuide fixed z-40 每个新浏览器 profile 必现，实测拦截页尾右侧控件点击，locale 无关 aside 选择器+超时直通）；golden_single 全链需 has_ner 识别服务，本地栈无该容器，全链留待门④云真机。
- 二期 Task 12b：类型搜索（用户点名「高级设置里需要一个搜索，不然那么多字段难道要一个个翻页找」）——/settings 识别项设置列表卡片头部加搜索框（settings-entity-search，显示名 OR 类型代码、大小写不敏感 substring；搜索态忽略分页全部命中一次渲染、翻页控件换成「匹配 n 项」、0 命中显「无匹配」、清空恢复原页码；compact 变体同口径）；/single 识别范围弹窗方案行下加全局搜索框（playground-scope-search，三区同时过滤、命中区不分页+翻页按钮禁用+区脚「匹配 n 项」、0 命中区显示空态不破坏区框、离线区/na 区不参与搜索、搜中点 chip 照常即点即存触发自定义态、清空恢复搜索前页码）；i18n 双侧对称 +8 键（searchPlaceholder/searchLabel/matchCount/noMatch × settings.entityList 与 playground.scope）；纯前端过滤零新依赖，请求链零改动。

## 2026-11-09

### 修正（Issue#252）供料模式 P0：MinerU 3.4.5 表格 caption/footnote 是 list，clean_segments 按 str 处理必崩

- **为什么**：云端（MinerU 3.4.5，content_list 口径）供料模式识别大扫描卷 100% 失败——实弹取证（第四卷抽页直打 16581）证实 v1 `*_content_list.json` 的 `table_caption`/`table_footnote`/`image_footnote` 是 **list** 类型：空 list（falsy）被 `or ""` 碰巧兜住，非空 list（真实表格标题/脚注）直达 `.strip()` 抛 AttributeError。无表格小件侥幸通过，任何带标注表格的卷必崩（实测：第四卷 149 页、合并文档 89 页双双整单 failed）。
- **改了什么**：`clean_segments` 表格分支 `table_caption`/`table_footnote` 与图像分支 `image_footnote` 三处改走既有 `_parts_text()`（#231 引入：str 直通 / list[str] / 部件数组归一，空值 → 不加行）——与 v2 归一层 `_v2_block_to_flat` 的既有处理同口径；v2 路经 normalize 后复用同一 `clean_segments`，一并覆盖。
- **测试**：+4 例（非空 list 标题/脚注不崩且进段文本、空 list 与修复前逐字节一致、list 图注进段、str 形态回归钉）；变异验证回退修复 2 红/恢复 2 绿；adapter 54 例 + 同族 89 例全绿。

### 修正（Issue#254）取消态竞态：识别完成撞取消 item 卡死 + redact 400 英文报错被吞成「匿名化失败」

- **为什么**：律师实测识别完成后点「开始匿名化」只弹笼统的「匿名化失败」。四相确认根因有二：①用户取消任务后识别工人不中断（#233 语义），完成回调对 cancelled item 写 `awaiting_review` 撞状态机（`InvalidStatusTransition`），回写 FAILED 同样被拒——item 永久卡取消态而文件快照带全部结果，UI 照常给出必然失败的匿名化入口；②该路径 400 返回英文 `item not awaiting_review: cancelled`，`HTTP_400` 不在前端错误码映射表，英文原句在 zh 界面按 localizeError 既有规则被替换为兜底文案「匿名化失败。」——真实原因对用户完全不可见（云实例实弹信封逐字复现）。
- **改了什么**：①`_mark_recognition_complete` 识别完成前检查 item 状态，取消态显式落账（结果保留在 file_store、item 维持取消态、一条可读 WARNING），不再 ERROR 堆栈刷屏；②`submit_single_redaction` 的拒绝 ValueError 改中文可读文案（状态中文名+「刷新重试/重新识别」指引），zh 界面原样透传；③前端 jobId/itemId 解析失败的抛错从笼统 `redactFailed` 换为专用指引词包 `playground.redactNoActiveTask`（zh/en）。
- **测试**：+2 例（取消态 redact 中文文案含「已取消/重新识别」且无英文原句——走真实 `store.cancel_job` 裸 SQL 路径；识别完成撞取消态不写 awaiting_review、不直通打码），前端兜底解析用例断言更新为新指引文案。后端全量 2781 passed 零回归、前端 378 passed。
## 2026-11-05

### 修正（Issue#248 真机事故）单字符原文整文替换毁全卷——草稿默认排除 + 渲染硬闸

- **为什么**：用户 UI 默认路径实弹（2026-11-06）——映射含退化检测项 `CASE_NUMBER:'0'`（单字符），confirm 整文 replace 把 80 个页标记（`<!-- page 8**0** -->` 全含 0）与全卷数字全灭，结构对账 `src=80/out=0` 拒绝出厂。此前 API 测试全靠手动 exclude 该项掩盖了默认路径；这是「传统脱敏毁上下文」在默认决策上的翻版。
- **改了什么**：①`build_mapping_draft` 单字符原文默认 `excluded=True`（映射表可见可改）；②`ORG` 默认 `excluded=True`（策略表「机构名保留原文」落到草稿默认，同时防泛词「证券/保险/支行」毁列头）；③`render_outputs_on_structure` 增 `degenerate_original` 硬闸——审阅里显式把单字符项改 keep→replace 时 fail-closed。
- **测试**：+2 例（渲染硬闸 + 草稿默认），全量 **2808 passed** 零回归；ruff 清。

### 修正（Issue#248 验收反馈）化名去括号 + 中文短占位符 + 出生时间戳误判过滤

- **为什么**：用户验收样张五条反馈——①`[廖某]` 带括号，下游 Agent 复述时必丢括号，映射表反查断裂；②`[CASE_NUMBER_1]` 英文长占位符啰嗦；③执行背调真卷 26 项银行申请/反馈时间戳（`yyyy-MM-dd HH:mm:ss`）被 BIRTH_DATE 误检替换，**事件时间线被毁**（违反「只脱是谁，不脱发生了什么」的用户定稿哲学）；④案号占位符意义确认保留（案号可反查当事人，属 who 载体）。
- **改了什么**：①`build_mapping_draft` 化名口径修订 2026-10-08 括号稿——PERSON 无括号自然化名（廖某/同姓廖某一/廖某二，同姓区分机制原有），非 PERSON 中文短名+序号（案号1/银行卡2/证件号1），`_TYPE_ZH` 补 CASE_NUMBER/ID_CARD/LICENSE_PLATE 三键；②`hybrid_ner_service._cross_validate` 增普适形态规则——BIRTH_DATE 且文本含时分秒（出生日期从不含 HH:mm）→ 丢弃（+3 例判据：时间戳丢/纯日期留/ISO 形态丢）。
- **测试**：+6 例（占位符形态 2 + 时间戳过滤 3 + 结构化链路化名 1 顺带），全量 **2805 passed** 零回归；ruff 清。
- **边界**：还原服务对任意 replacement 形态字面还原（自造夹具不受影响）；场景化策略模板（执行背调预设类型开关+替换策略）挂账二期；CASE_NUMBER:'0' 等检测假阳性属效果线另案。

### 修正（Issue#250）inline 平铺水印（并入主流/文本型 PDF）识别面过滤

- **为什么**：用户三份新样本（收条/诉讼费退费申请表/律师问卷，testdata/水印测试样本/）实测全部未被处理——水印文字直接在**主内容流文字层**里（45°、alpha 26/128、高频重复，「李游」为办案人名 PII），probe 不命中（无独立流/单页 <3 页下限）⇒ 文本型主链、扫描件页级文本层分支、docline 全部吃到水印文字。与已落地的独立流形态（#236/#245）互补，是 #206 预判的 inline 形态落地。
- **改了什么**：`watermark_overlay` 新增行级判定 `is_overlay_line`（|dir.y|≥0.2 + alpha∈[1,220]，与既有 span 判据同源——正文不透明水平、扫描件 OCR 层 alpha=0 天然不命中）+ `overlay_filtered_text`（零覆盖行页逐字节原样，零扰动由构造保证）；接入三个识别面提取点：①`_parse_pdf` 页文本（文本型主链）②`get_pdf_page_text_blocks` 行构建 ③docline `_word_boxes`（词中心点落在覆盖 span 外接框内则剔）。`ParseResult` 新增 `watermark_inline_filtered` 计数并落 file_store（弹窗告知面接入）。**渲染面像素不动**（半透明叠底图，span 级擦除伤底图，#206 教训）；交付面按用户裁定默认保留。
- **测试**：+6 例（inline 过滤保正文/零覆盖行逐字节不变/开关关闭回退/页级 block 剔除/docline 词框剔除/三份真实样本零水印词）。
 (fix(#248-验收反馈): 化名去括号+中文短占位符+BIRTH_DATE 时间戳误判过滤)

### 增强（Issue#244）水印产品化三件套：独立工具页 + 上传检测弹窗 + 导出水印逐次选择

- **为什么**：用户验证通过后追加产品需求——去水印作为独立小模块的完整用户面：a) 与识别流程解耦，出问题可单独测这个环节；b) 用户多一个功能选择（只要净化件的场景）；c) 检测到水印要主动告知（此前「干对事但不吭声」）。
- **交付默认值裁定反转**：`WATERMARK_OVERLAY_DELIVERY_KEEP_WATERMARK` 默认 **true**（2026-11-05 用户裁定：交付件默认保留水印，用户导出时逐次选择；覆盖 11-04 的「默认去除」）。`RedactionConfig` 新增 `watermark_delivery: "keep"|"strip"`（None=跟随全局），逐请求覆盖全局设置；`_delivery_source(delivery=...)` keep→原件 / strip→净化副本，与 5d「预览与交付同源」语义保持一致（打码结果预览随默认保留=原件视图；上传标注视图 `/files/{id}/page-image` 默认净化不变）。
- **水印工具 API**（files.py，归属校验与 download 同款）：`GET /files/{id}/watermark-status`（只读检测报告，`probe_report` 脱敏 dict 不含路径）、`POST /files/{id}/watermark-strip`（执行摘除 + 机读报告）、`GET /files/{id}/watermark-clean`（净化件下载；无可用副本 400 带码）。
- **前端三件套**：侧边栏新增「水印工具」页（上传→检测报告→去水印→下载净化 PDF，code 人话映射 zh/en）；单次/批量上传后水印检测弹窗三态（检测到能摘 / 检测到摘不了给原因 / 未检测到静默，失败静默不阻断）；导出「水印」选项（保留/去除，默认保留，替换模式下隐藏）。
- **测试**：新增 `tests/test_issue244_watermark_tool_api.py`（报告 hit/负样本/归属 404/strip+净化件下载零令牌/400 码/非 PDF 拒绝 6 例）；交付两测试按新默认重写（默认保留 + 显式 strip/keep 逐次覆盖）；前端 vitest（映射纯函数 + 弹窗三态）。

### 修正（Issue#243）水印预处理命中 ⇒ 文档级链（docline）弃权回落逐页线

- **为什么**：62（证据材料卷51）在栈上实识别暴露（2026-11-05）：探针命中（可见水印流已摘、预览已净化）但 **docline 吃原件文字层建链 872 实体 / 650 个水印框**——文字层里还有一份可读水印文字（4 字「百色市院」×65/页），恰好低于 #203 重复片段闸的「≥10 字长词」判据 ⇒ 闸放行。这是 #236 设计即知的盲区（§9.8 预告），用户实测卷上爆发后按既有修法方向落地。
- **改了什么**：`text_layer_doc_source._build` 资格链最前置加闸——`render_source`（memo+单飞，不膨胀 probe_calls 计数）`code=hit` ⇒ `_TransientIneligible(watermark_overlay_hit, permanent)` 负缓存，回落逐页线（逐页 OCR 吃净化副本页图、文本层分支吃净化副本文字层，均无水印）；fail-open：水印模块故障不改变 docline 既有路由。
- **实测**（栈上 62 卷）：`docline=watermark_overlay_hit`、识别框 863→**159**、「百色市院」命中 **0**、类型分布回到正文实体。
- **测试**：+5 例（命中拒+NER 零调用 / permanent 负缓存 retry_at=inf / 未命中放行不错杀 W2 / 关闭惰性 / render_source 故障放行）。

### 增强（Issue#245）水印摘除泛化：双保险水印流组合试摘 + 单 BT 多 Tj 形态门槛修正

- **为什么**：用户以真实卷验证泛化性时撞出两种新形态，旧判据全部弃权——①`水印卷-6号案卷-p1-10.pdf`（10 页）：同一水印**画两遍**（每页 `/Contents=[图, 6862B 水印×2]`，`徐汉勇+1450120161077595` 真实办案人 PII）；②`62（证据材料卷51）朱海松-p1-5.pdf`（5 页）：30° 旋转、纯黑无 `rg` 填色、65 份水印**塞进单个 BT…ET 块**（130 个 `Tm` 携 65 个 `Tj`）。①单流差分 oracle 天然失效（摘一条另一条还在 ⇒ 0 候选）；②被「每流 ≥3 个 BT 块」门槛误拒（BT=1）。两种形态均经独立实验证实**可无损摘除**（图像 XObject sha256 逐字节不变、非覆盖 span 逐字段不变、渲染差异恰为水印覆盖、PII 零残留）。
- **改了什么**（`watermark_overlay.py`，探针与构建两侧）：
  1. **组合试摘**：单流候选全败时，对页内合格流做组合枚举（升序、合格流 ≤6 封顶），要求「摘后无覆盖 span 且非覆盖 span 逐字段不变」的通过组合**唯一**，多于一组仍判歧义弃权；单流命中路径零改动（既有卷快路径不受影响）。探针产物 `slot_by_page` 从 `(page, slot)` 泛化为 `(page, (slot,…))`，构建按整组同摘。
  2. **构建侧三层新守卫**：探针验证过的子集走廉价行集校验；**其余组合必须过全套 span oracle**（防止借组合之名删正文）；**接受前守卫**——摘后不得残留覆盖 span（双保险页只摘到一层宁可熔断也不产出「看似净化实则带水印」的副本）。
  3. **文字门槛修正**：`_MIN_BT_PER_STREAM`（数 BT 块）改为 `_MIN_TEXT_OPS`（按 `Tj/TJ/'/"` 文字绘制 op 计数 ≥3）——BT 块数是形态相关信号，62 卷单块 65 Tj 形态被它误杀。
- **真卷证据（新代码模块级 E2E）**：6号案卷 10 页 `hit`（组合 `(0,2)`×5 页）→ `stripped 10/10`、222ms；62 卷 5 页 `hit`（单流 `(2,)`）→ `stripped 5/5`、66ms；两卷图像 sha 不变、非覆盖 span 不变、PII 零残留。单层回归：canary `hit/stripped 3/3`、第四卷 149 页 `hit/stripped 149/149`、916ms——行为与旧版一致。
- **测试**：54 例（F4 语义反转为「双保险整组摘除」含正文像素级对照；新增 62 卷单 BT 多 Tj 夹具形态钉；新增组合守卫三层负样本——混合流[倾斜水印+水平数字正文同流]必须三层全拒；F5/W1/F6/异构卷等既有负样本判据不变）；宿主全量回归 **2751 passed / 0 failed**；`ruff` 干净。
### 性能（Issue#216）截断补查链并发化——单文件识别提速第一杠杆

- **为什么**：#216 挂账「补查并行化 + 单请求死线」。补查链（截断恢复时按文本块 × 类型批重查）是双重串行 for，每段 6~14s、9~12 段接龙能吃光页预算（#209 页 133 老病）；串行接龙是单文件扫描线剩的最大乘法。
- **改了什么**：`has_client.ner_checked` 补查循环展平为任务列表；新配置 `HAS_NER_SUPPLEMENT_CONCURRENCY`（**默认 1 = 与旧行为逐字节等价**）>1 且任务数 >1 时线程池并发派发；窗口运行时钳 `HAS_NER_GLOBAL_MAX_INFLIGHT`（**单闸位内**扇出上限，全局有效天花板 = 闸宽×窗口——闸 bound 语义变更已如实写入 docstring/config/.env.example，是否加进程级补查闸待门④实测）；合并按**提交序**（不依赖完成序）；contextvar 独立拷贝进线程；观测面新增 `has_text_ner_supplement_jobs` 键 + `re-query finished` 日志带 jobs/window；首异常语义差异（窗口>1 时其余任务已跑完，有界 ≤jobs）写入注释。
- **测试**：净 +9 例（并发 Barrier 实证 / 默认串行等价 / 并行==串行 / 提交序合并钉 / 闸宽钳制 / raise_on_error 上抛 / jobs 指标 / **finish_reason 经 contextvar 透传判据**（裸 submit 变异必红）/ 夹具前提钉）；变异 4/4 KILLED；全量 2706 passed 零回归。坑：NER 结果缓存类级共享（conftest 只在测试间清），同测试内并行腿前必须清缓存。
- **实测**（本地隔离栈，NER=NEO001 RTX4060，冷缓存，页并发=1）：提速试点标准样本（testdata/提速试点样本/，第四卷 8 页混合）窗口=1 = 356.0s（34 补查链/171 任务）→ 窗口=4 = 302.0s = **1.18×**；11 页版 p130–140 = 1.20×；窗口=4 下 mode_smoke 20/20 全 PASS（零残留）。诚实边界：主查分组与 OCR 未动；本地卡单补查任务 1~3s（云 6~14s），云上倍率应更高。
- **边界**：副刀（页并发 2→4）不在本 PR，第一杠杆落地后另测；默认合入零行为变化，试点值 4 由部署 env 显式开启。
### 功能（Issue#248）供料产物结构保全——脱敏替换应用在官方结构化 md 上，表格/标题/页序原样保留

- **为什么**：执行背调场景（财产调查反馈表以表格为主）供料输出无可读性——`render_outputs` 用拍平 segs 以 `\n\n` join **重建**全文，解析层即使给出 HTML/管道表格，产物行列关系全散，Agent 无法基于产物问答。用户定稿授权：只脱「是谁」，不脱「发生了什么」。
- **改了什么**（检测与应用分离，NER 检测逻辑零改动）：
  1. `agent_md_replace.render_outputs_on_structure`：替换应用在 zip 官方结构化 md 上，语义与 legacy `render_outputs` 同构（非排除项全文替换、original 长度降序防嵌套、excluded 记 retained+页码从 segs 推导）。
  2. 三道机读闸（fail-closed，产物拒绝出厂）：①结构字符闸（replacement 含 `|<>` 反引号/换行 → `replacement_structural_chars`）；②零残留闸（替换后非排除项原文残留 → `zero_residual_violation`）；③结构对账 `verify_structure_integrity`（页标记序列 + HTML table/tr/td/th 计数 + 管道表分块行/竖线数，破坏 → `structure_integrity_violation`）。
  3. 单文件线 `confirm` 与批量线 `commit_agent_md_item` 接主路径：draft/任务携带 `structured_md`（Stage1 从 `result.md` 留存）；缺失回落 legacy 拍平重建并显式 warning（`structured_md_missing`，不静默）。批量新增 `parse_mineru_for_batch` 返回 `(segs, structured_md)`，`build_mineru_segments` 保留为兼容薄包装。
- **门⑥（r1 REVISE + r2 REVISE，处置后自查）**：C1 产物文本面=md 超出检测面（segs 丢弃 header/footer/转义形态）→ 增补**前向覆盖闸** `representation_mismatch`（每个非排除映射原文必须在结构化 md 中出现并被替换，表示失配 fail-closed）+ 门④真实卷逐字符对账取证（见下）；I1 confirm 闸失败原因不可达 → 路由转 422 AppError（机读码=message 前缀：replacement_structural_chars/representation_mismatch/zero_residual_violation/structure_integrity_violation），前端既有 catch 展示；I2 回落零判据 → 补 confirm/批量两条 `structured_md_missing` 判据 + 批量闸 ValueError 契约钉；M1 夹具偏移订正+页 2 excluded 真判据；M2–M5 边界补记。
- **门④实弹修正（真实 80 页卷 confirm 首跑当场抓到）**：拍平 segs 的实体文本可含换行（跨单元格拼接，如车牌 `桂A\n2026`），结构化 md 同事实是连续/异形空白形态，字节级 replace 替换不到——前向覆盖闸正确拒绝出厂后，改走**空白柔性匹配**（空白切段逐段 re.escape、`\s*` 连接做正则替换；纯项快路径不变）+3 例判据（跨格两形态替换/柔性零残留/柔性失配照拒）。**门⑥ r3 APPROVE with fixes 处置**：I-1（plain/flex 两段循环破坏全局 len 降序防嵌套——plain 短项先跑吃掉 flex 复合项前缀致尾段残留且三闸放行，评审实证）→ 并回单循环全局降序 + 回归判据（删单循环排序必红）；M-1 柔性切段滤空。+4 例；换基后全量 2801 passed 零回归（含 #236/#245/#243/#244 水印线并行合入，无交互）。
- **r2 新发现处置**：N1（Critical）前向覆盖闸误杀 img_ocr 来源实体——图片 OCR 文本只在检测面、不在 zip md、也不进结构化产物（无泄漏面），硬拦会让带文字图片案卷 confirm 必然失败 → 豁免 img_ocr/sentinel 锚定实体（专属判据 `test_img_ocr_entity_exempt_from_coverage_gate`，删豁免即红）；N2（Important）结构化产物不含图片区域 OCR 文本（legacy 拍平产物含）→ **边界记载于此**：带文字图片（流水截图/凭证）的内容不进供料结构化产物，正经修法=图片 OCR 文本按附录/占位写回产物（挂账后续，未实现前属已知行为差异）；M-1 四条闸机读码各一条路由级判据（防 message 格式漂移）；M-2 数字订正。
- **测试**：相对 main 净增 24 例（issue248 文件 19 + 路由信封 5，全部先红后绿）；变异 4/4 KILLED（删管线 verify/删零残留闸/删结构字符闸/删批量结构路径）；全量 2722 passed 零回归；ruff 清。
- **门④ C1 取证（真实执行背调 80 页卷，standard 档全页解析）**：md 文本面 4,952 个 ≥4 字符格逐格对齐检测面（segs），未覆盖仅 75 格=73 页脚「第N页」（page_number 块，有意丢弃，零 PII）+ 2 格 LaTeX 转义秒数（`${20}$` 形态，strip_inline 有意剥离，非 PII）；**无 PII 承载的页眉页脚**，r1 C1 担心的未扫描泄漏面在真实产物上不存在——评审路径 (b) 取证满足，前向覆盖闸+零残留+结构对账+本取证构成 C1 处置闭环（诚实边界：本卷页眉=法院名+表题，属策略表「机构名保留」类；该结论限同族法院反馈表形态，异构卷须复跑本取证）。
- **边界**：①表格质量取决于解析档位（flash 产物本就无表格结构——standard 档才有；档位是部署配置不在本 PR，执行背调场景部署时显式 `--mineru-tier standard`）；②映射原文自身含 `|` 或换行时替换会移除该字符 → 结构对账拒绝出厂，唯一出路=该映射项 exclude（fail-closed 且有出口）；③结构化产物含图片/链接语法，全文兜底 replace 理论上可命中 URL 区（实体文本与文件名巧合），未豁免、已记载；④页标记对账正则 `<!-- page N of M -->` 与真实 MinerU 4.x 产物格式一致（真实 80 页卷实测），门④将以真实 canary 复钉；⑤电子 PDF/Excel/Word 表格线、跨文件全局映射、台账产物=二期；NER 检测质量=效果线不变。

## 2026-11-04

### 变更（Issue#236）独立矢量覆盖水印流：识别取图前检测并摘除（仅内部副本）

- **为什么**：族 A 案卷（如「第四卷」149 页）每页 `/Contents=[A,B]`——A 是整页扫描图 + 不可见 OCR 文字层，B 是 5384B 的独立水印层（30 组 45° 半透明 `BT…Tj…ET`，`0.25098 rg` + `/Gs1 gs` alpha 51 + `NotoSansCJKsc-Regular` 29.22）。这类水印会污染 OCR 取图（浅灰字被当正文）与后续 PII 召回；而把它「当文本过滤」会连带擦掉正文（#206 实测 43,449→8,468 字符）。摘 B 是无损的：原件字节不动，只是内部渲染副本少一层。
- **改了什么**：
  1. 新模块 `backend/app/services/watermark_overlay.py`：卷级**差分 oracle 探针**（抽样页逐条内容流试摘 → 要求「水印 span 消失且非水印 span 逐字段相等」且候选唯一）→ 指纹闸（字体/尺寸极差/alpha/重复次数）→ 逐页摘除（op 白名单 + 行集合校验 + 残余水印令牌守卫）→ 副本落 `OUTPUT_DIR/.watermark_overlay`（0700/0600、TTL 清理、页数自检、原子 `os.replace`）。进程级 memo + 单飞：每卷只付一次探测/构建。
  2. `file_parser.get_pdf_page_image(..., allow_strip=True)`：识别/VL-MD 走净化副本，**页图缓存键加 clean/raw 变体**（否则预览与识别互相污染）；预览两点（`vision_service.preview_redaction`、`api/files.py` 预览端点）显式 `allow_strip=False`。
  3. `vision_service`：`pipeline_status` 新增 `watermark_overlay` 段（code/source/pages_probed/pages_hit/pages_stripped/pages_skipped/duration_ms），随任务指标与结果页透出；交付光栅化新增 `_delivery_source()`（**默认交付去水印**：交付 PDF 整页图从净化副本光栅化；置 `WATERMARK_OVERLAY_DELIVERY_KEEP_WATERMARK=true` 退回保留水印）。
  4. 配置 11 键（识别面默认开：`WATERMARK_OVERLAY_STRIP_ENABLED=true`；**交付默认去水印**，2026-11-04 用户裁定）——开关、探测页数、命中页下限、指纹阈值、倾斜/α 阈值、体积闸、副本 TTL、交付保留开关。
- **测试**：新增 `backend/tests/test_watermark_overlay_strip.py` 53 例（合成夹具手写内容流：命中摘除+栅格保全、暖负样本[水印并入主流/合法不可见文字层/矢量表格]、`[A,B1,B2]` 歧义、单页命中弃权、异构卷逐页记账、残余水印弃权且**不产出带水印副本**、合法流不被误摘、冷负样本零文件零构建、副本权限/TTL/结构、关闭时不探测、损坏/超限/自检失败 fail-open、预览原件视图、交付默认保留+反向支路像素级对照；门⑥ 两轮评审 + 增量短审补强的页级熔断/码表/负缓存/插槽映射/清理节拍接线运行时钉/TTL 下限等）；`ruff` 干净；变异验证 **12/12 KILLED**（M1–M12，每条改动都让具体用例转红）。
- **复核界面水印可见性**：`pipeline_status.watermark_overlay` 原先渲染成裸键「watermark_overlay 已运行 0」（前端 `pipelineLabel()` 无该键映射）⇒ 补「水印预处理」标签 + `code=hit/guard_refused_pages && source=copy` 时显示「已摘除 N 页」；新增 4 例前端用例。
- **上传后预览/标注视图默认净化**：`GET /api/v1/files/{id}/page-image` 从「固定原件视图」改为 `allow_strip=not raw`（默认净化副本；`?raw=true` 退回原件，诊断用），用户上传后翻页即见水印已摘除，不用等识别跑完。接线钉 `test_page_image_endpoint_defaults_to_clean_view`。
- **打码结果预览与交付同源**：`preview_redaction` 底图改走 `_delivery_source()`（默认即净化副本；原件视图仍由 `/files/{id}/page-image` 提供），修掉「预览有水印、下载没有」的口径错位；新增 AC5-5d 两态判据 `test_previewed_result_matches_delivery_watermark_policy`（默认 Δ>5 灰阶 / 保留支路 Δ<3 灰阶）。
- **单次处理结果页报「水印预处理 已摘除 N 页」**：新增共享读法 `frontend/src/lib/watermark-overlay.ts`（`watermarkOverlayStatusFromVisionQuality` + `watermarkStripChipText`），`playground-result-action-bar` 渲染 chip；草稿快照缺 `vision_quality` 时自取一次文件详情，未命中/未跑/关闭不显示（不谎报）。前端 47 文件 / 363 例全过。
- **交付件去水印（2026-11-04 用户裁定）**：`WATERMARK_OVERLAY_DELIVERY_KEEP_WATERMARK` 默认改 `false`，交付 PDF 整页图从净化副本光栅化。真卷复验（149 页，`output_file_id=4ce1e303-…`，69,507,957 B）：水印墨迹像素上 `|交付−净化副本|=1.66/0.97/2.00`、`|交付−原件|=35.87/35.95/35.65` ⇒ 交付件已无水印；原件仍一字节不动，预览/文本层/闸继续吃原件。测试改两条：默认支路断言「交付=净化渲染」、反向支路断言「置 true 恢复保留」（原「默认保留」用例同步改名）。
- **门④ 实测（本机 docker 栈整卷，149 页真卷）**：识别 149/149 页 `code=hit/source=copy`；副本文本 90,624→47,253 字符、水印令牌 1,788→0；OCR 实体框 3,499 个、水印串命中 0；`text_layer_doc` 仍按原件 coverage 0.376 弃用建链（#203 闸行为不变）；预览端点仍为原件视图；探针+构建 660 ms；未命中卷零新建副本；门⑥ 二轮处置后在最终 head 复跑：149/149 仍 `hit/copy`、`pages_guard_refused=0`、副本 25,798,037 B / 47,253 字符 / 令牌 0、实体框 3,540、预览 4,493,427 B（原件视图）、交付件 85,278,328 B 且水印笔画带内「交付↔原件 6.91 < 交付↔副本 19.91」；全量回归 18 failed（既有 `/dev/shm` 环境性）+ 2,722 passed，与基线失败集合逐条一致；增量短审处置后在 head `cbf344c` 第三次整卷复跑同样 149/149 `hit/copy`、`pages_guard_refused=0`、副本 47,253 字符/令牌 0、实体框 3,502、预览字节数=原件渲染、未命中卷零新建、清理节拍 790 ms。
- **识别入口统一解析净化源（2026-11-04 用户实测反馈后补）**：扫描件的「PDF 文本层识别」分支原先直接读**原件**文字层，水印层的文字会被当正文带进识别框（3 页试件上出现 96 个「林映峰」水印框而 `watermark_overlay` 却是 `not_applicable`）。现在 `VisionService.detect_with_dual_pipeline` 入口先解析一次 `_recognition_source()`（开关开启且本卷命中 ⇒ 净化副本），**页图渲染与文本层识别共用同一个源**，并把状态在入口写进 `pipeline_status`；回归用例 `test_recognition_source_is_clean_copy_when_strip_hits` / `test_recognition_source_falls_back_when_strip_disabled`。整卷复跑（149 页，head `5ac8e54`）：149/149 `hit/copy`、`pages_stripped=149`、`pages_guard_refused=0`、798 ms；实体框 3,517 个，`林映峰`/`14501201410363675`/`钦州市院` 命中 0（`钦州` 61 次为正文）。
- **边界**：只处理「独立矢量流」形态——烧进像素的水印、文字层内的水印碎片不在本 Change（#215 后续阶段）；文本层建链与 #203 闸**仍吃原件**（副本仅渲染，实测副本 coverage 0.0407 < 闸 0.15 会让 docline 接管并撞 300s 页墙）；agent_md（MinerU）走原件字节，不在本 Change。

### 修正（Issue#232/#237）错误信封机读化：拒收文案按码本地化 + 下载路径空 detail 出 "{}" + healthcheck 敲错门牌

- **为什么**：#229 门⑥两处挂账（#232）——①I-1：伪造扩展名拒收文案只有中文自由文本、无机读码，en locale 前端按既有决策只能显示通用兜底，「实际格式+可操作指引」只对 zh 生效；②M-3：下载/批量打包路径（`api-client.ts` fetchBlob、`api.ts` downloadArchive）仍是 `detail ?? message` 旧短路，空 `detail:{}` 给用户看 "{}"。#237：frontend healthcheck 的 `wget localhost:8080` 先解析 `::1` 而 nginx 只监听 IPv4 ⇒ 本地所有栈 frontend 容器常年误显 unhealthy（服务本身正常）。
- **改了什么**：
  1. 后端：新增 `FileContentMismatchError`（`ValueError` 子类，既有回滚路径不动）与 `detect_format_code` 机读格式码；`process_upload` 拒收升格为该异常，单文件/断点续传两路由按 `isinstance` 出 `AppError` 信封（`error_code=FILE_CONTENT_MISMATCH` + `detail={detected_format, extension}`），中文 message 逐字不变；批量入库逐件明细附同款机读字段。
  2. 前端：取错统一收口 `utils/errorEnvelope.ts`（`firstMessageText`/`errorEnvelopeMessage`/`errorCodeFromEnvelope`，playground、agent-md、下载路径全站一个具名函数，评审 M-4 落地）；`ApiError` 增 `detail` 字段；`localizeError` 增 `messageFromEnvelope`（已知码先映射：`FILE_CONTENT_MISMATCH` 按机读 detail 组合本地化文案，format 名走词包 13 键 × zh/en；未知码保持原行为）。
  3. #237：`docker-compose.yml` 与 `frontend/Dockerfile` healthcheck 改 `http://127.0.0.1:8080/`。
- **测试**：后端 +18 例（码表 12 形态 + unknown、单文件/断点续传信封两路径、常量契约锚、批量逐件明细），全量 2676 passed 零回归；前端 +15 例（envelope 取错 9、fetchBlob 信封 3、FILE_CONTENT_MISMATCH 组合/兜底/en 6），vitest 334 + tsc×2 + build 全绿；i18n parity 契约测试守键位。
- **边界**：`_MAGIC_TYPE_NAMES` 标签失准（RIFF≠仅 WebP 等，#228 评审 M-1）不在本修；批量入库「不支持的类型/文件过大」仍为纯文本 reason（无机读码，触发时再议）；en 词包懒加载，en 组合文案由 parity 契约测试保障。

### 变更（Issue#230 缺口）LA 服务未启动不再拦截识别——自动跳过视觉段并显式提示

- **为什么**：本地小显存环境常不启动 LA（visual-features），前端把「未部署」当「必需服务故障」直接拦停识别——用户勾了印章/签名就完全无法识别文字部分。用户裁定：不启动 LA 服务要能正常继续。
- **改了什么**（纯前端，无后端改动）：①`getRecognitionBlocker` 不再把 `visual_features` 计入拦截（OCR/NER 仍硬拦——缺了真跑不了）；②提交侧新增 `isVisualServiceBlocked` 判据（与 blocker 同源），LA 不可用且用户勾了视觉项时**照常提交、视觉段传空数组**（后端既有语义=显式不跑该段，编排器不回落默认全类型），并置 `visualSkippedNotice`；③识别面板顶部新增琥珀色提示条（zh/en），服务恢复后下次任务自动恢复检测。跳过而非静默——用户知道印章/签名本次未检测。
- **门⑥ I1 处置**：rerun 路径（handleRerunNer 图片分支）接入同款 skip 语义——LA 未启动时 rerun 也自动跳过视觉段（不再逐页撞后端预检硬报错），提示条随本次 rerun 刷新（服务已恢复即清掉过期提示）；rerun 防摘钉入 wiring-contract。
- **测试**：taskflow 新增 (h2)（LA 不可用 → `visualFeatureTypes: []` + 提示置位，负样本 (h) 全类型透传不变）；wiring-contract 契约钉同步新形状并加 skipVisual/提示接线两枚防摘钉；vitest 319 全绿、tsc（含测试）绿、build 绿。
- **边界**：识别结果页的 pipeline_status 级标注（后端侧）不在本 PR；烧进图片的水印、印章检测质量不受影响。### 功能（Issue#238）进度卡诚实化——识别段真完成数/✓语义/已运行时长 + 打码段逐页进度

- **为什么**：单文件等待卡三处与并行事实不符——①「87/149」实为页**开跑**水位（≈已完成+在飞，并行下"处理到第几页"不成立）；②通用「视觉识别」段不排除任何清单行，进行中的段带 ✓ 显示（进度条 58% 却见「✓ 文字识别 87页」自相矛盾）；③打码段整卷一次性执行零上报，大卷渲染数分钟用户只盯「正在匿名化...」。用户拍板方案 A（诚实化单卡）+ 已运行时长 + 打码补进度，交互预览先行验收。
- **改了什么**：
  1. 识别段（`task_queue_pipelines._run_vision`）：进度写点从页开跑移到页**终态**（完成/超时/失败都计入，单调防回退）；每段计数归零（两段式各扫一遍）；合并段跳过一阶段失败页时 `total` 缩水为该段实际页数（进度条不卡死）。
  2. 新增 `recognition.current_segment`（{index,total,label,concurrency}，段起跑覆写）——段数动态：两段式仅在「勾视觉要素且多页」存在；单段不编号；前端不猜配置。
  3. 打码段：`_run_redaction` 起跑写 `redaction_started` 0/N；`progress_cb(current,total)` 穿透 `execute_redaction_request → execute_redaction → Redactor.redact → _redact_image → VisionService.apply_redaction`，扫描件/图片栅格化路径逐页回调写「打码 page p/N」（单调防回退）；文本/docx 快链路（page_count<=0）不传回调。
  4. 前端等待卡：数字行「已完成 X/Y · 并行处理中 min(段并发,剩余) 页」（并行页数=信号量模型下在飞数的精确值）；段名行两段才显示「第 X 段/共 Y 段」；新增「已运行 {time}」（后端 `performance.<task>.started_at` 起算，刷新不走表，缺键回落本地计时）；✓ 清单排除集修正（OCR+HaS 段排除 OCR/NER 两行、泛视觉段排除三行、文本件排除 NER 行）；文本件数字行=「实体识别中」+ 进度条不定态（新 tailwind `indeterminate-slide`）。
  5. 打码被动小卡：`PlaygroundLoading variant="redaction"`（右下角 passive、无清单/提示/取消），由 pendingRedact 收尾订阅逐拍刷新，done/failed/换文件/重置即退场——「点完打码即可离开」语义不变。
- **测试**：后端净 +13 例（页完成写点/乱序单调/两段式段信息与段内归零/合并段 total 缩水/单段不编号/超时页计入/打码起跑+逐页+单调/无页数不传回调/回调链穿透三钉/栅格化逐页/单图 1,1），先红后绿；后端全量 2671 passed 零回归（修 test_mask_mode_gating 桩签名承接新 kwarg）；前端净 +15 例（task-progress 纯函数与打码映射注入负例 7 + PlaygroundLoading 新语义 6 + taskflow 并行中/时长 1 + redactProgress 订阅退场 1），换基后全量 351 passed、playground 180/180、tsc+build 绿。
- **边界**：打码并行化不在本 PR（打码仍串行，本 PR 只补上报）；供料线（agent/vl_md）进度形态不动；旧任务无 `current_segment`/`started_at` 回落旧行为不误报（AC7）；批次任务中心的进度展示不在本 PR 范围。
- **门⑥处置**：I1（重置后打码小卡永久残留——订阅 effect 的 `!currentFileId` 早退挡在 pending 失配清理之前，重置同批清空 pendingRedact+fileInfo 时 effect 早退、小卡冻结残留）⇒ 清理分支前移 + hook 级回归钉 (g)（confirmReset 直达 performReset，变异验证还原 bug 即红）；I2（CHANGELOG 混入提交哈希行 + 基线残留冲突标记尾行）⇒ 已清；M1 单调闸补「死防御勿删」注释；M2 ocrHas 段排除集补测试钉（此前变异存活）；M6 数字订正。M3（全败卷段标签瞬态）/M4（段切换数字毛刺）/M5（时钟偏移为设计取舍）挂账不修。
## 2026-11-03

### 修正（Issue#217）LibreOffice 烘焙进 backend 镜像——废弃宿主 soffice 挂载

- **为什么**：#217 因「dpkg 解包 LO 峰值 12.4GB RSS」改挂宿主 soffice；实测该方案在新宿主机（Ubuntu 25.x）上因 ABI 代差失效——单文件 bind mount 丢符号链接（容器内 `$0` 解析错位找不到 pagein-common），补 wrapper 后仍缺 30+ 宿主系统库（icu74/glibc 2.41 vs 容器 Debian 基座），.doc 转换产出占位错误文案（真实案卷冒烟 2026-11-03 抓出）。
- **改了什么**：`backend/Dockerfile` 直接 `apt-get install libreoffice-writer-nogui` 烘焙进镜像（**2026-11-03 实测整装 dpkg/apt 峰值 RSS ≈73MB，推翻 12.4GB 旧记录**——旧值系当时基镜像版本的环境产物，Record 已在注释中留痕）；`docker-compose.yml` 删宿主三挂载；`start_local.sh` 删宿主 LibreOffice 预检警告（不再依赖宿主）。
- **测试**：镜像内 `soffice --version` 通过；本地栈端到端——.doc 真卷走 agent_batch 供料线，映射表 8 条（修前 0 条）、三产物齐全、映射原文值零残留；CI docker-build 即构建验证。
- **边界**：镜像体积增大（LO nogui ≈+500MB）；云端部署同镜像受益，无需宿主预装。



- **为什么**：两处「失败/取消后不换策略」的同族健壮性缺陷。①#200：解析失败分批递归把外层温度原样传给子批——若不可解析的根因是复读产生的垃圾输出，同温度重发=同贪心轨迹复发（#198 同源家族，工单处置条件即「评估换采样」）；②#204 缺陷 B 取消侧：文档级建链被取消（页超时/用户取消等一切取消原因）时 `CancelledError`（BaseException）穿透 `except _TransientIneligible`/`except Exception` 两个 handler ⇒ memo/负缓存零写入 ⇒ 重试从零再放 O(整卷) 建链，#203 两次真实复现的放大机制。
- **改了什么**：
  1. `has_client.py` 解析失败递归：子批温度 = `max(调用方温度, HAS_NER_TRUNCATION_RETRY_TEMPERATURE)`——与 #198 补查同款只升不降语义；帽驱动预分批（非失败驱动）两处调用点**不动**（预规划继承调用方温度是合法语义）。
  2. `text_layer_doc_source.py`：建链取消 → 落短 TTL 负缓存（新 settings `TEXT_LAYER_DOCLINE_CANCEL_NEGATIVE_TTL_SEC=300`，reason_code=`cancelled`）→ **re-raise**（取消传播不能吞）；TTL 内重试直接短路，TTL 过后允许重建。
- **测试**：#200 +2 例（温度升级 + max 语义不被拉低）；**修正了原特征化钉的误钉**——旧钉以为 25 型走解析失败递归（24+1），实际先被帽驱动预分批切成 [12,12,1] 且 `type_guidance=[]` 不满足递归门槛（`is None`），钉的一直是预分批点；新测试 monkeypatch 预分批单批 + guidance=None 真正进入递归分支。#204-B +4 例（取消落 memo+re-raise / TTL 内重试零重建 / TTL 过后重建 / settings 口径钉）。变异双杀（#200 还原继承语义 1 红；#204-B 吞 re-raise 3 红）。全量 2627 passed 零回归。
- **边界**：#204 缺陷 A（建链 O(整卷) 受单页 300s 看管的预算解耦）不在本 PR，另行设计讨论；水印资格闸（#203 主修）行为不变。

### 修正（PR#234 门⑥挂账）sidecar 同 stem 覆盖 / warnings 透传 / mixed 重复告警 / WAIT fail-fast

- **为什么**：PR #234 门⑥独立评审的 4 条 Minor 挂账，用户裁定全部当期收口。①sidecar 一任务多输入同 stem（供料批量含 PDF+doc）时 `-o` 的 md 与 `mineru4_*.json` 都按 stem 命名会互相覆盖；②sidecar 任务级 `warnings`（窗口去重/部分 range 放宽/page_count 缺失）到不了 backend（`poll()` 只读 status）；③`agent_md_pipeline_service` v4 分支 `mixed_schema_v4_wins` 在两处重复 append（基线既有）；④`MINERU_SIDECAR_WAIT` 非数字报错是裸 `could not convert`，定位性差。
- **改了什么**：①`_run_mineru_sync` 建 per-task 消歧名（首现原名、重复 stem 加 `_1/_2` 序号），`-o` 与 mineru4 json 两处共用；②`MineruResult.warnings` 新字段 + `poll()` 归一化透出 + `wait_result` 终态塞入，单文件线与批量供料线各在 result 拿到处打 WARNING 日志（旧 sidecar 无键向后兼容为空 list）；③删第二处重复 append，mixed 线索只报一次；④`_mineru_wait_sec` 对非法值抛点名 `MINERU_SIDECAR_WAIT` 的 RuntimeError（经任务 error 可读透出）。
- **门⑥ r2 处置（3 Minor 全修）**：M1 消歧名防撞（`a.pdf+a.doc+a_1.rtf` 曾把第 2/3 输入都算成 `a_1` 静默覆盖 ⇒ 生成名撞 taken/used 即递增，实弹判据钉死）；M2 `MINERU_SIDECAR_WAIT=inf` 走 OverflowError 逃逸 ⇒ `except (ValueError, OverflowError)` + 判据；M3 pipeline_service logger 区空行规整。
- **测试**：净 +8 例（sidecar 2：同 stem 双输入 md/json 四文件互不覆盖且内容可区分、非法 WAIT 报错点名变量；client 2：warnings 透传/无键兼容——**附带发现并修正测试落点事故：这两例最初误追加到主检出后被还原抹掉，worktree 内从未存在过，变异复核时抓出**；dual_representation 1：mixed 条目 count==1 钉死；特征化钉 poll 响应随契约扩展补 `warnings` 键）。变异 6/6 KILLED（含 r2 两条新判据对旧实现各一杀）；全量 2664 passed 零回归。
- **验收实弹追加（内置浏览器+真容器，2026-11-04）**：抓出两个真缺陷并当回合修复——①**#234 引入的 .doc 回归**：缺省 `--pages all` 被上游对非 PDF 输入拒绝（`page_range_invalid: "doc uses full-document parsing"`），所有 .doc 喂料任务必失败 ⇒ 仅 PDF 传 `--pages`，非 PDF 不传（上游对 doc 本就全文档解析，无截断问题）+2 判据（变异 1/1 KILLED）；②**F4 断层**：sidecar 的可读失败原因被 client 在 failed 终态丢弃（UI 只见无信息的 task_id）⇒ `poll()` 透传 `error`、`wait_result` 抛错携带原文 +1 判据（UI 实测：`MINERU_SIDECAR_WAIT 非法: 'abc'（须为秒数，如 7080）` 直达前端）。F1 同 stem 消歧真容器实弹：三输入（a.pdf+a.doc+a_1.rtf）六产物互不覆盖、内容一一对应；F2 warnings 实弹到达 API 面。全量 2667 passed 零回归。
- **边界**：warnings 目前到日志面为止（前端展示不做，超范围）；`_transport` 特征化断言随契约更新（含新增 `error` 键）。

### 修正（Issue#230/#231）MinerU 4.x sidecar 三处 CLI 缺陷 + docvortex 表名/脚注/doc_title 静默丢失

- **为什么**：真卷（149 页扫描件 / 78 页表格卷）实测三处 sidecar CLI 缺陷——①不传 `--wait` 走 CLI 缺省 60s，真卷 standard 档必以 `parse_wait_timeout` 失败且留下孤儿 parse；②不传 `--pages` CLI 默认只解析前 10 页，所有 >10 页案卷静默变残卷（#218 引入即如此）；③补 `--pages all` 后 >64 页按窗口拆多份 json，撞后端 `mineru4_multiple_docs` fail-closed 整单作废。同批 #231：4.x docvortex 适配层只认 text/table_body/image_body 三种部件，`table_caption`/`table_footnote`/`image_footnote` 独立部件与 `doc_title` 块全部静默丢失（只在 warns 留痕、下游不失败）——表格卷丢「这张表是什么」的语义。
- **改了什么**：①sidecar 显式传 `--wait`（`MINERU_SIDECAR_WAIT`，缺省=超时−120s）与 `--pages`（`MINERU_SIDECAR_PAGES`，缺省 `all`）；②新增 `_merge_docvortex_docs` 纯函数按 `page_idx` 合并多窗口（同页同内容去重计数、同页异内容/schema_version 不一致显式失败不盲拼），后端 `normalize_docvortex` 的 fail-closed 原样保留当防御网；③合并后 `pages` 数 != `metadata.page_count` ⇒ 任务显式失败（残卷不许出厂）；④任务增 `warnings` 字段并在 `/tasks/{id}` 透出；⑤adapter v4 分支把三个已知部件映射到与 v2 同口径字段（`table_caption`/`table_footnote` 仅 table 块上浮、`image_footnote` 仅 image 块上浮，`" | "` 连接同 `_parts_text`），`doc_title` 按「标题→正文」口径进 `_DOCVORTEX_TEXT_BLOCK_TYPES`，未知 part type 仍告警不动；⑥`clean_segments` image 段并入图注、`enrich_image_blocks` 哨兵降级保留段内已有文本。
- **测试**：净 +21 例（sidecar 13：merge 五判据 + CLI 六判据（含门⑥ I1 重钉——TIMEOUT 非缺省值在 reload 前注入、I2 部分 range 放宽校验改 warning 两判据、M3 page_count 缺失留 warning）+ 端到端单份合并/截断显式失败；adapter 8：v2 同口径映射、多部件 `" | "` 连接、图注哨兵保留、**enrich 成功路径图注在前顺序**（M8）、`doc_title`、形似负例 `table_caption_v9` 仍告警、**错位部件点名告警**（M7）、**部件 content 列表形态**——真卷实测 caption/footnote 的 content 是文本部件列表非字符串，149 页真卷 7 个部件曾 0 上浮），先红后绿；变异 9/9 KILLED；后端全量 2643 passed 零回归。门④：149 页真卷 flash 档容器实测——doclib 恰为 Issue 同款双窗口（`1-10`+`11-149`）⇒ sidecar 合并单份 pages=149 连续、`mineru4_docs=1`、任务 completed 141s 零告警；7 个 caption/footnote 部件全数上浮进 80 个表格段（64 段含表名/脚注语义行）、unhandled part 告警零。
- **边界**：不做表格结构化还原（效果线另案，#224 同口径）；`normalize_docvortex` 多 doc fail-closed 不动；sidecar 契约向后兼容（新字段 `warnings` client 侧不读不受影响）；MinerU flash 档对表格零结构（无 `table_body`/caption 部件）是上游能力边界，属 #50 T2 范畴。
- **canary**：testdata `canary-issue230-doclib-two-windows/`（多窗口微缩夹具）+ `canary-issue231-docvortex-captions.json`（caption/footnote/doc_title/形似负例形态夹具），全合成假数据。

### 修正（Issue#224）MinerU 4.x 纯文本 table_body 全量丢失——表格型案卷 0 实体

- **为什么**：真卷验收发现（表格型扫描 PDF 走供料模式报「未识别到任何敏感实体」）：4.x flash 对扫描表格输出的 `table_body` 是**纯文本版面文字（无 HTML 标签）**，`table_to_text` 只收集 `<td>/<th>` 单元格 → 解析 0 行返回空 → `clean_segments` 记 `empty table block`，表格正文（真卷实测 47,256 字符）fail-open 全量丢弃，NER 只见 23 字符。既有夹具的 `table_body` 全是合成 HTML，与真实 4.x 形态不同源，测试未覆盖。#219 已知边界「4.x 比值守卫 warning-only 无回落」是同一风险另一面；本例在更上游（归一层有货、清洁层扔光）。
- **改了什么**（单函数，`agent_md_adapter.table_to_text`）：HTML 解析结果非空 ⇒ 原样返回（既有行为逐字节不变）；**解析结果为空且输入不含 HTML 表格标签** ⇒ 纯文本回落（按行拆分/行内空白折叠/`strip_inline`）；**含表格标签但解析不出行（畸形 HTML）不回落**——维持空 + `empty table block` 告警，不把标签碎片喂给 NER，并守住 #212 双表示守卫「丢表 ⇒ empty_table_blocks>0 ⇒ 回落 v1」语义链（独立冷审 B2 钉）。方案与验收过独立冷审（需修订 2 项已处置），根因报告/方案存私有仓 `docs/acceptance/2026-11-03-mineru4-plaintext-tablebody-*.md`。
- **测试**：净 +13 例（table_to_text 直测 8 + clean_segments 集成 3 + 管线级 2：表示选择出段且无 `docvortex_shortfall`、stage1 映射表含表格内实体），先红后绿；变异双杀（删回落分支 7 红、删标签守卫 2 红）；单文件与批量供料两线共用该函数同时受益。
- **边界**：不做表格结构还原（列对齐/表头语义），NER 与脱敏只需文本可见，结构化还原属效果线另案；3.x 路径（content["html"] 恒 HTML）行为逐字节不变；空表格告警语义不变。
### 变更（Issue#226）本地启动默认启用 MinerU（flash 档）+ 档位参数化

- **为什么**：用户裁定「默认栈要所有功能可用」——#217 当时因 12GB 显存预算把 MinerU 设为隐藏开关（`--with-mineru`），导致默认启动后供料模式不可用；同时 2026-11-02 实测隔离栈的 MinerU standard 档 vLLM 引擎常驻吃 5.4GB，本地不该常态跑 standard。
- **改了什么**（纯启动编排，无后端/模型栈改动）：
  1. `start_local.sh`：MinerU 默认 local + **flash 档**（无常驻 vLLM 引擎，显存 ≈2GB，单文件秒级）；新增 `--mineru-tier flash|standard`（standard ≈6.5GB 含引擎，与本地 OCR 互斥——硬报错带指引：`--ocr cloud` / 降回 flash / 上云实例）；新增 `--no-mineru`；`--with-mineru` 保留为向后兼容 no-op；显存预算账补 MinerU 两档（flash 2 / standard 6.5，standard 数值取自 196 栈实测）；档位打印含 tier；usage 同步。
  2. `docker-compose.yml`：mineru 服务透传 `MINERU_SIDECAR_TIER=${MINERU_SIDECAR_TIER:-flash}`（sidecar 本就按此变量选档，此前 compose 未透传、恒为默认 flash）。
  3. `README.md`：启动档位表同步（默认档含 MinerU flash ≈9GB；tier 参数与互斥说明）。
- **测试**：`bash -n` / `docker compose config` 过；互斥与端口占用判据本地实测（见 PR 评论）；flash 档全链起栈实测（canary → mapping_ready，8 条实体）。门⑥冷审 APPROVE with fixes：I1 CI 绑 head / I2 视觉档文档「让位」改为硬报错实态并给可用命令 / M1-M4（usage 简写、显存口径对齐、off 档 warn、LA 权重预检）——已全部处置。
- **红线**：脚本不内嵌任何地址/口令；模型源=ModelScope 不变。

### 变更（PR#223）密码规则放宽为仅 6-16 位长度校验

- **为什么**：用户裁定——密码不再要求大小写/数字/符号复杂度，只要 6-16 位即可；旧规则（≥8 位 + 四要素全查）对内部部署场景过严，弱密码经常被拒。
- **改了什么**：`validate_password_strength` 仅保留 6-16 位长度判断（4 个调用点 setup/register/change-password/auth-users 走同一函数，一处改全生效）；前端设置页与改密弹窗提示文案同步（en/zh）；`docs/交付清单.md` 与 `docs/性能与质量白皮书.md` 的「密码四要素」宣称对齐新规则。
- **影响面**：已有账号老密码不受影响（校验只在设置/改密时发生）；LDAP 登录路径不做复杂度校验（策略归目录），本地 break-glass 改密走新规则。
- **测试**：新增 `tests/test_password_policy.py` 13 例（纯函数表驱动 + setup/change-password 端点行为 + fail-closed 判据）；后端全量 2583 passed 零回归；本地浏览器验收 7/7（记录在 PR 评论区）。
- **边界**：长度按 Unicode 码点计数；纯空格密码在长度合法时可通过（产品口径，记账）；拒绝原因的本地化优化（机读 error_code）另案跟进。

## 2026-11-01

### 新增（Issue#219）MinerU 产物双 schema 适配：3.x content_list 与 4.x docvortex.middle 自动分流

- **为什么**：#217 后本地 MinerU 4.1.3 容器与生产实例 3.4.5 并存（刻意设计），但 4.x 大重构后的产物 schema（`docvortex.middle`：`metadata/pages/is_full_document`）与 3.x `content_list`（v1 扁平/v2 按页嵌套）完全不同——适配层读不出 4.x 文本，喂料全链在本地形态直接 `parse_no_text` 失败（fail-closed 守卫在位，无泄漏风险）。
- **改了什么**（三文件，3.x 路径逐字不动）：
  1. `agent_md_adapter.py`：`detect_schema`（形状判定判据本体：v4/v2/v1/unknown，schema 全等不做前缀匹配）+ `normalize_docvortex`（4.x→扁平 v1 归一，映射对照表实测锚定：text/paragraph_title→text（`\n` 拼接）、table 的 table_body 部件上浮、image 无 img_path 走既有 `[图片]` 哨兵降级、未知块/部件计数告警不静默；**pages 内容级去重**——doclib 双文件是 4.x 常态，pages 相同 metadata 不同必须去重成功，pages 不同 ⇒ fail-closed 防导出 md 全文翻倍；畸形结构（pages 非 list 等）跳过留痕不静默）+ `markdown_body_chars`（比值守卫参照口径）。
  2. `mineru_parse_client.py`：拆包按 `detect_schema` 形状分流（**单一事实源、形状优先于文件名**）；`MineruResult` 增 `mineru4_docs`/`unknown_jsons`；损坏 json 维持整包「非法 zip」。
  3. `agent_md_pipeline_service.py`：`_choose_representation` 起手探 v4（单表示，不走 #212 双表示守卫）；4.x 对 zip 内官方 .md 做比值守卫 `docvortex_shortfall`（`DOC_VORTEX_MD_RATIO=0.7`，锚点按发布口径重锚：markdown_body_chars 剥页注释后 md=401，v4=429，ratio 1.0698；探针 md 全文以合成数据入测试字面冻结）+ 缺 md 显式 `docvortex_md_missing`；混合产物 v4 优先并告警 `mixed_schema_v4_wins`；未知形状 json 出稿/失败两态均点名文件（owner 自见面，产物内容不进告警）；`parse_no_text` 失败消息按三形态区分（3.x 双路空 / 4.x 单路空 / 未知形状点名），机读原因码不变（前端零改动）。
- **测试**：后端净 +39 例（adapter detect/normalize 逐字段钉、客户端拆包接线判据、管线 v4 分支与 AC5 失败形态矩阵）；全量 2570 passed 零回归；变异 9/9 KILLED（page_idx 赋值/未知型吞并/schema 前缀化/删去重/删比值守卫/客户端 v4 分支删行/删部件告警/删畸形告警/日志点名）。
- **探针留档**：`testdata/canary-issue219-mineru4-schema.pdf`（3 页合成全假数据，生成器在私有仓 `scripts/issue219/`）；设计文档与门①.5 两轮评审回执在私有仓 `docs/acceptance/2026-11-01-issue219-mineru4-schema适配-设计与验收方案.md`。
- **边界**：不改 sidecar 契约/部署形态（#218 钉死）；4.x flash 档无 seal 标记 ⇒ 公章哨兵在 4.x 暂无来源（记录在案，留待上游演进）；识别主链（OCR+HaS+LA）无关。

### 修复（Issue#221）本地 OCR 容器三修：Dockerfile 构建必败 + 模型缓存不持久 + 模型源切 ModelScope

- **为什么**：本地 buildkit（新版）起栈实测暴露三问题——① `Dockerfile.ocr` 两处 `RUN apt-get` 续行符后跟空行，`COPY`/`USER` 被吞进 apt 命令，本地构建必败（云 CI 旧解析器能容忍，门④当时未暴露）；② OCR 容器无卷，PaddleX 模型落在容器层，`stop_local.sh`（down+recreate）后每次重启重下载约 4 分钟；③ 模型源默认 `huggingface`（`paddlex/utils/flags.py:72`），与 2026-10-31 裁定「模型权重一律走 ModelScope」冲突。
- **改了什么**（纯部署面）：
  1. `backend/Dockerfile.ocr`：去掉两处多余续行符（语义零变化）；镜像内预建 PaddleX 模型缓存目录（appuser 属主）——named volume 首次使用继承镜像内目录属主，避免 root 属主导致 appuser 写不进。
  2. `docker-compose.yml`：ocr 服务加 named volume `ocr-paddlex`（挂到 PaddleX 缓存目录，模型缓存跨重建持久）；注入 `PADDLE_PDX_MODEL_SOURCE=modelscope`（配合既有的 `PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True`）。
- **测试**：`docker compose config` 通过；本地 buildkit 从失效缓存起构建验证 RUN 层不再吞行（构建成功）；容器内 `flags.py` 实读确认变量名与默认值。
- **红线**：不内嵌任何地址/口令；模型源=ModelScope。

## 2026-10-31

### 新增（Issue#217 PR③）MinerU sidecar 仓内化 + GPU 容器形态 + 喂料通道接入

- **为什么**：喂料通道（供料模式）的 MinerU 解析服务此前只在云实例侧（#75），仓内只有客户端——本地开发/小显存部署无法独立启用；且实例关机即不可用。
- **改了什么**：
  1. **sidecar 收编**（`backend/scripts/mineru_sidecar.py`）：按 `mineru_parse_client.py` 已钉契约重写（POST /tasks multipart→202→轮询 status→GET result zip；/health）。任务表内存+meta.json 落盘（重启后 completed 任务仍可取）。执行形态=to_thread+同步 subprocess（asyncio 子进程在 uvicorn 非主线程收不到退出回调）+后台协程强引用集（create_task 无引用会被 GC——任务卡 pending 的真因）。
  2. **GPU 容器**（`backend/Dockerfile.mineru`）：官方 china 版组合——daocloud vLLM 0.21.0 基底 + mineru[torch]（阿里源）+ ModelScope 模型预下（`mineru-kit models download -s modelscope`）+ sidecar 层；推理一律 GPU 原则。
  3. **4.x CLI/server 适配**：4.x 移除 `mineru -p`（改 `mineru parse --json --tier T -o <md> PATH`）；standard 档走本地 parse-server（managed 模式）。**显存现实**：12GB 全栈（NER+OCR≈5GB）下 standard 的 vLLM 引擎起不来 ⇒ sidecar 默认 tier=**flash**（实测 5s 完成真解析、无需 server、显存轻）；standard 留 `MINERU_SIDECAR_TIER` 可选。
  4. **接入**：compose `mineru` profile（16581/GPU/健康检查/缓存卷）+ backend `MINERU_API_BASE_URL` 插值；`start_local.sh --with-mineru`（本地 GPU，与 `--with-visual` 互斥校验）/`--mineru cloud`/`--cloud` 全含。
- **测试**：契约单测 +4 例（health/全生命周期/404-409 状态机/failed 带原因；fake CLI 同步 4.x 形态）。A5 端到端实测：canary PDF 经真 MinerU 4.x（GPU flash）→ **completed 5s** → zip 双产物（markdown + 4.x 结构 JSON）。
- **挂账**：4.x schema（metadata/pages）与 3.x content_list 形状不同——backend 供料适配层的 4.x 支持另案（现役生产实例 3.4.5 产物不受影响）；standard 档显存调优（mem-fraction）同案。
- **红线**：sidecar/容器不内嵌任何地址口令；ModelScope 为模型默认源。

### 新增（Issue#217 PR②）可选服务双层知情：任务入口秒级预检 + 上传页识别项旁状态徽标

- **为什么**：改前勾选视觉识别项而 LA 不在场时，任务直接发起调用——挂起场景（模型加载中/隧道半开）实测单次 240.2s、全链 ≈723s；拒绝场景虽快（3.1s）但报错无指引。用户裁定：不能等任务跑起来才知道——上传页就要看见（双层知情的用户侧），任务入口预检兜底（机器侧）。
- **改了什么**：
  1. **任务入口秒级预检（后端）**：`detect_vision` 在视觉管线启用时先探 LA `/health`（3s 短超时 + 负结果 5s TTL 缓存，与 HaS 负健康同源模式）；不可达立即 `AppError(503, "visual_service_unavailable")`，文案给三选指引（本地 `--with-visual` / 云端 `--visual cloud` / 取消勾选）。**绝不静默跳过**——印章是打码对象，漏检=泄漏（承 #199/#203 fail-closed 原则）。canary-217 改后对照实测：**0.0s 秒级失败**（对照改前挂起 240.2s）。
  2. **上传页识别项旁徽标（前端）**：`VisualServiceBadge` 三态组件（在线=干净不渲染 / 未启动=琥珀徽标含怎么开 / 快照未知=不渲染防闪误报），挂在 playground 视觉识别项卡片头；消费既有 `/health/services` 轮询（`useServiceHealth`），新增 `isServiceLive` 导出。语义边界：快照是「拉取那一刻」，以任务执行时预检为准——两层知情互不替代。
- **测试**：后端 +5 例（预检通过/秒级失败/负缓存 TTL/成功重置/AST 接线钉）；前端 +6 例（三态钉子）。canary-217 样本（testdata `697b7a8`）双向留证：改前 240.2s 长挂 / 改后 0.0s。
- **兼容性**：不改变任何既有 API 形状；预检仅在视觉管线启用时触发，OCR+HaS 主线零影响。

### 新增（Issue#217 PR①）本地 NVIDIA 开发环境基建：三模型引擎本地/云端可选 + 启动脚本

- **为什么**：开发期大量 bug 并非云端独有，却每次都要上按小时计费的 DCU 实例复现。本机 NVIDIA（12GB 级）足够跑完整功能栈调试；云实例收敛到验收（门④）/性能调优/DCU 特有确认三类场景。分支模型不变——差异由运行环境承载。
- **改了什么**（纯部署面，无模型栈代码改动）：
  1. `docker-compose.yml`：backend 三个模型服务地址（`OCR_BASE_URL`/`VISUAL_FEATURES_BASE_URL`/`HAS_TEXT_VLLM_BASE_URL`）由硬编码改为 `${VAR:-默认}` 插值——缺省值与原硬编码逐字节相同（既有用户零变化）；backend 补 `extra_hosts: host.docker.internal:host-gateway`（云端隧道地址在容器内可达）；`ner`/`ocr`/`visual-features` 在既有 `gpu` profile 之外各加独立 profile（`ner`/`ocr-gpu`/`visual`），`--profile gpu` 服务集合经比对零差异。
  2. `start_local.sh`：三档预设（默认=NER+OCR 本地 GPU、LA 关；`--with-visual` 视觉档，与本地 GPU OCR 互斥硬校验；`--cloud` 全云档=只起界面+后端）+ 引擎细粒度 `--ner gpu|cloud` / `--ocr gpu|cloud` / `--with-visual` / `--visual cloud`；真机显存探测与按档预警（`LEXMASK_VRAM_GB` 可覆盖）；端口冲突探测；云端档地址必须由环境变量/根 `.env` 提供（未配置立即报错不起半残栈），容器内自动改写 `127.0.0.1`→`host.docker.internal`；隧道连通性预检（不通=警告继续，任务级预检兜底）。
  3. `stop_local.sh`：覆盖全部 profile 落栈，数据卷保留。
  4. 文档：README 新增「本地 NVIDIA 开发环境」章节（哪个环节/给谁用/三档怎么选）；WORKFLOW.md §3.5 本/云分工规则与不对称判据成文。
- **红线**：云端只指自有实例（SSH 隧道转发，非第三方云 API）；脚本不内嵌任何地址/口令；真实案卷禁止发往第三方云服务；推理一律 GPU（CPU 推理形态不进部署档）。
- **探针锚定（P0，规则 A）**：LA 不可用两场景实测——连接拒绝（服务未启动）全真实调用链 3.1s 失败（含 2 次重试+退避）；挂起（TCP 通但不响应，如模型加载中/隧道黑洞）单次 240.2s 烧满 `VISUAL_FEATURES_TIMEOUT`，全链推演 ≈723s（重试 2 次为代码常量）——「240s 长挂」实锤属于挂起场景，PR② 的秒级预检阈值按此锚定（实测×5 且 ≤30s 封顶）。
- **兼容性（评审 I2 修正措辞——「行为变化明细」而非笼统「不变」）**：B 方案 venv 线（`cloud-deploy/`）与 DCU 实例线文件零改动；**Docker 既有线有两处有意行为变化**：① backend 镜像不再内嵌 LibreOffice——.doc 老格式转换改为运行时挂载宿主 `/usr/lib/libreoffice`（宿主需 `sudo apt-get install --no-install-recommends libreoffice-writer-nogui`，缺失时 Docker 会把挂载点建成空目录→.doc 转换失败；`.docx`/PDF 不受影响；start_local.sh 已加宿主检测警告）；② ocr 容器从裸 uvicorn（init_ocr 从不执行、全端点 503 的坏形态）改为脚本直跑+结构模式 env（修复，GPU 占用 +~3GB、健康语义变化）；③ 根 `.env` 里已配置的三个 BASE_URL 键现在开始对 compose backend 生效（旧版被硬编码压制）。`--profile gpu` 服务集合不变。

## 2026-10-29

### 修复（Issue#212）供料模式对表格型扫描件静默零识别：v2 表格正文在 content.html、适配层只认 table_body

- **现象**：生产账号上传 80 页扫描件「财产查询反馈信息表」（MinerU 3.4.5 解析 7 分 18 秒成功），喂 Agent 模式回 0 实体、空映射，UI 无任何告警；同账号 3 页文本型「执行裁定书」同流程正常出 52 实体。
- **根因**（真实产物逐层坐实）：MinerU 产出的 v2 `_content_list_v2.json` 里表格正文在 `content.html`（79 个 table 块），而 `agent_md_adapter._v2_block_to_flat` 只上浮 `table_body`（v1 键名），`clean_segments` 取不到正文即静默跳过——同产物 v1 表示 33 段/187,119 字符，v2 表示只剩 6 段/138 字符，喂给 NER 的是 143 字符（含分块分隔符）⇒ 必然 0 实体。另 8 个 `title` 块因 `_V2_TYPE_TO_V1` 缺映射被当未知块整块丢弃（机构名/表名在里面）。
- **改法**（5 面）：
  1. **适配层**（`agent_md_adapter.py`）：表格 `table_body` 缺失时回落 `content["html"]`，上浮 `table_caption`/`table_footnote`（`_parts_text` 拼入正文）；`title` → `text` 映射（`title_content`）；新增 `CleanStats{empty_table_blocks, dropped_block_types}`，空表格与未知块型不再静默——各计一数并出 warn（`empty table block: page=N` / `unhandled block type: X`）。
  2. **第二表示来源**（`mineru_parse_client.py`）：`MineruResult.content_list_v1` 同时解 `*_content_list.json`（此前只认 v2、v1 从未被读）；缺 v1 不失败。
  3. **双表示守卫**（`agent_md_pipeline_service.py`）：`GUARD_RATIO=0.95` + `_choose_representation` 八行判据（v1 缺失→v2；两路 0 字符→显式 FAILED `parse_no_text`；v1 空→v2；v2 空→v1；v2 偏少或有空表→v1；v1 被水印撑大（`short` 但 v2 无空表）→v2；其余→v2），warn 带 `winner=` 与两路字符数 `v1=N v2=M`。
  4. **可观测**（`agent_md.py` status）：恒返 `warnings`（前 20）、`warnings_total`、`dropped_blocks{empty_table,unhandled,source}`、`chars`；四端点补 owner 校验（此前 status/mapping/confirm/artifacts 完全不校验，新增字段会跨用户泄露解析细节）。
  5. **零实体安全闸**：空映射时 confirm 默认 409 `ZERO_ENTITIES_NOT_ACKNOWLEDGED`（否则等于交付未脱敏原文），须显式 `acknowledge_zero_entities=true` 并写审计 `agent_md_confirm_zero_entities`（含 actor/chars/pages/entity_types）；用户主动停用全部实体类型（`entity_types_used` 为空）豁免。前端评审步空映射时显示告警 + 知情勾选（未勾选按钮禁用），处理步显示解析告警与空表计数。
- **测试**：后端 +33 例（v2 真形态 golden 逐段 258 字、80 页同形状 79 表全解析、title/caption/footnote、空表计数与页码、双表示守卫 13 例含 watermark 撑大/两路皆空/部分丢表回落/0.95 边界/并发、API 12 例含非零 79/owner 隔离含 file_id 入口/零实体闸/豁免/审计/正向对照、客户端 12 例）；前端 +18 例（helper 2、零实体组件 2、ack 载荷 2、解析告警面板 2、页面接线契约 1 等；40 文件 297 例）。红性由探针实测（未修时 v2 夹具 1 段 33 字、`unhandled block type: title`）。
- **影响面**：所有表格为主的扫描件（银行流水、财产查询反馈表、对账单、协执清单）在供料模式下此前必然 0 实体且零提示；#197 `agent_batch` 复用同一适配器，合入后自动获益。

### 修复（Issue#209）单页 HaS NER 超越 300s 页墙：完成帽按实体值需求放开 + 残差禁整页重发 + 补查切文本 + 页级预算硬墙

- **现象**：真实案卷「第四卷.pdf」149 页，逐页识别时**第 133 页单页 NER >420s 不收敛**（页墙 300s 一到整页清零）⇒ 整卷 148/149、失败页显式记账（#199 行为正常，但该页永远救不回来）。同卷其余 148 页 8–52s 全过。
- **根因（云侧闭环实测）**：`HAS_NER_COMPLETION_HARD_CAP=2048` 与该页真实输出需求（≈200 个实体值）正面撞车 ⇒ 主查必截断（单请求 65–124s 烧满帽）；截断后**残差逻辑**用「未被解释的墨迹」重算整页（`max_chars=32000`，主查只用 6000）⇒ `types=20 text_chars=5372` 再问一遍 ⇒ 又截断 ⇒ 再 9–12 段补查。页内排两轮整页生成 + 补查链 ⇒ 300–427s。隔离实验：同一页只跑 NER = 148s 一轮通过；跑流水线入口 = 25 次请求 427s。
- **改法**：① 完成帽 2048→4096（按实体值需求），并把 `_resolve_ner_budget` 的 `context_room` 与派发侧装填判据同源（扣满 reserve+safety，否则抬帽即破 D2 不变式）；② 残差**双闸**（比例 ≥35% **且** 绝对 ≥2000 字符 ⇒ 判定为截断，禁止整页重发，改走类型补查）；③ 补查成本轴定稿为**切文本**（整段 3924 字符 × 2 类型单请求实测 72.6s，比 450 字符块慢一个数量级），类型轴只在整批真装不下完成帽时才切；④ 页级 NER 预算硬墙（`HAS_NER_PAGE_BUDGET_SEC=255`，从**页入口**起算、`asyncio.wait` 真截断不可取消的 `to_thread`、`NerDeadlineExceeded` 拒发死线后的新请求）；⑤ C2：被 max_tokens 切断的桶并入补查集合（原 `missing` 不含"存在但被切"的桶 ⇒ 尾值静默丢失）。
- **门④云实测（单页 133）**：修前 >420s 不收敛 / 页墙清零；修后 **163.30s 完成、`failed_pages={}`、entities=206**——主查 `types=13 max_tokens=3648` 59.8s 一次直出 13 桶（无截断），补查 1 批 × 9 文本块各 5.7–8.0s，残差 `7827 chars > 35% of 3924` 被闸住不重发。
- **测试**：后端 +52 例（净；含门⑥ 两轮处置新增）（完成帽装填不变式 / 残差双闸 / 补查成本轴 / 页级预算与硬墙 / 补全被切断类型 / 近重复折叠）；既有 6 例按新默认修订（#103/#104 fill-budget 算术、#198 温度逃逸、截断治理）；全量 2449 passed / 18 failed（与 preview 基线同集合的 date/safe_regex 环境性失败）/ 54 skipped。
- **门⑥ 独立评审（REVISE → 处置）**：F1 Critical（近重复折叠在「标签+不同数字」真实列上删真值：实测 120 行只剩 5 行）⇒ 默认改 **关** + 加两条判别器（含字段分隔符的行、非「CJK 碎片+结尾数字」形状的行一律不折）+ 正控用例；F2/F3（C2 并入闸按类型基数近似、残缺结果仍 `confirmed=True` 污染共享缓存）⇒ 改按「是否真的同 payload 重发」判定 + 未并入时 `confirmed=False`；F5（死线首次 NER 才起算，OCR 耗时不入预算）⇒ 页入口起算；F6（`context_room ∈ (0,64]` 时不变量可超 64 token）⇒ 取 room 本身 + 带内扫描用例；F7/F9/F10/F11 同步处置（`.env.example` 15 键登记、词法级括号扫描、装填边常量化、docstring/计数口径订正）。
- **未决/边界**：A（近重复折叠）本件目标页实测折叠 0 行 ⇒ 定位为默认关的辅助能力；`HAS_NER_COMPLETION_HARD_CAP` 的实际上限受 8k 上下文约束（≈3.6k）；`to_thread` 超时后孤儿线程仍会跑完并写缓存（已记录，未做闸位回收）。

### 性能（Issue#207）预览翻页响应滞后——前端翻页 abort+加载态 + 后端渲染链移出事件循环

- **现象**：单次处理识别进行中/完成后，预览台快速连点上下页，页码即时跳变但画面滞后且无反馈；识别期间更明显（用户实测反馈）。
- **根因**（本地 canary 实测三重证实）：①前端翻页不取消在飞预览请求、加载期间旧图驻留且无加载态、连点跑赢空闲预取；②后端预览渲染链（`file_parser.get_pdf_page_image` 的 PyMuPDF 150dpi 渲染 + `vision_service.preview_redaction` 的 PIL 编码）在 async def 内裸跑同步 CPU 活——渲染跑在事件循环线程上、渲染期间循环对一切请求无响应（`call_soon` 探针 + `asyncio.run(debug=True)` 实测 5 页连渲染单步 0.259s），每次未命中缓存的翻页冻结整个后端，识别期间叠加放大。
- **改法**：前端 `use-playground-image` 新增页代 AbortController（换页/卸载掐掉上一代在飞预览请求，含预取与 redacted）+ `previewPageLoading` 加载态（`ImageBBoxEditor` 画布角落叠加指示，保留旧图不清空、防闪白语义不变）+ i18n 中英键；后端渲染/编码移入 `asyncio.to_thread`（fitz 每调用独立 Document，与仓库既有 to_thread 先例同构）+ 同键单飞（并发同页只渲染一次，连点风暴服务端成本钉在每唯一页一次；客户端 abort 不终止已进线程池的渲染）。不改任何识别产出（响应字节与改前逐字节一致，pytest 参考序列比对钉住）。
- **测试**：后端 +5 例（事件循环不阻塞 call_soon 探针改前红/改后绿、字节一致、缓存幂等、singleflight 并发单渲染）；前端 +8 例（连点 abort、卸载 abort、redacted 同掐、加载态出现/清除/失败保留旧图、abort 竞窗晚到不生效、预取晚到只进缓存、预取回归、共享页状态力学）。门④：云实例改前基线探针 + 部署后识别中/空闲两态连点取证（详见 PR）。

### 修复（Issue#203/#199）门⑥ 二段独立复核（APPROVE with fixes，Critical=0）发现项处置

- **复核对象**：门⑥ 第二段（只读 diff/源码与自跑判据，不看会话历史）所评 head `07ce1fa`；留痕 PR#205 评论 [6475545797](https://github.com/youayou-Lee/LexMask/pull/205#issuecomment-6475545797)。两轮变异在旧测下 SURVIVED（`>`↔`>=` 边界、去分母守卫）⇒ 判据缺口坐实，本条目即其处置。
- **F1 判据口径失真**（Important）：`measure_repeated_fragment_coverage` 的覆盖分子原含**全部** `qualifying` 长词、`spanning` 只用于判空，与 docstring 及 PR 文案「≥k 次 **且** 跨 ≥m 页」不符；实测页内高频长 token 可把 coverage 放大 **7.5×**（0.0311→0.2325），出厂阈值 0.15 下会把正常卷误拒。**改法**：分子只标 `spanning` 长词（`text_layer_docline_service.py`），新增 2 枚判据（单页不跨页 = 不可见；页内高频长词只能稀释分母、不得放大分子——回到 `qualifying` 口径即判红：0.1944 > 0.15）。**AC0 名义值不变（推导，非重测）**：真实第四卷 6 种长 token 全部横跨 149 页，分子集合不变、分母不变 ⇒ coverage 仍为 0.3827；旁证 = 云侧逐页 reason 码只读取证（`fragment_kinds=6`、`max_fragment_pages=149`）。真卷不在本地，未再跑一遍完整 AC0。
- **F2 边界钉名不副实**（Important）：`test_near_threshold_volume_passes_gate` 实测 coverage 0.1232、测试阈值 0.30（差 2.44×）且不断言数值。**改法**：夹具降为 `stamp_repeats=5`（实测 0.1466）、阈值改用冻结默认 0.15、显式断言 `0.65×阈值 < coverage < 阈值`，并新增「coverage == 阈值放行」判据（封死 `>`↔`>=`）。
- **F3 新配置键无文档**（Important）：4 个 `TEXT_LAYER_REPEATED_*` 键未登记 `.env.example`。**改法**：补 4 键 + 判据口径（≥min_token_chars 的长词出现 ≥min_repeats 次 **且** 跨 ≥min_pages 页，覆盖占比 > max 即拒绝）+「0=关闸（不推荐）」+ 已知边界（只认 ≥10 字整词、约有 1 分片漏判、召回侧代价）。
- **F4 交付裁定与展示面脱节**（Important）：带失败页的条目仍报 `ready_for_delivery=True` / `action_required=False` / `blocking=False`，失败页会随 ZIP 出厂。**改法**：新增 `_is_ready_for_delivery()` 单一真源（含 `recognition_failed_pages`），`_delivery_blocking_reasons()` 增 `recognition_failed_pages` 阻塞原因，`build_export_report` 与 `iter_report_file_rows` 两处共用（旧实现各复制一份条件、易漂移）。3 枚新测（含交付摘要由 `ready` → `action_required` 的端到端翻转）；变异：摘掉失败页条件 ⇒ 3 例判红。
- **F7 假阳性警示**（Minor）：AWAITING_REVIEW 迁移原样回写既有 `error_message` ⇒ 陈旧摘要会带到干净条目。**改法**：摘要只从权威清单派生（`_failed_pages_error_summary`），空清单显式写 `None`；并让 `_FakeStore.update_item_performance` 建模真实深合并列语义（否则「写 performance → 回读 item 行」的接线在测试里断裂）。2 枚新测；变异 2 例判红。
- **F8 护栏 fail-open**（Minor）：打码前失败页知情确认在 `getJob` 失败时静默放行（一次网络抖动即可绕过最后一道人工确认）。**改法**：查询失败 **fail-closed**（中止本次提交 + 提示重试，新 key `playground.redactPagesUnknownRetry` zh/en）。3 枚新测（查询失败中止 / 有失败页弹确认且取消不提交 / 无失败页不弹确认）。
- **F9 终态无钉**（Minor）：全失败卷条目终态无断言。**改法**：新测钉「全失败卷停在 AWAITING_REVIEW + `recognition.failed_pages` 清单 + 摘要 `all N page(s)` 措辞 + skip_review 抑制」。
- **F6 证据链补齐**（Important，云侧只读取证）：①逐页 `fallback_reason_code` 清单——148 页全为 `repeated_fragment_coverage`（页 133 无该块，因 `status=timeout`），与日志唯一 WARNING（coverage 0.383 > 0.15）及 `text_layer_docline built`=0 三方互证；②区域数记录项——`file_store.data_json` 的 `bounding_boxes` 共 **7560**（148 页，与日志逐页 `Vision detect stored` 求和一致；注意 `performance_json.pipeline_status.*.region_count` 求和 41/0 **与它不同源**，不可混用）；③**显式豁免**两项——3 页人工核对（本轮止于识别、打码未执行，无可观测面）与「页 133 补查复核请求笔数」（逐页线无 docline 补查概念，N/A）。取证全程只读（未写云、未重启/停止服务、未动 `.env`）；证据文本随 PR 评论留痕。
- **F5 状态档偏差留痕**（Important）：`completed_with_failures` → `AWAITING_REVIEW` 的口径偏差在 PR 描述与 Issue 评论留痕；不新增状态档的理由 = 免 DDL CHECK 迁移 + 前端枚举 + 导出消费者三面扩散，且安全性等价或更强（AWAITING_REVIEW 强制人停 + skip_review 抑制 + 卡片/弹窗/导出三面可见）。
- **F13 已知边界**（Minor）：判据只认 ≥10 字整词 ⇒ 5~9 字水印碎片维度不可见，已在 `.env.example` 与 Issue#203 留痕（水印专用维度登记为后续项）。
- **F14 边界写明**（Minor）：`agent_md_pipeline_service.py`（#196 供料通道）不共享 `run_page`、无单页门限，故不适用 #199 的失败页记账——已在 PR 描述写明。
- **F12 数字绑 head**（Minor）：PR 描述按 `07ce1fa` 复跑数字重写（旧描述三处互不一致且仍是 #203-only 口径）。
- **AC2 措辞纠正**：真实第四卷实测为 **148 completed + 1 timeout（页 133）**，即 #199 的设计意图，非「149 页全 completed」；口径改为「无整卷作废 + 失败页经 `recognition.failed_pages` 显式记账」。

## 2026-10-28

### 修复（Issue#199）页级失败隔离——单页超时/失败不再作废整卷

- **根因**（真实第四卷门④实证）：页 133 密集表格页单页耗尽 300s（单笔主查烧帽 72s + 补查/复核 211 笔请求）→ run_page TimeoutError 上抛 → 全部兄弟页被取消 → 整 item failed、149 页成果清零。单页失败=整卷作废是 #199 立案的韧性缺口，被 #203 的 docline 接管长期掩盖。
- **改法**（`task_queue_pipelines.py`、`job_store.py`、`job_management_service.py`、前端 playground，2 commits）：
  1. **页级隔离**：run_page 只捕 Exception 系就地记失败（timeout/failed + 原因），不再上抛取消兄弟页；`asyncio.CancelledError`/`KeyboardInterrupt`/`SystemExit` 照常穿透（t6 契约钉保持绿）；`MemoryError` 显式上抛（OOM 不放大，冷审 I2）；
  2. **失败清单无条件写**：`recognition.failed_pages` 深合并在重跑全成功后清空旧键（冷审 I1）；新增 `JobStore.set_item_error_message`（同状态迁移是 no-op 不能带错误，冷审 I4 核实）挂 AWAITING_REVIEW 条目的失败摘要；
  3. **两阶段调度**：视觉特征合并阶段跳过一阶段失败页（页账保留第一阶段结论，reason 带阶段标签，冷审 I3）；
  4. **skip_review 守卫**（冷审 C1a）：带失败页的条目不 honor skip_item_review，强制停审阅——封死「无人在环直通打码、失败页当干净页出厂」路径；
  5. **导出报告**（冷审 C1b）：per-file `recognition_failed_pages` + 警示文案（error_message 会活不过打码完成，结构化 performance 才是权威载体）；
  6. **前端**：任务详情卡失败页警示行（amber）+ 打码提交确认弹窗明示「这些页不会被脱敏」（zh/en）。
- **有效性**：22 例新测/重写全绿（12 隔离 + 1 导出 + 4 个 139 契约钉反转重写 + 5 前端）；变异：摘除隔离判据 6 例判红、还原回绿；139 的 t6（CancelledError 穿透）/t3（基础设施超时上抛）保持绿。
- **门④真实第四卷实测（隔离臂 a88745c，AC2-r2）**：docline 水印闸拒绝 1 次（coverage 0.383>0.15、permanent memo 生效、零建链）→ 逐页线接管 → **条目终态 awaiting_review、148/149 页 completed、页 133 显式标记 timeout**、`recognition.failed_pages={'133': 'OCR+HaS识别 page timeout after 300s'}` 落库（无条件清单 ✓、reason 带阶段标签 ✓）；全程 3814s，无卡死、无整卷作废。实测另抓出并修复一处覆盖洗白：AWAITING_REVIEW 迁移会整列覆盖 error_message（洗掉失败摘要）——已改为显式保留并加回归钉。
- **诚实边界**：①失败页=零识别区域，打码后该页无脱敏——可见性由任务卡/确认弹窗/导出报告承载，**成品 PDF 页内无印记**（水印/印记方案登记为开放项）；②页 133 的单页成本（复核风暴+帽子燃烧，#136 家族）未压缩——本 Issue 只保证它不再拖死整卷（该页会以 timeout 标记出现在失败清单，等待人工复核/单独重试）；③error_message 是短命展示面（打码完成即清），跨迁移权威 = performance.failed_pages。

### 修复（Issue#203）水印打散字形支配的文本层卷被文档级链接管 → 整卷建链撞单页 300s 门限整卷作废

- **根因**（真实案卷 149 页卷两次独立复现，页 1 分别存活 300.004s/300.007s）：该卷每页带内嵌文本层且大量为防泄漏水印的打散碎片词（同一 17 位工号长词逐页重复上千次；真实正文极薄——卷封页图像 OCR 仅 252 字符）。文档级链（#115/#129）预建链闸（墨迹对齐+文本质量五指标+页数预算）**无重复串维度**放行 → 整卷 9 万字符切 51 块跑 NER（204 笔请求、累计模型时间 1180s、docline 并发 4）→ 建链 await 被 `run_page` 的单页 300s `wait_for` 看管 → 页 1 到点取消整卷 → 149 页成果清零；且建链被 CancelledError 打断不落 memo，重试从零再建（必然复现）。
- **改法**（`text_layer_docline_service.py`、`text_layer_doc_source.py`、`core/config.py`）：
  1. **新度量** `measure_repeated_fragment_coverage`：字符数 ≥`TEXT_LAYER_REPEATED_TOKEN_MIN_CHARS`(10) 的**长词**出现 ≥8 次且跨越 ≥5 个不同页，其覆盖字符占文档流比例。判据用**长词**而非跨词 n-gram——红测实证真实卷模板样板（「讯问笔录第X页…」行行共享前后缀）会把跨词精确重复率推到 ~0.99 淹没水印信号，而模板词因数字变化各不相同，长词级重复只属于水印/印章类高熵 token；
  2. **预建链闸**：覆盖率 > `TEXT_LAYER_MAX_REPEATED_FRAGMENT_COVERAGE`（默认 0.15）在 **`run_document_ner` 之前**拒绝（冷审 Minor-2：卷级质量门在建链后才跑，门在后杀在前），机读原因码 `repeated_fragment_coverage`、**permanent 负缓存**（重试不重建、不再撞墙，AC4）；**PII 最小化**：reason/detail 只带计数与占比，不带命中片段字面量（水印串含真实姓名+编号）；
  3. 阈值语义向「宁可错拒」偏置：错拒代价=回落逐页线（慢、召回不降），漏拒代价=整卷作废。
- **AC0 测量（真实数据，AST 逐字同源抽取的度量实现）**：水印卷 coverage=**0.3827**（6 种长 token 横跨全部 149 页）；真实干净文本层卷（eval37-real 有层两份）coverage=**0.0**；判别间隔 ≫5×。权威块数 51（修正 Issue 正文 "~117 块"——117 笔 types=6 主查行是主查+残差全标签复调的叠加形态）。
- **有效性**：9 例新测全绿（拒绝支路：零 NER 调用断言 + permanent 负缓存 + PII 无字面量；放行支路：页眉/模板样板卷、贴阈值卷、混合卷 `covered_pages` 语义不受影响）。
- **诚实边界**：①本修复只修路由与预算，**不提升水印文本本身的识别与脱敏覆盖**（逐页线对水印串的处理是既有行为且未经验证，现场观测逐页 OCR 仅读出约 8 处水印实例）——已知限制登记于 #203/#204；②#204（O(整卷) 建链受 O(单页) 门限 + 取消不落负缓存，不依赖水印）仍开放，干净的大页数文本层卷同处风险区；③AC0 干净侧样本量有限（真实干净文本层卷 2 份 + 单元级模板样板夹具），阈值 0.15 基于实测 0.3827 下方 2.5× 余量冻结，后续新形态卷若误伤/漏杀以 AC0 同款测量复核。
- **零回归与评审**：全量 pytest `comm -3` 与 preview 基线对比（见 PR 证据评论）；验收方案经门①.5 冷审 r1 REVISE（9 条发现全采纳）→ r2 APPROVE 后开工。

## 2026-10-27

### 修复（Issue#198/#202）高重复表格页触发模型复读 → 单页 180s 超时整卷作废

- **根因**（真实案卷 134 页卷第 34 页两次复现实证）：「免税购货记录」高重复表格页使 temp=0 贪心解码走进复读循环，主请求烧满 max_tokens=1992（84s，finish_reason=length）；截断补查**继承温度 0** 对同文本同轨重发 → 又复读（76s）。双循环 160s 撞 `BATCH_RECOGNITION_PAGE_TIMEOUT=180` 单页门限（实测贴线 179.99s vs 180.0s）⇒ **任一页超时即整卷作废**，134 页成果清零。
- **改法**（`backend/app/services/has_client.py`、`vision/has_text_analysis.py` 关联、`core/config.py`、`frontend/src/i18n/{zh,en}.ts`，4+2 commits）：
  1. **补查温度下限** `HAS_NER_TRUNCATION_RETRY_TEMPERATURE=0.7`（max 只升不降，自洽趟不拉低）；
  2. **补查按行边界分块**（>600 字符切 450/块，逐块补查并合并）——门④实测**温度逃逸不充分**（temp=0.7 补查同文本仍复读，观测面抓到 `finish_reason=length confirmed=False recovered=1/7`）后追加；
  3. **补查逃逸观测面** `HaS NER re-query finished: finish_reason=… confirmed=… recovered=n/n`（区分「逃逸」与「被封顶」，正是它抓出了温度层不足）；
  4. **流式复读掐断**（Issue#202）：`_call_model` 改流式增量解码，每 256 字符检测循环签名（尾部 1KB 内 ≥24 字符片段 ≥3 次）命中即断连——种子复读 66-84s → ~5-10s 掐断；非 SSE 应答直接采纳响应体、HTTP 状态/传输错误原样上抛、流处理缺陷回落非流式；`HAS_STREAM_LOOP_ABORT` 开关默认开；
  5. **单页门限 180→300s**（修复后病理页实测 245→133s，180 无尾部余量）+ i18n「3 分钟→5 分钟」。
- **有效性（门④云实测，scnet-main 隔离臂）**：基线红跑复现事故全环（84s+54s 双循环 → 180s 超时整卷作废）；修复臂**5×绿跑全部完成**（231-286s）；追加流式层后病理页 **133s（-45%）**、100 实体/198 区域（恢复更全）；30-40 页 11 页合卷 **262s（-37%）** 零超时；正常页零变化、掐断零误伤（16 次全部命中表格页真复读）。
- **零回归**：全量 pytest FAILED 集合与基线 `3e362df` `comm -3` **0 行**（14 个失败均为本地环境性：SOCKS 代理/evalng ImportError）；新增 14 例专测全绿（温度 6 + 分块 3 + 流式 5）；变异验证：摘除补查温度/检测调用均判红。
- **评审**：验收方案经独立冷审「需修订」后并入 2 Critical 判据修订（补查自身三断言 + 5×重跑防假pass；终态产物比对防「漏检换幻觉」）再实施；门④ A/B 全程生产 6 服务零扰动。
- **诚实边界**：①合成 canary 在基线未复发（合成表 OCR 分组形态与真实页不同），病理红/绿证据以真实第 34 页 A/B 为准，样本保留为 GT 召回回归样本；②流式层 AC3「<120s」实测 133s 未达，余量属恢复链固有成本，挂账观察；③「解析失败分批递归」路径同样继承温度，本期不改（无实证案例 + 分批自带缓解），Follow-up #200 + 特征化测试钉住；④单页超时仍整卷作废（本次仅放宽门限），页级韧性改造在 #199。
## 2026-10-19

### 新增（Issue#196）供料模式（原「喂Agent」）加入批量 + 全站更名

- **更名（D1 拍板）**：模式定名**供料模式**——只改用户可见文案（zh/en i18n、README、接口基线标题），接口路径 `/agent-md`、内部标识 `agent_md_*`/`agentMd.*` key、占位符枚举 `placeholder` 零变化（改名判据进 CI：zh「喂Agent」/en「Feed Agent」旧词清零，可删即红）。
- **批量第四通道（`agent_batch`）**：批量入口新增供料卡；五步向导按需裁剪——step1 只选文本识别项（替换方式锁定占位符，隐藏图像打码/水印）；step2 全格式放行（pdf/扫描/图片走 MinerU 解析，docx/doc/txt/md/rtf/html 文本直取，D3 拍板）；step3 队列逐份解析+NER（阶段进度 parsing/ner）；**step4 为逐份映射表确认**（保留原文/自定义替换值/缺省占位符，D2 拍板不做页面级预览；一键确认沿用 bulk_confirm 权限门控）；step5 导出 ZIP 含每份 脱敏MD+映射表+保留清单。
- **后端**：新增 `JobType.AGENT_BATCH`（DDL CHECK 探针迁移至 `agent_batch`，旧库启动自动重建、幂等）；识别工人新增供料分支（草稿落 file_store `agent_md_draft`，watchdog 重入队幂等短路）；`commit_review`/`commit_all_reviews` 供料分支（渲染三产物替代 Redactor，同步完成不入队 redaction）；`reject` 语义=清草稿重识别；新增 `GET /jobs/{id}/items/{item_id}/agent-md-mapping`（与单文件 mapping 契约同构）；导出 `vl_md_meta` 附加入包。化名门控三入口对供料批同样生效。
- **修正（本地 4.x 栈验收实测，2026-11-03）**：批量供料线的 MinerU 表示选择改接单文件线同款 `_choose_representation`——#220 的 4.x 归一（docvortex.middle）此前只接进单文件线，批量线只读 3.x `content_list_v2`，4.x 形态下静默出空稿（同一 PDF 单文件 8 实体/批量 0 实体 A/B 实证；生产实例 3.4.5 有 content_list_v2，云冒烟测不出，#217/#219 双 schema 并存设计所抓的形态差异）；并补 fail-closed 守卫——解析不出正文时 item 显式 FAILED（`parse_no_text` 三形态措辞与单文件一致），禁「0 映射静默就绪」。后端净 +5 例（4.x 经批量线出段 / 3.x 不回归 / 空稿 fail-closed / 未知形状点名 / 工人端到端 FAILED），专项 26/26 绿、全量失败集与基线逐字节一致（零回归）；端到端复验：canary-issue219 批量线 8 实体/成品 md 451 字符（修前 0/0）。
- **修正（浏览器 UI 验收实测，2026-11-03）**：供料向导三处接线缺口——①step1 图像总览卡未随配置卡一起隐藏（`BatchStep1PreviewCards` 门控漏 agent，与「只选文本识别项、隐藏图像打码配置」相悖）；②向导任务恢复白名单漏 `agent_batch`，任务中心「继续审阅」深链对供料批误报「任务类型不匹配」退回 step1；③hydrate 行装载缺 `item_id`，刷新后供料 step4 审阅列表恒空（行来自文件列表 API 时自带、深链恢复时缺，故「初进可用、刷新必坏」）。前端净 +7 例（组件门控 3 + 恢复白名单 2 + 行装载 2，先红后绿），318/318、tsc 0 错、build 绿；浏览器复验 step1/step4 全过。
- **边界**：单文件供料动线零改动（仅文案更名）；既有三条批量通道与 `vl_md` 直通分支零改动。

## 2026-10-18

### 修正（Issue#153）`D1.corpus_premise` 改按「权威视图」判定——推定形态不再误报 FAIL

- **根因**：「扫描件 + 内嵌文本层」的卷（本地 `pdftotext` 有内容、后端按整页底图判 `is_scanned=True`）两侧视图**都合法**，旧判据要求两侧一致 ⇒ 该形态一律 `FAIL`；#115 全库重判显示真实案卷 **229/256** 属此形态。
- **改法**（`eval-ng/cli.py`，2 文件 +251/−23）：清单新增可选 `carrier_view`（`backend`/`local`，非法值报 `_Usage`）与内部 `carrier_source`（`declared`/`inferred_local`）；判据改为**权威视图**——①显式声明且未指定视图 ⇒ 以**后端路由视图**为权威（不符 ⇒ FAIL；相符 ⇒ PASS，本地计数降为观测项）；②显式指定视图 ⇒ 只按该视图（取不到 ⇒ UNVERIFIED）；③**形态由本地推定**（清单没写 `carrier`）⇒ 后端不一致判 **UNVERIFIED**（点名两视图 + 已知形态提示），不再误报 FAIL；④显式声明但无后端旁证 ⇒ 退回本地视图。
- **有效性**：改前树 + 本 PR 测试文件 ⇒ `TestCorpusPremise` **5 failed / 7 passed**（其中 **3 条状态级**：推定不一致 PASS、`carrier_view=local` 被旧码忽略、推定+缺计数 PASS；2 条仅文案归属不同）；改后 `tests/test_evalng_speed.py` **133 passed**。
- **零回归**：同刻两树全量 `pytest tests/` —— FIX **2316 passed / 47 skipped / 0 failed** vs BASE **2306 / 47 / 0**，`FAILED` 集合 `comm -3` **0 行**（差 +10）。
- **评审**：门①.5 r1（REVISE，2 阻断 = 推定分支缺判据 / 非法 `carrier_view` 零判据）→ r1b（Approve）→ 门⑥ r6（With fixes，抓出**推定+缺本地计数曾报 PASS**，即「声称验过不存在的测量」）+ 增量窄复核（B-1/I-2 双闭合，三条自造变异 KILLED）。
- **诚实边界**：AC1 的「已知形态提示」串（`#115 229/256`）**无断言**（纯 UX 文案）；AC4 的零回归属过程证据（无仓内用例）；本改动为纯逻辑、无 GPU 面上云项。

### 可观测（Issue#165）文档级链路失败卷的回落原因可见

- **根因**：`boxes_for_page()` 在「本该接管但判定失败」时返回 `(None, None)`，原因只落进进程内负缓存 `_MEMO`；`detect_vision` 只在 `doc_memo is not None` 时写 `pipeline_status["text_layer_doc"]` ⇒ 失败卷上该块**整块缺失**（而 `0 框` 那条路径反而有块），现场分不清「没有文本层 / 超预算 / 质量不可用 / 块失败 / 回映失败率 / 块错 / 截断比 / 覆盖率」。
- **改法**：①`_TransientIneligible` 携带机读 `code`/`detail`/`permanent`，卷级门逐条传码（`no_text_layer|build_budget|verdict_unusable|failed_chunks|map_failure_ratio|block_errors|truncated_chunks|coverage`）；②失败卷返回值由 `(None, None)` 改为 `(None, marker)`（含 `reason`/`reason_code`/`reason_detail`），**负缓存命中同样带原因**；③兜底 `except Exception` 带 `reason_code="unexpected"`（该分支正为「入口参数不可信」而存在，日志格式串一并由 `%d` 改 `%s`，否则日志自身抛错会把请求打成 5xx）；④块构造抽成纯函数 `redaction_orchestrator._text_layer_doc_status()` 并在失败卷上写出，新增 `fallback_reason`/`fallback_reason_code`/`fallback_failed_chunks`/`fallback_chunks`/`fallback_page`（旧字段逐字段保留）；⑤删除全仓无读者的 `stats["failed_chunks"]` 死计数器，失败块计数唯一真源 = `len(failed_set)`。
- **显式边界（门⑥ r6 订正）**：**显式不适用**（开关关 / 明确不跑 HaS / 无标签 / 非扫描件）仍返回 `(None, None)`、仍静默；**页面级回落分两种，均与改前一致**：①「混合卷本页无文本层」⇒ `(None, None)`、状态面**无**信号，只有一行 INFO（**不是** warning）；②「覆盖页 0 框」⇒ `(None, memo)` ⇒ **有**块（`failed=True`）且带既有 `text_layer_doc:fallback_page=N` warning（base 固有行为，本 PR 未改）。另：**扫描件但整卷无文本层**（`no_text_layer`）判「真回落」并产信号——理由与裁决见 Issue#165 评论（该形态是本仓库最常见真实形态，改前只有一行 INFO）。
- **契约变更（唯一一处）**：`boxes_for_page()` 第二个返回值在**判定失败**时由 `None` 改为 marker；仓内唯一生产调用方已同步，8 例既有用例按新契约更新（`boxes is None` + `reason_code` 等值）。
- **有效性证明**：改前树（`c1a8fd9` + 本 PR 测试文件）`tests/test_issue115_text_layer_docline.py` **9 failed / 36 passed**，块用例文件 **ImportError**（`_text_layer_doc_status` 不存在）；本树三文件 **68 passed**（base 三文件 57 ⇒ **+11**）；变异矩阵 **7/7 KILLED**（含门⑥ r6 的自造项：接线换回旧内联 dict、`no_text_layer` 改 `permanent=False`、`fallback_page` 写常量）（失败卷退回 `(None,None)` ⇒ 7 failed；块去掉 `fallback_reason_code` ⇒ 1；复活死计数器 ⇒ 1；不适用也产信号 ⇒ 2），每次按字节还原。
- **零回归**：同刻两树全量 `pytest tests/` —— FIX **2317 passed / 47 skipped / 0 failed** vs BASE（`c1a8fd9`）**2306 / 47 / 0**，`FAILED` 集合 `comm -3` **0 行**，差 **+11** = 本 PR 新增/加固用例数。
- **未上云**：失败卷真机观测面（`pipeline_status` 经任务状态返回）需实例 + 真卷，与 #148 对该观测面的处置同源 ⇒ 门④ 待跑。

### 工具链（Issue#157）前端测试文件纳入 `tsc`

- **根因**：`frontend/tsconfig.json` 的 `exclude` 排除 `src/**/*.test.ts(x)` 与 `src/**/__tests__/**`，而 CI 里 `npm test` 照跑 ⇒ **测试文件从不经过类型检查**，测试里的类型错误永远发现不了。
- **改法**：新增 `frontend/tsconfig.test.json`（extends 基础配置 + `noEmit` + `types: ["vitest/globals"]` + `include: ["src"]` + `exclude: []`），CI 前端 job 在 Build 之前新增一步 `Type-check tests (Issue#157)`：先 `npx tsc -p tsconfig.test.json`，再用 `--listFiles` 计数与 `find src -name '*.test.ts*'` 的盘上实数比对（漏一个即 `::error::` 退出 1，只 `grep -q` 的「≥1 地板」挡不住「只漏一类」）。
- **关键取舍：不引入 `@types/node`**——引入后全局 `setTimeout` 的重载来自 `@types/node`，三处 `let timer: ReturnType<typeof window.setTimeout> | null` 的**声明侧**会被解析成 `NodeJS.Timeout`，而 `window.setTimeout(...)` 返回的实参仍是 `number` ⇒ 三处赋值处报 `TS2322`（`use-batch-files.ts` / `use-history-data.ts` / `use-jobs.ts`），**等于让类型门改产品类型面**；因此把原本依赖 `node:fs`/`node:url` 的接线契约用例改成 Vite `?raw` glob 读源码，并修掉 `utils.visual-preview.test.ts` 里一处 `(b: never)` 标注。
- **有效性证明（AC2）**：判据命令 `npx tsc -p <config> --listFiles | grep -c '\.test\.'`：旧门 `tsconfig.json` = **0**、新门 `tsconfig.test.json` = **24**（22 个 `.test.ts` + 2 个 `.test.tsx`，与盘上实数一致）；注入 `const __i157_probe: number = "boom";` ⇒ 新门 rc=2（`TS2322` + `TS6133`）、旧门 rc=0；冷审另用 `.test.tsx` 注入复现同一判别（新门 rc=2 / 旧门 rc=0）；未修树一次性对照：`utils.visual-preview.test.ts(97,23) TS2345` + 接线用例两处 `TS2307`。
- **耗时（AC3，数字带 head 归属）**：本机新门 6.08 / 6.60 / 6.81 s vs 旧 `tsc` 6.31 / 6.77 / 7.19 s；**CI 侧 5.0 s** 量的是加断言**前**的 head `1c5eaa5`（run `39060724050`，前端 job `117477946925`，该版只跑一条 tsc）；本分支加了计数断言后该步跑两次 tsc，本机整步 **12.73 s**（CI 预计 9–10 s），以合入 head 的 CI 实测为准。
- **零回归**：`npx vitest run` 24 文件 / 166 例全绿；`npm run build` 绿（4.31 s）且 `dist` 无测试产物。
- **同族盲区（登记 follow-up，不在本 PR）**：`frontend/vite.config.ts` 在 `tsconfig.node.json` 的 include 里，但仓内**没有任何程序跑 `tsc -b` / `--build`** ⇒ 它也不在任何被执行的程序内。
### 修复（Issue#142）公开树扫描器 bearer 规则大小写对齐 + 文档精度

- `eval-ng/scripts/check_public_tree.py`：bearer 规则补 `(?i)`（与 `eval-ng/engine/speedkit/report.py` 的 `_BEARER_RE` 对齐，消除同族规则大小写口径漂移）；自检夹具补小写/混合大小写正例与 `&lt;24` 字符反例；`_COVERAGE_NOTE` 措辞精确化（尺寸写不敏感、声明与报告侧 **12 vs 24 的阈值差异是有意保留**、声明 base64 形态属安全侧多报），并修正一处**非法正则引文**（`(?i)\bbearer\s+{24,}` → 完整正则）。
- `eval-ng/scripts/check_report_redaction.py`：符号引用 `check_public_tree.SECRET_RES`（该符号不存在）→ `check_public_tree.credential_findings`。
- `backend/tests/test_public_tree_scanner.py`：新增「夹具存在性 + 真落网」判据（删夹具即判红，堵住 r1 冷审自造的 M3/M3b 存活）。
- `eval-ng/engine/speedkit/report.py` 的口径注释改动**撤回**：该文件含 11 处既有 `abs_path` 命中，任何改动都会被 G4 文件级判定误红 ⇒ 设计修法另立 **#187**（改为只扫新增行）。
- 验证：`--self-test` rc=0（21 例 / 11 类 / 27 夹具）；`backend/tests/test_public_tree_scanner.py` **33 passed**；改动面 `--base origin/preview` **零命中**；整树新旧扫描器命中行 `diff` **0**；零回归同刻两树均 **2300 passed / 47 skipped / 0 failed**（`comm -3` 0 行）；变异 **6/6 KILLED**。

### 工具链（Issue#160）公开树扫描器：中文取值口令不再漏判 + `ignore-line` 改为按规则点名

- **漏判修复**：`_is_secret_shaped()` 原先对含汉字的值整类返回 `False` ⇒ 形如「中文取值口令」的字面量在公开树上**永不报出**（具体字面量见 Issue#160 与 `backend/tests/test_pdf_encryption.py`）（#140 的验收标准当时其实被静默绕过）。现改为：含汉字的值长度下限降到 **6**，但必须满足**混排证据**（含数字/ASCII 字母/符号），并排除含中文标点的正文与 `2023年6月15日` 形日期 ⇒ 中文口令命中、中文正文不误报。
- **放过面收紧**：`ignore-line` 从「整行跳过」改为**按规则点名** `ignore-line(<规则名>, …)`；**裸标记不再放过任何规则**；新增自检契约 `_ignore_line_scope_check()` 实证「点名的放过、未点名的照报、裸标记不放过」。仓内既有标记按规则重算迁移（`git grep -c "public-tree-scan: ignore-line"` 合计 preview 76 行 → 本修订 82 行；含 `ignore-line(` 者 83 行），并订正 3 处**真**空括号标记；迁移后整树命中与旧版**逐条相同**。
- **口径声明**：含汉字的值在 Issue#160 之前整类不参与判定 ⇒ 该 Issue 前后的命中数**不可跨版本对账**（同版本内 A/B 才可比），已写入 `_COVERAGE_NOTE`。
- **验证**：`--self-test` 21 例阳性对照逐例命中 / 11 类全覆盖 / 干净样本零命中 / 四项自检契约通过；`backend/tests/test_public_tree_scanner.py` **37 passed**（新增五例：中文口令命中且中文正文不误报、`ignore-line` 规则级语义、`scope_check` 调用接线钉、空括号标记不放过、`scope_check` 函数体可判别性）；变异 **9/9 KILLED**（绑定本修订源码 sha256，见 `LexMask-workspace/github-issues/_i160/mutation.txt`；含「删 `scope_check` 调用」「掏空 `scope_check` 函数体」「空括号＝整行豁免」「去中文标点/日期排除」「去转义引号归一」）；同树旧/新扫描器输出 **逐条相同**（1007 文件 / **326 处**：`abs_path` 286 / `public_ip` 24 / `win_path` 16，`credential_*` 0，命中行 `diff` 为 0）。
- **不做**：不放宽任何规则；不动 CI 接线（`public-tree-scan` job 由 #159 建好）；纯汉字长口令（如 `"一二三四五六七八"`）仍在边界外（方案 §6.1）。
### 正交（Issue#135）`safe_finditer` 超时零计时协议用例

- **背景**：`safe_compile` 的超时协议在 #109 已有零计时用例，但同族的 `safe_finditer` / `safe_finditer_many` 的超时分支（`future.cancel()` → `_kill_and_replace_pool()` → 抛 `RegexTimeoutError`）**没有任何协议用例**；漏掉任一步都只会静默退化。
- **改动**：新增 `backend/tests/test_safe_regex_finditer_protocol.py`（16 例）：换池恰好一次、`cancel()` 被调用、异常类型与 message、命名组透传、批量顺序/空输入早退/单条失败隔离、非超时异常不得换池、真实池集成 2 例（受限环境按惯例显式 skip）。
- **零回归**（同刻两棵干净 worktree）：fix `05a505e` **2300 passed / 47 skipped** vs 基线 `a29b22a` **2284 passed / 47 skipped**，`comm -3` FAILED 集合 0 行（差 +16 = 新增用例；两棵在本机各有 6 条 json_repair 家族既有失败，两树逐条相同）。
- **AC4 的 CI 侧直接证据**：分支 backend job `2294 passed / 53 skipped` vs 基线 `2278 passed / 53 skipped` ⇒ passed +16、skipped 不变 ⇒ 16 例（含两条真池集成）在真 CI 上真执行。
- **变异**：V4（`except FuturesTimeoutError` 放宽为 `except Exception`）、V10（`if not future.cancel(): return []`）均 KILLED，逐字节还原校验 sha256。
- **遗留（已立案）**：`_MatchProxy` 的 `or []` 兜底分支不可达（冷审 Minor M3）⇒ 另立 #186。
- **不做**：不改 `safe_regex` 行为（纯测试）。
### 正则（Issue#156）超时可观测 + 真 `re.Match` 命名组语义（**生产行为不变**）

- **可观测性**：`backend/app/core/safe_regex.py` 新增进程级累计计数 `regex_timeout_stats()`（三条链标签 `safe_compile` / `safe_finditer` / `safe_finditer_many`），经 `GET /health` 的新字段 `regex_timeouts` 暴露（`backend/app/main.py`、`backend/app/models/common.py`）。既有 `_regex_timeout_events` 属**另一口径**（沿用旧用途），两者不互相替代。
- **门⑥ r6 处置（零行为改动）**：订正 `_declared_named_groups` 文档串（子进程载荷**刻意不传**声明表，代理只回参与组名）；把恒真反面对照 `test_successful_call_does_not_bump_counters` 改为**真跑一次成功路径**后断言计数为 0；A11 用例补「值变窄 ⇒ 命中数不增且命中区间被宽值区间包含」；新增 `get_type_hints` 契约用例钉住子进程载荷为 **4 元组**。
- **真 `re.Match` 命名组语义**：按声明序取组、空组不占位；「声明了命名组但全组未参与 ⇒ 不产出、不回退整 match」**只作用于真 `re.Match` 路径**。
- **⚠️ 行为边界（门①.5 r1 抓出并修正）**：该规则**不得**施加到生产路径（`_MatchProxy`，来自 `safe_finditer*` 子进程）——preset `DATE` 的单日期分支没有命名组（只有区间分支有 `seg_a/seg_b`），一旦越界，单日期实体被整条吞掉（`本院于2022年1月25日立案受理` 的 `2022年1月25日` 变成 0 条 = **漏打码/欠遮盖**）。修法=撤销子进程 payload 的五元组扩展（回到 4 元组，既有 stub 池兼容），生产路径**逐位保持**旧契约（无参与组 ⇒ 回退整 match）。新增生产入口回归闸 `backend/tests/test_issue156_date_preset_regression.py`（走 `HybridNERService._custom_regex_extract` + 真实 preset）；两路径在「命中分支无命名组」时的**按设计分歧**另有专门用例钉死。
- **A 组边界用例**：A2（后缀语境只出 DATE + 改前形态反面对照）/ A8（真 match 声明未参与 ⇒ 空）/ A10（跨 alternation 参与集合两路径逐位一致）/ A11（窄值落在长词内部 ⇒ 零命中、区间右端不得多吃字符）；新增 4 个用例文件共 **17 例**，另在既有文件补 1 例（子进程侧取序基准）。
- **零回归（本机 3.12、同刻两树、`PYTHONPATH=<临时 pylibs 目录>`）**：修复树 **2270 passed / 6 failed / 47 skipped** vs 干净基线 `26d11eb` **2252 passed / 6 failed / 47 skipped**，`comm -3` 的 FAILED 集合 **0 行**（无新增、无消失）；差 **+18** = 新增 17 例 + 补 1 例。6 条既有失败两树逐条相同（json_repair 家族等环境依赖），显式标注为既有。
- **变异**：9/9 KILLED（M1/M2 计数、M3' 代理退化、M4 真 match 声明、M5 判据退回、M6/M7 词边界、M8 `/health`、M9 子进程取序）。Issue 点名的 `groupdict()` 变异经复核实为**等价变异**（`re.Pattern` 无 `groupdict()`、6 种形态取序恒等）⇒ 已在 `mutation.txt` 写明理由并以替代变异覆盖。
- **IPC 量化（AC5，本机，云未测 ⇒ 只作量级）**：vision 链每页每类型 2 次子进程往返（`safe_compile` + `safe_finditer_many`，无编译缓存）；冷启动首次 `safe_compile` 39.91 ms、热态 2 次约 0.56 ms、190 组约 0.11 s ⇒ 调用次数不变、成本可忽略。
- **已知缺口（不影响本期四项交付，列二轮）**：A11 左边界与「同一命中重复登记」无判别用例；worker 内部超时形态无覆盖；超时计数为进程级累计（多 worker 需外部聚合）。
- **对外契约变更（唯一一处，向后兼容）**：`GET /health` 响应新增字段 `regex_timeouts`（进程级累计计数；**无超时发生时返回 `{}`**，字段始终存在）。此处「生产行为不变」的**限定范围**＝脱敏/NER 判定行为不变；该字段是新增只读观测面，多 worker 部署下只反映**被问到的那一个进程**。
- **两条路径的分歧是有意设计，不是缺陷**：真 `re.Match` 路径「声明了命名组但全组未参与 ⇒ 不产出」；`_MatchProxy`（生产唯一来源）路径保持既有契约「无参与命名组 ⇒ 回退整 match」。统一二者需要改 preset（给 `DATE` 单日期分支补命名组），属另一 Issue 的范围。
- **门①.5 r2 收尾处置（2026-10-18）**：清理收窄后残留的**误导性死管道**——两处返回注解改回 4 元组、删除零调用方的 `_MatchProxy.declared_groups` 形参与槽位、`declared_named_groups()` 与 `match_items` 的注释/docstring 重写（原 docstring 声称「两条路径都不回退」，与其自身用例矛盾；照它把声明表恢复传输会**重新引入漏打码**）。r2 独立复核结论：B1–B6 实质全闭合，自造 R2-1（恢复子进程声明表传输）⇒ 19 failed ⇒ 新闸真有判别力；变异 **12/12 KILLED**。

- **证据**：`LexMask-workspace/github-issues/_i156/`（`mutation.txt`、`ipc-cost.md`、`regression-found.md`、`pitfalls.md`、`r1/` 四件交付）；方案 `issue156-regex-observability-plan.md` 含 §7 r1 逐项处置表。

### 视觉（Issue#96）公章来源可切 MinerU —— 门④ AC2 判 **FAIL**，以「默认关」形态合入

- **背景**：#92 四臂单源对比显示 `mineru_seal`（58.0%）优于 `la`（43.5%），据此立项把公章框来源切到 MinerU。实现完成：框级替换（`official_seal` 照旧下传 LA，VL 印章通道不受影响）、失败回落 LA、新增三个配置键与 26 例单测、页号/单位/漂移三类口径缺陷修毕。
- **门④ AC2 判定（生产链配对：T3 池 43 个带章页 / 69 个章级 GT）**：`la` 臂章级召回 **0.5942**（41/69）、精确率 0.8723、误报每页 **0.140**；`mineru` 臂召回 **0.5652**（39/69）、精确率 **0.9286**、误报每页 **0.070**（分母统一为 43 个带章页）。**L1「并集不劣化」FAIL**（0.5652 < 0.5942 − 0.02，超阈值 0.90 pp）；同臂重跑噪声地板 **0.00 pp**（两臂命中集合逐位可复现）⇒ 差异真实、非抖动。L2/L3/L4/L5 PASS（补充域 win=4 / loss=6 / tie=24）。
- **#92 的四臂数字不可迁移**：四臂是「单源直测」；走生产链后 LA 受益于 `_merge_seal_shards` 等合并逻辑（43.5% → 59.4%），MinerU 反落到 56.5%。6 条丢失的章框面积 47k–303k px² 不等，无单一可调旋钮。
- **保留的真实收益**：精确率 +5.6 pp、误报/页减半；以及 **LA 不可用时的可用性兜底**（G1 演练：LA 端点黑洞下 `la` 臂 10 页 0 章、`mineru` 臂 10 页 6 章）。
- **处置（用户 2026-10-18 拍板 (b)）**：`VISUAL_SEAL_SOURCE` **默认保持 `la`（零行为变化）**，MinerU 通道作为显式可选能力合入；「章级召回不劣化」记为未达成并另立 follow-up Issue（扩池 / 调 MinerU 盒并 / 换版本）。新增 `test_visual_seal_source_default_is_la` 钉死默认值——任何人把默认翻回 `mineru` 都会被这条用例判红。
- **未跑项（显式声明，不静默跳过）**：AC3（喂 Agent 五卷端到端）与 AC4（服务拓扑 + 签名召回）**NOT-RUN**——星判据 AC2 FAIL 后按预注册规则停止，不为其继续投入；将来若启用 MinerU 通道需补跑。
- **门⑥ 一审处置（2026-10-18）**：①**NaN 组件**不再静默入箱（`_normalize_bbox` 显式拦 `v != v`，4 条负样本用例；**不用 `isfinite`**——那会把 `inf` 从「单位漂移」误判成「畸形」）；②补**默认关哨兵用例**（`source=la` 时不得构造 MinerU 通道、不得读源文件、原样返回同一 list——门⑥ 自造变异 M-6a/M-6b 此前 SURVIVED，现已 KILLED）；③误报/页口径订正为 0.140/0.070；④三个新键补进 `.env.example`。
- **门⑥ 二审待办（登记 follow-up）**：失败态永久缓存（一次抖动整卷退回 LA）、`last_seal_source_*` 暂无生产读者、`union` 模式需先扩展 validator。
- **证据**：`LexMask-workspace/github-issues/_i96/ac2-gate4-verdict.md` 与 `_i96/ac2/`（两臂四次运行原始 JSON、G1 演练、运行日志）；方案 `issue96-seal-mineru-plan.md` v10.3。

## 2026-10-17
- feat(playground): **识别期间渐进渲染——首屏从「整卷跑完」提前到第 1 页回流**（Issue#123）
  - **根因（门①.5 r1 抓出；v1 只诊断了第二层）**：`stage` 初值 `upload`，直到识别成功才切 `preview`（`frontend/src/features/playground/hooks/use-playground-file.ts` 的识别收尾）⇒ 整卷识别期间**结果画布根本不在渲染树里**，逐页回流的框无处可显（10 页 canary 总墙钟 86.74 s，而第 1 页在第 8.7 s 就已完成）；只把全屏遮罩降级为 transparent（v1 方案）**收益为 0**
  - 新增 `frontend/src/features/playground/lib/progressive-render.ts`（纯函数，供表现层与单测共用）：`shouldEnterPreviewDuringRecognition`（多页扫描件/图片 ⇒ parse 完成即进 `preview`，单页件与文本件保持既有行为）、`shouldUsePassiveOverlay`（仅「加载中 + 多页流式」才降级遮罩）、`overlayModeFor`（页面实传档位——单独抽成纯函数，使接线层取值可被单测判别）
  - `components/playground-loading.tsx`：新增 `overlay` 档位。默认 `blocking`＝改前行为逐字节一致；`passive`＝容器透明 + `pointer-events-none` 不遮挡，面板自身 `pointer-events-auto` 仍可交互（进度与取消入口保留）；`data-overlay` 属性供判据定位
  - **两条路径都接线**：上传路径（`use-playground-file.ts` 记录流式进度 + 提前切段）与**重跑路径**（`use-playground-image.ts` 的 `handleRerunNerImage`——r1 指出它是「唯一已有画布挂载」的路径，v1 漏接）；`use-playground.ts`/`playground-context.tsx` 透传 `visionStream`
  - 交互粒度沿用既有 `lockDraw` 语义：passive 期间可缩放/滚动查看、不可改框（不与 #106 的 in-flight 页并发交错写入）
  - 新增 `frontend/src/features/playground/progressive-render.test.tsx`（**17 例**）：两个谓词的边界（多页/单页/文本/页数为 0/加载态）、`overlayModeFor` 接线档位、`PlaygroundLoading` 的 blocking/passive 契约（类名、pointer-events、面板可交互、进度与取消入口保留）、**端到端档位链 3 例**（多页扫描 → passive；单页扫描 / 多页文本 → blocking）；另在 `frontend/src/features/playground/lib/playground-draft.test.ts` 补 **3 例** 草稿链判据；新增 `frontend/src/features/playground/utils.vision-stream-order.test.ts`（**3 例**，门⑥ r6 C-3：**AC2 首次具备机器判据**——乱序完成 ⇒ 回调顺序即渲染顺序、`completedPages` 连续、无重复无跳页、进度分母恒定）；新增 `frontend/src/features/playground/wiring-contract.test.ts`（**6 例**，门⑥ r6 I-1/I-2：**接线契约**，表达式级钉住 `shouldEnterPreviewDuringRecognition ↔ setStage('preview')` 成对、`overlay={overlayModeFor(progressiveRecognition)}`、`lockDraw` 含 `progressiveRecognition`、两处 `setVisionStream` 登记/更新/清空与上下文贯通）
  - **红先对照**：基线树 `fcac61a` + 同一用例文件 = **2 failed / 10 passed**（`expected null to be 'blocking'` / `'passive'`）；修复树 = **17 passed**；草稿链红先（基线 + 同一用例文件）= **1 failed / 19 passed**
  - **零回归（本机 3.12，同刻同解释器实测）**：修复树全量 **166 passed（24 文件）** vs 基线树 **137 passed（21 文件）**（差 **+29** = 新增 3 个用例文件 26 例 + 草稿链 3 例；文件数 +3 对得上），两侧 **0 failed**；`npm run build` 绿（4.68 s）；`npm run lint` 与基线同为 **6 problems（2 errors / 4 warnings，均为既有）**
  - **门④ 浏览器臂实测（2026-10-17 18:17–18:30，证据 `LexMask-workspace/github-issues/_i123/`）**：四臂分离（P 本改造 / B 仅提前切段 / A 仅遮罩降级 / C 基线）+ 单页对照件，10 页扫描件：
    **t1 首屏可见** P **14.60 s**（配对中位 14.28 s）vs B 43.01 s / A 42.55 s / C 42.49 s（配对中位 42.47 s）⇒ 首屏提前 **28.2 s（3.0×）**；
    B 臂的框 **15.75 s** 就进了 DOM 却因全屏遮罩到 43 s 才可见 ⇒ **两层缺一不可**（AC1(d) 四臂归因成立）；
    **AC1(b) 按字面判 FAIL**：14.28 s > 单页对照件 6.39×1.2 = 7.67 s，但该上限在既有 k=3 并发下不可达（第 1 页在并发中本身就要 14–16 s），P 已达可实现下界 —— 如实登记不改判据；
    **AC3 整卷墙钟** P/C 交替各 3 跑配对：中位 **42.55 s vs 42.35 s = 1.0047 ⇒ ≥0.98 PASS**（首轮单跑的 1.34× 表面劣化判为会话首跑冷启动，同轮 C 三次 42.13–42.65 s ±0.5%）；
    **AC5** 修复树 `npm test` **157 passed（22 文件）** vs 基线 **137**（+20 = 新用例 17 + 草稿链 3），`npm run build` 绿，lint 两树同 6 problems（均为既有）；
    **变异 13 条全部 KILLED**（门⑥ r6 判 SURVIVED 的 M6/M10/M15/M16 由新增接线契约用例补齐；M8/M9 由门①.5 r3 评审独立跑出）
  - **门①.5 r3 三条 Critical 的代码侧处置**：①**草稿往返回归修复**（r3 N4，真缺陷）——D1 之后识别期 `stage` 已是 `preview`，中途落盘的草稿会被 `parseDraft` 当成完整 preview 恢复，**半识别卷被呈现为已完成且不重跑**；草稿快照新增 `visionStream`，`parseDraft` 拒绝 `completed < total` 的草稿（走重跑），基线红先实证 **1 failed / 19 passed**（`AssertionError: expected {...} to be null`）；②`use-playground-file.ts` 在进入逐页检测前登记 `{completed: 0, total}`（r3 N3），遮罩进度从 0/N 起算；③`ImageBBoxEditor` 框容器新增 `data-lexmask-bbox` 锚点（r3 N2），供浏览器臂命中测试用（1 行、不改行为）
  - **门⑥ r6 处置 + 门④ k=8 重测（2026-10-17 18:46–18:59，head `7790a2f`，证据 `_i123/ac1-browser-arms-k8.md`）**：r6 指出「原四臂证据全部采集于合并前树（k=3）」，故同 head 配置重测。**AC1(b) 字面仍 FAIL**：对照件（1 页单独成卷）6.20 s ⇒ 阈值 7.44 s，P 三跑 t1 = 23.81 / 24.90 / 14.72 s；**k=1 臂 7.62 s 亦 FAIL（超 2.4%）**；机制硬证据不变（C 臂框到整卷结束 ≈40–52 s 才可见，P 臂 14.7–24.9 s 可见）。**AC3 在 k=8 下判 FAIL**：预注册配对口径 `ratio=C/P` 逐轮 0.7628 / 1.0005 / 0.9076 ⇒ 中位 **0.9076 < 0.98**，护栏 min **0.7628 < 0.90 被突破（记账）**；**负对照补做**：k=1 臂 ratio = 0.7356 < 0.98 ⇒ 判据可失败（正性证据成立）；反向顺序对照因 4 次无效臂（upload+parse 成功却未发起 vision）未完成 ⇒ 顺序偏置未排除；同臂轮间 CV 12–18% **远大于 2% 带宽** ⇒ 该带落在噪声地板下。**新事实**：k=8 使首屏比 k=3 更晚（P 中位 14.6 s → 23.8 s）⇒ Issue#123 原文「方向 2 首屏优先」未被本 PR 覆盖。**AC1(b)/AC3 两条 FAIL 与三条处置选项已升级用户裁决**，未单方改判据
  - **变异自证 13 条：13 KILLED / 0 SURVIVED**（M1 谓词恒 false→KILLED；M2 `pageCount > 1`→`>= 1`→KILLED；M3 `stream.total > 1`→`>= 1`→KILLED；M4 容器忽略 passive→KILLED；M5 去掉 `pointer-events-none`→KILLED；M7 `overlayModeFor` 恒 `blocking`→KILLED；**M6 页面接线硬编码 `blocking`→SURVIVED**）。M6/M10/M15/M16 由 `wiring-contract.test.ts` 的表达式级契约用例判红（变异驱动脚本 `i123_mut2.py` 实测，源码回写一致）。**诚实边界**：这是表达式级覆盖，不是行为级——行为面仍由门④ 浏览器臂承担；接线改名会误报红，属设计意图）
  - **诚实边界**：①接线层（四根线）现有**表达式级契约用例**兜底（M6/M10/M15/M16 已 KILLED）；但它们不是行为级证明，行为面仍归门④ 浏览器臂；②**jsdom 无布局** ⇒ 可见性/命中测试（`document.elementFromPoint`）**全部归浏览器臂**，本机只判模块契约与元素存在性；③AC1(a) 的**承重断言**是「画布已挂载 + 第 1 页框节点在 document 内」而非「新属性存在」（否则红先失败退化为属性缺失）；④AC1(b) 阈值口径＝**同运行内**第 1 页实测耗时 × 1.2（不引用跨运行人口 p50——r2 实测 `8.674×1.2 = 10.41 s` 与实验臂预期 ≈10.4 s 零余量）；⑤**AC3 硬红线＝Issue 原文「整卷墙钟不劣化 ≥0.98」**，方案自加的「≥10% 改善」降为**观测项**；⑥AC4（失败页不阻塞已完成页）按既有 `recognitionIssue` 语义已覆盖，**降为观测项**，并把 D1 带来的 `stage === 'preview'` 六处副作用（首屏空画布闪烁 / 进度分母 / 重跑入口 / 动作按钮时序 / `onBackToEdit` / 会话切换弹窗）在门④ 逐条观测落表；⑦**「不改闸门」的范围＝不改后端识别闸门**（并发闸位、`MAX_HAS_CHUNKS`、env 开关一律不动），D1 改的是前端 `stage` UI 状态（术语已在方案 §4 拆分）
  - **不做**：不改识别算法与任何并发度、不改后端；「首屏优先调度」与「剩余时间预估」（Issue 候选方向 2/3）另立 Issue；批量向导不在范围

- perf(frontend): **单文件页并发 3→8——吃满 HaS 批处理余量（k=32=1842 tok/s 未到顶）**（Issue#172）
  - 依据（2026-10-17 云门④实测）：LA k=8 p50 4.89s/零错误（藏在 OCR 4.2s/页 串行地板之下，#111 的 k=4×2.04 劣化在 k=8 全卷预算内可接受）；HaS vLLM 批处理 k=32=1842 tok/s（单流 14.9×）仍未到顶；整卡四资源占用仅 5-44%
  - `frontend/src/constants/timing.ts`：`PLAYGROUND_VISION_PAGE_CONCURRENCY` 3→8（注释附实测依据与低显存回退说明）
  - 低显存部署回退：置 1 恢复串行（playground/utils.ts 既有说明）；#111 显存刹车（`_effective_vision_page_concurrency`）为后续并联项
  - 验证：LA 直连 k=1/4/8 = 1.40/2.70/4.89s p50 零错误；10 页卷 16 vision 请求全 200；门①.5 REVISE 全处置（AC1 降级画像项/AC2 分段口径/B1 真值 2048/A1A2 拆分/回落档 k=6）
  - 环境：单卡 gfx928 / DTK 25.04.2 / vLLM 0.9.2-DAS（V0、bf16、gmu 0.18、无 eager）

- fix(vision): **病态密页「残值自证」风暴：新增每页上限 `HAS_NER_VERIFY_MAX_JOBS_PER_PAGE`（默认 200）**（Issue#136）
  - `backend/app/core/config.py`：新增 `HAS_NER_VERIFY_MAX_JOBS_PER_PAGE: int = 200`（validator `max(0, min(4096, v))`；`0` = 不限）
  - `backend/app/services/vision/has_text_analysis.py`：残余自证作业在派发前按**输入决定序**排序（值在**本页原文**中首次出现的位置升序；在原文中找不到的拟合值排最后——这类值不自证即不采纳，风险最低）后**确定性截断**；被截断的值不进入结果（与「自证未确认」同语义，保守不增 FP）。新增指标 `has_text_verify_jobs_before` / `has_text_verify_jobs_after` / `has_text_verify_jobs_by_type` / `has_text_verify_skipped` / `has_text_verify_ms`；触顶时 WARNING 带截断计数与**页内类型分布**
  - 新增 `backend/tests/test_issue136_verify_cap.py`（**16 例**）：上限生效/不触发/边界（N=0、1、=jobs、>jobs）、批量路径先截断再分批且批内确认集不误扩、有界派发保序、单值异常隔离、**截断序为输入序而非模型输出序**、**幻觉值不得挤掉原文真实值**、指标恒等、默认值与 validator 上下界
  - **云门④（DCU 单卡 + vLLM 8080 + 4 个样本页，27 格）**：① 病态页基线 519 次自证调用 / 自证模型墙钟 37.6–42.5 s；N=200 → **200 次调用（−61.5%）**〔口径＝**残余自证 NER 调用数**；残余分组主查调用不在上限内；端到端墙钟不设判据——见 `_i136/cap-ab-2026-10-17.md` §9〕、319 跳过、13.1 s，N=100 → 100 次（−80.7%）；恒等式 `jobs_before = jobs_after + skipped` 与 `calls == jobs_after` 全格成立。② GT 页基线 5 跑值集完全一致（66 值，R_GT 0.643）⇒ 可作判别性对照；所有触顶档 **R_GT ≥ 基线下界**（N=1/N=4 档 0.690）⇒ 召回红线 PASS。③ 真实案卷页与 ④ 良性页**未进入残余自证阶段**（原始 `duration_ms` 无任何 verify 键 ⇒ 代码不发指标；台账中 `jobs_before=0` 系分析器 `or 0` 推导值，非实测）
  - **默认值定档（AC3）**：候选须在 ① 真触顶且降幅 ≥50%、且在 ② 不造成召回损失；**取满足条件的最大 N**（上限只砍尾部，越松越接近基线行为 ⇒ 未知召回风险越小）⇒ **N*=200**，与预注册默认一致
  - **诚实边界**：只有 ② 有 GT，① 的丢值无法判召回、③ 无 GT 且上限未介入 ⇒「真实案卷召回不降」本次只有合成 GT 页代证；未覆盖中等残值量页（`jobs_before` 介于 15 与 500 之间）；② 上出现**非单调集合变动（7 进 7 出）**，机制未查明 ⇒ 只以 GT 召回为判据、不以值集包含关系为判据；批量档（`HAS_NER_VERIFY_BATCH_SIZE>0`）仅单测覆盖、默认维持 0；本上限定位是**病态页上界护栏**而非吞吐优化（① 端到端 256.3→217.7 s，收益被 OCR 抖动掩盖）
  - **有意未处置**：主查截断重查（该页模型耗时的 48%）属另一杠杆，见 Issue#128 的负结论；`HAS_NER_VERIFY_BATCH_SIZE` 默认化与有界池/跨类型合并各作 follow-up

- fix(queue): **Python 3.10 上页级/管线级超时被记成 `failed`、并被 I/O 分支抢吞**（Issue#139）
  - `backend/app/services/task_queue_pipelines.py`：三处 `except TimeoutError` 改为 `except (TimeoutError, asyncio.TimeoutError)  # noqa: UP041`（`:120` `_run_recognition` 终段、`:356` `_run_vision` 的 `run_page`、`:530` `_run_vision` 外层）。3.10 实测 `asyncio.TimeoutError is TimeoutError` 为 `False`、`issubclass(asyncio.TimeoutError, TimeoutError)` 为 `False`（3.11+ 才合并为内建类），只捕内建类时 `asyncio.wait_for` 抛出的超时会落到通用分支记成 `recognition failed (unexpected)`；`# noqa: UP041` 必须保留（`backend/pyproject.toml` `target-version = "py311"` 会把它误自动改写掉）
  - **同文件必须一并重排**：3.11+ 内建 `TimeoutError` 是 `OSError` 子类，而 `_run_recognition` 的 `except (FileNotFoundError, OSError)` 原本排在 `except TimeoutError` 之前 ⇒ 页级超时转换出的内建 `TimeoutError` 会被 I/O 分支抢先吞掉、记成「recognition I/O error」。改为超时分支在前（M4 变异实证：三处类型都改对但不重排时 T4 判红）
  - 新增 `backend/tests/test_issue139_pipeline_asyncio_timeout.py`（**14 例**）：以「非 `TimeoutError` 子类」的替身类（与 #137 D1 同一注入手法 + `**assert not issubclass(替身, TimeoutError)**` 空转护栏）触发超时，断言三处超时语义；另有**反向护栏**（真 `ValueError` 仍走 `failed`、真 `OSError`/`PermissionError` 仍走 I/O 分支）防止用「把捕获放宽成 `except Exception`」蒙过；T6 钉 `CancelledError` 不被吞、T7 钉多页批量页账不串页、T8 钉页数归一化、**T12 钉首错取消语义**（①被取消的在飞兄弟页不得被记成 `failed`；②兄弟页**确实收到取消**——观测点在协程内部的 `except asyncio.CancelledError`，取消投递用 `gather(..., return_exceptions=True)` **确定性**等待）；T11 是 3.10 环境前置门（3.11+ 上 `skipif` 跳过，**不计修复证据**）
  - **改前红 / 改后绿（原始输出，门⑥ 二轮后重拍）**：基线 `20520e4` 树 + 同一测试文件 = **6 failed（T1/T2/T3/T4/T7/T12）/ 8 passed / 1 skipped**；修复树 = **14 passed / 1 skipped**（云 3.10.12 上基线 6 failed / 9 passed、修复树 **15 passed / 0 skipped**，T11 真执行并通过）
  - **变异自证 M1–M5d + M6' 共 9 条全 KILLED**（各自指名用例栽掉）：收窄 `:120`→T1；收窄 `:356`→T2+T7+T12；收窄 `:530`→T3；三处类型都改对但不重排→T4；放宽 `:120` 为 `Exception`→T5+T9；放宽 `:356`→T10a；放宽 `:530`→T10a+T10b；放宽 `:120` 为 `BaseException`→T5+T6+T9；**删掉 `page_task.cancel()`（M6'）→T12**
  - **门⑥ 二轮处置（Important 1）**：T12 的初版加固写成「睡 0.05 s 后查 `task.cancelled()`」，被门⑥ 在**负载下实测假红**（全量 `13F/2191P/52S`，多出的正是 T12，`AssertionError: [False, False]`；低载与单跑 30/30 通过）——这是时序竞态，且 3.10 的 `wait_for` 会再包一层 task 使断言主语随版本漂移。已改为协程内记录取消 + `gather(..., return_exceptions=True)` 确定性归位：T12 单跑 **30/30 稳定通过**、全量两轮 `FAILED` 集合与基线逐条一致（无新增失败）、**M6' 仍判红 T12**、**云 3.10.12 上 T12 真执行并通过**
  - **有意未处置**：`backend/app/services/task_queue.py:566` 的 worker 顶层 `except (TimeoutError, OSError, RuntimeError, ValueError, KeyError, json.JSONDecodeError)` 仍未列 `asyncio.TimeoutError`。**该元组不是死代码**：本 PR 修复面覆盖的路径（三条管线的 `try` 区）确实到不了它（终端梯 `task_queue_pipelines.py:146/657/789` 已吞掉 `Exception` 子类，只有 `PdfEncryptedError` 经 `:141`/`:786` 按设计上抛），但 `:566` 的元组里含 `OSError` 与 `json.JSONDecodeError`，而前置序言（`store.get_job`/`get_item`、`json.loads(job config)`）与 `finally`（`_record_item_performance`、`store.touch_job_updated` + `_refresh_job_status`）都在该覆盖范围之外，对这两类**可达**且会被记成 `worker: OSError/JSONDecodeError`。同形位置另有 `_run_structured:647`、`_run_redaction:772`；触发/接续条件＝管线去掉终端 `except Exception` 时另立 follow-up Issue 一并收口
  - **归因纪律**：本 PR 只负责 `task_queue_pipelines.py` 的同类根因，`recognition failed (unexpected)` 这类观测**不得**归因 #137（#137 修的是 `task_queue.py` 的另一处）
- perf(dcu): **HaS vLLM 去 `--enforce-eager` 开 CUDA Graph——单流 decode 31.5→126.9 tok/s（4.0×）**（Issue#166）
  - 实测：生产 8080 现役启动参数带 `--enforce-eager`（#118 §7.2 已裁决 gfx928 cudagraph 67/67 OK 但生产从未开过）；测试口 18090 与生产**唯一差异=去该 flag**，同轮对测（399 in / 256 out、temp 0、k=1×3，`tps_probe`）：31.24/31.47/31.77 → 88.21/127.22/126.57 tok/s，中位 **31.5→126.9（4.0×）**，超过 llama.cpp Q8_0 的 118.8（#162，该路线已因 GPU 输出错误关闭）；CUDA Graph 不改数学定义（同 kernel 批量重放）；实测双锚零回归（hardcase Δ0.0007、exam-v3 Δ0.0001，实体级差异 2 处、噪声级）；零代码改动（环境：单卡 gfx928 / DTK 25.04.2 / vLLM 0.9.2+das.opt1.dtk25042）
  - `docker-compose.yml`：`ner` 服务（`vllm/vllm-openai`，HaS 8080 同款参数组合）去掉 `--enforce-eager`
  - `docs/dcu/海光DCU适配总览.md` 坑清单 15→16 条：补「gfx928 生产已开 cudagraph + A/B 4×」；第 10 条 gfx906「cudagraph 捕获必崩」**不适用于 gfx928**，勿混读
  - 实例持久卷 `start_all_dcu.sh:34`（生产运行时真身，主仓无此文件）同步去 flag 并重启 vllm-ner（部署侧动作）；上线验收 AC：六服务 200、同探针 decode ≥95 tok/s、hardcase F1=0.6696 锚、exam-v3 F1=0.8294 锚

- chore(security): **公开树预存凭据痕迹清零 + 云侧审计/E2E 脚本口令外置**（Issue#140、Issue#141）
  - `cloud-deploy/audit_scan_output.py`、`cloud-deploy/e2e_real_cases.py`：认证口令不再以字面量写死在登录循环里，改为 `os.environ.get("LEXMASK_EVAL_PASSWORD")`（账号名 `LEXMASK_EVAL_USER`，默认 `apitester`）；未设置时 `sys.exit` 并指向 README 的「凭据注入」小节；两个脚本文档串与 `cloud-deploy/README_CLOUD.md` 补运行前置（#141 AC1/AC4）
  - `backend/scripts/eval/benchmark_throughput.py` 用法示例里的口令改为 shell 环境变量形态（#140 AC1）
  - `backend/tests/test_pdf_encryption.py`、`backend/tests/test_ldap_auth.py`（bob/carol/dave/erin 共四处）、`backend/tests/test_role_matrix.py`、`backend/tests/test_dicom_core_realdata.py`（`mapping_secret` 两处）的夹具口令改为显式 `placeholder-*` 合成值
  - `backend/app/services/has_client.py` 的 `context_tokens` 文档行改书写形态：该行原本是 `键名: 值` 形态，被扫描器按 `credential_bare` 误报（键名尾巴 `token` 在 `CREDENTIAL_WORDS` 里，而值是文档引用 `Issue#104`）。**没有放宽任何规则**（#140 AC4）：把 `token` 整体挪进 `META_TAIL_WORDS` 会让真 `api_token: <真值>` 整类漏网，故只改被扫文件的写法
  - 验证：整树 `python eval-ng/scripts/check_public_tree.py --base "" --allow-prefix <DCU 栈 dtk 前缀> --allow-prefix <DCU 栈 hyhal 前缀>` 的 `credential_*` 命中 **14 → 0**；`--self-test` 仍全中；本机门③ 与同刻基线的 FAILED 集合逐条一致
  - ⚠️ **代码清理不等于暴露撤销**：该测试账号口令自 `55002ec`（v1.0.0 首发）起就留在公开提交历史里（`git log -S` 可查），本 PR 只清工作区与后续提交。**须轮换/停用该测试账号口令**（#141 AC2，由部署方执行，本 PR 无法代劳）

- fix(docline): **文本层文档级链的单块 NER 失败不再被静默当成「成功且零实体」**（Issue#148）
  - 根因：`backend/app/services/text_layer_docline_service.py` 的 `ask()` 只捕异常，而 `has_client.ner()` 把 HTTP 400 / 超时 / 整段输出不可解析 / 熔断开路四类失败**吞成 `{}`** 返回（与「模型合法地回答本块没有实体」在返回值上同形）⇒ 失败块被计入 coverage、`stats["http_errors"]`/`failed_chunks` 恒 0、整卷既不回落也不判不合格
  - 修复：`ask()` 改取 `ner_checked()` 的 `ok` 位（取口方式与 `has_service._call_client_ner_checked` 同源），`ok=False` 走与异常**完全相同**的失败路径（`http_errors`/`failed_chunks` 各 +1、该块不计入 coverage）⇒ 由 `text_layer_doc_source.py:192` 既有的 `failed_chunks > 0` 门把整卷判为「回落旧路径」；没有 `ner_checked` 的对象（旧 client/替身）退回 `ner()` 并**保守报 `ok=False`**（宁可判失败，也不把未确认结论当完整结论）
  - 口径边界（门①.5 段2 D2）：`stats["http_errors"]` 是**块级 `ask()` 失败次数**，而 `NER_ERRORS`（`has_client` 侧）记的是**上游异常分类**且经 `retry_sync` 重试后才计 1 次 ⇒ 同一块失败在两侧数字**天然不等，不可做差分对账**
  - 既有 `TEXT_LAYER_MAX_BLOCK_ERRORS`（`text_layer_doc_source.py:205`）在现行判定顺序下**结构性不可达**（`:192` 的 `failed_chunks > 0` 先抛 `_TransientIneligible`）；`TEXT_LAYER_MIN_COVERAGE`（`:230`）另有一个不可达原因——**不是** `:192`，而是更早的**质量分类器早退**（`:181-185` `verdict.usable` 为假先返回 `None`）（门⑥ 第二段 Minor①订正：全空白文本层卷的 `coverage_ratio` 本为 `0.0`，该门本会触发）；本次维持该语义（拍板 D1=方案 A：任一失败块即回落），只把「失败识别」修对、不改失败容忍度
  - 新增 `backend/tests/conftest.py`（此前 `backend/tests/` 下没有 conftest）：autouse fixture 每例前后复位模块级单例 `ner_breaker` 与 `HaSClient` 的三个类级缓存——否则注入超时会经 `retry_sync` 重试 3 次把 breaker 打到 open，污染同进程后续用例（门①.5 段2 Critical C-1）
  - 新增 `backend/tests/test_issue148_docline_ner_failure.py`（**14 例**）：`has_client` 侧三类故障各一条（HTTP 400 不重试 1 次 / `ReadTimeout` 重试 3 次 / 单批不可解析）+ `json_repair` 修复后仍缺全部请求类型 + 正向对照 + 同 payload 并发无可缓存结论 + 多批聚合翻转；文档级链侧混合块失败（≥2 块、失败词恰落一块、coverage 精确保留成功块并集）、并发部分失败保值、入口层负缓存 marker、回落被 memo 化且 TTL 内不重打上游；既有桩（`test_issue115_text_layer_docline.py` 的 5 处 fake）补 `ner_checked` 委派以保持旧语义
  - 变异自证：M1 退回旧语义（不取 `ok` 位）/ M2 取到 `ok` 但不处理 / M4 强制 `ok=True`——三者均被 **E1/E3/E4/E5/G2b 五条用例**同时 kill；M3（不 bump `stats["failed_chunks"]`）为**等价变异**：`DocLineResult.failed_chunks` 由 `len(failed_ranges)` 推导（`text_layer_docline_service.py:686`），该计数无读者，不是测试缺口
  - 门③：本机 head 与同刻基线 `b8a3398` 的 FAILED 集合逐条一致（`comm -3` 双向为空）
  - 诚实边界：本机无 GPU / 无 HaS 权重 ⇒ 门④（云 DCU 端到端 canary）待云窗口；`pipeline_status["text_layer_doc"]["http_errors"]` 观测面只在「无失败块」的卷上可见（失败卷在写 `doc_memo` 之前就回落旧链）

## 2026-10-16

- fix(eval-ng): **公开树红线扫描器非 ASCII 文件名不再静默漏扫 + 加两道自检**（Issue#154）
  - 根因：`eval-ng/scripts/check_public_tree.py` 取改动清单用 `git diff --name-only`（未加 `-z`）⇒ 中文名被 git 转义成 `"docs/\345..."`，`Path(escaped).is_file()` 判假 ⇒ 整份文件**静默跳过**（门显示「零命中」，实际那几个文件没被看；实证 PR#146 改 4 文件只报 3）
  - 新增 `changed_files(repo_root, base) -> list[str]`：三条 git 命令一律 `-z`（`diff -z --name-only --diff-filter=AMRT <base>...HEAD`、`diff -z ... HEAD`、`ls-files -z --others --exclude-standard`），路径按 NUL 切分、不经转义；`default_paths` 改为复用它
  - 新增**自检①（内部不变量）**：`default_paths`（目录+文件候选）与 `changed_files` 的条目数不一致 ⇒ 打印差值 + `exit 2`（**能抓**=`default_paths`（Python 侧后处理）未来偏离 `changed_files`，正对照实测 rc=2；**抓不到**=在 `changed_files` **内部**给某条 git 命令加过滤/去重/截断——`default_paths` 就是 `changed_files` 的直译、两侧同变小、恒等 ⇒ 静默绿；**`--diff-filter` 回退（`AM`↔`AMRT`）这类 I-2 回归形状只能靠用例钉，不是靠这条自检**。门⑥ 冷审 I-3 要求说清这个边界，第三轮增量复核 I1 实测证明原文把这份功劳记在了自检① 头上、方向恰好相反，此处已按实测更正）；**自检②**：候选路径「不是普通文件」（转义/解析回归的典型症状）⇒ 逐条列名 + `exit 2`；**自检③（Issue#154 AC2 的字面项）**：候选数 ≠「已扫描 + 已解释跳过」⇒ 打印三项计数 + `exit 2`（判定抽成纯函数 `unexplained_candidates`，便于单测）
  - **取改动清单失败必须报红（冷审 I-1）**：`_git_lines` 不再把 `CalledProcessError`/`OSError` 吞成 `[]`，改抛 `GitError` ⇒ `main` 打印「取改动清单失败：…」+ `exit 2`。旧行为在「`--base` 写错 + 仓里有未跟踪文件」时会**绿着放行**（冷审实测：bogus base → `1 文件，零命中` rc 0；正确 base → 命中 2 处 rc 1）；空区间的「没有候选文件」与取清单失败文案已分开（冷审 M-7）
  - **重命名/类型变更也进扫描集（冷审 I-2）**：`--diff-filter=AM` → `AMRT`（`-z --name-only` 对重命名只输出目标路径，NUL 口径下无需额外处理）；旧口径下 `git mv` 再改文件，只要同批还有别的改动就静默漏扫（冷审实测 R092 → rc 0）
  - 解析层只丢**空项**（不再用 `strip()` 当谓词：文件名全为空白的文件曾被静默丢掉，冷审 M-1）；子进程显式 `encoding="utf-8"` + `errors="surrogateescape"`，C locale / 非 UTF-8 文件系统编码下**说明原因**而非含糊报「路径解析可疑」（冷审 M-5/C2；`test_c_locale_run_*` 真子进程验证不崩）
  - 有意跳过（后缀不在扫描范围 / 超 2MiB / 非 UTF-8）改为**逐类打印原因**，仍不判红（整树巡检 `--base ""` 必须可用：实测本 PR 树 **992 文件**（基线 `8722503` 树 991，差 1 = 本 PR 新增的测试文件）、跳过明细 = 35 后缀 + 3 超上限 + 1 非 UTF-8；**边界**：整树巡检要求工作区干净，若工作区有已跟踪文件被删除，会逐条报「路径解析可疑」假红 rc=2——冷审第三轮 M5）
  - 新增 `backend/tests/test_public_tree_scanner.py`（**24 例**，tmp_path 现搭 git 仓 + 两个真子进程用例）：中文名/含空格/**含双引号**/重音名必被扫到、**已提交区间 `<base>...HEAD` 与 `git mv` 重命名**必须覆盖非 ASCII、红线必被报出、**bogus base 必须报红且不得出现「零命中」**、空区间与取清单失败文案可区分、自检②幻影路径与自检③余数必 `exit 2`、`--paths` 模式双向、有意跳过必留痕、C locale 下打印中文不崩、`--self-test` 与整树模式不回退；**变异自证 M1–M8 全 KILLED**（退回无 `-z` 旧口径 / **仅 `<base>...HEAD` 退回无 `-z`** / 过滤退回 `AM` / 解析退回 `strip()` / 吞 `CalledProcessError` / 去掉显式 `encoding` / 自检③接线改恒静默 / **`core.quotepath=false` 替代方案**，各自都有指名用例栽掉）
  - **第三轮增量冷审（With fixes）的处置**：补 5 例 —— **已跟踪文件改了但没提交**也要进扫描集（I2：`diff -z … HEAD` 那条命令就是「门禁在提交前就能跑」，此前把它整条删掉仍 19/19 全绿）、`.gitignore` 掉的路径一律不进扫描集（M1）、`AMRT` **不含 `D`** 故删除的文件不进扫描集（M2：含 `D` 会让任何删文件的 PR 假红）、同一轮里既有命中时自检② 的「路径解析」文案不得被绕过（M3）、非 UTF-8 文件系统编码的警告只在候选集合含非 ASCII 名时打（M4：全 ASCII 的树不再打吓人警告）；用例数 **19 → 24**，四条自造变异（删 `diff … HEAD` / 去 `--exclude-standard` / 改 `AMRTD` / 自检②加 `and not findings`）逐一 KILLED
  - 门③：本机全量 **12 failed / 2168 passed / 51 skipped**，与同刻基线（PR base `8722503`）**12 failed / 2144 passed / 51 skipped** 及上一版 head（`bbbfa41`，12F/2152P/51S）的 `FAILED` 集合逐条 `comm` **完全一致**（零新增失败；12 条均为本机沙箱建不出进程池 / `safe_regex` 子进程的既有环境失败）；`+24 passed` = 新增用例（第三轮又 +5）；`ruff check app/`（CI 唯一的 lint 门）干净——改动文件在本仓 ruff 下除 base 既有的 `EXE001`（脚本有 shebang 但无 exec 位；`eval-ng/*` 与 `backend/tests/*` 均不在 CI lint 门范围，冷审 M-9）外无新增告警；扫描器对本次改动自扫 `3 文件，零命中`
- fix(docs): 归档仓编号引用约定 + 修复 `eval/scripts/format_report.py` 旧仓 #46 错链（Issue#59）
  - `render_md` 不再拼 `github.com/youayou-Lee/LexMask/issues/46`（本仓 #46 语义完全不同），改标「归档仓 #46（DataInfra-RedactionEverything）」
  - `docs/协同开发规范-gh使用与仓库纪律.md` 新增 §5：外链必须指向本仓真实 Issue、指旧仓写「归档仓 #NN」、历史叙事不批量清理
  - `backend/tests/test_eval46_format_matrix.py::test_render_md_snapshot` 钉住「不再拼本仓 #46 错链」
- fix(gt): GT 写盘 warning 不再当致命错误——真实卷一处同类型实体部分重叠不再致整案写不出（Issue#73）
  - `eval/gt/gt_schema.py` 新增 `split_problems(problems) -> (errors, warnings)`（按 `warning:` 前缀分级；`validate_pagepack` 返回契约不变）
  - `write_gt_jsonl` 改为：仅真 error raise（文案只列真 error）且不落盘；warning 放行写出并**返回** warning 列表 + `logger.warning` 留痕（不再静默）
  - `backend/tests/test_gt_schema.py` 新增 4 例；变异自证：warning 当 error ⇒ 新用例 FAILED
- fix(ner): **正则兜底的「match → 待遮盖文本」语义与超时策略收敛到单一事实源**（#64、#65）。
  - **新增 `backend/app/core/safe_regex.py` 同源件**：`match_items_indexed(match)`（有命名组 ⇒ 按**声明顺序**逐组输出 `(sub_index, text, start, end)`、空组照样占位；声明了命名组但组全空 ⇒ 不产出、**不回退整 match**；无命名组 ⇒ 整 match 一项；`match_items` 是其去下标视图）+ `scaled_timeout(text)`（**2 s/MB**、上限 120 s；实测参考 0.14–0.19 s/MB ≈10× 余量，**出处＝Issue#65 正文记录的 PR#63 评审期实测**）+ `safe_finditer_many(compiled, texts, timeout)`（一次子进程扫本页**全部块文本**，预算按合计长度放缩）。
  - **三处 runner 全部同源**（冷审 C1）：`ocr_has_vision_service._apply_regex_fallback`（视觉兜底，原裸 `re.compile`/`finditer`/`group(0).strip()`）、`hybrid_ner_service._custom_regex_extract`（文本链）、`text_layer_docline_service`（**文档级 docline 保证层**，原裸 `re.finditer(pattern, stream)` 全卷流无预算、且该链默认开启 `TEXT_LAYER_DOCLINE_ENABLED=True`）现在统一走 `safe_compile` / `safe_finditer`(`_many`) / `match_items`；用户可控 pattern 在三条链上都有执行预算。**同源带来的两条行为面变化（冷审 M-1，已知且有意）**：① 三条链统一继承 `safe_compile` 的 `re.IGNORECASE`（旧 vision 的 `re.compile(regex_pattern)` 与旧 docline 的 `re.finditer(pattern, stream)` 均无 flag）⇒ 小写形态首次可命中（如 `悬挂粤rh0472号牌`）；② 命中值不再 `strip()` 边缘空白（docline 侧 `find_value_spans` 先 `_fold(value)` 归一去空白，**无漏检**）。两条方向均为**只增遮盖**。
  - **#64 视觉兜底与文本链同粒度**：`ocr_has_vision_service._apply_regex_fallback` 由裸 `re.compile` + `finditer` + `(m.group(0) or "").strip()` 改为 `safe_compile` / `safe_finditer_many` / `match_items` ⇒ 出生语境只出日期（`出生日期：1992年10月5日` 不再整段进）、区间拆首尾两个实体、`\s*` 空白与分隔符不入伪实体——与 Issue#52 已定的文本链语义一致，消除欠遮盖方向的粒度分歧。
  - **#65 超时按长度放缩且可观测**：文本链 `_custom_regex_extract` 由写死 `timeout=2.0` 改为 `scaled_timeout(text)`；**编译探针（固定 1.0s）与检索（长度预算）分开捕获**，文案区分 `PROBE TIMEOUT` / `SEARCH TIMEOUT`（冷审 I2），超时 ⇒ `HybridNERService._regex_timeout_events` 计数 + `logger.warning`（带类型 / `text_len` / 预算 / 事件数），**不再与 `re.error` 共用一条静默 warning 而整篇保证层无声丢弃**；vision 兜底与 docline 链补同等计数与区分文案；`re.error` 保持旧文案、不计入超时事件。
  - **实体编号逐字不变**（`regex_{type}_{index}_{sub_index}` / 无命名组 `regex_{type}_{index}`）⇒ Issue#52 既有契约与前端映射不受影响。`sub_index` 以**声明顺序**为基准、**空声明组占位**（与旧实现同基准；冷审 I1 抓出首版先滤空再编号会漂移，已回改并补 `(?P<opt>X*)(?P<date>\d{4})` on `1992年` ⇒ `regex_custom_x_0_1` 用例钉死）。
  - **测试**：新增 `backend/tests/test_safe_regex_match_items.py`（三态 + 编号声明顺序 + 预算放缩/上限 + 空页短接）、`backend/tests/test_issue64_65_regex_fallback.py`（文本链与 vision 兜底的语义/编号/超时区分与可观测 + 一条 `@requires_process_pool` 真实探针超时用例）、`backend/tests/test_issue115_text_layer_docline.py` 补两条 docline 链用例（命名组语义 + 预算放缩/超时不可致命）；把 Issue#109 的进程池守卫抽成共享 `backend/tests/process_pool_guard.py`，`test_issue43_case_number_floor.py` 两条 vision 真实池用例改挂 `@requires_process_pool`。本机全量 **12 failed / 2140 passed / 51 skipped**（同一 worktree 的改动前基线 `bbe9b8c` 为 **12 failed / 2113 passed / 48 skipped**），失败集合逐条 `comm` **完全相同**（零新增失败，12 条均为本机沙箱建不出进程池的既有环境失败）；`+27 passed` = 新增用例，`+3 skipped` = 上述两条 #43 vision 用例 + 新增的真实池超时用例（本机沙箱建不出进程池故跳过，CI/Linux 上真执行 —— CI 侧 `Δskipped = 0`）。
- test(frontend): **补齐 `ClampTooltipText` 组件级回归测试（#48）**——PR#42 门⑥ 遗留的 Important 项，该组件的行为变更当时只经人工浏览器验证（PR#42 当时 85 条前端测试无一条覆盖该不变量；**当前基线为 133 例**）。
  - **引入组件测试基建**：devDeps 新增 `jsdom` + `@testing-library/react` + `@testing-library/dom`；`vite.config.ts` 仍默认 `environment: 'node'`（纯逻辑测试不受影响），组件测试文件用 `// @vitest-environment jsdom` **逐文件**切换。
  - **新增 `src/features/settings/components/entity-type-list.clamp-tooltip.test.tsx`（4 例）**：直接渲染导出的 `ClampTooltipText`（本次改为 `export`，仅加导出、渲染逻辑零改动），断言 ①三种 `kind`（title/description/regex）截断翻转前后**根节点是同一 DOM 节点**（`isSameNode`）且标签类型不变（span/p/code）；②未截断时无 `tabIndex`、截断时 `tabIndex=0`，翻转回未截断时移除；③几何量用可变 `scrollHeight/clientHeight` 桩驱动，不依赖真实排版；④**Tooltip 包裹与气泡都真的还在**（`data-state="closed"` + 聚焦后气泡出现，假定时器推进 `delayDuration`），且**翻转期间键盘焦点仍在同一节点**（`document.activeElement`）——即 Issue#48 报告的原症状本身。后两条由门⑥ 冷审 I-1 指出并补齐：原断言无法区分「Tooltip 还在」与「Tooltip 已被删」（删整块 `<TooltipContent>` 或删整个包裹时用例仍全绿），补齐后两类变异均 KILLED。**第二轮增量复审（`0b8eaec..c060850`）另指出 4 条 Minor 并已处置**：气泡内容断言由 `toContain('一')` 收紧为**精确等于当前全文**（否则把气泡内容换成任意一个字仍全绿，该变异现已 KILLED）、删除经变异证实为死代码的 `vi.hoisted(IS_REACT_ACT_ENVIRONMENT)` 块（`@testing-library/react` 自身会在 act() 外围置位）、去掉与 `toBe` 严格蕴含的重复 `isSameNode` 断言、在 `data-state` 旁标注「绑定 Radix 内部属性，升级时须复核」。
  - **CI 纳入前端测试**：`.github/workflows/ci.yml` 的 `frontend` job 在 Build 前新增 `npm test` 步骤（原先只 `npm ci` + build，测试从未在 CI 跑过）。
  - 本地三道门：`npm test` **21 文件 / 137 测试全绿**、`npm run build` 成功、`npm run lint` 输出与改动前逐行相同（6 条既有问题：2 error + 4 warning，均在未触碰的文件里，属基线）。

- docs(gate): 门禁规范新增「效果类 PR 测试用例独立评审」维度（Issue#54，承旧仓 #74；PR #145）
  - §0 总览 ⑥ 行与核心原则补「写测试的人不得兼写漏测清单」，并注明**自 ①.5 独立冷审通过、且用户点头之日起生效**
  - 新增 §5.1：适用范围（改动面文件锚点 + 效果类测试文件同样适用 + 「纯逻辑重构」豁免须举证）、**两段式派发**（第一段只给 Issue 正文拿独立边界清单并留痕、留痕时间须早于交 diff；第二段同一 reviewer 对照查漏）、**负样本判据与漏测分级**（答得出「会复发/掩盖回归」者 Important 起、按 §5:118 走重走链；其余记 Minor 不作打回依据；清单不要求穷尽）、与 §1.5 分工表、留痕要求（独立清单原文须落 PR 描述或评论）
  - 同步三处引用：门禁 §5 评审清单、`docs/WORKFLOW.md` Step 5 审核阶段、`AGENTS.md`「八道门」（订正原「七道门」）
  - 预演留痕：#64/#65 正则兜底 PR（**#152**）的 ⑥ 冷审已按草案执行过，但未按两段式留独立清单原文 ⇒ 记为**预演**，不顺延为「已完整执行」
  - 第三轮 ①.5 冷审收口：回补**独立清单下限**（「至少覆盖缺陷描述/验收标准所指每一类边界」，第二轮拆句时误删 ⇒ 属遗漏，非有意放宽）；清单评论**发布后不得编辑**；删「不依赖裁量」的过度承诺（改「仅两维仅四格、不得引入第三档」）；`eval/scripts/**` 中定义评测口径者显式入范围、排除举例改非具名；变更记录补至三轮
  - 第二轮 ①.5 冷审收口：判定改**封闭 2×2 表**（覆盖×会复发=漏测 Important；**未覆盖×会复发同样 Important 起**，不因 Issue 未写明而降级）；恒真断言**与漏测同级 Important 起**（另有具名用例承担判别性方可豁免）；两段式补「第一段不得自取 PR/diff」+ 留痕限定 **PR 评论原文** + **派发方**在 PR 描述记录两段时间戳；适用范围补全锚点（`has_client.py`/`has_service.py`/`agent_md_ner.py`/`vision_service.py`/`vision_config.py`/`mineru_parse_client.py`）并收窄 `eval/**` 为 `eval/gt/**`+`eval/benchmarks/**`+`eval-ng/**`（报告/脚手架除外）；生效条件五处措辞统一
  - 变更记录：本条经**三轮** ①.5 独立冷审（2026-10-16：首轮 REVISE → 逐条修订；二轮 REVISE → 判据收口；三轮 REVISE → 回补清单下限）后**待用户点头生效**；并对原 Issue「漏项即打回」的**刻意收窄**（=满足负样本判据的漏测才 Important 起）在规范文末留痕
- feat(eval): 新增 **T3 框级视觉检测对比框架**（`eval/benchmarks/t3/`：预注册判读 spec / 四臂适配器 / 跑批 / 报告 / GT 复核 + **85 单测**）与成绩单 `eval/reports/2026-10-11-t3-visual-bench-issue92.md`（#92，PR #95）——回答 v2.0「印章 / 手写签名四条视觉臂能否替代现役 LocateAnything（LA）」，**选型工具不做闸门、现役管线零改动**。
  - **预注册判读跑批前锁死在 `spec.py`**（候选臂召回 ≥ LA 同 kind；难例桶不塌方=较 LA 同桶下降 >15pp 即判塌方；误报上界=FP/页 ≤2×LA；单页成本不高于 LA；失败页 >10% 判臂无效），事后不改阈值；样本 64 页真实案卷扫描页、GT 174 框（seal 69 / signature 105），GT 方法=四臂并集预标 → 轮 A 四人确认 → 轮 B 两人独立盲标 → 差集裁决（83 候选收 18）→ 轮 C 独立抽查 64 框 + 27 疑点，通过率 **95.31% ≥95% 门**。
  - **印章职责具备切换条件**：MinerU seal 召回 **58.0%** ≥ LA **43.5%**（clean 71.4 / hard 64.7，难例桶无塌方），FP 0.05/页 ≤2×LA 0.08，单页 **3.40s**（< LA 4.48s，且可 CPU，GPU 可让渡语义层）；后续接遮盖管线另立工程专项（#96）。
  - **签名职责 LA 留任**：sigdet 召回 **25.7%** < LA **29.5%**，且 FP **1.70/页** 超 2×LA 上界（LA 0.28 的两倍 ⇒ **6.1 倍**）、单页 100.44s 远高于 LA，触发重测条件与稳定性缺口（T0 探测 184 页 0 命中）留档 #98；PaddleX `seal_recognition` 召回 21.7%（<LA）、两难例桶塌方（-28.6pp / -25.5pp）、FP 0.27/页超上界 0.16、单页 12.33s>LA ⇒ 四条判据皆未过，不采纳。
  - **门⑥ 冷审抓出并修复两处聚合缺陷（2026-10-16 订正，见成绩单「订正记录」）**：无 GT 页的 FP 被按臂重复计数、无桶页的 `(all)` 分桶被计两次；另修 `(all)` 行 FP/页 不可见、缺 `la` 基线时判据被静默跳过、预注册的「召回 ≥ LA」「成本不高于 LA」两条未实现。用私有原始产物复算：**召回列逐格不变**（三条结论不变），签名 FP/页 LA 0.52→**0.28**、sigdet 2.69→**1.70**（倍数 5.2×→6.1×，仍超上界）；补回归测试至 **85 条**并逐个变异 KILLED。第二轮独立复审确认上述修复可用「忠实变异回旧实现」逐条杀死、订正数字双向可复现，另指出并已修：`README.md` 四臂表仍写「raw_answer 优先」（与 I5 改动矛盾）、报告侧单页耗时把失败页墙钟计入「ok 页平均」（本次 0 失败页、零影响）。LA 一臂原始框由响应 `raw_answer`（服务端旧版回退路径）解析，与现役消费的结构化 `boxes` 契约有别，适配器已改为优先消费 `boxes`，同源数字需上云重跑 LA。
  - **口径局限已写进成绩单公开侧**：GT 预标底为四臂并集 ⇒ 各臂召回存在向上偏置且**偏置有方向性**（贡献独有框的臂天然占优，MinerU seal 是印章 GT 候选贡献臂之一），**绝对值勿外推、只作臂间相对比较依据**；预注册阈值虽为 `spec.py` 字面常量，但该文件与跑批同日多次提交成型，git 无法独立证明先于跑批冻结；单页耗时为「有时耗页」均值（零出框页旧跑批未记耗时，已修）。坐标系（LA 0-1000 / MinerU seal 块 0-1000 / PaddleX 页像素 / sigdet 0-1）经云端对拍实证后统一换算进页像素契约。
  - GT 与四臂原始输出留私有区（`testdata/benchmarks/t3/`、`LexMask-workspace/perf-work/issue92-t1/`），**公开侧仅统计与判读**，无真实姓名/案名/实体原文。

- feat(eval-ng): 新增**速度测量模块**（`eval-ng/cli.py speed`）——把「端到端案卷速率」从一次性脚本变成可复跑、可出数、可机检的评测底座（#131，PR #133）。
  - **两条测量线**：`--driver single`（Python 驱动逐页 `/redaction/{id}/vision`，页间并发由运行时 k 控制）与 `--driver batch`（建 `smart_batch` 作业 → 上传 → 提交 → 轮询，逐页仪表取自作业详情），两侧都出**页线**（页数/分钟、逐页墙钟 p50/p95/IQR）与**卷线**（提交→末页完成的 `file_wall_s`）两组数字。
  - **判定面（四态 PASS/UNVERIFIED/BLOCKED/FAIL）**：D2 并发自证（驱动声明 k + 在飞峰值实测 k 双证）、D3 闭合校验（环和 vs 页总**同钟** ≤2%、残差双侧 0≤·≤10%、两序列**非同源**且比值 ∈[1.00,1.25]、键路径压平后必含五个环叶子）、D4 自足（`file_wall_s` 与客户端墙钟互证 ≤5%、md 与 JSON 逐项一致）、D5 脱敏（八项环境脚注硬校验、实例指纹四键 + sha256、数值字段白名单**路径级**表、质量子树判 FALSE）、D6 边界（非法轮排除、载荷同一 `force=true&include_result_image=false`、批作业经网关 ≥8 MiB 单文件）、D8 旁证（计数类只累加、字符串状态不丢、补丁生效断言必须真在文件里）。
  - **名实不一即 FAIL**：`D8.patch_effect` 要求臂声明的 `patches[].to`/`expect[].contains` 真出现在目标文件里；`D5.numeric_whitelist` 按**字段路径**放行（`summary.agg_value` 这种换名会被点名），质量字段换名也走不脱。
  - **新增公开树扫描器** `eval-ng/scripts/check_public_tree.py`（与报告脱敏扫描器同族，含**阳性对照自检**）：公网 IP / 绝对路径 / Windows 盘符路径 / 凭据形态 / `--denylist` 词表五类；默认只扫 `--base` 以来改动过的文件，行内 `public-tree-scan: ignore-line(<规则名>, …)` 可按规则放过（扫描器自己的阳性对照夹具用）。**注（Issue#160 起）**：该标记由「整行跳过」改为「按规则点名」，**裸标记与空括号 `ignore-line()` 都不再放过任何规则**。
  - **单测 128 条**（本 PR 新增 `backend/tests/test_evalng_speed.py` **120** 条，另有既有 `backend/tests/test_evalng_cli.py` 8 条；eval-ng 测试文件合计 **183** 条（`--collect-only` 实测）；零网络：`SpeedApi` 构造在单测里被替换成抛错版本，AC24 请求捕获用 `httpx.MockTransport`）；④ 云门实测又钉掉三处死角：`_hardware_capture` 取设备身份行而非 `hy-smi` 表头、`dedupe_checks` 同 id 按四态取最严、k 双证按 `min(声明 k, 该轮页数)`（页数足够仍达不到声明值依旧 FAIL）；eval-ng 全量绿；本机全量 `pytest` 失败名集合与 clean base 逐条一致（无回归）。
  - ④ 云实例端到端实测（10 页扫描 canary / 3 页真实案卷切片 / 8.3 MiB 单文件（8,703,181 B 的 1 页填充分片）过网关；实例：云 DCU 单机 + `SINGLE` 起 uvicorn，后端 `68f0e7ba`，仪器 `de197b74`–`f1f249c`）：
    - **D1 锚点复现**：`single` 线冷跑各 4 轮（弃第 1 轮取 2–4 中位）——k=3 **92.594s**（离散 10.1%，锚点 86.65s ⇒ +6.9%）、k=1 **207.468s**（离散 2.9%，锚点 198.13s ⇒ +4.7%），**`D1.anchor_ratio` = 2.241× ≥ 1.8× PASS**；两臂闭合/残差/跨钟/键路径/冷跑证据（pid 与 `start_time` 三轮互异）/并发双证全 PASS，判定 UNVERIFIED（无 FAIL；未项只有 env 臂的 `D8.patch_effect` 与无基准的 `D7.parity`）。
    - **D6④ 网关**：实例装不上真 nginx（无包网 + SCNet 禁 Docker）、本地 registry 不可达 ⇒ 用 stdlib **L4 原字节转发代理**（`equiv_proxy_l4.py`，只窥视每连接首个请求头做上限与记账）并声明偏差；两侧证据——**A 不限体量：8,703,181 B 的单文件经网关 `POST /files/upload` 200**（请求体 8,703,363 B，multipart 开销 182 B；0.68s）、**B 上限 1 MiB：同一请求 413 且不转发上游**（`{"detail":"请求体 8703363 字节超过反向代理上限 1048576 字节"}`，记账 `rejected_413=1`）。
    - **D9 真实案卷（67 页证据卷前 3 页切片、纯扫描、文本层 0 字符）**：`single` k=3 cold —— 逐页 p50 **19.09s**、`file_wall_s` p50 **24.538s**、闭合 0.001556 / 残差 0.001556 / 跨钟 1.041956 全在阈内、无质量数值、脱敏扫描 clean 且案名自检 0 命中；抽数后真卷报告已删（不留私有产物）。
    - **D7 与旧基准脚本对照 → PASS（原 BLOCKED 已闭环）**：批线曾因 `smart_batch` 在飞入队后 worker 不取件（提交后长期 `queued`、`processing recognition` 0 次，**只有重启恢复路径**能处理）而拿不到数——已另立 **Issue#137**（根因：Python 3.10 下 `asyncio.TimeoutError` 不是内建 `TimeoutError` 的子类 ⇒ `_worker_loop` 的 `except TimeoutError` 捕不到首个 2s 空闲超时 ⇒ worker 静默退出），修复 PR#138 经三轮独立冷评审后合入 `bf38eaad`。修复后两侧**均冷跑**、10 页合成扫描卷、同窗口对照：`pages_per_min` 相对差 **7.9%**（4.226 vs 4.59）、页 p50 相对差 **1.4%**（3489.5 vs 3540 ms），均 ≤20% ⇒ **`D7.parity` PASS**。（更早一次 33.8% 的 FAIL 系基准侧未冷跑、两侧温冷态不同，属对照装置不公平，不作判据。）⚠️ 云侧产物（instrument rev `f4b6acba`、`scnet-main`）本机两仓不可解析；口径与私有验收方案 §9.12 一致。
  - **批线账本根同作用域订正（④ 云门 D7 期实测发现，2026-10-16 补）**：批线逐页 `duration_ms`（外层）含槽位等待/装配等账外墙钟，与各管线 `<prefix>.total` 不同作用域 ⇒ 页根改取 `duration_breakdown_ms["total"]`（外层墙钟单列 `page_wall_ms`、跨页累积账单列 `other_breakdown_ms`，两者**只记录不入和**）；新增机判 `D3.root_scope`（作用域不对 ⇒ FAIL 并点名后果）。**阈值一处未动**：真实两页账目上改前闭合差 3.66%（>2% 必 FAIL）、改后 1.84% PASS；5 条新单测共存，把根换回外层墙钟即 4 红（改前红自证）。
  - **③ 第三轮冷评审处置**（裁决 REVISE；代码层无阻塞缺陷、无恒真判据，阻塞面在验收闭包）：新增 **`D1.corpus_premise`**——AC2/D1④ 从「只记录」变成**机判**：声明 `scanned_pdf`/`text_pdf` 必须与**本地 `pdftotext` 非空白计数**及**后端 `/parse` 的 `is_scanned`** 两侧一致，任一侧与【口径已于 2026-10-18 由 Issue#153 修订：改为按权威视图判定，『扫描件 + 内嵌文本层』不再误报 FAIL；本条保留 2026-10-16 当轮原文】声明不符 ⇒ FAIL，取不到某一侧 ⇒ UNVERIFIED（不猜 PASS）；夹具里的实例目录名字面量换成中性示例路径（不再靠 `ignore-line` 放行真实实例路径）；单测口径订正为 **128 条**（speed 120 + cli 8；eval-ng 测试文件合计 **183** 条，均 `--collect-only` 实测——含 R3/⑤ 补的扫描器判据与装配级接线用例，以及 ⑥/⑦ 补的**复合键 / 值形状 / 带类型注解赋值**三组扫描器判据；扫描器默认只扫 `--base origin/preview` 起的改动面，且需 `--allow-prefix <DCU 栈 dtk 前缀> --allow-prefix <DCU 栈 hyhal 前缀>` 才能过公开树红线门）。
    - **门③ 第六轮处置（凭据面）**：公开树扫描器的口令识别从「键名 `\b` 正则」改为「键名按 `_`/`-`/camelCase 切词元查词表 + 值形状 + 语境豁免」，从而覆盖 `api_secret` / `db_password` / `clientSecret` 这类**凭据词作后缀**的复合键（旧 `\b` 在 `_` 前后不成边界，整类漏网）；同时收紧值形状（分隔符 / JSON 片段 / CJK 文案 / CamelCase 类型名 / ID 形态一律不算口令），整树噪声由 18 处降到 **1 处预存示例**；口令面改为 `check_public_tree.credential_findings()` 单一真源，报表扫描器同源导入；补两条 pytest 判据（复合键必中 + 元数据/值形状必净），变异 M-S2/S3/S4/S5/S6 全 KILLED。
    - **门③ 第七/八轮处置（凭据面形态封堵）**：公开树扫描器再补两类赋值形态——`NAME: <类型注解> = "…"`（`[:=]` 会吃掉注解冒号、把 `str` 当值 ⇒ 整类漏网；注解段现锚到标识符开头、不含 `=` 与换行，覆盖 PEP 604 `str | None`、`Final[str | None]`、`Literal["a"]`、`Optional["Cfg"]`）与**引号键**形态（JSON / YAML / dict 的 `"key": "…"`，普通赋值的键名组不含引号 ⇒ 也曾整类漏网）；值组改为可捕获完整引号字面量（含空白）。探针矩阵 **17 正例 / 16 反例**全绿并落成 pytest 目录；整树模式 `credential_*` 13 条全部为合成夹具/文档示例，其中 `cloud-deploy/` 4 处疑似真实测试账号口令另立 **Issue#141**（同族 #140）。
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
