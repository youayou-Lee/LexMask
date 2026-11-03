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
_MIN_BT_PER_STREAM = 3
_MIN_TOKEN_LEN = 6


# 进程级计数器：验收判据（AC1/AC4/AC6/AC11）要能断言「探测 1 次、构建 1 次/0 次」，
# 不能只靠状态字段自证——状态是每次调用的返回值，计数器才是跨调用的行为证据。
_STATS_LOCK = threading.Lock()
_STATS = {"probe_calls": 0, "build_calls": 0, "render_calls": 0, "render_ms": 0}


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

    try:
        if max_file_mb > 0 and os.path.getsize(path) > max_file_mb * 1024 * 1024:
            return done("capped_by_size")
    except OSError as exc:
        return done("error", error=f"stat failed: {exc}")

    doc = None
    try:
        doc = fitz.open(path)
        page_count = len(doc)
        ids = _probe_indices(page_count, probes)
        if len(ids) < min_hit_pages:
            return done("below_min_pages", pages_probed=len(ids))

        hits: list[int] = []
        slots: list[int] = []
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
            candidates: list[int] = []
            if len(xrefs) >= 2:
                for slot, xref in enumerate(xrefs):
                    ops, ok = _stream_ops(doc.xref_stream(xref))
                    if not ok or ops.get("BT", 0) < _MIN_BT_PER_STREAM:
                        continue
                    keep = [x for j, x in enumerate(xrefs) if j != slot]
                    _set_contents(doc, page.xref, keep)
                    reloaded = doc.reload_page(page)
                    after = _spans(reloaded)
                    _set_contents(doc, reloaded.xref, xrefs)
                    page = doc.reload_page(reloaded)
                    if any(_is_overlay_span(s, tilt_min=tilt_min, alpha_max=alpha_max) for s in after):
                        continue
                    if after != base_non:
                        continue
                    candidates.append(slot)
            if len(candidates) != 1:
                continue

            hits.append(index)
            slots.append(candidates[0])
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
    except Exception as exc:  # noqa: BLE001 - fail-open：任何异常都回落原件
        logger.warning("watermark overlay probe failed: %s", exc)
        return done("error", error=f"{type(exc).__name__}: {exc}"[:200])
    finally:
        if doc is not None:
            doc.close()


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
    preferred_slots = list(outcome.slots)
    doc = None
    tmp_path = f"{dest}.tmp-{os.getpid()}-{threading.get_ident()}"
    try:
        doc = fitz.open(src)
        page_count = len(doc)
        stripped = 0
        skipped = 0
        for index in range(page_count):
            page = doc.load_page(index)
            xrefs = _contents_xrefs(doc, page)
            if len(xrefs) < 2:
                skipped += 1
                continue
            order = list(range(len(xrefs)))
            if index < len(preferred_slots) and preferred_slots[index] in order:
                order.remove(preferred_slots[index])
                order.insert(0, preferred_slots[index])
            dropped_here = False
            for slot in order:
                xref = xrefs[slot]
                ops, ok = _stream_ops(doc.xref_stream(xref))
                if not ok or ops.get("BT", 0) < _MIN_BT_PER_STREAM:
                    continue
                original_lines = _line_set(page)
                keep = [x for j, x in enumerate(xrefs) if j != slot]
                _set_contents(doc, page.xref, keep)
                reloaded = doc.reload_page(page)
                new_lines = _line_set(reloaded)
                if new_lines - original_lines:  # 摘除后凭空多出行：结构异常
                    _set_contents(doc, reloaded.xref, xrefs)
                    page = doc.reload_page(reloaded)
                    continue
                removed = original_lines - new_lines
                if not removed:  # 什么都没删掉：该流不是水印
                    _set_contents(doc, reloaded.xref, xrefs)
                    page = doc.reload_page(reloaded)
                    continue
                if not all(set(line) <= charset for line in removed):  # 删掉的不全是水印字符
                    _set_contents(doc, reloaded.xref, xrefs)
                    page = doc.reload_page(reloaded)
                    continue
                if tokens and _residual_tokens(new_lines, tokens):  # 摘后仍有水印令牌
                    _set_contents(doc, reloaded.xref, xrefs)
                    page = doc.reload_page(reloaded)
                    continue
                dropped_here = True
                break
            if dropped_here:
                stripped += 1
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
        if not stripped:
            # 逐页守卫全弃权 ⇒ 不得产出「看起来是净化副本、其实还带水印」的文件
            # （AC7-7c：副本文件不产出，或产出但 GT 令牌命中 > 0 一律 FAIL）。
            return done("no_strippable_pages", pages=page_count,
                        pages_stripped=0, pages_skipped=skipped)
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
            bytes_written=os.path.getsize(dest),
        )
    except Exception as exc:  # noqa: BLE001 - fail-open
        logger.warning("watermark overlay copy build failed: %s", exc)
        return done(f"error: {type(exc).__name__}", error=str(exc)[:200])
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


