# Issue#206 设计：识别前剥离可见平铺文本水印（预处理阶段，渲染面先行）

| 项 | 值 |
|---|---|
| Issue | youayou-Lee/LexMask#206（原「想法（暂不实施）」；本文档把想法变为可实施方案） |
| 基线 | `origin/preview` @ `686c4c0`（含 #203/#199 水印闸、#210 `perf(#207)`；本方案先做**阶段 2'**） |
| 分支 | `feat/issue206-word-filter`（worktree：`.worktrees/feat-issue206-word-filter`） |
| 状态 | **v1.5**（阶段 2' 已实现：判据=**文本 ∩ 几何**；①.5 冷审 REVISE 已逐条处置；独立 GT 复验 `body_missing_free=0/0/0`；门④ 双 job 已实测（纯水印框 78.3%→0%、框 −81.7%），待门⑥） |
| 关联 | #203（预建链水印闸，已合）· **#204（建链 O(整卷) 与单页门限解耦，OPEN，本方案的阶段 2 被它阻塞）** · #208（页级续跑）· #165（回落原因可观测） |
| 日期 | 2026-10-30 |

## §0 结论摘要

在**扫描件识别链路前面**加一个预处理阶段：判定该卷是否存在**可见的平铺文本水印**（本例形态：倾斜、
半透明、同字体同字号逐页平铺的灰色文本对象，盖在扫描底图之上、**不在像素里**；已确认两族：
45° / `alpha≈0.2` / `NotoSansCJKsc`，以及 −30° / `alpha≈0.3` / `STSong-Light`），命中则在
**副本**上把水印字形剥掉，让 OCR 取图与渲染看不到它。原件一字不动、产物不进交付、**不做成产品能力**
（无 UI、无接口参数）。

本方案**分两期**，因为两处消费点的风险完全不同：

- **阶段 1（本 PR 实施）：渲染面剥离。** 唯一收口点是 `file_parser.get_pdf_page_image`（全链唯一取图口），
  三个 OCR 消费点零改动、预览显式走原件。收益 = 水印上的**框噪声消失**、OCR 输入干净。
  **收益面要写准**：只有「走逐页图像 OCR 的卷」受影响 —— 族 A 这类被 #203 闸拒的卷（真卷 A 149 页）；
  而**族 B 卷今天走 docline 文本层、根本不取图**（12 卷抽样 11 卷判 `DOCLINE 会接管`），阶段 1 对它们
  没有直接影响，它们的框噪声要靠阶段 2 的建链流词过滤（§7）。
  **本阶段不主张任何提速结论**（门④ 实测：整份剥离副本送识别反而 **+14.6%**，见 §0）。
- **阶段 2（blocked-by #204）：建链面剥离。** 剥净文本层后 `repeated_fragment_coverage` 从 0.3764 降到
  0.0424，文档级链本可重新接管 130/149 页、只剩 ~19 页 OCR —— 但 `boxes_for_page` 的整卷建链仍在
  `run_page` 的 `BATCH_RECOGNITION_PAGE_TIMEOUT=300` 内跑完，剥净后约 26 块 ≈ 550s（实测 1122s/51 块按块数
  外推），让 docline 接管会在页 1 被掐死、整卷清零 —— **正是 #203 的灾难**。故阶段 2 必须等 #204 把
  「O(整卷) 的活」与「O(单页) 的门限」解耦。阶段 1 里 profile 与词过滤规则按**最终形态**定义好，阶段 2 只接线。

判据的立心之变：**不靠「串重复」识别水印**（#203 的 F13 已登记「只看 ≥10 字整词 ⇒ 5~9 字碎片维度不可见」），
而是靠**版式签名**（倾斜 + 半透明 + 同字体同 alpha 跨页平铺）。这条判据正好绕开碎片盲区，且实测反例集
（正文可见层的文本型卷、水平可见层、混合卷、印章卷、大页卷）**0 候选、0 字被剥**。

**语料验证结论（2026-10-29/30，只读，明细在私有底稿）**：

| 项 | 结果 |
|---|---|
| 判据（540 卷全扫） | 471 卷加密案卷（71,799 页）**全部命中**；61 卷明文案卷**全部不命中**；`near_miss = 0` |
| 水印族覆盖 | 2 族（45°/0.2/NotoSansCJKsc、−30°/0.3/STSong-Light）；`\|dir.y\| ≥ 0.2` 不锁角度是命中第二族的原因 |
| 剥离配方 | **按 text-showing op 擦除**（`get_texttrace()` 的 `entry.bbox`，+2pt），不是按 span 矩形 —— span 级每页留 2–3 字残水印 |
| 剥离效果 | 8 卷抽样（族 A 3 卷 + 族 B 5 卷）**残水印字符 = 0、可见字符 = 0**；目视渲染对照：水印消失、正文/手写/印章/底图无损 |
| 像素等价 | 族 A 4 页 + 族 B 3 页：水印矩形外 `changed = 0.000%`、`max\|Δ\| = 0` |
| 成本 | 族 A 149 页 1.94s / 350 页 16.86s；族 B 每页 1 条 op，20–33 页 1.0–4.0s；全语料 71,799 页外推 ≈1 小时 |

**门④ 云上 A/B 冒烟（2026-10-29，族 B 一卷 31 页，同一卷只差水印，同参数两份 job）**：

| 项 | 原件臂 | 剥离副本臂（整份副本送识别） |
|---|---|---|
| 走哪条路 | **docline 文本层**（`chunks=14 calls=28`，建链 **145.6s**，之后每页 0.28s） | **docline 弃用**（`sparse_text_layer:5.8<120`）⇒ 逐页图像 OCR |
| OCR+HaS 阶段 | 156s | **378s（2.4×）** |
| recognition 总 | 920.3s | 1054.8s（**+14.6%**） |
| 框总数 | 5,935 | **551（−90.7%）** |
| 纯水印字符框 | 4,612（77.7%） | **0** |
| 文本完整度 | 水印碎片（4 字词片段）、`20 22`、`00`、地名截断等 | 机关全称、完整起止时间串、完整 11 位手机号、完整到村组门牌的地址 |

结论三条（**都改变方案取舍**）：

1. **质量面符合预期**：水印框 −90.7%、纯水印框清零、正文串更完整（平均字长 ADDRESS 5.9→13.1、PHONE 4.6→11.0）。
2. **「整份剥离副本送识别」是错形态**：剥离副本的词表**为空**（op 级擦除把不可见正文层一起擦掉），
   于是 docline 弃用、卷子掉进逐页 OCR —— OCR 阶段 2.4×、整 job +14.6%。故**副本只能供渲染，绝不能供建链**（§6 边界 7）。
3. **族 B 的正解是建链流词级过滤**（不改渲染）：族 B 的文本层里水印就是 65 个 4 字词/页、与正文词并存，
   逐页只丢弃「文本 ⊆ 水印字符集」的词 ⇒ 流字符 −36.9%、块 14 → 约 7、建链约 73s（外推）、框 5,935 → 约 1,323（−77.7%），
   且**不依赖 #204**（是减负不是加压）。**尚未实测**（阶段 2' 的活，见 §7）。

## §1 定位、范围与硬边界

| 项 | 内容 |
|---|---|
| 做 | 仅**文本对象层**的可见平铺水印：倾斜 `\|dir.y\| ≥ 0.2`、半透明 `0 < alpha < 220`、同字体同 alpha 跨页平铺 |
| 做 | 渲染面剥离（阶段 1）；文本层词级过滤的实现与形态定稿（阶段 2 接线） |
| **不做** | 像素里烧死的水印 / 图章 / 溯源型水印（用户 2026-10-29 已定：靠识别能力，不做） |
| **不做** | 水平不透明的可见水印、docx / 图片格式 |
| **不做** | 做成产品能力：不新增 UI 开关、不新增接口参数、不改变用户可感知的交付物 |
| **不做** | 改 `is_scanned` 判定、路由、交付裁定、`file_store` 里的区域/框（剥离只影响「识别看到什么」） |
| 边界 | 判不准 / 剥离失败 / 超预算 ⇒ **回落今天的行为**，且必带机读码；绝不因去水印引入新失败 |

