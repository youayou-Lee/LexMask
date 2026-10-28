# Issue#207 方案：单次处理预览翻页响应滞后（性能调优）

> Issue: https://github.com/youayou-Lee/LexMask/issues/207
> 分支：`perf/issue207-preview-flip`（从 preview=89f51f9 切）
> 日期：2026-10-29。状态：①设计 r2（①.5 冷审 r1=REVISE，已逐条处置，待用户点头）

## 0. 需求确认（⓪ 结论）

用户实测反馈（原话归纳）：单次处理识别开始或完成后（非阻塞），在左侧预览上传文件、快速点击上下页翻转时，**预览内容没有及时响应**；用户怀疑前端渲染方式，希望性能调优。用户已确认 Issue 定稿（2026-10-29「OK」）并指示开工。

场景拆解：翻页控件页码即时更新，滞后的是页面内容本体。影响面：单次处理预览台的三类画布路径（扫描件 PDF / 图片 / 文本 PDF 页面视图）+ 文本分页视图（次要）；识别进行中比空闲时更明显。

## 1. 取证与根因（2026-10-29 实测，canary = testdata/canary-issue207-preview-flip-12p.pdf，12 页假数据扫描风 PDF）

### 1.1 前端三短板（`frontend/src/features/playground/hooks/use-playground-image.ts`）

| # | 短板 | 证据 |
|---|---|---|
| F1 | **翻页不取消在飞请求**：vision 重跑线有 AbortController（`visionAbortRef`），预览线 `loadOriginalPreviewPage`/`loadRedactedPreviewPage` 的 authFetch 均无 signal——快速连点时跨页大响应各自跑完，大 base64 JSON 解析占用主线程与带宽 | `use-playground-image.ts:103-137,165-220`（无 signal）vs `:487-489`（vision 有） |
| F2 | **加载期间旧图驻留且无加载态**：catch 分支有意保留上一张图（防闪白，Issue 记录在案），但没有任何加载指示——用户无法区分「正在加载」与「没反应」 | `use-playground-image.ts:155-157,299-303`；`ImageBBoxEditor.tsx:678-685`（img 只有 onLoad，无 loading 布线） |
| F3 | **快速连点跑赢空闲预取**：`scheduleNeighborPrefetch` 每次 defer 300ms 且串行单发，连点速度超过预取速度时必然回落到大响应串行等待 | `use-playground-image.ts:235-245` |

### 1.2 后端根因：preview 渲染链阻塞事件循环（H3 的真身，比「资源争用」严重）

`get_pdf_page_image`（`file_parser.py:618-656`）与 `preview_redaction`（`vision_service.py:2149-2184`，API 层同名处理器为 `preview_image_redaction` @ `redaction.py:90`）都是 **async def 内裸跑同步 CPU 活**（PyMuPDF 150dpi 渲染 + PIL 打开/编码 PNG），无 `asyncio.to_thread`。实测证据（本地，真实代码路径）：

1. **渲染跑在事件循环线程上**：`threading.get_ident()` 比对——main 协程线程 == 渲染执行线程（True）；
2. **渲染期间循环完全不响应**：渲染前排队的 `loop.call_soon` 回调，直到渲染结束都没执行（整段渲染 = 一个不中断的循环步）；`asyncio.run(debug=True)` 报 `Executing <Task main()> took 0.259 seconds`（5 页连渲染一步跑完）；
3. **单页成本**（本地合成 canary，偏轻）：fitz 渲染 ~52ms/页，渲染+PIL 编码 ~107ms/页，响应 base64 ~37KB/页。合成样本压缩率高，真实扫描件载荷更大——**识别中的实测数字在云④补测**（规则 A：先探针后定阈值），载荷真实性用加噪版 canary v2 近似。

