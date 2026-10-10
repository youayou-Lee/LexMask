"""Issue#75 NER 分块执行与映射草稿。

- chunk_segments: 清洁文本段 → NER 分块（段间 ``\\n\\n`` 连接，不跨段切分）。
- run_ner: 逐块顺序调 HybridNER，Entity.start/end 平移为全局偏移，page=块首页+1。
- build_mapping_draft: 同 (原文, 类型) 同替换值；PERSON 化名「[姓某]」，同姓多个加
  汉字序号「[姓某一]/[姓某二]」，其余类型 [类型_N] 占位符，N 按该类型内首次出现顺序从 1 递增。
"""
import logging

from app.core.config import settings
from app.models.schemas import Entity
from app.services.agent_md_types import Chunk, MappingItem, Seg
from app.services.redaction.replacement_strategy import (
    WRAPPING_QUOTE_CHARS,
    trim_quoted_span,
)

_SEP = "\n\n"
# 占位符中文短名；缺省回退 type id。（EntityTypeConfig.name 是全名如「银行卡号」，
# 与映射表口径不同，故此处用短名字典而非读配置。）
_TYPE_ZH = {
    "PERSON": "人名", "BIRTH_DATE": "出生日期", "ID_NUMBER": "证件号",
    "BANK_CARD": "银行卡", "PHONE": "电话", "PLATE": "车牌", "ADDRESS": "地址",
    "ORG": "机构",
}


def chunk_segments(segs: list[Seg], max_chars: int | None = None) -> list[Chunk]:
    """段落聚合为 NER 分块：不跨段切分，超长单段整段独立成块。

    chunk.page_idx 取块内首段页码；chunk.base_offset 为该块在全局拼接文本中的起始偏移
    （已冲销掉的先前块长度 + 块间分隔符）。
    """
    budget = max_chars or settings.AGENT_MD_CHUNK_CHARS
    chunks: list[Chunk] = []
    buf: list[str] = []
    buf_len = 0
    buf_page = 0
    base = 0
    for seg in segs:
        t = seg.text
        if buf and buf_len + len(_SEP) + len(t) > budget:
            chunks.append(Chunk(base_offset=base, text=_SEP.join(buf), page_idx=buf_page))
            base += buf_len + len(_SEP)
            buf, buf_len = [], 0
        if not buf:
            buf_page = seg.page_idx
        buf.append(t)
        buf_len = buf_len + (len(_SEP) if len(buf) > 1 else 0) + len(t)
    if buf:
        chunks.append(Chunk(base_offset=base, text=_SEP.join(buf), page_idx=buf_page))
    return chunks


async def run_ner(
    chunks: list[Chunk],
    entity_types: list,
    ner,
) -> tuple[list[Entity], list[Chunk]]:
    """逐块顺序 HybridNER；Entity.start/end 平移到全局偏移，page=块首页+1。

    Issue#88：NER 偶尔把包裹性引号并进实体 span（「“李四”」），不剥则映射表
    原文带引号、姓氏派生取到引号退化「[“某]」，确认后替换整 span 吞引号。
    在此收口剥引号并同步修正全局偏移。
    """
    entities: list[Entity] = []
    for chunk in chunks:
        ents = await ner.extract(chunk.text, entity_types)
        for ent in ents:
            span = trim_quoted_span(chunk.text, ent.start, ent.end)
            if span is None:
                continue
            if (span[0], span[1]) != (ent.start, ent.end):
                logging.getLogger(__name__).info(
                    "[agent-md] quoted span trimmed: %r -> %r",
                    ent.text[:20], chunk.text[span[0]:span[1]][:20])
            ent.start = span[0] + chunk.base_offset
            ent.end = span[1] + chunk.base_offset
            ent.text = chunk.text[span[0]:span[1]]
            ent.page = chunk.page_idx + 1
            entities.append(ent)
    return entities, chunks


def _type_label(entity_type: str) -> str:
    return _TYPE_ZH.get(entity_type, entity_type)


def _cn_numeral(n: int) -> str:
    """1→一 … 10→十、11→十一、20→二十、21→二十一 … 99；≥100 回退阿拉伯数字。"""
    if n >= 100:
        return str(n)
    digits = "零一二三四五六七八九"
    if n < 10:
        return digits[n]
    tens, unit = divmod(n, 10)
    return ("十" if tens == 1 else digits[tens] + "十") + (digits[unit] if unit else "")


def build_mapping_draft(entities: list[Entity]) -> list[MappingItem]:
    """同 (原文, 类型) 一行、同占位符；N 按该类型内首次出现顺序从 1 递增。

    终审 I2：每 (原文, 类型) 只出一行——render_outputs 的查找 dict 是 last-row-wins，
    重复行会让用户对非末行决策被静默忽略；去重后行 id 与 (text,type) 一一对应。
    PERSON 化名口径（用户定稿 2026-10-08）：替换值一律 [..] 括号包裹；该姓唯一实体
    → [袁某]；同姓多个 → 汉字序号 [袁某一]/[袁某二]…（≥100 回退阿拉伯数字）。
    其余类型保持 [类型_N] 占位符——机构/地址化名口径后续对齐 Issue#50 T4。
    """
    # 第一遍：按 (原文, 类型) 去重，首次出现顺序做类型内全局编号。
    numbers: dict[tuple[str, str], int] = {}
    per_type: dict[str, int] = {}
    for ent in entities:
        key = (ent.text, ent.type)
        if key in numbers:
            continue
        per_type[ent.type] = per_type.get(ent.type, 0) + 1
        numbers[key] = per_type[ent.type]
    # 同姓 PERSON 实体总数（按唯一 (text,type) 计数），决定唯一不加序号还是加汉字序号。
    # 姓氏取字前先剥包裹性引号（Issue#88 防御）：「“李四”」取到引号会退化「[“某]」。
    surname_totals: dict[str, int] = {}
    seen_keys: set[tuple[str, str]] = set()
    for ent in entities:
        key = (ent.text, ent.type)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        if ent.type == "PERSON":
            surname0 = ent.text.strip(WRAPPING_QUOTE_CHARS)[:1]
            surname_totals[surname0] = surname_totals.get(surname0, 0) + 1
    items: list[MappingItem] = []
    seen: set[tuple[str, str]] = set()
    per_surname: dict[str, int] = {}
    for ent in entities:
        key = (ent.text, ent.type)
        if key in seen:
            continue
        seen.add(key)
        if ent.type == "PERSON":
            surname = ent.text.strip(WRAPPING_QUOTE_CHARS)[:1]
            per_surname[surname] = per_surname.get(surname, 0) + 1
            if surname_totals[surname] == 1:
                replacement = f"[{surname}某]"
            else:
                replacement = f"[{surname}某{_cn_numeral(per_surname[surname])}]"
        else:
            replacement = f"[{_type_label(ent.type)}_{numbers[key]}]"
        items.append(
            MappingItem(
                id=f"e{len(items) + 1}",
                original_text=ent.text,
                entity_type=ent.type,
                replacement=replacement,
            )
        )
    return items
