"""Issue#279 页级结构化事件日志（JSONL）——唯一写入口。

布局（spec §2.1，已批准偏离 Issue 单文件口径）：
    DATA_DIR/logs/<job_id>/<item_id>.jsonl   每卷一文件，页号在本文件内唯一
    DATA_DIR/logs/<job_id>/summary.json      任务级复盘（page_event_summary 落盘）

铁律（spec §2.2）：
* fail-open —— 本模块任何异常不得上抛；每类故障告警恰一次、计数可查（stats()）；
* schema 白名单 —— 事件字段必须命中 EVENT_SCHEMAS，未声明字段=拒写整事件+计数
  （不是静默丢弃）；这是「日志零案卷正文」的第一道闸：没有自由文本通道；
* O_APPEND 单次 write —— 每事件拼好整行后一次 os.write，多 worker
  （WEB_CONCURRENCY>1）下逐行完整不交插；
* 统一脱敏 —— 所有 str 字段值过 sanitize_text（截断 + PII 探针替换）；
  traceback_frames 只含代码帧（文件:行号:函数名），不含源码行与消息文本
  （本仓源码注释含长中文，长中文启发式会误伤，故帧内不带源码）。

上下文沿 app/core/request_id.py 的 ContextVar 先例：识别入口 set_event_context，
页级 set_current_page；docline/视觉链深处无需透传 id。
"""
from __future__ import annotations

import contextlib
import contextvars
import hashlib
import json
import logging
import os
import re
import shutil
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core.config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 事件 schema（白名单制）。required/optional 之外的任何字段名=拒写整事件。
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EventSchema:
    required: tuple[str, ...] = ()
    optional: tuple[str, ...] = ()

EVENT_SCHEMAS: dict[str, EventSchema] = {
    "route_decided": EventSchema(
        required=("source", "gate", "verdict"),
        optional=("signals", "thresholds_snapshot", "config_hash"),
    ),
    "text_layer_extracted": EventSchema(optional=("chars", "blocks", "pages", "ms")),
    "chunk_failed": EventSchema(
        required=("stage", "chunk_index", "error_type"),
        # Issue#279 Task5：kind 回补白名单（main/residual 标注调用趟次；
        # 块号直接用必填的 chunk_index，不另设 chunk_no 冗余字段）。
        optional=("traceback_frames", "message_sanitized", "kind"),
    ),
    "ner_done": EventSchema(optional=("calls", "ms", "entities", "regions", "supplemented")),
    "ocr_done": EventSchema(optional=("ms", "regions", "source")),
    "page_done": EventSchema(optional=("ms", "stage_ms", "regions", "source")),
    "page_failed": EventSchema(
        required=("stage", "error_type"),
        optional=("message_sanitized", "traceback_frames", "attempts", "ms"),
    ),
    # attempt 语义（终审 I-4 裁定）：retry_started/retry_done 的 attempt、重试路
    # page_failed 的 attempts = 该页累计重试序号（含本次），与 DB page_retry_counts 同基。
    "retry_started": EventSchema(required=("attempt",), optional=("trigger",)),
    "retry_done": EventSchema(required=("attempt", "outcome"), optional=("ms",)),
    # —— 以下四种为架构 v2（#272-#275）预留：writer 契约可验（A1.7），本期无生产者 ——
    "arbitration": EventSchema(
        required=("winner", "loser"), optional=("metric", "values"),
    ),
    "stage_changed": EventSchema(required=("to",), optional=("from",)),
    "accelerator_block_done": EventSchema(required=("block_index",), optional=("ms",)),
    "accelerator_block_failed": EventSchema(
        required=("block_index", "error_type"), optional=("message_sanitized",),
    ),
}

# ---------------------------------------------------------------------------
# route settings 子集（config_hash 与阈值快照的统一口径，spec Global Constraints）
# ---------------------------------------------------------------------------

