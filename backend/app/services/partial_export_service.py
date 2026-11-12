"""Issue#257 一期 · 部分导出（页级选页 → 只含选中页的 PDF + 说明页）。

为什么要独立服务而不是给 ``execute_redaction`` 加参数：``execute_redaction`` 会把
产物、实体映射、框、版本表**写回 file_store**（主产物语义）。部分导出是"快照副本"，
绝不能改动主产物——AC1.3 的「主产物逐字节不变」判据就是钉这一点。这里直接调
``Redactor().redact``（纯读 file_info、只产出一个新文件），天然隔离。

链路（每一步都有对应的验收判据）：

1. **快照**：页面选择、框、失败清单、快照时间在锁内一次取齐（导出期间识别继续跑，
   包内容不受影响——AC1.3、D1 的 `snapshot_at`);
2. **先解析交付源**：``VisionService()._delivery_source`` 在**原件**上解析去水印净化
   副本——必须在切页之前做（否则切片文件被二次解析、或拿到带水印的源）；
3. **切页**：``insert_pdf`` 按**原卷升序**重建子卷（D6）；
4. **框重映射**：归一化坐标不变，只把 ``page`` 改成切页后的页号（``_redact_pdf`` 按
   ``b.selected and (b.page or 1) == page_no`` 取框，不重映射就会打错页/漏打）；
5. **打码**：config 副本强制 ``watermark_delivery="keep"``（源已净化，避免二次净化）；
6. **说明页**：``china-s`` 中文说明页插在第 1 页（容器实证逐字符可读）；
7. **打包**：复用 ``write_export_volumes``（分卷、manifest、下载端点、TTL 全部复用）。
"""

from __future__ import annotations

import asyncio
import copy
import io
import json
import os
import re
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from typing import Any

import fitz

from app.models.common import FileType, ReplacementMode
from app.models.entity_schemas import BoundingBox
from app.models.schemas import RedactionConfig
from app.services import page_status_service
from app.services.export_service import ExportTask, export_task_manager, write_export_volumes

#: 页级部分导出只对"有页且能栅格化"的类型成立（文本型 PDF 走替换链路会丢文本层，
#: 一期明确不支持——方案 R1；docx/txt/md 无页概念）。
SUPPORTED_EXPORT_FILE_TYPES: frozenset[str] = frozenset({"pdf", "pdf_scanned"})

COVER_FONT = "china-s"
COVER_FONTSIZE = 11.0
_COVER_MARGIN_X = 56.0
_COVER_START_Y = 90.0
_COVER_LINE_HEIGHT = 18.0
_COVER_PAGES_PER_LINE = 20
# A4 宽 595pt、左右各留 56pt ⇒ 483pt；取 470 作保守折行上限（横向页更宽，安全）。
_COVER_TEXT_WIDTH = 470.0


class PartialExportError(Exception):
    """带机读码的选页/导出错误（端点层转成 400/409 的 ``{detail:{code,...}}``）。"""

    def __init__(self, code: str, message: str = "", **detail: Any) -> None:
        self.code = code
        self.message = message or code
        self.detail: dict[str, Any] = {"code": code, "message": self.message, **detail}
        super().__init__(self.message)


def _file_type_value(file_type: Any) -> str:
    return str(getattr(file_type, "value", file_type) or "").strip().lower()


