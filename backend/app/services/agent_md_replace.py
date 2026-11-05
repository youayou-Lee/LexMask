"""Issue#75 决策应用与占位替换出稿（自带实现，产物与 #66 同构）。

纯逻辑层：无 I/O、不依赖具体服务；实体偏移为 "\n\n" 连接后的段级全局偏移，
段间隔长度恒为 2。

Issue#248：新增 ``render_outputs_on_structure``——替换应用在 zip 官方结构化
md 上（表格/标题/页序原样保留），与 ``render_outputs``（拍平重建，回落路径）
语义同构：非排除项全文替换（按 original 长度降序防嵌套）、excluded 记
retained。三道机读闸见各函数 docstring。
"""
import re

from app.models.schemas import Entity
from app.services.agent_md_types import MappingItem, Seg

_SEP = "\n\n"

# Issue#248 结构字符闸：replacement 出现这些字符会破坏 md 表格/标题结构
# （管道表被 | 切断、HTML 被尖括号破坏、表格被换行断行）。化名/掩码生成器
# 不产出这些字符；用户 custom 决策带入时 fail-closed。
_REPLACEMENT_STRUCTURAL_RE = re.compile(r"[|<>`\r\n]")

_PAGE_MARKER_RE = re.compile(r"<!--\s*page\s+\d+\s+of\s+\d+\s*-->")
_HTML_TAG_RES = {tag: re.compile(rf"<{tag}\b", re.IGNORECASE)
                 for tag in ("table", "tr", "td", "th")}
# 管道表行内未转义竖线（\| 是 md 转义字面量，不是分隔符）
_UNESCAPED_PIPE_RE = re.compile(r"(?<!\\)\|")


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


# ---- Issue#248：结构保全渲染（应用在官方结构化 md 上） --------------------


def _seg_of_offset(segs: list[Seg], offset: int) -> Seg | None:
    """实体全局偏移 → 所在 seg（越界/跨界兜底 None）。"""
    cursor = 0
    for seg in segs:
        end = cursor + len(seg.text)
        if cursor <= offset < end:
            return seg
        cursor = end + len(_SEP)
    return None


def _page_of_offset(segs: list[Seg], offset: int) -> int:
    """实体全局偏移 → 1 起始页码（与 render_outputs 的 seg.page_idx+1 口径一致）。"""
    seg = _seg_of_offset(segs, offset)
    if seg is not None:
        return seg.page_idx + 1
    return (segs[-1].page_idx + 1) if segs else 1