**放大机制**：识别进行中时，事件循环本就繁忙（任务轮询、流式编排）；每次未命中缓存的翻页 = 把整个后端所有请求（包括任务状态轮询、其他用户的请求）冻结渲染时长；快速连点 = 连续多次冻结叠加。前端 F1/F2 再把后端变慢放大成「点了没反应」的体感。

仓库既有规范本就要求避开此坑：`vision_service.py:1723` 注释「像素级过滤/收紧是纯 CPU 工作，放 worker 线程避免阻塞事件循环」、`has_client.py:327`「不得进事件循环」——预览链是漏网之鱼，修法与既有先例同构。

### 1.3 假设收口

- H1（前端串行等待+无反馈）成立 → F1+F2 修；
- H2（前端重渲染抖动）**降级为观察项**：轮询已自带 in-flight 守卫与 visibility 门控（`use-single-task-poll.ts:8`），后端阻塞修掉后预计大幅缓解；开发期 React Profiler 顺手看一眼，不为它扩 scope；
- H3（后端争用）真身 = 事件循环阻塞 → 本案后端修；
- H4（竞态卫生）并入 F1。

## 2. 改动点

### 2.1 前端 `use-playground-image.ts` + `playground-page.tsx`（+ ImageBBoxEditor 轻量口子）

1. **翻页 abort**：新增 per-hook 预览 AbortController。`currentPage` 变化时 abort 全部在飞预览请求（原始页、redacted 页、预取）再发起新页请求；卸载时同样 abort。保留 per-key in-flight 去重不变。与 vision 线既有 AbortController 模式对齐（visionAbortRef 独立、不受影响）。
2. **加载态**：hook 暴露 `previewPageLoading`（当前页内容在飞且缓存未命中）。playground-page 在画布容器上渲染轻量加载指示（叠加在旧图上，**不改「不闪白」语义**）；渲染失败时清除加载态、保留旧图、错误提示可见（既有 catch 语义 + 新增 loading 清理）。
3. **文案**：i18n 增加 loading 提示键（中英）。
4. 预取策略不动；abort 语义天然防止连点时预取堆积。

### 2.2 后端 `file_parser.py` + `vision_service.py`

1. `get_pdf_page_image`：渲染体（open doc → load_page → get_pixmap → tobytes → close）移入 `asyncio.to_thread`；缓存读写随渲染体一并进线程（`threading.Lock` 本就线程安全）。
2. `preview_redaction`：PIL 段（Image.open → convert → 打框 → save PNG）移入 `asyncio.to_thread`；两个源字节 await 保持不变。
3. **同键单飞（singleflight）**：`get_pdf_page_image` 增加按 `(resolved_path, page, dpi)` 的在飞去重——同页并发请求（连点风暴/多端同页）共用一次渲染。动机：客户端 abort 不会终止已进线程池的渲染，①.5 冷审 S7 指出无此层时连点 20 次=20 个线程池任务；单飞把服务端工作收敛到「每唯一页一次渲染」。实现小（in-flight future 表 + 既有锁），pytest 可测（并发同页只渲染一次）。
4. **fitz 线程安全**：每调用独立打开/关闭 Document（现状即如此），线程间不共享 Document 对象；与代码库既有 to_thread 先例（`ocr_has_vision_service.py:515` 等在 to_thread 里跑 OCR/PIL 混合负载）同构。开发期补注释说明该约束。
5. 不改任何识别产出：纯并发卫生，无行为差异（AC5 字节级钉）。

### 2.3 明确不做（scope 外）

- 文本分页 `renderMarkedContent` 重建成本（已有虚拟分页兜底，非本案主症状）；
- 预取窗口扩大/方向感知（无数据支撑收益，避免无据调参）；
- 识别管线本身的性能（#166/#172 线）；
- 服务端协作式取消（abort 后终止已开始的 to_thread 渲染）——单飞已把浪费收敛到每页一次，协作取消收益边际小、复杂度高；若云④ AC9 显示仍有挤压再立项。