def is_supported_export_type(file_type: Any) -> bool:
    return _file_type_value(file_type) in SUPPORTED_EXPORT_FILE_TYPES


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def config_from_job_config(raw: Any) -> RedactionConfig:
    """job.config_json → ``RedactionConfig``（与 ``task_queue_pipelines._run_redaction`` 同口径）。

    一期不做"导出配置与打码配置分叉"——部分导出沿用该 job 的打码配置，否则导出的
    产物会和正式交付件不是同一套规则（那才是真风险）。
    """
    cfg = raw if isinstance(raw, dict) else {}
    rm = cfg.get("replacement_mode") or "structured"
    try:
        replacement_mode = ReplacementMode(str(rm))
    except ValueError:
        replacement_mode = ReplacementMode.STRUCTURED
    return RedactionConfig(
        replacement_mode=replacement_mode,
        entity_types=list(cfg.get("entity_type_ids") or []),
        custom_replacements=dict(cfg.get("custom_replacements") or {}),
        image_redaction_method=cfg.get("image_redaction_method"),
        image_redaction_strength=int(cfg.get("image_redaction_strength") or 75),
        image_fill_color=str(cfg.get("image_fill_color") or "#000000"),
        watermark_text=(str(cfg.get("watermark_text") or "").strip() or None),
        watermark_delivery=cfg.get("watermark_delivery"),
    )


# ---------------------------------------------------------------------------
# 选页校验（AC1.4 / AC1.5）
# ---------------------------------------------------------------------------

def validate_partial_export_pages(
    pages: Iterable[Any],
    *,
    page_count: int,
    failed_pages: set[int],
    recognized_pages: set[int],
    acknowledge_failed_excluded: bool,
    file_type: Any = None,
    boxes_pages: Iterable[int] | None = None,
    redactable_pages: Iterable[int] | None = None,
) -> dict[str, Any]:
    """校验并规范化选页。返回 ``{"pages": [...], "excluded_failed_pages": [...]}``。

    错误码语义：

    * ``partial_export_unsupported_type``——类型不支持页级导出（docx/txt/md/文本 PDF）；
    * ``pages_empty`` / ``pages_out_of_range``——空选、越界（含 0/负数/超页数）；
    * ``failed_pages_not_allowed``——**任何**参数路径都不得把失败页放进产物（#199 红线）；
    * ``pages_not_recognized``——选了还没识别完的页（不能导出空白页冒充成品）；
    * （已废止）``failed_pages_ack_required``——2026-11-11 需求重对齐后**不再索要**任何知情确认：
      区间内的失败页由后端直接自动排除，并把排除清单如实回报（写进说明页/ manifest）。
      参数 ``acknowledge_failed_excluded`` 仅为旧客户端兼容而保留，已不影响判定；
    * ``pages_without_boxes``——选中页在 file_store 里**没有框账条目**（该页没跑过视觉识别）。
      这种页即使被记成 completed，打码阶段也无框可打，导出会得到一份「原样渲染、零遮蔽」的副本，
      属于 fail-open，必须拒收（评审 Important-2）。
    """
    if file_type is not None and not is_supported_export_type(file_type):
        raise PartialExportError(
            "partial_export_unsupported_type",
            "该文件类型不支持页级部分导出（仅支持扫描件/带框 PDF）",
        )

    normalized: list[int] = []
    seen: set[int] = set()
    for raw in pages or []:
        page = _safe_int(raw, 0)
        if page in seen:
            continue
        seen.add(page)
        normalized.append(page)
    if not normalized:
        raise PartialExportError("pages_empty", "未选择任何页")
    normalized.sort()

    page_count = max(_safe_int(page_count, 0), 0)
    out_of_range = [p for p in normalized if p < 1 or p > page_count]
    if out_of_range:
        raise PartialExportError(
            "pages_out_of_range",
            "所选页号超出原卷页范围",
            pages=out_of_range,
            page_count=page_count,
        )

    failed_in_selection = [p for p in normalized if p in failed_pages]
    if failed_in_selection:
        raise PartialExportError(
            "failed_pages_not_allowed",
            "识别失败的页不得进入交付包（失败页未被扫过敏感数据）",
            failed_pages=failed_in_selection,
        )

    not_recognized = [p for p in normalized if p not in recognized_pages]
    if not_recognized:
        raise PartialExportError(
            "pages_not_recognized",
            "所选页尚未识别完成",
            pages=not_recognized,
        )

    if boxes_pages is not None:
        with_boxes = {int(p) for p in boxes_pages}
        missing = [p for p in normalized if p not in with_boxes]
        if missing:
            raise PartialExportError(
                "pages_without_boxes",
                "所选页没有视觉识别框记录，无法保证遮蔽效果，拒绝导出",
                pages=missing,
            )
    if redactable_pages is not None:
        # 「有框账条目」不等于「框能落到这一页」：账里某条框的 ``page`` 与账页号不一致时，
        # 重映射会把它静默丢掉，产物就把这一页原样栅格化进包（文本层消失后连"零残留"
        # 检查都抓不到）。空框账（干净页）不算问题——只有「框落不到本页」才 fail-closed。
        blocked = [
            p for p in normalized if int(p) not in {int(x) for x in redactable_pages}
        ]
        if blocked:
            raise PartialExportError(
                "pages_with_unmappable_boxes",
                "所选页的框账有无法落到该页的框，无法保证遮蔽效果，拒绝导出",
                pages=blocked,
            )

    # 失败页永不进包（#199 红线），因此区间内的失败页直接自动排除——不需要用户再点确认。
    # 排除清单会写进说明页与 manifest，界面上的「将排除 N 页」就是唯一提示。
    lo, hi = normalized[0], normalized[-1]
    excluded = sorted(p for p in failed_pages if lo <= p <= hi)

    return {"pages": normalized, "excluded_failed_pages": excluded}


