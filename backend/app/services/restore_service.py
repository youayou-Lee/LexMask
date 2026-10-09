"""还原工具(Issue#70/#50 T7):脱敏文本 + 映射表 → 还原原文。

闭环最后一环:送出去脱敏(占位符/化名/泛化),Agent 结论带回来按映射表还原。
语义(设计定稿,独立评审 REVISE 后):
- 占位符精确查表;texts 多条=一对多,默认 safe(保留+ambiguous 附候选),first 显式取首条;
- 化名词(带序号)反查直还原,最长优先;
- 泛化词(不带序号)直还原但带后界断言:后界为日期/区划续接字符时视为子串碰撞,
  跳过进 ambiguous——宁可不还原,不可损坏文本;
- 映射表没有的占位符/化名进 unknown(幻觉/缺失显式暴露,不假装还原);
- 还原插入的原文不再参与后续匹配(单遍、按位置切片,天然防级联);
- 括号免疫(Issue#87):产物 [X] 被 Agent 剥成裸 X 后还原契约不失效——映射 key 的
  裸形态同样命中;裸形态后接可延长字符(中文数字/数字)进 ambiguous(boundary) 不猜;
  形似替换词但映射表没有的裸形态进 unknown(安全信号不失守)。
"""

import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

PLACEHOLDER_RE = re.compile(r"\[[A-Za-z][A-Za-z0-9_]*_\d+\]")
# 化名词:替换引擎生成的合成词形态(某+名词+可选序号)
PSEUDONYM_RE = re.compile(r"某[\u4e00-\u9fff]{1,6}\d{0,3}")
# 括号化名词(Issue#75 用户定稿口径 2026-10-08):[袁某]/[袁某一]/[袁某十二] 等
BRACKETED_PSEUDONYM_RE = re.compile(r"\[[\u4e00-\u9fff]某[一二三四五六七八九十]{0,2}\]")
# 括号包裹的映射 key(Issue#87):用于派生裸形态(剥括号后仍可还原)
BRACKETED_KEY_RE = re.compile(r"^\[([^\[\]]+)\]$")
# 裸化名形态(Issue#87 unknown 扫描):仅带汉字序号的姓某一/姓某十二。
# 无序号的裸「姓某」与原生文本(X某公司/于某/王某式原生匿名)不可区分——剥括号后
# 该信息已丢失,还原靠映射表不受影响,unknown 上报收窄到序号形态保零误报;
# X≠某(防「某某」链);(?<!\[) 括号内 token 不算裸;(?![甲乙丙丁人些]) 防「某甲/某人」
_BARE_PSEUDONYM_RE = re.compile(
    r"(?<![A-Za-z0-9_\[])[^\W某]某[一二三四五六七八九十]{1,2}(?![甲乙丙丁人些一二三四五六七八九十])"
)
# 裸占位符形态(Issue#87 unknown 扫描):机构_1/银行卡_12/PERSON_3 等无空白 token
_BARE_PLACEHOLDER_RE = re.compile(r"(?<![A-Za-z0-9_\[])[A-Za-z\u4e00-\u9fff][A-Za-z0-9_\u4e00-\u9fff]*_\d+")
# 映射值反查识别用:无序号裸化名也算产物形态(映射值上下文可信,不受自由文本噪声约束)
_PSEUDONYM_VALUE_RE = re.compile(r"[^\W某]某[一二三四五六七八九十]{0,2}")
# 泛化词后界续接字符(日期/区划):命中即视为子串碰撞
# 后界续接字符:日期(月日年时分秒号代初末底中旬)+ 区划(区县市省旗镇乡村路街巷道屯清新)。
# 注意:「起/以来/前后/起诉」是合法还原续接,不得加入(「2023年起诉」还原后语义正确)。
_BOUNDARY_CONT_RE = re.compile(r"[0-9〇一二三四五六七八九十月日年时分秒号代初末底中旬区县市省旗镇乡村路街巷道屯清新]")

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
    ambiguous: list = field(default_factory=list)  # [{key, candidates, reason?}]
    unknown: list[str] = field(default_factory=list)
    hits: dict = field(default_factory=dict)  # {placeholder, pseudonym, generalized, skipped_boundary}
    used_first: list[str] = field(default_factory=list)  # policy=first 时取首条还原的 key(评审 I3)


