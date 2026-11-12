import json

from app.services.page_routing import judge
from app.services.page_routing.types import PageSignals, RoutingThresholds

T = RoutingThresholds(
    min_chars=120, max_garbage=0.15, max_word_chars=400,
    min_reading_order=0.80, max_page_repeat=0.30, boundary_chars_band=1.5,
    max_type3_ratio=0.5, min_embedded_ratio=0.5, min_ink=0.02,
    chars_multiple=1.3, entities_multiple=1.5, fullpage_image_coverage=0.9,
)


def _sig(**kw) -> PageSignals:
    base = dict(page=1, is_pdf=True, chars=800, garbage_ratio=0.0,
                short_word_ratio=0.1, avg_word_len=2.0, max_word_len=20,
                word_count=300, font_kinds=3, embedded_ratio=1.0, type3_ratio=0.0,
                reading_order_agreement=0.95, page_repeat_ratio=0.0)
    return PageSignals(**{**base, **kw})


def test_non_pdf_is_ocr():
    d = judge.judge_page(_sig(is_pdf=False), T, routing_version=1)
    assert (d.action, d.reason) == ("ocr", "not_pdf")


def test_chars_below_min_is_ocr():
    d = judge.judge_page(_sig(chars=50), T, routing_version=1)
    assert (d.action, d.reason) == ("ocr", "chars_below_min")


def test_chars_below_min_band_interior_is_ocr():
    # 变异清账（I-3 judge-1）：chars∈[80,120) 的样本必须仍被字符下限拦截——
    # 只测 chars=50（远低于任何合理阈值）会让「阈值 120→80」类变异假绿。
    d = judge.judge_page(_sig(chars=100), T, routing_version=1)
    assert (d.action, d.reason) == ("ocr", "chars_below_min")


def test_chars_equal_min_is_not_below_min():
    # 变异清账（I-3 judge-3）：chars == min_chars 恰等号不算「低于下限」（严格 <）；
    # 落入灰区带（120 < 120×1.5）后墨迹健康 ⇒ default_verify。
    d = judge.judge_page(_sig(chars=120, ink_alignment=0.12), T, routing_version=1)
    assert (d.action, d.reason) == ("text_layer_verify", "default_verify")


def test_garbage_glued_order_repeat_rules():
    for kw, reason in [
        (dict(garbage_ratio=0.5), "garbage_ratio_high"),
        (dict(max_word_len=500), "glued_words"),
        (dict(reading_order_agreement=0.3), "reading_order_broken"),
        (dict(page_repeat_ratio=0.5), "page_repeat_ratio_high"),
    ]:
        d = judge.judge_page(_sig(**kw), T, routing_version=1)
        assert (d.action, d.reason) == ("ocr", reason), kw


def test_fullpage_image_with_layer_is_ocr():
    # 规则 5.5：整页图+内嵌文本层 ⇒ 层是扫描 OCR 产物（执行层 is_scanned_page 同
    # 语义），宁 OCR——验收 B3 发现的判定-执行闸缝隙（老闸拒绝 ⇒ 白付一次失败的
    # 文本层试探且升档被压死），判定序必须在灰区之前吸收该信号。
    d = judge.judge_page(_sig(image_coverage=0.95), T, routing_version=1)
    assert (d.action, d.reason) == ("ocr", "fullpage_image_with_layer")


def test_partial_image_coverage_does_not_trigger_fullpage_rule():
    # 低于阈值（部分插图页）不触发 5.5 ⇒ 走 default_verify。
    d = judge.judge_page(_sig(image_coverage=0.38), T, routing_version=1)
    assert (d.action, d.reason) == ("text_layer_verify", "default_verify")


def test_image_coverage_none_skips_fullpage_rule():
    # coverage 未采到（None）⇒ 跳过 5.5 不拦，保守放行后续规则。
    d = judge.judge_page(_sig(image_coverage=None), T, routing_version=1)
    assert (d.action, d.reason) == ("text_layer_verify", "default_verify")


def test_gray_zone_low_ink_is_ocr():
    d = judge.judge_page(_sig(chars=150, ink_alignment=0.001), T, routing_version=1)
    assert (d.action, d.reason) == ("ocr", "ink_alignment_low")


def test_gray_zone_high_ink_falls_through_to_verify():
    d = judge.judge_page(_sig(chars=150, ink_alignment=0.12), T, routing_version=1)
    assert (d.action, d.reason) == ("text_layer_verify", "default_verify")