# ---------------------------------------------------------------------------
# 说明页（AC1.2 / AC5：只允许页号、时间、原因码、导出 ID）
# ---------------------------------------------------------------------------

def _page_label_chunks(pages: Sequence[int], *, max_width: float | None = None) -> list[str]:
    """把页号切成若干行。给了 ``max_width`` 就按真实字宽折行（三位数页号一行 20 个会溢出）。"""
    if max_width is None:
        labels: list[str] = []
        for start in range(0, len(pages), _COVER_PAGES_PER_LINE):
            chunk = pages[start : start + _COVER_PAGES_PER_LINE]
            labels.append("、".join(str(p) for p in chunk))
        return labels

    labels: list[str] = []
    current: list[str] = []
    for page in pages:
        candidate = "、".join(current + [str(page)])
        if current and _text_width(candidate) > max_width:
            labels.append("、".join(current))
            current = [str(page)]
        else:
            current.append(str(page))
    if current:
        labels.append("、".join(current))
    return labels


def _wrap_to_width(line: str, max_width: float) -> list[str]:
    if _text_width(line) <= max_width:
        return [line]
    out: list[str] = []
    current = ""
    for ch in line:
        if current and _text_width(current + ch) > max_width:
            out.append(current)
            current = ch
        else:
            current += ch
    if current:
        out.append(current)
    return out


def _text_width(text: str, fontsize: float = COVER_FONTSIZE) -> float:
    try:
        return float(fitz.get_text_length(text, fontname=COVER_FONT, fontsize=fontsize))
    except Exception:  # pragma: no cover - 极端字体缺失时按经验值兜底
        return len(text) * fontsize * 0.6


_REASON_MAX_CHARS = 120
_REASON_UNSAFE_CHARS = re.compile(r"[\r\n\t]+")


def _safe_reason(reason: Any) -> str:
    """失败原因码只允许单行短文本（防止把案卷文本片段带进交付件）。"""
    text = _REASON_UNSAFE_CHARS.sub(" ", str(reason or "")).strip()
    return text[:_REASON_MAX_CHARS]


