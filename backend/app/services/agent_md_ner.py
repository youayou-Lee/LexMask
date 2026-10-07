"""Issue#75 NER 分块执行与映射草稿。

- chunk_segments: 清洁文本段 → NER 分块（段间 ``\\n\\n`` 连接，不跨段切分）。
- run_ner: 逐块顺序调 HybridNER，Entity.start/end 平移为全局偏移，page=块首页+1。
- build_mapping_draft: 同 (原文, 类型) 同占位符，N 按该类型内首次出现顺序从 1 递增。
"""
from app.core.config import settings
from app.models.schemas import Entity
from app.services.agent_md_types import Chunk, MappingItem, Seg

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
    """逐块顺序 HybridNER；Entity.start/end 平移到全局偏移，page=块首页+1。"""
    entities: list[Entity] = []
    for chunk in chunks:
        ents = await ner.extract(chunk.text, entity_types)
        for ent in ents:
            ent.start += chunk.base_offset
            ent.end += chunk.base_offset
            ent.page = chunk.page_idx + 1
            entities.append(ent)
    return entities, chunks


def _type_label(entity_type: str) -> str:
    return _TYPE_ZH.get(entity_type, entity_type)


def build_mapping_draft(entities: list[Entity]) -> list[MappingItem]:
    """同 (原文, 类型) 一行、同占位符；N 按该类型内首次出现顺序从 1 递增。

    终审 I2：每 (原文, 类型) 只出一行——render_outputs 的查找 dict 是 last-row-wins，
    重复行会让用户对非末行决策被静默忽略；去重后行 id 与 (text,type) 一一对应。
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
    # 第二遍：每个唯一 (原文, 类型) 出一行，共用编号。
    items: list[MappingItem] = []
    seen: set[tuple[str, str]] = set()
    for ent in entities:
        key = (ent.text, ent.type)
        if key in seen:
            continue
        seen.add(key)
        items.append(
            MappingItem(
                id=f"e{len(items) + 1}",
                original_text=ent.text,
                entity_type=ent.type,
                replacement=f"[{_type_label(ent.type)}_{numbers[key]}]",
            )
        )
    return items
