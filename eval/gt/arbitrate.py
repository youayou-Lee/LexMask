"""逐页仲裁表 R1-R7（Issue#56 M1 / Task 5）。

GT 管线的裁决核心：消费 Task 4 比对结论（``CompareResult``，**as-is 不重议**）
与三源实体列表，按 spec §3.4 仲裁分支表把每条实体读数归入三桶，产出
``Arbitration`` dict 供 Task 7 组装 pagepack。**纯函数**：无 IO、无时钟、
无随机，同入参必得同输出（桶内条目按判键排序保证确定性）；入参不被修改。

源语义：``ents_a`` = 云 v6 转录面实体、``ents_b`` = 云 VL 转录面实体、
``ents_md`` = 本地 vl-md 实体（``None`` = 未接入，视为 md 沉默）。实体为
Task 3 三通道合并产物（``origin`` 可为 ``conflict``）。

「同读」判键（底稿「三元组」措辞的控制器裁定落地）：以 **(span_normalized,
text) 二元组**对齐——键相同即同读；**type 不入键**，同键异型属类型冲突、
走 R2 冲突路径。三方比对以三键集之差为准：Ka/Kb/Km 定义 独有/确认 读数
（跨侧键比较按底稿设计执行：span 漂移使分歧区两侧读数天然不同键，恰落入
独有读数集合差；跨侧槽位配对不可靠，故逐读数成条、不做槽位配对）。

分支表（互斥，按序判定；verdict 来自 compare，``page_type`` 仅校验枚举、
不参与分支选择）：

- **R6**（最优先）：verdict == ``dispute`` 且争议 kind 含 ``single_side`` /
  ``set_mismatch`` → **不走仲裁整页升级**：全部读数逐条 disputed(R6) + 页级
  gap 记录（candidates.detail 携 compare 争议原文），零采信。compare 争议
  as-is 消费、不重议——T4 评审盲区存档：表格域集合比对**不数重复出现**
  （同 (type,text) 两次 vs 一次在 compare 层读 consistent），本层以集合差
  消费其结论、同键多实例同样折叠，该盲区由抽检（A5）盯防；低密度页
  ``single_side`` 的 detail 区分「单方多字」与「两侧文本不一致」，本层同样
  as-is 消费。
- **R7**：verdict == ``auto_ok_format``（纯格式残差）→ 同读实体
  auto_resolved(rule=R7, source="a")；未对齐读数（残差干扰抽取的异常）
  disputed(R7) 留痕、不静默丢弃。
- **R1/R2**：verdict == ``consistent``。同读且类型一致 → consistent 桶
  （裸实体、取 a 侧实例；R1 无独立标注，桶本身即其留痕）。读数已同但类型
  有争（同侧通道冲突 ``origin == "conflict"``，或跨侧同键异型）→ R2：
  **恰好一个正则通道类型名在场 → 正则胜**，auto_resolved(rule=R2,
  source=胜者侧)——T3 冲突标注把 per-entity 来源抹为 conflict，类型名 ∈
  ``REGEX_CHANNELS`` 键集是唯一可用的通道归属证据；NER 间冲突无正则、或
  ≥2 个正则类型名在场（来源不明）→ disputed(R2)（仍不明 → 升级）。compare
  一致但两侧实体集不合（窗域阈值下的实体级差异）→ disputed(R2)，不引入
  md 仲裁（md 仲裁专属两云分歧路径）。R2 的类型冲突判定适用于一切路径中
  「读数已同」的组（R7/R3-R5 页同享）。
- **R3/R4/R5**：verdict == ``dispute``（其余 kind）。同读键照常入
  consistent/R2；独有读数交 md 仲裁：md 确认 b(VL) 独有读数且不支持 a 独有
  → **R3 采 VL 面**——b 独有读数 auto_resolved(rule=R3, source="b")，a 独
  有读数为被否读数 → disputed(R3) 留痕（A4 无静默丢弃）；对称地 md 确认
  a 独有 → **R4 采 v6 面**（source="a"）；md 同时确认两侧（自相矛盾）、
  对独有读数全沉默（含 md 缺席）、或整页无实体级分歧可解释转录分歧 →
  **R5 disputed**：独有读数逐条 disputed(R5) + 页级 gap 记录（该页转录
  分歧未获实体级仲裁解释）。md 独有读数（Km 有、两云皆无）在任何路径均
  disputed(R5)——无两云佐证的第三方读数升级人工（R6 域内已被整页升级
  吸收，不重复成条）。

输出形状：``{"consistent": [Entity...], "auto_resolved": [{"entity", "rule",
"source"}...], "disputed": [{"entity"|"gap", "rule", "candidates"}...]}``，
rule ∈ R1..R7；``source`` ∈ {"a", "b"} 为 auto_resolved 的采信面标注
（R3→b、R4→a、R2→胜者侧、R7→a）；``candidates`` = {"a"/"b"/"md": 该读数
在各源的实例列表}，gap 条目的 candidates = {"detail": [compare 争议
detail...]}。未知 page_type / verdict → ``ValueError``（fail-fast，与
compare 的页型校验同款）。
"""
from __future__ import annotations