def normalize_mapping(raw: object) -> Mapping:
    """三种输入格式归一为新结构;非法条目跳过并计入 parse_warnings。"""
    mapping = Mapping()
    if not isinstance(raw, dict):
        mapping.parse_warnings.append("mapping 必须是对象,已按空映射处理")
        return mapping
    for key, value in raw.items():
        key = str(key).strip()
        # agent-md 映射表格式:{"items": [...]}(list 值仅此键合法)
        if key == "items" and isinstance(value, list):
            _ingest_items_list(mapping, value)
            continue
        if not key:
            continue
        # 扁平反查:{原文: 替换词};若键本身是占位符形态则按旧格式 {text} 语义
        if isinstance(value, str):
            v = value.strip()
            if not v:
                mapping.parse_warnings.append(f"条目 {key!r} 原文为空,已跳过")
            elif (
                PLACEHOLDER_RE.fullmatch(v)
                or PSEUDONYM_RE.fullmatch(v)
                or BRACKETED_KEY_RE.match(v)
                or _PSEUDONYM_VALUE_RE.fullmatch(v)
                or _BARE_PLACEHOLDER_RE.fullmatch(v)
            ):
                # 反查方向 {原文: 替换词}(如 build_preview_entity_map 的 entity_map)
                mapping.entries[v] = MappingEntry(texts=[key])
            else:
                # T1 退化格式 {替换词: 原文}
                mapping.entries[key] = MappingEntry(texts=[v])
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
                texts=texts,
                type=str(value["type"]) if value.get("type") else None,
            )
            continue
        mapping.parse_warnings.append(f"条目 {key!r} 值类型不支持({type(value).__name__}),已跳过")
    return mapping


def _ingest_items_list(mapping: Mapping, items: list) -> None:
    """agent-md 映射表格式（Issue#75）:{"items": [{id, original_text, entity_type,
    replacement, excluded}, ...]}。替换词 → 原文（还原方向）；excluded 项未被替换,
    无需还原,跳过并计一条提示。"""
    for entry in items:
        if not isinstance(entry, dict):
            mapping.parse_warnings.append(f"items 中非对象条目已跳过: {entry!r}")
            continue
        original = str(entry.get("original_text") or "").strip()
        replacement = str(entry.get("replacement") or "").strip()
        if not original or not replacement:
            mapping.parse_warnings.append(f"items 条目 {entry.get('id')!r} 原文或替换词为空,已跳过")
            continue
        if entry.get("excluded"):
            mapping.parse_warnings.append(f"items 条目 {entry.get('id')!r} 为保留项(excluded),未替换无需还原")
            continue
        mapping.entries[replacement] = MappingEntry(
            texts=[original],
            type=str(entry["entity_type"]) if entry.get("entity_type") else None,
        )


def _pseudonym_like(key: str) -> bool:
    if PLACEHOLDER_RE.fullmatch(key):
        return False
    return bool(PSEUDONYM_RE.fullmatch(key) or BRACKETED_PSEUDONYM_RE.fullmatch(key))


def _generalized_like(key: str) -> bool:
    """泛化词:非占位符、非带序号化名的非原文残留 key(某大学/广东省/1987年/2023年等)。"""
    if PLACEHOLDER_RE.fullmatch(key):
        return False
    if PSEUDONYM_RE.fullmatch(key) and key[-1].isdigit():
        return False
    if BRACKETED_PSEUDONYM_RE.fullmatch(key):
        return False  # 括号化名按化名处理,不吃泛化词后界断言
    return True


