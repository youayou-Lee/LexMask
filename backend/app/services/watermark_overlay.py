"""Issue#236：独立矢量覆盖水印流的检测与摘除（识别前预处理，仅内部副本）。

设计文档：docs/issue-236-watermark-overlay-strip.md

边界（不可协商）
----------------
* 净化副本**只供渲染**（识别取图 / VL-MD 取图）。文本层判定链、docline 建链、
  #203 重复片段闸、交付物光栅化与打印**一律继续吃原件**——把副本喂给文本层会让
  该族卷的 coverage 掉到 0.04、docline 接管整卷，撞页级 300 s 超时后整卷清零。
* 原件 1 字节不动；未命中卷不产生任何副本（逐字节原样通过）。
* 任何异常都回落原件（fail-open），并在状态里留下显式 failed/error。
"""
from __future__ import annotations

import hashlib
import itertools
import logging
import os
import re
import threading
import time
from collections import Counter
from dataclasses import dataclass

import fitz

logger = logging.getLogger(__name__)

# 内容流纯字节 op 扫描：只有「纯文字绘制」的流才允许被当作水印流摘除。
_SAFE_OPS = {
    "q", "Q", "cm", "BT", "ET", "Tf", "Td", "TD", "Tm", "T*", "TJ", "Tj", "'", '"',
    "Tc", "Tw", "Tz", "TL", "Ts", "Tr", "rg", "RG", "g", "G", "k", "K", "cs", "CS",
    "sc", "SCN", "scn", "gs", "w", "J", "j", "M", "d", "ri", "i", "re", "n",
}
_PAINT_OPS = {"f", "F", "f*", "B", "B*", "b", "b*", "S", "s", "Do", "BI", "sh"}
_TOKEN_RE = re.compile(rb"<[0-9A-Fa-f\s]*>|\((?:\\.|[^\\()])*\)|/[^\s/<>()\[\]{}%]+|[-+]?\d*\.?\d+|[A-Za-z*'\"]+")
_WS_RE = re.compile(r"\s+")

# 指纹默认值（锚定证据见设计文档第 2 节：族 A 卷 size 极差比 1.02、alpha 恒定、
# 每页水印最高重复 6 次、每页 15 个水印 span）。
_TILT_MIN = 0.2
_ALPHA_MIN = 1
_ALPHA_MAX = 220
# 「这条流真的在画文字」的门槛：按文字绘制 op（Tj/TJ/'/"）计数。锚定样本是
# 每份水印一个独立 BT…ET 块（30 组），但 62（证据材料卷51）形态把 65 份水印
# 塞进单个 BT 块（#245 外部对照）——数 BT 会误拒，数 Tj 才是形态无关的信号。
_MIN_TEXT_OPS = 3
_MIN_TOKEN_LEN = 6


def _has_text_drawing(ops: Counter) -> bool:
    return ops.get("Tj", 0) + ops.get("TJ", 0) + ops.get("'", 0) + ops.get('"', 0) >= _MIN_TEXT_OPS
# Issue#245：组合试摘只对「合格流 ≤ 此数」的页启用——超出即单流路径（fail-closed），
# 组合枚举成本 O(2^k)，页内合格流条数现实上限是个位数。
_MAX_COMBO_QUALIFIED = 6


# 进程级计数器：验收判据（AC1/AC4/AC6/AC11）要能断言「探测 1 次、构建 1 次/0 次」，
# 不能只靠状态字段自证——状态是每次调用的返回值，计数器才是跨调用的行为证据。
_STATS_LOCK = threading.Lock()
_STATS = {"probe_calls": 0, "build_calls": 0, "render_calls": 0, "render_ms": 0}


def _sanitize_error(exc: BaseException | str) -> str:
    """错误串里只留异常类型与原因，绝不带服务器绝对路径（状态会进 vision_quality）。"""
    if isinstance(exc, str):
        text = exc
    else:
        text = f"{type(exc).__name__}: {exc}"
    text = re.sub(r"(?:/[^\s:;,)\]]+)+", "<path>", text)
    return text[:200]


def _settings():
    from app.core.config import settings

    return settings


def _bump(name: str, delta: int = 1) -> None:
    with _STATS_LOCK:
        _STATS[name] = _STATS.get(name, 0) + delta


def stats() -> dict:
    """只读快照（供验收/测试断言与 /health 类诊断使用）。"""
    with _STATS_LOCK:
        return dict(_STATS)


def reset_stats() -> None:
    """测试辅助：清零计数器。"""
    with _STATS_LOCK:
        for key in _STATS:
            _STATS[key] = 0