## §2 判据：怎么认定「这卷有平铺文本水印」

新模块 `backend/app/services/visible_text_watermark.py`（纯逻辑 + 一次 dict 探测，不 import 网络/DB，可单测）。

**抽样面**：前 `VISIBLE_WATERMARK_PROBE_PAGES`（默认 5）页的 `page.get_text("dict")`。水印逐页平铺，
5 页足够；实测真卷 A 149 页 dict 抽取 ≈1.0s，5 页 <0.1s。

**探测不分流**：**每个 PDF 都探**，不按 `is_scanned` 分流。理由：文本型卷也会走图像 OCR（稀疏文本层回填、
「扫描件 + 内嵌 OCR 层」被判 scanned 后走 OCR），而水印可能正好盖在这样的卷上；探测成本 <0.1s 且无候选时
自然 `no_watermark`，没必要为了省这点开销引入一个可能与真实路由漂移的分流条件。

**候选 span**（两条与关系）：`line.dir` 倾斜（`|dir.y| ≥ 0.2`）**且** `0 < span.alpha < 220`。
`alpha=0` 的**不可见**文本对象不算候选（实测某「扫描件误判为文本型」样本正是 `alpha=0` 的倾斜层，
若不排除会被误当水印）。

**平铺性校验**（三条全过才算命中）：

1. 命中页数 ≥ 3（探测 5 页时即 ≥60% 的探测页有水印）；
2. 候选字符数 / 该卷**可见**字符数 ≥ 0.5（水印在可见文本里占主导，不是零星杂散）；
3. 候选的**签名** `(font, dir 取 3 位, alpha/255 取 2 位)` 中最高的那一个占比 ≥ 0.85；
   同签名内 `size` 极差 ≤ 2.0pt（容忍同一生成器在不同页的 29.2/29.6pt 抖动）。

**实测覆盖（2026-10-29，只读，私有底稿有全量明细）**：两代生成器都命中，且**没有**误判：

- 族 A（`NotoSansCJKsc-Regular` / 45° / `alpha≈0.2`）：testdata 3 卷（真卷 A/B/C，149 页 / 41,136 字符、
  350 页 / 267,400 字符、10 页 / 7,640 字符；文件名映射见私有底稿），
  三卷 `visible_share = 1.00`、命中页 = 全部探测页、最高签名占比 = 1.00。
- 族 B（`STSong-Light` / −30°，`dir=(0.866,−0.5)` / `alpha≈0.3`）：私有语料（不入库）中 **471 卷 / 71,799 页**
  加密案卷**全部命中**，三项指标同为 1.00 / 1.00 / 1.00。⇒ `|dir.y| ≥ 0.2` **不锁 45°** 是关键：换了角度的
  同一类水印仍然命中。
- 反例 0 误判：testdata 28 卷 + 私有语料 33 卷明文案卷全部 `no_watermark`（其中含可见正文 39,832 字符的大页样本、
  整卷 43,440 字符的可见层样本、以及 `alpha=0` 倾斜层的样本）。

**产出 `WatermarkProfile`**：签名、探测页数、命中页数、候选 span 数、候选字符数、可见占比，
以及**进程内**用的逐页 span 矩形与文本。落盘/落日志的只有计数与签名 —— **不带水印字面量、不带案卷名、
不带临时路径**（延续 #203 AC4 的 PII 最小化口径）。

**防误剥不变式**（词级剥离用，阶段 2）：每页被剥离的字符数 ≤ 该页水印 span 字符数 × 1.5。越界 ⇒
**该页**放弃剥离、记 `strip_invariant_exceeded`，不是整卷失败。

## §3 形态与数据流

```
parse() → ensure_pdf_accessible() → 原件 path
   │
   ├─ profile 探测（前 5 页 dict，memo by 原件 realpath + size + mtime）
   │     └─ 命中 ⇒ 构建「净化副本」：临时目录 0700 下 <hash>.pdf
   │          逐 text-showing op（get_texttrace() 的 entry.bbox，页面空间，外扩 2pt）
   │          add_redact_annot(rect, fill=False)
   │          + apply_redactions(images=PDF_REDACT_IMAGE_NONE, graphics=PDF_REDACT_LINE_ART_NONE)
   │          + save(garbage=3, deflate=True)
   │
   ├─【阶段 1】渲染消费：get_pdf_page_image 内部把「原件 path」解析为「净化副本 path」
   │     · 走副本：vision_service.py:234、vision_service.py:2162、vl_md_pipeline_service.py:192
   │     · 走原件：api/files.py:813（预览，显式 raw=True）
   └─【阶段 2】建链消费：extract_text_layer_pages(..., watermarks=profile) 词级过滤
```

关键设计点：

1. **只有一个决定点**。哪张图从哪里渲染，只由 `file_parser.get_pdf_page_image`（`backend/app/services/file_parser.py:618-660`）
   内部解析器决定，四个调用点里三个**零改动**，预览加一个 `raw=True`。避免四份并行实现漂移。
2. **为什么落盘副本而不是常开内存 Doc**：PyMuPDF 的 `Document` 非线程安全，`BATCH_RECOGNITION_PAGE_CONCURRENCY > 1`
   时并发渲染会踩共享 Doc；落盘副本让既有页图缓存（`_pdf_page_image_cache`）、`open_pdf_checked` 的
   加密处理、`_validate_path` 全部原样复用。代价是临时明文副本（仅权限密码卷在盘上本就明文；打开密码卷
   在 `parse()` 阶段已被 `_strip_pdf_encryption` 解密落盘 ⇒ **不新增暴露面**），配 0700 + TTL 清扫 + 体积闸。
3. **净化副本不能供文档级链**。redaction 会连带删掉矩形内**同一位置的正文不可见文本**：实测真卷 A 的正文
   字符从 **43,449 → 8,468（-80%）**（op 级擦除擦得更干净，连带删的正文也更多）。所以两条消费线必须分开取材 —— 渲染用副本，建链用**原件 + 词级过滤**。
4. **剥离必须按 text-showing op，不按 span 矩形**（实测踩过的坑）：span 级 redaction 在真卷 A 上**每页留 2–3 字**、
   全卷共 **445 字符**残水印，加 pad(1/2/4pt) 与「整页大框」都无效（整页大框仍留 6 字）。根因是 PyMuPDF 的
   redaction 只对**整条 text-showing op 都落在擦除区内**的 op 生效，而水印斜行的半数字形会越出页边
   （例：trace 第 27 条 bbox `(507.1,-94.1,872.0,270.8)`，`y0=-94` 在页顶之外）⇒ 该 op 被整条跳过。
   改用 `get_texttrace()` 每条 entry 的 `bbox`（已按页面空间给全，含越界字形）作擦除矩形后，三卷正样本
   **残水印字符 = 0、可见字符 = 0**。附带好处：PDF 里 font / size / 文字矩阵 / alpha 都是**op 级**状态，
   op 内字形属性天然同质，op 级签名判定不会误伤同一 span 里的正文。
5. **建链面的词过滤规则**（实测三选一，目标保住 43.4k 正文字符）：
   - 词中心点落在水印 span 框内 ⇒ 只剩 13,843 字 / 97 页（45° 长条框太大，误删 30k 正文）**否决**；
   - 词框与 slab 相交面积 >50% ⇒ 16,994 字 / 97 页，同样过删 **否决**；
   - **词框与水印 span 相交 ∧ 词文本是该 span 文本的子串** ⇒ 45,313 字 / 5,437 词 / 130 页、覆盖度 0.0424 **采纳**。
6. **缓存不串味**：页图缓存键 `_pdf_page_cache_key(file_path, page, dpi)` 已含 `realpath + mtime + size`，
   传入的是**实际打开的那个 path** ⇒ 原件与副本天然不同键，预览与 OCR 不会互相污染。
