"""Issue#196 供料批量（agent_batch）：管线共享逻辑层。

职责边界：批量工人与确认链共用的纯转换/编排函数——
- 段落构建（MinerU 件走解析适配；文本件走 FileParser 直取合成同构 Seg）；
- 草稿序列化（file_store 单键 ``agent_md_draft``，重启恢复的数据载体）；
- 确认渲染（决策应用 → render_outputs → 三产物落盘 → file_store 登记补丁）。

与单文件供料（agent_md_pipeline_service）的关系：同一套 NER/替换口径
（chunk_segments/run_ner/build_mapping_draft/apply_decisions/render_outputs），
但不用其内存任务态与全局锁——批量 item 的状态由 job_items 状态机承载。
"""
from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.models.schemas import Entity
from app.services.agent_md_types import MappingItem, Seg

logger = logging.getLogger(__name__)

AGENT_MD_DRAFT_KEY = "agent_md_draft"

_MINERU_FILE_TYPES = {"pdf", "pdf_scanned", "image"}


def is_mineru_carrier(file_info: dict[str, Any]) -> bool:
    """pdf/扫描件/图片走 MinerU 解析；其余（docx/doc/txt/md/rtf/html）文本直取。"""
    ft = str(file_info.get("file_type") or "").strip().lower()
    if ft in _MINERU_FILE_TYPES:
        return True
    # file_type 可能还是粗粒度（如 "pdf" 但 is_scanned 已判），两者任一命中即 MinerU
    return ft == "pdf" and bool(file_info.get("is_scanned"))


def build_text_segments(content: str, pages: list[str]) -> list[Seg]:
    """FileParser 直取结果 → 与 clean_segments 产物同构的 Seg 列表。

    有分页文本时逐页成段（retained 的 page 号有意义）；无分页（txt/html 等）
    退化为整文单段。空内容产出空列表（空映射表合法：确认后照出三产物）。
    """
    page_texts = [p for p in (pages or []) if p and p.strip()]
    if not page_texts and content and content.strip():
        page_texts = [content]
    return [Seg(text=t, page_idx=i, source="text") for i, t in enumerate(page_texts)]


async def build_mineru_segments(
    file_info: dict[str, Any],
    *,
    ocr=None,
    on_progress: Callable[[int, int], None] | None = None,
) -> list[Seg]:
    """MinerU 解析 → 清洁段（与单文件供料 Stage1 同一套适配器）。

    #220×#196 交叉：表示选择复用单文件线 ``_choose_representation``——
    4.x docvortex.middle 归一（含 pages 去重与 md 比值守卫）+ 3.x 双表示守卫。
    此前只读 content_list_v2（3.x 口径），4.x 形态下静默出空稿（本地 4.x 栈
    验收实测；生产 3.4.5 有 content_list_v2，云冒烟测不出）。
    解析不出正文时显式 fail-closed（RuntimeError，措辞与单文件 parse_no_text
    三形态一致），由批量工人 except 链落 item FAILED，禁「0 映射静默就绪」。
    """
    from app.services.agent_md_adapter import enrich_image_blocks
    from app.services.agent_md_pipeline_service import AgentMdPipelineService
    from app.services.mineru_parse_client import MineruParseClient

    file_bytes = Path(str(file_info.get("file_path") or "")).read_bytes()
    filename = str(file_info.get("original_filename") or file_info.get("filename") or "document.pdf")
    mineru = MineruParseClient()
    job_id = await mineru.submit(file_bytes=file_bytes, filename=filename)

    def _cb(done: int, total: int) -> None:
        if on_progress:
            on_progress(done, total)

    result = await mineru.wait_result(job_id, on_progress=_cb)
    for _sw in getattr(result, "warnings", ()) or []:
        logger.warning("[agent-md-batch] sidecar warning: %s", _sw)
    segs, warns, _stats, source = AgentMdPipelineService._choose_representation(result)
    if segs is None:
        raise RuntimeError(_parse_no_text_message(result, source))
    if ocr is None:
        from app.services.ocr_service import ocr_service as ocr
    segs, more = enrich_image_blocks(segs, result.images, ocr)
    for w in (*warns, *more):
        logger.warning("[agent-md-batch] adapter warning: %s", w)
    return segs