@dataclass(frozen=True)
class ProbeOutcome:
    code: str
    pages_probed: int = 0
    pages_hit: int = 0
    slots: tuple[int, ...] = ()
    # 每命中页的摘除槽位组：单流形态为 (page, (slot,))；Issue#245 双保险形态为
    # (page, (slot_a, slot_b, ...))——同一水印画多遍时必须整组同摘。
    slot_by_page: tuple[tuple[int, tuple[int, ...]], ...] = ()
    fonts: tuple[str, ...] = ()
    size_spread: float = 1.0
    alpha_spread: float = 1.0
    top_repeats: int = 0
    charset: str = ""
    tokens: tuple[str, ...] = ()
    elapsed_ms: int = 0
    error: str | None = None

    @property
    def hit(self) -> bool:
        return self.code == "hit"


@dataclass(frozen=True)
class BuildOutcome:
    code: str
    pages_guard_refused: int = 0
    copy_path: str | None = None
    pages: int = 0
    pages_stripped: int = 0
    pages_skipped: int = 0
    bytes_written: int = 0
    elapsed_ms: int = 0
    error: str | None = None


def _stream_ops(raw: bytes) -> tuple[Counter, bool]:
    """纯字节扫描内容流的 op 集合；任何绘制类/未知 op 都判不合格（fail-closed）。"""
    ops: Counter = Counter()
    for match in _TOKEN_RE.finditer(raw or b""):
        token = match.group()
        head = token[:1]
        if head in b"<(/[" or head.isdigit() or head in b"+-.":
            continue
        ops[token.decode("latin1")] += 1
    bad = any(op in _PAINT_OPS or op not in _SAFE_OPS for op in ops)
    return ops, not bad


def _contents_xrefs(doc: fitz.Document, page: fitz.Page) -> list[int]:
    kind, val = doc.xref_get_key(page.xref, "Contents")
    if kind == "xref":
        return [int(val.split()[0])]
    if kind == "array":
        return [int(part.split()[0]) for part in val.strip("[]").split(" 0 R") if part.strip()]
    return []


def _set_contents(doc: fitz.Document, xref: int, streams: list[int]) -> None:
    doc.xref_set_key(xref, "Contents", "[" + " ".join(f"{x} 0 R" for x in streams) + "]")


def _spans(page: fitz.Page) -> list[dict]:
    out: list[dict] = []
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []) or []:
            direction = tuple(round(float(v), 3) for v in line.get("dir", (1.0, 0.0)))
            for span in line.get("spans", []) or []:
                out.append(
                    {
                        "text": span.get("text", ""),
                        "bbox": tuple(round(float(v), 1) for v in span.get("bbox", (0, 0, 0, 0))),
                        "font": span.get("font", ""),
                        "size": round(float(span.get("size", 0.0)), 2),
                        "dir": direction,
                        "alpha": int(span.get("alpha", 255)),
                    }
                )
    return out


def _is_overlay_span(span: dict, *, tilt_min: float = _TILT_MIN, alpha_max: int = _ALPHA_MAX) -> bool:
    """旋转 + 半透明 + 有文字：族 A 水印（45°、alpha=51）命中，正文/不可见文字层不命中。"""
    if not span["text"].strip():
        return False
    if abs(span["dir"][1]) < tilt_min:
        return False
    return _ALPHA_MIN <= span["alpha"] <= alpha_max


def _probe_indices(page_count: int, probes: int) -> list[int]:
    if page_count <= 0 or probes <= 0:
        return []
    if page_count <= probes:
        return list(range(page_count))
    if probes == 1:
        return [0]
    return sorted({round(i * (page_count - 1) / (probes - 1)) for i in range(probes)})


def _spread(values: list[float]) -> float:
    positives = [v for v in values if v > 0]
    if len(positives) < 2:
        return 1.0
    return max(positives) / min(positives)


