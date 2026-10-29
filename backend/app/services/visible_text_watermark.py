"""可见平铺文本水印探测 + 建链流词级过滤（Issue#206 阶段 2'）。

背景（门④ 云上实测，2026-10-29）：私有案卷语料里的防泄漏水印**不是像素**，而是盖在
扫描图上的**倾斜半透明平铺文本层**。每页一次 text-showing op，把同一个 4 字词平铺
65 次。文本层文档级链（docline）会把这些词当正文送 NER ⇒ 一卷 31 页产出 5,935 个框，
其中 4,612 个（77.7%）是纯水印字符框 —— 既污染结果，也让用户看到满屏无效框。

本模块做两件**纯函数**的活：

1. `probe_visible_watermark()`：只看前几页的 `get_text("dict")`，按**版式签名**
   （倾斜 + 半透明 + 字体/角度/透明度一致 + 字号极差小 + 候选占可见文本比例高）
   判定「这卷有平铺水印」，并取出水印**字符集**。不读内容、不留字面量。
2. `filter_watermark_words()`：把词流里「字符集 ⊆ 水印字符集 且 本页重复 ≥ N 次」的
   词丢掉，把干净词流交给建链。

**为什么按 op/版式而不是按串匹配**：水印与正文同字体同色，字符级不可区分；平铺
特征（同词一页重复几十次、每页同形）才是判别信号。族 A（45°/alpha 0.2）与族 B
（−30°/alpha 0.3）两个生成器都被 `|dir.y| >= 0.2` + `0 < alpha < 220` 覆盖。

**硬约束（验收方案 v1.2 §1/§6）**：

- 过滤**只影响建链输入**，不改变任何资格闸：预算/质量/重复片段闸仍然吃**未过滤**的页
  ⇒ 本卷是否接管 docline 与今天完全一致（过滤只让流更小、更快，不会把「今天被拒的卷」
  放进来 —— 那是 #204 的账）；
- 任何异常 ⇒ 用未过滤的词流（= 今天的行为），记 `filter_failed`，绝不让过滤引入新失败；
- 输出面（`pipeline_status["docline_word_filter"]`）**只带计数与比例**，不带水印字面量
  （沿用 #203 AC4 的 PII 最小化口径）。
"""
from __future__ import annotations

import logging
import statistics
from collections import Counter
from dataclasses import dataclass, field

import fitz  # PyMuPDF

from app.services.text_layer_docline_service import TextLayerPage

logger = logging.getLogger(__name__)

# 版式判据的固定口径（与验收方案 §2 一致）。
_TILT_MIN_ABS_Y = 0.2  # |dir.y| 低于此值 = 水平层（正文 OCR 层 / 无旋转排版），不算水印
_ALPHA_VISIBLE_MIN = 1  # alpha <= 0 = 不可见层（内嵌 OCR 文本层），不剥
_ALPHA_OPAQUE_MAX = 220  # 近乎不透明 = 可能是正文，刻意不作为


@dataclass
class WatermarkProfile:
    """水印形态的**统计摘要**（不含字面量，可安全进日志/状态块）。"""

    wm_charset: frozenset[str]
    signature: tuple[str, float, float, float]  # (font, dir.x, dir.y, alpha_fraction)
    pages_probed: int
    pages_hit: int
    visible_share: float
    top_sig_share: float
    size_spread: float
    wm_chars_per_page: dict[int, int] = field(default_factory=dict)

    def to_stats(self) -> dict:
        """可观测面：只有计数与比例。"""
        return {
            "pages_probed": int(self.pages_probed),
            "pages_hit": int(self.pages_hit),
            "visible_share": round(float(self.visible_share), 4),
            "top_sig_share": round(float(self.top_sig_share), 4),
            "size_spread": round(float(self.size_spread), 4),
            "charset_size": len(self.wm_charset),
        }


def _span_signature(font: str, dir_x: float, dir_y: float, alpha: int) -> tuple[str, float, float, float]:
    return (font, round(dir_x, 3), round(dir_y, 3), round(float(alpha) / 255.0, 2))


