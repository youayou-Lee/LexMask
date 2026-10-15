"""Issue#115 C4：文档级文本层链的编排层接入（开关式、默认关）。

`detect_vision` 是**逐页**入口，而文档级链必须**整卷跑一次**再按页取结果，所以这里
放一个 per-file 的进程内 memo：第一次进入时判定资格 → 抽文本层 → 分类器判可用性 →
跑整卷 HaS 分块 → 值回映 `(页号, 框)`；之后同一文件的每页直接取用。

设计约束（验收方案 v2 §2）：

- **默认关**（`TEXT_LAYER_DOCLINE_ENABLED=False`），关时本模块一次也不被调用路径触发；
- 资格不满足（非扫描件 / 文本层不可用 / 类型表为空）⇒ 返回 `None`，调用方**原样走**旧路径；
- **任何异常都不得让请求失败**：一律 catch → 记 warning → 返回 `None`（退回旧路径 + 清单条目）；
- memo 以「文件路径 + 归一化类型表」为键，文件被替换（路径相同、指纹不同）时按 mtime+size 失效。
"""
from __future__ import annotations

import asyncio
import logging
import os
import time

from app.core.config import settings
from app.models.entity_schemas import BoundingBox
from app.services import text_layer_docline_service as docline
from app.services import text_layer_quality as quality
from app.services.has_client import has_client
from app.services.vision.has_text_payload import (
    _build_has_text_type_names,
    build_label_to_type_id,
)

logger = logging.getLogger(__name__)

_MEMO: dict[str, dict] = {}
_LOCK = asyncio.Lock()


class _TransientIneligible(Exception):
    """临时不可用（如块失败超阈）。负缓存带 TTL，过期后允许重试。"""

SOURCE_DETAIL = "text_layer_docline"


def reset_memo() -> None:
    """仅测试与文件级缓存失效使用。"""
    _MEMO.clear()


def _labels(ocr_has_types) -> list[str]:
    try:
        names = _build_has_text_type_names(ocr_has_types)
    except Exception as exc:  # noqa: BLE001 - 类型表推导失败 ⇒ 不启用本链
        logger.warning("text_layer_docline labels failed: %s", exc)
        return []
    return [str(name) for name in names if str(name).strip()]


_NEGATIVE_TTL_SEC = 120.0


def _tenant_has_regex_types(owner_id) -> bool:
    """租户配了带 regex_pattern 的类型时**不启用**文档级链（Issue#115 评审 I-3）。

    regex 保证层（Issue#43）只实现在 OCR+HaS 链内（`_apply_regex_fallback`），文档级链
    没有等价步骤。若照常启用，配置了正则类型的租户在覆盖页上会**丢掉这些类型**。
    宁可整卷回落慢路径，也不接受静默漏检。
    """
    if owner_id is None:
        return False
    try:
        from app.services import entity_type_service as ets

        return bool(ets.get_regex_types(owner_id))
    except Exception as exc:  # noqa: BLE001 - 查不到就当作有，保守回落
        logger.warning("text_layer_docline regex-type probe failed: %s", exc)
        return True


def _file_fingerprint(path: str) -> str:
    try:
        st = os.stat(path)
        return f"{int(st.st_size)}:{int(st.st_mtime)}"
    except OSError:
        return "unknown"


def _to_boxes(
    entities: list[docline.DocLineEntity],
    page: int,
    label_to_id: dict[str, str] | None = None,
) -> list[BoundingBox]:
    """把文档级实体转成本页框。**类型必须反向映射成 id**（模型返回的是中文标签桶）。

    不映射就直接拿标签当 `type` 会产出 `姓名`/`身份证号` 这类框，下游按 id 过滤时
    全部落空（云上首轮实测：召回 0/121）。查不到标签就保留原样（与既有链路回落一致）。
    """
    boxes: list[BoundingBox] = []
    mapping = label_to_id or {}
    for entity in entities:
        if entity.page != page:
            continue
        x0, y0, x1, y1 = entity.bbox
        width = max(0.0, x1 - x0)
        height = max(0.0, y1 - y0)
        if width <= 0 or height <= 0:
            continue
        type_id = mapping.get(entity.type, entity.type)
        boxes.append(
            BoundingBox(
                id=f"tld-{page}-{type_id}-{len(boxes)}",
                x=x0,
                y=y0,
                width=width,
                height=height,
                page=page,
                type=type_id,
                text=entity.value,
                selected=True,
                confidence=None,
                source="ocr_has",
                source_detail=SOURCE_DETAIL,
                evidence_source="ocr_has",
            )
        )
    return boxes