def verify_incomplete_pages_of(file_info: Any, pages: Iterable[Any]) -> list[int]:
    """读取 included 页里 ``page_routing`` 记了 ``verify_incomplete`` 的页号（升序去重）。

    Issue#273 拍板 b（交付+披露）：对账未实际发生的页按文本层结果交付，但必须
    在说明页里如实点名。账本形状（T7/T8 finalize 契约）：键为 ``str(page)``、
    ``verify_incomplete`` 为 bool（决策即落账的初记条目没有该键 = False）。

    **静默兜底**：说明页是交付物，不能因诊断信息崩——``page_routing`` 键缺失
    （file 模式/旧任务）、类型不对、条目非 dict、脏值一律按「无此类页」处理返回
    空表；只认严格 ``True``（账本契约是 bool，脏字符串真值不构成披露依据）。
    """
    try:
        routing = (file_info or {}).get("page_routing")
        if not isinstance(routing, dict):
            return []
        included = sorted({_safe_int(p, 0) for p in pages or []})
        hits: list[int] = []
        for page in included:
            if page <= 0:
                continue
            entry = routing.get(str(page))
            if isinstance(entry, dict) and entry.get("verify_incomplete") is True:
                hits.append(page)
        return hits
    except Exception:  # noqa: BLE001 - 披露信息读取失败绝不阻断导出
        return []


def build_cover_lines(
    *,
    pages: Sequence[int],
    page_count: int,
    excluded: Sequence[tuple[int, str]] | Iterable[tuple[int, str]] = (),
    snapshot_at: str,
    generated_at: str,
    export_id: str,
    recognized_pages_at_export: int,
    verify_incomplete_pages: Sequence[int] = (),
) -> list[str]:
    """说明页文本。**逐页列举**（不得写成「第 1-5 页」——那会在跳页时撒谎）。

    ``verify_incomplete_pages`` 非空 ⇒ 追加「未完成对账页」段（Issue#273 拍板 b）：
    这些页对账未实际发生、按文本层结果交付，须如实点名建议复核；空/缺省 ⇒ 不加段
    （零噪声，file 模式与旧任务不受影响）。
    """
    page_list = sorted({_safe_int(p, 0) for p in pages})
    labels = _page_label_chunks(page_list, max_width=_COVER_TEXT_WIDTH)
    lines: list[str] = ["【部分导出说明】"]
    if labels:
        lines.append(f"本包含原卷第 {labels[0]} 页")
        for extra in labels[1:]:
            lines.append(f"（续）第 {extra} 页")
    lines.append(f"共 {len(page_list)} 页（含说明页 1 页）")
    lines.append(f"原卷总页数：{max(_safe_int(page_count, 0), 0)}")
    excluded_list = [(int(p), _safe_reason(reason)) for p, reason in excluded]
    for page, reason in excluded_list[:_COVER_PAGES_PER_LINE]:
        reason_text = f"（失败原因码：{reason}）" if reason else ""
        lines.append(f"本次排除的失败页：第 {page} 页{reason_text}")
    if len(excluded_list) > _COVER_PAGES_PER_LINE:
        lines.append(f"（另有 {len(excluded_list) - _COVER_PAGES_PER_LINE} 页失败页已排除）")
    verify_list = sorted({_safe_int(p, 0) for p in verify_incomplete_pages})
    verify_list = [p for p in verify_list if p > 0]
    if verify_list:
        verify_labels = _page_label_chunks(verify_list, max_width=_COVER_TEXT_WIDTH)
        suffix = "（该页按文本层结果交付，建议复核）"
        lines.append(f"未完成对账页：{verify_labels[0]}{suffix}")
        for extra in verify_labels[1:]:
            lines.append(f"（续）未完成对账页：{extra}{suffix}")
    lines.append(f"结果快照时间：{snapshot_at}")
    lines.append(f"生成时间：{generated_at}")
    lines.append(f"识别成功页数：{max(_safe_int(recognized_pages_at_export, 0), 0)}")
    lines.append(f"导出 ID：{export_id}")
    lines.append("本包仅为所选页的最新识别结果快照；未识别页与失败页不在本包内，不可当作完整件使用。")
    return lines


# ---------------------------------------------------------------------------
# 切页 / 框重映射 / 插说明页
# ---------------------------------------------------------------------------

