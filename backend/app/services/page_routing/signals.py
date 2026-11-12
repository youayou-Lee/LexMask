"""Issue#273 Task 5：L1/L2 页级信号采集器 + 文件级 memo。

页级路由的信号源：``collect_l1`` 采文本面指标（字符量/垃圾率/词长/字体表/阅读序/
页内复读）+ **图像覆盖闸**（B3 修复后随 memo 一次采齐——判定序规则 5.5 需要在
灰区之前看到「整页图+内嵌层」，不能等灰区才触发的 L2 补采；口径=最大单图未剪裁
面积占比，与执行层 ``_is_scanned_page`` 同谓词），``collect_l2``
采渲染面指标（词框墨迹中位数/图像覆盖闸，后者保留返回以向后兼容）。

设计要点：

* **绝不抛**：两个入口对任何输入（文件不存在/损坏/加密无密码/页号越界/非 PDF）
  都返回 ``probe_error=True`` 的 :class:`PageSignals` 或 ``(None, None)``，
  任何异常都被吞进 fail-closed 兜底；
* **文件级 memo**：key=(realpath, mtime_ns, size)，LRU≤128（OrderedDict）——
  页级路由逐页采集，同一文件版本只 probe 一次；失败结果同样入缓存
  （文件被修复/替换时 mtime_ns 或 size 必变 ⇒ 自动换新 key）；
* 本模块是 ``page_routing`` 包内**唯一**允许 ``import fitz`` 的模块
  （judge/arbiter/state 都是纯逻辑）；
* ``chars/garbage_ratio/short_word_ratio/avg_word_len/max_word_len`` 复用
  :func:`app.services.text_layer_quality.compute_metrics` 的**单页**口径
  （``compute_metrics([[本页词文本]])``）；
* ``reading_order_agreement``：词序列（内容流序）与「行聚类（y 中心 ±0.5×词高
  同行）后行内按 x0 排序」序列的相邻对一致率（多重集合口径，容忍行间置换）；
* ``page_repeat_ratio``：页内 ≥6 字串出现 ≥8 次的字符占比——6 字滑窗计数器
  （≥6 字超串每出现一次，其全部 6 字窗口各出现一次 ⇒ 6 字窗口口径不多不少地
  覆盖所有合格串）；
* ``collect_l2`` 墨迹算法 = :func:`app.services.text_layer_docline_service.
  sample_ink_alignment` 的单页化重写（词框 ≤400、暗像素阈值 160、取中位数），
  独立实现、**不 import 其私有函数**。
"""

from __future__ import annotations

import os
import threading
from collections import Counter, OrderedDict
from dataclasses import dataclass, field

import fitz  # PyMuPDF

from app.services.page_routing.types import PageSignals
from app.services.text_layer_quality import compute_metrics

# --------------------------------------------------------------------------- memo

_MEMO_MAX = 128
_MEMO: OrderedDict[tuple[str, int, int], _DocProbe] = OrderedDict()
_MEMO_LOCK = threading.Lock()

_REPEAT_MIN_LEN = 6  # 页内重复串的最短字数
_REPEAT_MIN_HITS = 8  # 判定重复所需的最少出现次数
_INK_DARK_THRESHOLD = 160  # 词框暗像素阈值（与 sample_ink_alignment 同口径）
_INK_MAX_WORD_BOXES = 400  # 墨迹采样的词框上限


@dataclass
class _PageData:
    """单页 probe 结果。``error=True`` ⇒ 该页提取失败（fail-closed 退 ocr）。"""

    words: list[tuple[float, float, float, float, str]] = field(default_factory=list)
    word_texts: list[str] = field(default_factory=list)
    fonts: list[tuple] = field(default_factory=list)
    width: float = 0.0
    height: float = 0.0
    coverage: float | None = None  # 图像覆盖闸=最大单图未剪裁面积占比（B3 修复后 L1 即采，判定序 5.5 的输入面）
    error: bool = False