def probe(
    path: str,
    *,
    probes: int = 5,
    min_hit_pages: int = 3,
    min_repeats: int = 3,
    max_size_spread: float = 1.5,
    max_alpha_spread: float = 2.0,
    max_file_mb: float = 400.0,
    tilt_min: float = _TILT_MIN,
    alpha_max: int = _ALPHA_MAX,
) -> ProbeOutcome:
    """检测卷内是否存在可无损摘除的独立矢量覆盖水印流（每卷一次）。"""
    _bump("probe_calls")
    started = time.perf_counter()

    def done(code: str, **kwargs) -> ProbeOutcome:
        return ProbeOutcome(
            code=code,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            **kwargs,
        )

    if not str(path).lower().endswith(".pdf"):
        return done("not_pdf", pages_probed=0)
    try:
        if max_file_mb > 0 and os.path.getsize(path) > max_file_mb * 1024 * 1024:
            return done("capped_by_size")
    except OSError as exc:
        return done("error", error=_sanitize_error(f"stat failed: {exc}"))

    doc = None
    try:
        doc = fitz.open(path)
        # 加密/需要口令的卷一律不摘：摘流会破坏加密与可能的签名，且碳副本会绕过
        # 原件的访问控制（B4）。显式留码，而不是让后续逻辑「碰巧」失败。
        if getattr(doc, "needs_pass", 0) or getattr(doc, "is_encrypted", False):
            return done("encrypted", pages_probed=0)
        page_count = len(doc)
        if page_count <= 0:
            return done("not_applicable", pages_probed=0)
        ids = _probe_indices(page_count, probes)
        if len(ids) < min_hit_pages:
            return done("below_min_pages", pages_probed=len(ids))

        hits: list[int] = []
        slots: list[int] = []
        hit_slots: list[tuple[int, int]] = []
        sizes: list[float] = []
        alphas: list[float] = []
        fonts: set[str] = set()
        repeats: list[int] = []
        charset: set[str] = set()
        token_counter: Counter = Counter()
        saw_overlay = False

        for index in ids:
            page = doc.load_page(index)
            base = _spans(page)
            overlay = [s for s in base if _is_overlay_span(s, tilt_min=tilt_min, alpha_max=alpha_max)]
            if not overlay:
                continue
            saw_overlay = True
            base_non = [s for s in base if not _is_overlay_span(s, tilt_min=tilt_min, alpha_max=alpha_max)]
            xrefs = _contents_xrefs(doc, page)
            qualified: list[int] = []
            if len(xrefs) >= 2:
                for slot, xref in enumerate(xrefs):
                    ops, ok = _stream_ops(doc.xref_stream(xref))
                    if ok and _has_text_drawing(ops):
                        qualified.append(slot)

            def _try_subset(
                subset: tuple[int, ...], *, xrefs: list[int], base_non: list[dict]
            ) -> bool:
                """试摘一组流：摘后无覆盖 span 且非覆盖 span 逐字段不变（随后恢复）。"""
                nonlocal page
                keep = [x for j, x in enumerate(xrefs) if j not in subset]
                if not keep:
                    return False
                _set_contents(doc, page.xref, keep)
                reloaded = doc.reload_page(page)
                after = _spans(reloaded)
                _set_contents(doc, reloaded.xref, xrefs)
                page = doc.reload_page(reloaded)
                if any(_is_overlay_span(s, tilt_min=tilt_min, alpha_max=alpha_max) for s in after):
                    return False
                return after == base_non

            passing: list[tuple[int, ...]] = []
            singles = [slot for slot in qualified if _try_subset((slot,), xrefs=xrefs, base_non=base_non)]
            if len(singles) == 1:
                passing = [(singles[0],)]
            elif not singles:
                # 单流试摘全不成立 ⇒ 组合试摘（Issue#245：同一水印画两遍的双保险画法，
                # 摘一条另一条还在，单流差分 oracle 必然全败）。组合枚举只在「没有任何
                # 单流候选」时发生——既有单流卷的快路径零改动；多于一组成立仍判歧义
                # 弃权（fail-closed，同 AC7-7d）。
                if len(qualified) <= _MAX_COMBO_QUALIFIED:
                    for size in range(2, len(qualified) + 1):
                        for combo in itertools.combinations(qualified, size):
                            if _try_subset(combo, xrefs=xrefs, base_non=base_non):
                                passing.append(combo)
                if len(passing) > 1:
                    # 防御分支（fail-closed）：对 overlay-classified span 结构性几乎
                    # 不可达——两个不同子集同时通过 span oracle 会导出代数矛盾。
                    # 变异 M3（本评审）SURVIVED＝无构造性判据，挂账 PR#242（I-1）。
                    passing = []
            # len(singles) > 1：多条单流候选 = 歧义，维持弃权（同上，防御分支）。

            if len(passing) != 1:
                continue

            subset = passing[0]
            hits.append(index)
            slots.append(subset[0])
            hit_slots.append((index, subset))
            for span in overlay:
                sizes.append(span["size"])
                alphas.append(float(span["alpha"]))
                fonts.add(span["font"])
                charset.update(span["text"])
                normalized = _WS_RE.sub("", span["text"])
                if len(normalized) >= _MIN_TOKEN_LEN:
                    token_counter[normalized] += 1
            repeats.append(max(Counter(_WS_RE.sub("", s["text"]) for s in overlay).values()))

        if len(hits) < min_hit_pages:
            # 命中很稀疏（部分页有水印的异构卷）判 below_min_pages 并显式记账；
            # 只有零命中才落到「没有水印 / 不可分离」两种语义。
            if hits:
                code = "below_min_pages"
            else:
                code = "no_isolated_overlay" if saw_overlay else "no_wm_spans"
            return done(code, pages_probed=len(ids), pages_hit=len(hits))

        size_spread = _spread(sizes)
        alpha_spread = _spread(alphas)
        top_repeats = min(repeats) if repeats else 0
        tokens = tuple(sorted((t for t, n in token_counter.items() if n >= 2), key=len, reverse=True))
        common = dict(
            pages_probed=len(ids),
            pages_hit=len(hits),
            slots=tuple(slots),
            slot_by_page=tuple(hit_slots),
            fonts=tuple(sorted(fonts)),
            size_spread=round(size_spread, 3),
            alpha_spread=round(alpha_spread, 3),
            top_repeats=top_repeats,
            charset="".join(sorted(charset)),
            tokens=tokens[:16],
        )
        if size_spread > max_size_spread or alpha_spread > max_alpha_spread:
            return done("signature_drift", **common)
        if top_repeats < min_repeats:
            return done("signature_drift", **common)
        return done("hit", **common)
    except fitz.FileDataError as exc:  # 后缀是 .pdf、内容不是 PDF ⇒ 同样是「不是 PDF」
        return done("not_pdf", pages_probed=0, error=_sanitize_error(exc))
    except Exception as exc:  # noqa: BLE001 - fail-open：任何异常都回落原件
        logger.warning("watermark overlay probe failed: %s", type(exc).__name__)
        return done("error", error=_sanitize_error(exc))
    finally:
        if doc is not None:
            doc.close()


