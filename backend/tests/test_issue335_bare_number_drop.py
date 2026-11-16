"""Issue#335：页码/纯数字被当隐私打码——页眉带 + 正文无锚裸数字闸。

取证（8 页混合样本导出请求体全量 385 框）：1–4 位纯数字框 24 个
（AGE 18 / CASE_NUMBER 6），仅 1 个落在 #308 的页脚 8% 带内 ⇒
页眉页码与正文裸数字是 #308 的残留面。

用户拍板（2026-11-17）：
- 正文表格裸数字（如年龄列「41」）判定为无锚误打后**直接丢框**；
- CASE_NUMBER 无编号结构纯数字丢弃线 = **≤4 位**（5 位以上可能是
  发票号/流水号，保留，漏打面趋零）；
- 方案组合 B（值形状闸）+ A（页眉带对称补齐）+ D（分类型计数），
  不做 C（preset regex 当精度闸要改「正则兜底」的机制语义）。

负样本钉：真值（41岁/完整案号/带前缀编号）1:1 保留；manual 框永存
（#280）；visual_features 不参与值判据（0710 指纹实证保护，#308 模块
约束）；AMOUNT 裸数字是合法金额真值，不在射程内。
"""

from __future__ import annotations

from pathlib import Path

from app.services.ocr_has_vision_service import SensitiveRegion
from app.services.vision import box_quality_pass as bqp


def _region(text: str, entity_type: str, left: int, top: int, width: int, height: int,
            source: str = "ocr") -> SensitiveRegion:
    return SensitiveRegion(
        text=text, entity_type=entity_type, left=left, top=top,
        width=width, height=height, source=source,
    )


# ---------------------------------------------------------------------------
# ⑥ 无锚裸数字闸（值形状，B）——AGE
# ---------------------------------------------------------------------------

class TestAgeBareDigitDrop:
    def test_bare_two_digit_dropped(self) -> None:
        # 8 页样本实证主形态：表格年龄列裸数字（'41' 同值重复 12 次）
        regions = [_region("41", "AGE", 300, 500, 12, 14)] * 12
        got = bqp.drop_unanchored_number_regions(regions)
        assert got == []

    def test_bare_one_and_three_digit_dropped(self) -> None:
        regions = [_region("7", "AGE", 300, 500, 8, 14),
                   _region("105", "AGE", 300, 500, 18, 14)]
        assert bqp.drop_unanchored_number_regions(regions) == []

    def test_anchored_values_kept(self) -> None:
        # 个人年龄属性真值 1:1 保留（无情态前缀、无以上/以下后缀）；
        # 条件短语（未满/…以上）自拍板 2026-11-17 起改判丢弃，
        # 见 TestAgeConditionPhraseDrop。
        regions = [_region("35岁", "AGE", 300, 500, 24, 14),
                   _region("41 岁", "AGE", 300, 500, 24, 14)]
        got = bqp.drop_unanchored_number_regions(regions)
        assert [r.text for r in got] == ["35岁", "41 岁"]

    def test_chinese_numeral_age_kept(self) -> None:
        regions = [_region("四十一", "AGE", 300, 500, 30, 14)]
        assert len(bqp.drop_unanchored_number_regions(regions)) == 1

    def test_four_digit_age_out_of_scope_kept(self) -> None:
        # 拍板口径：AGE 射程 1–3 位；4 位（年份残段形态）不丢，漏打面趋零
        regions = [_region("1975", "AGE", 300, 500, 24, 14)]
        assert len(bqp.drop_unanchored_number_regions(regions)) == 1

    def test_non_scope_types_kept(self) -> None:
        # AMOUNT 裸数字是合法金额真值（表格金额列）；家族外永不参与
        regions = [_region("41", "AMOUNT", 300, 500, 12, 14),
                   _region("41", "PERSON", 300, 500, 12, 14)]
        assert len(bqp.drop_unanchored_number_regions(regions)) == 2

    def test_manual_source_never_dropped(self) -> None:
        # 人工框永存（#280 铁律）
        regions = [_region("41", "AGE", 300, 500, 12, 14, source="manual")]
        assert len(bqp.drop_unanchored_number_regions(regions)) == 1

    def test_visual_features_never_dropped(self) -> None:
        # 值判据对视觉特征框无意义（#308 模块约束：0710 指纹实证保护）
        regions = [_region("41", "AGE", 300, 500, 12, 14, source="visual_features")]
        assert len(bqp.drop_unanchored_number_regions(regions)) == 1


# ---------------------------------------------------------------------------
# ⑥ 无锚裸数字闸（值形状，B）——CASE_NUMBER
# ---------------------------------------------------------------------------

