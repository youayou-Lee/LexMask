# Copyright 2026 LexMask Contributors

"""Issue#280：失败页人工拉框兜底（``manual_verified``）的准入与提交/撤销。

与 :mod:`page_retry_service` 是**两条并行且可串联的兜底路径**（设计稿 §4.3）：
撤销补框后页回 ``failed_pages``，单页重试入口自然恢复可用。

语义边界（已裁定，不翻案）：

* **#199 红线不退让**：识别失败且无 ``manual_verified`` 的页永不进包。manual 页
  放行的唯一通道是带 auth 的显式 POST（+审计+事件+ledger），该动作物理把页移出
  ``recognition.failed_pages``——不存在「failed 页未处理却 ready」的状态组合；
* **框必填、文本可选**（ledger Ruling）：``boxes`` 为空 400，``text`` 可为 None
  （图形遮蔽照常打码）；
* **整卷重跑/单页重试不覆盖人工框**：写回点守卫在 ``redaction_orchestrator``
  （``source=="manual"`` 保留），人工框与自动框**共存**；整卷重跑即使自动成功，
  ledger 条目也保留（离开 manual_verified 的唯一通道 = 用户显式撤销）；
* verify 的「目标页整页替换」会丢弃该页旧框且 revoke 无快照可恢复 ⇒ 页不在
  ledger 的 ``source=="manual"`` 框（审阅期用户手拉的框）命中即 409
  ``manual_boxes_conflict``，绝不静默吞掉（r2 F-I4）；
* 并发：两个 operator 同时 verify 同一页 → 后写胜（RMW 窗口与整卷 finalize 同级，
  接受——与 ``_write_failed_pages`` 同一注释口径）。

页级迁移表（设计稿 §2.0；页状态的事实源 = 两本账 + 投影，非 item 级状态机）::

    PENDING          → SUCCESS | FAILED            （识别，既有）
    FAILED           → MANUAL_VERIFIED             （POST manual-verify，唯一人工入口）
    MANUAL_VERIFIED  → FAILED                      （DELETE manual-verify = 撤销；
                                                     或整卷重跑再失败——failed_pages
                                                     重新出现该页 ⇒ 投影以 failed_pages
                                                     为准回红叉，账本条目保留待重验/撤销）

账本形状（``performance_json.recognition.manual_verified_pages``，dict、键为
``str(page)``，与 ``failed_pages`` 同构）::

    {"5": {"verified_at": iso, "operator": str, "box_count": int,
           "original_reason": str}}

撤销 = 整条删除 ledger 条目 + ``original_reason`` 写回 ``failed_pages``（原因码
不丢）+ 审计留痕；不在 ledger 里留撤销残骸（避免投影处理第三种形状）。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from app.services.job_models import JobItemStatus
from app.services.page_status_service import PAGED_FILE_TYPES

logger = logging.getLogger(__name__)

MANUAL_LEDGER_PATH: tuple[str, ...] = ("recognition", "manual_verified_pages")
# 与 page_retry_service.SUPPORTED_RETRY_FILE_TYPES 同口径（设计稿 §2.1）。
SUPPORTED_MANUAL_FILE_TYPES = frozenset({"pdf", "pdf_scanned", "image"})
__all__ = [
    "MANUAL_LEDGER_PATH",
    "PAGED_FILE_TYPES",
    "SUPPORTED_MANUAL_FILE_TYPES",
    "PageManualError",
    "manual_ledger_of",
    "revoke_manual_verify",
    "submit_manual_verify",
    "validate_manual_verify",
]


class PageManualError(Exception):
    """人工补框请求被拒（机读 ``code`` + 人话 ``message``，同 PageRetryError 形态）。"""

    def __init__(self, code: str, message: str = "", **detail: Any) -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message or code
        self.detail = {"code": code, "message": self.message, **detail}


def _perf_of(item_row: dict[str, Any]) -> dict[str, Any]:
    perf = item_row.get("performance")
    if isinstance(perf, dict):
        return perf
    import json

    raw = item_row.get("performance_json")
    if isinstance(raw, str) and raw:
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _recognition_of(item_row: dict[str, Any]) -> dict[str, Any]:
    recognition = _perf_of(item_row).get("recognition")
    return recognition if isinstance(recognition, dict) else {}


def manual_ledger_of(item_row: dict[str, Any]) -> dict[int, dict[str, Any]]:
    """读 ledger（页号 int 键）。投影/导出/部分导出共用；形状异常按空账。"""
    raw = _recognition_of(item_row).get(MANUAL_LEDGER_PATH[1])
    if not isinstance(raw, dict):
        return {}
    out: dict[int, dict[str, Any]] = {}
    for key, value in raw.items():
        try:
            page = int(str(key))
        except (TypeError, ValueError):
            continue
        if page <= 0 or not isinstance(value, dict):
            continue
        out[page] = value
    return out


def _failed_pages_of(item_row: dict[str, Any]) -> dict[str, str]:
    raw = _recognition_of(item_row).get("failed_pages")
    if not isinstance(raw, dict):
        return {}
    return {str(key): str(value or "") for key, value in raw.items()}


def _box_source(box: Any) -> str:
    """框的 source（模型对象与 dict 两形态都吃；r3：模型按属性读）。"""
    if isinstance(box, dict):
        return str(box.get("source") or "")
    return str(getattr(box, "source", None) or "")


def _box_page(box: Any) -> int:
    try:
        if isinstance(box, dict):
            return int(box.get("page") or 1)
        return int(getattr(box, "page", None) or 1)
    except (TypeError, ValueError):
        return 1


def _pop_page_keys(mapping: dict, page: int) -> None:
    """按页 dict 形态里同页可能有 int/str 双键（历史快照 str、写回点 int）——
    整页替换前两键都摘掉，避免旧框在 str 键下残留、_boxes_by_page 归一时双计。"""
    mapping.pop(page, None)
    mapping.pop(str(page), None)


def _page_boxes_both_keys(mapping: dict, page: int) -> list[Any]:
    out: list[Any] = []
    for key in (page, str(page)):
        value = mapping.get(key)
        if isinstance(value, list):
            out.extend(value)
    return out


def _out_of_ledger_manual_boxes(page_boxes: Any, page: int) -> list[Any]:
    """目标页上「页不在 ledger 的」manual 框（审阅期用户手拉的框，r2 F-I4 口径）。"""
    items: list[Any] = []
    if isinstance(page_boxes, dict):
        for key, value in page_boxes.items():
            try:
                if int(str(key)) != page:
                    continue
            except (TypeError, ValueError):
                continue
            items.extend(value if isinstance(value, list) else [value])
    elif isinstance(page_boxes, list):
        items = [box for box in page_boxes if _box_page(box) == page]
    else:
        return []
    return [box for box in items if _box_source(box) == "manual"]


def _validate_boxes(boxes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """规范化并校验框（坐标 ∈ [0,1]、type 非空）。返回带 id/source 的新框 dict。"""
    normalized: list[dict[str, Any]] = []
    for index, box in enumerate(boxes):
        if not isinstance(box, dict):
            raise PageManualError("manual_boxes_invalid", f"box #{index} is not an object")
        coords = {}
        for key in ("x", "y", "width", "height"):
            value = box.get(key)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise PageManualError(
                    "manual_boxes_invalid", f"box #{index} field {key} must be a number",
                )
            if not (0.0 <= float(value) <= 1.0):
                raise PageManualError(
                    "manual_boxes_invalid",
                    f"box #{index} field {key}={value} outside [0,1]",
                    field=key,
                    value=value,
                )
            coords[key] = float(value)
        box_type = str(box.get("type") or "").strip()
        if not box_type:
            raise PageManualError(
                "manual_boxes_invalid", f"box #{index} has empty type", index=index,
            )
        text = box.get("text")
        normalized.append(
            {
                "id": uuid4().hex,
                "page": 0,  # submit 时按目标页回填
                **coords,
                "type": box_type,
                "text": str(text) if text not in (None, "") else None,
                "selected": True,
                "confidence": None,
                "source": "manual",
                "evidence_source": "manual",
            }
        )
    return normalized


def validate_manual_verify(
    item_row: dict[str, Any],
    page: int,
    boxes: list[Any],
    *,
    page_count: int,
    file_type: Any = None,
    page_boxes: Any = None,
) -> list[dict[str, Any]]:
    """人工补框准入校验。返回规范化框列表（未回填页号）；不合法抛 :class:`PageManualError`。

    ``page_boxes``：目标页当前框账（file_store 原始形态，dict/list 皆可）——
    用于 ``manual_boxes_conflict`` 判定（页不在 ledger 的 manual 框 ⇒ 409）。
    """
    raw_type = getattr(file_type, "value", file_type)
    if raw_type is not None and str(raw_type).lower() not in SUPPORTED_MANUAL_FILE_TYPES:
        raise PageManualError(
            "manual_verify_unsupported_type",
            f"manual verification requires a visual file type, got {raw_type}",
            file_type=str(raw_type),
        )

    status = str(item_row.get("status") or "")
    if status == JobItemStatus.PROCESSING.value:
        raise PageManualError(
            "item_busy", "another recognition task is running for this item",
        )
    if status != JobItemStatus.AWAITING_REVIEW.value:
        raise PageManualError(
            "manual_verify_requires_awaiting_review",
            "manual verification is only available while the item awaits review",
            status=status,
        )

    try:
        page_no = int(page)
    except (TypeError, ValueError):
        raise PageManualError("page_out_of_range", f"page must be an integer, got {page!r}") from None
    if page_no < 1 or page_no > max(1, int(page_count or 1)):
        raise PageManualError(
            "page_out_of_range", f"page {page_no} is outside 1..{page_count}",
            page=page_no, page_count=page_count,
        )

    failed_pages = _failed_pages_of(item_row)
    ledger = manual_ledger_of(item_row)
    in_failed = str(page_no) in failed_pages
    in_ledger = page_no in ledger
    if not in_failed and in_ledger:
        # r3 B3 补：页已在 ledger 且不在 failed_pages（非再失败态）⇒ 不允许重复 verify。
        raise PageManualError(
            "page_already_manual_verified",
            f"page {page_no} is already manually verified; revoke it first to redo",
            page=page_no,
        )
    if not in_failed:
        raise PageManualError(
            "page_not_failed", f"page {page_no} is not in the failed list", page=page_no,
        )

    # r2 F-I4：verify 是目标页整页替换，revoke 无快照可恢复 ⇒ 用户手拉框
    # （页不在 ledger 的 source=manual 框）不允许被静默吞掉。两个不触发 409 的例外：
    # (a) 自动框（页失败=识别没走完，残留自动框是半成品，整页替换正是本操作本意）；
    # (b) 页在 ledger 里的 manual 框（上一次 verify 写入的，重验=用户显式改框重交）。
    if page_boxes is not None and not in_ledger:
        conflicts = _out_of_ledger_manual_boxes(page_boxes, page_no)
        if conflicts:
            raise PageManualError(
                "manual_boxes_conflict",
                (
                    f"page {page_no} has {len(conflicts)} manually drawn box(es) from the "
                    "review canvas; remove them in the review canvas (or revoke the manual "
                    "verification) before annotating this page"
                ),
                page=page_no,
                conflicting_boxes=len(conflicts),
            )

    cleaned_input: list[dict[str, Any]] = []
    for box in boxes or []:
        cleaned_input.append(box if isinstance(box, dict) else _box_to_dict(box))
    if not cleaned_input:
        raise PageManualError(
            "manual_boxes_required",
            "at least one bounding box is required (text is optional)",
            page=page_no,
        )
    return _validate_boxes(cleaned_input)


def _box_to_dict(box: Any) -> dict[str, Any]:
    if isinstance(box, dict):
        return box
    if hasattr(box, "model_dump"):
        try:
            return box.model_dump()
        except Exception:  # noqa: BLE001 - 脏对象按 invalid 走统一错误面
            raise PageManualError("manual_boxes_invalid", "box is not serializable") from None
    raise PageManualError("manual_boxes_invalid", f"unsupported box payload {box!r}") from None


def _page_event(event: str, *, job_id: str, item_id: str, page: int, **fields: Any) -> None:
    """页事件旁路（同 submit_page_retry：fail-open，绝不影响主链）。"""
    try:
        from app.services import page_event_log as page_events

        page_events.log_event(
            event, page=int(page), job_id=job_id, item_id=item_id, **fields,
        )
    except Exception:  # noqa: BLE001 - 事件是旁路
        logger.debug("manual %s event emission failed", event, exc_info=True)


async def submit_manual_verify(
    *,
    store: Any,
    job: dict[str, Any],
    item: dict[str, Any],
    page: int,
    boxes: list[Any],
    operator: str,
    file_info: dict[str, Any] | None,
) -> dict[str, Any]:
    """落人工框（file_store + 审阅草稿）、移账本（failed_pages→manual_verified_pages）、
    发事件、写审计。返回 202 响应体。

    写入顺序（对齐 submit_page_retry 的「先账本后旁路」纪律的变体）：file_store 框
    → 草稿 → 账本 → 事件 → 审计。file_store 写持 ``_file_store_lock``（与 #196
    门⑥ I3 / detect_vision 写回同锁）。
    """
    item_id = str(item["id"])
    job_id = str(job["id"])
    file_id = str(item.get("file_id") or "")
    file_info = file_info or {}
    page_no = int(page)

    # 准入是 check-then-act：与 validate 之间隔着 await/锁窗口，投递前重读兜一道
    # （同 submit_page_retry 的 Critical 处置）。
    fresh_item = store.get_item(item_id) or {}
    page_count = max(1, _safe_int(file_info.get("page_count")) or _recognition_page_count(fresh_item))
    from app.services.file_management_service import _file_store_lock, file_store

    # 准入 conflict 判定读 file_store **当前**框账（validate 与 submit 之间的窗口内
    # 框账可能变化），不是 API 层快照。
    fresh_info = file_store.get(file_id) or file_info
    normalized = validate_manual_verify(
        fresh_item,
        page_no,
        boxes,
        page_count=page_count,
        file_type=file_info.get("file_type"),
        page_boxes=fresh_info.get("bounding_boxes"),
    )
    for box in normalized:
        box["page"] = page_no

    # 1. file_store 框：目标页整页替换（按页 dict 形态写回，与 detect_vision 写回点一致；
    #    int/str 双键同摘，防历史 str 键残留被 _boxes_by_page 归一时双计）。
    async def _write_file_store() -> None:
        async with _file_store_lock:
            info = file_store.get(file_id)
            if info is None:
                info = dict(fresh_info)
            if not isinstance(info.get("bounding_boxes"), dict):
                info["bounding_boxes"] = {}
            _pop_page_keys(info["bounding_boxes"], page_no)
            info["bounding_boxes"][page_no] = [dict(box) for box in normalized]
            file_store.set(file_id, info)

    await _write_file_store()

    # 2. 审阅草稿：草稿是交付期整份覆盖 file_store 的真源，漏并 = 交付丢人工框
    #    （_merge_retried_boxes_into_review_draft 的 Critical-2 同族）。无草稿跳过。
    draft = store.get_item_review_draft(item_id)
    if isinstance(draft, dict):
        draft_boxes = draft.get("bounding_boxes")
        draft_boxes = draft_boxes if isinstance(draft_boxes, list) else []
        kept = [
            box for box in draft_boxes
            if not (isinstance(box, dict) and _box_page(box) == page_no)
        ]
        draft = dict(draft)
        draft["bounding_boxes"] = [*kept, *[dict(box) for box in normalized]]
        store.save_item_review_draft(item_id, draft)

    # 3. 账本：两张表都整表替换（F1 语义）。读-改-写窗口与 _write_failed_pages 同级，
    #    并发双 verify 的丢更新风险与整卷 finalize 相同（接受并注释）。
    #    注意：get_item_performance 返回的就是 performance map（recognition 的父层），
    #    不能再过 _recognition_of（那会把它当 item_row 二次解包 → 恒空读）。
    fresh_recognition = (store.get_item_performance(item_id) or {}).get("recognition") or {}
    failed_pages = fresh_recognition.get("failed_pages")
    failed_pages = failed_pages if isinstance(failed_pages, dict) else {}
    ledger = fresh_recognition.get(MANUAL_LEDGER_PATH[1])
    ledger = dict(ledger) if isinstance(ledger, dict) else {}
    verified_at = datetime.now(UTC).isoformat()
    new_failed = {
        str(key): str(value or "")
        for key, value in failed_pages.items()
        if _safe_int(key, -1) != page_no
    }
    ledger[str(page_no)] = {
        "verified_at": verified_at,
        "operator": str(operator or "anonymous"),
        "box_count": len(normalized),
        "original_reason": failed_pages.get(str(page_no), ""),
    }
    store.update_item_performance(
        item_id,
        {
            "recognition": {
                "failed_pages": new_failed,
                MANUAL_LEDGER_PATH[1]: ledger,
            }
        },
        replace_paths=(("recognition", "failed_pages"), MANUAL_LEDGER_PATH),
    )
    store.touch_job_updated(job_id)

    # 4. 页事件（schema 白名单见 page_event_log.EVENT_SCHEMAS；operator 有意不入——
    #    人名走审计面 audit_log.user 与 ledger）。
    _page_event(
        "manual_verified", job_id=job_id, item_id=item_id,
        page=page_no, box_count=len(normalized),
    )

    # 5. 审计（签名对齐 app/core/audit.py:audit_log；operator 落顶层 user，r2 F-I1）。
    from app.core.audit import audit_log

    audit_log(
        "manual_verify_page",
        "job",
        job_id,
        user=str(operator or "anonymous"),
        detail={"item_id": item_id, "page": page_no, "box_count": len(normalized)},
    )
    logger.info(
        "manual verify stored: item=%s page=%d boxes=%d operator=%s",
        item_id[:8], page_no, len(normalized), operator,
    )
    return {
        "page": page_no,
        "state": "manual_verified",
        "box_count": len(normalized),
        "verified_at": verified_at,
        "operator": str(operator or "anonymous"),
        "item_status": str(fresh_item.get("status") or ""),
    }


async def revoke_manual_verify(
    *,
    store: Any,
    job: dict[str, Any],
    item: dict[str, Any],
    page: int,
    operator: str,
) -> dict[str, Any]:
    """撤销人工补框：页回 failed（写回原失败原因码）、ledger 整条删除、
    file_store/草稿删该页 manual 框、事件 + 审计。"""
    item_id = str(item["id"])
    job_id = str(job["id"])
    file_id = str(item.get("file_id") or "")
    page_no = int(page)

    fresh_item = store.get_item(item_id) or {}
    status = str(fresh_item.get("status") or "")
    if status == JobItemStatus.PROCESSING.value:
        raise PageManualError(
            "item_busy", "another recognition task is running for this item",
        )
    if status != JobItemStatus.AWAITING_REVIEW.value:
        raise PageManualError(
            "manual_verify_requires_awaiting_review",
            "manual revoke is only available while the item awaits review",
            status=status,
        )

    # 读账：get_item_performance 返回 performance map，直接取 recognition 子层
    # （不能过 _recognition_of 二次解包，否则恒空读 → 撤销永远 page_not_manual_verified）。
    fresh_recognition = (store.get_item_performance(item_id) or {}).get("recognition") or {}
    ledger = fresh_recognition.get(MANUAL_LEDGER_PATH[1])
    ledger = dict(ledger) if isinstance(ledger, dict) else {}
    entry = ledger.get(str(page_no))
    if not isinstance(entry, dict):
        raise PageManualError(
            "page_not_manual_verified",
            f"page {page_no} is not manually verified",
            page=page_no,
        )
    box_count = _safe_int(entry.get("box_count"), 0)

    # 1. failed_pages 写回。r3 §4.3 边角 2 处置：撤销写回时若页已在 failed_pages
    #    （整卷重跑再失败场景）则**不覆盖**现有原因码、仅确保页号在清单里——
    #    original_reason 的用途是「这页为什么曾需要人工」，不是「当前为什么失败」。
    failed_pages = fresh_recognition.get("failed_pages")
    failed_pages = failed_pages if isinstance(failed_pages, dict) else {}
    new_failed = {
        str(key): str(value or "")
        for key, value in failed_pages.items()
        if _safe_int(key, -1) != page_no
    }
    if str(page_no) in failed_pages:
        # 重跑再失败：保留 _write_failed_pages 刚写的新原因码，页号原样保留。
        new_failed[str(page_no)] = str(failed_pages[str(page_no)] or "")
    else:
        new_failed[str(page_no)] = str(entry.get("original_reason") or "")
    new_ledger = {
        key: value for key, value in ledger.items() if _safe_int(key, -1) != page_no
    }
    store.update_item_performance(
        item_id,
        {
            "recognition": {
                "failed_pages": new_failed,
                MANUAL_LEDGER_PATH[1]: new_ledger,
            }
        },
        replace_paths=(("recognition", "failed_pages"), MANUAL_LEDGER_PATH),
    )
    store.touch_job_updated(job_id)

    # 2. file_store：删该页 manual 框（自动框原本已在 verify 时清掉，页回红叉）。
    from app.services.file_management_service import _file_store_lock, file_store

    async def _strip_manual_boxes() -> None:
        async with _file_store_lock:
            info = file_store.get(file_id)
            if info is None:
                return
            raw = info.get("bounding_boxes")
            if not isinstance(raw, dict):
                return
            existing = _page_boxes_both_keys(raw, page_no)
            if not existing:
                return
            kept = [box for box in existing if _box_source(box) != "manual"]
            _pop_page_keys(raw, page_no)
            if kept:
                raw[page_no] = kept
            file_store.set(file_id, info)

    await _strip_manual_boxes()

    # 3. 草稿同删。
    draft = store.get_item_review_draft(item_id)
    if isinstance(draft, dict):
        draft_boxes = draft.get("bounding_boxes")
        if isinstance(draft_boxes, list):
            kept = [
                box for box in draft_boxes
                if not (
                    isinstance(box, dict)
                    and _box_page(box) == page_no
                    and _box_source(box) == "manual"
                )
            ]
            draft = dict(draft)
            draft["bounding_boxes"] = kept
            store.save_item_review_draft(item_id, draft)

    # 4. 页事件（box_count 取撤销时点的 ledger 快照口径）。
    _page_event(
        "manual_revoked", job_id=job_id, item_id=item_id, page=page_no, box_count=box_count,
    )

    # 5. 审计。
    from app.core.audit import audit_log

    audit_log(
        "manual_revoke_page",
        "job",
        job_id,
        user=str(operator or "anonymous"),
        detail={"item_id": item_id, "page": page_no, "box_count": box_count},
    )
    logger.info(
        "manual verify revoked: item=%s page=%d operator=%s", item_id[:8], page_no, operator,
    )
    return {
        "page": page_no,
        "state": "failed",
        "revoked_at": datetime.now(UTC).isoformat(),
        "operator": str(operator or "anonymous"),
    }


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _recognition_page_count(item_row: dict[str, Any]) -> int:
    return _safe_int(_recognition_of(item_row).get("page_count"))