async def _build(file_id: str, snapshot: dict, labels: list[str], ocr_has_types) -> dict | None:
    path = str(snapshot.get("file_path") or "")
    if not path or not os.path.exists(path):
        return None
    file_type = str(snapshot.get("file_type") or "")
    scanned = bool(snapshot.get("is_scanned")) or file_type in {"pdf_scanned", "image"}
    if not scanned:
        return None

    started = time.perf_counter()
    pages = await asyncio.to_thread(docline.extract_text_layer_pages, path)
    if not pages:
        logger.info("text_layer_docline skipped: no text layer file=%s", file_id[:8])
        return None

    ink = await asyncio.to_thread(docline.sample_ink_alignment, path, pages)
    metrics = quality.compute_metrics(
        [[word.text for word in page.words] for page in pages],
        ink_alignment=ink,
    )
    verdict = quality.classify(metrics)
    if not verdict.usable:
        logger.info(
            "text_layer_docline skipped: layer %s file=%s reasons=%s",
            verdict.severity,
            file_id[:8],
            "; ".join(verdict.reasons),
        )
        return None

    result = await docline.run_document_ner(pages, labels, has_client=has_client)
    max_errors = int(settings.TEXT_LAYER_MAX_BLOCK_ERRORS)
    if result.http_errors > max_errors:
        logger.warning(
            "text_layer_docline skipped: %d/%d chunks failed (limit %d) file=%s",
            result.http_errors,
            result.chunks,
            max_errors,
            file_id[:8],
        )
        raise _TransientIneligible(f"block_errors:{result.http_errors}/{result.chunks}")
    label_to_id = build_label_to_type_id(ocr_has_types)
    # **只有真正带词的页才算被覆盖**：`extract_text_layer_pages` 会把无文本层的页
    # 也返回（`words=[]`）。若按「页存在」算覆盖，混合卷里那些无层页会被判成
    # 「文档级链已处理、0 个实体」⇒ 静默丢检（云上混合卷实测：10 页里 4 页无层，
    # 却报 coverage=1.000 / OCR 调用 0）。
    page_min_chars = int(settings.TEXT_LAYER_PAGE_MIN_CHARS)
    page_chars = {int(page_meta.no): sum(len(w.text) for w in page_meta.words) for page_meta in pages}
    covered_pages = {no for no, chars in page_chars.items() if chars >= page_min_chars}
    by_page: dict[int, list[BoundingBox]] = {}
    for page_meta in pages:
        by_page[page_meta.no] = _to_boxes(result.entities, page_meta.no, label_to_id)
    memo = {
        "labels": labels,
        "label_to_id": label_to_id,
        "covered_pages": covered_pages,
        "boxes_by_page": by_page,
        "pages": result.pages,
        "entities": len(result.entities),
        "chunks": result.chunks,
        "coverage": round(result.coverage_ratio, 4),
        "ner_calls": result.ner_calls,
        "residual_calls": result.residual_calls,
        "map_failures": len(result.map_failures),
        "http_errors": result.http_errors,
        "truncated_chunks": result.truncated_chunks,
        "uncovered_pages": sorted(set(page_chars) - covered_pages),
        "verdict": verdict.severity,
        "ink_alignment": metrics.ink_alignment,
        "seconds": round(time.perf_counter() - started, 3),
    }
    logger.info(
        "text_layer_docline built file=%s pages=%d covered=%d ent=%d chunks=%d coverage=%.3f "
        "calls=%d residual=%d http_errors=%d truncated=%d elapsed=%.2fs",
        file_id[:8],
        result.pages,
        len(covered_pages),
        len(result.entities),
        result.chunks,
        result.coverage_ratio,
        result.ner_calls,
        result.residual_calls,
        result.http_errors,
        result.truncated_chunks,
        memo["seconds"],
    )
    return memo