class TestCaseNumberBareDrop:
    def test_short_bare_digits_dropped(self) -> None:
        regions = [_region("500", "CASE_NUMBER", 300, 500, 18, 14),
                   _region("12", "CASE_NUMBER", 300, 500, 12, 14),
                   _region("2026", "CASE_NUMBER", 300, 500, 24, 14)]
        assert bqp.drop_unanchored_number_regions(regions) == []

    def test_separator_garbage_dropped(self) -> None:
        # 8 页样本实证形态：千分位/小数分隔残段（数字总数 >4，但纯分隔形状）
        regions = [_region("500, 0. 00", "CASE_NUMBER", 300, 500, 40, 14),
                   _region("1,234", "CASE_NUMBER", 300, 500, 30, 14)]
        assert bqp.drop_unanchored_number_regions(regions) == []

    def test_five_plus_digits_kept(self) -> None:
        # 拍板口径：5 位以上无结构纯数字可能是发票号/流水号 → 保留
        regions = [_region("12345", "CASE_NUMBER", 300, 500, 30, 14),
                   _region("12345678", "CASE_NUMBER", 300, 500, 48, 14)]
        assert len(bqp.drop_unanchored_number_regions(regions)) == 2

    def test_structured_values_kept(self) -> None:
        # 编号结构锚：字母前缀 / （年份）/ 连字符（preset examples 形态）
        regions = [_region("BH-2026-0001", "CASE_NUMBER", 300, 500, 90, 14),
                   _region("SO-2026-0509", "CASE_NUMBER", 300, 500, 90, 14),
                   _region("（2026）京0101民初100号", "CASE_NUMBER", 300, 500, 150, 14),
                   _region("LS20260509001", "CASE_NUMBER", 300, 500, 100, 14)]
        got = bqp.drop_unanchored_number_regions(regions)
        assert [r.text for r in got] == ["BH-2026-0001", "SO-2026-0509",
                                         "（2026）京0101民初100号", "LS20260509001"]

    def test_non_pure_shape_kept(self) -> None:
        # 含 CJK/号尾的值不是裸数字形状 → 不在射程
        regions = [_region("第3页", "CASE_NUMBER", 300, 500, 36, 14),
                   _region("100号", "CASE_NUMBER", 300, 500, 30, 14)]
        assert len(bqp.drop_unanchored_number_regions(regions)) == 2

    def test_legal_case_id_pagenumber_dropped(self) -> None:
        # 86 页卷目录页实证（2026-11-17）：HaS 把页次列标成 LEGAL_CASE_ID
        # ——页码区间与 ≤4 位裸数字规则扩到该类型（原拍板以「零取证」划
        # 出射程，证据成立后扩面）
        regions = [_region("002-013", "LEGAL_CASE_ID", 400, 300, 60, 14),
                   _region("001", "LEGAL_CASE_ID", 400, 400, 30, 14),
                   _region("3", "LEGAL_CASE_ID", 300, 500, 8, 14)]
        assert bqp.drop_unanchored_number_regions(regions) == []

    def test_legal_case_id_real_values_kept(self) -> None:
        # 真案号（（YYYY）…号）与长流水号（≥5 位）保留
        regions = [_region("（2021）桂0603民初2237号", "LEGAL_CASE_ID", 400, 300, 150, 14),
                   _region("1000050001202008100703315460294", "LEGAL_CASE_ID", 400, 400, 200, 14)]
        got = bqp.drop_unanchored_number_regions(regions)
        assert [r.text for r in got] == ["（2021）桂0603民初2237号",
                                         "1000050001202008100703315460294"]

    def test_manual_source_never_dropped(self) -> None:
        regions = [_region("500", "CASE_NUMBER", 300, 500, 18, 14, source="manual")]
        assert len(bqp.drop_unanchored_number_regions(regions)) == 1


# ---------------------------------------------------------------------------
# 拍板追加（2026-11-17 用户实测截图）：页码区间 + 年龄条件短语
# ---------------------------------------------------------------------------

