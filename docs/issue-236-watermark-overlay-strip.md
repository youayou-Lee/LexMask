# Issue#236 水印预处理：独立矢量覆盖水印流的检测与摘除（设计与验收方案 v2.2）

- Issue: [#236](https://github.com/youayou-Lee/LexMask/issues/236)（#215 阶段 1 第一块）
- 关联: #215（族 A 卷结构与分阶段）、#206 / PR#214（同族另一路：渲染面 OP 级剥离 + 建链流词过滤；阶段 1 只规划未实现）、#203（文档级水印闸，**本 Issue 不改其行为**）、#204
- 门禁: 门①（设计含验收判据）→ 门①.5 独立冷审（冷审输入**只有** Issue 正文 + 第 5 节验收判据）→ 门② 开发
- v2 变更（门①.5 第一轮 REVISE）：订正 coverage 口径数值与方向（§2.2）、补暖负样本、栅格保全、正向对照、原件保护、入口矩阵、副本治理、错误码枚举、环境口径表、变异 10/10、门⓪ 异构卷口径。
- v2.1 变更（门①.5 第二轮 REVISE，8 阻断 + 7 重要）：AC3/AC8 绑「副本确实产出」防空转、AC2b 补**正文 span 集合 ON/OFF 相等**（正向对照判据，v2.2 已换成 GT 行对照）、差异掩码改用**只画 B 流的独立 oracle**、新增 **AC7 异构卷**（m6 的落点）、AC6 计数拆成「检测=1/构建=0」、AC5b 改为**不设环境变量验默认支路**、AC9 补 `not_pdf/not_applicable`、`pages_probed` 改抽样下限口径、性能/状态语义可机判、环境归属表订正，并把「#203 闸照旧触发」升为拍板项（§7.4）。
- v2.2 变更（门①.5 第三轮 REVISE）：正文 oracle 从「非水印 span 集合」改为**GT 行对照**（真卷 6,039 行→副本 3,804 行逐页相等，本机已核验 mismatch=0）+ 新增 F2「正文在文字层+水印独立流」夹具；像素掩码钉成「只画 B 流 + 阈值 254 + 膨胀 3 px」的独立 oracle（真卷 4.48%/canary 32.25%，掩码外差异 0、掩码内清除率 0.51/0.53）；补夹具清单 F1–F7/W1–W4/C1–C3 与 GT json；m3/m4/m6 改挂 F6/F7/F5；AC10 改为「阶段级枚举码 + 任务级与基线一致」并给 7 用例映射表；AC8③ 明确 VL-MD 渲染段接副本、agent_md/MinerU 段排除；AC9 补原子性/回收/TTL 竞态；守卫支路标注；数值口径统一（coverage 区间 0.36–0.39、canary 与真卷各自锚定）。
- 门①.5 已用满 3 轮（r1 9 条恒真+1 反号 → r2 8 阻断 → r3 4 阻断），v2.2 逐条处置；如需第 4 轮须用户授权（门禁上限 3 轮）。

---

## 1. 背景与目标

族 A 真实案卷（149 页扫描件）每页的 `/Contents` 是**两条独立内容流**：

| 流 | 体量 | 内容 | 作用 |
|---|---|---|---|
| A | 46 B | `/Fm1 Do` | 整页扫描图（Form XObject，内含不可见 OCR 文字层） |
| B | 5.3–5.4 kB | 30 组 `q … Q`：`0.25098 rg` / `/Gs1 gs`(alpha) / 45° `cm` / `BT /F1 29.22 Tf <hex> Tj ET` | 防泄漏水印层 |

水印是**独立的矢量覆盖流**，不是烧进像素、也不属于正文文字层，所以可**无损摘除**：把页对象 `/Contents` 从 `[A B]` 改成 `[A]` 即可，原件 1 字节不动。

**目标**：识别流水线最前置的预处理——检测「独立矢量覆盖水印流」并生成**内部识别专用净化副本**；**渲染面**（识别取图、VL-MD 取图）改吃净化副本，让 OCR/视觉/实体抽取看不到水印像素与不可见水印字形；**原件、文本层判定链、交付物默认路径不动**。

**非目标**（沿用 Issue）：烧进扫描图内部的栅格水印（需 inpaint，#215 阶段 2）、文字层内嵌水印碎片（无独立流形态，#203 闸继续管）、水印 OCR 质量优化本身。

---

## 2. 现状实测（判据阈值与红性的锚定证据）

全部数字由本机 PyMuPDF 1.27.2 系列（补丁号 3）+ 本仓真实代码对族 A 卷与三份负样本实测；脚本与产物在容器 `.tmp/i236/`（不入库）。

### 2.1 结构事实

- 149/149 页 `/Contents` 均 2 条流；A 全部 46 B（`Do`×1、`BT`×0），B 全部 5.3–5.4 kB（`BT`×30、`Do`×0）；水印流位置恒为第 2 条（slot=1）。
- 水印 span 特征（`get_text("dict")`）：**15 个/页**、去空白文本 distinct=5、**最高重复 6 次**、`font=NotoSansCJKsc-Regular`、`size` 29.22–29.79（**极差比 1.017**）、`alpha=51`、`dir=45°`。
- 全卷原始可提取文本 **90,475 字符**；其中水印 **43,355**（≈282 字符/页），正文层 **47,120**（16 页为 0；正文层在 A 的 XObject 内）。
- `quality.classify` = USABLE（avg_chars 275、ink_alignment 0.1312）。

### 2.2 关键口径订正：#203 闸的数字与方向（v1 写反，已实测）

用**本仓既有函数**实测（`extract_text_layer_pages` + `measure_repeated_fragment_coverage`，阈值 `TEXT_LAYER_MAX_REPEATED_FRAGMENT_COVERAGE=0.15`，`text_layer_doc_source.py:218` 判定 `coverage > 0.15` 才拒）：

| 输入 | 可提取字符 | coverage | 闸判定 | 后果 |
|---|---|---|---|---|
| **原件** | 90,475 | **0.3764**（fragment_kinds=6, max_fragment_pages=149） | **拒绝**（`repeated_fragment_coverage`） | docline 建链 **0 次**；路由=逐页 OCR，与今天一致 |
| **净化副本** | 47,120 | **0.0407**（fragment_kinds=4, max_fragment_pages=45） | **放行** | **若副本喂文本层 ⇒ docline 接管整卷** |

⇒ 不可协商的边界（数据钉死）：**净化副本只能供渲染**。副本喂文本层会让 coverage 从 0.3764 掉到 0.0407、闸从「拒」翻成「放行」，docline 接管 149 页；按约 22 s/2000 字块估算 ≈ 24 块 ≈ 528 s，而 `BATCH_RECOGNITION_PAGE_TIMEOUT=300 s` 是**页级**计时（O(卷) 工作量撞 O(页) 墙）⇒ **整卷清零**，正是 2026-10-28 事故的形态。因此文本层判定、docline 建链、#203 闸、**原件视图端点**一律继续吃原件；「打码结果预览」随交付策略（见 §3.3 接线表）。

### 2.3 检测与摘除成本实测

| 动作 | 范围 | 实测 |
|---|---|---|
| 差分 oracle 探测（5 页） | 族 A 卷 | **25.7 ms**（原型）/ **29 ms**（生产实现），5/5 命中，候选唯一（slot 全=1） |
| 全卷逐页差分 oracle | 149 页 | 1.12 s（149/149 唯一）⇒ 只在探测页用 dict oracle，全卷用廉价校验 |
| 全卷摘除 + 逐页廉价校验 + save | 149 页 | **633 ms**；含探测的首次调用 **681 ms**；dropped=149 / skipped=0；副本 25,798,037 B |
| 逐页廉价校验本体 | 149 页 | 0.247 s（被删 745 行，全部 ⊆ 水印字符集） |
| 负样本 78 页文本型 / 6 页扫描型 / 10 页带文字层 canary | — | 0 个水印 span ⇒ `no_wm_spans`；14 / 11 / 265 ms |
| 第二次 `render_source`（同卷） | 149 页 | **0.1 ms**（memo 命中，不再探测/构建） |

⇒ AC11「149 页预处理 < 1 s」空间充足（681 ms），且「每卷一次」由 memo + 卷级锁保证。

### 2.4 像素差对照（150 dpi，`get_pixmap(matrix=Matrix(150/72,150/72))`）

- 族 A 卷页 1（副本 vs 原件）：changed fraction **2.2961%**、max|Δ|=**34**；差异只应出现在水印笔画覆盖处（非水印区须逐像素相等）。
- 合成 canary 页 1（全栅格正文 + 30 份水印）：changed **17.083%**、max|Δ|=84；摘除后页文本 **0 字符**、正文仅在像素里。
- **独立掩码 oracle**：把页渲染成「只画被摘掉的那条流」（临时文档，`/Contents=[B]`）⇒ 得到水印笔画区域掩码；像素差值必须全部落在掩码内（掩码外 max|Δ|=0）。这是 AC1③/AC2c 的判定依据，不是用差集自证。

### 2.5 判据陷阱（教训）

- 「剩余文本行 ⊆ 水印字符集」不能当残余守卫：字符集仅 16 个字符（数字+固定字样），正文 18 位报关单号等 **147 行**天然 ⊆ 该集合。可用守卫=**剩余行包含已知水印令牌**（去空白、len≥6）。族 A 卷副本残余命中 **0**（令牌 6 个）。
- `insert_text` 的 `rotate` 只接受 90° 倍数；45° 水印只能用 `TextWriter.write_text(morph=(Point, Matrix(45)))` 合成。
- 合成 canary 实测（3 页、每页 `[A B]`）：probe 478 ms 命中（slots=(1,1,1)）、build 47 ms、摘后 0 字符。

---

## 3. 方案

### 3.1 检测（probe，每卷一次，纯 CPU）

对探测页（默认 5 页，按 `round(i*(n-1)/(k-1))` 均匀取，覆盖首/末页；页数 ≤ k 时全取；探测页数 < `MIN_HIT_PAGES` ⇒ `below_min_pages`）：

1. 取「旋转 + 半透明 + 有文字」span：`|dir.y| ≥ TILT_MIN(0.2)` 且 `ALPHA_MIN(1) ≤ alpha ≤ ALPHA_MAX(220)`。无此类 span ⇒ `no_wm_spans`。
2. 每条内容流做**纯字节 op 白名单**扫描：`q/Q/cm/BT/ET/Tf/Td/TD/Tm/T*/TJ/Tj/'/"` 与文字/图形状态类 op 允许；出现任何绘制类 op（`f/F/f*/B/B*/b/b*/S/s/Do/BI/sh`）或未知 op ⇒ 该流不合格（族 A 卷 A 流被排除、B 流通过）。
3. 对每条合格流做**差分 oracle**：临时把该流从 `/Contents` 摘掉 → `reload_page` → 取 span 列表，要求 **①摘后无水印 span** 且 **②摘后非水印 span 与摘前逐字段精确相等**（text/bbox/font/size/dir/alpha），随后恢复 `/Contents`。这是「锚定独立流而非重复文字」的判据：文字里重复的水印不会因摘掉某条流而消失（①不成立）；把正文或不可见文字层一并摘掉会让②不成立。
4. 该页候选流**必须唯一**（0 个 ⇒ 未命中；≥2 个 ⇒ 该页弃权）。
5. 卷级命中：`hit_pages ≥ MIN_HIT_PAGES(3)`，且各命中页签名一致——同字体、同旋转方向、`size` 极差比 ≤ `MAX_SIZE_SPREAD(1.5)`（实测 1.017）、`alpha` 极差比 ≤ `MAX_ALPHA_SPREAD(2.0)`（实测 1.0）、单页最高重复次数 ≥ `MIN_REPEATS(3)`（实测 6）。
6. 输出：`code ∈ {hit, no_wm_spans, no_isolated_overlay, below_min_pages, signature_drift, capped_by_size, encrypted, not_pdf, not_applicable, disabled, error}`（`encrypted` = 需要口令/已加密 ⇒ 一律不摘，防破坏加密与签名，也防副本绕过原件访问控制） + `pages_probed/pages_hit/slots/fonts/size_spread/alpha_spread/top_repeats/charset/tokens/elapsed_ms`。
   - `not_pdf`：文件不是 PDF（docx/图片等，读取即失败）。
   - `not_applicable`：是 PDF 但形态明确不适用（**仅** 0 页卷）；加密卷另给 `encrypted`，两者不混用。

### 3.2 摘除（build，每卷一次）

**卷级决策、页级执行**（门⓪ 口径）：卷级 probe 命中决定「本卷做预处理」，摘除与校验**逐页独立**；部分页有水印的异构卷 ⇒ 有水印页摘除、无水印页 `skip`，`pages_skipped > 0` 显式记账（不把页级差异汇总成「全卷成功」）。

逐页：
1. 内容流 < 2 条 ⇒ `skip`。候选顺序：**该页**在探测期被证明的 slot 优先（`ProbeOutcome.slot_by_page`，按页号记录；门⑥ 二轮修掉「按命中顺序索引页号」的错配），其余按白名单顺序尝试。
2. 摘掉该流 → `get_text()` 行集合**廉价校验**：`copy_lines ⊆ orig_lines` 且 `orig − copy` 的每一行 `set(line) ⊆ 水印字符集`。任一不满足 ⇒ 恢复 `/Contents` 并 `skip`。
3. **残余守卫**：摘后剩余行若仍含已知水印令牌（去空白、len≥6）⇒ 恢复并 `skip`。
4. 全卷处理完 → `save(garbage=3, deflate=True)` 到临时文件 → **落盘自检**（重新打开、页数与页尺寸逐页一致）→ 原子 `os.replace` 到副本路径、`chmod 0600`；自检不过 ⇒ 删临时文件、状态 `failed`、回落原件。
5. 任何异常 ⇒ 状态里 `failed=true` + `error`（脱敏后的类型/消息，不含服务器绝对路径），渲染回落原件，**不阻断识别**；构建自检失败单独给 `code=copy_verify_failed`（同样 `failed=true`）。失败状态按指纹进进程内 memo（重试不重复付代价）；fail-open 结果只保鲜 **60 s**（`_FAIL_OPEN_TTL_S`），窗口过后重试，避免权限/磁盘故障修好后被永久负缓存钉死。

### 3.3 接线（唯一取图口收口 + 交付开关）

新增 `backend/app/services/watermark_overlay.py`（不 import 业务服务），`file_parser` 增加一个 kwarg：

| 调用点 | 取图对象 | 改动 |
|---|---|---|
| `vision_service.py:298` 识别取图（批量/playground/重识别/重试都经此） | 净化副本 | 默认 `allow_strip=True` |
| `vl_md_pipeline_service.py:192` 供料 VL-MD 渲染 | 净化副本 | 默认 |
| `vision_service.py:2263` 预览红框底图 | **随交付策略**（默认去水印 ⇒ 副本） | `_delivery_source()` 解析后 `allow_strip=False`；与交付同源，避免「预览有水印、下载没有」 |
| `api/files.py:822` 文件预览/标注端点 | **净化视图**（默认 `allow_strip=not raw`） | 上传即见效；`?raw=true` 显式退回原件（诊断用） |
| `vision_service.py:2228` `_redact_pdf` 交付 PDF 光栅化 | **原件**（默认保留）/ 净化副本（开关关闭时） | 新参数 `source_path`，由交付开关决定 |
| `text_layer_docline_service.py:195` ink alignment | 原件 | 不动（直连 `get_pixmap`） |
| `redaction/watermark.py:50` 交付水印合成 | 原件 | 不动 |
| `agent_md`（MinerU 原件字节上传） | 原件 | **本 Issue 不动**（§6.2） |

`FileParser.get_pdf_page_image(file_path, page, dpi=150, *, allow_strip=True)`：
- 先 `await asyncio.to_thread(watermark_overlay.render_source, resolved)`（探测/构建都在 worker 线程，事件循环不冻结；并发由卷级锁串行到「只做一次」）；
- **页图缓存键必须携带变体**：`(*_pdf_page_cache_key(...), "clean"|"raw")`——否则预览（raw）与识别（clean）互相污染同一页缓存，正是「预览/交付悄悄变干净」的漏口（AC5 钉住）；
- `_render_pdf_page_image` 用 `open_pdf_checked(副本 | 原件)` 渲染。

交付开关：`WATERMARK_OVERLAY_DELIVERY_KEEP_WATERMARK=true`（默认）。置 false 时 `_redact_pdf` 以净化副本为光栅化源（交付 PDF 无水印）；副本不可用则回落原件并记 `delivery_source=original`。**两支路都必须实现并验收**，否则开关是死代码。

### 3.4 副本生命周期

- 目录 `settings.OUTPUT_DIR/.watermark_overlay/`（`makedirs(0o700)` + `chmod 0700`；`cleanup_orphan_files` 只扫顶层文件 ⇒ 不被小时清理误删，也不进任何导出）。
- 命名 `<sha256(realpath|size|mtime_ns)[:32]>.pdf`；原件更新即换名。
- TTL `WATERMARK_OVERLAY_COPY_TTL_HOURS=24`：每次**命中水印后的**构建前扫一次，删除超龄文件；memo 命中也要过 TTL 校验（过期副本不返回，按需重建）。清理只豁免「保鲜且被 memo 持有」的副本与在飞临时文件。
- **进程级定时清理（门⑥ 二轮处置补；增量短审后收紧）**：仅靠「下一次有水印卷构建」才扫，会让「只处理过一次的卷」副本无限滞留 ⇒ 新增 `sweep_expired(ttl_hours=None)`（返回删除数量；取 `WATERMARK_OVERLAY_COPY_TTL_HOURS`，`OSError`/非法取值一律返回 0 不抛；目录不存在直接返回 0，不 `makedirs`），由 `app/main.py` 的 `_cleanup_tick()` 统一调用，`_periodic_cleanup()` 每小时经 `asyncio.to_thread` 跑一次（不阻塞事件循环）+ `lifespan` 启动一次。
  - 判据：`test_sweep_expired_clears_stale_orphans`（过期清/保鲜留）、`test_sweep_expired_keeps_memo_held_copy`（小 TTL sweep 不删 memo 保鲜副本）、`test_cleanup_tick_calls_sweep_expired`（monkeypatch spy，**删或注释调用即红**）、`test_periodic_cleanup_runs_tick_in_thread`（AST 断言 `asyncio.to_thread(_cleanup_tick)`）。
  - `WATERMARK_OVERLAY_COPY_TTL_HOURS` 校验器下限抬到 **1**（0 会让 sweep/memo 双双永不过期，AC9 被静默废掉）；`test_copy_ttl_validator_forbids_zero` 钉住。
  - 诚实边界：多点位部署下 `_CACHE` 不跨进程，A 进程可能删掉 B 进程已在用的过期副本 ⇒ B 会收到一次 `FileNotFoundError`（有页级隔离兜住，不回落原件）；当前生产为单 uvicorn worker（`backend/Dockerfile:55` 无 `--workers`），且默认 TTL 24h 使窗口极小。
- 进程内 memo（键=fingerprint）：`{code, pages_probed, pages_hit, pages_stripped, pages_skipped, pages_guard_refused, duration_ms, error, copy_path}`；命中与 fail-open 都缓存（后者 60 s 短窗口）；对外状态**剔除 copy_path 与内部字段**。
- 并发：卷级 `threading.Lock`（进程级）+ in-flight 登记；第二个请求等首个完成（AC9 断言构建调用计数=1）。
- 体积闸：`size > WATERMARK_OVERLAY_MAX_FILE_MB(400)` ⇒ `capped_by_size`，不处理。

### 3.5 配置键（默认值）

| 键 | 默认 | 说明 |
|---|---|---|
| `WATERMARK_OVERLAY_STRIP_ENABLED` | **true** | 识别面总开关（关=完全回到今天行为） |
| `WATERMARK_OVERLAY_DELIVERY_KEEP_WATERMARK` | **true** | 交付面是否保留原水印（拍板项） |
| `WATERMARK_OVERLAY_PROBE_PAGES` | 5 | 探测页数 |
| `WATERMARK_OVERLAY_MIN_HIT_PAGES` | 3 | 卷级命中最低页数 |
| `WATERMARK_OVERLAY_MIN_REPEATS` | 3 | 单页水印文字最高重复下限（实测 6） |
| `WATERMARK_OVERLAY_MAX_SIZE_SPREAD` | 1.5 | 字号极差比（实测 1.017） |
| `WATERMARK_OVERLAY_MAX_ALPHA_SPREAD` | 2.0 | 透明度极差比（实测 1.0） |
| `WATERMARK_OVERLAY_TILT_MIN` | 0.2 | `|dir.y|` 下限（45° ⇒ 0.707） |
| `WATERMARK_OVERLAY_ALPHA_MAX` | 220 | 半透明上限 |
| `WATERMARK_OVERLAY_MAX_FILE_MB` | 400 | 体积闸 |
| `WATERMARK_OVERLAY_COPY_TTL_HOURS` | 24 | 副本 TTL |

`.env.example` 同步登记（含锚定注释）。

### 3.6 可观测（页级 `pipeline_status["watermark_overlay"]`）

```json
{"ran": true, "skipped": false, "failed": false, "region_count": 0, "error": null,
 "duration_ms": 681, "code": "hit", "pages_probed": 5, "pages_hit": 5,
 "pages_stripped": 149, "pages_skipped": 0, "source": "copy"}
```

- `source ∈ {copy, original}`：本页渲染实际吃了谁——**唯一机器可读证据**。
- 单字段 `code`（无独立 `error_code`）：探测期 `{hit, no_wm_spans, no_isolated_overlay, below_min_pages, signature_drift, capped_by_size, encrypted, not_pdf, not_applicable, disabled, error}` + 构建期 `{stripped⇒hit, guard_refused_pages, no_strippable_pages, copy_dir_unwritable, copy_verify_failed, error}`；`failed = code ∈ {error, copy_verify_failed}`。
- 全局关闭时固定返回 `{"code":"disabled","source":"original","skipped":true,...}`（OFF 轮也可判定）。
- 前端对 `pipeline_status` 通用遍历，新标签自动可见。

### 3.7 计数探针（供验收用，生产不加日志噪音）

模块暴露只读计数器 `watermark_overlay.stats()`：`{"probe_calls": n, "build_calls": n, "build_skipped": n, "copy_created": n}`（进程级，测试/运维可读）。验收判据里所有「调用了几次」都读它，不靠日志抽样。

---

## 4. 实施清单

| 文件 | 动作 |
|---|---|
| `backend/app/services/watermark_overlay.py` | 新增：probe / build / memo / 卷级锁 / TTL（含 `sweep_expired`）/ 状态 / stats |
| `backend/app/core/config.py` | 11 个配置键 + 6 个 validator |
| `backend/.env.example` | 同步登记 |
| `backend/app/services/file_parser.py` | `allow_strip` kwarg、变体缓存键、副本渲染源 |
| `backend/app/services/vision_service.py` | 写 `pipeline_status["watermark_overlay"]`；预览点 `allow_strip=False`；`_redact_pdf` 交付开关 |
| `backend/app/services/vl_md_pipeline_service.py` | 默认走副本（确认/无改动） |
| `backend/app/api/files.py` | 预览/标注端点默认净化视图 + `raw=true` 逃生口 |
| ~~`backend/tests/test_watermark_overlay_detect.py`~~ → 合并为 `backend/tests/test_watermark_overlay_strip.py` | 检测：正样本 + **暖负样本组** + 冷负样本 + 异构卷 |
| ~~`backend/tests/test_watermark_overlay_build.py`~~ → 同上单文件 | 摘除：逐页校验/残余守卫/异构卷/自检失败/原子性 |
| ~~`backend/tests/test_file_parser_watermark_wiring.py`~~ → 同上单文件 | 变体缓存/预览原件/原件 sha256/串行 memo/TTL/权限/损坏副本 |
| ~~`backend/tests/test_vision_watermark_status.py`~~ → 同上单文件 | `pipeline_status` 形状 + OFF=disabled + stats |
| ~~`backend/tests/test_delivery_watermark_switch.py`~~ → 同上单文件 | 交付开关双支路（像素级） |
| `CHANGELOG.md` | 合并时登记 |

测试一律**合成夹具**（公开仓不依赖 testdata）；真实案卷与 canary 走 `testdata/`（git-crypt）。

---

## 5. 验收判据（门①.5 冷审对象，v2.2）

> 规则：每条判据在「功能不做 / 被改坏」时**必须 FAIL**；阈值有第 2 节实测锚定；判据锚定**行为/输出**（GT 文本行、像素掩码、计数、机读码），不以「字段存在」为唯一证据。标〔守卫〕的条目单独在组件缺席时会通过，但它所属的 AC 另有一个组件缺席必红的支路。环境归属见 AC13。

### 5.0 夹具清单（落盘策略：F1 落私有 `testdata/` + GT；F2–F7/W1–W4 由测试内 `build_pdf()` 现造）

**落盘实现（2026-11-04 定稿）**：只有 **F1（canary）** 以文件形式落 `testdata/`（含 `-gt.json`），生成器 `LexMask-workspace/scripts/issue236/gen_canary.py`（`--out-dir` 指定目标、`--verify-module` 用仓库实现交叉核验）；其余夹具由 `backend/tests/test_watermark_overlay_strip.py::build_pdf()` 在临时目录现造——理由：单测需要「每次全新建文件」且不能让 CI 依赖 git-crypt，文件形态与下表逐条一致，判据不变。

F1 实测（生成即核验，`--verify-module`）：
```
pdf  testdata/canary-issue236-vector-overlay-watermark.pdf (1,825,674 B, 3 页)
gt   testdata/canary-issue236-vector-overlay-watermark-gt.json
sha  0ab90d517eab14002936cb62d4430a4693a17b48117089a99961e6ea618a5b45
verify: {"probe_code":"hit","pages_probed":3,"pages_hit":3,"build_code":"stripped",
         "pages_stripped":3,"copy_chars_total":0,"copy_token_hits":0}
```
- 每页 `/Contents=[A(栅格) B(水印)]`（页 1 `[7, 15]`），页文本 491 字符/30 行全部来自水印；GT `copy_lines=0 / copy_chars=0 / tokens=["某某市院张某某450102199001011234"]`。
- **sha256 非稳定**：PDF 内嵌生成时间戳，重跑会换 sha ⇒ GT 里的 `sha256` 只对应当次产物（判据依赖「形态 + 行/字符数」而非字节指纹）。


| 夹具 | 文件 | 形态 | 期望 |
|---|---|---|---|
| F1 | `canary-issue236-vector-overlay-watermark.pdf` ✅已落 testdata | 3 页；全栅格化名正文（PIL 150 dpi）；每页 30 份水印 `某某市院 张某某450102199001011234`（`TextWriter`，29.22 pt，opacity 0.25，45°），`/Contents=[A,B]`；页文本 491 字符/30 行（边缘份数被裁切，属允许形态） | `hit`，摘后页文本 0（已实测） |
| F2 | 🧪测试内构造 `canary-issue236-textlayer-overlay.pdf` | 3 页；**正文为不可见文字层**（`render_mode=3`，A 流）+ 水印独立流 B；页文本 = 正文 + 水印 | `hit`；副本页文本 = GT `copy_lines`（>0）、水印令牌 0 |
| F3 | 🧪测试内构造 `canary-issue236-partial-overlay.pdf` | 5 页；3 页 `[A,B]`（有水印）、2 页单流（无水印） | `hit`；`pages_stripped=3`、`pages_skipped=2` |
| F4 | 🧪测试内构造 `canary-issue236-double-overlay.pdf` | 3 页；`/Contents=[A,B1,B2]`，B1/B2 均为水印流 | `no_isolated_overlay`（候选不唯一），不产出副本 |
| F5 | 🧪测试内构造 `canary-issue236-overlap-residual.pdf` | 3 页；水印同时出现在可摘流 B **和** 扫描图内的不可见文字层（摘 B 后仍残留） | 逐页 `skip`（残余令牌守卫），`pages_skipped=3`，不产出可用副本 |
| F6 | 🧪测试内构造 `canary-issue236-dual-qualified.pdf` | 2 页；`[A,B_wm,B_body]`，B_body 是合法不可见正文层（水平、`alpha=0`） | `hit` 且只摘 B_wm；副本页文本 = GT 正文（B_body 全部保留） |
| F7 | 🧪测试内构造 `canary-issue236-single-page-overlay.pdf` | 5 页；仅 1 页有水印 | `below_min_pages`，不产出副本 |
| W1 | 🧪测试内构造 `canary-issue236-warm-inline.pdf` | 2 页；水印文字并入主内容流（图 `Do` + 旋转半透明文字，单流） | `no_isolated_overlay`/`no_wm_spans`，不产出副本 |
| W2 | 🧪测试内构造 `canary-issue236-warm-legal-textlayer.pdf` | 2 页；`[A,B]`，B 为合法 OCR 文字层（`alpha=0`、水平、不可见） | 同上 |
| W3 | 🧪测试内构造 `canary-issue236-warm-vector-table.pdf` | 2 页；`[A,B]`，B 为矢量表格/公章绘制（含 `f/S/re`） | 同上 |
| W4 | 🧪测试内构造 `canary-issue236-warm-raster-watermark.pdf` | 2 页；水印烧进栅格图，B 为空流 | 不产出副本（非目标形态） |
| C1–C3 | `testdata/文本型-叶辉审查报告.pdf`（78 页）、`testdata/扫描型PDF误判为文本型-1.pdf`（6 页）、`testdata/canary-issue115-textlayer-10p.pdf`（10 页） | 冷负样本（实存） | `no_wm_spans`，零副本 |
| R1 | `testdata/第四卷.pdf`（149 页，实存，私有） | 真卷：正文在 A 的不可见文字层、水印 B | `hit`；GT 见 §5.1 |

GT json 结构（F1 落盘一份，其余由 `build_pdf()` 现算）：`{"page": 1, "orig_lines": n, "wm_lines": m, "copy_lines": k, "copy_chars": c, "tokens": [...]}`，由生成器**从原件计算**（`copy_lines = orig_lines − wm_span_lines`），并附 `sha256`。

### 5.1 真卷 GT（本机实测，已核验）

149 页：原件非空文本行 **6,039** → 副本 **3,804**，水印行 **2,235**（15 行/页）；**逐页** `copy_lines == orig_lines − wm_lines` 全部成立（mismatch **0**）；副本总字符 **47,253**（原件 90,475）；**16 页**副本正文为 0（原件本来也没有正文层）。⇒ 该 GT 是真实、可分辨的判据基线：把整页替换成空白页会得到 0 行 ≠ GT。

### AC1 canary 栅格保全与检测命中（F1，本地可判）
- **F1 已有落盘件**：`testdata/canary-issue236-vector-overlay-watermark.pdf` + `-gt.json`，由 `LexMask-workspace/scripts/issue236/gen_canary.py` 生成；`--verify-module` 实跑 `{probe_code:"hit", pages_probed:3, pages_hit:3, pages_stripped:3, copy_chars_total:0, copy_token_hits:0}`（§5.0）。单测侧用 `build_pdf(...)` 现造同形态夹具，判据同下。
- 前置（组件缺席必红）：`stats()["probe_calls"]==1`、`copy_created==1`、`code=hit`。
- 判据：① 摘后每页文本 **0 字符**（摘前 492）；② 掩码 oracle（定义见 5.2）：掩码外 `max|Δ|=0`，掩码内差异像素 / 总差异像素 = **1.0**，差异像素占比 in **[16%, 18%]**（锚定 17.083%）；③ 页数、页尺寸、`/XObject` 图像流解压字节数前后相同；④ 原件 sha256 前后一致；⑤ 掩码非空且面积占比 **> 1%**。
- 红性：摘错流 ⇒ ②（掩码外出现整页差异）③（图像流缺失）FAIL；不摘 ⇒ ① FAIL。

### 5.2 像素掩码 oracle（AC1②/AC2-2d/AC7④ 共同口径，已实测）
掩码 = 把**被摘掉的那条流单独渲染**（临时文档 `/Contents=[B]`，150 dpi，`alpha=False`）→ 灰度 `min(channel) < 254` → 二值膨胀半径 **3 px**。实测：真卷页 1 掩码占比 **4.48%**、canary 页 1 **32.25%**；两者「掩码外差异像素数」均 **= 0**、「掩码内差异/掩码面积」**0.512 / 0.530**。
- 判据必须同时满足：掩码面积 > 1%（防空掩码恒真）、掩码外 `max|Δ| = 0`、**掩码内差异面积 / 掩码面积 ≥ 0.45**（证明掩码确实被清掉，不只看「差异都在掩码内」）。
- 注：canary 掩码密度（32%）远高于真卷（4.5%）是夹具排版差异（密排 30 份、边缘裁切），**两套夹具各自用各自的锚定区间，不互相外推**。

### AC2 渲染面净化与零残留（真卷 R1，云实例）
同一云实例、同一参数跑整卷识别（`WATERMARK_OVERLAY_STRIP_ENABLED` OFF / ON）：
- 2a 机制：ON 轮页级 `pipeline_status.watermark_overlay` 满足 `code=hit`、`source=copy`、`duration_ms>0`、`pages_probed ≥ 3`、`pages_hit == pages_probed`（R1 全页平铺，5/5）；OFF 轮 `code=disabled`、`source=original`。
- 2b 零残留：GT 令牌 = 完整水印串 `钦州市院 林映峰14501201410363675`（空白归一后子串匹配）。ON 轮 OCR 文本与识别框文本 GT 命中 **= 0**；OFF 轮 **> 0**（现场记录基线）；NER 实体不含 GT。
- 2c 正文没掉（GT 行对照，非 ∅≡∅）：副本页文本非空行集合（空白归一）**逐页等于 GT `copy_lines`**（§5.1：共 3,804 行，其中 16 页为 0 行）；**不允许整页空白替换**（页副本行数=0 但 GT>0 ⇒ FAIL）。
- 2d 像素机制：页 1（dpi=150，PNG）：掩码口径同 5.2 ⇒ 掩码外 `max|Δ|=0`、掩码内差异/掩码面积 ≥0.45、差异占比 in **[2.0%, 2.6%]**（锚定 2.30%）；掩码面积 ≈4.48%。
- 2e 文本层线路不变（口径订正，见 §7.4）：文档级 `pipeline_status.text_layer_doc.fallback_reason_code == "repeated_fragment_coverage"`、`fallback_reason` 含 coverage ∈ **[0.36, 0.39]**（本机实测 0.3764，历史记录 0.3827，同一区间）、`run_document_ner` 建链 **= 0**；ON/OFF 两轮该块逐字相同。
- 2f 一般不变式：文本层入口 `text_layer_doc_source._build` 收到的 `file_path` **始终**是原件（单测覆盖；本 Issue 不改该路由，任何卷都不允许收副本）。

### AC3 命中卷原件保护（R1，与 2a 同轮）
- 前置：`copy_created≥1`、`source=copy`（否则 FAIL，不允许空转）。
- 判据：① 原件 sha256/`mtime_ns`/`size` 一致；② 副本每页 `/Contents` **1** 条流（原件 2）、副本 GT 令牌命中 **0**、`st_dev+st_ino` 不同、内容 sha256 不同；③ 副本页文本行集合 = GT `copy_lines`（同 2c）。

### AC4 误杀红线：暖负样本（W1–W4，本地可判）
- 判据：W1–W4 全部 `code ∈ {no_wm_spans, no_isolated_overlay}`、`source=original`、`stats()["build_calls"]` 增量 **0**、`copy_created==0`、副本目录零新增、原件 sha256 不变。
- 红性归属（明确分工）：**W1** 杀「只按重复文字判分」（水印并入主流，重复判分必误摘）；**W2** 杀「只要存在独立流就摘」（合法不可见正文字层也是独立流）；W3 杀「不检查绘制 op」；W4 杀「只看页面上有水印像素」。
- 云侧（同批）：W1 关闭态与开启态识别框/文本集合逐页一致（千分位/空格归一）。

### AC5 预览面与交付面（默认不设环境变量）
- 5a〔判据，2026-11-04 二次裁定后改写〕上传后的预览/标注视图**默认就是净化视图**：`api/files.py` 端点返回的 PNG 与「净化副本 150 dpi 渲染」像素相等、与「原件渲染」在水印笔画带上显著不同；`raw=true` 时退回原件视图（诊断逃生口）。接线钉 `test_page_image_endpoint_defaults_to_clean_view` 防回退。
- 5b〔守卫〕交付默认（**不设** `DELIVERY_KEEP_WATERMARK`，验默认值本身）：交付 PDF **水印 GT 命中 = 0**（水印区像素与净化渲染一致、与「保留支路」在水印区显著不同）；反向支路（显式 `=true`）恢复保留；两种支路下 ZIP 内都不含副本文件。
- 5c 交付反向支路：显式 `DELIVERY_KEEP_WATERMARK=false`：交付 PDF GT **= 0**、每页 `/Contents` **1** 条流、页数=原件；正文保全 = 交付 PDF 每页 150 dpi 渲染 vs 副本渲染：掩码外 `max|Δ| ≤ 8`（JPEG 容差）、**差异面积 in [0.5%, 6%]**（两侧页均非空白，不允许 0 对 0）。
- **5d〔判据〕预览与交付同源**（Issue#236 二次裁定后补）：`preview_redaction` 的底图在水印笔画带上「默认支路 Δ>5 灰阶（已去水印）、保留支路 Δ<3 灰阶（仍带水印）」，两态都必须钉住——否则「预览有水印、下载没有」这类口径错位无人发现。用例 `test_previewed_result_matches_delivery_watermark_policy`。
- 红性：默认值漏传到预览/交付 ⇒ 5a/5b FAIL；只实现默认支路 ⇒ 5c FAIL；预览固定吃原件 ⇒ 5d FAIL。

### AC6 未命中卷零差异（C1–C3，本地可判）
- 判据：① 原件 sha256 一致；② `stats()["probe_calls"]` 增量 **1**、`duration_ms>0`、`pages_probed ≥ 3`（检测确实执行）；③ `stats()["build_calls"]` 增量 **0**、`copy_created==0`；④ `code ∈ {no_wm_spans, no_isolated_overlay}`、`source=original`；⑤ 副本目录零新增；⑥（云）识别框/文本/实体集合与 OFF 逐页一致（页级失败白名单见 AC13）。

### AC7 页级记账与多流歧义（F3/F4/F5/F6/F7，本地可判）
- 7a F3（异构卷）：`code=hit`、`pages_stripped=3`、`pages_skipped=2`；无水印页副本渲染 `max|Δ|=0`（逐像素相等）；水印页按 5.2 掩码判定；全卷副本文本行集合 = GT `copy_lines`。
- 7b F7（仅 1 页有水印）：`below_min_pages`、`build_calls` 增量 0、无副本（`MIN_HIT_PAGES` 语义）。
- 7c F5（重叠残留）：逐页 `skip`（`pages_skipped=3`），副本文件不产出或产出但 GT 令牌命中 >0 ⇒ 后者 FAIL —— 判据写死：**不得产出「仍含水印令牌」的副本**。
- 7c′ 部分页残留（门⑥ C2 补强，二轮收窄为**页级**判定）：卷内只要有一页**本身带覆盖水印**且该页没能被干净摘掉（字符闸/残余令牌/结构异常导致该页无候选成功），**整卷熔断**：`code=guard_refused_pages`、`source=original`、`pages_guard_refused≥1`、无副本文件。注意：本来没有水印的页（异构卷正文页）只记 `pages_skipped`，**不触发**熔断；同一页试了多个候选流全拒也只算 1 页。理由：产出「大部分页干净、个别页还带水印」的副本会让下游 OCR/NER 继续吃水印，而状态面却是绿的。
- 7d F4（双水印流）：`no_isolated_overlay`，不产出副本（候选不唯一必须弃权，不许「都摘」）。
- 7e F6（双合格流）：只摘 B_wm；副本页文本 = GT 正文（B_body 全保留），水印令牌 0。

### AC8 入口矩阵（防「某入口没接线」）
命中卷逐入口断言渲染源为副本：① 批量整卷识别（`task_queue_pipelines.run` → `vision_detect`）；② playground 单页 `POST /redaction/{file_id}/vision`；③ VL-MD 渲染线（`VlMdPipelineService` 的 `get_pdf_page_image` 段）；④ reject 后重识别；⑤ 队列重试；⑥ 预览（必须原件）。
- 证据：①–⑤ 页级 `pipeline_status.watermark_overlay.source == "copy"`（或等价机读记录）；⑥ 由 5a 像素断言承担（预览端点不写页级状态时以像素为准）。
- 显式排除：**agent_md（供料 MinerU 段，发送原件字节）本 Issue 不接线**，不计入红性（§6.2）。VL-MD 的取图渲染段与 MinerU 解析段是两条路径，前者接副本、后者不动。

### AC9 副本治理（本地可判）
- 前置：`copy_created ≥ 1` 且副本在 `<OUTPUT_DIR>/.watermark_overlay/` 下、mtime 属本次。
- 9a 结构：副本 `fitz.open` 重开、页数/逐页 `rect` 与原件一致。
- 9b 权限：目录 0700、文件 0600。
- 9c TTL：旧副本 mtime 调早 + `COPY_TTL_HOURS` 调小 ⇒ 下次构建时被**实际删除**；新鲜副本不删；**进程级** `sweep_expired()` 单独可判（不依赖下次构建）。
- 9d 并发单飞：`BATCH_RECOGNITION_PAGE_CONCURRENCY>1` 同卷并发 ⇒ `build_calls` 增量 **1**（多线程本地复测）。
- 9e 不泄漏：不进交付 ZIP（5b）、不被预览直出（5a）。
- 9f 原子性与回收：构建失败/异常后**不留下半成品文件**（临时名 + `os.replace`，断言目录内只有合法副本）；任务 reject/删除后副本在 TTL 内被清；TTL 清理与在飞渲染不互踩（在飞文件跳过删除）。
- 9g memo：同卷第二次渲染 < 5 ms 且两计数器不增。

### AC10 异常输入 fail-open（阶段级 vs 任务级，本地 + 云）
判据分两层：**阶段级**必须显式失败并给枚举错误码；**任务级**行为必须与「未接本组件」的基线一致（不能因预处理把原本能跑的任务弄失败，也不能吞掉原本就失败的解析错误）。
- 用例 → 错误码一一映射（钉死，不接受任意值）：
| 输入 | 期望 `error_code` | 期望任务级行为 |
|---|---|---|
| 损坏/截断 PDF | `error`（内容根本读不出 PDF 时报文归 `not_pdf`） | 与基线一致（基线本来就失败的识别失败照旧；水印阶段 `failed=true`） |
| 加密 PDF | `encrypted`（实现码；`failed=false`，属显式跳过） | 同上（基线 `open_pdf_checked` 拒绝/走既有加密路径） |
| 0 页 PDF | `not_applicable` | 同上 |
| 非 PDF（docx/图片） | `not_pdf` | 同上（该文件本就不走 PDF 路径） |
| 超大卷（> `MAX_FILE_MB`） | `capped_by_size` | 照常识别（原件） |
| 副本目录不可写 | `copy_dir_unwritable` | 照常识别（原件），任务**成功** |
| 落盘自检失败（注入：让 `save` 后自检读到页数不符，或直接替换 `xref` 断页数） | `copy_verify_failed`（`failed=true`、`source=original`） | 照常识别（原件），任务**成功** |
- 码名口径（门⑥ I1 对齐实现）：没有独立 `error_code` 字段，统一读 `code`；`error` 是阶段级异常的统一码（异常类型见 `error` 字段），`not_pdf`（后缀不是 .pdf 或内容不可读）/`not_applicable`（0 页）/`encrypted`/`copy_dir_unwritable`/`copy_verify_failed`/`guard_refused_pages`/`no_strippable_pages` 为实现直接产出的码。
- 通用判据：`source=original`、原件 sha256 不变、`failed` 与「真失败」一致（后两行 + `capped_by_size`/`encrypted` 为 `failed=false` 的显式跳过），且码属上表枚举；有效 PDF+可恢复故障（后两行）要求任务级 `COMPLETED` 且 `recognition.failed_pages` 不含该页；解析类故障要求任务级与基线逐项相同（用 OFF 轮同输入复跑做基线对照）。
- 红性：异常上抛 ⇒ 后两行任务级 FAIL；错误码写死/伪标（如一律 `not_applicable`）⇒ 表驱动映射 FAIL。

- 显式边界（门⑥ B3）：页数 < `MIN_HIT_PAGES`(3) 的卷（含单页/两页卷）一律 `below_min_pages` 弃权——卷级抽样检测的样本不足，宁可不摘。该边界已由 `test_two_overlay_pages_below_threshold_is_not_stripped` 与 F7 双侧钉住。

### AC11 性能与「一次/卷」（云实例）
- 判据：① 首次命中该卷 `duration_ms` < **1000 ms**（锚定 681 ms，单线程、warm page cache）；② `probe_calls`/`build_calls` 各增量 **1**、`pages_probed ≥ 3`、`pages_hit == pages_probed`（R1）；③ 同卷第二次渲染 `duration_ms ≤ 5 ms` 且两计数器不增。
- 红性：单边「<1s」⇒ 必须与 ②③ 联合；无 memo/单飞 ⇒ 149×681 ms ⇒ FAIL。

### AC12 变异验证（12/12 KILLED，每条落到具体判据）
| # | 需求级违例（突变体） | 由哪条判据杀 |
|---|---|---|
| m1 | 用「流内重复文字 ≥ N」判水印（不做差分 oracle） | AC4-W1 |
| m2 | 只要求「水印文字消失」，不要求「正文行不变」 | AC2-2c（同 F2）、AC7-7e |
| m3 | 只要存在合格流就摘（同页多条时全摘） | AC7-7e（F6）、AC4-W2 |
| m4 | 单页命中即认定整卷 | AC7-7b（F7）、AC6 |
| m5 | 只做卷级判定、逐页不做保真校验/记账 | AC7-7a（F3②③）、AC7-7c |
| m6 | 摘后不做残余检查 | AC7-7c（F5） |
| m7 | 预览/识别共用取图变体（`allow_strip` 不生效） | AC5a |
| m8 | 无 memo/卷级锁 | AC11②③、AC9-9d |
| m9 | 在原件上摘除（写回原件） | AC3① |
| m10 | 交付开关只实现默认支路 | AC5c |
- 判据：12/12 KILLED（M1–M12），且每个变异必须在**本条对应判据**上红（不得靠无关用例）。

### AC13 环境口径表
| 验收内容 | 环境 | 备注 |
|---|---|---|
| AC1、AC4（本地部分）、AC6①–⑤、AC7、AC9、AC10（阶段级 + 可恢复两行）、AC12 | **本地 compose / pytest** | 纯 PyMuPDF CPU |
| AC2、AC3、AC5a/5b/5c、AC6⑥、AC8、AC10（解析类任务级基线对照）、AC11、AC4 云侧一致性与 W1 关闭态对照 | **云 DCU 实例（GPU 栈）** | 需识别/OCR/NER |
| 页级失败白名单 | 云 | #199 后基线 **148/149 completed**（页 133 显式 timeout）；AC2/AC6⑥/AC8/AC11 比「逐页一致」时写入白名单，跑前先记 OFF 基线 |
| 性能计时（AC11） | 云 | 同进程、单线程、warm cache |

## 6. 风险与边界

### 6.1 风险与缓解
| 风险 | 缓解 |
|---|---|
| 误删非水印流 | 差分 oracle「非水印 span 逐字段相等」+ 候选唯一 + 签名一致 + 暖负样本判据（AC4）+ 正文 span 正向对照（AC2-2c） |
| 残余水印（同页双水印流） | probe 要求「摘后无水印 span」；build 残余令牌守卫；`pages_skipped>0` 显式（AC7） |
| 副本泄漏（同含 PII） | 0700/0600、TTL、只在 OUTPUT_DIR 下、不进导出与预览（AC5/AC9） |
| 副本拖慢识别 | 每卷一次、卷级锁、体积闸、worker 线程内执行（AC11/AC9-9d） |
| 缓存互相污染（clean/raw） | 页图缓存键携带变体（AC5a 钉住） |
| 与 #206 冲突 | 检测器不依赖 `visible_text_watermark.py`（preview 无该模块）；本 Issue 管「独立流」，#206 管「同流 OP 级剥离」 |

### 6.2 诚实边界
- 只覆盖「水印是独立内容流且流内无绘制 op」这一族；同流内嵌（族 B）不做（#206 阶段 1 范畴）。
- 异构卷：卷级 probe 若探测页恰好都没水印 ⇒ `below_min_pages`（不处理），不做页级穷举探测。
- `agent_md` 供料模式把**原件字节**送 MinerU：其水印误识别本 Issue 不解决，登记后续（不在 AC8 红性内）。
- 交付件水印口径（**2026-11-04 用户裁定，二次变更**）：交付件**同样去水印** —— 交付 PDF 的整页图从净化副本光栅化，水印不进交付物；置 `WATERMARK_OVERLAY_DELIVERY_KEEP_WATERMARK=true` 可退回「交付保留水印」。裁定理由（用户明示 + 实测）：水印串里带**真实姓名与身份证号**（`林映峰` / `14501201410363675`），保留在交付件等于把源院防泄漏水印里的 PII 原样交出去，而该处文字因「检测跑在净化副本上」不会有任何打码框覆盖 —— 是实打实的泄漏面，不是格式偏好。

---

## 7. 拍板结论（2026-11-04 用户确认）

1. **交付物水印默认保留** ✅（`WATERMARK_OVERLAY_DELIVERY_KEEP_WATERMARK=true`）；反向支路已实现，随时可切。
2. **净化副本只供渲染** ✅（文本层/docline/闸/预览一律原件）——§2.2 的 0.3764 vs 0.0407 实测钉死方向。
3. **识别面总开关默认开** ✅（`WATERMARK_OVERLAY_STRIP_ENABLED=true`，fail-open + 逐页校验 + 只影响渲染取图）；由 `test_shipped_switch_defaults_follow_decision` 钉住出厂值。
4. **门④ 顺序** ✅：默认值定案后即上云做整卷 E2E（真卷 R1 + canary + 变异 12/12 + 入口矩阵）。
4. **Issue 正文草案 AC#2 的口径修订**：草案写「摘除后 #203 coverage 闸对该卷触发 0 次」，但实测原件 coverage **0.3764**、闸本来就拒绝该卷（建链 0 次），副本才是 0.0407。因此本方案把 AC 改为「**闸行为不变**：仍拒绝该卷、docline 建链 0 次、ON/OFF 两轮一致」。**这是对 Issue 验收口径的反向修订，需要你确认**（我倾向修订，因为「闸 0 触发」只能靠把副本喂文本层达成，而那会触发整卷 300 s 清零）。

---

## 8. 门②/③ 实施进度（v2.2 设计已落地，2026-11-04）

### 8.1 落库文件

| 文件 | 状态 |
|---|---|
| `backend/app/services/watermark_overlay.py` | 新增 570 行：`probe()` / `build_stripped_copy()` / `render_source()` / `status_for()` / `stats()` / TTL sweep / 卷级 memo+单飞 |
| `backend/app/core/config.py` | 11 键 + 6 validator（`WATERMARK_OVERLAY_*`） |
| `.env.example` | 11 键登记（含默认关与边界注释） |
| `backend/app/services/file_parser.py` | `get_pdf_page_image(..., allow_strip=True)`、clean/raw 变体缓存键、6 元组键类型同步 |
| `backend/app/services/vision_service.py` | `pipeline_status["watermark_overlay"]`、预览 `allow_strip=False`、`_delivery_source()` + `_redact_pdf(source_path=)` |
| `backend/app/api/files.py` | 预览/标注端点默认净化视图 + `raw=true` 逃生口 |
| `backend/tests/test_watermark_overlay_strip.py` | 新增 36 例（合成夹具手写内容流，零 testdata 依赖；含 F9/F10 两条变异可红夹具、幂等/未摘页流逐字节不变+未摘页像素零差/日志零 PII/加密卷显式跳过/非水印区逐像素不变/并发单副本/副本目录不可写 fail-open/部分页残留整卷熔断/水平流不误杀/阈值 N−1/pipeline_status 标签 十一条门⑥ 补强判据） |
| `CHANGELOG.md` | 本轮登记 |

### 8.2 实现相对设计的偏离（均已在本轮固化）

1. **探针码语义收敛**：`hits < MIN_HIT_PAGES` 时，`hits>0` → `below_min_pages`（异构卷/稀疏命中）；`hits==0` → `no_isolated_overlay`（有覆盖 span 但无唯一可摘流）或 `no_wm_spans`（冷负样本）。v2.2 原文只写 `below_min_pages`，实测发现「零命中但不可分离」（W1）也落该码会把两种成因混为一谈。
2. **新增构建码 `no_strippable_pages`**：逐页守卫全弃权时必须**不产出副本**（AC7-7c）；`_build_once` 映射为 `failed=False / source=original / error=None`，与真失败（`error:Type`）区分。
3. **计数器 `stats()`**：`{probe_calls, build_calls, render_calls, render_ms}` 进程级；AC1/AC4/AC6/AC11 的「跨调用行为证据」用它，不靠单次返回状态自证。
4. **`probe(max_file_mb)` 改 float**：便于用极小阈值做体积闸红测（`int` 无法表达 1e-6）。
5. **夹具改为手写内容流**（不用 `TextWriter` 临时页搬运）：`TextWriter` 生成的 ExtGState 缺 `/Type /ExtGState`，搬进新资源字典后 MuPDF 会报 `cannot find ExtGState resource`；手写 `q … cm BT … Tj ET Q` 与族 A 卷形态更一致（实测 `dir=(0.707, 0.707)` 需要矩阵 `0.70711 -0.70711 0.70711 0.70711`，即 `Matrix(-45)` 等价）。**私有生成器同步注意：canary 的水印旋转方向要与真卷一致（+45° 视觉方向 ⇒ 该矩阵）。**

### 8.3 本地已跑通的判据（门③ 本地可判部分）

自动化：`pytest backend/tests/test_watermark_overlay_strip.py` → **51 passed / 5.2s**（head `188a601` + 增量处置），`ruff check` 干净。覆盖 AC1（命中+栅格保全+原件 sha）、AC4（W1 并入主流 / 合法不可见文字层 / 矢量表格三支暖负样本）、AC5（原件视图端点仍吃原件 / **打码结果预览与交付同源（5d 两态）** / 交付默认**去水印** / 交付保留反向支路，像素掩码相对判据）、AC6（冷负样本零文件零构建 + 检测执行证据）、AC7（F4 双流歧义 / F5 残余弃权且不出副本 / F6 合法流不被摘 / F7 单页命中弃权 / F3 异构卷逐页记账 / **F9 合法 ASCII 正文流被字符闸挡住** / **F10 水印与绘制 op 同流判不可摘**）、AC9（0700/0600、页数与 rect、TTL 实清、关闭不探测）、AC10（损坏 / 超限 / 自检失败 fail-open 且不留半成品）、AC11（memo：探测 1 次、构建 1 次、二次调用 0 增量）。

**AC12 变异验证：12/12 KILLED**（M1–M12；每条变异都让具体用例转红；变异脚本为一次性临时件、不入库）：

| # | 变异（唯一改动） | 转红用例 |
|---|---|---|
| M1 | `_is_overlay_span` 恒 `False`（指纹判据失效） | `test_build_strips_watermark_keeps_raster_and_original` 等 17 例 |
| M2 | op 白名单整体失效（`bad = False`） | `test_painted_stream_with_watermark_is_never_stripped`（F10） |
| M3 | 残余水印令牌守卫失效 | `test_residual_watermark_in_body_textlayer_causes_skip` |
| M4 | 逐页字符闸失效（删什么都放过） | `test_legal_ascii_body_stream_survives_charset_guard`（F9） |
| M5 | `min_hit_pages` 门槛失效 | `test_double_watermark_streams_are_ambiguous` / `test_single_overlay_page_is_below_min_pages` / `test_negative_volume_runs_probe_but_never_builds` |
| M6 | 构建为 0 页时不熔断（仍落副本） | `test_residual_watermark_in_body_textlayer_causes_skip` |
| M7 | 关闭开关仍探测/构建 | `test_disabled_switch_does_not_probe` |
| M8 | 构建错误不落 `failed`（静默回落） | `test_copy_verify_failure_falls_back_to_original` |
| M9 | 交付开关失效（总用副本） | `test_delivery_keeps_watermark_by_default` |
| M10 | 副本 TTL 清理失效（`_cached_copy_alive` 的 TTL 校验去掉） | `test_copy_ttl_sweeps_stale_files` |
| M11 | `sweep_expired` 内 `_sweep` 掏空（恒删 0） | `test_sweep_expired_clears_stale_orphans` |
| M12 | `_cleanup_tick` 里删/注释 `sweep_expired()` 调用 | `test_cleanup_tick_calls_sweep_expired`（spy，实测红） |

变异过程暴露的两条实现事实（都不是 bug，写清以免后人误读）：
- `_PAINT_OPS` 属于**第二道冗余防线**：绘制类 op 本来就不在 `_SAFE_OPS` 白名单里，所以只删「`op in _PAINT_OPS`」这半条判据不会让任何用例转红（等价变异）；真正承重的是整条白名单，故 M2 定为「白名单整体失效」。
- 字符闸（`removed` 的行必须全由水印字符组成）此前**没有任何用例单独依赖它**（残余令牌守卫会把同场景兜住）；F9 用「探测页有水印、正文页只有 ASCII 正文流」的异构卷把它单独钉住。

### 8.4 门⑥ 独立评审两轮处置（2026-11-04）

**第一轮（范围 `origin/preview...8f30da0`，判定 REVISE）**：C1 副本目录准备在 try 之外（OUTPUT_DIR 只读 ⇒ 连无水印卷也失败）/ C2 部分页残留仍产副本且 `failed=false` / C3 memo 命中不校验副本存活、`_sweep` 删在飞副本 ⇒ 已按「页级熔断 + 生命周期重写（存活校验、在飞豁免、TTL 生效）」处置。

**第二轮（范围 `8f30da0..48936a9`，判定 REVISE，1 阻断）**：

| 项 | 问题 | 处置 |
|---|---|---|
| **C2（阻断）** | `guard_refused` 按「候选流守卫尝试」计数 ⇒ 异构卷里被正确拒绝的**正文页**（F9 的 ASCII 正文流）也触发整卷熔断，与 AC7-7a（异构卷应 `hit` + `pages_stripped=3`）冲突 | 改为**页级**判定：`page_has_wm` 为真且该页最终无候选摘成功才计 `pages_guard_refused`；无水印页只记 `pages_skipped`。F9 恢复 AC7-7a 语义（产出副本、正文保留），熔断只覆盖「本页真有水印却摘不净」 |
| 1 | `copy_verify_failed` 的 `failed` 为 False，与文档/测试矛盾 | `failed = code in {"error", "copy_verify_failed"}`，并加断言用例 |
| 2 | `not_pdf`/0 页 `not_applicable` 等码零测试（码回退不红） | 新增 `test_non_pdf_input_maps_to_not_pdf`、`test_zero_page_pdf_maps_to_not_applicable`、`test_corrupt_pdf_maps_to_not_pdf_and_fails_open` |
| 3 | `pipeline_status` 标签只测纯函数，未钉接线 | 新增 `test_pipeline_status_initial_label_matches_helper`（`inspect.getsource` 接线钉 + 初值一致） |
| 4 | 无「不透明水印」负样本（删 alpha 上限不红） | 夹具加 `opacity=1.0` ⇒ `test_opaque_overlay_is_not_treated_as_watermark`（`no_wm_spans`、零构建） |
| 5 | `copy_dir()` 仍在 probe 之前（只读 OUTPUT_DIR 时无水印卷报 `copy_dir_unwritable`、probe 从不跑） | `_build_once` 改为 **probe 命中后**才准备目录 + sweep |
| 6 | TTL 对 memo 不生效（靠 `_CACHE.clear()` 掩盖） | `_cached_copy_alive()` 纳入 TTL 校验，`_sweep` 的在用白名单改用它；TTL 用例去掉 `clear()`，改为「过期被清 + 再请求重建」 |
| 7 | fail-open 异常不入负缓存（每页重付探测）、日志丢 message | 新增 `_remember_failure()` 短窗口（60 s）负缓存 + 日志带脱敏消息 |
| 8 | `no_strippable_pages` 判定先于熔断，码序与文档不符 | 熔断块前移（先 `guard_refused_pages` 再 `no_strippable_pages`） |
| 9 | 文档残留（码表/独立 error_code/加密与 not_applicable 冲突/自检失败 `failed` 口径） | §3.1/§3.2/§3.4/§3.5/§3.6/AC7-7c′/AC10 全部对齐实现 |
| 10 | 只读目录用例依赖非 root（容器内假红）；插槽按命中顺序索引页号会错配 | 用例加 `os.geteuid()==0` 跳过；`slot_by_page` 改按页号记录（新增 `test_probe_reports_slot_per_page`） |

局部工具输出（原文）：`43 passed in 3.27s`（含新增三例后）、`ruff check` 三文件全绿。

**增量短审（范围 `da396f3..188a601`，判定 APPROVE with fixes）**：

| 项 | 问题 | 处置 |
|---|---|---|
| **I1** | 进程级清理的「接线钉」用 `inspect.getsource` 数字面量 ⇒ 注释掉/挪进死代码仍绿（实测 MUT1/MUT2 绿、MUT3 整行删除才红） | 抽 `_cleanup_tick()` 同步函数，改 **monkeypatch spy** 运行时判据 `test_cleanup_tick_calls_sweep_expired`（删/注释调用即红）；另加 AST 结构钉 `test_periodic_cleanup_runs_tick_in_thread`（拿掉 `asyncio.to_thread` 即红） |
| **I2** | 文档「删任一接线即红」措辞过强；§8.4 未登记本轮处置 | 文案精确化（运行时 spy + AST 两钉分开描述）；§8.4 增本表 |
| **I3** | `COPY_TTL_HOURS` 校验器允许 0 ⇒ 实测 sweep 返回 0、副本与 memo 永不过期且无日志，AC9 被合法配置静默废掉 | 下限改 **1** + `test_copy_ttl_validator_forbids_zero` |
| **M1/M7** | 文档测试数过期、变异表缺本轮行 | 测试数改 49；变异表补 M11（`_sweep` 掏空 ⇒ 功能用例红）、M12（删 `_cleanup_tick` 内调用 ⇒ spy 钉红） |
| **M2** | `sweep_expired` docstring 重复字 + `float()` 在 try 外（实测 `ttl_hours="abc"` 抛 ValueError） | 文案订正；取值转换纳入 try，非法值返回 0 |
| **M3** | 经 `copy_dir()` 无条件 `makedirs`：开关关闭时启动也会建 `OUTPUT_DIR/.watermark_overlay/` | 先 `os.path.isdir` 判存在，不存在直接返回 0 |
| **M4** | 只测显式 `ttl_hours=1.0`，settings 分支与 memo 豁免无判据 | 新增 `test_sweep_expired_keeps_memo_held_copy` |
| **M5** | 返回值 0 的三义（关闭/不可读/无过期）静默 | `logger.debug("… dir=… ttl=… removed=…")` |
| **M6** | 同步阻塞清扫跑在事件循环上 | 统一 `await asyncio.to_thread(_cleanup_tick)` |
| **行尾噪声** | 首版把 `backend/app/main.py` 整文件 CRLF→LF（757/744 行纯噪声，淹没真实 hunk） | 恢复 CRLF，使 `git diff da396f3 -- app/main.py` 只剩 **24+/8-** |

局部工具输出（原文）：`49 passed in 4.76s`、`ruff check` 三文件全绿；两处变异实测：注释 `_cleanup_tick` 内调用 ⇒ `test_cleanup_tick_calls_sweep_expired` 红；把 `await asyncio.to_thread(_cleanup_tick)` 改成同步直调 ⇒ `test_periodic_cleanup_runs_tick_in_thread` 红。

## 9. 门④ 端到端实测（2026-11-04，本地 docker 栈整卷）

环境口径：云实例不可用（`scnet-main` 连接被拒、`neo001-lan-wsl` 无路由），门④ 在**本机 docker compose 栈**（项目 `lexmask`，含 ocr/ner/frontend/backend/mineru，全部 healthy）执行；backend 镜像按本分支源码重建（`DOCKER_BUILDKIT=0`，buildx 在本机有 activity 目录权限问题）。逐条对应 §5.1 环境口径表：CPU 侧预处理（探针/摘除）本机 docker 可判；OCR/NER 走容器内既有服务（DCU 侧栈非本次改动，不涉及 GPU 栈差异）。

### 9.1 真卷（第四卷.pdf，149 页，族 A）

| 判据 | 实测 | 结论 |
|---|---|---|
| 上传未改原件 | 容器内 `uploads/44d544cc….pdf` sha256 = `336e449a…21fc`，与本地原件逐字节相同 | PASS（AC3） |
| 识别任务终态 | job `af6d8bb4…` → `awaiting_review`，`page_count=149`，`failed_pages=None` | PASS（AC9 任务级） |
| 每页阶段状态 | 149/149 页 `pipeline_status.watermark_overlay = {code:"hit", source:"copy", pages_probed:5, pages_hit:5, pages_stripped:149, pages_skipped:0, duration_ms:660}` | PASS（AC1/AC2） |
| 副本落盘 | `outputs/.watermark_overlay/f0c35717….pdf`，25,798,037 B，`0600`，目录 `0700`，sha256 ≠ 原件 | PASS（AC1/AC9） |
| 副本文本 | 原件 90,624 字符 → 副本 47,253；水印令牌「林映峰」1,788 次 → **0**；空白页 16；页 132 正文 1,151 → 860 字符 | PASS（AC2-2c） |
| OCR/NER 残留 | `bounding_boxes` 149 页共 3,499 框；`林映峰`/`14501201410363675`/`钦州市院` 命中 **0**；`钦州` 61 次全为正文（钦州港海关等） | PASS（AC2-2b） |
| #203 闸行为不变 | 149/149 页 `text_layer_doc.fallback_reason_code = "repeated_fragment_coverage"`（coverage 0.376 > 0.15）⇒ 建链仍弃用、走逐页 OCR，与今日路由一致 | PASS（AC2-d） |
| 预览面原件视图 | `GET /files/{id}/page-image?page=1` → 4,493,427 B，与容器内原件 150dpi render 同尺寸；净化副本 render 为 4,162,084 B | PASS（AC5a） |
| 卷级性能 | 探针+构建合计 **660 ms**（149 页），远低于 AC11 的 1s | PASS（AC11） |
| 未命中卷 | 上传 `文本型-叶辉审查报告.pdf`（78 页）→ `page-image` 200 / 240,682 B；副本目录文件数处理前后 **1 → 1**（未新建） | PASS（AC6） |

### 9.2 回归

- 分支全量 `pytest tests/ -q -p no:randomly`：**18 failed / 2,681 passed / 54 skipped**；基线（未改动 `LexMask/backend`，preview `3dba572`）：**18 failed / 2,673 passed**。
- 失败集合逐条 diff：**branch-only 0 / baseline-only 0**——全部为容器 `/dev/shm` 受限导致的 `multiprocessing/synchronize.py:57 PermissionError`（既有环境性失败）；基线多出的 13 例来自主检出上未提交的 `tests/test_issue238_progress_reporting.py`。⇒ **零回归**。

### 9.3 诚实边界（云实例恢复后仍需补的项）

1. 本次门④ 在**本机 docker 栈**完成，不是 DCU 云实例；`backend` 镜像按分支源码重建，OCR/NER 服务沿用既有镜像（本次未改其代码）。云实例恢复后建议按 §5.1 表重跑一遍整卷，作为「云侧同源」证据。
2. `stats()` 是**进程内**计数器，`docker exec` 起的是新进程，读到 0 属正常；本次以 `pipeline_status` 与副本文件为判据。
3. 预览端点与识别共用 `FileParser` 实例的页图缓存，clean/raw 变体键已隔离；本次以字节尺寸对照取证（未做逐像素 diff，因为预览请求与渲染请求是两条 HTTP 路径，jpg/png 编码不同）。
4. 交付面（ZIP/PDF）本次只做了自动化像素判据（默认保留）；真卷「交付件仍带水印」未做人工目检，留待门⑤ 验收。

### 9.4 最终 head 重跑（`5bcc882`，门⑥ 处置后）

门⑥ 二段评审期间分支连动三次（`b2ab1f6` → `8f30da0` → `5bcc882`），按评审要求**在最终 head 上重跑门④**，结论与 §9.1 一致：

- 新 file_id `813ad917-4f60-404c-962f-a4273eb1c3e1`（上传 sha256 `336e449a…21fc`，与本地原件逐字节相同 ⇒ 原件未被改动）。
- 149/149 页 `pipeline_status.watermark_overlay = {code:"hit", source:"copy", pages_probed:5, pages_hit:5, pages_stripped:149, pages_skipped:0, pages_guard_refused:0, duration_ms:662}`（新增字段 `pages_guard_refused` 亦为 0）。
- 副本 `outputs/.watermark_overlay/2b7175de….pdf`：25,798,037 B、`0600`、目录 `0700`；原件 90,624 字符 → 副本 47,253；「林映峰」1,788 → **0**。
- `bounding_boxes` 149 页 **3,506** 框；`林映峰`/`14501201410363675`/`钦州市院` 均 **0**；`钦州` 61 次为正文。
- `text_layer_doc.fallback_reason_code` 仍为 `repeated_fragment_coverage`（coverage 0.376），闸未因本次改动松动。
- 预览 `page-image?page=1` → 4,493,427 B（原件 150dpi 视图）；未命中卷 `文本型-叶辉审查报告.pdf` → 副本目录 **2 → 2**（零新建）。

### 9.5 门⑥ 二轮处置后的最终 head 重跑（`043c471`）

二轮评审把熔断收窄为**页级**、改了码序与生命周期，属于会影响运行时行为的改动，故再次整卷重跑（本机 docker 栈，backend 镜像由本分支 `DOCKER_BUILDKIT=0 docker compose -p lexmask --profile gpu build backend` 重建，`/health` 200）：

| 判据 | 观测值 |
|---|---|
| 上传原件 | file_id `a83bab61-…`，容器内 sha256 `336e449aed87728616afb55a4a0021e7414a85f914d92f582693c4be9fe321fc` = 本地原件（逐字节相同） |
| 识别任务 | job `49b92422-…` → 终态 `awaiting_review`，149 页，`failed_pages=None` |
| 净化标签 | 149/149 页 `{code:"hit", source:"copy", pages_probed:5, pages_hit:5, pages_stripped:149, pages_skipped:0, pages_guard_refused:0, duration_ms:782}`（**页级熔断改完仍 0 页被拒，真卷不受影响**） |
| 副本 | `outputs/.watermark_overlay/9edfeb7b….pdf` 25,798,037 B、`0600`、目录 `0700`；149 页；原件 90,624 字符 → 副本 **47,253**；「林映峰」1,788 → **0**；空白页 16（与 §9.1 一致） |
| 零水印残留 | `bounding_boxes` 149 页 **3,540** 框；`林映峰`/`14501201410363675`/`钦州市院` 计数 **0**；`钦州` 61 次全部是正文（钦州港海关等） |
| #203 闸不变 | 149/149 页 `text_layer_doc.fallback_reason_code == "repeated_fragment_coverage"`（文本层入口仍吃原件、闸仍拒绝） |
| 预览面 | `page-image?page=1` → **4,493,427 B**（原件 150 dpi 视图；副本 150 dpi 渲染为 4,162,084 B） |
| 未命中卷 | `文本型-叶辉审查报告.pdf` → 预览 240,682 B；`.watermark_overlay` 文件数 **3 → 3**（零新建） |
| 交付件（当时口径=保留水印，历史记录） | `POST /redaction/execute` → `output_file_id=99cbbc71-…`，**85,278,328 B / 149 页**；页 132 水印笔画带（掩码 4.06% 面积）平均通道差：**交付 vs 原件 6.91 < 交付 vs 副本 19.91**（原件 vs 副本 16.59）⇒ 交付件更接近原件，即水印仍在。**该口径已于 2026-11-04 用户裁定反转**，见 §7 与 §9.7 |

诚实边界（同 §9.3）：本轮仍在本机 docker 栈（DCU 云实例 `ksai.scnet.cn:10451` 今日 Connection refused）；`stats()` 是进程内计数器，容器内另起进程读到 0 属正常，判定以 `pipeline_status` + 副本文件为准；交付件为 JPEG 光栅，笔画带判据用相对比较而非绝对像素等值。

### 9.6 增量短审处置后的最终 head 重跑（`cbf344c`）

本轮运行时变化只有「清理节拍 + TTL 下限」，但为保证「最终 head = E2E head」不变式，仍整卷复跑一次（同一本机 docker 栈，镜像由 `cbf344c` 重建，`/health` 200；容器内 `strip=True keep=True ttl=24`、`sweep_expired`/`_cleanup_tick` 均在）：

| 判据 | 观测值 |
|---|---|
| 上传原件 | file_id `130b615e-dbc3-4786-acc3-446321920099`；容器内 sha256 `336e449aed87728616afb55a4a0021e7414a85f914d92f582693c4be9fe321fc` = 本地原件（逐字节相同） |
| 识别任务 | job `42d7c17d-4b2e-4e1d-a831-f19a15462ce1` → 终态 `awaiting_review`，149 页，`failed=0` |
| 净化标签 | 149/149 页 `{code:"hit", source:"copy", pages_probed:5, pages_hit:5, pages_stripped:149, pages_skipped:0, pages_guard_refused:0, duration_ms:790}`（**全卷唯一状态，无一页 degraded**） |
| 副本 | `outputs/.watermark_overlay/4627f5bf….pdf` 25,798,037 B、`0600`、目录 `0700`；149 页；原件 90,624 字符 → 副本 **47,253**；「林映峰」1,788 → **0**、「14501201410363675」1,192 → **0**；空白页 16；页 132 正文 1,151 → 860 字符（正文保留） |
| 零水印残留 | `bounding_boxes` 149 页 **3,502** 框；`林映峰`/`14501201410363675`/`钦州市院` 计数 **0**；`钦州` 61 次全部是正文 |
| #203 闸不变 | 149/149 页 `text_layer_doc.fallback_reason_code == "repeated_fragment_coverage"`（`repeated_fragment_coverage:0.376`，文本层入口仍吃原件） |
| 预览面 | `page-image?page=1` → **4,493,427 B**，与容器内原件 150 dpi 渲染 PNG **字节数相同**；副本渲染 4,162,084 B ⇒ 预览走原件 |
| 未命中卷 | `文本型-叶辉审查报告.pdf` → file_id `747843df-…`，预览 240,682 B；`.watermark_overlay` 文件数 **4 → 4**（零新建） |
| 清理节拍（活体验证） | 容器内实跑 `sweep_expired()`：4 份真实副本（age 0.5/2.4/3.1/3.9 h，均 < TTL 24h）⇒ `removed=0`、一份未删；再放一个 `utime=0` 的探针文件 ⇒ `removed=1` 且探针被删、4 份真实副本不受影响 ⇒ AC9「TTL 实清 / 不误删保鲜副本」在生产数据上成立 |

诚实边界（同 §9.3/§9.5）：仍在本机 docker 栈；云实例 `ksai.scnet.cn:10451` 仍 Connection refused。

### 9.7 交付件去水印复验（2026-11-04 用户裁定后，head 未变，仅默认值翻转）

裁定把 `WATERMARK_OVERLAY_DELIVERY_KEEP_WATERMARK` 默认改为 `false`：交付 PDF 的整页图改从**净化副本**光栅化。复用同一真卷（`file_id=130b615e-…`，149 页）在重建镜像上重跑交付：

| 项 | 结果 |
|---|---|
| 交付件 | `output_file_id=4ce1e303-276e-44d2-a0c2-0d2ca725234a`，**69,507,957 B / 149 页**（`redacted_count=0`，本次只验水印，打码框为空） |
| 水印墨迹像素判定 | 以「原件 − 净化副本 > 20 灰阶」定义水印墨迹像素（每页约 15 万像素）：页 132/101/51 上 **`\|交付−净化副本\| = 1.66 / 0.97 / 2.00`**（JPEG 噪声量级），而 **`\|交付−原件\| = 35.87 / 35.95 / 35.65`** ⇒ 交付件在水印笔画处**已经没有水印**，与净化副本一致 |
| 人工目检 | `.tmp/i236/i236-deliv-check2.png`：三栏（原件水印可见 / 净化副本 / 交付件水印消失），正文与公章完整 |

边界：本次只翻转交付源，**原件仍一字节不动**、文本层/docline/#203 闸/预览一律仍吃原件（§2.2 的数据钉死不变）；把 `WATERMARK_OVERLAY_DELIVERY_KEEP_WATERMARK=true` 可随时退回「交付保留水印」。

**复核界面可见性（同一轮补）**：`pipeline_status.watermark_overlay` 在复核页此前显示为裸键 `watermark_overlay 已运行 0`（`pipelineLabel()` 里没有该键的标签，回落 `formatSourceDetail`）⇒ 用户看不出「到底摘了水印没有」。已补：标签「水印预处理」+ `code=hit/guard_refused_pages 且 source=copy` 时直接显示「**已摘除 N 页**」；其余码回落通用状态（不谎报）。新增前端用例 `frontend/src/features/batch/components/review-image-content.watermark-chip.test.ts`（4 例：命中、部分页熔断、未命中/未跑/关闭、source 非 copy 不显示）。

**识别输入 = 净化页图（直接取证）**：容器内 `FileParser().get_pdf_page_image(orig, page=132)` 默认支路返回 `code=hit / source=copy / pages_stripped=149` 的页图，与 `allow_strip=False` 原件渲染**同尺寸（2553×3642）但字节不同**；对照图 `.tmp/i236/i236-ocr-input.png`（左=原件渲染带水印，右=OCR 实际吃到的页图无水印）。

## §9.8 识别入口统一取净化源（2026-11-04 用户实测反馈后补）

**暴露的问题**：把真卷前三页单独存成试件（`.tmp/i236/第四卷-前3页.pdf`）后跑识别，`bounding_boxes` 里出现 **96 个水印文本框**（`林映峰`，按 45° 平铺位置分布），而 `pipeline_status.watermark_overlay` 却是 `not_applicable` —— 说明扫描件的「PDF 文本层识别」分支（`_detect_with_pdf_text_layer`）**直接读原件的文字层**，既没取页面图、也没解析净化副本；水印层的文字因此被当成正文实体带进识别结果。

**修法**：`VisionService.detect_with_dual_pipeline` 入口先解析一次识别源 `_recognition_source(file_path)`（开关开启且本卷命中 ⇒ 净化副本，否则原件；异常 fail-open 回原件），让**页图渲染与文本层识别共用同一个源**，并把 `watermark_overlay` 状态在入口就写进 `pipeline_status`（不再依赖「页图被渲染过」这一副作用）。回归用例：

- `test_recognition_source_is_clean_copy_when_strip_hits`：`detect_with_dual_pipeline` 跑 `PDF_SCANNED` 时，`_detect_with_pdf_text_layer` 收到的路径必须是副本、状态码 `hit/copy`。
- `test_recognition_source_falls_back_when_strip_disabled`：开关关闭 ⇒ 源=原件且 `render_calls` 不增长。

**已验证**：3 页试件重新识别后 `watermark_overlay` 三页均 `hit/copy/pages_stripped=3`；`|交付−副本|` 水印带差值 < 2 灰阶。

**已知边界（不在本 Issue 修）**：文档级文本层链（`text_layer_docline`）按设计仍吃原件，其准入靠 #203 的重复片段覆盖率闸（`coverage > 0.15` 拒）。**页数 < 5 或水印页稀疏的卷**覆盖率算不出来 ⇒ 闸放行 ⇒ docline 仍可能从原件水印层取出 `林映峰` 之类字符串并落成实体框（本次 3 页试件即此形态：96 个框全是水印位置）。修法方向是「docline 准入增加『水印预处理命中 ⇒ 强制回落逐页 OCR』一条」，但那会改变 #203 现有的拒因口径（`repeated_fragment_coverage`）并需要重跑整卷 E2E，故留作后续 Issue。

### §9.9 识别入口改造后的整卷复跑（head `5ac8e54`，本机 docker 栈）

| 判据 | 观测 |
|---|---|
| 上传 | `testdata/第四卷.pdf` → `file_id=8d39c1cc-6ea3-4fd3-abdd-9d43422d7f0b`（容器内原件 sha256 与本地一致） |
| 识别任务 | job `cc17c3c7-ad9c-441a-abc5-14d7993efa99` → `awaiting_review`，149 页，失败页 0 |
| 水印标签 | 149/149 页统一 `code=hit / source=copy / pages_stripped=149 / pages_skipped=0 / pages_guard_refused=0 / duration_ms=798` |
| 实体框 | 149 页共 **3,517** 框；`林映峰`=**0**、`14501201410363675`=**0**、`钦州市院`=**0**；`钦州`=61（全为「钦州港海关」等正文） |
| 文档级文本层 | 149 页统一 `fallback_reason_code=repeated_fragment_coverage`（原件 coverage≈0.376 ⇒ 闸仍拒，docline 未接管） |

结论：把「识别源解析」提到 `detect_with_dual_pipeline` 入口后，整卷行为与 §9.6 一致（`code=hit/source=copy`、零水印文本框、闸口径不变），并且**文本层识别分支不再回读原件水印层**（§9.8 的回归用例钉住）。

---

## §9.10 泛化扩展：双保险水印流组合试摘 + 单 BT 多 Tj 门槛修正（Issue#245，2026-11-05）

用户以真实卷验证泛化性，撞出两种旧判据弃权、但实验证明可无损摘除的新形态：

| 卷 | 形态 | 旧判据结果 | 新代码结果 |
|---|---|---|---|
| `水印卷-6号案卷-p1-10.pdf`（10 页） | 同一水印画**两遍**：`[图, 6862B 水印, 6862B 水印]`，`徐汉勇 1450120161077595` | `no_isolated_overlay`（单流差分 oracle：摘一条另一条还在 ⇒ 0 候选） | `hit`（组合 `(0,2)`）→ `stripped 10/10`、222ms |
| `62（证据材料卷51）朱海松-p1-5.pdf`（5 页） | 30°、纯黑无 `rg`、65 份水印**单个 BT 块**（130 Tm 携 65 Tj） | 白名单合格但 BT=1 < 3 误拒 | `hit`（单流 `(2,)`）→ `stripped 5/5`、66ms |

两卷新代码模块级 E2E：图像 XObject sha256 逐字节不变、非覆盖 span（生产口径 `_spans`+`_is_overlay_span`）逐字段不变、渲染差异 2.66%/水印覆盖量级、水印 PII 零残留。单层回归：canary `3/3`、第四卷 `149/149`（916ms）行为不变。

### 语义变更（门⑥ 复核点）

1. **F4（双水印流）从「弃权」改「整组摘除」**：原「候选不唯一必须弃权」只对单流候选成立；单流全败 + 唯一组合成立 ⇒ 整组摘。多于一组通过组合仍弃权（歧义 fail-closed 不变）。测试 `test_double_watermark_streams_strip_both`（含与无水印夹具的逐像素正文对照）。
2. **文字门槛从「数 BT 块 ≥3」改「文字绘制 op（Tj/TJ/'/") ≥3」**：BT 块数是形态相关信号。测试 `test_single_bt_multi_tj_watermark_stream_is_qualified`。
3. **构建侧新增三层守卫**：①探针未验证的组合必须过全套 span oracle（非覆盖 span 逐字段不变——防借组合删正文，`test_combo_removal_must_not_eat_legal_body_stream` 用混合流[倾斜水印+水平数字正文同流]钉死）；②接受前守卫：摘后不得残留覆盖 span；③escalated 判定以「子集是否为探针验证过的那个」为准，不以「页是否命中」为准（后者会把未验证组合放进快路径——实现过程中真踩过，测试转红后修正）。
4. F5 语义不变：残留水印的卷仍熔断不产出副本（残留流现在合格化后由行集守卫+接受前守卫双层拒绝）。

### 配套判据状态

- AC4/W1–W4、AC6 冷负样本、AC7-7a/7b/7e、F6 判据不变（54 例全过）。
- §5.0 夹具清单 F4 行语义随本文订正；62 卷形态对应新夹具 `wm_single_bt=True`。
- 门④ 云侧补跑承诺不变（与本 PR 主线同一口径，云恢复后按 §5.1 补）。
