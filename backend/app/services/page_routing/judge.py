"""Issue#273 P0 页级判定：输出动作决策（不判类型标签）。纯函数，无 IO。

判定语义（spec §2.2，阈值经 RoutingThresholds 注入）：
    任何「判 ocr」的规则都是安全方向（慢但不漏打）；「直接信任文本层」只在
    升档后发生。灰区页必须先过 L2 墨迹对齐；L2 缺失 ⇒ fail-closed 退 ocr。
"""
from __future__ import annotations

from app.services.page_routing.types import PageDecision, PageSignals, RoutingThresholds

ACTION_TEXT_LAYER = "text_layer"
ACTION_VERIFY = "text_layer_verify"
ACTION_OCR = "ocr"
ACTION_BOTH = "both"

_CONF_CLEAR = 1.0
_CONF_BOUNDARY = 0.5
_CONF_FALLBACK = 0.0


def _ocr(page: int, reason: str, signals: PageSignals, version: int, *, boundary: bool = False) -> PageDecision:
    # 置信优先级：probe/探针错误（fail-closed fallback）> boundary 实测边界 > clear。
    # 需求书测试 test_gray_zone_ink_missing_fails_closed_to_ocr 钉 ink_probe_error ⇒ 0.0，
    # 故 error 后缀必须压过 boundary 标志（boundary 仅修饰实测越界的规则）。
    if signals.probe_error or reason.endswith("_error"):
        conf = _CONF_FALLBACK
    elif boundary:
        conf = _CONF_BOUNDARY
    else:
        conf = _CONF_CLEAR
    return PageDecision(page, ACTION_OCR, False, conf, _signals_dict(signals), reason, version)


def _signals_dict(s: PageSignals) -> dict:
    from dataclasses import asdict
    d = asdict(s)
    for key in ("garbage_ratio", "short_word_ratio", "avg_word_len", "page_repeat_ratio"):
        d[key] = round(float(d[key]), 4)
    for key in ("embedded_ratio", "type3_ratio", "reading_order_agreement", "ink_alignment", "image_coverage"):
        if d[key] is not None:
            d[key] = round(float(d[key]), 4)
    return d


def needs_l2(signals: PageSignals, t: RoutingThresholds) -> bool:
    """灰区判定（spec §2.2 规则 7 前置条件）。只对「过了 1-6 条规则」的页有意义。"""
    if not signals.is_pdf or signals.probe_error:
        return False
    if signals.chars < t.min_chars:
        return False
    if signals.chars < t.min_chars * t.boundary_chars_band:
        return True
    if signals.type3_ratio is not None and signals.type3_ratio > t.max_type3_ratio:
        return True
    if signals.embedded_ratio is not None and signals.embedded_ratio < t.min_embedded_ratio:
        return True
    return False


def judge_page(
    signals: PageSignals,
    t: RoutingThresholds,
    *,
    routing_version: int,
    upgraded: bool = False,
    sampled: bool = False,
    escalated: bool = False,
) -> PageDecision:
    p = signals.page

    # 0. 采集器故障 ⇒ fail-closed（安全方向 = ocr）
    if signals.probe_error or not signals.is_pdf:
        return _ocr(p, "signal_probe_error" if signals.probe_error else "not_pdf", signals, routing_version)

    # 1-2. 字符下限 / 垃圾率
    if signals.chars < t.min_chars:
        return _ocr(p, "chars_below_min", signals, routing_version)
    if signals.garbage_ratio > t.max_garbage:
        return _ocr(p, "garbage_ratio_high", signals, routing_version)

    # 3. 粘连层
    if signals.max_word_len > t.max_word_chars:
        return _ocr(p, "glued_words", signals, routing_version)

    # 4. 阅读顺序（None = 未采到，不拦——保守）
    if signals.reading_order_agreement is not None and signals.reading_order_agreement < t.min_reading_order:
        return _ocr(p, "reading_order_broken", signals, routing_version, boundary=True)

    # 5. 页内重复串（水印支配）
    if signals.page_repeat_ratio > t.max_page_repeat:
        return _ocr(p, "page_repeat_ratio_high", signals, routing_version)

    # 6. 灰区 ⇒ L2 墨迹判定（在升档/抽样之前：灰区页不享受豁免）
    if needs_l2(signals, t):
        if signals.ink_alignment is None:
            return _ocr(p, "ink_probe_error", signals, routing_version, boundary=True)
        if signals.ink_alignment < t.min_ink:
            return _ocr(p, "ink_alignment_low", signals, routing_version, boundary=True)
        # 灰区但墨迹健康 → 继续往下（verify/抽样/升档）

    # 7. 卷内升档（直接信任）
    if upgraded:
        if sampled:
            return PageDecision(p, ACTION_BOTH, True, _CONF_BOUNDARY, _signals_dict(signals), "sampled_both", routing_version)
        return PageDecision(p, ACTION_TEXT_LAYER, False, _CONF_CLEAR, _signals_dict(signals), "upgraded_after_3_pass", routing_version)

    # 8. 整卷升级 both
    if escalated:
        return PageDecision(p, ACTION_BOTH, True, _CONF_BOUNDARY, _signals_dict(signals), "volume_escalated_both", routing_version)

    # 9. 抽样 both
    if sampled:
        return PageDecision(p, ACTION_BOTH, True, _CONF_BOUNDARY, _signals_dict(signals), "sampled_both", routing_version)

    # 10. 默认：先文本层但必须对账
    return PageDecision(p, ACTION_VERIFY, True, _CONF_CLEAR, _signals_dict(signals), "default_verify", routing_version)
