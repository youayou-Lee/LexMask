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
    """卷级门判定「本卷不接管」。

    Issue#165：除了人读消息，还要把**机读原因**带给上层（负缓存 marker 与
    `pipeline_status["text_layer_doc"]`），否则失败卷上「为什么回落到旧路径」完全不可见。
    `permanent=True` 表示本快照下不会好转（无文本层/超预算/质量判定不可用），
    对应 `retry_at=inf`；其余保持 120s 负缓存 TTL 语义不变。
    """

    def __init__(self, message: str, *, code: str = "ineligible",
                 detail: dict | None = None, permanent: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.detail = dict(detail or {})
        self.permanent = bool(permanent)

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
        raise _TransientIneligible("no_text_layer", code="no_text_layer", permanent=True)

    # **建流预算闸**（Issue#129 评审 I1）：整卷建流在**首个请求里同步跑完**，页数过多
    # 会让该请求超过网关 `proxy_read_timeout`（`frontend/nginx.conf.template`，
    # 已与前端 `VISION_TIMEOUT=900s` 对齐）而 **504** —— 且这条路径**不经过任何回落**
    # （后端自己不 5xx，但网关会把连接掐掉，用户看到网关错误，重试才撞上 memo）。
    # 预估超预算 ⇒ 本卷直接不接管，逐页走旧视觉线（每页有界，不会撑爆网关）。
    budget = float(getattr(settings, "TEXT_LAYER_DOCLINE_MAX_BUILD_SEC", 0.0) or 0.0)
    if budget > 0:
        rate = float(getattr(settings, "TEXT_LAYER_DOCLINE_BUILD_SEC_PER_PAGE", 1.6) or 1.6)
        estimated = len(pages) * rate
        if estimated > budget:
            logger.warning(
                "text_layer_docline skipped: estimated build %.0fs > budget %.0fs "
                "(%d pages) file=%s",
                estimated,
                budget,
                len(pages),
                file_id[:8],
            )
            raise _TransientIneligible(
                f"build_budget:{estimated:.0f}s/{budget:.0f}s",
                code="build_budget",
                detail={"estimated_sec": round(estimated, 1), "budget_sec": budget},
                permanent=True,
            )

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
        raise _TransientIneligible(
            f"verdict_unusable:{verdict.severity}",
            code="verdict_unusable",
            detail={"severity": str(verdict.severity)},
            permanent=True,
        )

    # **跨页高频重复片段闸**（Issue#203）：文本层被防泄漏水印的打散碎片词支配的卷
    # （同一水印词组逐页重复上千次、真实正文极薄）不得接管文档级链——整卷 NER 的
    # 建链耗时是 O(整卷)，却受 run_page 的单页页超时看管，任一页到点即整卷作废
    # （2026-10-28 第四卷两次真实复现）。闸必须在 run_document_ner **之前**：
    # 卷级质量门（映射失败率/截断率/覆盖率）在建链完成后才跑——门在后、杀在前。
    # 错拒代价=回落逐页线（慢、召回不降）；漏拒代价=整卷作废。阈值偏「宁可错拒」。
    repeat = docline.measure_repeated_fragment_coverage(pages)
    max_repeat_coverage = float(settings.TEXT_LAYER_MAX_REPEATED_FRAGMENT_COVERAGE)
    if max_repeat_coverage > 0 and float(repeat["coverage"]) > max_repeat_coverage:
        logger.warning(
            "text_layer_docline skipped: repeated-fragment coverage %.3f > %.2f "
            "(fragment_kinds=%d, max_fragment_pages=%d) file=%s",
            float(repeat["coverage"]),
            max_repeat_coverage,
            int(repeat["fragment_kinds"]),
            int(repeat["max_fragment_pages"]),
            file_id[:8],
        )
        # PII 最小化（#203 AC4）：水印串含真实姓名+编号，reason/detail 只带计数
        # 与占比，不带命中片段字面量。permanent：层内容是文件快照的函数，重试
        # 不会变好——落永久负缓存，重试不重建、不再撞墙（回落逐页线照常工作）。
        raise _TransientIneligible(
            f"repeated_fragment_coverage:{float(repeat['coverage']):.3f}",
            code="repeated_fragment_coverage",
            detail={
                "coverage": round(float(repeat["coverage"]), 4),
                "max": max_repeat_coverage,
                "fragment_kinds": int(repeat["fragment_kinds"]),
                "max_fragment_pages": int(repeat["max_fragment_pages"]),
            },
            permanent=True,
        )

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
        raise _TransientIneligible(
            f"failed_chunks:{result.failed_chunks}/{result.chunks}",
            code="failed_chunks",
            detail={"failed_chunks": int(result.failed_chunks), "chunks": int(result.chunks)},
        )
    map_ratio = float(result.map_failure_ratio)
    if map_ratio > float(settings.TEXT_LAYER_MAX_MAP_FAILURE_RATIO):
        logger.warning(
            "text_layer_docline skipped: map failure ratio %.3f file=%s", map_ratio, file_id[:8]
        )
        raise _TransientIneligible(
            f"map_failure_ratio:{map_ratio:.3f}",
            code="map_failure_ratio",
            detail={"ratio": round(map_ratio, 4)},
        )
    max_errors = int(settings.TEXT_LAYER_MAX_BLOCK_ERRORS)
    if result.http_errors > max_errors:
        logger.warning(
            "text_layer_docline skipped: %d/%d chunks failed (limit %d) file=%s",
            result.http_errors,
            result.chunks,
            max_errors,
            file_id[:8],
        )
        raise _TransientIneligible(
            f"block_errors:{result.http_errors}/{result.chunks}",
            code="block_errors",
            detail={"http_errors": int(result.http_errors), "chunks": int(result.chunks)},
        )
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
            f"truncated_ratio:{truncated_ratio:.3f}:{result.truncated_chunks}/{result.chunks}",
            code="truncated_chunks",
            detail={"truncated": int(result.truncated_chunks), "chunks": int(result.chunks),
                    "ratio": round(truncated_ratio, 4)},
        )
    coverage = float(result.coverage_ratio)
    if coverage < float(settings.TEXT_LAYER_MIN_COVERAGE):
        logger.warning(
            "text_layer_docline skipped: coverage %.3f < %.2f file=%s",
            coverage, float(settings.TEXT_LAYER_MIN_COVERAGE), file_id[:8],
        )
        raise _TransientIneligible(
            f"coverage:{coverage:.3f}",
            code="coverage",
            detail={"coverage": round(coverage, 4),
                    "min": round(float(settings.TEXT_LAYER_MIN_COVERAGE), 4)},
        )
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
    """读 memo：命中且未过期返回 memo；负缓存仍有效返回其 marker（带原因）；否则 None。

    Issue#165：以前负缓存命中返回 `_NEGATIVE` 哨兵，调用方只能 `return None, None`
    ⇒ 失败卷的原因（块失败数等）在上层彻底不可见。
    """
    async with _LOCK:
        memo = _MEMO.get(key)
        if memo is None:
            return None
        if memo.get("unusable"):
            retry_at = float(memo.get("retry_at") or 0.0)
            if retry_at == float("inf") or time.monotonic() < retry_at:
                return memo
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
    """返回该页的文档级框与元信息。

    Issue#165：**回落失败时第二个返回值不再恒为 `None`**——本卷不接管（资格不符 /
    显式不跑 HaS / 无标签 / 非扫描件）仍返回 `(None, None)`；而「本该接管但判定失败」
    （无文本层、超预算、质量或覆盖率闸、块失败、任何异常）返回
    `(None, {"unusable": True, "reason_code": ..., "reason_detail": ...})`，
    供上层写进 `pipeline_status["text_layer_doc"]`（否则失败卷上无任何机读回落信号）。
    """
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
        f"{settings.TEXT_LAYER_DOCLINE_MAX_BUILD_SEC}:"
        f"{settings.TEXT_LAYER_DOCLINE_BUILD_SEC_PER_PAGE}:"
        f"{';'.join(f'{tid}={pat}' for tid, pat in regex_types)}"
    )
    key = (
        f"{file_id}|{_file_fingerprint(str(snapshot.get('file_path') or ''))}"
        f"|{','.join(labels)}|{knobs}"
    )
    try:
        async with _key_lock(key):
            memo = await _lookup_memo(key)
            if memo is not None and memo.get("unusable"):
                if memo.get("reason_code") == "not_applicable":
                    return None, None
                # Issue#165：负缓存也要把原因交回上层，否则失败卷的
                # `pipeline_status["text_layer_doc"]` 整块缺失。
                return None, memo
            if memo is None:
                try:
                    built = await _build(file_id, snapshot, labels, ocr_has_types, regex_types)
                except _TransientIneligible as exc:
                    built = None
                    marker = {
                        "unusable": True,
                        "retry_at": (
                            float("inf") if exc.permanent
                            else time.monotonic() + _NEGATIVE_TTL_SEC
                        ),
                        "reason": str(exc),
                        "reason_code": exc.code,
                        "reason_detail": exc.detail,
                    }
                except Exception as exc:  # noqa: BLE001 - 本链任何故障都退回旧路径
                    logger.warning("text_layer_docline build failed file=%s: %s", file_id[:8], exc)
                    built = None
                    marker = {"unusable": True, "retry_at": time.monotonic() + _NEGATIVE_TTL_SEC,
                              "reason": f"error:{type(exc).__name__}",
                              "reason_code": "error", "reason_detail": {}}
                else:
                    # `_build` 返回 None = **本卷不适用**（无路径 / 非扫描件），不是回落失败：
                    # 不能报成 fallback，否则每个文本型 PDF 的每一页都会多出一块假失败。
                    marker = {"unusable": True, "retry_at": float("inf"),
                              "reason_code": "not_applicable"}
                memo = built if built is not None else marker
                async with _LOCK:
                    _MEMO[key] = memo
                    if len(_MEMO) > 16:
                        _MEMO.pop(next(iter(_MEMO)))
                if memo.get("unusable"):
                    if memo.get("reason_code") == "not_applicable":
                        return None, None
                    return None, memo
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
        # Issue#165：用 `%s` 而非 `%d`——兜底分支正是为「入口参数不可信」而存在，
        # 日志格式串本身不能再因为 page 不是整数而抛错（否则兜底分支反而把请求打成 5xx）。
        logger.warning("text_layer_docline failed file=%s page=%s: %s", file_id[:8], page, exc)
        # Issue#165：**意外故障**同样要带机读原因。此前这里也返回 `(None, None)`，
        # 于是「本链出意外」与「本链不适用」在上层长得一模一样（都是无信号回落）。
        return None, {
            "unusable": True,
            "retry_at": time.monotonic() + _NEGATIVE_TTL_SEC,
            "reason": f"unexpected:{type(exc).__name__}",
            "reason_code": "unexpected",
            "reason_detail": {},
        }
