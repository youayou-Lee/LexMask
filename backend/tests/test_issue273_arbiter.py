from app.services.page_routing.arbiter import arbitrate
from app.services.page_routing.types import RoutingThresholds, SideResult

T = RoutingThresholds(
    min_chars=120, max_garbage=0.15, max_word_chars=400, min_reading_order=0.8,
    max_page_repeat=0.3, boundary_chars_band=1.5, max_type3_ratio=0.5,
    min_embedded_ratio=0.5, min_ink=0.02, chars_multiple=1.3, entities_multiple=1.5,
)


def test_chars_multiple_switches_to_ocr():
    v = arbitrate(SideResult(100, 5), SideResult(200, 5), T)
    assert v.winner == "ocr" and v.switch_reason == "ocr_evidence_higher_chars"


def test_entities_multiple_switches_to_ocr():
    v = arbitrate(SideResult(1000, 4), SideResult(1000, 7), T)
    assert v.winner == "ocr" and v.switch_reason == "ocr_evidence_higher_entities"


def test_zero_tl_entities_and_ocr_positive_switches():
    v = arbitrate(SideResult(900, 0), SideResult(900, 2), T)
    assert v.winner == "ocr"


def test_tie_keeps_text_layer_and_no_switch():
    v = arbitrate(SideResult(1000, 5), SideResult(1000, 5), T)
    assert (v.winner, v.switched_from, v.switch_reason) == ("text_layer", None, None)
    # 恰好 1.3× 不改判（严格 >）
    v2 = arbitrate(SideResult(100, 5), SideResult(130, 5), T)
    assert v2.winner == "text_layer"


def test_empty_evidence_is_invalid_not_pass():
    v = arbitrate(SideResult(1000, 5), SideResult(0, 0), T)
    assert v.winner == "text_layer" and v.evidence_valid is False


def test_empty_evidence_with_entities_counts_valid():
    v = arbitrate(SideResult(1000, 5), SideResult(0, 3), T)
    assert v.evidence_valid is True


def test_tl_stage_error_switches_unconditionally():
    v = arbitrate(SideResult(0, 0), SideResult(50, 1), T, tl_stage_error=True)
    assert (v.winner, v.switch_reason) == ("ocr", "text_layer_stage_error")