def is_overlay_line(line: dict, *, tilt_min: float = _TILT_MIN, alpha_max: int = _ALPHA_MAX) -> bool:
    """get_text("dict") 行级覆盖水印判定（Issue#250 inline 形态）。

    行倾斜 + 行内任一 span 半透明（alpha∈[1, alpha_max]）⇒ 覆盖水印行。
    正文（水平不透明）与扫描件不可见 OCR 层（alpha=0）天然不命中。
    """
    direction = line.get("dir") or (1.0, 0.0)
    if abs(float(direction[1])) < tilt_min:
        return False
    for span in line.get("spans", []) or []:
        alpha = int(span.get("alpha", 255))
        if _ALPHA_MIN <= alpha <= alpha_max and str(span.get("text", "")).strip():
            return True
    return False


def overlay_filtered_text(page: fitz.Page, *, enabled: bool | None = None) -> tuple[str, int]:
    """`page.get_text()` 的识别面净化版（Issue#250）：剔除覆盖水印行后按阅读序重组。

    页面无覆盖行（或功能关闭）时返回 `page.get_text()` **逐字节原样**——零扰动由
    构造保证。返回 `(文本, 剔除行数)`。
    """
    from app.core.config import settings

    if enabled is None:
        enabled = bool(getattr(settings, "WATERMARK_OVERLAY_STRIP_ENABLED", False))
    if not enabled:
        return page.get_text(), 0
    raw = page.get_text("dict")
    has_overlay = any(
        is_overlay_line(line)
        for block in raw.get("blocks", [])
        if block.get("type") == 0
        for line in block.get("lines", []) or []
    )
    if not has_overlay:
        return page.get_text(), 0
    kept: list[str] = []
    removed = 0
    for block in raw.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []) or []:
            spans = line.get("spans", []) or []
            line_text = "".join(str(span.get("text", "")) for span in spans)
            if is_overlay_line(line):
                removed += 1
                continue
            if line_text.strip():
                kept.append(line_text)
    return ("\n".join(kept), removed) if kept else ("", removed)


def probe_report(path: str) -> dict:
    """检测报告（Issue#244 水印工具面）：probe 结果的脱敏 dict——不含副本路径。

    注意：每次调用真实执行探测（不走 memo）；工具页手动触发，代价可接受。
    开关与阈值读 settings（门⑥ I-2：与识别面同源，防 env 调参后两面静默分叉）；
    模块关闭时返回 disabled（工具页/上传弹窗不再全量探测）。
    """
    from app.core.config import settings

    if not bool(getattr(settings, "WATERMARK_OVERLAY_STRIP_ENABLED", False)):
        return {
            "code": "disabled", "pages_probed": 0, "pages_hit": 0,
            "fonts": [], "size_spread": 1.0, "alpha_spread": 1.0,
            "top_repeats": 0, "elapsed_ms": 0, "error": None,
        }
    outcome = probe(
        path,
        probes=int(getattr(settings, "WATERMARK_OVERLAY_PROBE_PAGES", 5)),
        min_hit_pages=int(getattr(settings, "WATERMARK_OVERLAY_MIN_HIT_PAGES", 3)),
        min_repeats=int(getattr(settings, "WATERMARK_OVERLAY_MIN_REPEATS", 3)),
        max_size_spread=float(getattr(settings, "WATERMARK_OVERLAY_MAX_SIZE_SPREAD", 1.5)),
        max_alpha_spread=float(getattr(settings, "WATERMARK_OVERLAY_MAX_ALPHA_SPREAD", 2.0)),
        max_file_mb=float(getattr(settings, "WATERMARK_OVERLAY_MAX_FILE_MB", 400.0)),
    )
    data = {
        "code": outcome.code,
        "pages_probed": outcome.pages_probed,
        "pages_hit": outcome.pages_hit,
        "fonts": list(outcome.fonts),
        "size_spread": outcome.size_spread,
        "alpha_spread": outcome.alpha_spread,
        "top_repeats": outcome.top_repeats,
        "elapsed_ms": outcome.elapsed_ms,
        "error": outcome.error,
    }
    return data