def _probe_page(page: fitz.Page) -> dict:
    """单页统计：可见字符数 / 候选字符数 / 各签名计数与字号、字符集。"""
    visible = 0
    candidate = 0
    sig_chars: Counter = Counter()
    sig_sizes: dict[tuple, list[float]] = {}
    sig_charset: dict[tuple, set[str]] = {}
    for block in page.get_text("dict").get("blocks", []):
        for line in block.get("lines", []):
            dir_v = tuple(line.get("dir") or (1.0, 0.0))
            dir_y = float(dir_v[1]) if len(dir_v) > 1 else 0.0
            dir_x = float(dir_v[0]) if dir_v else 1.0
            for span in line.get("spans", []):
                text = str(span.get("text") or "")
                n_chars = len(text.strip())
                if n_chars <= 0:
                    continue
                alpha = int(span.get("alpha", 255) or 0)
                if alpha < _ALPHA_VISIBLE_MIN:
                    continue
                visible += n_chars
                if abs(dir_y) < _TILT_MIN_ABS_Y or alpha >= _ALPHA_OPAQUE_MAX:
                    continue
                candidate += n_chars
                key = _span_signature(str(span.get("font") or ""), dir_x, dir_y, alpha)
                sig_chars[key] += n_chars
                sig_sizes.setdefault(key, []).append(float(span.get("size") or 0.0))
                bucket = sig_charset.setdefault(key, set())
                bucket.update(ch for ch in text if ch.strip())
    return {
        "visible": visible,
        "candidate": candidate,
        "sig_chars": sig_chars,
        "sig_sizes": sig_sizes,
        "sig_charset": sig_charset,
    }


def probe_visible_watermark(file_path: str, *, max_pages: int | None = None) -> WatermarkProfile | None:
    """探测整卷形态；命中返回 profile，否则 None（= 今天的行为）。

    只读前 `max_pages` 页（默认 `VISIBLE_WATERMARK_PROBE_PAGES`=5）。加密卷由调用方
    已解密落盘（`ensure_pdf_accessible`），这里再遇到 `needs_pass` 就视为不可探测。
    """
    from app.core.config import settings

    limit = int(
        max_pages if max_pages is not None else getattr(settings, "VISIBLE_WATERMARK_PROBE_PAGES", 5)
    )
    min_hit_pages = int(getattr(settings, "VISIBLE_WATERMARK_MIN_PAGES", 3))
    min_visible_share = float(getattr(settings, "VISIBLE_WATERMARK_MIN_VISIBLE_SHARE", 0.5))
    min_top_sig_share = float(getattr(settings, "VISIBLE_WATERMARK_MIN_TOP_SIG_SHARE", 0.85))
    max_size_spread = float(getattr(settings, "VISIBLE_WATERMARK_MAX_SIZE_SPREAD", 2.0))

    visible_total = 0
    candidate_total = 0
    pages_hit = 0
    pages_probed = 0
    sig_chars_total: Counter = Counter()
    sig_sizes_all: dict[tuple, list[float]] = {}
    sig_charset_all: dict[tuple, set[str]] = {}
    per_page_sig: list[tuple[int, Counter]] = []

    with fitz.open(file_path) as doc:
        if doc.needs_pass:
            return None
        total = doc.page_count if limit <= 0 else min(doc.page_count, limit)
        for index in range(total):
            page = doc[index]
            if page.rect.width <= 0 or page.rect.height <= 0:
                continue
            pages_probed += 1
            stats = _probe_page(page)
            visible_total += int(stats["visible"])
            candidate_total += int(stats["candidate"])
            if int(stats["candidate"]) > 0:
                pages_hit += 1
            sig_chars_total.update(stats["sig_chars"])
            per_page_sig.append((int(page.number) + 1, Counter(stats["sig_chars"])))
            for key, sizes in stats["sig_sizes"].items():
                sig_sizes_all.setdefault(key, []).extend(sizes)
            for key, charset in stats["sig_charset"].items():
                sig_charset_all.setdefault(key, set()).update(charset)

    if pages_probed <= 0 or candidate_total <= 0 or not sig_chars_total:
        return None

    top_sig, top_chars = max(sig_chars_total.items(), key=lambda item: item[1])
    visible_share = float(candidate_total) / float(visible_total) if visible_total else 0.0
    top_sig_share = float(top_chars) / float(candidate_total)
    sizes = sig_sizes_all.get(top_sig) or []
    size_spread = (max(sizes) - min(sizes)) if sizes else 0.0
    charset = frozenset(ch for ch in (sig_charset_all.get(top_sig) or set()) if ch.strip())

    if (
        pages_hit < min_hit_pages
        or visible_share < min_visible_share
        or top_sig_share < min_top_sig_share
        or size_spread > max_size_spread
        or not charset
    ):
        return None
    profile = WatermarkProfile(
        wm_charset=charset,
        signature=top_sig,
        pages_probed=pages_probed,
        pages_hit=pages_hit,
        visible_share=visible_share,
        top_sig_share=top_sig_share,
        size_spread=float(size_spread),
        wm_chars_per_page={
            no: int(counter.get(top_sig, 0))
            for no, counter in per_page_sig
            if int(counter.get(top_sig, 0)) > 0
        },
    )
    logger.info(
        "visible_watermark probed: hit pages=%d/%d visible_share=%.3f top_sig=%.3f "
        "size_spread=%.2f charset=%d",
        pages_hit,
        pages_probed,
        visible_share,
        top_sig_share,
        size_spread,
        len(charset),
    )
    return profile