@dataclass
class _DocProbe:
    """一次文件级 probe 的完整结果（memo 缓存体）。

    ``error`` 语义：``None``=正常；``"not_pdf"``=非 PDF（is_pdf=False）；
    其余非空值=打不开/加密无密码（probe_error）。
    """

    is_pdf: bool = True
    page_count: int = 0
    pages: dict[int, _PageData] = field(default_factory=dict)
    error: str | None = None


def _probe(file_path: str) -> _DocProbe:
    """打开并整卷提取词框/字体表。任何异常都折算成 error 标记，不外抛。"""
    probe = _DocProbe()
    try:
        with fitz.open(file_path) as doc:
            if not doc.is_pdf:
                probe.is_pdf = False
                probe.error = "not_pdf"
                return probe
            if doc.needs_pass:
                # 无密码口令可用 ⇒ 加密无密码，fail-closed。
                probe.error = "encrypted"
                return probe
            probe.page_count = doc.page_count
            for i in range(doc.page_count):
                pd = _PageData()
                try:
                    page = doc[i]
                    pd.words = [(x0, y0, x1, y1, str(text)) for x0, y0, x1, y1, text, *_rest in page.get_text("words")]
                    pd.word_texts = [w[4] for w in pd.words]
                    # (xref, ext, type, basefont, name, encoding[, referencer])：
                    # ext != "n/a" 判嵌入；type 含 "Type3" 判 Type3。
                    pd.fonts = list(page.get_fonts(full=True))
                    rect = page.rect
                    pd.width = float(rect.width)
                    pd.height = float(rect.height)
                    # coverage 随 memo 一次采齐（get_image_info 毫秒级，A3 <20ms 判据安全）；
                    # 单项失败只降级为 None，不污染该页的词框/字体提取。
                    try:
                        pd.coverage = _max_image_coverage(page, pd.width, pd.height)
                    except Exception:
                        pd.coverage = None
                except Exception:
                    # 单页提取失败只记该页，不污染整文件缓存。
                    pd.error = True
                probe.pages[i + 1] = pd  # 页号 1-based，与 collect_l1 入参同口径
    except Exception:
        probe.error = "open_failed"
    return probe


def _load(file_path: str) -> _DocProbe | None:
    """文件级 memo 读取；stat 失败（文件不存在/不可读）返回 None 且不入缓存。"""
    try:
        st = os.stat(file_path)
    except OSError:
        return None
    key = (os.path.realpath(file_path), st.st_mtime_ns, st.st_size)
    with _MEMO_LOCK:
        cached = _MEMO.get(key)
        if cached is not None:
            _MEMO.move_to_end(key)
            return cached
    probe = _probe(file_path)
    with _MEMO_LOCK:
        _MEMO[key] = probe
        _MEMO.move_to_end(key)
        while len(_MEMO) > _MEMO_MAX:
            _MEMO.popitem(last=False)
    return probe


# --------------------------------------------------------------------------- L1


def collect_l1(file_path: str, page: int) -> PageSignals:
    """L1 文本面信号采集。任何失败 ⇒ probe_error=True，绝不抛。"""
    try:
        probe = _load(file_path)
        if probe is None:
            # 文件不存在/不可读（stat 失败）。
            return PageSignals(page=page, probe_error=True)
        if probe.error == "not_pdf":
            # 分类明确：非 PDF。probe_error 不置位（不是采集器故障），
            # 判官按 not_pdf → ocr。
            return PageSignals(page=page, is_pdf=False)
        if probe.error is not None:
            # 打不开（损坏）或加密无密码。
            return PageSignals(page=page, probe_error=True)
        pd = probe.pages.get(page)
        if pd is None or pd.error:
            # 页号越界（含 <1）/该页提取失败。
            return PageSignals(page=page, probe_error=True)
        return _signals_from_page(page, pd)
    except Exception:  # pragma: no cover - 双保险：任何意外路径都 fail-closed
        return PageSignals(page=page, probe_error=True)