def slice_pdf_pages(source_path: str, pages: Sequence[int], out_path: str) -> None:
    """按**原卷升序**把选中页重建为一个子卷（D6：不看传入顺序）。"""
    ordered = sorted({_safe_int(p, 0) for p in pages})
    if not ordered:
        raise PartialExportError("pages_empty", "未选择任何页")
    with fitz.open(source_path) as src:
        total = src.page_count
        bad = [p for p in ordered if p < 1 or p > total]
        if bad:
            raise PartialExportError(
                "pages_out_of_range", "所选页号超出原卷页范围", pages=bad, page_count=total
            )
        out = fitz.open()
        try:
            for page_no in ordered:
                out.insert_pdf(src, from_page=page_no - 1, to_page=page_no - 1)
            out.save(out_path, garbage=4, deflate=True)
        finally:
            out.close()


def normalize_boxes(raw: Any, *, strict: bool = False) -> list[BoundingBox]:
    """把 file_store 里的框结构（``{page: [box, ...]}`` 或平铺 list）归一成对象列表。

    ``file_store.bounding_boxes`` 是**按页 dict**（#199 之后的形态），键在内核里是
    int、落 JSON 后变 str；两种都要吃。

    ``strict=True`` 时坏框**不再静默丢弃**：历史脏记录若被悄悄跳过，打码集合就会比
    框账少，产物里留原样页却仍被当作"已遮蔽"——所以导出链路必须 fail-closed
    （``partial_export_box_invalid``），只有纯展示/统计路径才容错。
    """
    items: list[Any]
    if isinstance(raw, dict):
        items = []
        for value in raw.values():
            if isinstance(value, (list, tuple)):
                items.extend(value)
    elif isinstance(raw, (list, tuple)):
        items = list(raw)
    else:
        return []
    boxes: list[BoundingBox] = []
    invalid = 0
    for item in items:
        if isinstance(item, BoundingBox):
            boxes.append(item)
            continue
        if not isinstance(item, dict):
            invalid += 1
            continue
        try:
            boxes.append(BoundingBox.model_validate(item))
        except Exception:  # noqa: BLE001 - 脏框计数，严格模式由调用方决定是否拒绝
            invalid += 1
    if strict and invalid:
        raise PartialExportError(
            "partial_export_box_invalid",
            "框记录里有无法解析的框，无法保证遮蔽效果，拒绝导出",
            invalid_boxes=invalid,
        )
    return boxes


def remap_boxes_to_subset(boxes: Iterable[BoundingBox], pages: Sequence[int]) -> list[BoundingBox]:
    """把选中框的 ``page`` 从原卷页号改到切页后的 1-based 序号。

    归一化坐标不变；未选中的框不进包（沿用 ``selected`` 语义）；返回**新对象**，
    不就地改写调用方手里的框（导出的快照语义）。
    """
    ordered = sorted({_safe_int(p, 0) for p in pages})
    index_by_page = {page: idx + 1 for idx, page in enumerate(ordered)}
    remapped: list[BoundingBox] = []
    for box in boxes or []:
        if not getattr(box, "selected", False):
            continue
        original_page = _safe_int(getattr(box, "page", 1), 1) or 1
        target = index_by_page.get(original_page)
        if target is None:
            continue
        remapped.append(box.model_copy(update={"page": target}))
    return remapped


def _remove_quietly(path: str) -> None:
    """删临时文件（不存在/被占用都忽略），避免未打码切片在导出目录残留。"""
    try:
        os.remove(path)
    except OSError:
        pass


