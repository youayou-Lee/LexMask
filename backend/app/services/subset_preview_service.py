"""Issue#325：子集预览打码（不触碰 item 生命周期）。

设计要点（门⑥ C-1/C-3 返工）：
- 识别运行中即可调用：本服务**不迁移 item/job 状态**，也不写 ``output_path``；
- 产物独立存放（``preview_output_path``），与主产物解耦，可反复重跑；
- 只读 file_store 快照 + 自己的临时切片，不参与识别 worker 的写路径；
- 页集合必须在可导出页（success ∪ manual_verified）内，显式空集 400；
- 文本型 PDF 保留 ``FileType.PDF``，让实体→框定位在**切片卷**上发生（门⑥ C-2）。
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from typing import Any

import fitz

from app.core.config import settings
from app.models.entity_schemas import BoundingBox, Entity
from app.services import page_status_service
from app.services.partial_export_service import (
    PartialExportError,
    remap_boxes_to_subset,
    slice_pdf_pages,
)

logger = logging.getLogger(__name__)


def _remove_quietly(path: str | None) -> None:
    if not path:
        return
    try:
        os.remove(path)
    except OSError:
        pass


def _remove_stale_preview(path: str, file_id: str = "") -> None:
    """Issue#332：删上一份子集预览（路径必须在 OUTPUT_DIR 内，best-effort）。

    失败口径（与 ``redaction_orchestrator._discard_subset_preview_file`` 相近但不完全一致——
    那边用 ``os.path.exists`` 预判、不区分「已不存在」）：路径已不存在（并发/重试）静默，
    其余 ``OSError`` 留 warning——否则孤儿文件无声堆积，运维无从发现。
    """
    try:
        from app.core.file_validation import safe_path_in_dir

        if not safe_path_in_dir(path, settings.OUTPUT_DIR):
            logger.warning("Refuse to delete subset preview outside OUTPUT_DIR: %s", path)
            return
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        except OSError:
            logger.warning(
                "Failed to delete stale subset preview for %s: %s", file_id, path, exc_info=True
            )
    except Exception:  # 清理是 best-effort，任何异常都不得影响主流程
        logger.warning(
            "Unexpected error while deleting stale subset preview %s", path, exc_info=True
        )


def _has_selected_regions(
    boxes: Sequence[Any], entities: Sequence[Any], pages: Sequence[int]
) -> bool:
    """所选页上至少有一个选中的框/实体才算「会打码」；零命中不得静默返回 200。"""
    wanted = {int(p) for p in pages}
    for box in boxes:
        if getattr(box, "selected", True) is not False:
            return True
    for ent in entities:
        if getattr(ent, "selected", True) is not False and int(getattr(ent, "page", 1) or 1) in wanted:
            return True
    return False


def _normalize_pages(pages: Sequence[int], page_count: int) -> list[int]:
    """1-based 升序去重；空/越界/负数一律 400（不静默退化全卷）。"""
    if not pages:
        raise PartialExportError("subset_pages_empty", "至少选择一页后再开始匿名化")
    normalized = sorted({int(p) for p in pages})
    if normalized[0] < 1 or normalized[-1] > page_count:
        raise PartialExportError(
            "subset_pages_out_of_range",
            f"页号必须在 1..{page_count} 内",
            pages=[p for p in normalized if p < 1 or p > page_count],
        )
    return normalized


def _normalize_boxes(raw: Iterable[Any] | None) -> list[BoundingBox]:
    out: list[BoundingBox] = []
    for item in raw or []:
        if isinstance(item, BoundingBox):
            out.append(item)
        elif isinstance(item, dict):
            out.append(BoundingBox.model_validate(item))
    return out


def _normalize_entities(raw: Iterable[Any] | None) -> list[Entity]:
    out: list[Entity] = []
    for item in raw or []:
        if isinstance(item, Entity):
            out.append(item)
        elif isinstance(item, dict):
            out.append(Entity.model_validate(item))
    return out


def _boxes_from_snapshot(raw: Any) -> list[BoundingBox]:
    """file_store 的框账可能是 dict{页:list} 或扁平 list，统一成 list。"""
    out: list[BoundingBox] = []
    if isinstance(raw, dict):
        for page_key, arr in raw.items():
            if not isinstance(arr, list):
                continue
            default_page = int(page_key) if str(page_key).isdigit() else 1
            for box in arr:
                if isinstance(box, BoundingBox):
                    out.append(box)
                elif isinstance(box, dict):
                    out.append(BoundingBox.model_validate({**box, "page": box.get("page", default_page)}))
    elif isinstance(raw, list):
        for box in raw:
            if isinstance(box, BoundingBox):
                out.append(box)
            elif isinstance(box, dict):
                out.append(BoundingBox.model_validate(box))
    return out


async def create_subset_preview(
    *,
    file_store: Any,
    store: Any,
    job_row: dict[str, Any],
    item_row: dict[str, Any],
    file_info: dict[str, Any],
    config: Any,
    pages: Sequence[int],
    bounding_boxes: Sequence[Any] | None = None,
    entities: Sequence[Any] | None = None,
) -> dict[str, Any]:
    """对 ``pages`` 生成一份预览打码 PDF，返回产物元数据（不改 item 状态）。"""
    page_count = int(file_info.get("page_count") or 0)
    if page_count <= 0:
        raise PartialExportError("subset_page_count_unknown", "文件页数未知，无法生成子集预览")

    normalized = _normalize_pages(pages, page_count)
    exportable = page_status_service.exportable_page_numbers(item_row, page_count)
    file_type = str(file_info.get("file_type") or "").lower()
    # C-4：文本型 PDF 的 NER 是整卷异步任务，生产账本不写逐页 performance.pages；
    # 识别完成（AWAITING_REVIEW/COMPLETED）即视为全卷可用。
    text_pdf_finished = file_type == "pdf" and str(item_row.get("status") or "") in {
        "awaiting_review",
        "review_approved",
        "redacting",
        "completed",
    }
    if (
        text_pdf_finished
        and not exportable
        and not page_status_service.failed_page_numbers(item_row)
    ):
        exportable = set(range(1, page_count + 1))
    not_ready = [p for p in normalized if int(p) not in {int(x) for x in exportable}]
    if not_ready:
        raise PartialExportError(
            "subset_pages_not_ready",
            "所选页尚未识别成功或未人工确认，不能进入子集打码",
            pages=not_ready,
        )

    if file_type not in {"pdf", "pdf_scanned"}:
        raise PartialExportError(
            "subset_unsupported_type", "子集打码当前仅支持 PDF（文本型/扫描型）"
        )

    source_path = str(file_info.get("file_path") or "")
    if not source_path or not os.path.isfile(source_path):
        raise PartialExportError("subset_source_unreadable", "原始文件不存在或不可读")

    boxes = _normalize_boxes(bounding_boxes) if bounding_boxes is not None else _boxes_from_snapshot(
        file_info.get("bounding_boxes")
    )
    ents = _normalize_entities(entities) if entities is not None else _normalize_entities(
        file_info.get("entities")
    )

    # 切片卷：文本型 PDF 保留 FileType.PDF（实体→框定位在切片卷上发生）；
    # 扫描件/图片走 PDF_SCANNED（只吃重映射后的视觉框）。
    effective_type = "pdf" if file_type == "pdf" else "pdf_scanned"

    tmp_subset = os.path.join(
        settings.OUTPUT_DIR, f"subset-src-{uuid.uuid4().hex}.pdf"
    )
    preview_path = os.path.join(
        settings.OUTPUT_DIR, f"subset-preview-{uuid.uuid4().hex}.pdf"
    )
    redacted_output: str | None = None
    try:
        await asyncio.to_thread(slice_pdf_pages, source_path, normalized, tmp_subset)
        remapped = remap_boxes_to_subset(boxes, normalized)
        if not _has_selected_regions(remapped, ents, normalized):
            raise PartialExportError(
                "subset_no_regions",
                "所选页没有任何选中框或实体，预览不会产生打码；请先补框或确认实体",
            )

        from app.services.redactor import Redactor

        effective_info = {
            "file_path": tmp_subset,
            "file_type": effective_type,
            "is_scanned": effective_type == "pdf_scanned",
            "page_count": len(normalized),
        }
        config_copy = _config_copy_keep(config)
        result = await Redactor().redact(effective_info, ents, remapped, config_copy)
        redacted_output = str(result.get("output_path") or "")
        if int(result.get("redacted_count") or 0) == 0:
            raise PartialExportError(
                "subset_no_regions",
                "所选页没有任何实际打码命中，预览不会产生打码；请先补框或确认实体",
            )
        if not redacted_output or not os.path.isfile(redacted_output):
            raise PartialExportError("subset_preview_failed", "子集预览打码未产出文件")

        with fitz.open(redacted_output) as doc:
            actual = doc.page_count
        if actual != len(normalized):
            raise PartialExportError(
                "subset_preview_page_count_mismatch",
                f"预览页数 {actual} != 预期 {len(normalized)}",
                expected=len(normalized),
                actual=actual,
            )
        await asyncio.to_thread(os.replace, redacted_output, preview_path)
    except BaseException:
        _remove_quietly(tmp_subset)
        _remove_quietly(redacted_output)
        _remove_quietly(preview_path)
        raise
    finally:
        _remove_quietly(tmp_subset)

    page_map = {idx + 1: page for idx, page in enumerate(normalized)}
    payload = {
        "preview_output_path": preview_path,
        "processed_pages": normalized,
        "page_map": page_map,
        "preview_entity_map": dict(result.get("entity_map") or {}),
        "preview_redacted_count": int(result.get("redacted_count") or 0),
        "preview_created_at": datetime.now(UTC).isoformat(),
        "preview_page_count": len(normalized),
    }
    # C-1：与识别写路径共用同一把 file_store 锁，避免非原子读改写互相覆盖。
    fid = str(file_info.get("file_id") or file_info.get("id") or "")
    if fid:
        from app.services.file_management_service import get_file_store_lock

        async with get_file_store_lock():
            # Issue#332：重复生成会覆盖 preview_output_path —— 上一份预览 PDF 必须跟着删，
            # 否则每点一次「开始匿名化」就在 OUTPUT_DIR 里多留一份（无清扫器，只增不减）。
            previous = file_store.get(fid)
            stale_preview = (
                previous.get("preview_output_path") if isinstance(previous, dict) else None
            )
            file_store.update_fields(fid, payload)
        if isinstance(stale_preview, str) and stale_preview.strip():
            # Issue#332：按 realpath 比较——OUTPUT_DIR 可能是符号链接/历史拼写，
            # 字符串比较相等即跳过删除（绝不误删刚写好的新产物）。
            if os.path.realpath(stale_preview) != os.path.realpath(preview_path):
                _remove_stale_preview(stale_preview, fid)
    return {
        "file_id": file_info.get("file_id") or file_info.get("id"),
        "processed_pages": normalized,
        "page_map": page_map,
        "page_count": len(normalized),
        "redacted_count": payload["preview_redacted_count"],
        "preview_output_path": preview_path,
    }


def _config_copy_keep(config: Any) -> Any:
    import copy

    config_copy = copy.copy(config)
    try:
        config_copy.watermark_delivery = "keep"
    except AttributeError:
        pass
    return config_copy