def _signals_from_page(page: int, pd: _PageData) -> PageSignals:
    m = compute_metrics([pd.word_texts])
    n_fonts = len(pd.fonts)
    embedded_ratio: float | None = None
    type3_ratio: float | None = None
    if n_fonts:
        embedded = sum(1 for f in pd.fonts if str(f[1]) != "n/a")
        type3 = sum(1 for f in pd.fonts if "Type3" in str(f[2]))
        embedded_ratio = embedded / n_fonts
        type3_ratio = type3 / n_fonts
    return PageSignals(
        page=page,
        is_pdf=True,
        chars=m.char_count,
        garbage_ratio=m.garbage_char_ratio,
        short_word_ratio=m.short_word_ratio,
        avg_word_len=m.avg_word_len,
        max_word_len=m.max_word_len,
        word_count=m.word_count,
        font_kinds=n_fonts,
        embedded_ratio=embedded_ratio,
        type3_ratio=type3_ratio,
        image_coverage=pd.coverage,
        reading_order_agreement=_reading_order_agreement(pd.words),
        page_repeat_ratio=_page_repeat_ratio(pd.word_texts),
    )


def _reading_order_agreement(words: list[tuple[float, float, float, float, str]]) -> float:
    """内容流序 vs 视觉阅读序的相邻对一致率；词数 <2 ⇒ 1.0。

    视觉序 = 行聚类（y 中心相差 ≤0.5×词高 同行）后行内按 x0 升序、行间按 y 升序。
    相邻对按多重集合相交（容忍行间整体置换，仍能抓住行内乱序/交叠排版）。
    """
    if len(words) < 2:
        return 1.0
    stream = [w[4] for w in words]
    visual: list[str] = []
    for line in _cluster_lines(words):
        visual.extend(w[4] for w in line)
    stream_pairs = Counter(zip(stream, stream[1:], strict=False))
    visual_pairs = Counter(zip(visual, visual[1:], strict=False))
    common = sum((stream_pairs & visual_pairs).values())
    total = len(stream) - 1
    return common / total if total else 1.0


def _cluster_lines(words: list[tuple[float, float, float, float, str]]) -> list[list[tuple]]:
    """y 中心聚类行（与行中心差 ≤0.5×词高 归同行），行内按 x0、行间按 y 升序。"""
    ordered = sorted(words, key=lambda w: (w[1] + w[3]) / 2.0)
    lines: list[list[tuple]] = []
    cur: list[tuple] = []
    cur_center = 0.0
    for w in ordered:
        center = (w[1] + w[3]) / 2.0
        height = abs(w[3] - w[1])
        tolerance = 0.5 * (height if height > 0 else 1e-6)
        if cur and abs(center - cur_center) <= tolerance:
            cur.append(w)
            # 行中心取运行均值，防止逐词漂移把远行链进来。
            cur_center += (center - cur_center) / len(cur)
        else:
            if cur:
                lines.append(cur)
            cur = [w]
            cur_center = center
    if cur:
        lines.append(cur)
    for line in lines:
        line.sort(key=lambda w: w[0])
    lines.sort(key=lambda line: min(w[1] for w in line))
    return lines


def _page_repeat_ratio(word_texts: list[str]) -> float:
    """页内 ≥6 字重复串（出现 ≥8 次）的字符占比（6 字滑窗计数器）。"""
    text = "".join(word_texts)
    n = len(text)
    if n < _REPEAT_MIN_LEN:
        return 0.0
    win = _REPEAT_MIN_LEN
    hits: dict[str, list[int]] = {}
    for i in range(n - win + 1):
        hits.setdefault(text[i : i + win], []).append(i)
    covered = bytearray(n)
    for idxs in hits.values():
        if len(idxs) >= _REPEAT_MIN_HITS:
            for i in idxs:
                covered[i : i + win] = b"\x01" * win
    total = sum(covered)
    return total / n if n else 0.0