class TestPageRangeDrop:
    """卷内目录页次列（`002-013`）——连字符在此是页码分隔不是编号锚。"""

    def test_page_range_dropped(self) -> None:
        regions = [_region("002-013", "CASE_NUMBER", 400, 300, 60, 14),
                   _region("050-053", "CASE_NUMBER", 400, 400, 60, 14),
                   _region("1-3", "CASE_NUMBER", 400, 500, 24, 14)]
        assert bqp.drop_unanchored_number_regions(regions) == []

    def test_year_segment_range_kept(self) -> None:
        # 4 位年份段是真编号形态（2026-0001）——页码区间规则不碰
        regions = [_region("2026-0001", "CASE_NUMBER", 400, 300, 80, 14)]
        assert len(bqp.drop_unanchored_number_regions(regions)) == 1

    def test_long_segment_range_kept(self) -> None:
        # 段长超 3 位（发票号/流水号区间形态）不在射程
        regions = [_region("123-45678", "CASE_NUMBER", 400, 300, 80, 14)]
        assert len(bqp.drop_unanchored_number_regions(regions)) == 1

    def test_letter_prefix_range_kept(self) -> None:
        regions = [_region("BH-2026-0001", "CASE_NUMBER", 400, 300, 90, 14)]
        assert len(bqp.drop_unanchored_number_regions(regions)) == 1

    def test_page_range_legal_case_id_dropped(self) -> None:
        # 86 页卷目录页实证：页次区间被标 LEGAL_CASE_ID → 同规则拦截
        regions = [_region("002-013", "LEGAL_CASE_ID", 400, 300, 60, 14)]
        assert bqp.drop_unanchored_number_regions(regions) == []

    def test_page_range_manual_source_kept(self) -> None:
        regions = [_region("002-013", "CASE_NUMBER", 400, 300, 60, 14, source="manual")]
        assert len(bqp.drop_unanchored_number_regions(regions)) == 1


class TestAgeConditionPhraseDrop:
    """法律条件短语 ≠ 个人年龄属性（用户实测「未满18周岁」被打码）。"""

    def test_condition_prefix_dropped(self) -> None:
        regions = [_region("未满18周岁", "AGE", 300, 500, 60, 14),
                   _region("已满16周岁", "AGE", 300, 500, 60, 14),
                   _region("年满十八周岁", "AGE", 300, 500, 60, 14)]
        assert bqp.drop_unanchored_number_regions(regions) == []

    def test_condition_suffix_dropped(self) -> None:
        regions = [_region("60岁以上", "AGE", 300, 500, 48, 14),
                   _region("18周岁以下", "AGE", 300, 500, 48, 14)]
        assert bqp.drop_unanchored_number_regions(regions) == []

    def test_prefix_and_suffix_dropped(self) -> None:
        regions = [_region("未满18周岁以上", "AGE", 300, 500, 72, 14)]
        assert bqp.drop_unanchored_number_regions(regions) == []

    def test_person_attribute_age_kept(self) -> None:
        # 无情态前缀且无以上/以下后缀 = 个人年龄属性 → 照常保留打码
        regions = [_region("41岁", "AGE", 300, 500, 24, 14),
                   _region("41周岁", "AGE", 300, 500, 30, 14),
                   _region("41 岁", "AGE", 300, 500, 24, 14)]
        got = bqp.drop_unanchored_number_regions(regions)
        assert [r.text for r in got] == ["41岁", "41周岁", "41 岁"]

    def test_chinese_numeral_person_age_kept(self) -> None:
        regions = [_region("四十一", "AGE", 300, 500, 30, 14)]
        assert len(bqp.drop_unanchored_number_regions(regions)) == 1

    def test_non_age_type_condition_kept(self) -> None:
        # 射程仅 AGE：同形值落在家族外类型不碰
        regions = [_region("未满18周岁", "PERSON", 300, 500, 60, 14)]
        assert len(bqp.drop_unanchored_number_regions(regions)) == 1

    def test_condition_manual_source_kept(self) -> None:
        regions = [_region("未满18周岁", "AGE", 300, 500, 60, 14, source="manual")]
        assert len(bqp.drop_unanchored_number_regions(regions)) == 1


class TestPresetAgeGuidanceNarrowed:
    """用户拍板的源头治理：preset 与 HaS builtin guidance 不再教条件短语。"""

    def test_preset_age_examples_person_attribute_only(self) -> None:
        import json
        cfg = json.loads((Path(__file__).resolve().parents[1] / "config" / "preset_entity_types.json").read_text())
        assert "未满18周岁" not in cfg["AGE"]["examples"]
        assert "60岁以上" not in cfg["AGE"]["examples"]

    def test_builtin_guidance_examples_person_attribute_only(self) -> None:
        from app.services.has_service import HaSService
        guidance = HaSService.BUILTIN_TYPE_GUIDANCE["AGE"]
        assert "未满18周岁" not in guidance["examples"]
        assert "60岁以上" not in guidance["examples"]
        assert "未满" in guidance["description"]  # 负向指引写明条件短语不是年龄

# ---------------------------------------------------------------------------
# ⑤ 页眉带（几何，A）——与页脚 8% 带对称
# ---------------------------------------------------------------------------

