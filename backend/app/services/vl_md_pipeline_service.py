"""VL-MD 脱敏管线(Issue#66/#50 T1):多格式文档 → 脱敏 Markdown + 映射表 + 保留字段清单。

POC(/root/redaction/vl_desens_poc2.py)转正。设计+验收方案见
workspace docs/plans/2026-10-06-issue66-t1-vlmd-pipeline-plan.md。

关键口径(#50 门⓪拍板):
- 扫描页 → VL 服务(8095 /parse raw=true)出 MD + 原始识别块;文本页/文本文件走文字层;
- 数字类 PII(ID_CARD/PHONE/BANK_CARD)管线级正则优先占 span,NER(HybridNER)
  只补非重叠 span——修 POC 实证的「18 位串被 NER 误标 PHONE」;
- 替换 = PLACEHOLDER 模式([TYPE_N] 全局一致,ORG 走化名口径,BIRTH_DATE 留年份,
  ADDRESS 泛化,见 replacement_strategy);
- T5 实体 diff:VL raw 识别块 vs MD 侧收集,三类甄别(covered/superseded/真丢),
  真丢补映射,仍在脱敏 MD 的自愈替换;
- 多轮收敛零泄漏自检(≤3 轮),终态残留非空判 LEAK(不静默)。
"""

import asyncio
import json
import logging
import os
import re
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field

from app.models.entity_schemas import Entity
from app.models.schemas import ReplacementMode
from app.services.redaction.replacement_strategy import RedactionContext

logger = logging.getLogger(__name__)

# 语义类型送 HybridNER(HaS;LICENSE_PLATE 自带 preset 正则走 Stage2)
VL_MD_SEMANTIC_TYPES = ("PERSON", "ORG", "ADDRESS", "CASE_NUMBER", "BIRTH_DATE", "LICENSE_PLATE")
VL_MD_DEFAULT_TYPE_IDS = (*VL_MD_SEMANTIC_TYPES, "ID_CARD", "PHONE", "BANK_CARD")

# 数字类 PII 管线级正则(POC 同款,中文属 \w 故用数字环视):正则优先占 span
VL_MD_PII_REGEXES = {
    "ID_CARD": re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"),
    "PHONE": re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
    "BANK_CARD": re.compile(r"(?<!\d)6222\d{12,15}(?!\d)"),
}

# 成对联动(#50 §2.2 硬约束):证件/银行卡在列时出生日期必须处理
VL_MD_LINKAGE_TRIGGERS = ("ID_CARD", "BANK_CARD")
VL_MD_LINKAGE_TYPE = "BIRTH_DATE"

# NER/VL 偶尔把包裹性引号并进实体 span(Issue#88):引号属标点不属敏感本体,
# 若不剥,替换会吞引号(产物「证人某人1证实」)、姓氏派生取到引号退化「某人N」。
# 不含书名号《》(有语义,不剥)。
QUOTE_CHARS = "“”‘’「」『』\"'"


def _trim_quoted_span(text: str, start: int, end: int) -> tuple[int, int] | None:
    """剥 span 首尾引号,返回修正后 (start, end);全 span 皆引号则 None。

    越界偏移(上游 NER 偶发)原样放行不剥——引号剥写只对有效 span 生效,
    越界 span 交给 apply_entities 漂移检查按泄漏面兜底(Issue#88 修订)。
    """
    if not (0 <= start < end <= len(text)):
        return (start, end)
    while start < end and text[start] in QUOTE_CHARS:
        start += 1
    while end > start and text[end - 1] in QUOTE_CHARS:
        end -= 1
    return None if start >= end else (start, end)

PLACEHOLDER_RE = re.compile(r"\[[A-Za-z][A-Za-z0-9_]*_\d+\]")
_PLACEHOLDER_TYPE_RE = re.compile(r"\[([A-Za-z][A-Za-z0-9_]*)_\d+\]")
CONVERGENCE_MAX_ROUNDS = 3


def _type_of_placeholder(replacement: str) -> str | None:
    m = _PLACEHOLDER_TYPE_RE.fullmatch(replacement.strip())
    return m.group(1) if m else None


def _replaced_words(context: RedactionContext) -> set:
    """已生成的化名合成词(替换值≠原文),供 diff/自检防化名子串污染。"""
    return {v for k, v in context.entity_map.items() if v != k}


def residual_input(text: str, context: RedactionContext) -> str:
    """零泄漏复检的输入:剥占位符 + 剥已生成的化名合成词。

    化名替换词(某人民法院1/某派出所1)不是占位符,留在文本里会被 NER 当成
    新实体再次替换(真实冒烟实证:法院→某人民法院1→某公司8 套娃)。保留原文
    的公共机构不在此剥(value==key 判定),它们是设计内保留,仍在复检面里。
    """
    replaced_words = {v for k, v in context.entity_map.items() if v != k}
    for v in sorted(replaced_words, key=len, reverse=True):
        text = text.replace(v, "")
    return PLACEHOLDER_RE.sub("", text)


