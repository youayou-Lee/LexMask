"""实体抽取与合并（Issue#56 M1 / Task 3）。

GT 管线实体层：在归一化文本（norm 面）上抽实体，命中区间经 FaceMap
双面回填原文 span。三通道：

- extract_regex    REGEX_CHANNELS 五类正则（身份证号/电话/银行卡号/案号/车牌号），
                   于 norm 面 ``re.finditer``；跨通道重叠按「先长后短、特定类型
                   优先」占位去重（银行卡号/身份证排除电话前缀重叠）；
- extract_ner      NERClient 协议（与 backend NER 端点响应同形：类型中文名 →
                   实体串列表）；实体串在 norm 面定位，每次出现各计一条，
                   定位不到则跳过；``NEROff`` 为关闭态（恒空）;
- merge_entities   同面同 span 同型 → origin 合并 ``regex+ner``；同 span 类型
                   冲突 → 两条均保留并标 ``conflict``（交 Task 5 仲裁）。

Entity dict 形状（Task 5/7 消费）：
``{"text", "type", "span_original", "span_normalized", "origin"}``，
``origin ∈ {"regex", "ner", "regex+ner", "conflict"}``。

通道键为 preset 中文名（backend/config/preset_entity_types.json 单一事实源）；
测试以 ``common_api.TYPE_ID_TO_NAME`` 值集动态断言键集合 ⊆ preset 名集。

正则模式为计划底稿逐字值，唯一例外：银行卡号底稿写作 ``\\d{15,19}``
（前后非数字）——括号约束以 lookaround 落实为 ``(?<![0-9])\\d{15,19}(?![0-9])``。
电话正则底稿无边界约束、逐字保留（长数字串内部可能命中电话，占位去重
只对更长的身份证/银行卡生效，残余误报交仲裁）。
"""
from __future__ import annotations

import re
from typing import Protocol

from gt.normalize import FaceMap

# 计划底稿逐字正则（键 = preset 中文名；银行卡号按底稿括号约束加数字边界 lookaround）
REGEX_CHANNELS: dict[str, str] = {
    "身份证号": r"\d{6}(18|19|20)\d{2}(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])\d{3}[\dXx]",
    "电话": r"(1[3-9]\d{9})|(0\d{2,3}-?\d{7,8})",
    "银行卡号": r"(?<![0-9])\d{15,19}(?![0-9])",
    "案号": r"[（(]\s*\d{4}\s*[）)][^，。；、\s（）()]{1,20}号",
    "车牌号": r"[京津沪渝冀晋蒙辽吉黑苏浙皖闽赣鲁豫鄂湘粤桂琼川贵云藏陕甘青宁新使领][A-HJ-NP-Z][A-HJ-NP-Z0-9]{4,6}",
}

_COMPILED = {name: re.compile(pattern) for name, pattern in REGEX_CHANNELS.items()}

# 特定类型优先序（同长 span 多通道命中时先者胜）：
# 身份证号（结构化：地区码+生日+校验位）> 银行卡号（泛数字长串）> 电话（可作前缀嵌入）；
# 案号/车牌号与数字通道 span 不相交，序值仅作同长平手决断。
_SPECIFICITY = {"身份证号": 0, "银行卡号": 1, "电话": 2, "案号": 3, "车牌号": 4}

Entity = dict


class NERClient(Protocol):
    """backend NER 端点同形协议：norm 面文本进，类型中文名 → 实体串列表出。"""

    def ner(self, text: str) -> dict[str, list[str]]: ...


class NEROff:
    """NER 关闭态：恒返回空结果（等价于无 NER 通道）。"""

    def ner(self, text: str) -> dict[str, list[str]]:
        return {}


def _make_entity(text: str, etype: str, n0: int, n1: int,
                 face_map: FaceMap, origin: str) -> Entity:
    o0, o1 = face_map.to_original(n0, n1)
    return {
        "text": text,
        "type": etype,
        "span_original": [o0, o1],
        "span_normalized": [n0, n1],
        "origin": origin,
    }


