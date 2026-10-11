"""T3 视觉检测对比 benchmark — schema、枚举、预注册阈值与判定函数。

本模块评测脱敏流水线的 **T3 定位**（框级视觉检测）：在页面图像上框出
公章（seal）与手写签名（signature），四臂对比：
  la / mineru_seal / paddlex_seal / sigdet（见 adapters.py）。

是 `eval/benchmarks/t2/`（文本级 NER）的框级姊妹版；选型工具，不做闸门。

## 统一数据契约（明细 jsonl 与 GT jsonl 同构，坐标一律页像素、左上原点）

```json
{"page_id": "<卷名>#p<N>", "arm": "la|mineru_seal|paddlex_seal|sigdet|gt",
 "kind": "seal|signature", "box": {"x":0,"y":0,"w":0,"h":0},
 "confidence": 1.0, "raw": ""}
```

- `box` 键名是 `x/y/w/h`（页像素整数或浮点）。
- `confidence`：有真实分数则填，无分数的臂（MinerU 块无分数、GT）填 1.0。
- `raw`：来源臂的原始输出片段（调试用），GT 填 ""。
- benchmark 明细行在契约字段之外追加 `status`（ok|failed）、`elapsed_ms`、
  `error`（仅 failed）三个评测字段；契约字段本身不变。

## 预注册阈值（写代码前先定，防事后挑阈值）

- COLLAPSE_DROP_PP = 15：某桶召回比该臂整体召回低 ≥15 个百分点 → 桶塌方。
- FALSE_POS_RATIO_CAP = 2.0：**候选臂误报/页 ≤ 2 × LA 臂误报/页**（与 LA
  基线臂逐页对比、按页均 FP）；LA 误报/页 = 0 时退化为绝对上限
  FP_ABSOLUTE_CAP_PER_PAGE = 0.5/页（0.5 × 0 = 0 过严，等价放宽一句：候选臂
  误报/页 ≤ 0.5 判过）。突破 → 误报上界。
- ARM_FAIL_PAGE_RATIO = 0.10：失败页占比 > 10% → 该臂无效。
- 匹配：IoU ≥ 0.5 贪心（预测按 confidence 降序，GT 一经命中即占用）。
"""

from __future__ import annotations

from typing import Any

# ---- 枚举 ----------------------------------------------------------------

ARMS = ("la", "mineru_seal", "paddlex_seal", "sigdet")
GT_ARM = "gt"
KINDS = ("seal", "signature")

# ---- 预注册阈值 ----------------------------------------------------------

COLLAPSE_DROP_PP = 15.0
FALSE_POS_RATIO_CAP = 2.0
# LA 基线误报/页 = 0 时的绝对上限（见 docstring 预注册段）
FP_ABSOLUTE_CAP_PER_PAGE = 0.5
ARM_FAIL_PAGE_RATIO = 0.10
IOU_MATCH_THRESHOLD = 0.5

# ---- schema --------------------------------------------------------------

RECORD_KEYS = ("page_id", "arm", "kind", "box", "confidence", "raw")
BOX_KEYS = ("x", "y", "w", "h")

MANIFEST_ITEM_KEYS = ("page_id", "volume", "page", "buckets", "has_seal", "has_signature")
MANIFEST_BUCKETS = ("seal_clean", "seal_hard", "sig_hard")


def _box_error(box: Any) -> str | None:
    if not isinstance(box, dict):
        return "box 必须是对象"
    for key in BOX_KEYS:
        if key not in box:
            return f"box 缺少键 {key}"
        value = box[key]
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return f"box.{key} 必须是数值"
        if value < 0:
            return f"box.{key} 不能为负"
    if box["w"] <= 0 or box["h"] <= 0:
        return "box.w/box.h 必须为正"
    return None


def validate_record(record: Any, *, require_arm: bool = True) -> list[str]:
    """校验一条契约记录，返回错误列表（空 = 合法）。"""
    if not isinstance(record, dict):
        return ["记录必须是 JSON 对象"]
    errors: list[str] = []
    for key in RECORD_KEYS:
        if key not in record:
            errors.append(f"缺少键 {key}")
    if errors:
        return errors
    if require_arm:
        arm = record["arm"]
        if arm not in ARMS and arm != GT_ARM:
            errors.append(f"arm 非法: {arm!r}（允许 {ARMS} 或 {GT_ARM!r}）")
    if record["kind"] not in KINDS:
        errors.append(f"kind 非法: {record['kind']!r}（允许 {KINDS}）")
    box_err = _box_error(record["box"])
    if box_err:
        errors.append(box_err)
    conf = record["confidence"]
    if not isinstance(conf, (int, float)) or isinstance(conf, bool) or not 0.0 <= conf <= 1.0:
        errors.append("confidence 必须在 [0,1]")
    if not isinstance(record["page_id"], str) or not record["page_id"]:
        errors.append("page_id 必须是非空字符串")
    if not isinstance(record["raw"], str):
        errors.append("raw 必须是字符串")
    return errors


