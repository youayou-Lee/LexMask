"""三分法闸门 + 宽松度档位（Issue#56 P0，口径见方案 §7 / §8）。

**这个模块是整套设计的安全阀。**

Agent 说「这两处读数是同一个东西的不同写法」——这句话**程序能验**：
把建议的标准形态拿去和两侧归一化结果比、再去异源通道文本里查它是否逐字出现。
验得过才算数，验不过就转人审。于是 Agent 的灵活性进得来，幻觉进不来。

**判据不是"差异大不大"**（§7.1）：身份证差一位的字符串相似度是 94%，与"日期多几个
空格"在相似度上分不开，但前者是值差、后者是形态差。所以判据是**差异的性质**，
而性质由 ``type_classes`` 的**域**决定：

===============  ==========================================  ==================
域               放行规则                                     谁判
===============  ==========================================  ==================
identifier       归一化后必须逐位相等；不等即值差              规则，不用模型
datetime         归一到日期元组；粒度差容忍（§7.3）              规则，不用模型
quantity         归一到 (数值, 单位) 后必须相等，无粒度容忍       规则，不用模型
text/enum        归一化到枚举值后必须相等                      规则，不用模型
text/name        语义等价；**近形字警惕**                      软层分诊 + 证据闸门
text/passage     实体对齐 + 内容重叠率，不按逐字                 软层分诊 + 证据闸门
===============  ==========================================  ==================

**三种输出**：``auto_pass``（自动定案）/ ``needs_soft``（须调软层，流程信号）/
``human_pending``（转人审）。**没有"用模型自信度放行"这条路**——``confidence``
不参与裁决。

**证据闸门**（粒度差与 name 形态差的唯一放行路径）：要求软层给出 ``canonical_form``，
且该形态在 **≥2 个异源通道**的页面文本里逐字出现（多数佐证）。佐证不足 → 落到档位
的 ``unverified`` 策略。**调用方必须只传异源通道**：本地 ``vl-md`` 与云 VL 同为
PaddleOCR-VL 1.6，属同源，传进来会伪造"多数"。

**档位（§8）只改"值差怎么办"，不改口径**：口径类判定（归一化相等、日期粒度、
数值单位）与档位无关；缺读数类（一侧为空 / 无法解析 / 缺 / 看不清）一律人审。
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher

from gt import type_classes
from gt.date_norm import date_equal
from gt.normalize import normalize_text
from gt.value_norm import VALUE_KIND_BY_TYPE, value_equal

#: 宽松度档位（§8）
STRICTNESS_LEVELS = ("S0-trial", "S1-internal", "S2-delivery")

#: 闸门结论（不含"模型自信度"这一路）
VERDICTS = ("auto_pass", "needs_soft", "human_pending")

#: 全部判定情形（配置表必须逐档覆盖；单测断言无缺项）
SITUATIONS = (
    "normalized_equal",
    "one_side_empty",
    "identifier_mismatch",
    "datetime_value_mismatch",
    "datetime_unparsable",
    "datetime_granularity_gap_verified",
    "datetime_granularity_gap_unverified",
    "quantity_mismatch",
    "quantity_unparsable",
    "text_enum_mismatch",
    "text_name_morphological_verified",
    "text_name_morphological_unverified",
    "text_value_mismatch",
    "text_missing",
    "text_unclear",
    "text_passage_equivalent",
    "text_passage_digit_mismatch",
    "text_passage_review",
    "no_soft_available",
)

#: 各档位策略。「值差」类情形逐档收紧；「口径」类情形逐档一致。
_GATE_CONFIG: dict[str, dict] = {
    "S0-trial": {
        "normalized_equal": "pass",
        "one_side_empty": "human",
        "identifier_mismatch": "pass_suspect",
        "datetime_value_mismatch": "pass_suspect",
        "datetime_unparsable": "human",
        "datetime_granularity_gap_verified": "pass",
        "datetime_granularity_gap_unverified": "pass_suspect",
        "quantity_mismatch": "pass_suspect",
        "quantity_unparsable": "human",
        "text_enum_mismatch": "pass_suspect",
        "text_name_morphological_verified": "pass",
        "text_name_morphological_unverified": "pass_suspect",
        "text_value_mismatch": "pass_suspect",
        "text_missing": "human",
        "text_unclear": "human",
        "text_passage_equivalent": "pass",
        "text_passage_digit_mismatch": "human",
        "text_passage_review": "human",
        "no_soft_available": "pass_suspect",
        "passage_overlap_threshold": 0.60,
        "passage_auto_pass": True,
        "emit_suspect_flag": True,
    },
    "S1-internal": {
        "normalized_equal": "pass",
        "one_side_empty": "human",
        "identifier_mismatch": "human",
        "datetime_value_mismatch": "human",
        "datetime_unparsable": "human",
        "datetime_granularity_gap_verified": "pass",
        "datetime_granularity_gap_unverified": "human",
        "quantity_mismatch": "human",
        "quantity_unparsable": "human",
        "text_enum_mismatch": "human",
        "text_name_morphological_verified": "pass",
        "text_name_morphological_unverified": "human",
        "text_value_mismatch": "human",
        "text_missing": "human",
        "text_unclear": "human",
        "text_passage_equivalent": "pass",
        "text_passage_digit_mismatch": "human",
        "text_passage_review": "human",
        "no_soft_available": "needs_soft",
        "passage_overlap_threshold": 0.85,
        "passage_auto_pass": True,
        "emit_suspect_flag": False,
    },
    "S2-delivery": {
        "normalized_equal": "pass",
        "one_side_empty": "human",
        "identifier_mismatch": "human",
        "datetime_value_mismatch": "human",
        "datetime_unparsable": "human",
        "datetime_granularity_gap_verified": "pass",
        "datetime_granularity_gap_unverified": "human",
        "quantity_mismatch": "human",
        "quantity_unparsable": "human",
        "text_enum_mismatch": "human",
        "text_name_morphological_verified": "pass",
        "text_name_morphological_unverified": "human",
        "text_value_mismatch": "human",
        "text_missing": "human",
        "text_unclear": "human",
        "text_passage_equivalent": "pass",
        "text_passage_digit_mismatch": "human",
        "text_passage_review": "human",
        "no_soft_available": "needs_soft",
        # 交付档唯一比 S1 更严之处：模糊重叠**不自动放行**，须人确认
        "passage_overlap_threshold": 0.85,
        "passage_auto_pass": False,
        "emit_suspect_flag": False,
    },
}

#: 佐证所需的最少异源通道数
MIN_CORROBORATING_SOURCES = 2

_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_DIGITS = re.compile(r"\d+")


def gate_config(level: str) -> dict:
    """取某档位策略表的副本（防止调用方就地改全局配置）。"""
    if level not in STRICTNESS_LEVELS:
        raise ValueError(f"未知档位 {level!r}；可选 {STRICTNESS_LEVELS}")
    return dict(_GATE_CONFIG[level])


def _overlap(a: str, b: str) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b, autojunk=False).ratio()


def _digit_runs(s: str) -> tuple[str, ...]:
    """数字串序列——**长描述里的数字守卫**：任何一位数字不同都不得自动放行。

    这条守卫直接来自 §7.1 的教训：长文本整体重叠率高，会把"内嵌身份证号差一位"
    这种致命差异稀释掉。所以把数字单独拎出来比。
    """
    return tuple(_DIGITS.findall(s))


def _diff_regions(a: str, b: str) -> tuple[bool, list[dict]]:
    """返回 ``(差异是否只涉及非汉字, 差异区列表)``。

    长描述（passage）自动放行的前提是**差异区不含任何汉字**——即只有标点、空白、
    编号这类排版差异。一旦有汉字不同，就必须人看：那可能是名字/地名真的不同。
    """
    matcher = SequenceMatcher(None, a, b, autojunk=False)
    regions: list[dict] = []
    cjk_diff = False
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        seg_a, seg_b = a[i1:i2], b[j1:j2]
        if _CJK.search(seg_a) or _CJK.search(seg_b):
            cjk_diff = True
        regions.append({"tag": tag, "a": seg_a, "b": seg_b})
    return (not cjk_diff), regions


def _corroboration(canonical_norm: str, transcripts) -> int:
    """``canonical`` 在多少个**异源**通道文本里逐字出现（同源只计一次）。"""
    if not canonical_norm or not transcripts:
        return 0
    seen: set[str] = set()
    for source_id, text in transcripts:
        if source_id in seen:
            continue
        if canonical_norm in normalize_text(text):
            seen.add(source_id)
    return len(seen)


def _decide(out: dict, cfg: dict, situation: str, reason: str) -> dict:
    action = cfg[situation]
    out["situation"] = situation
    out["reason"] = reason
    if action == "pass":
        out["verdict"] = "auto_pass"
    elif action == "pass_suspect":
        out["verdict"] = "auto_pass"
        out["suspect"] = bool(cfg.get("emit_suspect_flag", False))
    elif action == "needs_soft":
        out["verdict"] = "needs_soft"
    else:
        out["verdict"] = "human_pending"
    return out


def _evidence_gate(out: dict, cfg: dict, soft: dict | None, transcripts,
                   *, situation: str, reason: str) -> dict:
    """证据闸门：软层的 ``canonical_form`` 须获 ≥2 个异源通道佐证。

    佐证不足 → 落到该情形的 ``_verified`` / ``_unverified`` 策略（§7.3：
    GT 生产**不自动放行**粒度差；档位只决定"验不过时是人审还是标 suspect 放行"）。
    """
    canonical = normalize_text((soft or {}).get("canonical_form") or "")
    sources = _corroboration(canonical, transcripts)
    out["evidence"]["evidence_gate"] = {
        "canonical_form": canonical,
        "corroborating_sources": sources,
        "required": MIN_CORROBORATING_SOURCES,
    }
    if canonical and sources >= MIN_CORROBORATING_SOURCES:
        out["soft_verified"] = True
        return _decide(out, cfg, f"{situation}_verified", reason + "；且获异源多数佐证")
    out["soft_verified"] = False
    out["anomaly"] = "soft_unverified"
    return _decide(
        out, cfg, f"{situation}_unverified",
        reason + f"；佐证不足（{sources}/{MIN_CORROBORATING_SOURCES} 异源通道），不自动放行",
    )


def gate_verdict(entity_type: str, text_a: str, text_b: str, soft: dict | None = None,
                 level: str = "S1-internal", transcripts=None) -> dict:
    """对一处两侧读数不一致的实体做闸门裁决。

    参数
        entity_type   实体类型名（**必须** ∈ preset，经 ``type_classes.classify`` 取域）
        text_a/b      两侧通道的原始读数
        soft          软层 B 的结构化输出（§6.3）；``None`` = 未调软层
        level         ∈ STRICTNESS_LEVELS
        transcripts   ``[(异源 source_id, 页面文本), ...]``，仅供证据闸门做多数佐证。
                      **只传异源通道**——同源通道会伪造"多数"。

    返回 dict：``verdict`` / ``situation`` / ``domain`` / ``subdomain`` / ``level`` /
    ``reason`` / ``suspect`` / ``soft_verified`` / ``anomaly`` / ``evidence``。
    """
    if level not in STRICTNESS_LEVELS:
        raise ValueError(f"未知档位 {level!r}；可选 {STRICTNESS_LEVELS}")
    domain, subdomain = type_classes.classify(entity_type)
    cfg = _GATE_CONFIG[level]
    out: dict = {
        "entity_type": entity_type,
        "domain": domain,
        "subdomain": subdomain,
        "level": level,
        "verdict": None,
        "situation": None,
        "reason": "",
        "suspect": False,
        "soft_verified": None,
        "anomaly": None,
        "evidence": {},
    }

    na, nb = normalize_text(text_a), normalize_text(text_b)
    out["evidence"]["normalized"] = {"a": na, "b": nb}

    # ---- 口径层（与档位无关）：归一化后逐字相等即定案，这是 L0.5 归一回一的核心
    if na and na == nb:
        return _decide(out, cfg, "normalized_equal", "归一化后逐字相等")
    if not na or not nb:
        return _decide(out, cfg, "one_side_empty", "一侧读数为空（缺读数，非差异）")

    # ---- identifier：逐位相等，不等即值差，不做任何模糊判断
    if domain == "identifier":
        return _decide(out, cfg, "identifier_mismatch",
                       f"强标识符归一化后不等：{na!r} ≠ {nb!r}")

    # ---- datetime：归一到元组，粒度差容忍但要证据
    if domain == "datetime":
        verdict = date_equal(text_a, text_b)
        out["evidence"]["date"] = verdict
        if verdict["verdict"] == "same":
            return _decide(out, cfg, "normalized_equal", "日期归一后全等")
        if verdict["verdict"] == "same_granularity_gap":
            return _evidence_gate(
                out, cfg, soft, transcripts,
                situation="datetime_granularity_gap",
                reason=(f"日期粒度差（{verdict['gap_direction']}）："
                        f"{render_hint(text_a)} vs {render_hint(text_b)}"),
            )
        if verdict["verdict"] == "different":
            return _decide(out, cfg, "datetime_value_mismatch", verdict["reason"])
        return _decide(out, cfg, "datetime_unparsable", verdict["reason"])

    # ---- quantity：归一到 (数值, 单位)，无粒度容忍
    if domain == "quantity":
        verdict = value_equal(text_a, text_b, kind=VALUE_KIND_BY_TYPE.get(entity_type))
        out["evidence"]["value"] = verdict
        if verdict["verdict"] == "same":
            return _decide(out, cfg, "normalized_equal", "数值归一后相等")
        if verdict["verdict"] == "different":
            return _decide(out, cfg, "quantity_mismatch", verdict["reason"])
        return _decide(out, cfg, "quantity_unparsable", verdict["reason"])

    # ---- text 域
    diff_class = (soft or {}).get("diff_class")

    if subdomain == "enum":
        if diff_class == "missing":
            return _decide(out, cfg, "text_missing", "软层判为缺读数")
        if diff_class == "unclear":
            return _decide(out, cfg, "text_unclear", "软层判为看不清")
        return _decide(out, cfg, "text_enum_mismatch",
                       f"枚举字段归一化后不等：{na!r} ≠ {nb!r}")

    if subdomain == "name":
        if diff_class is None:
            return _decide(out, cfg, "no_soft_available", "text/name 须软层分诊")
        if diff_class == "missing":
            return _decide(out, cfg, "text_missing", "软层判为缺读数")
        if diff_class == "unclear":
            return _decide(out, cfg, "text_unclear", "软层判为看不清")
        if diff_class == "morphological":
            return _evidence_gate(
                out, cfg, soft, transcripts,
                situation="text_name_morphological",
                reason=f"名称形态差待验：{na!r} → {(soft or {}).get('canonical_form')!r}",
            )
        return _decide(out, cfg, "text_value_mismatch",
                       f"软层判为值差（{diff_class}）：{na!r} ≠ {nb!r}")

    # subdomain == "passage"：长描述按重叠率 + 数字守卫
    cjk_free, regions = _diff_regions(na, nb)
    overlap = _overlap(na, nb)
    digits_ok = _digit_runs(na) == _digit_runs(nb)
    out["evidence"]["passage"] = {
        "overlap": round(overlap, 4),
        "threshold": cfg["passage_overlap_threshold"],
        "digit_runs_equal": digits_ok,
        "cjk_free_diff": cjk_free,
        "regions": regions,
    }
    if not digits_ok:
        return _decide(out, cfg, "text_passage_digit_mismatch",
                       "长描述内嵌数字串不同（§7.1 数字守卫）")
    if not cfg["passage_auto_pass"]:
        return _decide(out, cfg, "text_passage_review",
                       "交付档：长描述模糊重叠须人确认")
    if cjk_free and overlap >= cfg["passage_overlap_threshold"]:
        return _decide(out, cfg, "text_passage_equivalent",
                       f"长描述差异仅涉非汉字且重叠 {overlap:.2f} "
                       f"≥ {cfg['passage_overlap_threshold']}")
    return _decide(out, cfg, "text_value_mismatch",
                   f"长描述差异含汉字或重叠不足（{overlap:.2f}）")


def render_hint(text: str) -> str:
    """证据里给人看的读数短形态（避免把整段长文本塞进 reason）。"""
    s = normalize_text(text)
    return s if len(s) <= 24 else s[:24] + "…"


def requires_soft(result: dict) -> bool:
    """该结论是否在要求调用软层（编排器的分支依据）。"""
    return result.get("verdict") == "needs_soft"