def extract_regex(text: str, face_map: FaceMap) -> list[Entity]:
    """五类正则通道于 norm 面抽取，跨通道重叠按先长后短、特定类型优先占位去重。

    ``text`` 应为 ``face_map.norm``（与检测同面）；命中区间经
    ``face_map.to_original`` 回填原文 span。结果按 norm 面起点有序。
    """
    candidates: list[tuple[int, int, str]] = []
    for name, compiled in _COMPILED.items():
        for m in compiled.finditer(text):
            if m.end() > m.start():  # 防零宽匹配占位
                candidates.append((m.start(), m.end(), name))
    # 先长后短 → 特定类型优先 → 起点，贪心占位：与已占位区间重叠者弃
    candidates.sort(key=lambda c: (-(c[1] - c[0]), _SPECIFICITY.get(c[2], len(_SPECIFICITY)), c[0]))
    accepted: list[tuple[int, int, str]] = []
    for s, e, name in candidates:
        if any(s < a_e and a_s < e for a_s, a_e, _ in accepted):
            continue
        accepted.append((s, e, name))
    accepted.sort(key=lambda c: (c[0], c[1]))
    return [_make_entity(text[s:e], name, s, e, face_map, "regex")
            for s, e, name in accepted]


def extract_ner(text: str, ner: NERClient | None, face_map: FaceMap) -> list[Entity]:
    """NER 通道：实体串在 norm 面定位（逐次出现各计一条），定位不到则跳过。

    ``text`` 应为 ``face_map.norm``，并原样喂给 ``ner.ner``；``ner`` 为
    ``None`` 或关闭态（``NEROff``）时返回空列表。结果按 norm 面起点有序。
    """
    if ner is None:
        return []
    results = ner.ner(text) or {}
    entities: list[Entity] = []
    seen: set[tuple[int, int, str]] = set()
    for etype, strings in results.items():
        for s in strings or []:
            if not s:
                continue
            start = text.find(s)
            while start != -1:
                n0, n1 = start, start + len(s)
                key = (n0, n1, etype)
                if key not in seen:
                    seen.add(key)
                    entities.append(_make_entity(s, etype, n0, n1, face_map, "ner"))
                start = text.find(s, start + 1)
    entities.sort(key=lambda e: (e["span_normalized"][0], e["span_normalized"][1], e["type"]))
    return entities


def _span_key(e: Entity) -> tuple:
    """同面判键：原文 span 与 norm span 两者一致才算同 span。"""
    return (tuple(e["span_original"]), tuple(e["span_normalized"]))


def _merge_origin(x: str, y: str) -> str:
    if x == y:
        return x
    if "conflict" in (x, y):
        return "conflict"
    return "regex+ner"  # regex × ner 任意组合（含已是 regex+ner）


def merge_entities(a: list[Entity], b: list[Entity]) -> list[Entity]:
    """双通道实体合并（Task 5/7 消费口）。

    - 同面同 span 同型：合并为一条，``origin`` 按 regex/ner 归并 ``regex+ner``；
    - 同 span 类型冲突：两条均保留，``origin`` 标 ``conflict``（交仲裁）；
    - 未命中：原样保留（通常 a=regex 侧、b=ner 侧，方向不敏感）。

    结果按位置（span_original 起点）稳定排序；入参不被修改。
    """
    merged: list[Entity] = [dict(e) for e in a]
    index: dict[tuple, list[int]] = {}
    for i, e in enumerate(merged):
        index.setdefault(_span_key(e), []).append(i)
    for ent in b:
        hits = index.get(_span_key(ent), [])
        if not hits:
            merged.append(dict(ent))
            index.setdefault(_span_key(ent), []).append(len(merged) - 1)
            continue
        if all(merged[i]["type"] == ent["type"] for i in hits):
            i = hits[0]
            merged[i]["origin"] = _merge_origin(merged[i]["origin"], ent["origin"])
            continue
        for i in hits:  # 类型冲突：两条均保留并标 conflict
            merged[i]["origin"] = "conflict"
        conflict = dict(ent)
        conflict["origin"] = "conflict"
        merged.append(conflict)
        index.setdefault(_span_key(conflict), []).append(len(merged) - 1)
    merged.sort(key=lambda e: (e["span_original"][0], e["span_original"][1],
                               e["span_normalized"][0], e["type"], e["text"]))
    return merged