def insert_cover_page(pdf_path: str, lines: Sequence[str]) -> None:
    """把说明页插到第 1 页（页尺寸随原第 1 页，折行与字号按页宽高自适应）。"""
    if not lines:
        return
    with fitz.open(pdf_path) as doc:
        if doc.page_count <= 0:
            return
        first = doc[0]
        width, height = first.rect.width, first.rect.height
        text_width = max(width - 2 * _COVER_MARGIN_X, 120.0)
        wrapped: list[str] = []
        for line in lines:
            wrapped.extend(_wrap_to_width(line, text_width))
        usable = max(height - _COVER_START_Y - _COVER_MARGIN_X, 60.0)
        fontsize = COVER_FONTSIZE
        line_height = _COVER_LINE_HEIGHT
        while len(wrapped) * line_height > usable and fontsize > 7.0:
            fontsize -= 0.5
            line_height = fontsize * 1.6
        cover = doc.new_page(pno=0, width=width, height=height)
        y = _COVER_START_Y
        for line in wrapped:
            if y > height - _COVER_MARGIN_X:  # 兜底：宁可截断也不写到页外
                break
            cover.insert_text(
                (_COVER_MARGIN_X, y),
                line,
                fontname=COVER_FONT,
                fontsize=fontsize,
            )
            y += line_height
        buf = io.BytesIO()
        doc.save(buf, garbage=4, deflate=True)
        buf.seek(0)
    with open(pdf_path, "wb") as f:
        f.write(buf.getvalue())


# ---------------------------------------------------------------------------
# 主链路
# ---------------------------------------------------------------------------

async def _resolve_delivery_source(source_path: str, delivery: str | None) -> str:
    """交付源必须在**切页前**在原件上解析（去水印净化副本）。"""
    from app.services.vision_service import VisionService

    service = VisionService()
    resolved = await service._delivery_source(source_path, delivery=delivery)  # noqa: SLF001
    return resolved or source_path


async def build_partial_package(
    *,
    source_path: str,
    pages: Sequence[int],
    boxes: Iterable[BoundingBox],
    config: Any,
    cover_lines: Sequence[str],
    out_dir: str,
    final_name: str,
    progress_cb: Any = None,
) -> dict[str, Any]:
    """生成「说明页 + 选中页打码产物」的单个 PDF。返回 ``{"pdf_path","bytes","page_count"}``。"""
    from app.services.redactor import Redactor

    os.makedirs(out_dir, exist_ok=True)
    subset_path = os.path.join(out_dir, "subset.pdf")
    delivery = getattr(config, "watermark_delivery", None)
    source = await _resolve_delivery_source(source_path, delivery)
    remapped = remap_boxes_to_subset(boxes, pages)

    # 源已净化 ⇒ 让 Redactor 不要再解析交付源（避免对切片文件二次净化/失败）。
    config_copy = copy.copy(config)
    try:
        config_copy.watermark_delivery = "keep"
    except AttributeError:  # pragma: no cover - 兼容 strict dataclass
        pass

    file_info = {"file_path": subset_path, "file_type": FileType.PDF_SCANNED, "is_scanned": True}
    redacted_path = ""
    final_path = os.path.join(out_dir, final_name)
    try:
        def _sync_slice() -> None:
            slice_pdf_pages(source, pages, subset_path)

        await asyncio.to_thread(_sync_slice)
        result = await Redactor().redact(
            file_info, [], remapped, config_copy, progress_cb=progress_cb
        )
        redacted_path = str(result["output_path"])

        def _sync_cover() -> None:
            insert_cover_page(redacted_path, cover_lines)

        await asyncio.to_thread(_sync_cover)
        await asyncio.to_thread(os.replace, redacted_path, final_path)

        expected = len(set(pages)) + 1
        with fitz.open(final_path) as doc:
            actual = doc.page_count
        if actual != expected:
            raise PartialExportError(
                "partial_export_page_count_mismatch",
                f"产物页数 {actual} != 预期 {expected}",
                expected=expected,
                actual=actual,
            )
    except BaseException:
        # 中途失败绝不能把「未打码的选中页副本」留在导出目录里（原样含文本层）。
        _remove_quietly(subset_path)
        if redacted_path:
            _remove_quietly(redacted_path)
        _remove_quietly(final_path)
        raise
    finally:
        _remove_quietly(subset_path)

    return {
        "pdf_path": final_path,
        "bytes": os.path.getsize(final_path),
        "page_count": len(set(pages)) + 1,
    }


# ---------------------------------------------------------------------------
# 导出任务（ExportTask 扩展字段 + 扫盘列记录）
# ---------------------------------------------------------------------------

_UNSAFE_NAME_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