async def boxes_for_page(
    *,
    file_id: str,
    snapshot: dict,
    page: int,
    ocr_has_types,
    owner_id=None,
) -> tuple[list[BoundingBox] | None, dict | None]:
    """返回该页的文档级框与元信息；资格不符或出错时返回 `(None, None)`。"""
    if not settings.TEXT_LAYER_DOCLINE_ENABLED:
        return None, None
    if _tenant_has_regex_types(owner_id):
        logger.info("text_layer_docline skipped: tenant regex types file=%s", file_id[:8])
        return None, None
    labels = _labels(ocr_has_types)
    if not labels:
        return None, None
    knobs = (
        f"{settings.TEXT_LAYER_DOCLINE_CHUNK_CHARS}:{settings.TEXT_LAYER_DOCLINE_CHUNK_OVERLAP}:"
        f"{settings.TEXT_LAYER_DOCLINE_CONCURRENCY}:{settings.TEXT_LAYER_PAGE_MIN_CHARS}"
    )
    key = (
        f"{file_id}|{_file_fingerprint(str(snapshot.get('file_path') or ''))}"
        f"|{','.join(labels)}|{knobs}"
    )
    try:
        async with _LOCK:
            memo = _MEMO.get(key)
            if memo is not None and memo.get("unusable"):
                # 负缓存（评审 I-6）：确定性不合格的卷（无层/层不可用/租户有 regex）
                # 永久缓存；临时故障（块失败超阈）只缓存 120s，过期允许重试。
                retry_at = float(memo.get("retry_at") or 0.0)
                if retry_at == float("inf") or time.monotonic() < retry_at:
                    return None, None
                memo = None
            if memo is None:
                try:
                    built = await _build(file_id, snapshot, labels, ocr_has_types)
                except _TransientIneligible as exc:
                    built = None
                    marker = {"unusable": True, "retry_at": time.monotonic() + _NEGATIVE_TTL_SEC,
                              "reason": str(exc)}
                except Exception as exc:  # noqa: BLE001 - 本链任何故障都退回旧路径
                    logger.warning("text_layer_docline build failed file=%s: %s", file_id[:8], exc)
                    built = None
                    marker = {"unusable": True, "retry_at": time.monotonic() + _NEGATIVE_TTL_SEC,
                              "reason": f"error:{type(exc).__name__}"}
                else:
                    marker = {"unusable": True, "retry_at": float("inf")}
                memo = built if built is not None else marker
                _MEMO[key] = memo
                if len(_MEMO) > 16:
                    _MEMO.pop(next(iter(_MEMO)))
                if memo.get("unusable"):
                    return None, None
        if int(page) not in memo["covered_pages"]:
            # **混合卷**：本页没有内嵌文本层。此处绝不能返回空列表——那等于
            # 「文档级链说这页 0 个实体」，会让该页**静默丢检**。返回 None 让调用方
            # 原样回落到 OCR+HaS 旧路径，保证逐页覆盖率不降（验收 D8）。
            logger.info("text_layer_docline page %d 无文本层，回落旧路径 file=%s", page, file_id[:8])
            return None, None
        boxes = list(memo["boxes_by_page"].get(int(page)) or [])
        if not boxes and settings.TEXT_LAYER_EMPTY_PAGE_FALLBACK:
            # **0 框 ≠ 这页没内容**（评审 C-1）：覆盖页一个实体都没出时，绝不能把
            # `pipeline_ocr_types` 清空当作「文档级链已处理」。宁可多跑一趟 OCR。
            logger.info(
                "text_layer_docline page %d 0 框，回落旧路径 file=%s", page, file_id[:8]
            )
            return None, memo
        return boxes, memo
    except Exception as exc:  # noqa: BLE001 - 本链任何故障都退回旧路径，绝不 5xx
        logger.warning("text_layer_docline failed file=%s page=%d: %s", file_id[:8], page, exc)
        return None, None