7. **单飞与负缓存**：探查+构建按「realpath + fingerprint」单飞（复用 `text_layer_doc_source._key_lock` 的形态），
   失败不写正 memo，写短 TTL 负缓存并回落原件（避免 149 页里每页重试一遍失败构建）。

## §4 接线点、开关与可观测

新增配置（`backend/app/core/config.py`，位置紧随 `TEXT_LAYER_REPEATED_*` 段之后）：

| 键 | 默认 | 说明 |
|---|---|---|
> 下表是**阶段 1（渲染面）的规划值**，除探测/判据那几个键（阶段 2' 已落地、见 §7.1）外**尚未在
> `config.py` 里存在**；键名以本表为准，落地时按此命名（门⑥ 第一段冷审 Minor#1：原表里
> `..._TOP_SIG_SHARE` 与已实现键名 `..._MIN_TOP_SIG_SHARE` 不一致）。

| 键 | 默认 | 说明 |
|---|---|---|
| `VISIBLE_WATERMARK_STRIP_ENABLED` | `False` | 阶段 1 总开关（规划）。实现期默认关；④ 云验收通过后按 #129 惯例拍板默认开 |
| `VISIBLE_WATERMARK_PROBE_PAGES` | `5` | 探测页数（**已落地**，阶段 2' 共用） |
| `VISIBLE_WATERMARK_MIN_PAGES` | `3` | 命中页数下限（已落地） |
| `VISIBLE_WATERMARK_MIN_VISIBLE_SHARE` | `0.35` | 候选/可见字符占比下限（已落地；0.5 在「正文层不透明」的卷上余量只剩 0.005） |
| `VISIBLE_WATERMARK_MIN_TOP_SIG_SHARE` | `0.85` | 最高签名占比下限（已落地） |
| `VISIBLE_WATERMARK_MAX_SIZE_SPREAD` | `2.0` | 同一签名内字号极差上限 pt（已落地） |
| `VISIBLE_WATERMARK_MAX_FILE_MB` | `400` | 体积闸；超则不建副本直接回落（**阶段 1 规划**） |
| `VISIBLE_WATERMARK_COPY_TTL_MIN` | `30` | 临时副本清扫 TTL（**阶段 1 规划**） |

