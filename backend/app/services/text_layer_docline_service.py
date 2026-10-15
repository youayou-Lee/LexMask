"""扫描件内嵌文本层 → 文档级分块 NER 链（Issue#115 C2）。

现状：扫描页即使带**可用的**内嵌文本层，也会被 `vision_service` 主动拒绝并回落逐页
图像 OCR + HaS 多趟（实测 24.1s/页）。本模块提供另一条路：整卷文本层一次抽出 →
按段分块 → HaS 一次问多个实体 → 值回映到 (页号, 包围框)。

设计要点（与验收方案 v2 对应）：

- **抽取**用 PyMuPDF 的词框，并按 `page.rotation_matrix` 校正旋转（不校正会让
  rot 90/270 的卷得出「完全不对齐」的假结论，见 `docs/acceptance/` 对齐报告）；
- **分块**按词边界切，带重叠，保证跨块边界的实体不被切碎（验收 D9）；
- **多趟**：一趟主查（全类型）→ residual 二次问（把本块里**未被任何已识别值覆盖**
  的文字再问一次，语义对齐页线的 residual 机制）。单趟主查实测只有页线召回的 73%；
- **回映**：NFKC 归一 + 去空白后匹配（真卷里有全角 `1－17` 对半角 `1-17` 的坑），
  匹配到词序列后取并集框；同一份流上定位，**因此与分块边界无关**；
- **复用**而非重写：模型调用一律走 `has_client.ner`（自带缓存、闸门、帽容量分批、
  解析与重试），不另起一套请求逻辑。

本模块只做「算出结果」；写回 `file_store` 与开关判定在编排层（C3）。
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
import unicodedata
from dataclasses import dataclass, field

import fitz  # PyMuPDF

from app.core.config import settings
from app.core.gpu_inference_gate import shared_gpu_inference_slot

logger = logging.getLogger(__name__)

_WORD_SEP = " "
_MIN_RESIDUAL_CHARS = 80


@dataclass
class TextLayerWord:
    x0: float
    y0: float
    x1: float
    y1: float
    text: str


@dataclass
class TextLayerPage:
    no: int  # 1-based
    width: float
    height: float
    words: list[TextLayerWord] = field(default_factory=list)

    @property
    def text(self) -> str:
        return _WORD_SEP.join(w.text for w in self.words if w.text)


@dataclass
class DocLineEntity:
    page: int
    type: str
    value: str
    bbox: tuple[float, float, float, float]  # 归一化 x0,y0,x1,y1


@dataclass
class DocLineResult:
    pages: int
    chunks: int
    chunk_chars: int
    stream_chars: int
    layer_chars: int
    covered_chars: int
    entities: list[DocLineEntity]
    ner_calls: int
    truncated_chunks: int
    http_errors: int
    residual_calls: int
    seconds: float
    failed_chunks: int = 0
    map_failures: list[dict] = field(default_factory=list)

    @property
    def map_failure_ratio(self) -> float:
        """回映失败率（门⑥ 二轮 Important#5）：分母 = 成功实体数 + 失败项数。"""
        total = len(self.map_failures)
        denom = len(self.entities) + total
        return (total / denom) if denom else 0.0

    @property
    def coverage_ratio(self) -> float:
        """分子 = **并集**覆盖的字符（重叠块不重复计）；分母 = 文档流总长。

        文档流由词 + 词间分隔符拼成；分隔符是我们插的，所以判据按流长算，
        另报 `layer_chars`（PDF 文本层自身字符数）供交叉核对。见验收 D2。
        """
        if self.stream_chars <= 0:
            return 0.0
        return self.covered_chars / self.stream_chars


# --------------------------------------------------------------------------- 抽取


def _word_boxes(page: fitz.Page) -> list[TextLayerWord]:
    """词框，按旋转校正后归一化前的「页面坐标」（与 `page.rect` 同空间）。"""
    rotation = int(getattr(page, "rotation", 0) or 0)
    matrix = page.rotation_matrix if rotation % 360 else None
    out: list[TextLayerWord] = []
    for x0, y0, x1, y1, text, *_rest in page.get_text("words"):
        rect = fitz.Rect(x0, y0, x1, y1)
        if matrix is not None:
            rect = rect * matrix
        cleaned = str(text or "").strip()
        if cleaned:
            out.append(TextLayerWord(rect.x0, rect.y0, rect.x1, rect.y1, cleaned))
    out.sort(key=lambda w: (round(w.y0, 1), w.x0))
    return out


