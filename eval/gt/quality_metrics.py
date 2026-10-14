"""质量三数自动化（Issue#105 G5，方案 §10）。

每批产出后自动出三个数，**不做任何模型调用**——输入都是既有产物：

1. **自动消化率**：不经人审即收敛的占比。分层看：上游（字符/语义关）、
   软层（闸门放行）、全链。目标：把 58% 往上推的那把尺子。
2. **Agent 错误率**：抽检样本中 Agent 结论与人工判定不符的比例。
   超档位阈值 → **熔断停批**（宁可停，不可静默跑歪）。
3. **同源一致率**：同模型两次跑（prompt 两版）的结论一致率。
   低 → 任务本身不确定，别急着信 Agent。需要两份跑批产物，缺任一侧
   则标 ``available: False``（如实缺数，不编造 100%）。

输出统一 dict（可 JSON 序列化），``build_quality_report`` 一次出全部并
附熔断判定。
"""
from __future__ import annotations


# ---- 1. 自动消化率 -----------------------------------------------------------

def auto_digest_rate(adjudications: list[dict], upstream_closed: int,
                     total_entities: int) -> dict:
    """三层消化率，分母统一 = 实体总数（不是残留数，否则比率会 >1）。

    ``adjudications``：soft_gate 裁定条目（verdict 字段）；
    ``upstream_closed``：字符/语义归一关停数；``total_entities``：实体总数。
    """
    soft_total = len(adjudications)
    soft_closed = sum(1 for a in adjudications
                      if a.get("verdict") == "auto_closed")
    upstream = upstream_closed / total_entities if total_entities else 0.0
    soft = soft_closed / total_entities if total_entities else 0.0
    overall = ((upstream_closed + soft_closed) / total_entities
               if total_entities else 0.0)
    return {"upstream": upstream, "soft_layer": soft, "overall": overall}


# ---- 2. Agent 错误率 ----------------------------------------------------------

def agent_error_rate(spot_check: list[dict]) -> dict:
    """抽检结果 → 错误率。``spot_check`` 条目带 ``confirmed: bool``。"""
    sampled = len(spot_check)
    errors = sum(1 for s in spot_check if s.get("confirmed") is False)
    return {"sampled": sampled, "errors": errors,
            "error_rate": errors / sampled if sampled else 0.0,
            "available": sampled > 0}


# ---- 3. 同源一致率 --------------------------------------------------------------

def _entity_set(page: dict) -> set[tuple[str, str]]:
    return {(e.get("text", ""), e.get("type", ""))
            for e in (page.get("entities") or [])}


def same_source_agreement(run1: list[dict] | None,
                          run2: list[dict] | None) -> dict:
    """同模型两跑的页级实体集合一致率（Jaccard 平均；双方都有产物才算数）。"""
    if not run1 or not run2:
        return {"available": False, "pages_compared": 0, "agree": 0,
                "agreement": 0.0}
    by_page2 = {p.get("page"): p for p in run2}
    compared = agree = 0
    for p1 in run1:
        p2 = by_page2.get(p1.get("page"))
        if p2 is None:
            continue
        compared += 1
        s1, s2 = _entity_set(p1), _entity_set(p2)
        union = s1 | s2
        if not union:
            agree += 1  # 双方都判空页也是一致
            continue
        if len(s1 & s2) / len(union) == 1.0:
            agree += 1
    return {"available": compared > 0, "pages_compared": compared,
            "agree": agree,
            "agreement": agree / compared if compared else 0.0}


# ---- 熔断 ---------------------------------------------------------------------

def circuit_breaker(error_rate: float, threshold: float,
                    sampled: int | None = None) -> bool:
    """错误率超档位阈值 → 停批。无样本（sampled==0）不熔断（不瞎停）。"""
    if sampled == 0:
        return False
    return error_rate > threshold


# ---- 汇总 ---------------------------------------------------------------------

def build_quality_report(adjudications: list[dict], upstream_closed: int,
                         upstream_total: int, spot_check: list[dict],  # noqa: ARG001 upstream_total=实体总数
                         same_source: tuple | None,
                         thresholds: dict | None = None) -> dict:
    """三数一次出齐 + 熔断判定（report.md 与停批决策的唯一数据源）。"""
    th = {"agent_error": 0.10, **(thresholds or {})}
    digest = auto_digest_rate(adjudications, upstream_closed, upstream_total)
    err = agent_error_rate(spot_check)
    ss = (same_source_agreement(*same_source)
          if same_source else {"available": False, "pages_compared": 0,
                               "agree": 0, "agreement": 0.0})
    tripped = circuit_breaker(err["error_rate"], th["agent_error"],
                              sampled=err["sampled"])
    return {"digest": digest, "agent_error": err, "same_source": ss,
            "circuit_break": {"tripped": tripped,
                              "threshold": th["agent_error"]}}
