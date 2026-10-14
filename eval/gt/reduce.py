"""L0.5 归一回一（Issue#56 P1，方案 §5 Step 5）。

现状免审档是**字符级 exact**，全半角/空格之外的写法差全部白进人眼，免审率
卡在 58% 到顶。本模块在字符归一之上补**语义归一**（date_norm / value_norm），
把"归一后其实相等"的假分歧在进软层（Agent）之前就关掉——

- 关闭 = ``normalized_closed``，**不调任何模型**，纯函数可重放（AC2 幂等）；
- 关不掉的（值差 / 粒度差 / 解析不了 / 类型漂移）→ ``disputed``，留给
  Step 6 软层与 Step 7 闸门，本模块**不做裁决**（铁规 2：软层之外不做裁决，
  本模块是硬层，只吃确定性判等）；
- 归一抛异常 → 该条转 ``disputed`` 且 reason 带异常类名（**不吞异常**），
  批内其余条目继续。

**故意不关的两种差**（宁可多进人眼，不可静默放行真错）：

- ``datetime`` 粒度差（``2023年`` vs ``2023年6月15日``）——判等口径里算同一
  实体，但 GT 生产场景可能是 OCR 截断或凭空补全，必须过证据闸门（§7.3）；
- 分域表未登记的类型（如「机构名」——xbg-clean2 的 118 条漂移同款）——
  保守不关，reason 可见可计数（G3 缺口的运行时探针）。
"""
from __future__ import annotations

from gt.date_norm import date_equal
from gt.normalize import normalize_text
from gt.type_classes import classify
from gt.value_norm import VALUE_KIND_BY_TYPE, value_equal

VERDICTS = ("normalized_closed", "disputed")


# ---- 逐对语义判等原语（gate 复用同一把尺子） ---------------------------------

def semantic_equal(entity_type: str, a: str, b: str) -> dict:
    """单类型下的两侧读数判等。返回 ``{matched, method, reason, detail}``。

    - ``text``（enum/name/passage）/ ``identifier``：``normalize_text`` 逐位相等；
    - ``datetime``：``date_equal``，仅 ``same``（严格）关闭，粒度差**不关**；
    - ``quantity``：``value_equal``（金额 kind=amount，无粒度容忍）。
    未登记类型 raise ``KeyError``（调用方决定如何保守处理）。
    """
    domain, _sub = classify(entity_type)
    if domain in ("text", "identifier"):
        matched = normalize_text(a) == normalize_text(b)
        return {"matched": matched, "method": "normalize",
                "reason": "" if matched else "归一化后仍不等（逐位比较）",
                "detail": {"domain": domain}}
    if domain == "datetime":
        v = date_equal(a, b)
        if v["verdict"] == "same":
            return {"matched": True, "method": "date_norm", "reason": "",
                    "detail": {"domain": domain, "verdict": v["verdict"]}}
        if v["verdict"] == "same_granularity_gap":
            return {"matched": False, "method": "date_norm",
                    "reason": ("粒度差不自动关（可能是 OCR 截断或凭空补全，"
                               "须过证据闸门）"),
                    "detail": {"domain": domain, "verdict": v["verdict"],
                               "gap_direction": v["gap_direction"]}}
        return {"matched": False, "method": "date_norm",
                "reason": f"日期判不等：{v['reason']}",
                "detail": {"domain": domain, "verdict": v["verdict"]}}
    # quantity
    kind = VALUE_KIND_BY_TYPE.get(entity_type, "quantity")
    v = value_equal(a, b, kind=kind)
    if v["verdict"] == "same":
        return {"matched": True, "method": "value_norm", "reason": "",
                "detail": {"domain": domain, "kind": kind}}
    return {"matched": False, "method": "value_norm",
            "reason": f"数值判不等：{v['reason']}",
            "detail": {"domain": domain, "kind": kind, "verdict": v["verdict"]}}


# ---- 单条归一回一 ------------------------------------------------------------