## 3. 验收标准（Definition of Done）— r2（吸收 ①.5 r1 全部有效意见）

> 阈值锚定声明（规则 A）：本地探针数字来自 §1.2 实测（合成 canary，2026-10-29）；云④ AC7/AC8 的**判定式**（通过线，非描述数字）在改前基线探针跑完后冻结写进 PR——冻结的是判定式，不是观测值。**基线采集顺序硬约束：改前基线必须在部署改后代码之前采集并留证。**

**自动化测试（③，本地）**：

- **AC1 前端 vitest（机制：abort）**：模拟连点 1→2→3，断言页 1/2 的在飞 fetch 被 abort（signal.aborted=true），最终只有页 3 的响应被采纳；redacted 预览请求与预取请求同判据；卸载时在飞请求被 abort。
- **AC2 前端 vitest（加载态）**：缓存未命中翻页时 `previewPageLoading=true` 且画布出现加载指示（data-testid）；指示叠加在旧图上（画布不清空）；缓存命中翻页无加载指示、内容立即切换（防闪白不回归）；渲染失败时加载态清除、旧图保留、错误提示可见、后续翻页不受阻。
- **AC3 前端 vitest（结果级竞态，r1 重构）**：晚到响应**永不**覆盖当前页。两个 abort 管不到的晚到来源分别构造：(a) abort 竞窗——翻页已发起、abort 生效前后响应抵达的缝隙；(b) 预取晚到——某页预取响应在用户已翻过该页之后抵达。断言画布显示页 = 目标页。（注：AC1 钉实现机制，AC3 钉用户可见结果；AC3 在 AC1 存在时仍须可红——靠 (a)(b) 两来源构造，不依赖「慢响应后到」这一被 abort 消灭的场景。）
- **AC4 后端 pytest（事件循环不阻塞）**：`FileParser.get_pdf_page_image`（冷缓存）与 `VisionService.preview_redaction` 调用期间，同循环排队的 `call_soon` 回调能执行（探针模式进测试）。**改前红跑取证留痕进 PR**（判别性证明），改后绿。
- **AC5 后端 pytest（跨版本字节一致，r1 S8）**：改前基线代码对 canary 页抓取 preview-image 响应字节存为黄金夹具；改后同输入逐字节比对一致（替代 r1 版「二次调用字节一致」的弱判据；缓存幂等断言保留为子项）。
- **AC6 本地门禁（护栏，非判别性）**：`frontend: npm run build && npm test && npm run lint` + `backend: pytest tests/`，与 preview 基线 FAILED 集合 `comm -3` 零新增失败。
- **AC10 前端 vitest（回归三件套，r1 S5/S6/S9）**：(a) 文本分页路径 B 快速连点内容/页码一致；(b) 翻页落定后邻居预取仍发生并入缓存（下次翻命零加载指示）；(c) 同页并发只触发一次服务端渲染（singleflight，配 2.2-3）。

**云实例端到端（④；**全部用假数据 canary**：canary-12p + 加噪版 canary v2（模拟真实扫描件载荷），真实案卷不进验收链路）**：

- **AC7 空闲态翻页（过程采样，r1 S4）**：自动化连点 5 页，**连点过程中**轮询采样画布状态——断言出现过的每个页码要么内容已就位要么加载指示在场（不留「页码变内容无声滞后」窗口）；落定后内容身份 = 末页（img data-url 哈希比对，非肉眼截图）；空闲态末次点击→内容就绪时限 ≤ 冻结阈值（基线回填时冻结）。
- **AC8 识别进行中翻页（标题场景，r1 S1+S2）**：识别运行期间 (a) 连点 5 页功能断言同 AC7；(b) preview-image 响应时长分布：改后 p95 ≤ 改前基线 p95 × K（K 在基线采集时冻结）；(c) 无单请求级多秒停滞（上界从基线分布冻结）；(d) 任务状态轮询最大间隔 ≤ N × 正常周期（N 在基线采集时冻结）。
- **AC9 旁路与健康（r1 S7）**：连点风暴期间 status 轮询等旁路端点延迟有界（判定式同上冻结）；六服务健康检查 200；singleflight 生效证据（服务端日志/计数：风暴期间同页渲染次数收敛）。
- **部署与顺序**：rsync frontend/src + backend 改动文件 → 实例 rebuild 重启；**AC8 改前基线在部署改后代码前采集**。