class VlMdLeakError(RuntimeError):
    """终态零泄漏自检未通过。产物照常落盘(供审计),但任务按失败处理。"""


@dataclass
class VlMdResult:
    desens_md: str
    mapping: dict = field(default_factory=dict)          # 占位符 -> {text, type}
    diff: dict = field(default_factory=dict)             # covered/superseded/self_healed/lost
    rounds: int = 0
    leaks: list = field(default_factory=list)
    page_sources: list = field(default_factory=list)     # 每页 "vl"|"text"
    entity_count: int = 0


class VlMdPipelineService:
    def __init__(self, vl_client=None, ner_service=None, file_parser=None):
        self._vl_client = vl_client
        self._ner_service = ner_service
        self._file_parser = file_parser
        # job 级映射上下文:同 job 跨文件共享一张映射表(同一实体同一占位符,#50 验收 A4)。
        # 队列 worker 单进程串行,内存态即可;进程重启丢表 → 各文件重新编号(不泄漏,仅一致性降级)。
        # LRU 封顶防无界增长——映射表内存态含真实姓名,PII 不允许永驻(评审 Important#1)。
        self._job_contexts: OrderedDict[str, RedactionContext] = OrderedDict()

    def _vl(self):
        if self._vl_client is None:
            from app.services.vl_parse_client import VlParseClient
            self._vl_client = VlParseClient()
        return self._vl_client

    def _ner(self):
        if self._ner_service is None:
            from app.services.hybrid_ner_service import HybridNERService
            self._ner_service = HybridNERService()
        return self._ner_service

    def _parser(self):
        if self._file_parser is None:
            from app.services.file_parser import FileParser
            self._file_parser = FileParser()
        return self._file_parser

    # ---------- 口径解析(含成对联动) ----------

    def resolve_types(self, cfg: dict, owner_id: str | None):
        from app.services.entity_type_service import resolve_requested_entity_types

        requested = [t for t in (cfg.get("entity_type_ids") or []) if t]
        if not requested:
            requested = list(VL_MD_DEFAULT_TYPE_IDS)
        types = resolve_requested_entity_types(requested, owner_id)
        ids = {t.id for t in types}
        if set(VL_MD_LINKAGE_TRIGGERS) & ids and VL_MD_LINKAGE_TYPE not in ids:
            types.extend(resolve_requested_entity_types([VL_MD_LINKAGE_TYPE], owner_id))
        return types

    # ---------- 页级 MD 组装 ----------

    async def build_markdown(self, file_info: dict):
        """返回 (pages_md, raw_texts, page_sources)。扫描页走 VL,文本页走文字层。"""
        file_path = str(file_info.get("file_path") or "")
        file_type = str(getattr(file_info.get("file_type"), "value", file_info.get("file_type")) or "")
        if file_type.startswith("pdf"):
            return await self._build_markdown_pdf(file_path)
        content = str(file_info.get("content") or "")
        return [content], [], ["text"]

    async def _build_markdown_pdf(self, file_path: str):
        import fitz

        parser = self._parser()
        loop = asyncio.get_running_loop()

        def _open():
            doc = fitz.open(file_path)
            return doc

        doc = await loop.run_in_executor(None, _open)
        pages: list[str] = []
        raw_texts: list[list[str]] = []
        sources: list[str] = []
        try:
            for i in range(1, len(doc) + 1):
                scanned = await parser.is_pdf_page_scanned(file_path, i)
                if scanned:
                    png_path = await self._render_page_png(file_path, i)
                    try:
                        result = await self._vl().parse(png_path)
                        pages.append(result.markdown)
                        raw_texts.extend(result.raw_texts or [])
                    finally:
                        try:
                            os.unlink(png_path)
                        except OSError:
                            pass
                    sources.append("vl")
                else:
                    pages.append(doc[i - 1].get_text())
                    sources.append("text")
        finally:
            doc.close()
        return pages, raw_texts, sources

    async def _render_page_png(self, file_path: str, page: int) -> str:
        import tempfile

        png_bytes = await self._parser().get_pdf_page_image(file_path, page)
        fd, out = tempfile.mkstemp(prefix="vlmd_", suffix=".png")
        with os.fdopen(fd, "wb") as f:
            f.write(png_bytes)
        return out

    # ---------- 实体收集(正则优先占 span) ----------

    async def collect_entities(self, text: str, types) -> list[Entity]:
        claimed: list[tuple[int, int]] = []
        merged: list[Entity] = []
        resolved_ids = {t.id for t in types}

        def _overlap(start: int, end: int) -> bool:
            return any(start < ce and cs < end for cs, ce in claimed)

        seq = 0
        for type_id, rx in VL_MD_PII_REGEXES.items():
            if type_id not in resolved_ids:
                continue
            for m in rx.finditer(text):
                span = _trim_quoted_span(text, m.start(), m.end())
                if span is None:
                    continue
                seq += 1
                merged.append(Entity(
                    id=f"vlmd-rx-{seq}", text=text[span[0]:span[1]], type=type_id,
                    start=span[0], end=span[1], source="regex",
                ))
                claimed.append(span)

        for e in await self._ner().extract(text, types):
            span = _trim_quoted_span(text, e.start, e.end)
            if span is None:
                continue
            if _overlap(*span):
                continue
            if span == (e.start, e.end):
                merged.append(e)
            else:
                merged.append(e.model_copy(
                    update={"text": text[span[0]:span[1]], "start": span[0], "end": span[1]}))
        return merged

    # ---------- 替换 ----------

    @staticmethod
    def apply_entities(md: str, entities: list[Entity],
                       context: RedactionContext) -> tuple[str, list[str]]:
        """按 start 降序切片替换:偏移精确,天然免疫子串误替换(POC 长实体优先的升级)。

        返回 (替换后文本, 偏移漂移被跳过的实体文本列表)——漂移=未替换,调用方须计入泄漏面。
        """
        drifted: list[str] = []
        for e in sorted(entities, key=lambda x: (x.start, -(x.end - x.start)), reverse=True):
            if md[e.start:e.end] != e.text:
                logger.warning("[vl-md] offset drift for entity %r, skip", e.text[:20])
                drifted.append(e.text)
                continue
            md = md[:e.start] + context.get_replacement(e) + md[e.end:]
        return md, drifted

    # ---------- 主流程 ----------

    MAX_CACHED_JOB_CONTEXTS = 64

    def context_for_job(self, job_id: str | None, word_pools: dict | None) -> RedactionContext:
        """同 job 复用同一映射上下文(跨文件一致);无 job_id(直连调用)则独立建表。"""
        if not job_id:
            return RedactionContext(ReplacementMode.PLACEHOLDER, word_pools=word_pools)
        ctx = self._job_contexts.get(job_id)
        if ctx is not None:
            self._job_contexts.move_to_end(job_id)
            return ctx
        while len(self._job_contexts) >= self.MAX_CACHED_JOB_CONTEXTS:
            self._job_contexts.popitem(last=False)
        ctx = RedactionContext(ReplacementMode.PLACEHOLDER, word_pools=word_pools)
        self._job_contexts[job_id] = ctx
        return ctx

    def release_job_context(self, job_id: str) -> None:
        """job 终态后由队列侧调用:释放映射表内存态(含真实姓名)。"""
        self._job_contexts.pop(job_id, None)

    async def process(self, *, pages: list[str], raw_texts: list[list[str]], types,
                      word_pools: dict | None = None,
                      context: RedactionContext | None = None) -> VlMdResult:
        md = "\n\n".join(pages)
        entities = await self.collect_entities(md, types)
        context = context or RedactionContext(ReplacementMode.PLACEHOLDER, word_pools=word_pools)
        desens, drifted = self.apply_entities(md, entities, context)
        md_entity_texts = {e.text for e in entities}

        # T5 diff(raw 块 vs MD 侧;自愈替换在 diff 内同步落 desens)
        diff: dict[str, list] = {"covered": [], "superseded": [], "self_healed": [], "lost": []}
        if raw_texts:
            raw_text = "\n".join(t for pg in raw_texts for t in pg if t)
            if raw_text.strip():
                for e in await self.collect_entities(raw_text, types):
                    if e.text in md_entity_texts or e.text in context.entity_map:
                        continue
                    if any(e.text in m for m in md_entity_texts if m != e.text):
                        diff["covered"].append({"text": e.text, "type": e.type})
                    elif any(m in e.text for m in md_entity_texts):
                        diff["superseded"].append({"text": e.text, "type": e.type})
                    elif e.text in desens and not any(
                        e.text in w for w in _replaced_words(context)
                    ):
                        desens = desens.replace(e.text, context.get_replacement(e))
                        diff["self_healed"].append({"text": e.text, "type": e.type})
                    else:
                        context.get_replacement(e)
                        diff["lost"].append({"text": e.text, "type": e.type})

        # 多轮收敛零泄漏自检
        rounds = 0
        for _ in range(CONVERGENCE_MAX_ROUNDS):
            stripped = residual_input(desens, context)
            residual = [
                e for e in await self.collect_entities(stripped, types)
                if e.text not in context.entity_map and e.text in desens
            ]
            if not residual:
                break
            rounds += 1
            for e in sorted(residual, key=lambda x: len(x.text), reverse=True):
                desens = desens.replace(e.text, context.get_replacement(e))

        final_stripped = residual_input(desens, context)
        leaks = sorted({
            e.text for e in await self.collect_entities(final_stripped, types)
            if e.text not in context.entity_map
        } | set(drifted))  # 替换时偏移漂移被跳过的实体=未替换,计入泄漏面(评审 Minor#2)

        # 反转构建:同替换值(如两个出生日期同年→同一「1976年」)不得互相覆盖(评审 Important#2)
        mapping: dict = {}
        for text, replacement in context.entity_map.items():
            entry = mapping.setdefault(replacement, {"type": _type_of_placeholder(replacement), "texts": []})
            if text not in entry["texts"]:
                entry["texts"].append(text)
        return VlMdResult(
            desens_md=desens,
            mapping=mapping,
            diff=diff,
            rounds=rounds,
            leaks=leaks,
            page_sources=[],
            entity_count=len(context.entity_map),
        )

    # ---------- 文件级入口(产物落盘) ----------

    async def process_file(self, file_info: dict, cfg: dict, job_id: str | None = None) -> dict:
        from app.core.config import settings
        from app.services.file_management_service import file_store
        from app.services.word_pool_service import load_word_pools

        owner_id = str(file_info.get("owner_id") or "local_user")
        types = self.resolve_types(cfg, owner_id)
        pages, raw_texts, sources = await self.build_markdown(file_info)
        if not any(p.strip() for p in pages):
            # A7:空文件/纯图片无文字层/解析失败 → 显式报错,不静默产空产物
            raise ValueError(
                f"文档无可解析文本(file_type={file_info.get('file_type')},"
                "空文件或图片页无文字层),拒绝产出空脱敏文档"
            )
        context = self.context_for_job(job_id, load_word_pools(owner_id))
        result = await self.process(
            pages=pages, raw_texts=raw_texts, types=types, context=context,
        )
        result.page_sources = sources

        output_file_id = uuid.uuid4().hex
        base = os.path.join(settings.OUTPUT_DIR, f"{output_file_id}")
        md_path = f"{base}.desens.md"
        mapping_path = f"{base}.mapping.json"
        retained_path = f"{base}.retained_fields.json"
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(result.desens_md)
        with open(mapping_path, "w", encoding="utf-8") as f:
            json.dump(result.mapping, f, ensure_ascii=False, indent=1)
        with open(retained_path, "w", encoding="utf-8") as f:
            json.dump(self.retained_fields(types), f, ensure_ascii=False, indent=1)

        file_store.update_fields(str(file_info.get("id")), {
            "output_file_id": output_file_id,
            "output_path": md_path,
            "entity_map": result.mapping,
            "redacted_count": result.entity_count,
            "vl_md_meta": {
                "mapping_path": mapping_path,
                "retained_path": retained_path,
                "diff": result.diff,
                "rounds": result.rounds,
                "page_sources": sources,
            },
        })
        summary = {
            "output_file_id": output_file_id,
            "md_path": md_path,
            "mapping_path": mapping_path,
            "retained_path": retained_path,
            "entity_count": result.entity_count,
            "rounds": result.rounds,
            "leaks": result.leaks,
            "verdict": "ZERO-LEAK OK" if not result.leaks else "LEAK",
        }
        if result.leaks:
            raise VlMdLeakError(
                f"零泄漏自检未通过,残留 {len(result.leaks)} 处: {result.leaks[:5]};产物已落盘 {md_path}"
            )
        return summary

    @staticmethod
    def retained_fields(types) -> dict:
        """#50 §2.4 审计清单:本 job 实际生效的口径档位。"""
        resolved = {t.id for t in types}
        keep = [t for t in ("DATE", "TIME", "AMOUNT", "AGE") if t not in resolved]
        return {
            "policy": "issue50-default",
            "replaced_placeholder": sorted(
                (resolved & set(VL_MD_DEFAULT_TYPE_IDS)) - {"BIRTH_DATE", "ADDRESS"}
            ),
            "generalized": sorted(resolved & {"BIRTH_DATE", "ADDRESS"}),
            "pseudonym": sorted(resolved & {"ORG"}),
            "retained_by_default": keep,
            "note": "保留档类型未进识别清单=原样保留(设计内暴露,见 #50 §2.4)",
        }


_service: VlMdPipelineService | None = None


def get_vl_md_pipeline_service() -> VlMdPipelineService:
    global _service
    if _service is None:
        _service = VlMdPipelineService()
    return _service