class TestHeaderPagenumberDrop:
    def test_header_short_digit_dropped(self) -> None:
        # 页码在页眉：top=30（1000 高页面的 3%）
        regions = [_region("3", "LEGAL_CASE_ID", 497, 30, 3, 13)]
        assert bqp.drop_header_pagenumber_regions(regions, 1000, 1000) == []

    def test_header_two_digit_dropped_at_band_edge(self) -> None:
        regions = [_region("12", "LEGAL_CASE_ID", 497, 70, 20, 13)]
        assert bqp.drop_header_pagenumber_regions(regions, 1000, 1000) == []

    def test_below_band_digit_kept(self) -> None:
        # top=100（10%）已出页眉带 → 不丢
        regions = [_region("3", "LEGAL_CASE_ID", 497, 100, 3, 13)]
        assert len(bqp.drop_header_pagenumber_regions(regions, 1000, 1000)) == 1

    def test_header_full_case_number_kept(self) -> None:
        # 首页页眉的完整案号不是纯数字 → 存活
        regions = [_region("（2021）桂0603民初2237号", "LEGAL_CASE_ID", 200, 30, 300, 13)]
        assert len(bqp.drop_header_pagenumber_regions(regions, 1000, 1000)) == 1

    def test_header_nonpure_digit_kept(self) -> None:
        regions = [_region("第3页", "LEGAL_CASE_ID", 480, 30, 40, 13)]
        assert len(bqp.drop_header_pagenumber_regions(regions, 1000, 1000)) == 1

    def test_wide_box_kept(self) -> None:
        # 宽度 >5% 页宽的框不丢（几何侧证据不足）
        regions = [_region("12", "LEGAL_CASE_ID", 100, 30, 200, 13)]
        assert len(bqp.drop_header_pagenumber_regions(regions, 1000, 1000)) == 1

    def test_zero_page_guard_fail_open(self) -> None:
        regions = [_region("3", "LEGAL_CASE_ID", 497, 30, 3, 13)]
        assert len(bqp.drop_header_pagenumber_regions(regions, 0, 0)) == 1


# ---------------------------------------------------------------------------
# 组合入口（接线口径）：页脚 + 页眉 + 无锚裸数字三闸一次到位
# ---------------------------------------------------------------------------

class TestCompositePass:
    def test_composite_drops_all_three_kinds(self) -> None:
        regions = [
            _region("3", "LEGAL_CASE_ID", 497, 937, 3, 13),        # 页脚页码（#308）
            _region("12", "LEGAL_CASE_ID", 497, 30, 20, 13),       # 页眉页码（⑤）
            _region("41", "AGE", 300, 500, 12, 14),                # 正文裸年龄（⑥）
            _region("500", "CASE_NUMBER", 400, 500, 18, 14),       # 正文裸编号（⑥）
            _region("41岁", "AGE", 500, 500, 24, 14),              # 真值保留
            _region("（2026）京0101民初100号", "CASE_NUMBER", 600, 500, 150, 14),
            _region("93815", "AMOUNT", 700, 500, 36, 14),          # 合法金额保留
        ]
        got = bqp.drop_edge_and_unanchored_number_regions(regions, 1000, 1000)
        assert [r.text for r in got] == ["41岁", "（2026）京0101民初100号", "93815"]

    def test_composite_zero_dims_still_applies_value_gate(self) -> None:
        # 页面尺寸不可得（文本层线）：几何两闸 fail-open，值形状闸照常生效
        regions = [_region("41", "AGE", 300, 500, 12, 14),
                   _region("3", "LEGAL_CASE_ID", 497, 30, 3, 13)]
        got = bqp.drop_edge_and_unanchored_number_regions(regions, 0, 0)
        # 值闸对 LEGAL_CASE_ID 裸数字同样生效（86 页卷扩面）
        assert got == []


# ---------------------------------------------------------------------------
# 接线钉（#308 同款手法）：组合入口两处调用 + 新 pass 在公开面
# ---------------------------------------------------------------------------

class TestWiringNails:
    def test_vision_service_wires_composite_pass(self) -> None:
        # 两处调用点：_filter_ocr_has_regions（图像线）+ _detect_with_pdf_text_layer
        src = (Path(__file__).resolve().parents[1] / "app" / "services" / "vision_service.py").read_text()
        assert src.count("box_quality_pass.drop_edge_and_unanchored_number_regions(") == 2

    def test_passes_registered_in_module(self) -> None:
        for name in (
            "drop_header_pagenumber_regions",
            "drop_unanchored_number_regions",
            "drop_edge_and_unanchored_number_regions",
        ):
            assert hasattr(bqp, name), name