ROUTE_SETTINGS_KEYS: tuple[str, ...] = (
    "TEXT_LAYER_DOCLINE_ENABLED",
    "TEXT_LAYER_DOCLINE_MAX_BUILD_SEC",
    "TEXT_LAYER_DOCLINE_BUILD_SEC_PER_PAGE",
    "TEXT_LAYER_DOCLINE_MAX_PAGES",
    "TEXT_LAYER_MAX_REPEATED_FRAGMENT_COVERAGE",
    "TEXT_LAYER_MAX_MAP_FAILURE_RATIO",
    "TEXT_LAYER_MAX_BLOCK_ERRORS",
    "TEXT_LAYER_MAX_TRUNCATED_RATIO",
    "TEXT_LAYER_MIN_COVERAGE",
    "TEXT_LAYER_MIN_CHARS_PER_PAGE",
    "TEXT_LAYER_MAX_SHORT_WORD_RATIO",
    "TEXT_LAYER_MAX_GARBAGE_RATIO",
    "TEXT_LAYER_MIN_AVG_WORD_LEN",
    "TEXT_LAYER_MIN_INK_ALIGNMENT",
    "TEXT_LAYER_MAX_WORD_CHARS",
    "TEXT_LAYER_PAGE_MIN_CHARS",
    # 重复片段闸的三个**测量**参数（终审 I-3，A1.4/A1.5 收口）：coverage 阈值之外，
    # 覆盖度本身随这三个键变——闸决策输入必须在 config_hash 里可复盘。
    "TEXT_LAYER_REPEATED_TOKEN_MIN_CHARS",
    "TEXT_LAYER_REPEATED_FRAGMENT_MIN_REPEATS",
    "TEXT_LAYER_REPEATED_FRAGMENT_MIN_PAGES",
)

# ---------------------------------------------------------------------------
# PII 探针（本模块脱敏与 scripts/audit_page_event_log.py --self-test 共用表）
# ---------------------------------------------------------------------------

PII_PROBE_RES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("id_card", re.compile(r"(?<!\d)[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx](?!\d)")),
    ("mobile", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    ("bank_card", re.compile(r"(?<!\d)\d{13,19}(?!\d)")),
    ("long_cjk", re.compile(r"[\u4e00-\u9fff]{20,}")),
)

def probe_hits(text: str) -> list[str]:
    return [name for name, pattern in PII_PROBE_RES if pattern.search(text or "")]

def sanitize_text(text: str, *, limit: int = 300) -> str:
    """截断 + PII 探针替换。替换值带内容哈希（同值同替、可对账、不可逆）。"""
    raw = str(text or "")
    if not raw:
        return ""
    out = raw
    for name, pattern in PII_PROBE_RES:
        # _n 经默认参数绑定当前 name（ruff B023：避免闭包迟到绑定）
        out = pattern.sub(lambda m, _n=name: f"<redacted:{_n}:{_short_hash(m.group(0))}>", out)
    return out[:limit]

def _short_hash(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8", "ignore")).hexdigest()[:10]

def traceback_frames(exc: BaseException) -> list[str]:
    """代码帧列表（文件:行号 in 函数），不含源码行与异常消息文本。"""
    import traceback as _tb
    try:
        return [
            f"{frame.filename}:{frame.lineno} in {frame.name}"
            for frame in _tb.extract_tb(exc.__traceback__)
        ]
    except Exception:  # noqa: BLE001 - 取帧失败按空帧处理（fail-open）
        return []

# ---------------------------------------------------------------------------
# 上下文
# ---------------------------------------------------------------------------

_ctx: contextvars.ContextVar[dict[str, str] | None] = contextvars.ContextVar("page_event_ctx", default=None)
_page_var: contextvars.ContextVar[int | None] = contextvars.ContextVar("page_event_page", default=None)

class _Unset:
    ...

_UNSET = _Unset()

@contextlib.contextmanager
def event_context(job_id: str, item_id: str, file_id: str = ""):
    """任务级上下文（识别入口/重试入口调用；asyncio 子协程自动继承）。"""
    token = _ctx.set({"job_id": str(job_id), "item_id": str(item_id), "file_id": str(file_id or "")})
    try:
        yield
    finally:
        _ctx.reset(token)

def set_event_context(job_id: str, item_id: str, file_id: str = "") -> None:
    """非 with 形态（长生命周期任务入口用，退出时显式 clear_event_context）。"""
    _ctx.set({"job_id": str(job_id), "item_id": str(item_id), "file_id": str(file_id or "")})

def clear_event_context() -> None:
    _ctx.set(None)

def _is_valid_page(value: Any) -> bool:
    """合法页号：int 且非 bool（bool 是 int 子类，须显式排除）。"""
    return isinstance(value, int) and not isinstance(value, bool)

def set_current_page(page: int | None) -> None:
    """fail-open（评审 r1 Finding 1）：非法 page（非 int/bool）一律置 None，绝不向调用链上抛。"""
    _page_var.set(page if _is_valid_page(page) else None)

# ---------------------------------------------------------------------------
# 配置哈希与阈值快照
# ---------------------------------------------------------------------------

def route_thresholds_snapshot(keys: tuple[str, ...]) -> dict[str, Any]:
    """闸阈值快照。fail-open（spec 铁律「日志任何故障 fail-open」，终审 I-2）：
    单键读取失败（缺键/设置改名）跳过该键；整体故障返回 {}；绝不上抛——
    本函数在 docline 闸帧内被调用，AttributeError 外泄会改变识别路由。"""
    out: dict[str, Any] = {}
    for key in keys:
        try:
            out[key] = getattr(settings, key)
        except Exception:  # noqa: BLE001 - 缺键不进快照（宁缺勿错，快照是旁路证据）
            continue
    return out

def route_config_hash() -> str:
    """route 设置子集（ROUTE_SETTINGS_KEYS）的内容哈希。fail-open（终审 I-2）：
    任何读取故障返回 ""（空串=本任务 hash 不可用，可判别），绝不上抛。"""
    try:
        payload = {k: repr(getattr(settings, k)) for k in ROUTE_SETTINGS_KEYS}
    except Exception:  # noqa: BLE001 - 子集键缺失时 hash 不可信，给空串而非崩闸
        return ""
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]

