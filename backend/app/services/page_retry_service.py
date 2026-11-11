# Copyright 2026 LexMask Contributors
"""Issue#257 一期能力 2：失败页单页重试的准入与提交。

设计边界（方案 v2 §3.5）：

* 只允许「识别已完成、尚未确认」（``awaiting_review``）的条目重试——``processing``
  与进行中的 #199 隔离逻辑互斥，``completed`` 属二期（``VALID_ITEM_TRANSITIONS``
  里 ``COMPLETED`` 无出边，放开它等于改 #233 语义）；
* 只能重试**已在失败清单里**的页：成功页的「重试」没有语义（正常路径是 reject 整卷重跑），
  放行它会让页账与实体/框之间出现无声漂移；
* 同一 item 同一时刻只允许一条重试任务：``performance.recognition.page_retry_pages``
  非空即视为在跑（重启后靠它让补偿重派 ``page_retry``，见 ``job_store_repair``）。
"""

from __future__ import annotations

import logging
from typing import Any

from app.services.job_models import JobItemStatus

logger = logging.getLogger(__name__)

PAGE_RETRY_MARKER_PATH: tuple[str, ...] = ("recognition", "page_retry_pages")
SUPPORTED_RETRY_FILE_TYPES = frozenset({"pdf", "pdf_scanned", "image"})


class PageRetryError(Exception):
    """重试请求被拒（机读 ``code`` + 人话 ``message``）。"""

    def __init__(self, code: str, message: str = "", **detail: Any) -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message or code
        self.detail = {"code": code, "message": self.message, **detail}


def _failed_page_numbers(failed_pages: dict[str, Any]) -> set[int]:
    out: set[int] = set()
    for key in failed_pages or {}:
        try:
            out.add(int(str(key)))
        except (TypeError, ValueError):
            continue
    return out


def _perf_of(item_row: dict[str, Any]) -> dict[str, Any]:
    perf = item_row.get("performance")
    if isinstance(perf, dict):
        return perf
    raw = item_row.get("performance_json")
    if isinstance(raw, str) and raw:
        import json

        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def in_flight_pages(item_row: dict[str, Any]) -> list[int]:
    """本次若在重试中，返回在飞页号（供准入判断与重启补偿读）。"""
    recognition = _perf_of(item_row).get("recognition") or {}
    raw = recognition.get(PAGE_RETRY_MARKER_PATH[1]) or []
    out: list[int] = []
    for value in raw if isinstance(raw, list) else []:
        try:
            out.append(int(value))
        except (TypeError, ValueError):
            continue
    return out


def validate_page_retry(
    item_row: dict[str, Any],
    pages: list[int],
    *,
    page_count: int,
    failed_pages: dict[str, Any],
    file_type: str | None = None,
    busy_pages: list[int] | None = None,
) -> list[int]:
    """准入校验，返回去重升序后的页号；不合法抛 :class:`PageRetryError`。"""
    if file_type and str(file_type).lower() not in SUPPORTED_RETRY_FILE_TYPES:
        raise PageRetryError(
            "partial_retry_unsupported_type",
            f"page retry requires a visual file type, got {file_type}",
            file_type=file_type,
        )

    pending = busy_pages if busy_pages is not None else in_flight_pages(item_row)
    status = str(item_row.get("status") or "")
    # 「忙」只认处理中状态：标记非空但状态已回到 awaiting_review，说明上一次重试的
    # 收尾没走完（或重启把状态改了），这时必须允许重新提交，否则标记会永久卡住这个
    # item（完整评审 Critical-1）。新提交会覆盖旧标记，页清单取新的那份。
    if status == JobItemStatus.PROCESSING.value:
        raise PageRetryError(
            "item_busy",
            "another recognition task is running for this item",
            pages=sorted(pending),
        )
    if status != JobItemStatus.AWAITING_REVIEW.value:
        raise PageRetryError(
            "retry_requires_awaiting_review",
            "page retry is only available while the item awaits review "
            "(items already delivered need the phase-2 cache resume path)",
            status=status,
        )

    cleaned: list[int] = []
    for value in pages or []:
        try:
            page = int(value)
        except (TypeError, ValueError):
            raise PageRetryError("pages_invalid", f"page must be an integer, got {value!r}") from None
        if page < 1 or page > max(1, int(page_count or 1)):
            raise PageRetryError(
                "pages_out_of_range",
                f"page {page} is outside 1..{page_count}",
                page=page,
                page_count=page_count,
            )
        if page not in cleaned:
            cleaned.append(page)
    if not cleaned:
        raise PageRetryError("pages_empty", "no page selected for retry")

    failed = _failed_page_numbers(failed_pages)
    not_failed = [page for page in cleaned if page not in failed]
    if not_failed:
        raise PageRetryError(
            "page_not_failed",
            f"page(s) {not_failed} are not in the failed list",
            pages=not_failed,
        )
    return sorted(cleaned)