def _line_set(page: fitz.Page) -> Counter:
    return Counter(line.strip() for line in page.get_text().splitlines() if line.strip())


def _residual_tokens(lines: Counter[str], tokens: tuple[str, ...]) -> int:
    hits = 0
    for line in lines:
        normalized = _WS_RE.sub("", line)
        if any(token in normalized for token in tokens):
            hits += 1
    return hits


def build_stripped_copy(
    src: str,
    dest: str,
    outcome: ProbeOutcome,
    *,
    tilt_min: float = _TILT_MIN,
    alpha_max: float = _ALPHA_MAX,
) -> BuildOutcome:
    """按卷内每页摘除水印流，产出内部识别副本（原子落盘，不触碰原件）。"""
    _bump("build_calls")
    started = time.perf_counter()

    def done(code: str, **kwargs) -> BuildOutcome:
        return BuildOutcome(code=code, elapsed_ms=int((time.perf_counter() - started) * 1000), **kwargs)

    if outcome.code != "hit":
        return done(outcome.code, error=outcome.error)

    charset = set(outcome.charset)
    tokens = tuple(outcome.tokens)
    subset_for_page = dict(outcome.slot_by_page)
    doc = None
    tmp_path = f"{dest}.tmp-{os.getpid()}-{threading.get_ident()}"
    try:
        doc = fitz.open(src)
        page_count = len(doc)
        stripped = 0
        skipped = 0
        guard_refused = 0
        for index in range(page_count):
            page = doc.load_page(index)
            xrefs = _contents_xrefs(doc, page)
            if len(xrefs) < 2:
                skipped += 1
                continue
            # 该页本身有没有覆盖水印：决定「摘不掉」是隐患（refused）还是本来无事（skipped）。
            base_spans = _spans(page)
            page_has_wm = any(
                _is_overlay_span(span, tilt_min=tilt_min, alpha_max=alpha_max)
                for span in base_spans
            )
            qualified: list[int] = []
            for slot, xref in enumerate(xrefs):
                ops, ok = _stream_ops(doc.xref_stream(xref))
                if ok and _has_text_drawing(ops):
                    qualified.append(slot)
            original_lines = _line_set(page)
            base_non = [
                s for s in base_spans
                if not _is_overlay_span(s, tilt_min=tilt_min, alpha_max=alpha_max)
            ]

            # 候选子集顺序：探针验证过的组合优先（快路径），其后单流（既有路径），
            # 最后组合枚举（Issue#245：探针只抽样 5 页，未抽到的双保险页也要能摘）。
            subsets: list[tuple[int, ...]] = []
            preferred = subset_for_page.get(index)
            if preferred:
                subsets.append(tuple(sorted(preferred)))
            subsets.extend((slot,) for slot in qualified)
            if page_has_wm and 2 <= len(qualified) <= _MAX_COMBO_QUALIFIED:
                for size in range(2, len(qualified) + 1):
                    subsets.extend(itertools.combinations(qualified, size))

            dropped_here = False
            for subset in subsets:
                keep = [x for j, x in enumerate(xrefs) if j not in subset]
                if not keep:
                    continue
                # 未被探针验证过的组合走全套 span oracle（摘后无覆盖 span、非覆盖
                # span 逐字段不变），防止「借组合之名删正文」；探针子集已在探测期
                # 通过 span oracle，这里走廉价行集校验。注意以「子集是否就是探针
                # 验证过的那个」判定，而不是「页是否被探针命中」——探针只背书它
                # 试过的那一个子集。
                probed = subset_for_page.get(index)
                escalated = len(subset) > 1 and (
                    probed is None or tuple(sorted(subset)) != tuple(sorted(probed))
                )
                _set_contents(doc, page.xref, keep)
                reloaded = doc.reload_page(page)
                new_lines = _line_set(reloaded)
                refuse = False
                if new_lines - original_lines:  # 摘除后凭空多出行：结构异常
                    refuse = True
                else:
                    removed = original_lines - new_lines
                    if not removed:
                        # 什么都没删掉：该组合不是水印，换下一个候选（不算页级拒绝）。
                        _set_contents(doc, reloaded.xref, xrefs)
                        page = doc.reload_page(reloaded)
                        continue
                    if not all(set(line) <= charset for line in removed):  # 删掉的不全是水印字符
                        refuse = True
                    elif tokens and _residual_tokens(new_lines, tokens):  # 摘后仍有水印令牌
                        refuse = True
                    elif escalated:
                        after = _spans(reloaded)
                        after_non = [
                            s for s in after
                            if not _is_overlay_span(s, tilt_min=tilt_min, alpha_max=alpha_max)
                        ]
                        if after_non != base_non or any(
                            _is_overlay_span(s, tilt_min=tilt_min, alpha_max=alpha_max)
                            for s in after
                        ):
                            refuse = True
                if refuse:
                    _set_contents(doc, reloaded.xref, xrefs)
                    page = doc.reload_page(reloaded)
                    continue
                # 接受前守卫（Issue#245）：摘后不得残留覆盖 span——双保险页只摘到
                # 一层时仍带水印，宁可拒绝（页级 refused ⇒ 卷级熔断）也不产出
                # 「看起来净化、其实还带水印」的副本。
                after = _spans(reloaded)
                if any(
                    _is_overlay_span(s, tilt_min=tilt_min, alpha_max=alpha_max)
                    for s in after
                ):
                    _set_contents(doc, reloaded.xref, xrefs)
                    page = doc.reload_page(reloaded)
                    continue
                dropped_here = True
                break
            if dropped_here:
                stripped += 1
            elif page_has_wm:
                # 页级判定：这一页**确实有**覆盖水印，但没有一个候选流能被干净摘掉
                # （哪怕试了多个 slot 全拒，也只算 1 页，不按尝试次数放大）。
                guard_refused += 1
            else:
                skipped += 1

        doc.save(tmp_path, garbage=3, deflate=True)
        doc.close()
        doc = None

        # 落盘自检：副本必须可打开且页数一致，否则丢弃（绝不喂渲染一个坏副本）。
        check = fitz.open(tmp_path)
        try:
            if len(check) != page_count:
                raise ValueError(f"copy page count {len(check)} != {page_count}")
        finally:
            check.close()
        if guard_refused:
            # 卷级熔断：有页**确实带水印却摘不干净**时，整卷不产出副本。
            # 否则下游 OCR/NER 会继续吃那一页的水印，而状态面却是绿的（门⑥ C2）。
            # 注意：只按「页」判定——本来没有水印的页（异构卷正文页）只记 skipped，
            # 不触发熔断（AC7-7a：异构卷应 hit + pages_stripped=3）。
            logger.warning(
                "watermark overlay: %d page(s) with watermark refused by guard; falling back to original",
                guard_refused,
            )
            return done("guard_refused_pages", pages=page_count,
                        pages_stripped=stripped, pages_skipped=skipped,
                        pages_guard_refused=guard_refused)
        if not stripped:
            # 逐页守卫全弃权 ⇒ 不得产出「看起来是净化副本、其实还带水印」的文件
            # （AC7-7c：副本文件不产出，或产出但 GT 令牌命中 > 0 一律 FAIL）。
            return done("no_strippable_pages", pages=page_count,
                        pages_stripped=0, pages_skipped=skipped,
                        pages_guard_refused=guard_refused)
        os.replace(tmp_path, dest)
        try:
            os.chmod(dest, 0o600)
        except OSError:
            pass
        return done(
            "stripped",
            copy_path=dest,
            pages=page_count,
            pages_stripped=stripped,
            pages_skipped=skipped,
            pages_guard_refused=guard_refused,
            bytes_written=os.path.getsize(dest),
        )
    except Exception as exc:  # noqa: BLE001 - fail-open
        logger.warning("watermark overlay copy build failed: %s", type(exc).__name__)
        code = "copy_verify_failed" if isinstance(exc, ValueError) else "error"
        return done(code, error=_sanitize_error(exc))
    finally:
        if doc is not None:
            doc.close()
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# 进程内生命周期：每卷一次探测/构建，失败也缓存（避免每页重付 700 ms）。
# ---------------------------------------------------------------------------