def _safe_name(raw: str, fallback: str = "partial") -> str:
    """保留中文与括号（zip 内文件名要可读），只替换路径危险字符。"""
    stem = _UNSAFE_NAME_CHARS.sub("_", str(raw or "")).strip("._- ")
    return (stem or fallback)[:80]


_TITLE_PAGE_LIST_LIMIT = 6


def build_export_title(*, filename: str | None, pages: Sequence[int]) -> str:
    """标题里的页号必须与产物一致：跳页时逐页列举（「第1-5页」会撒谎）。"""
    stem = os.path.splitext(str(filename or "案卷"))[0] or "案卷"
    ordered = sorted({_safe_int(p, 0) for p in pages})
    if not ordered:
        return f"{stem}-部分导出"
    contiguous = len(ordered) == ordered[-1] - ordered[0] + 1
    if contiguous:
        label = str(ordered[0]) if len(ordered) == 1 else f"{ordered[0]}-{ordered[-1]}"
        return f"{stem}-部分导出(第{label}页)"
    head = "、".join(str(p) for p in ordered[:_TITLE_PAGE_LIST_LIMIT])
    if len(ordered) > _TITLE_PAGE_LIST_LIMIT:
        # 「…等 9 页」已经自带量词：再套「(第…页)」会变成「等 9 页页」。
        return f"{stem}-部分导出(第{head} 等 {len(ordered)} 页)"
    return f"{stem}-部分导出(第{head}页)"


async def submit_partial_export(
    *,
    owner_id: str,
    item_row: dict[str, Any],
    file_info: dict[str, Any],
    config: Any,
    pages: Sequence[int],
    excluded_failed_pages: Sequence[int],
    snapshot_at: str,
    manager: Any = None,
) -> ExportTask:
    """提交一个部分导出任务（走 export_task_manager 的独立 IO worker，不阻塞识别）。"""
    manager = manager or export_task_manager
    page_count = page_status_service.page_count_of(item_row, file_info)
    recognized = page_status_service.recognized_page_numbers(item_row, page_count)
    # 原因码来自 failed_pages 清单（权威），丢进说明页。
    reasons = page_status_service.failed_page_reasons(item_row)
    excluded_with_reason = [
        (page, reasons.get(page, ""))
        for page in sorted({_safe_int(x, 0) for x in excluded_failed_pages})
    ]

    title = build_export_title(filename=file_info.get("original_filename"), pages=pages)
    boxes_snapshot = normalize_boxes(file_info.get("bounding_boxes"))
    source_path = str(file_info.get("file_path") or "")
    pages_snapshot = sorted({_safe_int(p, 0) for p in pages})
    # Issue#273 拍板 b：included 页里的对账未完成页在快照期一次取齐（helper 内部
    # 静默兜底，file 模式/旧任务/账本异常都返回空表 ⇒ 说明页零噪声）。
    verify_incomplete_snapshot = verify_incomplete_pages_of(file_info, pages_snapshot)
    snapshot = snapshot_at

    async def _runner(task: ExportTask, progress: Any) -> list[dict[str, Any]]:
        generated_at = datetime.now(UTC).isoformat()
        cover_lines = build_cover_lines(
            pages=pages_snapshot,
            page_count=page_count,
            excluded=excluded_with_reason,
            snapshot_at=snapshot,
            generated_at=generated_at,
            export_id=task.export_id,
            recognized_pages_at_export=len(recognized),
            verify_incomplete_pages=verify_incomplete_snapshot,
        )
        work_dir = os.path.join(task.out_dir, "work")
        arcname = f"{_safe_name(title)}.pdf"

        def _progress(current: int, total: int) -> None:
            progress(current=current, total=total, stage="rendering")

        product = await build_partial_package(
            source_path=source_path,
            pages=pages_snapshot,
            boxes=boxes_snapshot,
            config=config,
            cover_lines=cover_lines,
            out_dir=work_dir,
            final_name=arcname,
            progress_cb=_progress,
        )
        progress(current=len(pages_snapshot), total=len(pages_snapshot), stage="packaging")
        manifest = {
            "kind": "partial_redaction",
            "job_id": item_row.get("job_id"),
            "item_id": item_row.get("id"),
            "file_id": item_row.get("file_id"),
            "pages": pages_snapshot,
            "page_count": page_count,
            "excluded_failed_pages": [p for p, _ in excluded_with_reason],
            "snapshot_at": snapshot,
            "recognized_pages_at_export": len(recognized),
            "cover": list(cover_lines),
        }
        entries = [(product["pdf_path"], arcname, product["bytes"])]
        volumes = await asyncio.to_thread(
            write_export_volumes,
            entries,
            task.out_dir,
            manifest,
            progress,
        )
        # work/ 只留干净目录（体积清理，避免 TTL 前占盘）。
        for name in ("subset.pdf",):
            try:
                os.remove(os.path.join(work_dir, name))
            except OSError:
                pass
        return volumes

    task = manager.submit(
        owner_id=owner_id,
        kind="partial_redaction",
        runner=_runner,
        title=title,
        total_bytes=0,
        file_count=1,
        meta={
            "job_id": item_row.get("job_id"),
            "item_id": item_row.get("id"),
            "file_id": item_row.get("file_id"),
            "pages": pages_snapshot,
            "page_count": page_count,
            "excluded_failed_pages": [p for p, _ in excluded_with_reason],
            "snapshot_at": snapshot,
            "recognized_pages_at_export": len(recognized),
        },
    )
    return task