# --------------------------------------------------------------------------- L2


def collect_l2(file_path: str, page: int, dpi: int) -> tuple[float | None, float | None]:
    """L2 渲染面信号：(词框墨迹中位数, 图像覆盖闸)。绝不抛。

    整体失败（文件打不开/非 PDF/页缺失）⇒ ``(None, None)``；单项失败
    （numpy 缺失、页上无有效词框、coverage 计算异常）时仅该项为 ``None``、
    另一项照常返回。
    """
    try:
        probe = _load(file_path)
        if probe is None or probe.error is not None or not probe.is_pdf:
            return (None, None)
        pd = probe.pages.get(page)
        if pd is None or pd.error or pd.width <= 0 or pd.height <= 0:
            return (None, None)
        ink: float | None = None
        coverage: float | None = None
        with fitz.open(file_path) as doc:
            if doc.needs_pass:
                return (None, None)
            pg = doc[page - 1]
            try:
                coverage = _max_image_coverage(pg, pd.width, pd.height)
            except Exception:
                coverage = None
            try:
                ink = _page_ink(pg, pd, dpi)
            except Exception:
                ink = None
        return (ink, coverage)
    except Exception:  # pragma: no cover - 双保险：任何意外路径都 fail-closed
        return (None, None)


def _max_image_coverage(pg: fitz.Page, width: float, height: float) -> float | None:
    """最大单图未剪裁面积占比——与 file_parser._is_scanned_page 同谓词，消除两闸语义差。

    取「单张图 bbox 的 ``abs()``（未剪裁面积）/ 页面积」的最大值：不与页 rect 求交
    ⇒ 90° 旋转页（图 bbox 与旋转后页 rect 错位，交集只剩 ~0.7）仍得 1.0，
    与执行层 ``abs(rect) >= SCAN_PAGE_IMAGE_COVERAGE_RATIO × 页面积`` 的判定
    完全一致；无图 ⇒ 0.0（封顶 1.0 只影响展示值，不影响 ≥0.9 判定方向）；
    页面积非法 ⇒ None。
    """
    page_area = width * height
    if page_area <= 0:
        return None
    max_ratio = 0.0
    for info in pg.get_image_info():
        bbox = info.get("bbox")
        if not bbox:
            continue
        max_ratio = max(max_ratio, abs(fitz.Rect(bbox)) / page_area)
    return min(1.0, max_ratio)


def _page_ink(pg: fitz.Page, pd: _PageData, dpi: int) -> float | None:
    """词框（≤400）内暗像素率的中位数——sample_ink_alignment 的单页化。

    numpy 缺失或页上无有效词框 ⇒ None（判定降级为纯文本指标）。
    """
    try:
        import numpy as np
    except Exception:  # pragma: no cover - numpy 是既有依赖，缺失时静默降级
        return None
    if not pd.words:
        return None
    pix = pg.get_pixmap(dpi=dpi)
    arr = np.frombuffer(pix.samples, dtype=np.uint8)
    arr = arr.reshape(pix.height, pix.width, pix.n)[:, :, :3].mean(axis=2)
    scale = dpi / 72.0
    values: list[float] = []
    for x0, y0, x1, y1, _text in pd.words[:_INK_MAX_WORD_BOXES]:
        cx0 = max(0, int(x0 * scale))
        cy0 = max(0, int(y0 * scale))
        cx1 = min(pix.width, int(x1 * scale))
        cy1 = min(pix.height, int(y1 * scale))
        if cx1 <= cx0 or cy1 <= cy0:
            continue
        sub = arr[cy0:cy1, cx0:cx1]
        if sub.size:
            values.append(float((sub < _INK_DARK_THRESHOLD).mean()))
    if not values:
        return None
    values.sort()
    return values[len(values) // 2]