**验收方案（⑤，分级）**：**AI 自验收**（r1 S10 修订表述）：验收链路全部使用假数据 canary（12p + 加噪 v2），不涉真实案卷；判据全部客观可机核（单测断言、响应字节、响应计时、自动化浏览器 DOM/哈希采样）。AI 逐条实测留证进 PR。用户交付后可自行用真实案卷体验复核（不作为门；若用户点头时改选人工验收，转 §4 人工流程）。

### 3.1 AC 交付偏差清单（门⑥ r1 留痕，2026-10-29）

- **AC5**：交付为测试内内联参考序列比对（复刻改前调用序列），非文档原拟的「跨版本黄金夹具」——黄金字节跨 PIL/fitz 版本易碎，同进程同库参考序列同样钉住「to_thread 迁移不改管线步骤」，判据强度等价。
- **AC1**：预取请求 abort 判据首轮遗漏，门⑥ r1 指出后已补（「翻页时在飞邻居预取请求一并 abort」用例）。
- **AC3**：竞窗主判据（页 2 真晚到，豁免 abort 拒绝）具判别性；预取晚到一例为**行为钉**（预取路径构造上不触显示 state，不可能红），已在测试头注如实标注——判别性由主判据承担。
- **AC10(a)**：路径 B 文本分页交付为 hook 共享页状态力学 vitest + 文本内容由 currentPage 纯派生的事实；云④未单独跑文本卷翻页（画布路径已全项覆盖，路径 B 无独立网络请求链）。

**abort 的有意代价注记（M5）**：翻页中途放弃的请求被 abort 后不回填缓存，立即回翻该页会重新拉取（改前会完成并入缓存）；加载指示已覆盖该窗口的反馈缺口，「翻页零等待」仅适用于已完成预取的邻居页。

## 4. ①.5 冷审记录（r1，2026-10-29）

独立评审员（仅 Issue 正文 + 验收方案 r1）结论 **REVISE**。处置表：

| 意见 | 处置 |
|---|---|
| AC3 在 AC1 落地后条件恒真 | **采纳**，重构为 (a) abort 竞窗 + (b) 预取晚到两来源（AC3） |
| AC6 事后截图准恒真 | **采纳**，改过程采样 + 内容哈希身份断言（AC7） |
| AC7 未冻结判定式 | **采纳**，明确冻结判定式而非观测值；基线采集顺序写为硬约束（§3 声明 + AC8） |
| AC4「二次调用字节一致」相对恒真 | **采纳**，换跨版本黄金字节比对（AC5），幂等断言降为子项 |
| 识别中翻页功能级断言缺失 | **采纳**，新增 AC8(a) |
| 用户级时限缺失 | **采纳**，空闲态时限进 AC7、识别中判定式进 AC8 |
| 路径 B / 预取 / 失败路径零覆盖 | **采纳**，AC10(a)(b) + AC2 失败路径 |
| abort 服务端后果（线程池堆积） | **采纳**，新增 2.2-3 singleflight + AC9 生效证据；协作取消列 scope 外（2.3）留触发条件 |
| `preview_redaction` 符号名误 | **部分采纳**：两层符号均存在——服务层 `VisionService.preview_redaction`（vision_service.py:2149，本方案测试目标）与 API 层 `preview_image_redaction`（redaction.py:90）；文档已注明层级，不改名 |
| 门⑤「不涉真实案卷」与 AC6 真卷矛盾 | **采纳**，云④全部改用假数据 canary（新增加噪 v2 近似真实载荷），真卷体验复核列为用户交付后可选项（§5） |
| AC1 钉实现机制 vs 序号令牌方案 | **知悉**：abort 方向已在 Issue 建议方向拍板，AC3 作为结果级护栏兜底机制选择 |
| AC5/AC8（现 AC6/AC9）护栏非判别 | **采纳**，AC6 标注「护栏，非判别性」 |

