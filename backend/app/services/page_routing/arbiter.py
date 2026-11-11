"""Issue#273 结果级仲裁（spec §2.3）：证据有效性前置 + K1/K2 改判。纯函数。

不对称代价：漏改判（保留坏文本层）⇒ 静默漏打；误改判 ⇒ 多花一次已有结果的成本。
阈值偏「宁改判」。平局保留文本层（框更贴排版层）。
"""
from __future__ import annotations

from app.services.page_routing.types import ArbitrationVerdict, RoutingThresholds, SideResult


def arbitrate(
    text_layer: SideResult,
    ocr: SideResult,
    t: RoutingThresholds,
    *,
    tl_stage_error: bool = False,
) -> ArbitrationVerdict:
    metrics = {
        "chars": {"text_layer": text_layer.chars, "ocr": ocr.chars},
        "entities": {"text_layer": text_layer.entities, "ocr": ocr.entities},
    }
    if tl_stage_error:
        return ArbitrationVerdict("ocr", "text_layer", "text_layer_stage_error", True, metrics)

    evidence_valid = not (ocr.chars == 0 and ocr.entities == 0)
    if not evidence_valid:
        # B9：空证据 = 对账未实际发生 ⇒ 保留文本层 + verify_incomplete（router 映射）
        return ArbitrationVerdict("text_layer", None, None, False, metrics)

    if ocr.chars > text_layer.chars * t.chars_multiple:
        return ArbitrationVerdict("ocr", "text_layer", "ocr_evidence_higher_chars", True, metrics)
    if ocr.entities > text_layer.entities * t.entities_multiple:
        return ArbitrationVerdict("ocr", "text_layer", "ocr_evidence_higher_entities", True, metrics)
    if text_layer.entities == 0 and ocr.entities > 0:
        return ArbitrationVerdict("ocr", "text_layer", "ocr_evidence_higher_entities", True, metrics)
    return ArbitrationVerdict("text_layer", None, None, True, metrics)