def _replace_spans(text: str, spans: list[tuple[int, int, str]]) -> str:
    """单遍拼接替换(升序扫描);span 互不重叠,插入文本不参与后续匹配。
    (Issue#87:裸形态使 span 数可达数万,原降序逐个切片整串拷贝为 O(n²))"""
    if not spans:
        return text
    out: list[str] = []
    prev = 0
    for start, end, repl in sorted(spans, key=lambda x: x[0]):
        out.append(text[prev:start])
        out.append(repl)
        prev = end
    out.append(text[prev:])
    return "".join(out)


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
    # 已消费区间的覆盖位置集(Issue#87: span 总长 ≤ 文本长,查询 O(span 长度);
    # 原线性 any() 扫描在大文本下 O(n²))
    _covered: set[int] = set()

    def overlapped(s: int, e: int) -> bool:
        return not _covered.isdisjoint(range(s, e))

    def consume(s: int, e: int) -> None:
        _covered.update(range(s, e))

    def _in_consumed(pos: int) -> bool:
        return pos in _covered

    def resolve(entry: MappingEntry, key: str) -> str | None:
        if len(entry.texts) == 1:
            return entry.texts[0]
        if policy == "first":
            result.used_first.append(key)
            return entry.texts[0]
        return None  # safe:一对多不猜

    # ① 占位符精确扫描
    for m in PLACEHOLDER_RE.finditer(text):
        key = m.group(0)
        entry = mapping.entries.get(key)
        if entry is None:
            result.unknown.append(key)
            continue
        repl = resolve(entry, key)
        if repl is None:
            result.ambiguous.append({"key": key, "candidates": list(entry.texts)})
            continue
        spans.append((m.start(), m.end(), repl))
        consume(m.start(), m.end())
        hits["placeholder"] += 1

    # ② 化名词/泛化词/括号形态:按形态长度降序做字面扫描(含序号完整命中)。
    # Issue#87 括号免疫:每个括号包裹 key 额外派生裸形态(剥括号后仍命中);
    # 裸形态命中且续接可延长字符(中文数字/数字)= 边界歧义,不猜进 ambiguous。
    forms: list[tuple[str, str, bool]] = []  # (匹配形态, 映射 key, 是否裸形态)
    for k in mapping.entries:
        if not PLACEHOLDER_RE.fullmatch(k):  # ASCII 括号占位符归 pass①,不重扫
            forms.append((k, k, False))
        bare = BRACKETED_KEY_RE.match(k)
        if bare and bare.group(1) not in mapping.entries:
            forms.append((bare.group(1), k, True))
    forms.sort(key=lambda f: len(f[0]), reverse=True)
    for form, key, bare_form in forms:
        entry = mapping.entries[key]
        generalized = _generalized_like(key)
        digit_ended = form[-1].isdigit()
        start = 0
        while True:
            i = text.find(form, start)
            if i == -1:
                break
            j = i + len(form)
            if overlapped(i, j) or (bare_form and i > 0 and text[i - 1] == "["):
                start = j
                continue
            if bare_form and j < len(text) and _BOUNDARY_CONT_RE.match(text[j]):
                # 裸形态后接可延长字符(袁某一二=袁某一+二? 机构_12=机构_1+2?):
                # 括号已失,边界不可判,跳过进 ambiguous——宁可不还原,不可损坏文本
                result.ambiguous.append({"key": key, "candidates": list(entry.texts), "reason": "boundary"})
                hits["skipped_boundary"] += 1
                start = j
                continue
            if not bare_form and (generalized or digit_ended) and j < len(text) and _BOUNDARY_CONT_RE.match(text[j]):
                # 带序号 key 后接数字(某公司12 中的 某公司1)= 部分吞吃,跳过;
                # 该次出现按 unknown 暴露(存在更长的映射外形态)
                # 子串碰撞(如「2023年3月7日」中的「2023年」):跳过进 ambiguous
                if digit_ended:
                    if key not in result.unknown:
                        result.unknown.append(key)
                else:
                    result.ambiguous.append({"key": key, "candidates": list(entry.texts), "reason": "boundary"})
                hits["skipped_boundary"] += 1
                start = j
                continue
            repl = resolve(entry, key)
            if repl is None:
                result.ambiguous.append({"key": key, "candidates": list(entry.texts)})
                start = j
                continue
            spans.append((i, j, repl))
            consume(i, j)
            hits["pseudonym" if not generalized else "generalized"] += 1
            start = j

    # unknown 化名词:只认带数字后缀的合成形态(某公司99);原生「某甲」「某些」非产物,不报
    for m in re.finditer(r"某[\u4e00-\u9fff]{1,6}\d{1,3}", text):
        w = m.group(0)
        if w in mapping.entries:
            continue
        if any(w == k or w in k or k in w for k in mapping.entries):
            continue  # 与某 key 完全相等/互为子串(如「某公司1」是「某公司12」前缀态)不报;
            # 纯前缀重叠(「某公司」⊂「某公司1」且前者非 key)由 I1 数字后界断言负责
        if _in_consumed(text.find(w)):
            continue  # 位于将被替换的区间内(原文形态),非幻觉
        if w not in result.unknown:
            result.unknown.append(w)

    # unknown 括号化名:映射表没有的 [李某] 形态 → 显式暴露(幻觉/缺失,不假装还原)
    for m in BRACKETED_PSEUDONYM_RE.finditer(text):
        w = m.group(0)
        if w in mapping.entries:
            continue
        if _in_consumed(m.start()):
            continue  # 位于将被替换的区间内(原文形态),非幻觉
        if w not in result.unknown:
            result.unknown.append(w)

    # unknown 裸形态(Issue#87):剥括号后形似替换词但映射表没有(裸姓某/裸 X_N token)
    # → 显式暴露。与任一 key(含其裸形态)相等/互为子串的不报(前缀态/部分吞吃由上负责)
    known_forms = {k for k in mapping.entries}
    known_forms |= {b.group(1) for k in mapping.entries if (b := BRACKETED_KEY_RE.match(k))}

    def _unrelated(w: str) -> bool:
        return not any(w == f or w in f or f in w for f in known_forms)

    for m in _BARE_PSEUDONYM_RE.finditer(text):
        w = m.group(0)
        if w in known_forms or not _unrelated(w):
            continue
        if _in_consumed(m.start()):
            continue  # 位于将被替换的区间内(原文形态),非幻觉
        if w not in result.unknown:
            result.unknown.append(w)
    for m in _BARE_PLACEHOLDER_RE.finditer(text):
        w = m.group(0)
        if w in known_forms or not _unrelated(w):
            continue
        if _in_consumed(m.start()):
            continue
        if w not in result.unknown:
            result.unknown.append(w)

    result.restored_text = _replace_spans(text, spans)
    result.restored_count = len(spans)
    result.unknown = sorted(set(result.unknown))
    # ambiguous 按 key 去重,occurrences 计数(评审 M2)
    deduped: list = []
    for a in result.ambiguous:
        for d in deduped:
            if d["key"] == a["key"]:
                d["occurrences"] = d.get("occurrences", 1) + 1
                break
        else:
            deduped.append({**a, "occurrences": 1})
    result.ambiguous = deduped
    result.hits = hits
    return result