def _parse_no_text_message(result: Any, source: str) -> str:
    """解析空稿的失败消息（措辞与单文件线 parse_no_text 三形态对齐）。

    与 agent_md_pipeline_service 的分支文案刻意同文——机读原因码统一
    parse_no_text，owner 看到的失败原因不因单件/批量而异。
    """
    if source == "docvortex_middle" or getattr(result, "mineru4_docs", None):
        return ("parse_no_text: MinerU 4.x 产物未解析出任何正文"
                "（docvortex_middle 路 0 字符或文档 pages 不一致）")
    if getattr(result, "unknown_jsons", None):
        return ("parse_no_text: MinerU 产物 schema 无法识别"
                f"（未知 JSON: {', '.join(result.unknown_jsons)}）")
    return "parse_no_text: MinerU 未解析出任何正文（v1/v2 两路均 0 字符）"


def resolve_agent_md_entity_types(entity_type_ids: list[str] | None, owner_id: str | None) -> list:
    """识别项口径：向导清单优先；空则回落单文件同款 owner 缺省（默认九类三态过滤）。"""
    from app.services.entity_type_service import resolve_requested_entity_types
    from app.services.vl_md_pipeline_service import VL_MD_DEFAULT_TYPE_IDS

    ids = [str(x) for x in (entity_type_ids or []) if str(x).strip()]
    if ids:
        return resolve_requested_entity_types(ids, owner_id)
    return resolve_requested_entity_types(list(VL_MD_DEFAULT_TYPE_IDS), owner_id)


async def run_agent_md_stage1(
    file_info: dict[str, Any],
    cfg: dict[str, Any],
    *,
    owner_id: str | None = None,
    ner=None,
    on_parse_progress: Callable[[int, int], None] | None = None,
    on_ner_progress: Callable[[int, int], None] | None = None,
) -> dict[str, Any]:
    """Stage1：段落构建 → NER → 映射草稿 → 可序列化 draft dict。

    抛出的任何异常由批量工人的 except 链接管（该 item FAILED，不阻塞整批）。
    """
    from app.services.agent_md_ner import build_mapping_draft, chunk_segments, run_ner

    if ner is None:
        from app.services.hybrid_ner_service import HybridNERService

        ner = HybridNERService()

    if is_mineru_carrier(file_info):
        carrier = "mineru"
        segs = await build_mineru_segments(file_info, on_progress=on_parse_progress)
    else:
        from app.models.schemas import FileType
        from app.services.file_parser import FileParser

        carrier = "text"
        raw_type = str(file_info.get("file_type") or "txt")
        try:
            ftype = FileType(raw_type)
        except ValueError:
            ftype = FileType.TXT
        parsed = await FileParser().parse(str(file_info.get("file_path") or ""), ftype)
        segs = build_text_segments(parsed.content, parsed.pages)

    if on_parse_progress:
        on_parse_progress(1, 1)
    chunks = chunk_segments(segs)
    entity_types = resolve_agent_md_entity_types(
        list(cfg.get("entity_type_ids") or []), owner_id
    )
    entities, _chunks = await run_ner(
        chunks, entity_types, ner,
        on_chunk=(lambda done, total: on_ner_progress(done, total)) if on_ner_progress else None,
    )
    mapping = build_mapping_draft(entities)
    return serialize_draft(segs, entities, mapping, carrier=carrier)


# ---------------------------------------------------------------------------
# 草稿序列化（file_store 行是 JSON 单值存储，dataclass 需手工双向）
# ---------------------------------------------------------------------------