_CACHE_LOCK = threading.Lock()
_CACHE: dict[str, dict] = {}
_INFLIGHT: dict[str, threading.Lock] = {}


def _fingerprint(path: str) -> str:
    stat = os.stat(path)
    payload = f"{os.path.realpath(path)}|{int(stat.st_size)}|{int(stat.st_mtime_ns)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def copy_dir() -> str:
    from app.core.config import settings

    directory = os.path.join(settings.OUTPUT_DIR, ".watermark_overlay")
    os.makedirs(directory, mode=0o700, exist_ok=True)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass
    return directory


def _sweep(directory: str, ttl_hours: float) -> int:
    """清过期副本；**绝不删正在被 memo/在飞请求引用的文件**（门⑥ C3）。返回删除数量。"""
    if ttl_hours <= 0:
        return 0
    cutoff = time.time() - ttl_hours * 3600
    with _CACHE_LOCK:
        # 只有「仍然保鲜」的副本才算在用；过期条目不再豁免，允许被清掉后按需重建。
        in_use = {
            os.path.basename(v["copy_path"])
            for v in _CACHE.values()
            if v.get("copy_path") and _cached_copy_alive(v)
        }
    try:
        names = os.listdir(directory)
    except OSError:
        return 0
    removed = 0
    for name in names:
        if name in in_use or ".tmp-" in name:
            continue
        candidate = os.path.join(directory, name)
        try:
            if os.path.isfile(candidate) and os.path.getmtime(candidate) < cutoff:
                os.remove(candidate)
                removed += 1
        except OSError:
            continue
    return removed