async def submit_page_retry(
    *,
    store: Any,
    job: dict[str, Any],
    item: dict[str, Any],
    pages: list[int],
    enqueue: Any | None = None,
) -> dict[str, Any]:
    """落标记、把 item 置 processing、投递 ``page_retry`` 任务。

    写入顺序是「状态 → 标记 → 投递」：中途崩溃时最坏结果是 item 停在
    ``processing`` 且标记为空，``repair_stale_processing_items`` 会把它当整卷
    重识别派发（慢但正确）；反过来先写标记会导致「标记在、状态未变」的死锁
    （准入永远判 ``item_busy``）。
    """
    from app.services.task_queue import TaskItem, get_task_queue

    item_id = str(item["id"])
    # 准入是 check-then-act（validate 与 submit 之间隔着一次 await）：并发两笔时第二笔
    # 会把标记覆盖成自己的页，而队列按 (task_type, item_id) 去重、只跑第一笔的 meta，
    # 第二笔的页就静默丢了（完整评审 Important-1）。投递前**重读**状态兜一道。
    fresh = store.get_item(item_id) or {}
    if str(fresh.get("status") or "") != JobItemStatus.AWAITING_REVIEW.value:
        raise PageRetryError(
            "item_busy",
            "another recognition task is running for this item",
            pages=in_flight_pages(fresh),
        )
    store.update_item_status(item_id, JobItemStatus.PROCESSING)
    store.update_item_performance(
        item_id,
        {"recognition": {PAGE_RETRY_MARKER_PATH[1]: list(pages)}},
        replace_paths=(PAGE_RETRY_MARKER_PATH,),
    )
    store.touch_job_updated(str(job["id"]))

    # Issue#279：retry_started 逐页一条，attempt = 既有重试计数 + 1（submit 期口径；
    # 真实终局序号由 _run_page_retry 的 retry_done 钉）。submit 跑在 API 请求协程里、
    # 无页事件上下文 ⇒ job/item 显式传参，不依赖 ContextVar。读账/写事件全程
    # fail-open：读账失败按无账（attempt=1）继续逐页发，绝不影响「标记 → 投递」链。
    try:
        from app.services import page_event_log as page_events

        try:
            recognition = (store.get_item_performance(item_id) or {}).get("recognition") or {}
            prev_counts = recognition.get("page_retry_counts") or {}
            if not isinstance(prev_counts, dict):
                prev_counts = {}
        except Exception:  # noqa: BLE001 - 读账失败按无账处理（提交链不受影响）
            logger.debug("retry_started: unable to read page_retry_counts", exc_info=True)
            prev_counts = {}
        for page in pages:
            attempt = 1
            try:
                attempt = int(prev_counts.get(str(page), 0)) + 1
            except (TypeError, ValueError):
                attempt = 1
            page_events.log_event(
                "retry_started",
                page=int(page),
                attempt=attempt,
                trigger="manual",
                job_id=str(job["id"]),
                item_id=item_id,
            )
    except Exception:  # noqa: BLE001 - 事件是旁路，提交链（状态/标记/投递）不受影响
        logger.debug("retry_started event emission failed", exc_info=True)

    task = TaskItem(
        job_id=str(job["id"]),
        item_id=item_id,
        file_id=str(item["file_id"]),
        task_type="page_retry",
        meta={"pages": list(pages)},
    )
    if enqueue is None:
        get_task_queue().enqueue(task)
    else:
        enqueue(task)
    logger.info(
        "page retry queued: item=%s pages=%s", item_id[:8], pages,
    )
    return {"queued_pages": list(pages), "item_id": item_id}
