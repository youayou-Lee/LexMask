"""Issue#75 NER 分块执行与映射草稿。

- chunk_segments: 清洁文本段 → NER 分块（段间 ``\\n\\n`` 连接，不跨段切分）。
- run_ner: 逐块顺序调 HybridNER，Entity.start/end 平移为全局偏移，page=块首页+1。
- build_mapping_draft: 同 (原文, 类型) 同替换值；PERSON 化名「[姓某]」，同姓多个加
  汉字序号「[姓某一]/[姓某二]」，其余类型 [类型_N] 占位符，N 按该类型内首次出现顺序从 1 递增。
"""
import logging
import re
from collections.abc import Callable

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
    "ORG": "机构", "CASE_NUMBER": "案号", "ID_CARD": "证件号",
    "LICENSE_PLATE": "车牌", "WECHAT_ALIPAY": "支付账号",
}

# Issue#281 续：模型召回兜底（正则优先占 span）。真卷 86 页扫描件验收时
# NER 漏了 21 个身份证号/22 个手机号——扫描件 OCR 文本里这类 18/11 位数字
# 串还有格式噪声，模型召回不稳定；供料产物是要交给下游 AI 的，漏一个就是
# 一次泄露。故与 vl_md 管线同口径补一层数字类 PII 正则，外加微信 ID
# （wxid_ 前缀，OCR 常把下划线写成各种形态，统一按原文兜住）。
_PII_REGEXES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("ID_CARD", re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")),
    ("PHONE", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    ("BANK_CARD", re.compile(r"(?<!\d)6222\d{12,15}(?!\d)")),
    ("WECHAT_ALIPAY", re.compile(r"wxid_[0-9A-Za-z_-]{4,}")),
)


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
    on_chunk: Callable[[int, int], None] | None = None,
) -> tuple[list[Entity], list[Chunk]]:
    """逐块顺序 HybridNER；Entity.start/end 平移到全局偏移，page=块首页+1。

    Issue#88：NER 偶尔把包裹性引号并进实体 span（「“李四”」），不剥则映射表
    原文带引号、姓氏派生取到引号退化「[“某]」，确认后替换整 span 吞引号。
    在此收口剥引号并同步修正全局偏移。
    on_chunk(done, total)：每块完成后回调（供料批量进度条用，Issue#196；缺省无）。
    """
    entities: list[Entity] = []
    total = len(chunks)
    for done, chunk in enumerate(chunks, start=1):
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
        if on_chunk:
            on_chunk(done, total)
    return entities, chunks


def regex_pii_entities(segs: list[Seg], type_ids: set[str] | None = None) -> list[Entity]:
    """正则补层：数字类 PII + 微信 ID（Issue#281 续）。

    偏移口径与 run_ner 一致——全局拼接文本（段间 ``\\n\\n``）上的绝对偏移，
    ``page = 段页码 + 1``。正则按 _PII_REGEXES 顺序优先占 span，重叠不再重复出项。
    """
    text = _SEP.join(seg.text for seg in segs)
    bounds: list[tuple[int, int, int]] = []
    pos = 0
    for seg in segs:
        bounds.append((pos, pos + len(seg.text), seg.page_idx))
        pos += len(seg.text) + len(_SEP)
    out: list[Entity] = []
    taken: list[tuple[int, int]] = []
    for type_id, pattern in _PII_REGEXES:
        # 识别范围契约：owner 关掉某类型时补层也不得越权打码（与 vl_md 同口径）。
        if type_ids is not None and type_id not in type_ids:
            continue
        for mo in pattern.finditer(text):
            start, end = mo.span()
            if any(start < t_end and t_start < end for t_start, t_end in taken):
                continue
            page_idx = next(
                (p for b_start, b_end, p in bounds if b_start <= start < b_end), 0)
            out.append(Entity(
                id=f"rx{len(out) + 1}",
                text=mo.group(0),
                type=type_id,
                start=start,
                end=end,
                page=page_idx + 1,
            ))
            taken.append((start, end))
    return out


