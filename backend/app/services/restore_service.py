"""还原工具(Issue#70/#50 T7):脱敏文本 + 映射表 → 还原原文。

闭环最后一环:送出去脱敏(占位符/化名/泛化),Agent 结论带回来按映射表还原。
语义(设计定稿,独立评审 REVISE 后):
- 占位符精确查表;texts 多条=一对多,默认 safe(保留+ambiguous 附候选),first 显式取首条;
- 化名词(带序号)反查直还原,最长优先;
- 泛化词(不带序号)直还原但带后界断言:后界为日期/区划续接字符时视为子串碰撞,
  跳过进 ambiguous——宁可不还原,不可损坏文本;
- 映射表没有的占位符/化名进 unknown(幻觉/缺失显式暴露,不假装还原);
- 还原插入的原文不再参与后续匹配(单遍、按位置切片,天然防级联)。
"""

import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

PLACEHOLDER_RE = re.compile(r"\[[A-Za-z][A-Za-z0-9_]*_\d+\]")
# 化名词:替换引擎生成的合成词形态(某+名词+可选序号)
PSEUDONYM_RE = re.compile(r"某[\u4e00-\u9fff]{1,6}\d{0,3}")
# 泛化词后界续接字符(日期/区划):命中即视为子串碰撞
_BOUNDARY_CONT_RE = re.compile(r"[0-9〇一二三四五六七八九十月日时分秒号区县市省旗镇乡村路街巷道屯清新]")

VALID_POLICIES = ("safe", "first")


@dataclass
class MappingEntry:
    texts: list[str]
    type: str | None = None


@dataclass
class Mapping:
    entries: dict[str, MappingEntry] = field(default_factory=dict)
    parse_warnings: list[str] = field(default_factory=list)


@dataclass
class RestoreResult:
    restored_text: str
    restored_count: int = 0
    ambiguous: list = field(default_factory=list)   # [{key, candidates}]
    unknown: list[str] = field(default_factory=list)
    hits: dict = field(default_factory=dict)        # {placeholder: n, pseudonym: n, generalized: n, skipped_boundary: n}


def normalize_mapping(raw: object) -> Mapping:
    """三种输入格式归一为新结构;非法条目跳过并计入 parse_warnings。"""
    mapping = Mapping()
    if not isinstance(raw, dict):
        mapping.parse_warnings.append("mapping 必须是对象,已按空映射处理")
        return mapping
    for key, value in raw.items():
        key = str(key).strip()
        if not key:
            continue
        # 扁平反查:{原文: 替换词};若键本身是占位符形态则按旧格式 {text} 语义
        if isinstance(value, str):
            # 字符串值 = T1 退化格式 {替换词: 原文};等价于 {text: 原文}
            if value.strip():
                mapping.entries[key] = MappingEntry(texts=[value.strip()])
            else:
                mapping.parse_warnings.append(f"条目 {key!r} 原文为空,已跳过")
            continue
        if isinstance(value, dict):
            texts: list[str] | None = None
            if isinstance(value.get("texts"), list) and value["texts"]:
                texts = [str(t) for t in value["texts"] if str(t).strip()]
            elif isinstance(value.get("text"), str) and value["text"].strip():
                texts = [value["text"].strip()]
            if not texts:
                mapping.parse_warnings.append(f"条目 {key!r} 无有效原文,已跳过")
                continue
            mapping.entries[key] = MappingEntry(
                texts=texts, type=str(value["type"]) if value.get("type") else None,
            )
            continue
        mapping.parse_warnings.append(f"条目 {key!r} 值类型不支持({type(value).__name__}),已跳过")
    return mapping


def _pseudonym_like(key: str) -> bool:
    if PLACEHOLDER_RE.fullmatch(key):
        return False
    return bool(PSEUDONYM_RE.fullmatch(key))


def _generalized_like(key: str) -> bool:
    """泛化词:非占位符、非带序号化名的非原文残留 key(某大学/广东省/1987年/2023年等)。"""
    if PLACEHOLDER_RE.fullmatch(key):
        return False
    if PSEUDONYM_RE.fullmatch(key) and key[-1].isdigit():
        return False
    return True