def sweep_expired(ttl_hours: float | None = None) -> int:
    """清过期副本（供进程级定时清理调用）：返回删除数量，OSError/取值错误一律不抛（返回 0）。

    构建路径只在「下一次有水印卷要处理」时才扫一次，若某卷只处理一次就再无扫描
    ⇒ 靠 `main._cleanup_tick`（每小时一次 + 启动一次）兜住 AC9「TTL 实清」。
    目录不存在时直接返回 0（不 `makedirs`：开关关闭时不该凭空建目录，门⑥ M3）。
    """
    from app.core.config import settings

    try:
        ttl = (
            float(getattr(settings, "WATERMARK_OVERLAY_COPY_TTL_HOURS", 24))
            if ttl_hours is None
            else float(ttl_hours)
        )
    except (TypeError, ValueError):
        logger.debug("watermark overlay sweep: invalid ttl %r", ttl_hours)
        return 0
    try:
        directory = os.path.join(settings.OUTPUT_DIR, ".watermark_overlay")
        if not os.path.isdir(directory):
            return 0
        removed = _sweep(directory, ttl)
    except OSError:
        return 0
    logger.debug("watermark overlay sweep dir=%s ttl_hours=%s removed=%d", directory, ttl, removed)
    return removed


def status_for(path: str) -> dict:
    """只读查询（事件循环内可安全调用）：本卷的检测/构建结果。"""
    try:
        key = _fingerprint(path)
    except OSError:
        return {"code": "error", "source": "original"}
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
    return _public(cached) if cached else {}


def _public(status: dict | None) -> dict:
    """对外的状态形状：不暴露副本文件系统路径（前端只关心 code/source/计数）。"""
    if not status:
        return {}
    return {k: v for k, v in status.items() if k != "copy_path" and not k.startswith("_")}


_FAIL_OPEN = {"ran": True, "skipped": False, "failed": False, "region_count": 0,
              "source": "original", "copy_path": None}
# fail-open 状态也进 memo（否则持续失败的卷会每页重付一次探测/构建），但只在短窗口内生效：
# 权限/磁盘类故障修好后不该被永久负缓存钉死。
_FAIL_OPEN_TTL_S = 60.0


def _fail_open_status(code: str, error: str | None, duration_ms: int = 0) -> dict:
    status = dict(_FAIL_OPEN, code=code, error=error, duration_ms=duration_ms)
    status["failed"] = code == "error"
    return status


def _cached_copy_alive(status: dict) -> bool:
    """memo 是否仍然可用：副本要存在且未过 TTL；fail-open 结果只保鲜一个短窗口。"""
    if status.get("source") != "copy":
        cached_at = status.get("_cached_at")
        if cached_at is None:
            return False
        return time.time() - float(cached_at) <= _FAIL_OPEN_TTL_S
    copy_path = status.get("copy_path")
    if not copy_path or not os.path.exists(copy_path):
        return False
    try:  # TTL 对 memo 同样生效：过期副本不返回，交给 _build_once 重建（并顺手 sweep）
        ttl_hours = float(getattr(_settings(), "WATERMARK_OVERLAY_COPY_TTL_HOURS", 24))
        if ttl_hours <= 0:
            return True
        return os.path.getmtime(copy_path) >= time.time() - ttl_hours * 3600
    except OSError:
        return False


def render_source(path: str) -> tuple[str | None, dict]:
    """为渲染返回（净化副本路径 | None）与状态；卷级只探测/构建一次。

    **永不抛异常**：任何失败都回落原件（fail-open），因为识别面默认开启，
    水印模块的故障不能把正常卷的识别整卷打掉（门⑥ C1）。
    """
    from app.core.config import settings

    if not getattr(settings, "WATERMARK_OVERLAY_STRIP_ENABLED", False):
        return None, {"ran": False, "skipped": True, "failed": False, "code": "disabled",
                      "source": "original", "region_count": 0, "error": None, "duration_ms": 0}
    render_started = time.perf_counter()
    try:
        return _render_source_locked(path)
    except PermissionError as exc:
        message = _sanitize_error(exc)
        logger.warning("watermark overlay copy dir not writable: %s: %s", type(exc).__name__, message)
        return None, _remember_failure(path, _fail_open_status("copy_dir_unwritable", message))
    except Exception as exc:  # noqa: BLE001 - fail-open 兜底
        message = _sanitize_error(exc)
        logger.warning("watermark overlay render_source failed: %s: %s", type(exc).__name__, message)
        return None, _remember_failure(path, _fail_open_status("error", message))
    finally:
        _bump("render_calls")
        _bump("render_ms", int((time.perf_counter() - render_started) * 1000))