def serialize_draft(
    segs: list[Seg], entities: list[Entity], mapping: list[MappingItem], *, carrier: str
) -> dict[str, Any]:
    return {
        "carrier": carrier,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "segs": [
            {"text": s.text, "page_idx": s.page_idx, "source": s.source, "img_path": s.img_path}
            for s in segs
        ],
        "entities": [e.model_dump(mode="json") for e in entities],
        "mapping": [
            {
                "id": m.id,
                "original_text": m.original_text,
                "entity_type": m.entity_type,
                "replacement": m.replacement,
                "excluded": m.excluded,
            }
            for m in mapping
        ],
    }


def load_draft(file_info: dict[str, Any] | None) -> dict[str, Any] | None:
    """取合法草稿；缺键/形状不对一律按无草稿处理（触发重跑而非崩溃）。"""
    if not isinstance(file_info, dict):
        return None
    draft = file_info.get(AGENT_MD_DRAFT_KEY)
    if not isinstance(draft, dict) or not isinstance(draft.get("segs"), list):
        return None
    return draft


def deserialize_draft(draft: dict[str, Any]) -> tuple[list[Seg], list[Entity], list[MappingItem]]:
    segs = [
        Seg(
            text=str(s.get("text") or ""),
            page_idx=int(s.get("page_idx") or 0),
            source=str(s.get("source") or "text"),
            img_path=str(s.get("img_path") or ""),
        )
        for s in draft.get("segs") or []
    ]
    entities = [Entity.model_validate(e) for e in draft.get("entities") or []]
    mapping = [
        MappingItem(
            id=str(m.get("id") or ""),
            original_text=str(m.get("original_text") or ""),
            entity_type=str(m.get("entity_type") or ""),
            replacement=str(m.get("replacement") or ""),
            excluded=bool(m.get("excluded")),
        )
        for m in draft.get("mapping") or []
    ]
    return segs, entities, mapping


# ---------------------------------------------------------------------------
# 确认渲染（commit 环节；CPU 毫秒级，同步执行）
# ---------------------------------------------------------------------------

def commit_agent_md_item(file_info: dict[str, Any], decisions: list[dict] | None) -> dict[str, Any]:
    """应用映射决策 → 渲染三产物落盘 → 返回 file_store 登记补丁。

    草稿缺失（识别段被跳过/库行被清）抛 ValueError，由调用方把 item 打回
    AWAITING_REVIEW 并带错误信息；decisions 为空/None = 全部按默认占位符替换。
    """
    from app.services.agent_md_pipeline_service import _friendly_stem
    from app.services.agent_md_replace import apply_decisions, render_outputs

    draft = load_draft(file_info)
    if draft is None:
        raise ValueError("agent_md draft missing: re-run recognition first")

    segs, entities, mapping = deserialize_draft(draft)
    apply_decisions(mapping, list(decisions or []))
    md, mapping_json, retained_json = render_outputs(segs, entities, mapping)

    output_dir = Path(settings.OUTPUT_DIR)
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = _friendly_stem(
        str(file_info.get("original_filename") or file_info.get("filename") or "document")
    )
    ts = datetime.now().strftime("%Y%m%d_%H%M")
    short4 = _artifact_short4()
    md_path = output_dir / f"{stem}_脱敏MD_{ts}_{short4}.md"
    mapping_path = output_dir / f"{stem}_映射表_{ts}_{short4}.json"
    retained_path = output_dir / f"{stem}_保留清单_{ts}_{short4}.json"
    md_path.write_text(md, encoding="utf-8")
    mapping_path.write_text(
        json.dumps(mapping_json, ensure_ascii=False, indent=2), encoding="utf-8")
    retained_path.write_text(
        json.dumps(retained_json, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(
        "[agent-md-batch] artifacts written: %s (+mapping/retained)", md_path.name,
    )
    # 键名契约同单文件 confirm：vl_md_meta（孤儿清理 main.py 只认 output_path+vl_md_meta）
    return {
        "output_path": str(md_path),
        "original_filename": md_path.name,
        "vl_md_meta": {
            "mapping_path": str(mapping_path),
            "retained_path": str(retained_path),
            "carrier": draft.get("carrier") or "text",
        },
    }


def _artifact_short4() -> str:
    import uuid

    return uuid.uuid4().hex[:4]