def test_gray_zone_ink_missing_fails_closed_to_ocr():
    d = judge.judge_page(_sig(chars=150, ink_alignment=None), T, routing_version=1)
    assert (d.action, d.reason) == ("ocr", "ink_probe_error")
    assert d.confidence == 0.0


def test_probe_error_is_ocr_fallback():
    d = judge.judge_page(_sig(probe_error=True), T, routing_version=1)
    assert (d.action, d.reason, d.confidence) == ("ocr", "signal_probe_error", 0.0)


def test_upgraded_direct_trust_only_when_not_gray():
    d = judge.judge_page(_sig(chars=800), T, routing_version=1, upgraded=True)
    assert (d.action, d.reason) == ("text_layer", "upgraded_after_3_pass")
    # 灰区页不受升档豁免：仍走 L2 判定（此处 ink 缺失 ⇒ fail-closed）
    d2 = judge.judge_page(_sig(chars=150, ink_alignment=None), T, routing_version=1, upgraded=True)
    assert d2.action == "ocr"


def test_sampled_page_is_both_even_upgraded():
    d = judge.judge_page(_sig(chars=800), T, routing_version=1, upgraded=True, sampled=True)
    assert (d.action, d.reason) == ("both", "sampled_both")


def test_escalated_volume_is_both():
    d = judge.judge_page(_sig(chars=800), T, routing_version=1, escalated=True)
    assert (d.action, d.reason) == ("both", "volume_escalated_both")


def test_escalated_outranks_upgraded_in_combined_state():
    # 终审 I-1 组合态钉：抽样改判引发整卷升级后，后续页即使已 upgraded 直信、
    # 整卷升级语义也不得被升档遮蔽——escalated 判定必须在 upgraded 之前 ⇒ both。
    d = judge.judge_page(_sig(), T, routing_version=1, upgraded=True, escalated=True)
    assert (d.action, d.reason) == ("both", "volume_escalated_both")


def test_gray_zone_resolved_before_escalation():
    # 门⑥ 挂账钉：灰区页在整卷升级之前先走 L2 判定——灰区不享受升级豁免
    #（ink 缺失/过低都 fail-closed 退 ocr，escalated=True 压不过规则 6）。
    d = judge.judge_page(_sig(chars=150, ink_alignment=0.001), T, routing_version=1, escalated=True)
    assert (d.action, d.reason) == ("ocr", "ink_alignment_low")


def test_needs_l2_boundary_only():
    assert judge.needs_l2(_sig(chars=150), T) is True       # 150 < 120×1.5
    assert judge.needs_l2(_sig(chars=800), T) is False
    assert judge.needs_l2(_sig(chars=800, type3_ratio=0.9), T) is True
    assert judge.needs_l2(_sig(chars=800, embedded_ratio=0.1), T) is True
    assert judge.needs_l2(_sig(chars=800, type3_ratio=None, embedded_ratio=None), T) is False


def test_judge_is_idempotent_and_json_stable():
    s = _sig()
    outs = [json.dumps(judge.judge_page(s, T, routing_version=1).as_dict(), sort_keys=True) for _ in range(3)]
    assert len(set(outs)) == 1


def test_boundary_equality_is_not_gray():
    # 恰好 120×1.5=180 不算灰区（严格 <）
    d = judge.judge_page(_sig(chars=180, ink_alignment=None), T, routing_version=1)
    assert d.action == "text_layer_verify"


def test_short_word_ratio_and_avg_word_len_not_in_p0_rules():
    # spec 判定序 P0 未含 short_word/avg_word 独立规则（复用键仅入 signals 供账本）
    d = judge.judge_page(_sig(short_word_ratio=0.99, avg_word_len=1.0), T, routing_version=1)
    assert d.action == "text_layer_verify"


def test_judge_module_import_surface_is_pure():
    import sys

    import app.services.page_routing.judge as m
    banned = {"fitz", "httpx", "sqlalchemy", "redis", "requests"}
    hit = banned & set(sys.modules) & set(getattr(m, "__dict__", {}).keys())
    assert not hit, f"judge.py 不允许 import {hit}"


def test_judge_source_has_no_io_imports():
    import inspect

    import app.services.page_routing.judge as m
    src = inspect.getsource(m)
    for token in ("import fitz", "import httpx", "import requests", "sqlalchemy", "file_store"):
        assert token not in src, token