## 5. 流程与产物清单

- ①.5：r1 已完成（上表）；本 r2 设计与验收方案**一起交用户点头**，点头后进 ②；
- ②③：TDD（AC1-5、AC10 先红后绿；AC4 改前红跑留痕）→ 本地门禁全绿；
- ④：按 §3 顺序上云实测（先基线后新码）；canary v2（加噪版）随本案生成入 testdata（假数据，`canary-issue207-*.pdf` 命名）；
- ⑤：AI 自验收逐条留证；⑥：独立 review（**纯前端交互 + 与识别产出无关的后端内部重构，§5.1 豁免类**，PR 描述注明类别与证据：AC5 字节级一致 + 识别产出无关声明）；
- CHANGELOG 当天条目；方案文档随 PR 入库（本文件）；
- 交付后用户可选：真实案卷体验复核（不作为门）。

## 6. 门④实测结果回填（2026-10-29，实例 scnet-main @preview=89f51f9 → 本分支 9bf31c9）

**改前基线采集顺序**：先在现役 preview 代码跑探针（baseline.json），后部署，判定式随基线冻结如下并全部 PASS：

| 判定式（基线后冻结） | 判定线 | 基线 | 改后 | 结果 |
|---|---|---|---|---|
| AC8(b) 识别中 preview p95 | ≤ 509ms（基线 339.4×1.5） | 339.4ms | 333.2ms | PASS |
| AC8(c) 识别中 preview max | ≤ 500ms | 340.4ms | 336.9ms | PASS |
| AC8(d) 任务轮询最大间隔 | ≤ 500ms（2×周期 250ms） | **1390.3ms** | **373.4ms** | PASS |
| AC8(d) 单笔轮询最大延迟 | ≤ 300ms | **1140.2ms** | **123.3ms** | PASS |
| AC9 旁路 /health max | ≤ 200ms | 64.2ms | 55.8ms | PASS |
| AC9 并发同页 ×6 总耗时（单飞签名） | ≤ 700ms | **1226.1 / 1305.2ms** | **431.6 / 490.9ms** | PASS |

- 轮询两项即「事件循环阻塞」的实例级直接证据：改前每次未命中缓存的翻页把全部请求冻结渲染时长（1.4s ≈ 4×336ms 渲染叠加），改后渲染在线程池、循环不再被冻。
- 并发同页 ×6 从 3.7× 单渲染收敛到 1.3× = 同键单飞生效的直接证据。
- **AC7 浏览器过程采样 PASS**：空闲态连点 1→5，31 个采样点（~10ms 分辨率）0 错误、0 无声滞后窗口，冷页加载窗口内 25 点加载指示在场（`data-testid="preview-page-loading"`）；落定页码 5/12 且画面 md5 与慢速参照 pass 逐字节一致（c93feb0460）。
- 部署：rsync（backend 两文件 + frontend/src 整树）→ 实例 rebuild → tmux 重启。**事故记录**：respawn 用 `pane_start_command` 原文再包引号导致窗口即死，已按 start_all_dcu.sh 同款命令重建窗口、五服务 200 复核；教训=该返回值自带外层引号，不可再包一层。
- 探针与样本：实例侧 `issue207` 探针目录（probe207.py、baseline.json、fixed.json，完整路径属实例运维细节留私有部署记录）；canary 两份已入 testdata（假数据）；实例 uploads/file_store 中 7 条本案测试记录已清理（作业流水留痕）。
