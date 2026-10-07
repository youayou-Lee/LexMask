"""Issue#75 决策应用与占位替换出稿（自带实现，产物与 #66 同构）。

纯逻辑层：无 I/O、不依赖具体服务；实体偏移为 "\n\n" 连接后的段级全局偏移，
段间隔长度恒为 2。
"""
from app.models.schemas import Entity
from app.services.agent_md_types import MappingItem, Seg

_SEP = "\n\n"


def apply_decisions(mapping: list[MappingItem], decisions: list[dict]) -> list[MappingItem]:
    """把用户决策落到映射表上（就地修改并返回）。

    action 语义：keep→excluded=False；exclude→excluded=True；
    custom→replacement 取决策值（空/None 回退原值）且 excluded=False；
    未知 id 静默忽略。
    """
    by_id = {m.id: m for m in mapping}
    for d in decisions or []:
        item = by_id.get(d.get("id"))
        if item is None:
            continue
        action = d.get("action")
        if action == "exclude":
            item.excluded = True
        elif action == "keep":
            item.excluded = False
        elif action == "custom":
            item.replacement = d.get("replacement") or item.replacement
            item.excluded = False
    return mapping


def render_outputs(
    segs: list[Seg], entities: list[Entity], mapping: list[MappingItem]
) -> tuple[str, dict, dict]:
    """产出 (脱敏MD全文, 映射表JSON, 保留字段JSON)。

    - excluded 项：原文保留并记入 retained_fields（不替换）；
    - 全局偏移按段拆回（段 i 占 [cursor, cursor+len(text))，过后 cursor+=len+2），
      段内替换按起始偏移**倒序**执行避免位移；
    - (text, type) 不在 mapping 中的实体静默跳过；
    - 全文兜底替换（E4 零残留）：span 替换只覆盖 NER 标注处，同一原文在文档
      其他位置未标注出现时由兜底补齐——对每个未 excluded 的映射项在拼装后的
      全文上执行 text.replace(original, replacement)，按 original_text 长度
      **降序**执行：若被替换的长文本内部包含某 excluded 项文本，长文本优先
      整体替换（excluded 项不单独生效，不产生嵌套损坏）；替换完成后 excluded
      文本仍可能独立出现，按定义允许保留。占位符含 ``[`` ``]`` 而映射原文不含
      该形态，普通 replace 不会二次命中已插入的占位符。
    """
    key = {(m.original_text, m.entity_type): m for m in mapping}
    spans: list[tuple[int, int, str | None]] = []
    for ent in entities:
        item = key.get((ent.text, ent.type))
        if item is None:
            continue
        spans.append((ent.start, ent.end, None if item.excluded else item.replacement))

    pieces: list[str] = []
    retained: list[dict] = []
    cursor = 0
    for seg in segs:  # segs 顺序即阅读序
        seg_start = cursor
        seg_end = cursor + len(seg.text)
        local: list[tuple[int, int, str | None, str]] = []
        for start, end, rep in spans:
            if seg_start <= start and end <= seg_end:
                ls, le = start - seg_start, end - seg_start
                local.append((ls, le, rep, seg.text[ls:le]))
        buf = seg.text
        for ls, le, rep, orig in sorted(local, key=lambda t: t[0], reverse=True):
            if rep is None:
                retained.append(
                    {"text": orig, "type": _find_type(mapping, orig), "page": seg.page_idx + 1}
                )
            else:
                buf = buf[:ls] + rep + buf[le:]
        pieces.append(buf)
        cursor = seg_end + len(_SEP)

    md = _SEP.join(pieces)

    # 全文兜底：NER 只标注了部分出现处，同值其余出现处也要替换（E4 零残留）。
    # 长文本优先（len 降序）避免子串嵌套损坏；excluded 项跳过（保留原文）；
    # 占位符含 [] 而原文不含，不会误伤已插入的占位符；空原文跳过防误插。
    for m in sorted(
        (m for m in mapping if not m.excluded and m.original_text),
        key=lambda m: len(m.original_text),
        reverse=True,
    ):
        md = md.replace(m.original_text, m.replacement)

    mapping_json = {
        "items": [
            {
                "id": m.id,
                "original_text": m.original_text,
                "entity_type": m.entity_type,
                "replacement": m.replacement,
                "excluded": m.excluded,
            }
            for m in mapping
        ]
    }
    retained_json = {"retained_fields": retained}
    return md, mapping_json, retained_json


def _find_type(mapping: list[MappingItem], original: str) -> str:
    """按原文反查实体类型；查不到（理论不该发生）回退 UNKNOWN。"""
    for m in mapping:
        if m.original_text == original:
            return m.entity_type
    return "UNKNOWN"