def list_item_exports(
    *,
    owner_id: str,
    job_id: str | None,
    item_id: str | None,
    manager: Any = None,
) -> dict[str, list[Any]]:
    """列出某 item 的导出记录（**扫盘**为主，内存任务覆盖状态）。

    导出任务表只在内存里（重启即丢），但每次 ``submit`` 与状态变更都会把
    ``export-status.json`` 落盘 ⇒ 记录必须从磁盘重建；内存里的同 id 任务状态更新
    （running/completed 更及时）覆盖盘上快照。
    """
    manager = manager or export_task_manager
    from app.services.export_service import _safe_owner_dir  # noqa: PLC0415,SLF001

    root = _safe_owner_dir(owner_id)
    records: dict[str, dict[str, Any]] = {}
    unavailable: list[str] = []
    if os.path.isdir(root):
        for export_id in os.listdir(root):
            # 目录名直接拼进路径，先按 export_id 格式校验（防 `..`/异常名）。
            if not re.fullmatch(r"[0-9a-f]{8,32}", export_id):
                continue
            status_path = os.path.join(root, export_id, "export-status.json")
            if not os.path.isfile(status_path):
                continue
            try:
                with open(status_path, encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict):
                continue
            if job_id is not None and str(data.get("job_id") or "") != str(job_id):
                continue
            if item_id is not None and str(data.get("item_id") or "") != str(item_id):
                continue
            # 归属以状态文件里的私有键为准（目录名有损，可能撞车）。
            stored_owner = data.get("_owner_id")
            if stored_owner is not None and str(stored_owner) != str(owner_id):
                continue
            records[str(data.get("export_id") or export_id)] = data

    for export_id, task in list(getattr(manager, "_tasks", {}).items()):  # noqa: SLF001
        if getattr(task, "owner_id", None) != owner_id:
            continue
        public = task.public()
        if job_id is not None and str(public.get("job_id") or "") != str(job_id):
            continue
        if item_id is not None and str(public.get("item_id") or "") != str(item_id):
            continue
        records[export_id] = public

    ordered = sorted(records.values(), key=lambda item: str(item.get("created_at") or ""), reverse=True)
    for record in ordered:
        export_id = str(record.get("export_id") or "")
        out_dir = os.path.join(root, export_id)
        if record.get("status") == "completed" and not os.path.isdir(out_dir):
            unavailable.append(export_id)
    return {"exports": ordered, "unavailable": unavailable}
