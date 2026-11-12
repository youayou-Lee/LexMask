"""Issue#257 一期 · 页状态投影（三态页格 / 部分导出选页器的唯一权威）。

投影只读三处既有数据，**不引入新表、不写 file_store**：

* ``performance.recognition.pages.<p>``——``task_queue_pipelines.run_page`` 逐页写的
  页账（``status`` 取 ``completed`` / ``failed`` / ``timeout``）；
* ``performance.recognition.failed_pages``——``#199`` 的失败清单，**原因码的权威**；
* item 状态机——"识别是否完成"的唯一权威（页账不能判完成：失败页也照样记账）。

设计要点（门①.5 冷审处置）：

* ``timeout`` 与 ``failed`` **都**投影成失败。真实形态里超时页记的是
  ``status="timeout"``，只认 ``failed`` 会让红叉页在页格里显示成功；
* 无页记录 = 未跑（``pending``），不得默认成功；
* ``reason`` 只在失败页给，且以 ``failed_pages`` 清单为准，清单缺该键才回落页记录的
  ``error``——重试成功的历史脏页记录里可能仍留着上次的 ``error``，不能污染成功格。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

# 这些类型有页的概念（页格/部分导出/单页重试只对它们成立）。
PAGED_FILE_TYPES: frozenset[str] = frozenset({"pdf", "pdf_scanned", "image"})

# item 状态 → 识别阶段。以状态机为权威，避免「页全绿但任务其实还在合并/打码」被误报完成。
_RECOGNITION_STATE_BY_ITEM_STATUS: dict[str, str] = {
    "draft": "not_started",
    "pending": "not_started",
    "queued": "not_started",
    "processing": "running",
    "awaiting_review": "done",
    "review_approved": "done",
    "redacting": "done",
    "completed": "done",
    "failed": "failed",
    "cancelled": "cancelled",
}

_PAGE_STATE_COMPLETED = "success"
_PAGE_STATE_FAILED = "failed"
_PAGE_STATE_PENDING = "pending"


def is_paged_file_type(file_type: Any) -> bool:
    raw = getattr(file_type, "value", file_type)
    return str(raw or "").strip().lower() in PAGED_FILE_TYPES


def recognition_state_for(item_status: Any) -> str:
    raw = getattr(item_status, "value", item_status)
    return _RECOGNITION_STATE_BY_ITEM_STATUS.get(str(raw or "").strip().lower(), "not_started")


def _coerce_dict(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}
    return {}


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _performance_of(item_row: dict[str, Any]) -> dict[str, Any]:
    if isinstance(item_row.get("performance"), dict):
        return item_row["performance"]
    return _coerce_dict(item_row.get("performance_json"))


def _page_count_of(item_row: dict[str, Any], file_info: dict[str, Any] | None) -> int:
    recognition = _coerce_dict(_performance_of(item_row).get("recognition"))
    count = _safe_int(recognition.get("page_count"), 0)
    if count > 0:
        return count
    if file_info:
        count = _safe_int(file_info.get("page_count"), 0)
    return max(count, 0)


def _failed_page_map(item_row: dict[str, Any]) -> dict[str, str]:
    recognition = _coerce_dict(_performance_of(item_row).get("recognition"))
    raw = recognition.get("failed_pages")
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for key, value in raw.items():
        page = _safe_int(key, -1)
        if page <= 0:
            continue
        out[str(page)] = str(value or "")
    return out


def _text_layer_route_of(record: dict[str, Any]) -> tuple[bool, str | None, Any]:
    """读页记录 ``pipeline_status.text_layer_doc`` → (该页是否被 docline 接管, fallback 原因码, coverage)。

    缺块 / 形状异常一律按「未接管、无码、无覆盖率」处理（``#165`` 之前的旧页记录
    没有 ``pipeline_status``，不能让投影对旧数据抛错）。
    """
    pipeline_status = record.get("pipeline_status")
    if not isinstance(pipeline_status, dict):
        return False, None, None
    text_layer_doc = pipeline_status.get("text_layer_doc")
    if not isinstance(text_layer_doc, dict):
        return False, None, None
    ran = text_layer_doc.get("ran") is True
    code = text_layer_doc.get("fallback_reason_code")
    code = str(code) if code not in (None, "") else None
    return ran, code, text_layer_doc.get("coverage")


def _route_summary_of(raw_pages: dict[str, Any], page_count: int) -> dict[str, Any] | None:
    """卷级路由摘要（Issue#279）：docline 接管优先，其次首个非空 fallback 码，两无 → None。

    与 per-page ``route_verdict`` 同源（同一份页账），但卷级只报「整卷走了哪条路」：
    任一页被 docline 接管即 ``docline``（fallback 码置 None）；否则整卷逐页时取
    第一个非空 ``fallback_reason_code`` 作人话解释；页账里毫无路由证据 → None。
    """
    docline_hit = False
    first_fallback: str | None = None
    for page_no in range(1, page_count + 1):
        record = raw_pages.get(str(page_no))
        if not isinstance(record, dict):
            continue
        ran, code, _coverage = _text_layer_route_of(record)
        if ran:
            docline_hit = True
        if first_fallback is None and code:
            first_fallback = code
    if docline_hit:
        return {"verdict": "docline", "fallback_reason_code": None}
    if first_fallback:
        return {"verdict": "page_pipeline", "fallback_reason_code": first_fallback}
    return None


def _page_detail_of(record: dict[str, Any], retry_counts: dict[str, Any], page_no: int) -> dict[str, Any]:
    """per-page「这页发生了什么」（Issue#279）：路由 / 分段耗时 / 失败类型 / 尝试次数。

    只读页账既有字段做投影，不改变三态判定（state/reason 逻辑零改动）；
    ``fallback_reason_code``/``coverage``/``stage_ms`` 缺省时省键（None-safe 透传），
    ``error_type``/``attempts`` 恒在（成功页 error_type=None、无账 attempts=1）。
    """
    ran, fallback_code, coverage = _text_layer_route_of(record)
    detail: dict[str, Any] = {
        "route_verdict": "docline" if ran else "page_pipeline",
        "error_type": str(record["error_type"]) if record.get("error_type") else None,
        # attempts 口径与事件埋点一致（task_queue_pipelines.retry_count_of）：已重试次数 + 1。
        "attempts": _safe_int(retry_counts.get(str(page_no)), 0) + 1,
    }
    if fallback_code is not None:
        detail["fallback_reason_code"] = fallback_code
    if coverage is not None:
        detail["coverage"] = coverage
    breakdown = record.get("duration_breakdown_ms")
    if isinstance(breakdown, dict) and breakdown:
        detail["stage_ms"] = dict(breakdown)
    if record.get("duration_ms") is not None:
        detail["duration_ms"] = _safe_int(record.get("duration_ms"), 0) or None
    return detail


def _page_state(record: dict[str, Any]) -> str:
    status = str(record.get("status") or "").strip().lower()
    if status == "completed":
        return _PAGE_STATE_COMPLETED
    if status in {"failed", "timeout"}:
        return _PAGE_STATE_FAILED
    # 起跑记录（只有 started_at）说明这页在跑但还没终态 ⇒ 未跑（页格仍是 ⚪，
    # 由 recognition_state=running 告诉前端"正在跑"）。
    return _PAGE_STATE_PENDING


def build_page_status(
    item_row: dict[str, Any],
    file_info: dict[str, Any] | None = None,
    *,
    snapshot_at: str | None = None,
) -> dict[str, Any]:
    """把 item 行 + 文件信息投影成页格/选页器所需的三态结构。"""
    file_type = (file_info or {}).get("file_type")
    if file_type is None:
        file_type = item_row.get("file_type")
    paged = is_paged_file_type(file_type)
    page_count = _page_count_of(item_row, file_info) if paged else 0

    recognition = _coerce_dict(_performance_of(item_row).get("recognition"))
    raw_pages = recognition.get("pages") if isinstance(recognition.get("pages"), dict) else {}
    failed_map = _failed_page_map(item_row)
    retry_counts = recognition.get("page_retry_counts")
    retry_counts = retry_counts if isinstance(retry_counts, dict) else {}

    pages: list[dict[str, Any]] = []
    for page_no in range(1, page_count + 1):
        record = raw_pages.get(str(page_no))
        record = record if isinstance(record, dict) else {}
        state = _page_state(record) if record else _PAGE_STATE_PENDING
        # 失败清单是权威（#199/#257）：只要页号在 failed_pages 里就是失败，即便页记录
        # 写着 completed。两套账本若因任何路径分叉（旧数据、并发写、手工修库）不一致，
        # 页格必须以"交付闸会拒收的那一侧"为准，否则会出现"页格全绿但导出被拒"的死局。
        if str(page_no) in failed_map:
            state = _PAGE_STATE_FAILED
        reason: str | None = None
        if state == _PAGE_STATE_FAILED:
            reason = failed_map.get(str(page_no)) or str(record.get("error") or "") or None
        pages.append(
            {
                "page": page_no,
                "state": state,
                "reason": reason,
                "duration_ms": _safe_int(record.get("duration_ms"), 0) or None,
                "cache_hit": bool(str(record.get("cache_status") or "").startswith("hit")),
                # Issue#279：这页发生了什么（路由/分段耗时/失败类型/尝试次数）——
                # 纯只读扩展，state/reason 判定不动。
                "detail": _page_detail_of(record, retry_counts, page_no),
            }
        )

    failed_pages: list[int] = []
    for key in failed_map:
        page = _safe_int(key, -1)
        if 0 < page <= page_count:
            failed_pages.append(page)

    return {
        "item_id": item_row.get("id"),
        "job_id": item_row.get("job_id"),
        "file_id": item_row.get("file_id"),
        "paged": paged,
        "page_count": page_count,
        "recognition_state": recognition_state_for(item_row.get("status")),
        # 原始状态：``recognition_state`` 为了三态展示把 awaiting_review 与 completed
        # 都折叠成 done，而页级重试只允许前者（后者属二期缓存续跑），所以必须单列。
        "item_status": str(item_row.get("status") or ""),
        "snapshot_at": snapshot_at or datetime.now(UTC).isoformat(),
        "pages": pages,
        "failed_pages": sorted(set(failed_pages)),
        # Issue#279：卷头路由摘要（docline 接管 / 逐页 + 首个 fallback 码 / 证据全无 → None）。
        "route_summary": _route_summary_of(raw_pages, page_count),
    }


def recognized_page_numbers(item_row: dict[str, Any], page_count: int) -> set[int]:
    """识别成功（``status=completed``）的页号集合——部分导出的选页白名单来源。"""
    recognition = _coerce_dict(_performance_of(item_row).get("recognition"))
    raw_pages = recognition.get("pages") if isinstance(recognition.get("pages"), dict) else {}
    out: set[int] = set()
    for key, record in raw_pages.items():
        page = _safe_int(key, -1)
        if not (0 < page <= page_count):
            continue
        if isinstance(record, dict) and _page_state(record) == _PAGE_STATE_COMPLETED:
            out.add(page)
    # 失败清单页即便记录写着 completed 也不算"识别成功"（与投影、交付闸同口径）。
    return out - set(failed_page_numbers(item_row))


def recognized_page_count(item_row: dict[str, Any], page_count: int) -> int:
    return len(recognized_page_numbers(item_row, page_count))


def page_count_of(item_row: dict[str, Any], file_info: dict[str, Any] | None = None) -> int:
    """页数三段回落（performance → file_info → 0）。列表接口与投影共用同一口径。"""
    return _page_count_of(item_row, file_info)


def failed_page_numbers(item_row: dict[str, Any]) -> list[int]:
    """失败清单里的页号（升序，不按 page_count 截断——列表接口要如实透出）。"""
    out: list[int] = []
    for key in _failed_page_map(item_row):
        page = _safe_int(key, -1)
        if page > 0:
            out.append(page)
    return sorted(set(out))


def failed_page_reasons(item_row: dict[str, Any]) -> dict[int, str]:
    """失败页 → 原因码（说明页/诊断面板要写原因码，页号转 int 便于比较）。"""
    out: dict[int, str] = {}
    for key, reason in _failed_page_map(item_row).items():
        page = _safe_int(key, -1)
        if page > 0:
            out[page] = reason
    return out