def _remember_failure(path: str, status: dict) -> dict:
    """把 fail-open 结果写进短窗口负缓存，避免同一卷每页重付探测/构建。"""
    try:
        key = _fingerprint(path)
    except OSError:
        return status
    with _CACHE_LOCK:
        _CACHE[key] = dict(status, _cached_at=time.time())
    return status


def _render_source_locked(path: str) -> tuple[str | None, dict]:
    try:
        key = _fingerprint(path)
    except OSError as exc:
        return None, _fail_open_status("error", _sanitize_error(exc))

    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached is not None and _cached_copy_alive(cached):
            return (cached.get("copy_path"), _public(cached))
        if cached is not None:  # 副本已被 TTL 清掉：丢掉这条 memo，重建
            _CACHE.pop(key, None)
        lock = _INFLIGHT.setdefault(key, threading.Lock())
    with lock:
        try:
            with _CACHE_LOCK:
                cached = _CACHE.get(key)
                if cached is not None and not _cached_copy_alive(cached):
                    _CACHE.pop(key, None)
                    cached = None
            if cached is None:
                cached = _build_once(path, key)
                with _CACHE_LOCK:
                    _CACHE[key] = cached
        finally:
            with _CACHE_LOCK:
                _INFLIGHT.pop(key, None)
    return (cached.get("copy_path"), _public(cached))


def _build_once(path: str, key: str) -> dict:
    from app.core.config import settings

    started = time.perf_counter()

    def stamp(status: dict) -> dict:
        status["duration_ms"] = int((time.perf_counter() - started) * 1000)
        return status

    outcome = probe(
        path,
        probes=int(getattr(settings, "WATERMARK_OVERLAY_PROBE_PAGES", 5)),
        min_hit_pages=int(getattr(settings, "WATERMARK_OVERLAY_MIN_HIT_PAGES", 3)),
        min_repeats=int(getattr(settings, "WATERMARK_OVERLAY_MIN_REPEATS", 3)),
        max_size_spread=float(getattr(settings, "WATERMARK_OVERLAY_MAX_SIZE_SPREAD", 1.5)),
        max_alpha_spread=float(getattr(settings, "WATERMARK_OVERLAY_MAX_ALPHA_SPREAD", 2.0)),
        max_file_mb=float(getattr(settings, "WATERMARK_OVERLAY_MAX_FILE_MB", 400)),
        tilt_min=float(getattr(settings, "WATERMARK_OVERLAY_TILT_MIN", _TILT_MIN)),
        alpha_max=int(getattr(settings, "WATERMARK_OVERLAY_ALPHA_MAX", _ALPHA_MAX)),
    )
    status = {
        "ran": True,
        "skipped": False,
        "failed": outcome.code in {"error", "copy_verify_failed"},
        "region_count": 0,
        "error": outcome.error,
        "code": outcome.code,
        "pages_probed": outcome.pages_probed,
        "pages_hit": outcome.pages_hit,
        "pages_stripped": 0,
        "pages_skipped": 0,
        "pages_guard_refused": 0,
        "source": "original",
        "copy_path": None,
    }
    if not outcome.hit:
        return stamp(status)
    # 副本目录的准备**必须**在 fail-open 保护范围内，且放在探测之后：OUTPUT_DIR 只读/磁盘满时
    # 不能把正常卷的识别整卷打掉（AC10 / 门⑥ C1）；而没有水印的卷根本不需要动文件系统。
    try:
        directory = copy_dir()
        _sweep(directory, float(getattr(settings, "WATERMARK_OVERLAY_COPY_TTL_HOURS", 24)))
    except OSError as exc:
        logger.warning("watermark overlay copy dir unavailable: %s", type(exc).__name__)
        status.update(
            code="copy_dir_unwritable",
            failed=False,
            error=_sanitize_error(exc),
            source="original",
        )
        return stamp(status)
    dest = os.path.join(directory, f"{key}.pdf")
    build = build_stripped_copy(path, dest, outcome)
    status["pages_stripped"] = build.pages_stripped
    status["pages_skipped"] = build.pages_skipped
    status["pages_guard_refused"] = build.pages_guard_refused
    if build.copy_path:
        status.update(code="hit", source="copy", copy_path=build.copy_path)
    else:  # 构建没产出副本：回落原件，但状态里必须显式可查
        code = build.code if not build.code.startswith("error") else "error"
        status.update(
            code=code,
            failed=code in {"error", "copy_verify_failed"},
            error=build.error or (None if code == "no_strippable_pages" else code),
            source="original",
            copy_path=None,
        )
    return stamp(status)