def reduce_dispute(dispute: dict) -> dict:
    """对一条争议做归一回一。

    返回 outcome dict：``{dispute_id, verdict, method, canonical, reason, detail}``。
    ``canonical`` 取命中对的 a 侧原文（关闭后供下游写回的原始形态）。

    异常路径：任何一侧归一抛异常 → ``verdict="disputed"``、reason 带
    ``归一化异常 <ExcName>``——不吞异常，也不断送整批。
    """
    did = dispute["dispute_id"]
    base = {"dispute_id": did, "method": None, "canonical": None,
            "reason": "", "detail": {}}
    cand = dispute.get("candidates") or {}
    side_a = cand.get("a") or []
    side_b = cand.get("b") or []
    if not side_a or not side_b:
        return {**base, "verdict": "disputed",
                "reason": "候选为空（缺一侧读数），留给软层判 missing"}

    try:
        classify(dispute["entity_type"])
    except KeyError:
        return {**base, "verdict": "disputed",
                "reason": f"实体类型 {dispute.get('entity_type')!r} 未登记分域，保守不关",
                "detail": {"unregistered_type": dispute.get("entity_type")}}
    try:
        for x in side_a:
            for y in side_b:
                r = semantic_equal(dispute["entity_type"], x, y)
                if r["matched"]:
                    return {**base, "verdict": "normalized_closed",
                            "method": r["method"], "canonical": x,
                            "detail": r["detail"]}
        # 逐对跑完无一命中 → 拿第一对的 reason 做代表（确定性）
        rep = semantic_equal(dispute["entity_type"], side_a[0], side_b[0])
        return {**base, "verdict": "disputed",
                "reason": rep["reason"], "detail": rep["detail"]}
    except Exception as e:  # noqa: BLE001 —— 按 §5 Step 5：转 disputed 不吞异常
        return {**base, "verdict": "disputed",
                "reason": f"归一化异常 {type(e).__name__}: {e}",
                "detail": {}}


def reduce_disputes(disputes: list[dict],
                    queue=None) -> list[dict]:
    """批归一回一（保持输入顺序，确定性）。

    ``queue``（可选 ``gt.dispute.DisputeQueue``）：关闭的条目自动迁移到
    ``normalized_closed``；已是终态/非 pending 的条目跳过迁移（幂等重放安全），
    disputed 保持 ``pending``（留给软层）。
    """
    outcomes = []
    for d in disputes:
        out = reduce_dispute(d)
        outcomes.append(out)
        if queue is not None and out["verdict"] == "normalized_closed":
            item = queue.get(d["dispute_id"])
            if item["status"] == "pending":
                queue.transition(d["dispute_id"], "normalized_closed")
    return outcomes


# ---- 基线与报告（免审率对比出数） ---------------------------------------------

def baseline_closed(dispute: dict) -> bool:
    """字符级 exact 基线（现状免审判据）：normalize 后两侧存在逐位相等的对。"""
    cand = dispute.get("candidates") or {}
    a_norm = {normalize_text(x) for x in (cand.get("a") or []) if isinstance(x, str)}
    b_norm = {normalize_text(y) for y in (cand.get("b") or []) if isinstance(y, str)}
    return bool(a_norm & b_norm)


def reduce_report(disputes: list[dict]) -> dict:
    """同一批争议跑两种尺子，出免审率对比（report.md / 排期引用的数据源）。"""
    outcomes = reduce_disputes(list(disputes))
    total = len(disputes)
    closed = sum(1 for o in outcomes if o["verdict"] == "normalized_closed")
    baseline = sum(1 for d in disputes if baseline_closed(d))
    by_method: dict[str, int] = {}
    for o in outcomes:
        if o["verdict"] == "normalized_closed":
            by_method[o["method"]] = by_method.get(o["method"], 0) + 1
    return {
        "total": total,
        "semantic_closed": closed,
        "disputed": total - closed,
        "semantic_closed_ratio": closed / total if total else 0.0,
        "baseline_closed": baseline,
        "baseline_closed_ratio": baseline / total if total else 0.0,
        "closed_by_method": by_method,
    }