def validate_manifest(manifest: Any) -> list[str]:
    """校验 T0 manifest（数组，每项见 MANIFEST_ITEM_KEYS）。"""
    if not isinstance(manifest, list):
        return ["manifest 必须是 JSON 数组"]
    errors: list[str] = []
    seen: set[str] = set()
    for idx, item in enumerate(manifest):
        prefix = f"manifest[{idx}]"
        if not isinstance(item, dict):
            errors.append(f"{prefix}: 必须是对象")
            continue
        for key in MANIFEST_ITEM_KEYS:
            if key not in item:
                errors.append(f"{prefix}: 缺少键 {key}")
        page_id = item.get("page_id")
        if not isinstance(page_id, str) or not page_id:
            errors.append(f"{prefix}: page_id 必须是非空字符串")
        elif page_id in seen:
            errors.append(f"{prefix}: page_id 重复: {page_id!r}")
        else:
            seen.add(page_id)
        buckets = item.get("buckets")
        if not isinstance(buckets, list) or not all(b in MANIFEST_BUCKETS for b in buckets):
            errors.append(f"{prefix}: buckets 必须是 {MANIFEST_BUCKETS} 的子集")
        if not isinstance(item.get("page"), int) or item.get("page", 0) < 1:
            errors.append(f"{prefix}: page 必须是正整数")
        for flag in ("has_seal", "has_signature"):
            if not isinstance(item.get(flag), bool):
                errors.append(f"{prefix}: {flag} 必须是布尔")
    return errors


# ---- 几何 ----------------------------------------------------------------


def iou(a: dict[str, float], b: dict[str, float]) -> float:
    """两框（x/y/w/h，同坐标系）的交并比；退化框并集为 0 时返回 0。"""
    ax2, ay2 = a["x"] + a["w"], a["y"] + a["h"]
    bx2, by2 = b["x"] + b["w"], b["y"] + b["h"]
    ix1, iy1 = max(a["x"], b["x"]), max(a["y"], b["y"])
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    inter = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
    union = a["w"] * a["h"] + b["w"] * b["h"] - inter
    return inter / union if union > 0 else 0.0


def greedy_match(
    predictions: list[dict[str, Any]],
    ground_truths: list[dict[str, Any]],
    threshold: float = IOU_MATCH_THRESHOLD,
) -> tuple[list[tuple[int, int]], list[int], list[int]]:
    """贪心匹配：预测按 confidence 降序，逐个找未占用 GT 中 IoU 最高者。

    返回 (matched[(pred_idx, gt_idx)], unmatched_pred_idx, unmatched_gt_idx)。
    confidence 缺失按 1.0 处理（MinerU/GT 无分数）。
    """
    pred_order = sorted(
        range(len(predictions)),
        key=lambda i: -float(predictions[i].get("confidence", 1.0) or 0.0),
    )
    claimed: set[int] = set()
    matched: list[tuple[int, int]] = []
    for pi in pred_order:
        best_j, best_iou = -1, threshold
        for gj in range(len(ground_truths)):
            if gj in claimed:
                continue
            value = iou(predictions[pi]["box"], ground_truths[gj]["box"])
            if value >= best_iou:
                best_j, best_iou = gj, value
        if best_j >= 0:
            claimed.add(best_j)
            matched.append((pi, best_j))
    matched_pred = {pi for pi, _ in matched}
    matched_gt = {gj for _, gj in matched}
    unmatched_pred = [i for i in range(len(predictions)) if i not in matched_pred]
    unmatched_gt = [j for j in range(len(ground_truths)) if j not in matched_gt]
    return matched, unmatched_pred, unmatched_gt


# ---- 判定函数（阈值全部预注册于本文件顶部） -------------------------------


def is_bucket_collapsed(bucket_recall_pct: float, arm_overall_recall_pct: float) -> bool:
    """桶塌方：该桶召回比臂整体召回低 ≥ COLLAPSE_DROP_PP 个百分点。"""
    return (arm_overall_recall_pct - bucket_recall_pct) >= COLLAPSE_DROP_PP


def fp_per_page(fp: int, pages: int) -> float:
    """按页均 FP；pages=0（没跑成任何页）返回 0.0（臂无效判定另有兜底）。"""
    return fp / pages if pages > 0 else 0.0


def fp_cap_per_page(la_fp: int, la_pages: int) -> float:
    """候选臂的误报上界（每页）：2 × LA 误报/页；LA 基线为 0 时绝对上限 0.5。"""
    la_rate = fp_per_page(la_fp, la_pages)
    return FALSE_POS_RATIO_CAP * la_rate if la_rate > 0 else FP_ABSOLUTE_CAP_PER_PAGE


def is_fp_excessive(
    candidate_fp: int, candidate_pages: int, la_fp: int, la_pages: int
) -> bool:
    """误报上界：候选臂误报/页 > 上界（= 2 × LA 臂误报/页）。

    LA 误报/页 = 0 时上界退化为绝对上限 FP_ABSOLUTE_CAP_PER_PAGE = 0.5/页
    （见 docstring 预注册段）。candidate_pages=0 → 误报/页按 0 计（页缺失由
    is_arm_invalid 兜底）。
    """
    return fp_per_page(candidate_fp, candidate_pages) > fp_cap_per_page(la_fp, la_pages)


def is_arm_invalid(failed_pages: int, total_pages: int) -> bool:
    """臂无效：失败页占比 > ARM_FAIL_PAGE_RATIO（total=0 视为无效——没跑成任何页）。"""
    if total_pages <= 0:
        return True
    return failed_pages / total_pages > ARM_FAIL_PAGE_RATIO


# ---- 计分 ----------------------------------------------------------------


def score_arm_kind(
    predictions: list[dict[str, Any]],
    ground_truths: list[dict[str, Any]],
) -> dict[str, Any]:
    """单一 (page, kind) 粒度计分：TP / FP / FN。"""
    matched, unmatched_pred, unmatched_gt = greedy_match(predictions, ground_truths)
    return {
        "tp": len(matched),
        "fp": len(unmatched_pred),
        "fn": len(unmatched_gt),
    }