def _would_be_masked(ent: Entity) -> bool:
    """与 build_mapping_draft 的默认 excluded 规则同口径（ORG/单字符/短数字不算已覆盖）。"""
    if len(ent.text) <= 1 or ent.type == "ORG":
        return False
    return not (ent.text.isdigit() and len(ent.text) <= 4)


def merge_regex_entities(entities: list[Entity], segs: list[Seg],
                         type_ids: set[str] | None = None) -> list[Entity]:
    """NER 实体 + 正则补层。

    仅当 NER 已有一个「会真正打码」的实体**完整包住**补层命中时才丢弃补层项；
    部分重叠不丢——真卷 86 页扫描件里 NER 会产出 1 字符的 CASE_NUMBER:'4' 压在
    身份证号首位，旧「区间重叠即丢」规则会让 21 个身份证号全部漏网。
    """
    extra = regex_pii_entities(segs, type_ids)
    if not extra:
        return entities
    out = list(entities)
    changed = False
    for ent in extra:
        # 同一 span 文本已被 NER 以别的类型召回：数字类形态以正则类型为准
        # （真卷里 NER 把 18 位身份证号标成 CASE_NUMBER，产物会读成「身份证号码:案号26」）。
        exact = [e for e in out if e.start == ent.start and e.text == ent.text]
        if exact:
            if any(e.type == ent.type for e in exact):
                continue
            out = [e for e in out if not (e.start == ent.start and e.text == ent.text)]
            out.append(ent)
            changed = True
            continue
        covered = any(
            _would_be_masked(e) and e.start <= ent.start and ent.end <= e.end
            for e in out
        )
        if covered:
            continue
        out.append(ent)
        changed = True
    if not changed:
        return entities
    logging.getLogger(__name__).info(
        "[agent-md] regex PII supplement: %d entities (net %+d)",
        len(extra), len(out) - len(entities))
    return out


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
    化名口径（用户 2026-11-05 裁定，修订 2026-10-08 括号稿）：**不带括号**——
    供料产物是给下游 Agent 吃的自然文本，带 [..] 语法标记会被 Agent 复述时
    变形（丢括号），映射表精确反查就断了。该姓唯一实体 → 廖某；同姓多个 →
    汉字序号 廖某一/廖某二…（≥100 回退阿拉伯数字）。其余类型 中文短名+序号
    （案号1/银行卡2）——同 reason，`类型_N` 括号形态一并废弃。
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
                replacement = f"{surname}某"
            else:
                replacement = f"{surname}某{_cn_numeral(per_surname[surname])}"
        else:
            replacement = f"{_type_label(ent.type)}{numbers[key]}"
        # 门④真机事故修复（用户 UI 确认默认路径，2026-11-06）：单字符原文
        # （退化检测如 CASE_NUMBER:'0'）整文 replace 会命中全卷高频字符——
        # 页标记 `<!-- page 8**0** -->` 全灭、数字全毁，结构对账 src=80/out=0。
        # 默认 excluded=True（映射表可见、律师可改），渲染层另有硬闸兜底。
        # Issue#281 追加：纯数字短原文（'10'/'11'/'008'）同族——NER 在扫描卷
        # 里把页码/年份片段误判成 CASE_NUMBER，整文 replace 会把
        # `<!-- page 10 of 86 -->` 的页号改掉（真卷实测页标记 86→84 判结构
        # 破坏、整卷 fail）。长度 ≤3 的纯数字一律默认保留。
        excluded = len(ent.text) == 1 or (
            ent.text.isdigit() and len(ent.text) <= 4
        )
        # 策略表（用户定稿）：机构名保留原文——ORG 默认 excluded=True（泛词
        # 「证券/保险/支行」整文替换还会毁掉列头）；律师可逐条改 keep。
        if ent.type == "ORG":
            excluded = True
        items.append(
            MappingItem(
                id=f"e{len(items) + 1}",
                original_text=ent.text,
                entity_type=ent.type,
                replacement=replacement,
                excluded=excluded,
            )
        )
    return items