def _sweep(directory: str, ttl_hours: float) -> None:
    if ttl_hours <= 0:
        return
    cutoff = time.time() - ttl_hours * 3600
    try:
        for name in os.listdir(directory):
            candidate = os.path.join(directory, name)
            try:
                if os.path.isfile(candidate) and os.path.getmtime(candidate) < cutoff:
                    os.remove(candidate)
            except OSError:
                continue
    except OSError:
        return


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
    return {k: v for k, v in status.items() if k != "copy_path"}


def render_source(path: str) -> tuple[str | None, dict]:
    """为渲染返回（净化副本路径 | None）与状态；卷级只探测/构建一次。"""
    from app.core.config import settings

    if not getattr(settings, "WATERMARK_OVERLAY_STRIP_ENABLED", False):
        return None, {"ran": False, "skipped": True, "failed": False, "code": "disabled",
                      "source": "original", "region_count": 0, "error": None, "duration_ms": 0}
    render_started = time.perf_counter()
    try:
        return _render_source_locked(path)
    finally:
        _bump("render_calls")
        _bump("render_ms", int((time.perf_counter() - render_started) * 1000))


def _render_source_locked(path: str) -> tuple[str | None, dict]:
    try:
        key = _fingerprint(path)
    except OSError as exc:
        return None, {"ran": True, "skipped": False, "failed": True, "code": "error",
                      "source": "original", "region_count": 0,
                      "error": f"{type(exc).__name__}: {exc}"[:200], "duration_ms": 0}

    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached is not None:
            return (cached.get("copy_path"), _public(cached))
        lock = _INFLIGHT.setdefault(key, threading.Lock())
    with lock:
        with _CACHE_LOCK:
            cached = _CACHE.get(key)
        if cached is None:
            cached = _build_once(path, key)
            with _CACHE_LOCK:
                _CACHE[key] = cached
            with _CACHE_LOCK:
                _INFLIGHT.pop(key, None)
    return (cached.get("copy_path"), _public(cached))


def _build_once(path: str, key: str) -> dict:
    from app.core.config import settings

    directory = copy_dir()
    _sweep(directory, float(getattr(settings, "WATERMARK_OVERLAY_COPY_TTL_HOURS", 24) or 0))
    dest = os.path.join(directory, f"{key}.pdf")
    started = time.perf_counter()
    outcome = probe(
        path,
        probes=int(getattr(settings, "WATERMARK_OVERLAY_PROBE_PAGES", 5) or 5),
        min_hit_pages=int(getattr(settings, "WATERMARK_OVERLAY_MIN_HIT_PAGES", 3) or 3),
        min_repeats=int(getattr(settings, "WATERMARK_OVERLAY_MIN_REPEATS", 3) or 3),
        max_size_spread=float(getattr(settings, "WATERMARK_OVERLAY_MAX_SIZE_SPREAD", 1.5) or 1.5),
        max_alpha_spread=float(getattr(settings, "WATERMARK_OVERLAY_MAX_ALPHA_SPREAD", 2.0) or 2.0),
        max_file_mb=int(getattr(settings, "WATERMARK_OVERLAY_MAX_FILE_MB", 400) or 400),
        tilt_min=float(getattr(settings, "WATERMARK_OVERLAY_TILT_MIN", _TILT_MIN) or _TILT_MIN),
        alpha_max=int(getattr(settings, "WATERMARK_OVERLAY_ALPHA_MAX", _ALPHA_MAX) or _ALPHA_MAX),
    )
    status = {
        "ran": True,
        "skipped": False,
        "failed": outcome.code == "error",
        "region_count": 0,
        "error": outcome.error,
        "duration_ms": int((time.perf_counter() - started) * 1000),
        "code": outcome.code,
        "pages_probed": outcome.pages_probed,
        "pages_hit": outcome.pages_hit,
        "pages_stripped": 0,
        "pages_skipped": 0,
        "source": "original",
        "copy_path": None,
    }
    if not outcome.hit:
        return status
    build = build_stripped_copy(path, dest, outcome)
    if build.copy_path:
        status.update(
            code="hit",
            pages_stripped=build.pages_stripped,
            pages_skipped=build.pages_skipped,
            source="copy",
            copy_path=build.copy_path,
            duration_ms=int((time.perf_counter() - started) * 1000),
        )
    else:  # 构建没产出副本：回落原件，但状态里必须显式可查
        code = build.code if not build.code.startswith("error") else "error"
        status.update(
            code=code,
            failed=code == "error",
            error=build.error or (None if code == "no_strippable_pages" else build.code),
            source="original",
            copy_path=None,
            duration_ms=int((time.perf_counter() - started) * 1000),
        )
    return status