# ---------------------------------------------------------------------------
# 写入器
# ---------------------------------------------------------------------------

_stats_lock = threading.Lock()
_stats: dict[str, int] = {
    "written": 0, "dropped_disabled": 0, "dropped_unknown_event": 0,
    "dropped_unknown_field": 0, "dropped_no_context": 0,
    "dropped_unserializable": 0, "write_errors": 0,
}
_warned_causes: set[str] = set()
_fd_cache: dict[str, int] = {}          # job_id -> fd（O_APPEND 只写）
_stats_reported = threading.Lock()

def _bump(counter: str) -> None:
    with _stats_lock:
        _stats[counter] = _stats.get(counter, 0) + 1

def _warn_once(cause: str, message: str) -> None:
    with _stats_lock:
        first = cause not in _warned_causes
        _warned_causes.add(cause)
    if first:
        logger.warning("%s", message)

def reset_for_tests() -> None:
    """清 fd 缓存与计数器（仅测试注入路径使用）。"""
    global _stats, _warned_causes
    for fd in list(_fd_cache.values()):
        with contextlib.suppress(OSError):
            os.close(fd)
    _fd_cache.clear()
    _stats = {k: 0 for k in _stats}
    _warned_causes.clear()
    clear_event_context()
    set_current_page(None)

def set_log_root(root: str) -> None:
    """测试注入覆盖日志根目录（生产走 settings 推导）。"""
    global _log_root_override
    _log_root_override = root if root else None

_log_root_override: str | None = None

def _log_root() -> Path:
    if _log_root_override:
        return Path(_log_root_override)
    configured = str(getattr(settings, "PAGE_EVENT_LOG_DIR", "") or "").strip()
    base = Path(configured) if configured else Path(str(settings.DATA_DIR)) / "logs"
    return base

def job_log_dir(job_id: str) -> Path:
    return _log_root() / _safe_component(job_id)

def _safe_component(value: str) -> str:
    """路径成分消毒：只留字母数字 _ - .；其余转 _（A1.8，非法 job_id 不逃逸目录）。

    '.'/'..' 虽全是合法字符，但作为目录成分会相对逃逸日志根（delete_job_logs('..')
    会 rmtree 到根之外）——显式钉成 '_'（评审 r1 Finding 2）。
    """
    cleaned = re.sub(r"[^A-Za-z0-9_.-]", "_", str(value or ""))
    if cleaned in {".", ".."}:
        return "_"
    return cleaned or "_"

def _open_fd(job_id: str, item_id: str) -> int:
    # 终审 I-1：缓存键按**消毒后**成分算——与磁盘目录名/文件名同一坐标系。
    # 否则 retention sweep 以消毒目录名 close_job 时关不到 raw 键的缓存条目：
    # 目录被 rmtree 后 fd 指向已 unlink 的 inode，后续 log_event 照常「成功」
    # 计数却写进虚空（fd 泄漏 + 事件静默丢失）。
    jid_s = _safe_component(job_id)
    iid_s = _safe_component(item_id)
    key = f"{jid_s}\x00{iid_s}"
    fd = _fd_cache.get(key)
    if fd is not None:
        return fd
    directory = job_log_dir(job_id)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{iid_s}.jsonl"
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    _fd_cache[key] = fd
    return fd