from gt.compare import PAGE_TYPES, CompareResult
from gt.entities import REGEX_CHANNELS, Entity

# R2「正则胜」的通道证据：T3 的 conflict 标注抹掉 per-entity 来源，
# 类型名 ∈ 正则通道键集是唯一可用的通道归属证据
_REGEX_TYPE_NAMES = frozenset(REGEX_CHANNELS)

Arbitration = dict


def _key(e: Entity) -> tuple:
    """「同读」判键：(span_normalized, text) 二元组；type 不入键（异型走 R2）。"""
    return (tuple(e["span_normalized"]), e["text"])


def _group(ents: list[Entity]) -> dict[tuple, list[Entity]]:
    """按判键分组（组内保入参序）；实体浅拷贝入组，入参不被修改。"""
    grouped: dict[tuple, list[Entity]] = {}
    for e in ents:
        grouped.setdefault(_key(e), []).append(dict(e))
    return grouped


def _cands(ga: dict, gb: dict, gm: dict, key: tuple) -> dict:
    """争议条目的候选：该读数在各源（a/b/md）的实例列表。"""
    return {"a": list(ga.get(key, ())), "b": list(gb.get(key, ())),
            "md": list(gm.get(key, ()))}


def _rep(*inst_lists) -> Entity | None:
    """代表实例：a 优先，其次 b、md。"""
    for insts in inst_lists:
        if insts:
            return insts[0]
    return None


def _details(cmp: CompareResult) -> list[str]:
    return [d.get("detail", "") for d in (cmp.get("disputes") or [])]


def _gap_entry(cmp: CompareResult, rule: str, fallback: str) -> dict:
    """页级 gap 记录：gap 值以 compare 争议 detail 为证据（缺失时用兜底文案）。"""
    details = _details(cmp)
    return {"gap": "；".join(d for d in details if d) or fallback,
            "rule": rule, "candidates": {"detail": details}}


def _resolve_agreed(ga: dict, gb: dict, gm: dict, key: tuple) -> tuple[str, object]:
    """「读数已同」组的裁决：R1（类型一致）或 R2（类型冲突：正则胜/仍不明）。

    返回 ``("consistent", entity)`` / ``("auto", entry)`` / ``("disputed", entry)``。
    正则胜的判定：冲突类型中恰一名 ∈ 正则通道键集（其余为非正则类型）→
    该类型实体胜（实例 a 侧优先）；零名或 ≥2 名正则类型名在场 → 仍不明。
    """
    insts_a, insts_b = ga.get(key, []), gb.get(key, [])
    types = {e["type"] for e in insts_a + insts_b}
    if len(types) <= 1:
        return "consistent", _rep(insts_a, insts_b)
    regex_types = types & _REGEX_TYPE_NAMES
    if len(regex_types) == 1:
        winner = next(iter(regex_types))
        for side, insts in (("a", insts_a), ("b", insts_b)):
            for e in insts:
                if e["type"] == winner:
                    return "auto", {"entity": e, "rule": "R2", "source": side}
    return "disputed", {"entity": _rep(insts_a, insts_b), "rule": "R2",
                        "candidates": _cands(ga, gb, gm, key)}