可观测（**无条件写块**，延续 #165 口径）：`pipeline_status["visible_watermark"] =
{code, pages_probed, pages_hit, span_count, chars, visible_share, build_ms, copy_bytes}`。
机读码枚举：`stripped` / `no_watermark` / `probe_failed` / `probe_budget_exceeded` / `build_failed` /
`copy_too_large` / `render_fallback` / `strip_invariant_exceeded`。
日志：命中一次 info；每次回落一次 warning（只带码、计数、异常类型）。

## §5 失败回落矩阵

| 触发 | 行为 | 码 |
|---|---|---|
| 探测异常（损坏 PDF / 加密无密码 / dict 口径抛错） | 原件渲染，行为=今天 | `probe_failed` |
| 探测超预算（页多且大） | 原件渲染 | `probe_budget_exceeded` |
| 未命中 profile（任一条平铺判据不过） | 原件渲染 | `no_watermark` |
| 副本构建异常 / 超时 | 原件渲染 | `build_failed` |
| 原件体积 > `MAX_FILE_MB` | 原件渲染 | `copy_too_large` |
| 单页从副本渲染异常 | **该页**回落原件渲染 | `render_fallback` |
| 词剥离越不变式（阶段 2） | **该页**用原词表 | `strip_invariant_exceeded` |

最后两行是「单页级」回落，其余是「文件级」。任何一行都不会让整卷比今天更坏：**最坏等于今天**。

## §6 验收标准（AC）

> 本节 AC1–AC14 属阶段 1（渲染面剥离）；阶段 2' 的 AC15–AC33 在 §7.1 末尾，按门①.5 冷审（REVISE）逐条重写。

门号沿用 `docs/开发门禁-测试与独立评审规范.md`。语料分三层（语料↔文件名映射与原始测量数字留在私有仓底稿，见 §10）：

- **正样本 4 卷 / 2 个水印族**：族 A 三卷（真卷 A 149 页扫描卷、真卷 B 350 页解密卷、真卷 C 文本型命名但实为水印卷前 10 页）
  + 族 B 从 471 卷私有语料里抽 5 卷（不同卷宗目录、每卷 20–33 页，用水印角度/透明度都不同的生成器）。
- **反例集 11 卷**（正文可见层文本型 2 卷、「扫描件误判为文本型」2 卷、issue88 ×2、issue115 混合/文本层 ×2、
  issue96 印章、issue104 大页、issue198 重复表）。
- **规模反例 61 卷 / 规模正例 471 卷**：testdata 其余 28 卷 + 私有语料 33 卷明文案卷 = 61 卷必须 `no_watermark`；
  471 卷加密案卷（71,799 页）必须全部命中。这一层**不进 CI**（语料在私有体系），作为一次性只读验证 + 底稿留档。

| AC | 判据（可判） | 证据 | 门 |
|---|---|---|---|
| AC1 判据正样本 | **4 卷 / 2 族**正样本 profile 全部命中；每卷 `visible_share ≥ 0.9` 且命中页 = 全部探测页（实测 2 族 4 卷均为 1.00 / 全页） | 单测 + 一次性只读脚本输出（入私有底稿） | ②③ |
| AC2 判据反例 | 11 卷反例**全部** `no_watermark`；**0 个字符**被剥；反例里 `alpha=0` 倾斜层的那卷必须报 `no_watermark`（不得误判）。规模层：61 卷明文案卷（testdata 28 + 私有语料 33，含 39,832 字符大页样本与 43,440 字符可见层样本）全部 `no_watermark` | 单测（每卷一条断言）+ 变异：删「倾斜」或删「alpha」任一条件必须造出误剥 ⇒ 判据转红；规模层一次性脚本 | ②③ |
| AC3 判据红性 | 三条平铺判据（命中页数 / 可见占比 / 最高签名占比）各有 1 条合成夹具 + 1 次变异（阈值改到必过或必不过）⇒ 必须红 | 变异记录（KILLED 表） | ②③ |
| AC4 渲染等价 | 净化副本 vs 原件同页 150dpi：**非水印区域逐像素不变**（口径：水印 op 矩形外扩 2px 之外的像素，`max|Δ| == 0`）。**已实测成立**：族 A 4 页（0/20/74/133）、族 B 3 页（0/15/30）`changed = 0.000%`、`max|Δ| = 0`；水印区内变化 1.31–3.26%、`max|Δ|` 34–76。**残水印字符 = 0**（族 A 3 卷、族 B 5 卷抽样，可见字符全部归零）；全部页面渲染成功 | 本机脚本 + 证据图（私有底稿） | ②③⑤ |
| AC5 原件不变 | 剥离前后原件 `sha256` 与 `mtime` 完全相同；交付物不含任何副本路径；临时目录清扫后为空 | 脚本断言 + 单测 | ②③ |
| AC6 取图矩阵 | 四处调用点行为：3 处走副本、预览走原件；`raw=True` 时即便 profile 命中也不走副本；页图缓存键随实际 path 变化（不串味） | 单测（monkeypatch 计数） | ②③ |
| AC7 失败矩阵 | §5 七行各一条单测，且每行可变异成红（把回落分支删掉/改成抛异常） | 单测 + 变异 | ②③ |
| AC8 可观测 | profile 命中/回落都在 `pipeline_status["visible_watermark"]` 出现，码枚举与 §4 一致；**不含字面量与路径**（正则扫输出：无水印串、无 `/tmp`） | 单测 + 输出扫描 | ②③ |
| AC9 副本成本 | 本机实测入带（op 级口径）：真卷 A（149 页 / 4,470 op / 26MB）**1.94s**；真卷 B（350 页 / 29,400 op / 205MB）**16.86s**、产物 102.7MB；族 B 抽样（20–33 页、每页 1 条 op）1.0–4.0s、体积变化 <1%。全私有语料 71,799 页按此比例外推 ≈1 小时（**外推，不是实测**）；超过 `MAX_FILE_MB` 与 `COPY_TTL_MIN` 有判据 | 只读测量脚本 + 单测（体积闸） | ②③④ |
| AC10 框噪声（云，④） | **阶段 1 形态**：取**走逐页 OCR 的族 A 真卷**（真卷 A 149 页），原件 vs 渲染净化副本各一份同参数 job，对 `file_store.data_json.bounding_boxes` 做区域级 diff ⇒ 水印区内命中框数从 N 降到 ~0（N 与降幅报实数），耗时变化报实数；**正文召回不降**（8–10 页人工核对表：正文框数不减、无新增漏检） | job 前后 diff + 人工核对表（私有底稿） | ④⑤ |
| AC10b 整份副本送识别（已实测，**反向证据**，不进 AC） | 族 B 一卷 31 页：框 5,935→551（−90.7%）、纯水印框 4,612→0，但 OCR 阶段 156s→378s、整 job +14.6%（因副本文本层被擦除 ⇒ docline 弃用）。此臂只用于否决「副本既供渲染又供建链」的形态 | job 两份 + 路由日志（私有底稿 §8） | ④ |
| AC11 零回归 | 后端全量测试基线对比 `comm -3` = 0 行；前端全量；反例集在**默认关**与**默认开**两种配置下，路由与区域数都不变 | 双跑 + 集合差 | ②③④ |
| AC12 默认开判据 | 默认关时全链行为与 `89f51f9` 逐字节等价（同 job 比对）；翻转默认值单独一次提交，附④ 证据 | 配置翻转 + 同 job 比对 | ④⑤ |
| AC13 ①.5/⑥ 独立评审 | 本设计过 ①.5 冷审（对照 Issue 原文查「恒过判据」）；实现过 ⑥ 冷审（Critical 0） | 评审原文（私有仓） | ①.5⑥ |
| AC14 规模化判据 | 私有语料 540 卷一次性只读扫描：**471/471 命中**（两族签名各自唯一、`top_sig_share = 1.00`）、**61/61 明文案卷不命中**、`near_miss = 0`；被抽样的 5 卷族 B 剥离后**可见字符 = 0**。判据阈值调整后这张表必须重算（脚本与结果入私有底稿） | 只读脚本 + 结果表（私有底稿） | ③④ |

**诚实边界（必须写进 PR 描述，不得省略）**：

1. 只覆盖**文本对象层**的倾斜半透明平铺水印。已验证 2 个族（45°/alpha 0.2/NotoSansCJKsc 与 −30°/alpha 0.3/STSong-Light），
   但两族都出自**同一批送检机构语料**；**水平 / 不透明 / 像素级烧死 / 图章 / 溯源型**水印仍不覆盖，这类卷行为=今天。
2. 阶段 1 **不取得速度收益**：门④ 实测整份剥离副本送识别反而 **+14.6%**（族 B 一卷：156s→378s 的 OCR 阶段差
   全来自「副本文本层被擦除 ⇒ docline 弃用」）。任何「提速」表述都不允许出现。
3. 框噪声收益在阶段 1 只能通过「OCR 不再看见水印」推断，**必须由 AC10 的云上前后 diff 证明**，不得用外推数字
   当结论（#203 遗留的「约 8 处/页」是未经验证的既有行为）。
4. 角度容忍已实测跨 45° 与 −30°；但 alpha 落在 `[220, 255]`（近乎不透明）或 `alpha = 0`（不可见）的层**一律不剥**，
   这是刻意的不作为 —— 前者可能被误当正文、后者本就是不可见层，两种情况都比「剥错」安全。
   判据仍基于版式签名而非内容，未覆盖的第三族会漏判 ⇒ 回落今天行为，记为已知边界而非缺陷。
5. **剥离会连带删掉同一位置上的不可见正文文本对象**（真卷 A 正文 43,449 → 8,468 字符）。这是可接受的，因为副本
   只供渲染；建链面的词级过滤走原件（阶段 2），不用副本。任何「副本可以顺手给 docline 用」的想法都必须重新测量。
6. 阶段 2 落地后仍需复验 #203 闸：剥净卷**应当**通过闸，但「通过闸」不等于「建链不超时」（#204 未解）。
7. **净化副本会连带擦掉不可见正文层，副本本身不能再当「有文本层的 PDF」用**：族 B 卷副本
   `get_text("words")` 为空、管线报 5.8 字/页，docline 判 `UNUSABLE`（门④ 实测）。任何「副本顺手给建链/给预览用」
   的形态都必须重新测量 —— 已实测的那次就是这个形态，代价是 OCR 阶段 2.4×。
8. 族 B 卷**今天普遍走 docline**（12 卷抽样 11 卷会接管；水印是 4 字词，低于 #203 的 `≥10 字` 口径 ⇒ 闸看不见）。
   按 docline 实测费率推演，120 页以上的卷建链已逼近/超过 `BATCH_RECOGNITION_PAGE_TIMEOUT=300`，可能今天就撞
   **#204 的页 1 掐断**；这条属 #204 的账，未实测，不得当 #206 的结论使用。

## §7 分期与依赖

| 期 | 内容 | 前置 | 交付判据 |
|---|---|---|---|
| **阶段 1（本 PR）** | `visible_text_watermark.py`（profile 探测）+ 净化副本构建器 + `get_pdf_page_image` 解析器 + 配置 + 状态块 | 无（独立可交付） | AC1–AC9、AC11、AC12 |
| **阶段 2'（本 PR，族 B 价最高）** | `visible_text_watermark` 词级过滤：只丢「文本 ⊆ 水印字符集 且本页 ≥8 次」的词（**keep floor 7 份**），把干净流喂 docline（**不动渲染、闸口径不变**） | 无（是**减负**：流 −36.9%、块 14→约 7，不加剧 #204） | AC15–AC33（主判据=门④ AC18/AC19/AC26） |
| **阶段 2（原计划）** | （若阶段 2' 不足以让族 A 通过闸）剥净卷让 docline 接管 | **#204 合入**（建链脱离单页门限） | 追加：剥净卷 coverage ≤0.15、接管页数、端到端提速实测 |

阶段 2 的代码位置与规则已在 §3.4 定稿，阶段 1 不写、不埋「死开关」（YAGNI）。

### §7.1 阶段 2'（建链流词级过滤）实施规格 —— 本轮先做的那块

**依据（门④ 实测，2026-10-29）**：族 B 卷的文本层里，水印就是**每页 65 个 4 字词**（与正文词并存，字符集固定、
逐页同形），而今天的 docline 直接把这些词当正文送 NER ⇒ 77.7% 的框是水印框。把词丢掉，流就干净了，
**不需要动渲染、不需要重 OCR、也不需要 #204**（流更小 ⇒ 建链更快）。

**唯一决定点**：`text_layer_doc_source._build()` 里，**三道资格闸（预算 / 质量 / 跨页重复片段）跑完之后、
`run_document_ner` 之前**插入一次过滤；`extract_text_layer_pages` 保持「只读文件、不判语义」，
不在服务层塞业务规则。

> **v1.3 变更（门①.5 冷审 D2/D4/D6 处置）**：v1.2 写的是「过滤后再过闸」。实现改为**闸吃未过滤页、
> 只有 NER 吃过滤页**，理由有两条，且都被 AC24/AC25 钉住：①闸保持与今天完全同口径 ⇒ 本卷是否接管
> docline 与今天逐卷一致，阶段 2' 不可能把今天被拒的卷（族 A 类）放进来（那是 #204 的账）；
> ②`quality.compute_metrics(page_word_texts)` 的入参就是每页词文本，若过滤在闸之前，短词比 / 平均词长 /
> 每页字符 / 覆盖率全套判据的通过集合都会变，收益与风险同时不可解释。

```python
# backend/app/services/visible_text_watermark.py（阶段 2' 只实现这三个纯函数 + 一次 dict 探测）
profile: WatermarkProfile | None = probe_visible_watermark(path)   # 前 5 页 dict 探测（§2 判据）
wm_rects = collect_watermark_rects(path)                            # 每页「可见候选 op」的逐字形框
filtered, stats = filter_watermark_words(pages, profile, wm_rects=wm_rects)
# 调用点：text_layer_doc_source._build() —— 上面三道闸仍用 pages；只有
#         docline.run_document_ner(filtered, ...) 收到过滤后的页。
```

**词级过滤规则（v1.4：文本 ∩ 几何，四条同时成立才丢）**：

1. 该词字符集 **⊆ 水印字符集**（`profile.wm_charset`，来自探测命中的可见半透明倾斜 span）；
2. 该词字长 ≥ 2（1 字词一律不丢）；
3. 该词词框被**本页水印字形框**覆盖 ≥ `VISIBLE_WATERMARK_WORD_COVER_MIN`（默认 **0.35**；
   字形框 = `get_texttrace()` 里 `0<opacity<0.86` 且 `|dir.y|≥0.2` 的 op 的**逐字形** bbox，页面空间）；
4. 本页**落在水印字形上的**同形词 ≥ `VISIBLE_WATERMARK_WORD_MIN_REPEAT`（默认 **8**；族 B 实测 65/页）。

> **v1.4 变更（门④ 独立 GT 抓出的漏打码风险）**：v1.3 的纯文本判据 + keep floor 被 GT 否掉——
> 族 B 卷的「不可见正文层」本身就是**对着带水印的扫描图 OCR 出来的**，同页里也有十几二十份水印串。
> 纯文本判据下「正文位置的 1–2 份真实简称」与「OCR 顺带认进来的 18 份水印」不可区分，keep floor 只能把
> 误删压到 7 份/页，却**不能保证留下的是正文位置那几份**（可能留的是水印位置的、正文位置的被丢 ⇒ 该实例
> 不打码）。实测：3 卷抽样里纯文本判据共丢 **322 / 462 / 254** 个正文 GT 词（全部是水印串）。
> 改为**文本 ∩ 几何**后同一脚本复验：**`body_missing_free = 0 / 0 / 0`**，水印位置字符 **8,060 / 8,580 / 7,540**
> 全部清掉（第 3 卷有 2 页因不变式越界保持原样 ⇒ 残留噪声，方向安全）。keep floor 随之取消。

**防误剥不变式**（每页独立判定，越界只放弃该页）：
① 被丢字符数 ≤ **`VISIBLE_WATERMARK_MAX_DROP_VS_WM_CHARS`(1.5) × 探测到的每页水印字符数中位数**；
② **页级保留下限**（门⑥ 冷审 I2/I6）：过滤后本页剩余字符 < `page_chars × VISIBLE_WATERMARK_MIN_KEEP_RATIO`(0.15)
⇒ 该页保持原样、单独记 `pages_skipped_would_wiped`（与预算越界**分开计数**）。
第二种情况是「正文本身写成倾斜半透明平铺」的假阳性通道（实测丢弃比 0.96），单一 `not kept`
只挡得住「丢成 0 词」。族 B 最薄的真页（正文 74 / 水印 260 字符，丢弃比 0.78）仍然被剥。
（v1.2 写的「≤ 该页总字符数 × 0.5」已废：薄页正文本就少，占比天然 >0.5，会把最需要过滤的薄页全部挡下。）

**可观测**（键名以实现为准：写在 `pipeline_status["text_layer_doc"]["word_filter"]`；门⑥ 第一段冷审 Minor#1）：
`{applied, code, pages_total, pages_filtered, pages_skipped_invariant, pages_skipped_no_geometry,
pages_skipped_would_wiped, words_dropped, chars_dropped, dropped_ratio, profile}`；开启过滤且真剥过时
额外带 `gate_basis_unfiltered_coverage`（见下方 C2 处置）。
`code ∈ {applied, disabled, no_watermark, no_geometry, would_wiped_page, filter_invariant_exceeded,
filter_failed}` —— **五态必须可区分**（门⑥ 冷审 I3）：`no_watermark` = 探测不中；`no_geometry` =
探测到水印但拿不到逐字形框（整卷没剥）；`would_wiped_page` = 页级保留下限把整页挡下（假阳性形态）；
`filter_invariant_exceeded` = 预算越界；`applied` = 真剥了。`profile` 只带计数与比例。
**不带任何字面量**（延续 #203 AC4 口径）。

**失败回落**：开关关 ⇒ `disabled`；探测不中 ⇒ `no_watermark`；任何异常 ⇒ 用**未过滤**页 + `filter_failed`。
绝不抛给上层、绝不写新的永久负缓存。

**开关**：`VISIBLE_WATERMARK_WORD_FILTER_ENABLED` 默认 **False**；翻转默认值必须单独提交并附 ④ 证据（AC19）。
开关与**全部**阈值（`WORD_MIN_REPEAT` / `WORD_COVER_MIN` / `MAX_DROP_VS_WM_CHARS` / `PROBE_PAGES` /
`MIN_PAGES` / `MIN_VISIBLE_SHARE` / `MIN_TOP_SIG_SHARE` / `MAX_SIZE_SPREAD` / `MIN_KEEP_RATIO`）都进 memo key，
否则「同 file_id 开关两份 job」会吃到同一 memo、两臂必然零差异（AC27）。键组装抽成纯函数
`text_layer_doc_source._memo_key()`，让「阈值是否真的进了拼接」可被单测判红（原先只直接比对
`_word_filter_knob_key()` 的字串，把这一行从拼接里删掉照样绿 —— 门⑥ 冷审 I4/M3）。

**门④ 实测（阶段 2'，2026-10-29，族 B 31 页真卷；两臂同一份字节各自独立上传、唯一差别是开关）**：

| 项 | 臂 A（开关关=默认交付态） | 臂 B（开关开） |
|---|---|---|
| 终态 / 失败页 | `awaiting_review` / `{}` | `awaiting_review` / `{}` |
| 总墙钟（驱动侧） | 752.4s | 734.6s |
| `recognition.duration_ms` | 761,914 | 744,196（−2.3%） |
| **框总数** | **5,892** | **1,077（−81.7%）** |
| **纯水印框（独立 GT）** | **4,612（78.3%）** | **0（0.0%）** |
| docline 建链日志 | `chunks=14 calls=28 elapsed=92.33s word_filter=disabled/0` | `chunks=9 calls=18 elapsed=137.10s word_filter=applied/8060` |

读法（诚实口径，别把噪声读成收益）：
- AC18 主判据**通过**：纯水印框 78.3% → **0%**（≤5%×N）。GT 独立于过滤器：框文本全字符 ⊆ 水印字符集
  **且**框中心落在该页水印逐字形框内；A 臂 charset 与几何两个口径互相印证同得 4,612。
- 块数/请求数随 token 量下降（14→9 / 28→18），但**建链墙钟本臂反而 92s→137s** —— 同一天 A 臂与阶段 1
  基线（145s）波动同样大，属服务端时延波动，**本方案不主张提速**。总 job 墙钟 −2.4% 主要由视觉特征
  阶段（占 ~75%）决定，与本题无关。
- 类型分布 A→B：`GOVERNMENT_AGENCY 2,772→70`、`WORK_UNIT 2,116→66`；`DATE 139→102`、`ADDRESS 146→145`、
  `PERSON 134→160`、`AGE 158→194` —— 水印字符集相关类型塌缩，其余基本不动（小升=流变干净后重新切分）。
- AC19 默认态：A 臂**未设任何环境变量**跑出 `word_filter=disabled/0`，行为=今天。
- AC26 安全阀：build 137.10s ≪ 300s，无 `build_budget` 回落、无整卷清零、无 300s 整页被杀。

### v1.5：门⑥ 第一段独立评审的逐条处置（`686c4c0..9bbca7f`）

评审结论 REVISE，两条 Critical + 六条 Important + Minor/AC 表判读。逐条处置与证据：

| 项 | 判定 | 处置（代码锚点） |
|---|---|---|
| **C1 旋转页坐标系错位 ⇒ 误删正文（漏打码）** | 成立 | `_visible_char_rects` 与非零 `page.rotation` 页同样乘 `page.rotation_matrix`（与 `text_layer_docline_service._word_boxes` 同一校正）。单测 `test_filter_works_on_rotated_page_keeps_body`（3 页 `/Rotate 90`，断言正文词逐字保留）；变异=删掉该乘法 ⇒ 红 |
| **C2 后置卷级门吃过滤流 ⇒ 可能改路由** | 机制成立，但**不可事后重建未过滤口径** | 覆盖率门**仍按过滤后流判**：未过滤分子里含大量水印噪声实体（它们正因为水印进了流才被认出），若改用 `covered/未过滤流长`，族 B 真卷会从今天的 1.000 掉到约 0.6 ⇒ 今天走 docline 的卷全部回落 OCR、本阶段收益归零（门④ 实测过滤后 `chunks 14→9` 仍走 docline）。因此改为**上报**：`gate_basis_unfiltered_coverage` 写入 memo + 日志（`filtered=… unfiltered_basis=… stream_chars a->b`），并由 **AC17b** 在语料上冻结「由拒转过/由通过转拒」清单 = 0。`truncated_ratio`/`map_failure_ratio` 的分母只会更小 ⇒ 方向只能是更严，已在 §7.1 说明 |
| I1 `MIN_VISIBLE_SHARE=0.5` 余量只有 0.005 | 成立 | 默认改 **0.35**（正文层不透明时 300 字/页 share=0.459 仍判命中） |
| I2 假阳性（正文本身是倾斜半透明平铺）无兜底 | 成立 | 新增页级保留下限 `MIN_KEEP_RATIO=0.15`（见上），单独计数 `pages_skipped_would_wiped` |
| I3 几何缺失仍报 `no_watermark` | 成立 | `_filter_code()` 五态，新增 `no_geometry` |
| I4 7 个变异 SURVIVED | 成立 | 变异表扩到 **16 个、全部 KILLED**（见 AC31）；新增用例：`_memo_key` 真拼接、`compute_metrics` 实参、`page_chars` 来源、空页回退、probe 三闸、倾斜/不透明常量、字符集条件、`MIN_VISIBLE_SHARE` 默认值、`no_geometry` 分支、keep-ratio 守护、旋转校正、C2 上报 |
| I5 memo key 缺 4 个探测阈值 | 成立 | 全阈值进 key（见上），并抽 `_memo_key()` |
| I6 无页级字符下限 / 计数器混用 | 成立 | keep-ratio 守护 + 分开计数（见上） |
| I7 1–2 页卷永不剥 | 方向安全，登记 | 见诚实边界 14 |
| I8 `pages_hit` 口径不绑 `top_sig` | 部分成立 | `top_sig_share≥0.85` 兜底；登记为边界 15 |
| Minor 键名漂移 / `.env.example` 未登记 / COVER_MIN 可配 0 / 两次开 PDF 无上限 | 成立 | §4 表标注规划态并改对键名；`.env.example` 登记 9 个键；`WORD_COVER_MIN` 钳 **[0.2, 1.0]**；`collect_watermark_rects` 只是逐页 trace（族 B 31 页 0.08s、族 A 149 页 <1s），登记为边界 16 |
| AC15①/AC16/AC19/AC21/AC22①/AC24/AC26/AC27/AC31 判读 | 成立 | 逐条重写（见 AC 表）：AC16 加 `pages_probed>0 & profile.pages_hit>0` 反恒真断言；AC21 换 keep-ratio/no_geometry；AC22① 补字符集钉子；AC24 补 `compute_metrics` 实参；AC26 去掉恒真式；AC27 换 `_memo_key`；AC31 换 16 变异表 |

**我方对评审的三处反驳（已在 v1.3 记录并复核）**：①`testdata/第四卷.pdf` 实为 45°/opacity 0.2/
`NotoSansCJKsc-Regular`（族 A），不是 −30°；②`~/caseData`（44GB/540 卷）在**本机**，语料脚本本地只读可跑；
③「过滤会改变闸输入」在最终实现里不成立（闸吃原页）。

**阶段 2' 的 AC（v1.3 重写；门①.5 冷审逐条处置，旧 AC15–AC20 已替换）**：

| AC | 判据（可判，未实现时必须红） | 证据 | 门 |
|---|---|---|---|
| AC15 过滤正确性与正文零损失（**独立 GT**） | GT **不取自过滤器输出**：直接从 PDF 文本对象层标注——① 被丢词全部落在水印字形上（`chars_dropped` 与「可见 span 字符数」误差 ≤5%；第 3 卷 allowlist 2 页不变式外，其余页 100%）；② **正文零损失**：把「只由 `alpha=0` span 覆盖的词、且不被水印字形覆盖」作为正文 GT，过滤后按 `(页, 归一化坐标)` 逐条必须仍在 ⇒ `body_missing_free == 0`；③ 同一脚本必须同时报出「水印位置的同形词被丢掉多少」（丢弃数 / GT 水印字符数 ≥95%）。族 B 抽样 ≥3 卷（本地只读 `~/caseData`），**已跑出 0/0/0**（私有底稿 §10） | 单测（合成夹具）+ 只读脚本 + 结果表（私有底稿） | ②③ |
| AC16 反例零动作（默认开配置下） | 11 卷反例 + 61 卷明文案卷：`docline_word_filter.code == "no_watermark"`（**字段缺失即 FAIL**，不是「计数为 0」）、`words_dropped == chars_dropped == 0`、且文本层逐页词表与今天**逐条相同**（深比较）；必含 4 卷 `alpha=0` 倾斜层样本与 2 卷「扫描件误判为文本型」 | 单测（每卷一条）+ 规模脚本 | ②③ |
| AC17 路由零变化 | 族 A 真卷（149 页）在**开启**过滤时仍被 `repeated_fragment_coverage` 拒（闸吃原页 ⇒ coverage 0.3764 不变）、`reason_code` 与今天逐字相同；**零个「由拒转过」卷**（全量清单见 AC25）；与现网行为一致性由 AC24 的顺序钉住 | 单测 + 云 ④ 单卷 | ②③④ |
| AC17b 路由冻结清单（C2 反恒真） | 在 12 卷 docline 资格抽样的同一批卷上，重跑「开关关 / 开关开」并逐卷比较 `docline_word_filter` 与路由结果：**「由拒转过」清单 = 0**；「由通过转拒」清单只允许 §7.1 明列的已知形态（无正文层卷 ⇒ `would_wiped_page`）。同时冻结 `gate_basis_unfiltered_coverage` 上报值（每卷 1 个数），差值 >±0.05 即说明过滤器动了流（与 AC25 同源）。理由：C2 机制（后置门分母缩小）真实存在但**不可事后重建未过滤口径**，只能用「语料上零路由漂移」代替「机制不可能」 | 只读脚本 + 清单 | ②③ |
| AC18 云上收益（④，同卷**两份独立上传**） | 主判据 = **纯水印框（独立 GT：框 text 全字符 ⊆ 可见水印字符集 且框中心落在水印字符 bbox 内，GT 由 PDF trace 标注，不用过滤器输出）** 从 N 降到 **≤ 5% × N**（v1.4：几何判据下预期近 0，留 5% 容 NER 把相邻正文与水印粘连）；框总数、`stream_chars`、`chunks`、`recognition.duration_ms` 全部报实数，且 `duration_ms ≤ 基线 × 1.05`。**禁止**用「5,935−4,612」这类算术重述充当证据 | 两份 job + diff + GT 表（私有底稿） | ④⑤ |
| AC19 默认交付态 | ① 单测钉住 config 默认值（`ENABLED is False`）与三个阈值默认值；② ④ 取证用**环境变量**开跑；③ 若拍板默认开，则翻转必须同 PR 内单独提交，并跑一次**不设任何环境变量**的 job 证明默认生效；④ 默认开时族 B 抽样 10 卷 docline 接管率 ≥ 现网基线（10/11），`docline_word_filter.applied` 的卷 ≥8 | 单测 + ④ job 清单 | ②④ |
| AC20 可观测 + 敏感面 | `docline_word_filter` 在成功/回落两态都出现且键集稳定；`reason_detail` / 日志 / 状态块不含水印字面量、案卷名、临时路径；新增文件过 G4 公开树扫描 0 命中，且**同轮注入 canary**（假水印词 + 假案卷名各一）证明扫描器能抓（否则该条恒真） | 单测 + 扫描 + canary | ②③ |
| AC21 页级下限、薄页与假阳性兜底 | ① 过滤后逐页保留字符 >0（丢空即该页回退原样）；② **页级保留下限** `MIN_KEEP_RATIO=0.15`：夹具构造「正文本身写成倾斜半透明平铺」⇒ 该页整页保持原样、记 `pages_skipped_would_wiped`（不是被剥空）；真卷侧验证「薄页照样被剥」（族 B 3/31 页正文 <120 字）；③ **几何缺失单独成码**：profile 命中但 `collect_watermark_rects` 空 ⇒ `code == "no_geometry"`（不许报 `no_watermark`）；④ `covered_pages` / `uncovered_pages` 与今天逐页相同（深比较；`page_chars` 是 `_build` 内联量、不在 memo 键里） | 单测 ×4 + 只读脚本 | ②③ |
| AC22 高频正文短语保护 | 夹具三种：① 字符集**不在**水印字符集里的 4 字短语（「本院认为」形态）同页 5 次、跨 160 页共 800 次 ⇒ **0 丢弃**；② 字符集**在**水印字符集里的 4 字短语、同页 5 次且**不在水印字形上** ⇒ 0 丢弃；③ 同上但落在水印字形上 65 份 ⇒ 只丢这 65 份、正文位置的同形词一份不动（`test_filter_keeps_body_position_copies_of_watermark_text`）；④ **字符集是必要条件**：4 字词即使 65 份全落在水印字形上，字符集不在 `profile.wm_charset` ⇒ 0 丢弃（`test_filter_requires_charset_membership_even_when_covered`；变异=删字符集判断必须红）。真卷侧由 AC15 的正文 GT 兜住 | 单测 ×4 + 变异 | ②③ |
| AC23 异常/极端/并发/幂等 | 无文本层、0 页、损坏 PDF、加密件、非 CJK、>1000 页（`MAX_PAGES` 截断）、单页无水印的混合卷、过滤后正文全空、同卷 4 并发、job 重试：每例「行为 == 今天」；**不新增 permanent 负缓存**（断言 `reason_code` 与 `retry_at` 非新增 inf，除非与今天同因） | 单测 + 云故障注入 | ②③④ |
| AC24 闸位置钉住 | 单测：`compute_metrics`、`measure_repeated_fragment_coverage` 与建流预算闸收到的是**同一个未过滤 `pages` 对象**（`is` 比较；`compute_metrics` 的 `page_word_texts` 实参也逐页等于未过滤词文本，stub 不得丢参），`run_document_ner` 收到过滤后的新对象；变异=把过滤挪到闸之前或丢参 ⇒ 必须红 | 单测 + 变异 | ②③ |
| AC25 闸指标清单（④） | 抽样 5 卷报 `short_word_ratio / avg_word_len / chars_per_page / garbage_ratio / coverage / map_failure_ratio` 过滤前后数值；因闸吃原页，判据=**逐值相同**（差异出现即说明过滤位置被改坏）；并列「由拒转过 / 由通过转拒」卷清单（预期 0） | ④ 只读 DB + 日志 | ④ |
| AC26 安全阀真实数值（④） | 3 卷（含 ≥1 卷 >120 页）：逐卷报「建链墙钟 / 块数 / 是否回落 / 回落码」，判据只有两条 —— ① **没有任何一页被 300s 门限杀**（`recognition.failed_pages` 不出现 `timeout`）；② 终态 ∈ `{awaiting_review, completed, completed_with_failures}` 且未出现整卷清零。**不再断言「必须回落 build_budget」**（默认 600s ÷1.6s/页 下 149 页估 238.4s 永不触发；现网码 `repeated_fragment_coverage`），也不把「三选一」当判据（恒真）。已实测（门④）：build 137.10s ≪ 300s、无回落、无杀页 | 单测（合成阈值）+ ④ 实测 | ②③④ |
| AC27 开关/memo 隔离 | 单测直接打 `text_layer_doc_source._memo_key(file_id, snapshot, labels, regex_types)`：开关 / `WORD_MIN_REPEAT` / `MAX_DROP_VS_WM_CHARS` / `MIN_KEEP_RATIO` / `MIN_PAGES` / `MIN_VISIBLE_SHARE` / `MIN_TOP_SIG_SHARE` / `MAX_SIZE_SPREAD` **任一变** ⇒ key 变（`test_memo_key_includes_filter_settings`）；④ 两臂必须**各自独立上传**（不同 `file_id`）。注：`_memo_key` 是 `boxes_for_page` 里 memo 的唯一拼装入参，`_build` 本身不读写 memo（旧写法「跑两次 `_build`」抓不到 knobs 拼接，已弃） | 单测 + ④ 记录 | ②③④ |
| AC28 原件/产物边界 | 原件 `sha256` 与 `mtime` 过滤前后完全一致（只读打开，不写盘）；过滤只在内存里产出新列表、不落任何中间文件；不新增下载/导出/预览面 | 单测 + 脚本断言 | ②③ |
| AC29 产品能力边界 | 前端无「去水印」设置项/无对应 API 参数/文档不宣称支持去水印；开关只在后端配置；PR 描述不含「支持去水印/可去水印」表述 | grep 断言 + 人工核对 | ②③ |
| AC30 零回归口径（重写） | 基线 = **分支点 `686c4c0`**（preview HEAD，含 #210），不是 `89f51f9`；比较**归一化投影**（boxes 归一化坐标 + 类型 + 页号、`reason_code`、region 计数），显式排除 timestamp / job_id / duration / 临时路径；后端「FAILED 集合 `comm -3` = 0 行」并给两侧总数；前端给例数基线（当前 279） | 双跑 + 投影 diff | ②③④ |
| AC31 变异测试 | **16 个变异全部 KILLED**：M3 knob key 拼接、M5 `page_chars` 来源、M7 空页回退、M8 旋转校正、M9/M9b/M9c probe 三闸、M10 倾斜阈值 0、M10b 不透明上限 255、M11 删字符集条件、M12 `no_geometry` 分支、M13 NER 吃未过滤页、M14 删 memo key knob 段、M17 `MIN_VISIBLE_SHARE` 默认值、C2 删未过滤口径上报、N1 删 keep-ratio 守护。变异纪律：先 `assert old in s` 再写（否则「SURVIVED」可能是没改到），复原用 `cp .bak` 而非 `git checkout`（未提交改动会被抹掉） | 变异记录表 | ②③ |
| AC32 部署链路 | 本地 `docker compose --profile gpu` 冒烟（backend+ocr+ner+frontend）；云 DCU ④（docline NER 走 DCU vLLM）；docker nginx 链路断言无 504（`frontend/nginx.conf.template` `proxy_read_timeout=900s`，本 PR 不改超时）。`deploy/dual-5090` 若本地不可达，写明「本 PR 未改超时/端点/取图口以外的东西 ⇒ 跳过 + 理由」，不得沉默跳过 | 三处日志/截图 | ②④⑤ |
| AC34 旋转页坐标一致性（C1） | 单测：3 页 `/Rotate 90` 的族 B 形态夹具 ⇒ `code=="applied"`、水印词被剥、**正文词逐字保留**；`words` 与 `get_texttrace()` 字形框必须落在**同一页面空间**（旋转页两者都乘 `page.rotation_matrix`）。变异=删字形框旋转校正 ⇒ 用例必红（已 KILLED）。真卷侧：docline 抽样里含 `/Rotate 90/270` 页的卷 | 单测 + 变异 + 真卷抽页 | ②③ |
| AC35 页级保留下限与几何缺失（I2/I3/I6） | 单测三条：① keep-ratio 越界 ⇒ 整页原样且 `pages_skipped_would_wiped` 计数；② keep-ratio 允许薄页真正文被剥；③ profile 命中但几何为空 ⇒ `no_geometry`。真卷侧：[2] 29 页无正文层卷实测 `code=="would_wiped_page"`、0 丢弃（守护生效） | 单测 ×3 + 真卷 | ②③ |
| AC33 抽样预注册 | 抽样帧、随机种子、卷名清单**先落** `LexMask-workspace/docs/acceptance/`（git-crypt），再跑；≥5 卷须含 1 页卷、>120 页大卷、混合卷（部分页无水印）、45°/0.2 生成器卷；禁止事后挑选 | 预注册文件 + job 清单 | ④ |

**阶段 2' 的诚实边界（追加，必须进 PR 描述）**：

9. 判据是**文本 ∩ 几何**：正文字符集即使完全落在水印字符集里，只要不在水印字形框上就一律保留（AC22 单测钉住）。
   残余风险只剩「正文与水印字形物理重叠」这一形态——那种位置上文字已被水印盖住，本来就认不准；此时按几何丢弃，
   与今天「出一堆水印框」相比不劣化正文识别，但**确实**会少一个正文实例框。该风险写进 PR 描述，不做成静默行为。
10. 不变式用的「每页水印字符数中位数」取自**前 5 页探测**；若卷内水印密度前重后轻，后段页可能被不变式
    整页跳过（记 `pages_skipped_invariant`），表现为该页噪声未降——不是误删。
11. 族 A 卷（45°/0.2）仍被 #203 闸拒（闸吃原页），词级过滤对它们**没有收益也没有损失**；要让它们获益是
    阶段 2 的活，卡在 #204。
12. 本阶段不主张任何提速：族 B 的建链流 −36.9% 是**估计**，收益必须由 AC18/AC26 的 ④ 实测确认。
13. **族别标签的实测归属（回应门①.5 冷审「族别与卷名冲突」）**：`testdata/第四卷.pdf`（149 页）实测
    `dir=(0.7071,0.7071)`（+45°）、`opacity=0.2`、`NotoSansCJKsc-Regular` ⇒ **族 A**；私有语料 471 卷
    实测 `dir=(0.866,−0.5)`（−30°）、`opacity=0.3`、`STSong-Light` ⇒ **族 B**。两族逐卷列名见私有底稿 §1，
    不以族标签取卷。另：471+61 卷私有语料在**本机** `~/caseData`（只读），不在云实例——规模层脚本可在本地跑，
    结果落私有底稿（回应冷审「环境不可达」第 1 条）。

14. 1–2 页卷**永不剥**（探测要求 `pages_hit >= 3`）：这类卷今天噪声也最小，方向是「少做但不做错」；要覆盖需改探测口径，另开 issue。
15. `pages_hit` 口径是「本页候选字符 >0」，不强制这些字符属于 `top_sig`：多签名卷可能「页数够但签名主占比不够」，此时由 `top_sig_share >= 0.85` 兜底，**不会**把签名不占主导的层当水印。
16. `collect_watermark_rects` 在探测命中后再开一次 PDF 逐页取 `get_texttrace()`：族 B 31 页 0.08s、族 A 149 页 <1s（都在建链预算内），但**没有独立耗时上限**；千页级卷需按建流预算闸同款加超时（登记，不在本 PR）。
17. 覆盖率/截断率/块失败率这几道**后置**卷级门仍吃过滤后的流（C2 机制）：本 PR 只用「语料上零路由漂移（AC17b）+ 上报未过滤口径」证明没把今天被拒的卷放进来，**没有**证明「机制上不可能」；`truncated_ratio` / `map_failure_ratio` 分母变小只会更严，方向安全，但不是形式化证明。

## §8 未采用的方案与理由（防后人重走）

| 方案 | 否决理由 |
|---|---|
| 只渲染图像 XObject、跳过文本对象 | 多图/局部图/水印与图分离的版面一崩就废；且要自己实现 image → pixmap 的摆放与变换 |
| 渲染后按 alpha 反解混合（`under = (out - 0.2·gray)/0.8`） | 反锯齿边缘有损，本质是「修图」而不是「不画」；与「水印不在像素里」的事实不符 |
| 常开内存 Doc 就地 redact | PyMuPDF `Document` 非线程安全，与页并发渲染冲突；还要自己管缓存与清理 |
| 靠「串重复」识别水印（沿用 #203 判据） | #203 F13 已登记 5~9 字水印碎片维度不可见；版式签名判据精度更高且绕开该盲区 |
| 直接把污染层丢弃、整卷重 OCR | 放弃 130/149 页可用文本层，与「速度」目标相反 |
| 只在建链面剥离（strip-then-gate 单独落地） | 剥净卷 docline 接管后必在页 1 撞 300s 门限整卷清零 —— 复现 #203 灾难（见 §0） |
| **整份剥离副本送识别**（副本既供渲染又供建链） | 门④ 实测否掉：op 级擦除把不可见正文层一起擦掉 ⇒ 副本文本层实际为空（5.8 字/页）⇒ docline 弃用 ⇒ 族 B 卷掉进逐页 OCR，OCR 阶段 2.4×、整 job +14.6%。质量确实好（框 −90.7%、纯水印框 0），但代价是换路由 |
| docline 产出后按水印字符集丢框（零成本后处理） | 会误删真实正文：水印 4 字词本身就是该地市级检察机关的**正规简称**（本卷 62 个「混合框」里就混着真实机关全称，字符集与水印重叠），且完全不改善 OCR 文本质量；仅当需要「立刻止血」时可作为临时手段另立 Issue |

## §9 代码触点清单（阶段 1）

| 文件 | 改动 |
|---|---|
| `backend/app/services/visible_text_watermark.py` | **新增**：profile 探测（dict 口径）、平铺判据、净化副本构建（**trace 口径 op 级擦除**）、副本解析器与单飞/TTL |
| `backend/app/services/file_parser.py:618-660` | `get_pdf_page_image(..., raw: bool = False)`：`raw=False` 时经解析器取副本 |
| `backend/app/services/vision_service.py:234,2162` | 零改动（走默认 `raw=False`） |
| `backend/app/services/vl_md_pipeline_service.py:192` | 零改动 |
| `backend/app/api/files.py:813` | 加 `raw=True`（预览看原件） |
| `backend/app/core/config.py` | §4 七个键 + 注释（含「为何是版式签名不是串重复」） |
| `backend/app/services/task_queue_pipelines.py` | 写 `pipeline_status["visible_watermark"]`（沿用无条件写块形态） |
| `backend/tests/test_visible_watermark*.py` | 新增：判据、反例、红性夹具、失败矩阵、取图矩阵、体积闸、可观测扫描 |
| `docs/issue-206-visible-watermark-strip-design.md` | 本文档 |
| `CHANGELOG.md` | 当天条目（阶段 1 收益与**明确不主张提速**） |

## §10 语料与测量底稿

真实案卷文件名、逐卷测量原始输出、改前后 job 的 diff 证据图，全部留在**私有仓**
`LexMask-workspace/docs/acceptance/issue206-语料与测量底稿.md`（git-crypt）。本文档只写形态与数字，
不写案卷名、不写水印字面量、不写绝对路径。

底稿对应章节：§8 = 门④ 云上 A/B 冒烟（两份 job 的原始数值、按类型表、文本样例对照、路由日志）；
§9 = docline 资格抽样（12 卷，复刻四道闸的只读脚本 `scripts/issue206/check_docline_eligibility.py`）。
脚本：`scripts/issue206/{scan_case_corpus,strip_sample_b,smoke_cloud_ab,smoke_cloud_single_arm,check_docline_eligibility}.py`。