def extract_text_layer_pages(
    file_path: str,
    *,
    max_pages: int | None = None,
    password: str | None = None,
) -> list[TextLayerPage]:
    """抽出逐页词框。`max_pages` 之外不读（大卷分段落盘，见验收 G9）。"""
    limit = int(max_pages if max_pages is not None else settings.TEXT_LAYER_DOCLINE_MAX_PAGES)
    pages: list[TextLayerPage] = []
    with fitz.open(file_path) as doc:
        if doc.needs_pass:
            if not password or doc.authenticate(password) <= 0:
                raise ValueError("encrypted_pdf_without_password")
        total = doc.page_count if limit <= 0 else min(doc.page_count, limit)
        for index in range(total):
            page = doc[index]
            rect = page.rect
            if rect.width <= 0 or rect.height <= 0:
                continue
            pages.append(
                TextLayerPage(
                    no=index + 1,
                    width=float(rect.width),
                    height=float(rect.height),
                    words=_word_boxes(page),
                )
            )
    return pages


def sample_ink_alignment(
    file_path: str,
    pages: list[TextLayerPage],
    *,
    dpi: int = 150,
    sample_pages: int | None = None,
    password: str | None = None,
) -> float | None:
    """抽样估「词框内是否真有墨」的中位数。

    这是 `vision_service` 当初拒绝内嵌文本层时担心的真实风险（层与图不一致）。
    真卷实测 0.1183、随机空白 0.0000；合成对齐样本 0.1778、下移半行 0.0614。
    没有 numpy 时返回 None（判定退化为纯文本指标）。
    """
    try:
        import numpy as np
    except Exception:  # pragma: no cover - numpy 是既有依赖，缺失时静默降级
        return None
    if not pages:
        return None
    count = int(sample_pages if sample_pages is not None else settings.TEXT_LAYER_INK_SAMPLE_PAGES)
    picks = pages[: max(1, count)]
    scale = dpi / 72.0
    values: list[float] = []
    try:
        with fitz.open(file_path) as doc:
            if doc.needs_pass and password and doc.authenticate(password) <= 0:
                return None
            for page_meta in picks:
                if page_meta.no > doc.page_count:
                    continue
                page = doc[page_meta.no - 1]
                pix = page.get_pixmap(dpi=dpi)
                arr = np.frombuffer(pix.samples, dtype=np.uint8)
                arr = arr.reshape(pix.height, pix.width, pix.n)[:, :, :3].mean(axis=2)
                for word in page_meta.words[:400]:
                    x0 = max(0, int(word.x0 * scale))
                    y0 = max(0, int(word.y0 * scale))
                    x1 = min(pix.width, int(word.x1 * scale))
                    y1 = min(pix.height, int(word.y1 * scale))
                    if x1 <= x0 or y1 <= y0:
                        continue
                    sub = arr[y0:y1, x0:x1]
                    if sub.size:
                        values.append(float((sub < 160).mean()))
    except Exception as exc:  # pragma: no cover - 采样失败不阻断主链
        logger.warning("ink alignment sampling failed: %s", exc)
        return None
    if not values:
        return None
    values.sort()
    return values[len(values) // 2]


# --------------------------------------------------------------------------- 分块


@dataclass
class _WordSpan:
    page: int
    start: int
    end: int
    word: TextLayerWord


def build_stream(pages: list[TextLayerPage]) -> tuple[str, list[_WordSpan]]:
    """把逐页词拼成一整条文档流，并记录每个词在流中的字符区间。"""
    parts: list[str] = []
    spans: list[_WordSpan] = []
    cursor = 0
    for page in pages:
        for word in page.words:
            parts.append(word.text)
            spans.append(_WordSpan(page.no, cursor, cursor + len(word.text), word))
            cursor += len(word.text)
            parts.append(_WORD_SEP)
            cursor += len(_WORD_SEP)
    return "".join(parts), spans


def iter_chunks(
    stream: str,
    *,
    chunk_chars: int | None = None,
    overlap: int | None = None,
) -> list[tuple[str, int, int]]:
    """按**词边界**切块（不切碎词），相邻块带重叠。返回 (text, start, end)。"""
    size = int(chunk_chars if chunk_chars is not None else settings.TEXT_LAYER_DOCLINE_CHUNK_CHARS)
    pad = int(overlap if overlap is not None else settings.TEXT_LAYER_DOCLINE_CHUNK_OVERLAP)
    size = max(200, size)
    pad = max(0, min(pad, size // 2))
    if not stream:
        return []
    chunks: list[tuple[str, int, int]] = []
    start = 0
    total = len(stream)
    while start < total:
        end = min(total, start + size)
        if end < total:
            space = stream.rfind(_WORD_SEP, start + size // 2, end)
            if space > start:
                end = space
        text = stream[start:end].strip()
        if text:
            chunks.append((text, start, end))
        if end >= total:
            break
        start = max(start + 1, end - pad)
    return chunks


# --------------------------------------------------------------------------- 匹配


def _normalized_stream(stream: str) -> tuple[str, list[int]]:
    """NFKC 归一 + 去空白后的流，以及每个字符回原流的索引表。"""
    out: list[str] = []
    index_map: list[int] = []
    for i, ch in enumerate(stream):
        folded = unicodedata.normalize("NFKC", ch)
        for piece in folded:
            if piece.isspace():
                continue
            out.append(piece)
            index_map.append(i)
    return "".join(out), index_map


def _fold(value: str) -> str:
    return "".join(
        piece for piece in unicodedata.normalize("NFKC", str(value or "")) if not piece.isspace()
    )


def find_value_spans(
    normalized: str,
    index_map: list[int],
    value: str,
    *,
    word_starts: set[int] | None = None,
    word_ends: set[int] | None = None,
    stream: str | None = None,
) -> list[tuple[int, int]]:
    """在归一化流里找 value 的全部出现，返回原流坐标区间。

    `word_starts` / `word_ends` / `stream` 三件套给定时启用**词边界校验**
    （Issue#115 评审 I-4）：只在「匹配起点落在某个词的开头」且「匹配终点落在
    某个词的结尾」时才算命中。没有这层校验时 `str.find` 会把 `张三` 匹配进
    `张三丰` 内部——框会盖住半个词，且脱敏会把不该涂的字一起涂掉。
    """
    needle = _fold(value)
    if not needle:
        return []
    check_bounds = word_starts is not None and word_ends is not None and stream is not None
    spans: list[tuple[int, int]] = []
    cursor = 0
    while True:
        pos = normalized.find(needle, cursor)
        if pos < 0:
            break
        start = index_map[pos]
        end = index_map[pos + len(needle) - 1] + 1
        cursor = pos + 1  # 允许重叠匹配（如「张三」与「张三丰」），由边界校验筛选
        if check_bounds and not _aligned_to_words(
            start, end, stream, word_starts, word_ends  # type: ignore[arg-type]
        ):
            continue
        spans.append((start, end))
    return spans


def _aligned_to_words(
    start: int,
    end: int,
    stream: str,
    word_starts: set[int],
    word_ends: set[int],
) -> bool:
    """匹配区间是否恰好落在词边界上（首字在词首、末字在词尾）。"""
    left_ok = start in word_starts or (start > 0 and stream[start - 1].isspace())
    right_ok = end in word_ends or (end < len(stream) and stream[end].isspace())
    return left_ok and right_ok


def _word_span_index(word_spans: list[_WordSpan]) -> set[tuple[int, int]]:
    """词区间索引 `{(start, end)}`。

    **必须是独立函数**：Issue#115 P1（189 页真实卷）实测把这段集合构造写在
    `map_values` 的「多命中收口」分支里 ⇒ 每个出现多次的值都重建全部词区间，
    O(值数 × 词数) ≈ 6×10^8 次元组构造，把事件循环堵死 >2 分钟（`/health` 超时、
    单核 100%）。抽成函数后可用『调用次数』做确定性回归（见
    `test_map_values_indexes_word_spans_once`），不依赖计时。
    """
    return {(span.start, span.end) for span in word_spans}


def map_values(
    pages: list[TextLayerPage],
    stream: str,
    word_spans: list[_WordSpan],
    values_by_type: dict[str, set[str]],
    *,
    unconstrained_types: set[str] | None = None,
) -> tuple[list[DocLineEntity], list[dict]]:
    r"""值 → (页号, 归一化并集框)。返回 (实体, 映射失败清单)。

    **不做词边界硬校验**：HaS 给的是「张三丰」这类词内子串（旧链
    `match_entities_to_ocr` 本身就支持 sub-word positioning），硬校验会让新链比旧链
    更窄而静默漏检。框精度改由词内插值保证。`unconstrained_types` 保留（regex
    保证层的类型本就不受词边界约束），当前两种情形走同一条路。正则命中
    天然可能落在词中间，用 `\d{3}号` 这类模式时不该被判掉）。这类匹配的框会按
    词内字符比例**插值**收紧到命中的那几个字，而不是整词涂掉。
    """
    normalized, index_map = _normalized_stream(stream)
    page_meta = {page.no: page for page in pages}
    # **一次性**索引：`(start, end)` 词区间集合。189 页真实卷有 ~20 万个词，
    # 若放在下面的「多命中收口」里逐值重建，就是 O(值数 × 词数) ≈ 6 亿次元组构造
    # ——P1（189 页）实测把事件循环堵死 >2 分钟（/health 超时、CPU 100% 单核）。
    aligned_keys = _word_span_index(word_spans)
    entities: list[DocLineEntity] = []
    failures: list[dict] = []
    seen: set[tuple[int, str, str, int, int]] = set()
    for type_id, values in values_by_type.items():
        for value in sorted(values):
            # **不做词边界硬校验**（门⑥ 第二轮 Critical#2）：HaS 返回的正是
            # 「张三丰」这类**词内子串**，中文正文里值紧贴称谓/括号/前缀是常态
            # （`被告人张三丰`、`电话13612345678`、`（张三）`）。旧链
            # `match_entities_to_ocr` 自身就支持 sub-word positioning，硬校验会让
            # 新链比旧链更窄 ⇒ 静默漏检。框精度改由下面的**词内插值**保证。
            spans = find_value_spans(normalized, index_map, value)
            if len(spans) > 1:
                # **子串匹配的过度命中收口**：取消词边界硬校验后，短值（如年龄 `35`）
                # 可能命中长串内部（身份证/银行卡号里就会出现）。规则：只要存在
                # 「整词恰等」的命中就只取那些，否则才退回全部子串命中——既保住
                # 词内子串的召回（真实卷 14.3% 的词是 16-48 字连排段），又不至于
                # 把带前缀的串当成独立值。
                aligned = [(a, b) for a, b in spans if (a, b) in aligned_keys]
                if aligned:
                    spans = aligned
            if not spans:
                failures.append({"type": type_id, "value": value, "reason": "not_found_in_stream"})
                continue
            hits = 0
            rejected = False
            for start, end in spans:
                # 收集命中词，并记录命中字符在词内的偏移：用于把框**收紧**到真正的
                # 那几个字（否则 `（2022）京0101刑初123号` 这种长「词」会被整块涂掉）。
                per_page: dict[int, list[tuple[TextLayerWord, int, int]]] = {}
                for span in word_spans:
                    if span.end <= start or span.start >= end:
                        continue
                    off0 = max(0, start - span.start)
                    off1 = min(len(span.word.text), end - span.start)
                    per_page.setdefault(span.page, []).append((span.word, off0, off1))
                if not per_page:
                    continue
                # 跨页命中一律丢弃（Critical#2 之二）：流里**页间与词间用的是同一个
                # 空格**，`p1 末词 + p2 首词` 会被拼成原文根本不存在的值。
                if len(per_page) > 1:
                    failures.append({"type": type_id, "value": value, "reason": "cross_page"})
                    rejected = True
                    continue
                for page_no, hits_words in per_page.items():
                    meta = page_meta.get(page_no)
                    if meta is None or meta.width <= 0 or meta.height <= 0:
                        continue
                    # 框 = 命中字符在词内的线性插值端点（Important#1）：旧写法后又取
                    # 词的 x0/x1 边界，而 `min(x1) >= min(x0)` 恒成立 ⇒ 插值必然退化
                    # 成整词，「收紧」是死代码。
                    x_starts: list[float] = []
                    x_ends: list[float] = []
                    for word, off0, off1 in hits_words:
                        chars = max(1, len(word.text))
                        span_w = word.x1 - word.x0
                        x_starts.append(word.x0 + span_w * off0 / chars)
                        x_ends.append(word.x0 + span_w * off1 / chars)
                    x0 = min(x_starts)
                    x1 = max(x_ends)
                    if x1 <= x0:
                        x0 = min(w.x0 for w, _a, _b in hits_words)
                        x1 = max(w.x1 for w, _a, _b in hits_words)
                    y0 = min(w.y0 for w, _a, _b in hits_words)
                    y1 = max(w.y1 for w, _a, _b in hits_words)
                    bbox = (
                        max(0.0, x0 / meta.width),
                        max(0.0, y0 / meta.height),
                        min(1.0, x1 / meta.width),
                        min(1.0, y1 / meta.height),
                    )
                    # 去重键含 y（Issue#115 评审 I-4）：同一列同 x0 的两个不同出现
                    # 是两个必须分别打码的位置，不能因为只按 x0 判重而丢掉第二个。
                    key = (page_no, type_id, value, int(bbox[0] * 10000), int(bbox[1] * 10000))
                    if key in seen:
                        continue
                    seen.add(key)
                    entities.append(DocLineEntity(page_no, type_id, value, bbox))
                    hits += 1
            if hits == 0 and not rejected:
                failures.append({"type": type_id, "value": value, "reason": "no_word_overlap"})
    return entities, failures


# --------------------------------------------------------------------------- 调用


def _residual_text(chunk_text: str, hit_values: list[str]) -> str:
    """本块里**未被任何已识别值覆盖**的片段（对齐页线的 residual 语义）。"""
    covered = [False] * len(chunk_text)
    normalized_chunk, index_map = _normalized_stream(chunk_text)
    for value in hit_values:
        for start, end in find_value_spans(normalized_chunk, index_map, value):
            for i in range(max(0, start - 2), min(len(covered), end + 2)):
                covered[i] = True
    parts: list[str] = []
    current: list[str] = []
    for i, ch in enumerate(chunk_text):
        if covered[i]:
            if current:
                parts.append("".join(current))
                current = []
        else:
            current.append(ch)
    if current:
        parts.append("".join(current))
    return " ".join(p.strip() for p in parts if p.strip())


async def run_document_ner(
    pages: list[TextLayerPage],
    labels: list[str],
    *,
    has_client,
    chunk_chars: int | None = None,
    overlap: int | None = None,
    concurrency: int | None = None,
    residual_pass: bool = True,
    regex_types: list[tuple[str, str]] | None = None,
) -> DocLineResult:
    """文档级 NER 主入口。`labels` 是 HaS 提示词标签（中文，见 has_text_payload）。"""
    started = time.perf_counter()
    stream, word_spans = build_stream(pages)
    chunks = iter_chunks(stream, chunk_chars=chunk_chars, overlap=overlap)
    limit = int(concurrency if concurrency is not None else settings.TEXT_LAYER_DOCLINE_CONCURRENCY)
    semaphore = asyncio.Semaphore(max(1, limit))
    values_by_type: dict[str, set[str]] = {label: set() for label in labels}
    stats = {"calls": 0, "residual_calls": 0, "truncated": 0, "http_errors": 0,
             "regex_values": 0, "failed_chunks": 0}
    failed_ranges: list[tuple[int, int]] = []
    lock = asyncio.Lock()

    async def ask(text: str, use_labels: list[str]) -> dict[str, list[str]]:
        if not text.strip() or not use_labels:
            return {}
        async with semaphore:
            try:
                # 块级派发同样要经过全局 GPU 闸门：本链是**新增的并发入口**，
                # 若自建信号量就会绕开 HAS_NER_GLOBAL_MAX_INFLIGHT 的全局上限
                # （Issue#115 评审 I-5）。
                async with shared_gpu_inference_slot("TextLayerDocLine NER"):
                    result = await asyncio.to_thread(has_client.ner, text, use_labels)
            except Exception as exc:  # noqa: BLE001 - 单块失败不得拖垮整卷
                logger.warning("docline NER chunk failed: %s", exc)
                async with lock:
                    stats["http_errors"] += 1
                    stats["failed_chunks"] += 1
                return None
        async with lock:
            stats["calls"] += 1
            for _label, found in (result or {}).items():
                if len(found or []) >= 200:
                    stats["truncated"] += 1
                    break
        return result or {}

    async def one_chunk(chunk: tuple[str, int, int]) -> None:
        text = chunk[0]
        result = await ask(text, labels)
        if result is None:
            # 块级硬失败：该块覆盖的字符**不计入 coverage**，且整卷判不合格
            # （门⑥ 第二轮 Critical#2 的「失败必须可见」要求）。
            async with lock:
                failed_ranges.append((chunk[1], chunk[2]))
            return
        hit_values: list[str] = []
        async with lock:
            for label, found in result.items():
                bucket = values_by_type.setdefault(label, set())
                for value in found or []:
                    cleaned = str(value or "").strip()
                    if cleaned:
                        bucket.add(cleaned)
                        hit_values.append(cleaned)
        if not residual_pass:
            return
        residual = _residual_text(text, hit_values)
        if len(residual) < _MIN_RESIDUAL_CHARS:
            return
        residual_result = await ask(residual, labels)
        if residual_result is None:
            async with lock:
                failed_ranges.append((chunk[1], chunk[2]))
            return
        async with lock:
            stats["residual_calls"] += 1
            for label, found in (residual_result or {}).items():
                bucket = values_by_type.setdefault(label, set())
                for value in found or []:
                    cleaned = str(value or "").strip()
                    if cleaned:
                        bucket.add(cleaned)

    await asyncio.gather(*(one_chunk(chunk) for chunk in chunks))

    # **regex 保证层**（Issue#115 评审 I-3）：租户配置的 regex 类型在既有 OCR+HaS 链里
    # 由 `_apply_regex_fallback` 兜底；文档级链没有那一步，若不在此补上，配置了正则类型的
    # 租户会在覆盖页上**丢掉这些类型**（漏检）。这里直接在整卷流上跑同一批正则，命中的值
    # 交给同一套回映（含词边界校验）出框，语义与旧链一致。
    regex_type_ids: set[str] = set()
    for type_id, pattern in regex_types or []:
        if not type_id or not pattern:
            continue
        try:
            hits = {m.group(0).strip() for m in re.finditer(pattern, stream)}
        except re.error as exc:
            logger.warning("docline regex type %s has bad pattern: %s", type_id, exc)
            continue
        hits.discard("")
        if hits:
            values_by_type.setdefault(type_id, set()).update(hits)
            stats["regex_values"] = int(stats.get("regex_values", 0)) + len(hits)
            regex_type_ids.add(type_id)

    entities, failures = map_values(
        pages, stream, word_spans, values_by_type, unconstrained_types=regex_type_ids
    )
    # **coverage 只算成功块**（Important#5）：旧写法对全部块求并集，而块首尾相接+
    # 重叠 ⇒ 并集恒等于整条流 ⇒ `coverage_ratio` 结构上恒为 1.0，是不可能失败的
    # 指标。改为剔除失败块后求并集，失败块就会如实压低 coverage。
    failed_set = set(failed_ranges)
    ok_chunks = [c for c in chunks if (c[1], c[2]) not in failed_set]
    covered = _union_coverage(ok_chunks, len(stream))
    layer_chars = sum(len(w.text) for span in word_spans for w in [span.word])
    return DocLineResult(
        failed_chunks=len(failed_set),
        pages=len(pages),
        chunks=len(chunks),
        chunk_chars=int(chunk_chars or settings.TEXT_LAYER_DOCLINE_CHUNK_CHARS),
        stream_chars=len(stream),
        layer_chars=layer_chars,
        covered_chars=covered,
        entities=entities,
        ner_calls=stats["calls"],
        residual_calls=stats["residual_calls"],
        truncated_chunks=stats["truncated"],
        http_errors=stats["http_errors"],
        seconds=time.perf_counter() - started,
        map_failures=failures,
    )


def _union_coverage(chunks: list[tuple[str, int, int]], total: int) -> int:
    """块区间的**并集**长度（重叠块不重复计）。"""
    if total <= 0 or not chunks:
        return 0
    intervals = sorted((start, end) for _t, start, end in chunks)
    covered = 0
    current_start, current_end = intervals[0]
    for start, end in intervals[1:]:
        if start <= current_end:
            current_end = max(current_end, end)
        else:
            covered += current_end - current_start
            current_start, current_end = start, end
    covered += current_end - current_start
    return min(covered, total)