def _resolve_agreed_into(ga: dict, gb: dict, gm: dict, key: tuple,
                         consistent: list, auto: list, disputed: list) -> None:
    """把同读组的裁决分发进三桶（R1 直入；R2 两臂按裁决入 auto/disputed）。"""
    outcome, payload = _resolve_agreed(ga, gb, gm, key)
    if outcome == "consistent":
        consistent.append(payload)
    elif outcome == "auto":
        auto.append(payload)
    else:
        disputed.append(payload)


def _arbitrate_r6(ga: dict, gb: dict, gm: dict, cmp: CompareResult) -> Arbitration:
    """R6：单方多字/集合不合 → 不走仲裁整页升级（页级 gap + 全读数逐条 disputed）。"""
    disputed = [_gap_entry(cmp, "R6", "单方多字/集合不合，整页升级（R6）")]
    for key in sorted(set(ga) | set(gb) | set(gm)):
        disputed.append({"entity": _rep(ga.get(key), gb.get(key), gm.get(key)),
                         "rule": "R6", "candidates": _cands(ga, gb, gm, key)})
    return {"consistent": [], "auto_resolved": [], "disputed": disputed}


def _arbitrate_r7(ga: dict, gb: dict, gm: dict) -> Arbitration:
    """R7：纯格式差 → 同读实体自动放行；未对齐读数升级（不静默丢弃）。"""
    ka, kb, km = set(ga), set(gb), set(gm)
    auto: list = []
    disputed: list = []
    for key in sorted(ka & kb):
        outcome, payload = _resolve_agreed(ga, gb, gm, key)
        if outcome == "consistent":
            auto.append({"entity": payload, "rule": "R7", "source": "a"})
        elif outcome == "auto":
            auto.append(payload)  # 同读组上的类型冲突已由 R2 裁决
        else:
            disputed.append(payload)
    for key in sorted((ka - kb) | (kb - ka)):  # 残差干扰抽取的未对齐读数
        disputed.append({"entity": _rep(ga.get(key), gb.get(key)),
                         "rule": "R7", "candidates": _cands(ga, gb, gm, key)})
    for key in sorted(km - ka - kb):  # md 独有读数：升级
        disputed.append({"entity": gm[key][0], "rule": "R5",
                         "candidates": _cands(ga, gb, gm, key)})
    return {"consistent": [], "auto_resolved": auto, "disputed": disputed}


def _arbitrate_r1_r2(ga: dict, gb: dict, gm: dict) -> Arbitration:
    """R1/R2：两云一致——同读同型入一致集；类型冲突/跨侧读数不合走 R2。"""
    ka, kb, km = set(ga), set(gb), set(gm)
    consistent: list = []
    auto: list = []
    disputed: list = []
    for key in sorted(ka & kb):
        _resolve_agreed_into(ga, gb, gm, key, consistent, auto, disputed)
    for key in sorted((ka - kb) | (kb - ka)):  # 一致页上的实体级读数差（窗域阈值之下）
        disputed.append({"entity": _rep(ga.get(key), gb.get(key)),
                         "rule": "R2", "candidates": _cands(ga, gb, gm, key)})
    for key in sorted(km - ka - kb):  # md 独有读数：升级
        disputed.append({"entity": gm[key][0], "rule": "R5",
                         "candidates": _cands(ga, gb, gm, key)})
    return {"consistent": consistent, "auto_resolved": auto, "disputed": disputed}


