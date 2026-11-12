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


def test_entities_exact_multiple_keeps_text_layer():
    # K2 恰等号负样本：3 == 2×1.5 不改判（严格 >）——保留文本层、无改判痕迹。
    v = arbitrate(SideResult(100, 2), SideResult(100, 3), T)
    assert (v.winner, v.switched_from, v.switch_reason, v.evidence_valid) == (
        "text_layer", None, None, True,
    )


def test_ocr_switch_path_marks_switched_from_text_layer():
    # 门⑥ 挂账钉：两条改判路径都必须带 switched_from="text_layer"（账本改判链）。
    v_chars = arbitrate(SideResult(100, 5), SideResult(200, 5), T)
    assert v_chars.switched_from == "text_layer"
    v_entities = arbitrate(SideResult(1000, 4), SideResult(1000, 7), T)
    assert v_entities.switched_from == "text_layer"
    v_zero = arbitrate(SideResult(900, 0), SideResult(900, 2), T)
    assert v_zero.switched_from == "text_layer"


def test_metrics_records_both_sides_for_any_verdict():
    # 门⑥ 挂账钉：metrics 无论改判与否都记 chars/entities 双侧数值（审计面）。
    v_switch = arbitrate(SideResult(100, 5), SideResult(200, 5), T)
    assert v_switch.metrics == {"chars": {"text_layer": 100, "ocr": 200},
                                "entities": {"text_layer": 5, "ocr": 5}}
    v_keep = arbitrate(SideResult(1000, 5), SideResult(1000, 5), T)
    assert v_keep.metrics == {"chars": {"text_layer": 1000, "ocr": 1000},
                              "entities": {"text_layer": 5, "ocr": 5}}
    v_invalid = arbitrate(SideResult(1000, 5), SideResult(0, 0), T)
    assert v_invalid.metrics["chars"]["ocr"] == 0 and v_invalid.metrics["entities"]["ocr"] == 0


def test_empty_evidence_is_invalid_not_pass():
    v = arbitrate(SideResult(1000, 5), SideResult(0, 0), T)
    assert v.winner == "text_layer" and v.evidence_valid is False


def test_empty_evidence_with_entities_counts_valid():
    v = arbitrate(SideResult(1000, 5), SideResult(0, 3), T)
    assert v.evidence_valid is True


def test_tl_stage_error_switches_unconditionally():
    v = arbitrate(SideResult(0, 0), SideResult(50, 1), T, tl_stage_error=True)
    assert (v.winner, v.switch_reason) == ("ocr", "text_layer_stage_error")


def test_arbiter_module_import_surface_is_pure():
    # 终审 M-1：arbiter 纯函数钉（对照 judge 家族的同型钉）——import 面白名单。
    import sys

    import app.services.page_routing.arbiter as m
    banned = {"fitz", "httpx", "sqlalchemy", "redis", "requests", "file_store", "settings"}
    hit = banned & set(sys.modules) & set(getattr(m, "__dict__", {}).keys())
    assert not hit, f"arbiter.py 不允许 import {hit}"


def test_arbiter_source_has_no_io_imports():
    # 终审 M-1：源码 token 钉——arbiter 只允许纯比较逻辑，禁任何 IO/存储/配置依赖。
    import inspect

    import app.services.page_routing.arbiter as m
    src = inspect.getsource(m)
    for token in ("import fitz", "import httpx", "import requests", "sqlalchemy",
                  "file_store", "settings"):
        assert token not in src, token