_GRID_CELL = 64.0


def _page_char_rects(page: fitz.Page) -> tuple[list[fitz.Rect], list[fitz.Rect]]:
    """**一次** `get_texttrace()` 同时产出 `(可见候选字形框, 正文层字形框)`。

    为什么要一次：云上实测（PyMuPDF 1.27 + 120 页卷）在**单独第二遍**调用 trace 时踩到
    解释器级崩溃 `Fatal Python error: none_dealloc: deallocating None`（C 扩展在回收
    trace 元组里的 `None` 字形框时双重释放）。一次遍历 + 立即转成 `fitz.Rect` 既省一遍
    C 侧分配/回收，也把「同一页两份几何」保证在同一快照上。

    `chars[i][3]` 偶有为空（无字形框的 op）；这里显式跳过，不把 `None` 留在引用链里。
    """
    rotation = int(getattr(page, "rotation", 0) or 0)
    rotate = page.rotation_matrix if rotation % 360 else None
    visible: list[fitz.Rect] = []
    body: list[fitz.Rect] = []
    for entry in page.get_texttrace():
        opacity = float(entry.get("opacity") or 0.0)
        is_visible = _ALPHA_VISIBLE_MIN / 255.0 < opacity < _ALPHA_OPAQUE_MAX / 255.0
        tilted = False
        if is_visible:
            direction = entry.get("dir") or (1.0, 0.0)
            tilted = len(direction) >= 2 and abs(float(direction[1])) >= _TILT_MIN_ABS_Y
        if not is_visible and not tilted:
            target = body       # opacity==0 或全不透明 ⇒ 正文层
        elif is_visible and tilted:
            target = visible    # 倾斜 + 半透明 ⇒ 水印候选
        else:
            continue            # 水平半透明 / 倾斜不透明：两头都不算（刻意不作为）
        for char in entry.get("chars") or ():
            if len(char) < 4:
                continue
            box = char[3]
            if box is None or len(box) != 4:
                continue
            rect = fitz.Rect(float(box[0]), float(box[1]), float(box[2]), float(box[3]))
            if rect.is_empty:
                continue
            if rotate is not None:
                rect = rect * rotate
            target.append(rect)
    return visible, body


