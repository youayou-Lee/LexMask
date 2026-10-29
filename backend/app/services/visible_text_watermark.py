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


def _visible_char_rects(page: fitz.Page) -> list[fitz.Rect]:
    """本页所有「可见候选 op」（倾斜 + 半透明）的**字形框**，页面空间坐标。

    实测（族 B 真卷 + PyMuPDF 1.27）：`get_texttrace()` 的 `chars[i][3]` 与
    `get_text("words")` **同一坐标空间**（原点在页左上、y 向下；例如字形框
    y=3479..3520 与词框 y=3434..3520 的行对齐），**不需要 `height - y` 翻转**。
    （阶段 1 的笔记里记过「chars 在 PDF 空间」，那是另一份文档的坐标系记忆；这里
    以实测为准。若某天坐标系变了，覆盖率会整体趋 0 ⇒ 该页不剥，失败方向安全。）

    用**逐字形框**而不是 `entry["bbox"]`：平铺水印一个 op 的 bbox 常常覆盖大半页
    （斜行 + 越出页边），拿它当几何会把整页正文都判成水印。
    """
    out: list[fitz.Rect] = []
    for entry in page.get_texttrace():
        opacity = float(entry.get("opacity") or 0.0)
        if not (_ALPHA_VISIBLE_MIN / 255.0 < opacity < _ALPHA_OPAQUE_MAX / 255.0):
            continue
        direction = entry.get("dir") or (1.0, 0.0)
        if len(direction) < 2 or abs(float(direction[1])) < _TILT_MIN_ABS_Y:
            continue
        for char in entry.get("chars") or ():
            box = char[3]
            if len(box) != 4:
                continue
            rect = fitz.Rect(float(box[0]), float(box[1]), float(box[2]), float(box[3]))
            if rect.is_empty:
                continue
            out.append(rect)
    return out


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
    """词框被水印字形框覆盖的面积比例（重叠按逐框裁剪累加，上限 1.0）。"""
    if rect.is_empty:
        return 0.0
    area = rect.width * rect.height
    if area <= 0:
        return 0.0
    covered = 0.0
    if grid is None:
        candidates = boxes
    else:
        seen: set[int] = set()
        picked: list[int] = []
        for cx in range(int(rect.x0 // cell), int(rect.x1 // cell) + 1):
            for cy in range(int(rect.y0 // cell), int(rect.y1 // cell) + 1):
                for index in grid.get((cx, cy), ()):  # 跨格重复只算一次
                    if index not in seen:
                        seen.add(index)
                        picked.append(index)
        candidates = [boxes[index] for index in picked]
    for box in candidates:
        inter = rect & box
        if not inter.is_empty:
            covered += inter.width * inter.height
    return min(1.0, covered / area)


def collect_watermark_rects(file_path: str) -> dict[int, list[fitz.Rect]]:
    """整卷每页的水印字形框（1 起页号）。只读；加密卷由调用方已解密落盘。"""
    rects: dict[int, list[fitz.Rect]] = {}
    with fitz.open(file_path) as doc:
        if doc.needs_pass:
            return rects
        for index in range(doc.page_count):
            page = doc[index]
            if page.rect.width <= 0 or page.rect.height <= 0:
                continue
            found = _visible_char_rects(page)
            if found:
                rects[index + 1] = found
    return rects


def _watermark_word_drops(
    page,
    charset: frozenset[str],
    min_repeat: int,
    boxes: list[fitz.Rect],
    cover_min: float,
    grid: dict[tuple[int, int], list[int]] | None = None,
) -> tuple[set[int], int, int]:
    """本页该丢哪些词（按位置下标）、丢多少词、丢多少字符（纯计算，不改页）。

    判据是**文本 ∩ 几何**双条件（门①.5 冷审 D3 的处置）：候选词必须同时满足
    ①字符集 ⊆ 水印字符集、②字长 ≥2、③**词框被水印字形框覆盖 ≥ `cover_min`**；
    然后在本页内，只有「落在水印字形上的这些候选词」出现 ≥ `min_repeat` 次才丢。

    为什么要几何：族 B 卷的不可见正文层本身就是**对着带水印的扫描图 OCR 出来的**，
    里面同样有几十份 `…市院` —— 纯文本判据分不清「正文里的真实简称 2 份」与
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


def filter_watermark_words(
    pages: list[TextLayerPage],
    profile: WatermarkProfile | None,
    *,
    wm_rects: dict[int, list[fitz.Rect]] | None = None,
    min_repeat: int | None = None,
    max_drop_vs_wm: float | None = None,
    cover_min: float | None = None,
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

    stats = {
        "applied": False,
        "code": "no_watermark",
        "pages_total": len(pages),
        "pages_filtered": 0,
        "pages_skipped_invariant": 0,
        "pages_skipped_no_geometry": 0,
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
        drop, words_dropped, chars_dropped = _watermark_word_drops(
            page, profile.wm_charset, repeat, boxes, cover, _grid_index(boxes)
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
        out.append(
            TextLayerPage(no=page.no, width=page.width, height=page.height, words=kept)
        )
        pages_filtered += 1
        words_dropped_total += words_dropped
        chars_dropped_total += chars_dropped

    stats.update(
        {
            "applied": pages_filtered > 0,
            "code": (
                "applied"
                if pages_filtered > 0
                else ("filter_invariant_exceeded" if pages_skipped > 0 else "no_watermark")
            ),
            "pages_filtered": pages_filtered,
            "pages_skipped_invariant": pages_skipped,
            "pages_skipped_no_geometry": pages_skipped_no_geometry,
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
        wm_rects = collect_watermark_rects(file_path)
        return filter_watermark_words(pages, profile, wm_rects=wm_rects)
    except Exception as exc:  # noqa: BLE001 - 过滤失败绝不引入新失败
        logger.warning("visible_watermark word filter failed: %s", type(exc).__name__)
        return pages, {
            "applied": False,
            "code": "filter_failed",
            "pages_total": len(pages),
            "error": type(exc).__name__,
        }
