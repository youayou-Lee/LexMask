"""Issue#115 C4：文档级文本层链的编排层接入（开关式，Issue#129 起默认开）。

`detect_vision` 是**逐页**入口，而文档级链必须**整卷跑一次**再按页取结果，所以这里
放一个 per-file 的进程内 memo：第一次进入时判定资格 → 抽文本层 → 分类器判可用性 →
跑整卷 HaS 分块 → 值回映 `(页号, 框)`；之后同一文件的每页直接取用。

设计约束（验收方案 v2 §2）：

- **默认开**（`TEXT_LAYER_DOCLINE_ENABLED=True`，Issue#129 拍板）；置 `False` 时本模块
  一次也不被调用路径触发，行为与 Issue#115 之前完全一致；
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


def _regex_types(owner_id) -> list[tuple[str, str]]:
    """租户启用的 regex 类型 (id, pattern) 清单（Issue#115 评审 I-3）。

    既有 OCR+HaS 链有 `_apply_regex_fallback` 保证层；文档级链必须在同一份流上
    补跑同一批正则，否则配置了正则类型的租户会在覆盖页上漏检这些类型。
    """
    if owner_id is None:
        return []
    try:
        from app.services import entity_type_service as ets

        return [
            (str(item.id), str(item.regex_pattern))
            for item in ets.get_regex_types(owner_id)
            if getattr(item, "id", None) and getattr(item, "regex_pattern", None)
        ]
    except Exception as exc:  # noqa: BLE001 - 探不到就当作没有（与旧链同口径）
        logger.warning("text_layer_docline regex-type probe failed: %s", exc)
        return []


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


async def _build(
    file_id: str,
    snapshot: dict,
    labels: list[str],
    ocr_has_types,
    regex_types: list[tuple[str, str]] | None = None,
) -> dict | None:
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

    result = await docline.run_document_ner(
        pages, labels, has_client=has_client, regex_types=regex_types
    )
    # **卷级合格门**（门⑥ 二轮 Important#5）：失败块/回映失败率/覆盖率任一越界即整卷
    # 放弃走文档级链、回落旧路径。这些门以前不存在，或只在**任何值**下都成立。
    if result.failed_chunks > 0:
        logger.warning(
            "text_layer_docline skipped: %d/%d chunks failed file=%s",
            result.failed_chunks, result.chunks, file_id[:8],
        )
        raise _TransientIneligible(f"failed_chunks:{result.failed_chunks}/{result.chunks}")
    map_ratio = float(result.map_failure_ratio)
    if map_ratio > float(settings.TEXT_LAYER_MAX_MAP_FAILURE_RATIO):
        logger.warning(
            "text_layer_docline skipped: map failure ratio %.3f file=%s", map_ratio, file_id[:8]
        )
        raise _TransientIneligible(f"map_failure_ratio:{map_ratio:.3f}")
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
    truncated_ratio = (
        float(result.truncated_chunks) / float(result.chunks) if result.chunks else 0.0
    )
    max_truncated = float(settings.TEXT_LAYER_MAX_TRUNCATED_RATIO)
    if truncated_ratio > max_truncated:
        # 门⑥ 第三轮 Important#2：模型撞完成帽 ⇒ 该块实体不全（真卷已知失败模态）。
        # 截断本来只进 memo 与上报，既不过门又写**正缓存**（进程内永久固化），
        # 召回损失不可见也不可自愈。residual 二遍能找回一部分，但不是保证。
        logger.warning(
            "text_layer_docline skipped: %d/%d chunks truncated (limit %.2f) file=%s",
            result.truncated_chunks, result.chunks, max_truncated, file_id[:8],
        )
        raise _TransientIneligible(
            f"truncated_ratio:{truncated_ratio:.3f}:{result.truncated_chunks}/{result.chunks}"
        )
    coverage = float(result.coverage_ratio)
    if coverage < float(settings.TEXT_LAYER_MIN_COVERAGE):
        logger.warning(
            "text_layer_docline skipped: coverage %.3f < %.2f file=%s",
            coverage, float(settings.TEXT_LAYER_MIN_COVERAGE), file_id[:8],
        )
        raise _TransientIneligible(f"coverage:{coverage:.3f}")
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


_NEGATIVE: dict = {"_negative": True}
_KEY_LOCKS: dict[str, asyncio.Lock] = {}


def _key_lock(key: str) -> asyncio.Lock:
    """按 key 的构建锁（门⑥ 二轮 Important#4）。

    旧写法把整卷 HaS（可达数分钟）放在**全局** `_LOCK` 里 ⇒ A 文件第 1 页在建流时，
    其它文件的所有页都在排队。改为按 key 加锁：跨文件可并发构建，同文件只建一次。
    """
    lock = _KEY_LOCKS.get(key)
    if lock is None:
        if len(_KEY_LOCKS) > 64:  # 简单的容量兜底
            _KEY_LOCKS.clear()
        lock = asyncio.Lock()
        _KEY_LOCKS[key] = lock
    return lock


async def _lookup_memo(key: str) -> dict | None:
    """读 memo：命中且未过期返回 memo；负缓存仍有效返回 `_NEGATIVE`；否则 None（需重建）。"""
    async with _LOCK:
        memo = _MEMO.get(key)
        if memo is None:
            return None
        if memo.get("unusable"):
            retry_at = float(memo.get("retry_at") or 0.0)
            if retry_at == float("inf") or time.monotonic() < retry_at:
                return _NEGATIVE
            return None
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
    # **「不跑 HaS」必须整条链跳过**（门⑥ 二轮 Critical#1）：`effective_ocr_has_types`
    # 为空时旧链是全跳过，而 `has_text_payload` 的 `if vision_types else 默认` 会让空
    # 列表 falsy 而回落到 **38 类默认表** —— 于是用户明确要的「只做视觉特征、不跑 HaS」
    # 反而变成整卷 38 类 HaS，且账号停用的类型会在这条链上复活。
    # 只拦**空列表**：orchestrator 传的永远是已收口的列表（`filter_types_by_account_enabled`
    # 之后），空列表 = 用户明确「不跑 HaS」；`None` 保留原语义（不限制 ⇒ 用默认表）。
    if ocr_has_types is not None and len(ocr_has_types) == 0:
        return None, None
    labels = _labels(ocr_has_types)
    if not labels:
        return None, None
    regex_types = _regex_types(owner_id)
    knobs = (
        f"{settings.TEXT_LAYER_DOCLINE_CHUNK_CHARS}:{settings.TEXT_LAYER_DOCLINE_CHUNK_OVERLAP}:"
        f"{settings.TEXT_LAYER_DOCLINE_CONCURRENCY}:{settings.TEXT_LAYER_PAGE_MIN_CHARS}:"
        f"{';'.join(f'{tid}={pat}' for tid, pat in regex_types)}"
    )
    key = (
        f"{file_id}|{_file_fingerprint(str(snapshot.get('file_path') or ''))}"
        f"|{','.join(labels)}|{knobs}"
    )
    try:
        async with _key_lock(key):
            memo = await _lookup_memo(key)
            if memo is _NEGATIVE:
                return None, None
            if memo is None:
                try:
                    built = await _build(file_id, snapshot, labels, ocr_has_types, regex_types)
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
                async with _LOCK:
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
        if not boxes:
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