def _visible_char_rects(page: fitz.Page) -> list[fitz.Rect]:
    """本页「可见候选 op」（倾斜 + 半透明）的**字形框**，页面空间坐标。

    实测（族 B 真卷 + PyMuPDF 1.27）：`get_texttrace()` 的 `chars[i][3]` 与
    `get_text("words")` **同一坐标空间**（原点左上、y 向下）。用**逐字形框**而不是
    `entry["bbox"]`：平铺水印一个 op 的 bbox 常覆盖大半页，拿它当几何会把整页正文判成水印。
    旋转页的坐标校正见 `_page_char_rects`（门⑥ 冷审 C1）。
    """
    return _page_char_rects(page)[0]


def _body_char_rects(page: fitz.Page) -> list[fitz.Rect]:
    """本页**正文层**（opacity==0 或全不透明）的字形框，用于判据的来源维度。"""
    return _page_char_rects(page)[1]


def _grid_index(boxes: list[fitz.Rect], cell: float = _GRID_CELL) -> dict[tuple[int, int], list[int]]:
    """把字形框按 64pt 网格分桶：词框只需要看自己覆盖的几个格子。"""
    grid: dict[tuple[int, int], list[int]] = {}
    for index, box in enumerate(boxes):
        for cx in range(int(box.x0 // cell), int(box.x1 // cell) + 1):
            for cy in range(int(box.y0 // cell), int(box.y1 // cell) + 1):
                grid.setdefault((cx, cy), []).append(index)
    return grid


def _covered_ratio(rect: fitz.Rect, boxes: list[fitz.Rect],
                   grid: dict[tuple[int, int], list[int]] | None = None,
                   cell: float = _GRID_CELL) -> float:
    """词框被**字形框并集**覆盖的面积比例（门⑥ 冷审 I4：必须并集，不能逐框累加）。

    逐框交集直接相加会把同一处被多个字形框重复计入，实测 28pt 时 1.000 vs 真并集 0.763、
    11pt 时 0.575 vs 0.462 ⇒ 判据系统性放宽，正是「正文被当水印删」的必要条件。
    这里用 x 方向的区间扫描求精确并集（每词只有几个字形框，代价可忽略）。
    """
    if rect.is_empty:
        return 0.0
    area = rect.width * rect.height
    if area <= 0:
        return 0.0
    if grid is None:
        candidates = boxes
    else:
        seen: set[int] = set()
        picked: list[int] = []
        for cx in range(int(rect.x0 // cell), int(rect.x1 // cell) + 1):
            for cy in range(int(rect.y0 // cell), int(rect.y1 // cell) + 1):
                for index in grid.get((cx, cy), ()):  # 跨格重复只取一次
                    if index not in seen:
                        seen.add(index)
                        picked.append(index)
        candidates = [boxes[index] for index in picked]
    clipped: list[tuple[float, float, float, float]] = []
    for box in candidates:
        inter = rect & box
        if not inter.is_empty and inter.width > 0 and inter.height > 0:
            clipped.append((inter.x0, inter.x1, inter.y0, inter.y1))
    if not clipped:
        return 0.0
    xs = sorted({x for item in clipped for x in (item[0], item[1])})
    covered = 0.0
    for left, right in zip(xs, xs[1:], strict=False):
        if right <= left:
            continue
        spans = sorted((item[2], item[3]) for item in clipped if item[0] <= left and item[1] >= right)
        if not spans:
            continue
        merged = 0.0
        cur_lo, cur_hi = spans[0]
        for lo, hi in spans[1:]:
            if lo > cur_hi:
                merged += cur_hi - cur_lo
                cur_lo, cur_hi = lo, hi
            else:
                cur_hi = max(cur_hi, hi)
        merged += cur_hi - cur_lo
        covered += merged * (right - left)
    return min(1.0, covered / area)


def collect_watermark_rects(
    file_path: str,
) -> tuple[dict[int, list[fitz.Rect]], dict[int, list[fitz.Rect]]]:
    """整卷每页字形框，返回 `(水印可见候选, 正文层)` 两份字典（1 起页号）。

    两份一起取保证同一次 `get_texttrace()` 读到的字形在判据里按来源区分（门⑥ 冷审 C1）。
    只读；加密卷由调用方已解密落盘。
    """
    visible: dict[int, list[fitz.Rect]] = {}
    body: dict[int, list[fitz.Rect]] = {}
    with fitz.open(file_path) as doc:
        if doc.needs_pass:
            return visible, body
        for index in range(doc.page_count):
            page = doc[index]
            if page.rect.width <= 0 or page.rect.height <= 0:
                continue
            found, body_found = _page_char_rects(page)
            if found:
                visible[index + 1] = found
            if body_found:
                body[index + 1] = body_found
    return visible, body


def _watermark_word_drops(
    page,
    charset: frozenset[str],
    min_repeat: int,
    boxes: list[fitz.Rect],
    cover_min: float,
    grid: dict[tuple[int, int], list[int]] | None = None,
    body_boxes: list[fitz.Rect] | None = None,
    body_grid: dict[tuple[int, int], list[int]] | None = None,
) -> tuple[set[int], int, int]:
    """本页该丢哪些词（按位置下标）、丢多少词、丢多少字符（纯计算，不改页）。

    判据是**文本 ∩ 几何**双条件（门①.5 冷审 D3 的处置）：候选词必须同时满足
    ①字符集 ⊆ 水印字符集、②字长 ≥2、③**词框被水印字形框覆盖 ≥ `cover_min`**；
    然后在本页内，只有「落在水印字形上的这些候选词」出现 ≥ `min_repeat` 次才丢。

    为什么要几何：族 B 卷的不可见正文层本身就是**对着带水印的扫描图 OCR 出来的**，
    里面同样有几十份水印同形词 —— 纯文本判据分不清「正文里的真实简称 2 份」与
    「OCR 把水印也认进来的 18 份」，会在真实简称同页出现 ≥8 次时把正文实例一起丢掉
    （实测口径：31 页卷 322 个正文 GT 词被丢，全部是水印串）。几何一叠上来，
    正文位置的实例一律保留（覆盖不到水印字形），水印位置的实例一律丢掉。
    """
    candidates: list[tuple[int, str]] = []
    for index, word in enumerate(page.words):
        text = word.text
        if len(text) < 2 or not charset.issuperset(set(text)):
            continue
        word_rect = fitz.Rect(word.x0, word.y0, word.x1, word.y1)
        if _covered_ratio(word_rect, boxes, grid) < cover_min:
            continue
        # 门⑥ 冷审 C1（承重）：**来源**维度。词框同时被正文层（opacity=0/全不透明）字形
        # 覆盖时，它是正文，不是水印 —— 正文 OCR 层也可能恰好写在水印字形的位置上
        # （同一引擎对着带水印的扫描图 OCR），只凭几何会把它删掉 ⇒ 漏打码。
        if body_boxes and _covered_ratio(word_rect, body_boxes, body_grid) >= cover_min:
            continue
        candidates.append((index, text))
    if not candidates:
        return set(), 0, 0
    counts = Counter(text for _index, text in candidates)
    eligible = {text for text, times in counts.items() if times >= min_repeat}
    if not eligible:
        return set(), 0, 0
    drop_indices = {index for index, text in candidates if text in eligible}
    chars_dropped = sum(len(page.words[index].text) for index in drop_indices)
    return drop_indices, len(drop_indices), chars_dropped


def _filter_code(
    *,
    pages_filtered: int,
    pages_skipped_invariant: int,
    pages_skipped_would_wiped: int,
    pages_skipped_no_geometry: int,
    pages_total: int,
) -> str:
    """机读码：必须能区分「真剥了 / 探测有但几何缺失 / 假阳性挡下 / 预算越界 / 无水印」。

    门⑥ 冷审 I3：几何缺失原本会落回 `no_watermark`，运维读成「本卷没水印」，
    实际是「有水印、拿不到几何、整卷没剥」——两件事必须用不同码。
    """
    if pages_filtered > 0:
        return "applied"
    if pages_skipped_would_wiped > 0:
        return "would_wiped_page"
    if pages_skipped_invariant > 0:
        return "filter_invariant_exceeded"
    if pages_total > 0 and pages_skipped_no_geometry >= pages_total:
        return "no_geometry"
    return "no_watermark"


def filter_watermark_words(
    pages: list[TextLayerPage],
    profile: WatermarkProfile | None,
    *,
    wm_rects: dict[int, list[fitz.Rect]] | None = None,
    body_rects: dict[int, list[fitz.Rect]] | None = None,
    min_repeat: int | None = None,
    max_drop_vs_wm: float | None = None,
    cover_min: float | None = None,
    min_keep_ratio: float | None = None,
) -> tuple[list[TextLayerPage], dict]:
    """把水印词从词流里去掉；返回新页表 + 只带计数的统计块。

    规则（四条同时成立才丢一个词，见验收方案 §7.1）：字符集 ⊆ 水印字符集 /
    字长 ≥ 2 / **词框被水印字形框覆盖 ≥ `cover_min`** / 本页落在水印字形上的
    同形词 ≥ `min_repeat` 次。几何条件是承重的（见 `_watermark_word_drops`）。

    每页不变式：被丢字符数不得超过「探测到的每页水印字符数中位数 ×
    `max_drop_vs_wm`」（默认 1.5），越界或会丢成空页 ⇒ **该页保持原样**并计入
    `pages_skipped_invariant`。口径刻意不用「该页总字符数占比」——薄页（正文
    <120 字）的水印占比天然 >0.5，用占比会把最需要过滤的薄页全部挡下。
    """
    from app.core.config import settings

    repeat = int(
        min_repeat if min_repeat is not None else getattr(settings, "VISIBLE_WATERMARK_WORD_MIN_REPEAT", 8)
    )
    max_ratio = float(
        max_drop_vs_wm
        if max_drop_vs_wm is not None
        else getattr(settings, "VISIBLE_WATERMARK_MAX_DROP_VS_WM_CHARS", 1.5)
    )
    cover = float(
        cover_min
        if cover_min is not None
        else getattr(settings, "VISIBLE_WATERMARK_WORD_COVER_MIN", 0.35)
    )
    keep_ratio = float(
        min_keep_ratio
        if min_keep_ratio is not None
        else getattr(settings, "VISIBLE_WATERMARK_MIN_KEEP_RATIO", 0.15)
    )

    stats = {
        "applied": False,
        "code": "no_watermark",
        "pages_total": len(pages),
        "pages_filtered": 0,
        "pages_skipped_invariant": 0,
        "pages_skipped_no_geometry": 0,
        "pages_skipped_would_wiped": 0,
        "words_dropped": 0,
        "chars_dropped": 0,
    }
    if profile is None or not profile.wm_charset or not pages:
        return pages, stats

    per_page = [n for n in profile.wm_chars_per_page.values() if n > 0]
    if not per_page:
        # 没有每页水印字符数就无从给不变式定界 ⇒ 宁可不剥
        stats["code"] = "filter_invariant_exceeded"
        return pages, stats
    drop_budget = int(round(float(max_ratio) * float(statistics.median(per_page))))

    out: list[TextLayerPage] = []
    total_chars = 0
    pages_filtered = 0
    pages_skipped = 0
    pages_skipped_no_geometry = 0
    pages_skipped_would_wiped = 0
    words_dropped_total = 0
    chars_dropped_total = 0
    for page in pages:
        page_chars = sum(len(word.text) for word in page.words)
        total_chars += page_chars
        boxes = (wm_rects or {}).get(page.no) or []
        if not boxes:
            # 没有本页水印字形就拿不到几何判据 ⇒ 这一页原样保留（宁可留噪声）
            out.append(page)
            pages_skipped_no_geometry += 1
            continue
        body_boxes = (body_rects or {}).get(page.no) or []
        drop, words_dropped, chars_dropped = _watermark_word_drops(
            page,
            profile.wm_charset,
            repeat,
            boxes,
            cover,
            _grid_index(boxes),
            body_boxes=body_boxes,
            body_grid=_grid_index(body_boxes) if body_boxes else None,
        )
        if not drop:
            out.append(page)
            continue
        kept = [word for index, word in enumerate(page.words) if index not in drop]
        if not kept or page_chars <= 0 or chars_dropped > drop_budget:
            # 不变式越界：这一页原样保留（宁可留噪声，不可误删正文）
            out.append(page)
            pages_skipped += 1
            continue
        kept_chars = page_chars - chars_dropped
        if kept_chars < int(round(keep_ratio * page_chars)):
            # 页级保留下限（门⑥ 冷审 I2/I6）：这页几乎全是候选词 ⇒ 更可能是「正文本身
            # 就写成倾斜半透明平铺」的假阳性，或薄页里 OCR 把水印认成了一大片。
            # 与预算越界**分开计数**，避免两种原因在运维口径里混成一个。
            out.append(page)
            pages_skipped_would_wiped += 1
            continue
        out.append(
            TextLayerPage(no=page.no, width=page.width, height=page.height, words=kept)
        )
        pages_filtered += 1
        words_dropped_total += words_dropped
        chars_dropped_total += chars_dropped

    stats.update(
        {
            "applied": pages_filtered > 0,
            "code": _filter_code(
                pages_filtered=pages_filtered,
                pages_skipped_invariant=pages_skipped,
                pages_skipped_would_wiped=pages_skipped_would_wiped,
                pages_skipped_no_geometry=pages_skipped_no_geometry,
                pages_total=len(pages),
            ),
            "pages_filtered": pages_filtered,
            "pages_skipped_invariant": pages_skipped,
            "pages_skipped_no_geometry": pages_skipped_no_geometry,
            "pages_skipped_would_wiped": pages_skipped_would_wiped,
            "words_dropped": words_dropped_total,
            "chars_dropped": chars_dropped_total,
            "dropped_ratio": round(
                float(chars_dropped_total) / float(total_chars), 4
            ) if total_chars else 0.0,
            "profile": profile.to_stats(),
        }
    )
    if pages_filtered:
        logger.info(
            "visible_watermark words filtered: pages=%d/%d words=%d chars=%d ratio=%.3f skipped=%d",
            pages_filtered,
            stats["pages_total"],
            words_dropped_total,
            chars_dropped_total,
            stats["dropped_ratio"],
            pages_skipped,
        )
    return out, stats


def maybe_filter_pages(file_path: str, pages: list[TextLayerPage]) -> tuple[list[TextLayerPage], dict]:
    """编排层入口：开关关 / 探测不中 / 任何异常 ⇒ 原样返回未过滤页。

    失败一律**回落未过滤页**（= 今天的行为），只记 `code`，绝不抛给上层。
    """
    from app.core.config import settings

    if not bool(getattr(settings, "VISIBLE_WATERMARK_WORD_FILTER_ENABLED", False)):
        return pages, {"applied": False, "code": "disabled", "pages_total": len(pages)}
    try:
        profile = probe_visible_watermark(file_path)
        if profile is None:
            return pages, {
                "applied": False,
                "code": "no_watermark",
                "pages_total": len(pages),
            }
        # 几何判据是承重条件（见 `_watermark_word_drops` 与 §7.1）：没有本页水印字形框
        # 就无法把「正文位置的同形词」与「水印位置的同形词」分开，宁可不剥。
        wm_rects, body_rects = collect_watermark_rects(file_path)
        return filter_watermark_words(
            pages, profile, wm_rects=wm_rects, body_rects=body_rects
        )
    except Exception as exc:  # noqa: BLE001 - 过滤失败绝不引入新失败
        logger.warning("visible_watermark word filter failed: %s", type(exc).__name__)
        return pages, {
            "applied": False,
            "code": "filter_failed",
            "pages_total": len(pages),
            "error": type(exc).__name__,
        }