def _replace_spans(text: str, spans: list[tuple[int, int, str]]) -> str:
    """按 start 降序切片替换,插入文本不参与后续匹配。"""
    for start, end, repl in sorted(spans, key=lambda x: x[0], reverse=True):
        text = text[:start] + repl + text[end:]
    return text


def restore(text: str, mapping: Mapping, policy: str = "safe") -> RestoreResult:
    if policy not in VALID_POLICIES:
        raise ValueError(f"policy 必须是 {VALID_POLICIES} 之一,当前: {policy!r}")
    result = RestoreResult(restored_text=text)
    if not text or not mapping.entries:
        # 空文本原样;空映射时把可见占位符报为 unknown(安全信号)
        if text and not mapping.entries:
            result.unknown = sorted(set(PLACEHOLDER_RE.findall(text)))
        return result

    spans: list[tuple[int, int, str]] = []
    hits = {"placeholder": 0, "pseudonym": 0, "generalized": 0, "skipped_boundary": 0}
    consumed_spans: list[tuple[int, int]] = []

    def overlapped(s: int, e: int) -> bool:
        return any(s < ce and cs < e for cs, ce in consumed_spans)

    def resolve(entry: MappingEntry) -> str | None:
        if len(entry.texts) == 1:
            return entry.texts[0]
        if policy == "first":
            return entry.texts[0]
        return None  # safe:一对多不猜

    # ① 占位符精确扫描
    for m in PLACEHOLDER_RE.finditer(text):
        key = m.group(0)
        entry = mapping.entries.get(key)
        if entry is None:
            result.unknown.append(key)
            continue
        repl = resolve(entry)
        if repl is None:
            result.ambiguous.append({"key": key, "candidates": list(entry.texts)})
            continue
        spans.append((m.start(), m.end(), repl))
        consumed_spans.append((m.start(), m.end()))
        hits["placeholder"] += 1

    # ② 化名词/泛化词:按 key 长度降序做字面扫描(含序号完整命中)
    pseudo_keys = sorted(
        (k for k in mapping.entries if _pseudonym_like(k) or _generalized_like(k)),
        key=len, reverse=True,
    )
    for key in pseudo_keys:
        entry = mapping.entries[key]
        generalized = _generalized_like(key)
        start = 0
        while True:
            i = text.find(key, start)
            if i == -1:
                break
            j = i + len(key)
            if overlapped(i, j):
                start = j
                continue
            if generalized and j < len(text) and _BOUNDARY_CONT_RE.match(text[j]):
                # 子串碰撞(如「2023年3月7日」中的「2023年」):跳过进 ambiguous
                result.ambiguous.append({"key": key, "candidates": list(entry.texts),
                                         "reason": "boundary"})
                hits["skipped_boundary"] += 1
                start = j
                continue
            repl = resolve(entry)
            if repl is None:
                result.ambiguous.append({"key": key, "candidates": list(entry.texts)})
                start = j
                continue
            spans.append((i, j, repl))
            consumed_spans.append((i, j))
            hits["pseudonym" if not generalized else "generalized"] += 1
            start = j

    # unknown 化名词:文本里的化名形态词,若不被任何映射 key 覆盖则报 unknown
    for m in PSEUDONYM_RE.finditer(text):
        w = m.group(0)
        if w in mapping.entries:
            continue
        if any(w in k or k in w for k in mapping.entries):
            continue  # 是某 key 的子串/超串(如「某公司」是「某公司1」前缀形态),不误报
        if any(ws <= text.find(w) < we for ws, we in consumed_spans):
            continue  # 位于将被替换的区间内(原文形态),非幻觉
        if w not in result.unknown:
            result.unknown.append(w)

    result.restored_text = _replace_spans(text, spans)
    result.restored_count = len(spans)
    result.unknown = sorted(set(result.unknown))
    result.hits = hits
    return result