def _arbitrate_r3_r4_r5(ga: dict, gb: dict, gm: dict, cmp: CompareResult) -> Arbitration:
    """R3/R4/R5：两云分歧（非 R6 kind）——md 佐证单方则整页采信面，否则升级。

    - md 确认 b(VL) 独有读数且不支持 a 独有 → R3 采 VL（b 独有 auto，a 独有被否留痕）；
    - 对称地 md 确认 a 独有 → R4 采 v6；
    - md 同时佐证两侧 / 全沉默（含缺席）/ 无实体级分歧可解释 → R5 disputed
      （独有读数逐条 + 页级 gap）。
    """
    ka, kb, km = set(ga), set(gb), set(gm)
    consistent: list = []
    auto: list = []
    disputed: list = []
    for key in sorted(ka & kb):
        _resolve_agreed_into(ga, gb, gm, key, consistent, auto, disputed)
    a_only, b_only = ka - kb, kb - ka
    confirmed_a, confirmed_b = a_only & km, b_only & km
    if confirmed_a and confirmed_b:  # md 自相矛盾：三方各执
        for key in sorted(a_only | b_only):
            disputed.append({"entity": _rep(ga.get(key), gb.get(key)),
                             "rule": "R5", "candidates": _cands(ga, gb, gm, key)})
        disputed.insert(0, _gap_entry(cmp, "R5",
                                      "两云转录分歧未获实体级仲裁解释，整页升级（R5）"))
    elif confirmed_b:  # md 与 b(VL) 同读 → 采 VL 面
        for key in sorted(b_only):
            auto.append({"entity": gb[key][0], "rule": "R3", "source": "b"})
        for key in sorted(a_only):  # 被否读数留痕（A4 无静默丢弃）
            disputed.append({"entity": ga[key][0], "rule": "R3",
                             "candidates": _cands(ga, gb, gm, key)})
    elif confirmed_a:  # md 与 a(v6) 同读 → 采 v6 面
        for key in sorted(a_only):
            auto.append({"entity": ga[key][0], "rule": "R4", "source": "a"})
        for key in sorted(b_only):  # 被否读数留痕
            disputed.append({"entity": gb[key][0], "rule": "R4",
                             "candidates": _cands(ga, gb, gm, key)})
    else:  # md 沉默/缺席，或无独有读数（转录分歧未获实体级解释）
        for key in sorted(a_only | b_only):
            disputed.append({"entity": _rep(ga.get(key), gb.get(key)),
                             "rule": "R5", "candidates": _cands(ga, gb, gm, key)})
        disputed.insert(0, _gap_entry(cmp, "R5",
                                      "两云转录分歧未获实体级仲裁解释，整页升级（R5）"))
    for key in sorted(km - ka - kb):  # md 独有读数：升级
        disputed.append({"entity": gm[key][0], "rule": "R5",
                         "candidates": _cands(ga, gb, gm, key)})
    return {"consistent": consistent, "auto_resolved": auto, "disputed": disputed}


def arbitrate_page(cmp: CompareResult, ents_a: list[Entity], ents_b: list[Entity],
                   ents_md: list[Entity] | None, page_type: str) -> Arbitration:
    """按仲裁表 R1-R7 裁决一页的三源实体读数（纯函数，入参不被修改）。

    ``cmp`` 为 Task 4 ``compare_transcripts`` 产物（as-is 消费）；``ents_*``
    为 Task 3 合并产物；``ents_md`` 为 ``None`` 时视为 md 沉默。分支判定见
    模块 docstring；未知 page_type / verdict raise ``ValueError``。
    """
    if page_type not in PAGE_TYPES:
        raise ValueError(f"未知页型 {page_type!r}，有效页型：{PAGE_TYPES}")
    verdict = cmp["verdict"]
    ga, gb = _group(ents_a or []), _group(ents_b or [])
    gm = _group(ents_md or [])
    kinds = {d.get("kind") for d in (cmp.get("disputes") or [])}

    if verdict == "dispute" and kinds & {"single_side", "set_mismatch"}:
        return _arbitrate_r6(ga, gb, gm, cmp)
    if verdict == "auto_ok_format":
        return _arbitrate_r7(ga, gb, gm)
    if verdict == "consistent":
        return _arbitrate_r1_r2(ga, gb, gm)
    if verdict == "dispute":
        return _arbitrate_r3_r4_r5(ga, gb, gm, cmp)
    raise ValueError(f"未知 compare verdict {verdict!r}")