def _validate_fields(event: str, fields: dict[str, Any]) -> str | None:
    """返回拒绝原因码；None=通过。"""
    schema = EVENT_SCHEMAS.get(event)
    if schema is None:
        return "unknown_event"
    allowed = set(schema.required) | set(schema.optional)
    unknown = [k for k in fields if k not in allowed]
    missing = [k for k in schema.required if k not in fields]
    if unknown or missing:
        return "unknown_field" if unknown else "missing_required"
    return None

def log_event(
    event: str,
    *,
    page: int | None | _Unset = _UNSET,
    job_id: str | None = None,
    item_id: str | None = None,
    **fields: Any,
) -> bool:
    """唯一写入口。返回是否落盘；任何丢弃/失败只计数与一次性告警，绝不上抛。"""
    if not bool(getattr(settings, "PAGE_EVENT_LOG_ENABLED", True)):
        _bump("dropped_disabled")
        return False
    ctx = _ctx.get() or {}
    jid = str(job_id or ctx.get("job_id") or "")
    iid = str(item_id or ctx.get("item_id") or "")
    if not jid or not iid:
        _bump("dropped_no_context")
        return False
    page_val = _page_var.get() if isinstance(page, _Unset) else page
    # fail-open（评审 r1 Finding 1）：page 非法（非 int；bool 是 int 子类须排除）时
    # 拒写整事件+计数（dropped_unserializable），与 schema 白名单「拒写整事件」同哲学；
    # 绝不因坏 page 向识别链上抛。不选「写 page=None 半截行」——页号是本日志主维度。
    if page_val is not None and not _is_valid_page(page_val):
        _bump("dropped_unserializable")
        _warn_once("unserializable", f"page_event_log: event {event} has invalid page value, dropped")
        return False
    reason = _validate_fields(event, fields)
    if reason == "unknown_event":
        _bump("dropped_unknown_event")
        _warn_once("unknown_event", "page_event_log: unknown event type rejected")
        return False
    if reason is not None:
        _bump("dropped_unknown_field")
        _warn_once("unknown_field", f"page_event_log: event {event} rejected (reason={reason})")
        return False
    record: dict[str, Any] = {
        "ts": _utc_iso(),
        "job_id": jid,
        "item_id": iid,
        "page": None if page_val is None else int(page_val),
        "event": event,
    }
    for key, value in fields.items():
        if isinstance(value, str):
            record[key] = sanitize_text(value)
        else:
            record[key] = value
    try:
        line = (json.dumps(record, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
    except (TypeError, ValueError):
        _bump("dropped_unserializable")
        _warn_once("unserializable", f"page_event_log: event {event} has unserializable value, dropped")
        return False
    try:
        fd = _open_fd(jid, iid)
        os.write(fd, line)  # O_APPEND：单次 write，多进程逐行完整
        _bump("written")
        return True
    except OSError as exc:
        _bump("write_errors")
        _warn_once("write_error", f"page_event_log: write failed ({getattr(exc, 'errno', '?')}), recognition unaffected")
        # 丢弃坏 fd，恢复后重开（A1.2「恢复后可续写」）。fd 可能是 _open_fd 抛错
        # 前未取得的——从缓存按 key 取，不许引用未绑定变量。key 与 _open_fd 同消毒
        # 口径（终审 I-1）。
        key = f"{_safe_component(jid)}\x00{_safe_component(iid)}"
        cached = _fd_cache.get(key)
        if cached is not None:
            with contextlib.suppress(OSError):
                os.close(cached)
            _fd_cache.pop(key, None)
        return False

def _utc_iso() -> str:
    from datetime import UTC, datetime
    return datetime.now(UTC).isoformat(timespec="milliseconds")

def close_job(job_id: str) -> None:
    # 参数与缓存键同消毒（终审 I-1）：retention sweep 传的是消毒后的目录名，
    # 调用方也可能传原始 id——两种形态都必须命中同一条缓存（幂等）。
    prefix = f"{_safe_component(job_id)}\x00"
    for key, fd in list(_fd_cache.items()):
        if key.startswith(prefix):
            with contextlib.suppress(OSError):
                os.close(fd)
            _fd_cache.pop(key, None)

def delete_job_logs(job_id: str) -> None:
    """任务删除钩子（A5.1）：整目录移除，fail-open。"""
    close_job(job_id)
    directory = job_log_dir(job_id)
    try:
        if directory.exists():
            shutil.rmtree(directory)
    except OSError as exc:
        _warn_once("delete_logs", f"page_event_log: failed to delete logs for job ({exc})")

def stats() -> dict[str, int]:
    """写入器计数快照（written + 各 dropped_* + write_errors）；返回副本，调用方可安全持有。"""
    with _stats_lock:
        return dict(_stats)