def render_outputs_on_structure(
    structured_md: str, segs: list[Seg], entities: list[Entity],
    mapping: list[MappingItem],
) -> tuple[str, dict, dict]:
    """在结构化 md 上执行脱敏替换（Issue#248 主路径）。

    与 render_outputs 语义同构，差别只在载体：替换落在 zip 官方 md
    （含 HTML/管道表格、标题、页标记）上，检测层 segs 仅用于 excluded
    项的页码记账。四道机读闸：

    1. 结构字符闸：非排除项 replacement 含 ``|`` ``<`` ``>`` `` ` `` 或换行
       → ValueError（replacement_structural_chars）——替换值破坏表格结构
       是结构性风险，不许带病进产物；
    2. 前向覆盖闸（门⑥ C1）：非排除项 original_text 必须在结构化 md 中
       出现（替换计数 ≥1）→ 缺失即 ValueError（representation_mismatch）。
       产物文本面=md 而检测面=segs，两者形态失配（拍平剥空白/HTML 转义/
       表示选择分叉）意味着该检测项在产物上没有被替换——映射侧泄漏面
       fail-closed。**豁免**（门⑥ N1）：锚定 seg 来源为 ``img_ocr``/``sentinel``
       的实体——其文本只经 OCR 进检测面、从不在 zip 官方 md 里（md 只有
       图片引用），且结构化产物只写 .md 不带 images/，该文本根本不出现在
       产物中，不存在泄漏；硬拦反而误杀带文字图片案卷（流水截图/凭证）。
    3. 零残留闸：替换完成后任一非排除项 original_text 仍在产物中出现
       → ValueError（zero_residual_violation）——典型成因是替换值回插
       原文（如 replacement="张三丰" 且 original="张三"），机读判据不许
       靠肉眼；
    4. 结构对账：``verify_structure_integrity(输入md, 输出md)`` 由调用方
       在写盘前执行（本函数产出即输入同一字符串，对账交给调用方组装）。

    已知残留边界（门⑥ C1 处置）：md 中存在 NER 从未扫描的文本（clean_segments
    丢弃的 header/footer/page_number 块、strip_inline 剥掉的转义形态）——
    上述闸只覆盖映射侧；该泄漏面用真实卷逐字符对账（门④）取证定级，
    结论写 CHANGELOG。excluded 项不替换（全文兜底跳过，长文本优先的嵌套
    语义与 render_outputs 一致：若 excluded 文本落在更长被替换文本内部，
    随长文本整体替换）。

    retained 口径注（门⑥ M2）：按**实体**记账（每条 excluded 实体一行，
    页码取实体起点所在 seg 的 page_idx+1），非按出现次数；``_page_of_offset``
    对越界偏移兜底取末页——与 legacy「span 未完整落入单 seg 则不记账」在
    台账层面存在口径分歧，记账面放宽（不影响产物文本，仅影响 retained 台账）。
    """
    items = [m for m in mapping if not m.excluded and m.original_text]
    bad = [m for m in items if _REPLACEMENT_STRUCTURAL_RE.search(m.replacement or "")]
    if bad:
        first = bad[0]
        raise ValueError(
            f"replacement_structural_chars: 映射 {first.id} 的替换值含 md 结构字符"
            f"（|<>` 或换行），会破坏表格——original={first.original_text!r} "
            f"replacement={first.replacement!r}"
        )

    retained: list[dict] = []
    key = {(m.original_text, m.entity_type): m for m in mapping}
    for ent in entities:
        item = key.get((ent.text, ent.type))
        if item is not None and item.excluded:
            retained.append(
                {"text": ent.text, "type": _find_type(mapping, ent.text),
                 "page": _page_of_offset(segs, ent.start)}
            )

    md = structured_md
    # 门④实弹修正：拍平 segs 的实体文本可能含换行（跨单元格拼接，如车牌
    # ``桂A\\n2026``），结构化 md 里同事实是连续/异形空白形态——字节级
    # replace 替换不到。含空白项改走**空白柔性匹配**：按空白切段、逐段
    # re.escape、以 ``\\s*`` 连接做正则替换（语义=同事实的空白形态差，
    # 不放宽到其他字符）；纯项保持字节级 replace（快路径不变）。
    _has_ws = re.compile(r"\s")
    _flex = [m for m in items if _has_ws.search(m.original_text)]
    _plain = [m for m in items if not _has_ws.search(m.original_text)]
    _flex_res = {m.id: re.compile(r"\s*".join(re.escape(p) for p in re.split(r"\s+", m.original_text)))
                 for m in _flex}
    # 门⑥ C1 前向覆盖闸：检测项必须在产物载体上真实出现并 ≥1 次被替换。
    # 缺失=检测面（segs）与产物面（md）表示失配——该项在 md 上未被脱敏。
    # 门⑥ N1 豁免：锚定 seg 为 img_ocr/sentinel 的实体（其文本只经 OCR 进
    # 检测面、不在 zip 官方 md、也不进结构化产物——无泄漏面，不构成失配）。
    exempt = set()
    for ent in entities:
        seg = _seg_of_offset(segs, ent.start)
        if seg is not None and seg.source in ("img_ocr", "sentinel"):
            exempt.add(ent.text)
    uncovered = [m for m in _plain if m.original_text not in md and m.original_text not in exempt]
    uncovered += [m for m in _flex if not _flex_res[m.id].search(md) and m.original_text not in exempt]
    if uncovered:
        names = "、".join(repr(m.original_text) for m in uncovered[:3])
        raise ValueError(
            f"representation_mismatch: {len(uncovered)} 个映射项原文在结构化 md 中"
            f"不存在（{names}{' 等' if len(uncovered) > 3 else ''}）——检测面与产物面"
            "表示失配，该项在产物上未被替换，禁止出厂"
        )
    for m in sorted(_plain, key=lambda m: len(m.original_text), reverse=True):
        md = md.replace(m.original_text, m.replacement)
    for m in sorted(_flex, key=lambda m: len(m.original_text), reverse=True):
        md = _flex_res[m.id].sub(m.replacement, md)

    residual = [m for m in _plain if m.original_text in md]
    residual += [m for m in _flex if _flex_res[m.id].search(md)]
    if residual:
        names = "、".join(repr(m.original_text) for m in residual[:3])
        raise ValueError(
            f"zero_residual_violation: {len(residual)} 个映射项原文在产物中残留"
            f"（{names}{' 等' if len(residual) > 3 else ''}）——替换值回插原文或"
            "映射自冲突，禁止出厂"
        )

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
    return md, mapping_json, {"retained_fields": retained}


def _pipe_blocks(md: str) -> list[tuple[int, int]]:
    """管道表格分块：连续 ``|`` 开头行 ⇒（行数, 未转义竖线总数）。

    结构字符闸保证替换值不带 ``|``，故块边界与竖线数在脱敏前后不变——
    变了就是结构被破坏。
    """
    blocks: list[tuple[int, int]] = []
    lines = md.splitlines()
    i = 0
    while i < len(lines):
        if lines[i].lstrip().startswith("|"):
            n, pipes = 0, 0
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                n += 1
                pipes += len(_UNESCAPED_PIPE_RE.findall(lines[i]))
                i += 1
            blocks.append((n, pipes))
        else:
            i += 1
    return blocks


def verify_structure_integrity(src_md: str, out_md: str) -> None:
    """结构对账（Issue#248 AC-A1 机读判据）：脱敏不许改变结构。

    对账三面：页标记序列、HTML 表格标签计数（table/tr/td/th）、管道表
    分块（行数+竖线数）。任一不一致 ⇒ ValueError，产物拒绝出厂。
    正文内容变化（脱敏替换本体）不参与对账。
    """
    problems: list[str] = []
    if _PAGE_MARKER_RE.findall(src_md) != _PAGE_MARKER_RE.findall(out_md):
        problems.append(
            f"页标记序列变化 src={len(_PAGE_MARKER_RE.findall(src_md))}"
            f" out={len(_PAGE_MARKER_RE.findall(out_md))}"
        )
    for tag, rx in _HTML_TAG_RES.items():
        a, b = len(rx.findall(src_md)), len(rx.findall(out_md))
        if a != b:
            problems.append(f"<{tag}> 计数 {a}→{b}")
    if _pipe_blocks(src_md) != _pipe_blocks(out_md):
        problems.append(
            f"管道表分块变化 src={_pipe_blocks(src_md)} out={_pipe_blocks(out_md)}"
        )
    if problems:
        raise ValueError(
            "structure_integrity_violation: 脱敏过程破坏了 md 结构——" + "；".join(problems)
        )
