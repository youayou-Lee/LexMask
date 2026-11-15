"""Issue#278 统一导出链（原 Issue#257 部分导出服务，就地改造不改文件名）。

统一「完整导出」与「选页导出」为**一条 item 级导出链**（``POST /jobs/{j}/items/{i}/export``）：
选页只是导出的参数（``kind="partial"`` 必带 pages；``kind="full"`` 缺省 = 全部可导出页）。
两者进同一个 submit 函数、同一个 ExportTask、同一种 zip 包形态、同一份 manifest 结构，
仅 ``kind`` 字段与渲染策略不同。产物 = 一个包（``write_export_volumes`` 既有落盘）：
``<导出标题>.zip`` = ``part001.zip``（内含脱敏产物 + ``manifest.json`` [+ 可选 ``说明.txt``]）。

**说明页废除**（用户口径 2026-11-12）：正文 PDF 一律不插任何页；说明信息退为可选的
``说明.txt``（``include_summary=true`` 才生成，默认关）。``insert_cover_page`` 已删除。

**full 产物策略（r2 F1）**：主产物直取两档——
* 无失败页卷（exportable == 全卷）：逐字节直取（``shutil.copyfile``，O(1)），
  包内 PDF 与主产物 sha256 相等；
* 有失败页卷（exportable ⊂ 全卷）：主产物的**结构级切页副本**（``slice_pdf_pages``，
  fitz ``insert_pdf``，不走 Redactor、不栅格化、文本层保留）——**失败页物理不进包**，
  manifest ``pages`` 的「包内页清单」语义为真。
* 非分页件（agent md/docx/image 等）：无页可切，退化为逐字节直取。

为什么要独立服务而不是给 ``execute_redaction`` 加参数：``execute_redaction`` 会把
产物、实体映射、框、版本表**写回 file_store**（主产物语义）。导出是"快照副本"，
绝不能改动主产物——AC1.3 的「主产物逐字节不变」判据就是钉这一点。partial 直接调
``Redactor().redact``（纯读 file_info、只产出一个新文件），天然隔离。

**框账权威（改框即生效，§2.6）**：``submit_item_export`` 取框的权威次序 =
**review 草稿 > file_store**（与交付期 ``seed_file_store_from_draft_payload`` 的
权威次序逐字一致）。草稿可能滞后的既有语义（整卷重跑改了 file_store、草稿仍是
旧框）**如实继承**——导出与交付保持同一真源次序，不引入第三种权威。快照只读、
不回写 file_store。

链路（partial，每一步都有对应的验收判据）：

1. **快照**：页面选择、框、失败清单、快照时间在提交协程内一次取齐（导出期间识别
   继续跑，包内容不受影响——AC1.3、D1 的 ``snapshot_at``）；
2. **先解析交付源**：``VisionService()._delivery_source`` 在**原件**上解析去水印净化
   副本——必须在切页之前做（否则切片文件被二次解析、或拿到带水印的源）；
3. **切页**：``insert_pdf`` 按**原卷升序**重建子卷（D6）；
4. **框重映射**：归一化坐标不变，只把 ``page`` 改成切页后的页号；
5. **打码**：config 副本强制 ``watermark_delivery="keep"``（源已净化，避免二次净化）；
6. **打包**：复用 ``write_export_volumes``（分卷、manifest、下载端点、TTL 全部复用）。
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import hashlib
import json
import os
import re
import shutil
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from typing import Any

import fitz

from app.core.config import settings
from app.core.file_validation import safe_path_in_dir
from app.models.common import FileType, ReplacementMode
from app.models.entity_schemas import BoundingBox
from app.models.schemas import RedactionConfig
from app.services import page_event_log, page_status_service
from app.services.export_service import ExportTask, export_task_manager, write_export_volumes

#: 页级部分导出只对"有页且能栅格化"的类型成立（文本型 PDF 走替换链路会丢文本层，
#: 一期明确不支持——方案 R1；docx/txt/md 无页概念）。**仅约束 partial**（切页重渲染）
#: 的准入；full 走主产物直取、不重渲染，类型闸的前提不成立 ⇒ full 跳过（r2 F6）。
SUPPORTED_EXPORT_FILE_TYPES: frozenset[str] = frozenset({"pdf", "pdf_scanned"})

COVER_FONT = "china-s"
COVER_FONTSIZE = 11.0
_COVER_PAGES_PER_LINE = 20
# A4 宽 595pt、左右各留 56pt ⇒ 483pt；取 470 作保守折行上限（横向页更宽，安全）。
_COVER_TEXT_WIDTH = 470.0

#: 说明.txt 在包内的固定名（机读契约，前端/i18n 不参与）。
SUMMARY_TXT_NAME = "说明.txt"

EXPORT_KINDS: frozenset[str] = frozenset({"full", "partial"})


class PartialExportError(Exception):
    """带机读码的选页/导出错误（端点层转成 400/409 的 ``{detail:{code,...}}``）。"""

    def __init__(self, code: str, message: str = "", **detail: Any) -> None:
        self.code = code
        self.message = message or code
        self.detail: dict[str, Any] = {"code": code, "message": self.message, **detail}
        super().__init__(self.message)


#: Issue#278 统一导出别名（机读错误信封不变；旧 import 面仍可用）。
ItemExportError = PartialExportError


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

    一期不做"导出配置与打码配置分叉"——导出沿用该 job 的打码配置，否则导出的
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
# 选页校验（AC1.4 / AC1.5；#278 增 kind 维度）
# ---------------------------------------------------------------------------

def validate_export_pages(
    pages: Iterable[Any] | None,
    *,
    kind: str,
    page_count: int,
    failed_pages: set[int],
    recognized_pages: set[int],
    file_type: Any = None,
    boxes_pages: Iterable[int] | None = None,
    redactable_pages: Iterable[int] | None = None,
) -> dict[str, Any]:
    """校验并规范化选页。返回 ``{"pages": [...], "excluded_failed_pages": [...]}``。

    ``kind="partial"``：``pages`` 必填（None ⇒ ``pages_required_for_partial``），
    走既有校验管线（含类型闸——切页重渲染会丢文本层，docx/txt/md/文本 PDF 不准入）；
    ``kind="full"``：**跳过类型闸**（主产物直取不重渲染，类型闸的前提不成立；且
    agent md/docx 的完整导出现在就存在），``pages`` 参数被忽略，缺省集 =
    ``recognized_pages``（调用方喂 ``exportable_page_numbers``，即自动成功页 ∪
    人工补框页）∩ 页范围。

    错误码语义：

    * ``partial_export_unsupported_type``——partial 类型不支持页级导出；
    * ``pages_required_for_partial``——partial 缺 pages（#278 新增，400）；
    * ``export_no_pages``——full 且缺省集为空（分页卷无任何可导出页，409）；
    * ``pages_empty`` / ``pages_out_of_range``——空选、越界（含 0/负数/超页数）；
    * ``failed_pages_not_allowed``——**任何**参数路径都不得把失败页放进产物（#199 红线）；
    * ``pages_not_recognized``——选了还没识别完的页（不能导出空白页冒充成品）；
    * ``pages_without_boxes``——选中页在框账里**没有条目**（该页没跑过视觉识别）。
      这种页即使被记成 completed，打码阶段也无框可打，导出会得到一份「原样渲染、
      零遮蔽」的副本，属于 fail-open，必须拒收（评审 Important-2）。
    """
    if kind not in EXPORT_KINDS:
        raise PartialExportError("export_kind_invalid", "kind 必须是 full 或 partial")

    if kind == "full":
        # full：pages 参数被忽略（缺省语义 = 全部可导出页），跳过类型闸。
        total = max(_safe_int(page_count, 0), 0)
        default_pages = sorted(p for p in recognized_pages if 1 <= p <= total)
        if total > 0 and not default_pages:
            raise PartialExportError(
                "export_no_pages",
                "本卷没有可导出的页（全部失败或未识别），请改用选页导出拿快照",
                page_count=total,
            )
        excluded = sorted(p for p in failed_pages if 1 <= p <= total)
        return {"pages": default_pages, "excluded_failed_pages": excluded}

    # partial：pages 必填。
    if pages is None:
        raise PartialExportError(
            "pages_required_for_partial", "选页导出必须指定 pages", kind="partial"
        )

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
    # 排除清单会写进说明.txt 与 manifest，界面上的「将排除 N 页」就是唯一提示。
    lo, hi = normalized[0], normalized[-1]
    excluded = sorted(p for p in failed_pages if lo <= p <= hi)

    return {"pages": normalized, "excluded_failed_pages": excluded}


# ---------------------------------------------------------------------------
# 框账可导页集合（jobs.py 迁入：覆盖视图的口径唯一出处，#280 F-I3 同族教训）
# ---------------------------------------------------------------------------

def boxes_pages_of(file_info: dict[str, Any] | None) -> set[int]:
    """框账里有条目的页号集合（校验「选了没跑过视觉识别的页」）。

    file_store 形态是按页 dict（键 int/JSON str 都吃）；review 草稿覆盖视图是
    平铺 list（``seed_file_store_from_draft_payload`` 同款形态），按框自带 page 归组。
    """
    raw = (file_info or {}).get("bounding_boxes") or {}
    pages: set[int] = set()
    if isinstance(raw, dict):
        for key in raw:
            try:
                pages.add(int(key))
            except (TypeError, ValueError):
                continue
    elif isinstance(raw, (list, tuple)):
        for box in raw:
            if isinstance(box, dict):
                try:
                    pages.add(int(box.get("page") or 1))
                except (TypeError, ValueError):
                    continue
    return pages


def redactable_pages_of(file_info: dict[str, Any] | None) -> set[int]:
    """框账里「已选框都能落到本页」的页集合（干净页=空框账，也算安全）。

    账页号与框自带的 ``page`` 不一致时，重映射会静默丢框、产物就把该页原样
    栅格化进包（文本层消失后连"零残留"检查都抓不到）⇒ 这类页 fail-closed。
    """
    raw = (file_info or {}).get("bounding_boxes") or {}
    safe: set[int] = set()
    if isinstance(raw, dict):
        for key, boxes in raw.items():
            try:
                page_no = int(key)
            except (TypeError, ValueError):
                continue
            parsed = normalize_boxes(boxes, strict=True)
            if all(int(box.page or 1) == page_no for box in parsed if box.selected):
                safe.add(page_no)
    elif isinstance(raw, (list, tuple)):
        # 平铺 list（review 草稿形态）：每条框自带 page，映射以框自带页号为准。
        for box in raw:
            if not isinstance(box, dict):
                continue
            parsed = normalize_boxes([box], strict=True)
            for parsed_box in parsed:
                if parsed_box.selected:
                    safe.add(int(parsed_box.page or 1))
    return safe


# ---------------------------------------------------------------------------
# 说明.txt（AC1.2 / AC5：只允许页号、时间、原因码、导出 ID；#278 删插页）
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

    Issue#273 拍板 b（交付+披露）：对账未发生的页按文本层结果交付，但必须
    在说明.txt 里如实点名。账本形状（T7/T8 finalize 契约）：键为 ``str(page)``、
    ``verify_incomplete`` 为 bool（决策即落账的初记条目没有该键 = False）。

    **静默兜底**：说明.txt 是交付物，不能因诊断信息崩——``page_routing`` 键缺失
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


def build_summary_lines(
    *,
    pages: Sequence[int],
    page_count: int,
    excluded: Sequence[tuple[int, str]] | Iterable[tuple[int, str]] = (),
    snapshot_at: str,
    generated_at: str,
    export_id: str,
    recognized_pages_at_export: int,
    verify_incomplete_pages: Sequence[int] = (),
    # 人工补框页（#280 §2.5）：调用方从 manual 账本投影取页号（partial=included ∩ 账本，
    # full=全部账本条目），本函数只负责逐页披露。
    manual_pages: Sequence[int] = (),
) -> list[str]:
    """说明.txt 文本。**逐页列举**（不得写成「第 1-5 页」——那会在跳页时撒谎）。

    #278：删除旧说明页的「共 N 页（含说明页 1 页）」+1 语义（改「共 N 页」）；
    新增人工补框段（#280 §2.5 强制）：逐页「第 X 页为人工补框结果，未经自动识别校验」。
    ``verify_incomplete_pages`` 非空 ⇒ 追加「未完成对账页」段（Issue#273 拍板 b）：
    这些页对账未实际发生、按文本层结果交付，须如实点名建议复核；空/缺省 ⇒ 不加段
    （零噪声，file 模式与旧任务不受影响）。

    ``manual_verified_pages`` 非空 ⇒ 逐页追加人工补框披露行（Issue#280，#199 红线的
    显式标注义务）：「第 X 页为人工补框结果，未经自动识别校验」。缺标注即 §6 变异
    3 判红。
    """
    page_list = sorted({_safe_int(p, 0) for p in pages})
    labels = _page_label_chunks(page_list, max_width=_COVER_TEXT_WIDTH)
    lines: list[str] = ["【导出说明】"]
    if labels:
        lines.append(f"本包含原卷第 {labels[0]} 页")
        for extra in labels[1:]:
            lines.append(f"（续）第 {extra} 页")
    lines.append(f"共 {len(page_list)} 页")
    lines.append(f"原卷总页数：{max(_safe_int(page_count, 0), 0)}")
    excluded_list = [(int(p), _safe_reason(reason)) for p, reason in excluded]
    for page, reason in excluded_list[:_COVER_PAGES_PER_LINE]:
        reason_text = f"（失败原因码：{reason}）" if reason else ""
        lines.append(f"本次排除的失败页：第 {page} 页{reason_text}")
    if len(excluded_list) > _COVER_PAGES_PER_LINE:
        lines.append(f"（另有 {len(excluded_list) - _COVER_PAGES_PER_LINE} 页失败页已排除）")
    # Issue#280：人工补框页逐页点名（included 页里的人工页；快照语义与 manifest 同拍）。
    manual_list = sorted({_safe_int(p, 0) for p in manual_pages})
    manual_list = [p for p in manual_list if p > 0]
    for page in manual_list[:_COVER_PAGES_PER_LINE]:
        lines.append(f"第 {page} 页为人工补框结果，未经自动识别校验")
    if len(manual_list) > _COVER_PAGES_PER_LINE:
        lines.append(f"（另有 {len(manual_list) - _COVER_PAGES_PER_LINE} 页人工补框页）")
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


def write_summary_txt(lines: Sequence[str], out_path: str) -> None:
    """说明.txt 落盘：UTF-8 无 BOM、逐行 join（替代被废除的 PDF 插页，#278）。"""
    text = "\n".join(str(line) for line in lines)
    with open(out_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
        if text:
            f.write("\n")


# ---------------------------------------------------------------------------
# 切页 / 框重映射（full 结构级切页与 partial 重渲染共用）
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
    """把框结构（``{page: [box, ...]}`` 或平铺 list）归一成对象列表。

    ``file_store.bounding_boxes`` 是**按页 dict**（#199 之后的形态），键在内核里是
    int、落 JSON 后变 str；两种都要吃。review 草稿覆盖视图（#278 §2.6）是平铺
    list，同样支持。

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


# ---------------------------------------------------------------------------
# 主链路：build_item_package（full=主产物直取/结构级切页；partial=切页重渲染）
# ---------------------------------------------------------------------------

async def _resolve_delivery_source(source_path: str, delivery: str | None) -> str:
    """交付源必须在**切页前**在原件上解析（去水印净化副本）。"""
    from app.services.vision_service import VisionService

    service = VisionService()
    resolved = await service._delivery_source(source_path, delivery=delivery)  # noqa: SLF001
    return resolved or source_path


async def build_item_package(
    *,
    kind: str,
    source_path: str,
    pages: Sequence[int],
    boxes: Iterable[BoundingBox],
    config: Any,
    out_dir: str,
    final_name: str,
    include_summary: bool = False,
    summary_lines: Sequence[str] = (),
    output_path: str | None = None,
    paged: bool = False,
    source_page_count: int = 0,
    progress_cb: Any = None,
) -> dict[str, Any]:
    """生成单件导出产物。返回 ``{"pdf_path","bytes","page_count","summary_path"}``。

    * ``kind="full"``：主产物直取——校验 ``output_path`` 存在且在 OUTPUT_DIR 内；
      paged 卷且 ``exportable ⊂ 全卷``（有失败页）→ 按 ``pages`` 结构级切页
      （``slice_pdf_pages``，不走 Redactor、不栅格化，文本层保留，失败页物理不进包）；
      ``exportable == 全卷`` → 逐字节 copyfile（包内 sha256 == 主产物）；非分页件 →
      逐字节 copyfile。
    * ``kind="partial"``：切页重渲染（唯一能产子卷的方式）。
    * ``include_summary=True`` ⇒ 产物旁落 ``说明.txt``（``summary_path`` 带回路径）。
    """
    if kind not in EXPORT_KINDS:
        raise PartialExportError("export_kind_invalid", "kind 必须是 full 或 partial")

    os.makedirs(out_dir, exist_ok=True)
    final_path = os.path.join(out_dir, final_name)
    summary_path: str | None = None
    subset_path = os.path.join(out_dir, "subset.pdf")

    if kind == "full":
        try:
            _build_full_product(
                output_path=str(output_path or ""),
                pages=pages,
                paged=paged,
                source_page_count=source_page_count,
                final_path=final_path,
                subset_path=subset_path,
            )
        finally:
            _remove_quietly(subset_path)
    else:
        subset_redacted_path = ""
        try:
            from app.services.redactor import Redactor

            delivery = getattr(config, "watermark_delivery", None)
            source = await _resolve_delivery_source(source_path, delivery)
            remapped = remap_boxes_to_subset(boxes, pages)

            # 源已净化 ⇒ 让 Redactor 不要再解析交付源（避免对切片文件二次净化/失败）。
            config_copy = copy.copy(config)
            with contextlib.suppress(AttributeError):
                config_copy.watermark_delivery = "keep"

            file_info = {"file_path": subset_path, "file_type": FileType.PDF_SCANNED, "is_scanned": True}

            def _sync_slice() -> None:
                slice_pdf_pages(source, pages, subset_path)

            await asyncio.to_thread(_sync_slice)
            result = await Redactor().redact(
                file_info, [], remapped, config_copy, progress_cb=progress_cb
            )
            subset_redacted_path = str(result["output_path"])
            await asyncio.to_thread(os.replace, subset_redacted_path, final_path)

            expected = len(set(pages))
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
            if subset_redacted_path:
                _remove_quietly(subset_redacted_path)
            _remove_quietly(final_path)
            raise
        _remove_quietly(subset_path)

    if include_summary:
        summary_path = os.path.join(out_dir, SUMMARY_TXT_NAME)
        write_summary_txt(summary_lines, summary_path)

    return {
        "pdf_path": final_path,
        "bytes": os.path.getsize(final_path),
        "page_count": len(set(pages)) if (kind == "partial" or paged) else 0,
        "summary_path": summary_path,
    }


def _build_full_product(
    *,
    output_path: str,
    pages: Sequence[int],
    paged: bool,
    source_page_count: int,
    final_path: str,
    subset_path: str,
) -> None:
    """full 主产物直取两档（r2 F1）。失败页物理不进包（#199 红线在产物层成立）。"""
    if not output_path or not safe_path_in_dir(output_path, settings.OUTPUT_DIR):
        raise PartialExportError(
            "export_not_ready",
            "主产物不存在或路径不可用，请先完成识别打码（或改用选页导出拿快照）",
        )
    if not os.path.isfile(output_path):
        raise PartialExportError(
            "export_not_ready",
            "主产物文件缺失，请先完成识别打码（或改用选页导出拿快照）",
        )
    ordered = sorted({_safe_int(p, 0) for p in pages})
    needs_slice = bool(
        paged
        and max(_safe_int(source_page_count, 0), 0) > 0
        and len(ordered) < max(_safe_int(source_page_count, 0), 0)
    )
    try:
        if needs_slice:
            # 有失败页卷：结构级切页副本（fitz insert_pdf，O(N) 结构复制，
            # 不走 Redactor、不栅格化，文本层天然保留）。
            slice_pdf_pages(output_path, ordered, final_path)
        else:
            # 无失败页卷 / 非分页件：逐字节直取（O(1)，sha256 == 主产物）。
            shutil.copyfile(output_path, final_path)
    except BaseException:
        _remove_quietly(final_path)
        raise


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


def _sha256_of_file(path: str) -> str:
    """流式计算 entry sha256（万级文件也不攒内存）。"""
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _log_event_safe(event: str, **fields: Any) -> None:
    """导出事件旁路（fail-open 纪律）：日志任何故障不得中断导出（#279/#278 §2.5）。"""
    try:
        page_event_log.log_event(event, **fields)
    except Exception:  # noqa: BLE001 - 旁路绝不阻断交付动作
        pass


async def submit_item_export(
    *,
    owner_id: str,
    store: Any,
    job_row: dict[str, Any],
    item_row: dict[str, Any],
    file_info: dict[str, Any],
    config: Any,
    kind: str,
    pages: Sequence[int] | None = None,
    include_summary: bool = False,
    manager: Any = None,
) -> ExportTask:
    """提交一个统一导出任务（full | partial；走 export_task_manager 的独立 IO worker）。

    提交时序（快照语义，全部在提交协程内一次取齐——导出期间识别继续跑，包内容
    不受影响）：

    1. 页集合：``exportable_page_numbers``（自动成功页 ∪ manual 页，#280 投影）；
       partial 走 ``validate_export_pages``（失败页自动排除、区间内排除清单照旧）；
       full 跳类型闸、忽略 pages，缺省集 = exportable；排除清单 = 全部失败页带原因码。
    2. manual 页快照：``manual_ledger_of``（#280 账本，防御性形状兜底）；partial 只列
       included ∩ ledger，full 列全部条目。
    3. **框账快照（改框即生效的核心，§2.6）**：review 草稿存在 ``bounding_boxes``
       键且非 None 时**整份覆盖** file_info 的框（与交付期
       ``seed_file_store_from_draft_payload`` 权威次序一致）；草稿无该键（未进过审阅
       的卷）→ 用 file_store，行为与旧链完全一致。快照只读、不回写 file_store。
    4. route_summary / verify_incomplete / recognized_pages_at_export 快照。

    task kind 直接用 ``kind`` 值（r2 F8 (a)：meta 不带 ``kind`` 键——
    ``_PUBLIC_TASK_KEYS`` 含 kind，重启路径 ``_load_persisted`` 从顶层读、内存路径
    ``public()`` 从 task.kind 读，两路同源，无双写覆盖巧合）。
    """
    manager = manager or export_task_manager
    if kind not in EXPORT_KINDS:
        raise PartialExportError("export_kind_invalid", "kind 必须是 full 或 partial")

    page_count = page_status_service.page_count_of(item_row, file_info)
    paged = page_status_service.is_paged_file_type(file_info.get("file_type"))
    # Issue#280（r2 F-I3）：「可导出页」与说明页「已识别 N 页」都走 exportable 口径
    # （auto ∪ manual，见 page_status_service）。两处消费（API 准入闸 jobs.py export
    # 端点 + 本处 validate/recognized_pages_at_export）必须同源，只改一处即口径分裂。
    exportable = page_status_service.exportable_page_numbers(item_row, page_count)
    failed_map = page_status_service.failed_page_reasons(item_row)

    # —— 框账权威次序：review 草稿 > file_store（§2.6，改框即生效）——
    draft: Any = None
    if store is not None:
        try:
            draft = store.get_item_review_draft(item_row["id"])
        except Exception:  # noqa: BLE001 - 草稿读取失败按"无草稿"处理（file_store 兜底）
            draft = None
    if isinstance(draft, dict) and draft.get("bounding_boxes") is not None:
        boxes_source = draft["bounding_boxes"]
    else:
        boxes_source = (file_info or {}).get("bounding_boxes")
    export_file_view = {**(file_info or {}), "bounding_boxes": boxes_source}

    selection = validate_export_pages(
        pages if kind == "partial" else None,
        kind=kind,
        page_count=page_count,
        failed_pages=set(page_status_service.failed_page_numbers(item_row)),
        recognized_pages=exportable,
        file_type=None if kind == "full" else file_info.get("file_type"),
        boxes_pages=None if kind == "full" else boxes_pages_of(export_file_view),
        redactable_pages=None if kind == "full" else redactable_pages_of(export_file_view),
    )
    pages_snapshot = sorted({_safe_int(p, 0) for p in selection["pages"]})

    # —— 排除清单（对象化，#278 manifest 契约；full=全部失败页带原因码，
    # partial=校验器返回的区间排除）——
    excluded_pages = selection["excluded_failed_pages"]
    excluded_with_reason = [
        (page, failed_map.get(page, ""))
        for page in sorted({_safe_int(x, 0) for x in excluded_pages})
    ]

    # —— manual 页快照（#280 §4.2 契约；full 列全部条目，partial 只列 included）——
    ledger = page_status_service.manual_ledger_of(item_row)
    if kind == "partial":
        included = set(pages_snapshot)
        manual_entries = [entry for entry in ledger if entry["page"] in included]
    else:
        manual_entries = [entry for entry in ledger if 0 < entry["page"] <= max(page_count, 0)]

    boxes_snapshot = normalize_boxes(boxes_source)
    source_path = str((file_info or {}).get("file_path") or "")
    # Issue#273 拍板 b：included 页里的对账未完成页在快照期一次取齐。
    verify_incomplete_snapshot = verify_incomplete_pages_of(file_info, pages_snapshot)
    route_summary = page_status_service.route_counts_of(item_row, pages_snapshot)
    recognized_pages_at_export = len(exportable)
    snapshot_at = datetime.now(UTC).isoformat()

    # —— full 准入前置：主产物必须已存在（完整导出 = 成品包，选页导出 = 快照包）——
    output_path = str((file_info or {}).get("output_path") or "")
    if kind == "full" and (not output_path or not os.path.isfile(output_path)):
        raise PartialExportError(
            "export_not_ready",
            "主产物不存在，请先完成识别打码（或改用选页导出拿快照）",
        )

    # —— 标题与 arcname ——
    if kind == "full":
        stem = os.path.splitext(str((file_info or {}).get("original_filename") or "案卷"))[0] or "案卷"
        title = f"{stem}-完整导出"
        product_ext = os.path.splitext(output_path)[1].lower() or ".pdf"
        arcname = f"{_safe_name(stem)}-脱敏{product_ext}"
    else:
        title = build_export_title(filename=(file_info or {}).get("original_filename"), pages=pages_snapshot)
        arcname = f"{_safe_name(title)}.pdf"

    job_id = item_row.get("job_id")
    item_id = item_row.get("id")
    file_id = item_row.get("file_id")

    # —— 供料附产物（#194/#196：与 collect_batch_zip_entries 的 agent_artifact 口径
    # 一致；缺失记 manifest skipped 不回滚主成品）——
    skipped: list[dict[str, str]] = []
    agent_entries: list[tuple[str, str, int]] = []
    vl_meta = (file_info or {}).get("vl_md_meta")
    if kind == "full" and isinstance(vl_meta, dict):
        for artifact_kind in ("mapping_path", "retained_path"):
            extra_path = vl_meta.get(artifact_kind)
            reason = f"missing_agent_artifact_{artifact_kind}"
            if (
                not extra_path
                or not safe_path_in_dir(str(extra_path), settings.OUTPUT_DIR)
                or not os.path.isfile(str(extra_path))
            ):
                skipped.append({"file_id": str(file_id or ""), "reason": reason})
                continue
            try:
                extra_size = os.path.getsize(str(extra_path))
            except OSError:
                skipped.append({"file_id": str(file_id or ""), "reason": reason})
                continue
            esafe = os.path.basename(str(extra_path)) or "artifact"
            agent_entries.append((str(extra_path), esafe, extra_size))

    async def _runner(task: ExportTask, progress: Any) -> list[dict[str, Any]]:
        # 页事件上下文（#279）：runner 无请求上下文，显式 set；全程 fail-open。
        try:
            page_event_log.set_event_context(str(job_id or ""), str(item_id or ""), str(file_id or ""))
            page_event_log.log_event(
                "export_started",
                kind=kind,
                pages_count=len(pages_snapshot),
                include_summary=bool(include_summary),
                export_id=task.export_id,
            )
        except Exception:  # noqa: BLE001
            pass
        generated = datetime.now(UTC).isoformat()
        work_dir = os.path.join(task.out_dir, "work")
        try:
            return await _runner_body(task, progress, work_dir, generated)
        except BaseException as exc:
            # 失败事件（#278 §2.5）：error_type 非空；fail-open 包裹，绝不二次抛错。
            with contextlib.suppress(Exception):
                page_event_log.log_event(
                    "export_failed",
                    error_type=type(exc).__name__,
                    export_id=task.export_id,
                )
            raise

    async def _runner_body(task: ExportTask, progress: Any, work_dir: str, generated: str) -> list[dict[str, Any]]:
        lines = build_summary_lines(
            pages=pages_snapshot,
            page_count=page_count,
            excluded=excluded_with_reason,
            snapshot_at=snapshot_at,
            generated_at=generated,
            export_id=task.export_id,
            recognized_pages_at_export=recognized_pages_at_export,
            verify_incomplete_pages=verify_incomplete_snapshot,
            manual_pages=[entry["page"] for entry in manual_entries],
        ) if include_summary else []

        def _progress(current: int, total: int) -> None:
            progress(current=current, total=total, stage="rendering")
            # 逐页事件（#278 §2.5 / #279 白名单）：page 由上下文携带。
            with contextlib.suppress(Exception):
                page_event_log.set_current_page(int(current) if current else None)
                page_event_log.log_event("export_page_done", export_id=task.export_id)

        product = await build_item_package(
            kind=kind,
            source_path=source_path,
            pages=pages_snapshot,
            boxes=boxes_snapshot,
            config=config,
            out_dir=work_dir,
            final_name=arcname,
            include_summary=include_summary,
            summary_lines=lines,
            output_path=output_path or None,
            paged=paged,
            source_page_count=page_count,
            progress_cb=_progress,
        )

        entries: list[tuple[str, str, int]] = [(product["pdf_path"], arcname, product["bytes"])]
        if product.get("summary_path"):
            entries.append(
                (product["summary_path"], SUMMARY_TXT_NAME, os.path.getsize(product["summary_path"]))
            )
        entries.extend(agent_entries)
        files_payload = [
            {"name": name, "bytes": size, "sha256": _sha256_of_file(path)}
            for path, name, size in entries
        ]
        # 记录面持久化（V7/A6）：files sha256 写进 task.meta，worker 在 runner 返回后
        # _persist(task) ⇒ export-status.json 里即有该键，重启扫盘可还原。
        task.meta["files_sha256"] = files_payload

        manifest = {
            "export_id": task.export_id,
            "kind": kind,
            "job_id": job_id,
            "item_id": item_id,
            "file_id": file_id,
            "source_filename": (file_info or {}).get("original_filename") or "",
            "source_page_count": max(_safe_int(page_count, 0), 0) if paged else 0,
            "pages": pages_snapshot,
            "page_count": product["page_count"],
            "excluded_failed_pages": [
                {"page": page, "reason_code": _safe_reason(reason)}
                for page, reason in excluded_with_reason
            ],
            # Issue#280 §4.2 机读契约：partial 只列 included 页的人工页，
            # full 列全部账本条目；空数组显式存在（键不省）。
            "manual_verified_pages": manual_entries,
            "manual_verified_pages_count": len(manual_entries),
            "route_summary": route_summary,
            "recognized_pages_at_export": recognized_pages_at_export,
            "verify_incomplete_pages": list(verify_incomplete_snapshot),
            "include_summary": bool(include_summary),
            "files": files_payload,
            "skipped": skipped,
            "snapshot_at": snapshot_at,
            "generated_at": generated,
            "app_version": settings.APP_VERSION,
        }
        progress(current=len(entries), total=len(entries), stage="packaging")
        volumes = await asyncio.to_thread(
            write_export_volumes,
            entries,
            task.out_dir,
            manifest,
            progress,
        )
        with contextlib.suppress(Exception):
            page_event_log.log_event(
                "export_packaged",
                export_id=task.export_id,
                bytes_total=sum(size for _p, _n, size in entries),
                volumes=len(volumes),
            )
        # work/ 只留干净目录（体积清理，避免 TTL 前占盘）。
        with contextlib.suppress(OSError):
            shutil.rmtree(work_dir, ignore_errors=True)
        return volumes

    task = manager.submit(
        owner_id=owner_id,
        kind=kind,
        runner=_runner,
        title=title,
        total_bytes=0,
        file_count=1,
        meta={
            "job_id": job_id,
            "item_id": item_id,
            "file_id": file_id,
            "pages": pages_snapshot,
            "page_count": len(pages_snapshot) if paged else 0,
            "excluded_failed_pages": [
                {"page": page, "reason_code": _safe_reason(reason)}
                for page, reason in excluded_with_reason
            ],
            # Issue#280：记录面同拍快照（清单本体在 manifest；此处带 count 供任务卡）。
            "manual_verified_pages_count": len(manual_entries),
            "files_sha256": [],  # runner 完成后回填（见 _runner_body），随 export-status.json 落盘
            "include_summary": bool(include_summary),
            "snapshot_at": snapshot_at,
            "recognized_pages_at_export": recognized_pages_at_export,
            "route_summary": route_summary,
            "skipped": skipped,
            "verify_incomplete_pages": list(verify_incomplete_snapshot),
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
