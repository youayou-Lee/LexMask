"""Issue#308：实体框质量打磨五 pass（值级 2b/2c + 框级 ①③④）。

设计底稿：LexMask-workspace/github-issues/2026-11-15-issue308-box-quality-polish.md
验收合同 A1′–A4′ 的单测投影；真卷级断言走本地栈 API（run_volume_snapshot.py）。

四类打磨项的根因（基线 pre-fix-file-snapshot.json 实证）：
- ① 标签双份：同值落 LEGAL_CASE_ID+CASE_NUMBER 双框（p1/p2 ×5 组）；
- ② 过度捕获：NER 值带叙述前缀/括号包裹 + 碎片框（事调解书》）
  + AMOUNT 吞整句（含数字，2c 杀不掉 → 2b 尾锚收缩）；
- ③ 页脚页码：p3 页脚 `3`（y=0.937）穿透 ink/hull 全部现有闸；
- ④ 断行拆分：p4 `（2021）桂0603民`+`初2237号`（行尾+次行行首；
  union 单框被像素证据否决 → 同值双紧框，几何不变只并值）。
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
# 2b 值级核心提取
# ---------------------------------------------------------------------------

class TestValueCoreTrim:
    def test_case_id_strips_narrative_prefix(self) -> None:
        ents = [{"type": "LEGAL_CASE_ID", "text": "防城区法院作出（2021）桂0603民初2237号"}]
        got = bqp.normalize_number_shaped_entities(ents)
        assert got[0]["text"] == "（2021）桂0603民初2237号"

    def test_case_id_strips_bracket_wrapped(self) -> None:
        # 基线 p1/p4 实证形态：[（2026）桂06执155号] / [案号（2023）桂06执异57号]
        ents = [
            {"type": "LEGAL_CASE_ID", "text": "[（2026）桂06执155号]"},
            {"type": "LEGAL_CASE_ID", "text": "[案号（2023）桂06执异57号]"},
        ]
        got = bqp.normalize_number_shaped_entities(ents)
        assert got[0]["text"] == "（2026）桂06执155号"
        assert got[1]["text"] == "（2023）桂06执异57号"

    def test_case_id_noop_when_already_core(self) -> None:
        core = "（2021）桂0603民初2237号"
        ents = [{"type": "LEGAL_CASE_ID", "text": core}]
        assert bqp.normalize_number_shaped_entities(ents)[0]["text"] == core

    def test_case_id_noop_without_core_shape(self) -> None:
        ents = [{"type": "LEGAL_CASE_ID", "text": "北京市海淀区人民法院"}]
        assert bqp.normalize_number_shaped_entities(ents)[0]["text"] == "北京市海淀区人民法院"

    def test_case_id_inner_spaces_preserved(self) -> None:
        # p6 空格形态（#301 召回成果）——核心内的空白必须原样保留
        ents = [{"type": "LEGAL_CASE_ID", "text": "综上，有（2023）桂 0603 民初 30号"}]
        got = bqp.normalize_number_shaped_entities(ents)
        assert got[0]["text"] == "（2023）桂 0603 民初 30号"

    def test_amount_unit_anchored_noop_on_genuine_values(self) -> None:
        for v in ("171328.42元", "798万元", "93815元"):
            ents = [{"type": "AMOUNT", "text": v}]
            assert bqp.normalize_number_shaped_entities(ents)[0]["text"] == v

    def test_amount_trailing_digit_run_trim(self) -> None:
        # 基线 p4 实证：吞整句框无单位可锚，只能尾锚收缩
        v = "最终判决解除对案涉账户的冻结，并确认超大公司对该账户内的201280"
        ents = [{"type": "AMOUNT", "text": v}]
        assert bqp.normalize_number_shaped_entities(ents)[0]["text"] == "201280"

    def test_amount_chinese_numeral_unit_trim(self) -> None:
        ents = [{"type": "AMOUNT", "text": "共计赔偿壹拾万元整并承担诉讼费"}]
        assert bqp.normalize_number_shaped_entities(ents)[0]["text"] == "壹拾万元整"

    def test_amount_noop_pure_number(self) -> None:
        ents = [{"type": "AMOUNT", "text": "93815"}]
        assert bqp.normalize_number_shaped_entities(ents)[0]["text"] == "93815"

    def test_nonfamily_types_untouched(self) -> None:
        # 家族外类型（PERSON 等）不做值改写——防误修剪人名等
        ents = [{"type": "PERSON", "text": "张三（2021）桂0603民初2237号"}]
        assert bqp.normalize_number_shaped_entities(ents)[0]["text"] == "张三（2021）桂0603民初2237号"


# ---------------------------------------------------------------------------
# 2c 碎片值过滤
# ---------------------------------------------------------------------------

class TestFragmentDrop:
    def test_digitless_case_id_fragment_dropped(self) -> None:
        ents = [
            {"type": "LEGAL_CASE_ID", "text": "事调解书》"},
            {"type": "LEGAL_CASE_ID", "text": "（2021）桂0603民初2237号"},
        ]
        got = bqp.drop_fragment_entities(ents)
        assert [e["text"] for e in got] == ["（2021）桂0603民初2237号"]

    def test_digitless_amount_sentence_dropped(self) -> None:
        ents = [{"type": "AMOUNT", "text": "最终判决解除对案涉账户的冻结"}]
        assert bqp.drop_fragment_entities(ents) == []

    def test_chinese_numeral_amount_kept(self) -> None:
        ents = [{"type": "AMOUNT", "text": "壹佰万元整"}]
        assert bqp.drop_fragment_entities(ents) == ents

    def test_nonfamily_never_dropped(self) -> None:
        ents = [{"type": "PERSON", "text": "张三"}]
        assert bqp.drop_fragment_entities(ents) == ents


# ---------------------------------------------------------------------------
# ① 同值跨类型消解
# ---------------------------------------------------------------------------

class TestSameValueDedupe:
    def test_exact_twin_pair_keeps_legal(self) -> None:
        # 基线 p1：同位置同值 LEGAL_CASE_ID+CASE_NUMBER → 1 框 LEGAL_CASE_ID
        regions = [
            _region("（2026）桂06执155号", "LEGAL_CASE_ID", 350, 791, 270, 18),
            _region("（2026）桂06执155号", "CASE_NUMBER", 350, 791, 268, 17),
        ]
        got = bqp.dedupe_same_value_regions(regions)
        assert len(got) == 1
        assert got[0].entity_type == "LEGAL_CASE_ID"

    def test_series_range_routes_to_legal(self) -> None:
        # 基线 p2 `2282号-2287号`：无（YYYY）前缀但以号收尾 → 编号形态 → LEGAL_CASE_ID 存活
        regions = [
            _region("2282号-2287号", "LEGAL_CASE_ID", 561, 321, 160, 26),
            _region("2282号-2287号", "CASE_NUMBER", 561, 321, 158, 25),
        ]
        got = bqp.dedupe_same_value_regions(regions)
        assert len(got) == 1
        assert got[0].entity_type == "LEGAL_CASE_ID"

    def test_different_occurrences_all_kept(self) -> None:
        # p2 ×4 合法出现：同页同值但几何分离 → 全保留（A1′ 按出现位置计数）
        boxes = [_region("（2021）桂0603民初2237号", "LEGAL_CASE_ID", x, y, 200, 20)
                 for x, y in ((100, 100), (100, 200), (100, 300), (100, 400))]
        assert len(bqp.dedupe_same_value_regions(boxes)) == 4

    def test_nonoverlap_no_crossline_collapse(self) -> None:
        # 多行值逐行矩形（split_regions_across_lines 产物：同文不同行）不互消解
        boxes = [
            _region("（2021）桂0603民初2237号", "LEGAL_CASE_ID", 689, 208, 181, 18),
            _region("（2021）桂0603民初2237号", "LEGAL_CASE_ID", 139, 245, 104, 17),
        ]
        assert len(bqp.dedupe_same_value_regions(boxes)) == 2

    def test_visual_features_never_deduped(self) -> None:
        # 0710 指纹实证保护：LA/视觉特征框（source=visual_features）不参与消解
        regions = [
            _region("张三", "LEGAL_CASE_ID", 100, 100, 200, 20),
            _region("张三", "CASE_NUMBER", 100, 100, 200, 20, source="visual_features"),
        ]
        assert len(bqp.dedupe_same_value_regions(regions)) == 2

    def test_different_values_never_deduped(self) -> None:
        regions = [
            _region("（2026）桂06执155号", "LEGAL_CASE_ID", 350, 791, 270, 18),
            _region("（2025）桂06执100号", "CASE_NUMBER", 350, 791, 268, 17),
        ]
        assert len(bqp.dedupe_same_value_regions(regions)) == 2

    def test_deterministic_tiebreak(self) -> None:
        # 同类型同值重叠（防御路径）：保留面积更大者，结果与输入顺序无关
        pair_a = [
            _region("（2026）桂06执155号", "CASE_NUMBER", 350, 791, 270, 18),
            _region("（2026）桂06执155号", "CASE_NUMBER", 350, 791, 200, 15),
        ]
        pair_b = list(reversed(pair_a))
        out_a = bqp.dedupe_same_value_regions(pair_a)
        out_b = bqp.dedupe_same_value_regions(pair_b)
        assert len(out_a) == 1 and len(out_b) == 1
        assert out_a[0].width == out_b[0].width == 270

    def test_nonfamily_same_value_overlap_untouched(self) -> None:
        # 预览栈越界实证的回归钉：家族外跨类型同值重叠是合法共存
        # （COMPANY_NAME×WORK_UNIT 同文双框；0710 判例：不同类型=不同实体）
        regions = [
            _region("广西佳润商贸有限公司", "COMPANY_NAME", 100, 100, 300, 20),
            _region("广西佳润商贸有限公司", "WORK_UNIT", 100, 100, 300, 20),
        ]
        assert len(bqp.dedupe_same_value_regions(regions)) == 2

    def test_family_vs_nonfamily_overlap_untouched(self) -> None:
        # 家族框与家族外框同值重叠：家族框存活、家族外框不受影响
        regions = [
            _region("（2026）桂06执155号", "LEGAL_CASE_ID", 350, 791, 270, 18),
            _region("（2026）桂06执155号", "ORG", 350, 791, 270, 18),
        ]
        got = bqp.dedupe_same_value_regions(regions)
        assert len(got) == 2


# ---------------------------------------------------------------------------
# ④ 断行拆分合并（同值双紧框）
# ---------------------------------------------------------------------------

class TestWrappedMerge:
    def test_pair_merge_same_value_dual_tight_rects(self) -> None:
        # 基线 p4 实测几何：行尾（0.689,0.208）+ 次行行首（0.139,0.245）
        left = _region("（2021）桂0603民", "LEGAL_CASE_ID", 689, 208, 181, 18)
        right = _region("初2237号", "LEGAL_CASE_ID", 139, 245, 104, 17)
        got = bqp.merge_wrapped_number_regions([left, right])
        assert len(got) == 2
        # 值并、几何不动（union 否决：罩住两整行无辜文字）
        assert all(r.text == "（2021）桂0603民初2237号" for r in got)
        assert (got[0].left, got[0].top, got[0].width, got[0].height) == (689, 208, 181, 18)
        assert (got[1].left, got[1].top, got[1].width, got[1].height) == (139, 245, 104, 17)

    def test_reject_shape_mismatch(self) -> None:
        # 拼接不构成完整编号形态 → 不动
        left = _region("（2021）桂0603民", "LEGAL_CASE_ID", 689, 208, 181, 18)
        right = _region("某公司债务", "LEGAL_CASE_ID", 139, 245, 104, 17)
        got = bqp.merge_wrapped_number_regions([left, right])
        assert [r.text for r in got] == ["（2021）桂0603民", "某公司债务"]

    def test_reject_far_apart_rows(self) -> None:
        left = _region("（2021）桂0603民", "LEGAL_CASE_ID", 689, 208, 181, 18)
        right = _region("初2237号", "LEGAL_CASE_ID", 139, 400, 104, 17)
        got = bqp.merge_wrapped_number_regions([left, right])
        assert [r.text for r in got] == ["（2021）桂0603民", "初2237号"]

    def test_reject_reversed_order(self) -> None:
        # 右半在上方（阅读序颠倒）→ 不动
        top = _region("初2237号", "LEGAL_CASE_ID", 139, 100, 104, 17)
        bottom = _region("（2021）桂0603民", "LEGAL_CASE_ID", 689, 208, 181, 18)
        got = bqp.merge_wrapped_number_regions([top, bottom])
        assert [r.text for r in got] == ["初2237号", "（2021）桂0603民"]

    def test_reject_cross_type_pair(self) -> None:
        # 两半不同类型：不合并（类型路由交给 ①，拼接证据只对同类型成立）
        left = _region("（2021）桂0603民", "LEGAL_CASE_ID", 689, 208, 181, 18)
        right = _region("初2237号", "CASE_NUMBER", 139, 245, 104, 17)
        got = bqp.merge_wrapped_number_regions([left, right])
        assert [r.text for r in got] == ["（2021）桂0603民", "初2237号"]

    def test_adjacent_genuine_numbers_not_merged(self) -> None:
        # 负样本：上框已是完整编号、下框是另一完整编号 → 各自完整，不动
        top = _region("（2021）桂0603民初2237号", "LEGAL_CASE_ID", 139, 100, 300, 18)
        bottom = _region("（2023）桂06执异57号", "LEGAL_CASE_ID", 139, 135, 250, 18)
        got = bqp.merge_wrapped_number_regions([top, bottom])
        assert [r.text for r in got] == ["（2021）桂0603民初2237号", "（2023）桂06执异57号"]


# ---------------------------------------------------------------------------
# ③ 页脚页码过滤
# ---------------------------------------------------------------------------

class TestFooterPagenumberDrop:
    def test_footer_short_digit_dropped(self) -> None:
        # 基线 p3 实测：y=0.937（1000 高页面的 937）
        regions = [_region("3", "LEGAL_CASE_ID", 497, 937, 3, 13)]
        got = bqp.drop_footer_pagenumber_regions(regions, page_width=1000, page_height=1000)
        assert got == []

    def test_two_digit_pagenumber_dropped(self) -> None:
        regions = [_region("12", "LEGAL_CASE_ID", 497, 945, 20, 13)]
        assert bqp.drop_footer_pagenumber_regions(regions, 1000, 1000) == []

    def test_full_case_number_at_footer_kept(self) -> None:
        # 负样本（合成钉）：页脚位置的完整案号不是纯数字 → 存活
        regions = [_region("（2021）桂0603民初2237号", "LEGAL_CASE_ID", 200, 940, 300, 13)]
        got = bqp.drop_footer_pagenumber_regions(regions, 1000, 1000)
        assert len(got) == 1

    def test_body_short_digit_kept(self) -> None:
        # 正文区的孤立短数字不受页脚闸影响
        regions = [_region("3", "AMOUNT", 400, 500, 3, 13)]
        got = bqp.drop_footer_pagenumber_regions(regions, 1000, 1000)
        assert len(got) == 1

    def test_nonpure_digit_footer_kept(self) -> None:
        regions = [_region("第3页", "LEGAL_CASE_ID", 480, 940, 40, 13)]
        got = bqp.drop_footer_pagenumber_regions(regions, 1000, 1000)
        assert len(got) == 1

    def test_four_digit_value_kept(self) -> None:
        # 页码 3 位封顶：4 位数字（如年份残段）不在页脚闸射程内
        regions = [_region("2023", "LEGAL_CASE_ID", 480, 940, 40, 13)]
        got = bqp.drop_footer_pagenumber_regions(regions, 1000, 1000)
        assert len(got) == 1

    def test_zero_page_guard_fail_open(self) -> None:
        # PDF 文本层线拿不到页面尺寸（0,0）时必须 fail-open（不丢框）
        regions = [_region("3", "LEGAL_CASE_ID", 497, 937, 3, 13)]
        got = bqp.drop_footer_pagenumber_regions(regions, 0, 0)
        assert len(got) == 1


# ---------------------------------------------------------------------------
# 2a preset 正则收窄（命名组：前缀上下文不入实体）
# ---------------------------------------------------------------------------

PRESET_PATH = Path(__file__).resolve().parents[1] / "config" / "preset_entity_types.json"


class TestPresetLegalCaseIdRegex:
    _pattern: str | None = None

    @classmethod
    def _pat(cls) -> str:
        if cls._pattern is None:
            import json

            cls._pattern = json.loads(PRESET_PATH.read_text())["LEGAL_CASE_ID"]["regex_pattern"]
        return cls._pattern

    def _items(self, text: str) -> list[str]:
        import sys

        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
        from app.core.safe_regex import match_items, safe_compile, safe_finditer

        compiled = safe_compile(self._pat(), timeout=1.0)
        out = []
        for m in safe_finditer(compiled, text, timeout=5.0):
            out.extend(item for item, _s, _e in match_items(m))
        return out

    def test_prefix_not_in_value(self) -> None:
        # 旧 pattern 会把「防城区法院作出」整段吞入实体（#308 P1 根因 b）
        got = self._items("防城区法院作出（2021）桂0603民初2237号确认")
        assert got == ["（2021）桂0603民初2237号"]

    def test_bare_form_unchanged(self) -> None:
        got = self._items("（2021）桂0603民初2237号")
        assert got == ["（2021）桂0603民初2237号"]

    def test_inner_spaces_unchanged(self) -> None:
        got = self._items("（2023）桂 0603 民初 30号")
        assert got == ["（2023）桂 0603 民初 30号"]

    def test_series_range_still_not_matched(self) -> None:
        # preset 无（YYYY）前缀不命中——`2282号-2287号` 的捕获靠 NER，行为不变
        assert self._items("案号2282号-2287号涉案") == []

    def test_leading_whitespace_not_in_value(self) -> None:
        got = self._items("详见 （2021）桂0603民初2237号文书")
        assert got == ["（2021）桂0603民初2237号"]


# ---------------------------------------------------------------------------
# 接线钉：删接线判据必须红（仿 #302 范式，源码断言级）
# ---------------------------------------------------------------------------

class TestWiringNails:
    def _source(self, relpath: str) -> str:
        return (Path(__file__).resolve().parents[1] / "app" / "services" / relpath).read_text()

    def test_ocr_has_wires_both_lines_symmetrically(self) -> None:
        # I-2（MUT-1 变异实证）：存在性断言判不出「删一半接线」——按调用
        # 点计数：两线（图像/PDF 文本层）各 L1×2 + L2 dedupe×2 + merge×1。
        src = self._source("ocr_has_vision_service.py")
        assert src.count("box_quality_pass.normalize_number_shaped_entities(") == 2
        assert src.count("box_quality_pass.drop_fragment_entities(") == 2
        assert src.count("box_quality_pass.dedupe_same_value_regions(") == 4
        assert src.count("box_quality_pass.merge_wrapped_number_regions(") == 2
        # 懒 import 与调用点同数（删一线即少一处 import，同步红）
        assert src.count("from app.services.vision import box_quality_pass") == 4

    def test_vision_service_wires_footer_pass(self) -> None:
        # 页脚闸随 #335 收敛进组合入口（页脚+页眉+裸数字三闸一次接线），
        # 两处调用点不变：_filter_ocr_has_regions（图像线）+ _detect_with_pdf_text_layer
        src = self._source("vision_service.py")
        assert src.count("box_quality_pass.drop_edge_and_unanchored_number_regions(") == 2

    def test_passes_registered_in_module(self) -> None:
        # 五个 pass 都在 box_quality_pass 公开面（防实现被静默改名悬空接线）
        for name in (
            "normalize_number_shaped_entities",
            "drop_fragment_entities",
            "dedupe_same_value_regions",
            "merge_wrapped_number_regions",
            "drop_footer_pagenumber_regions",
        ):
            assert hasattr(bqp, name), name


# ---------------------------------------------------------------------------
# AMOUNT 块路径最小覆盖收缩（ocr_entity_match._crop_amount_block_region）
# ---------------------------------------------------------------------------

class TestAmountBlockCrop:
    @staticmethod
    def _block(text: str, *, with_chars: bool = True, recovered: bool = False):
        from test_issue43_case_number_floor import _block_with_chars

        block = _block_with_chars(text)
        if not with_chars:
            block.chars = None
        block.recovered = recovered
        return block

    @staticmethod
    def _amount_regions(block, entity_text: str):
        from app.services.vision.ocr_entity_match import match_entities_to_ocr

        return [r for r in match_entities_to_ocr([block], [{"type": "AMOUNT", "text": entity_text}])
                if r.entity_type == "AMOUNT"]

    def test_display_variant_crops_to_amount_span_when_provable(self) -> None:
        # 显示形态不同（￥20,1280元 vs 201280元）→ 块路径触发 → 有 chars 时
        # 收缩到金额自身跨度（#308 P1「金额框吞整句」的修复面）
        text = "合同总金额￥20,1280元相关费用另行计算"
        regions = self._amount_regions(self._block(text), "201280元")
        assert len(regions) == 1
        r = regions[0]
        assert r.text == "￥20,1280元"
        assert r.width < 10 * len(text), "必须紧裁到金额跨度，不得整块打码"

    def test_charsless_block_keeps_whole_block_overmask(self) -> None:
        # 无 chars 不可证明 → 整块 over-mask（never-uncover 底线不变）
        text = "合同总金额￥20,1280元相关费用另行计算"
        regions = self._amount_regions(self._block(text, with_chars=False), "201280元")
        assert len(regions) == 1
        assert regions[0].text == text
        assert regions[0].width == 10 * len(text)

    def test_recovered_block_never_whole_masks(self) -> None:
        # recovered 块不进块路径（数字巧合不得整块打码的既有守卫）
        text = "合同总金额￥20,1280元相关费用另行计算"
        regions = self._amount_regions(self._block(text, recovered=True), "201280元")
        assert regions == []


# ---------------------------------------------------------------------------
# 门⑥修复波钉（2026-11-15 评审 APPROVE with fixes：I-1/I-2/M-1/M-2/M-4/M-5）
# ---------------------------------------------------------------------------

class TestGate6FixWave:
    def test_non_code_shaped_pair_keeps_case_number(self) -> None:
        # I-1：非编号形态值（无（YYYY）、不以号收尾）的业务类型优先——
        # CASE_NUMBER 存活、LEGAL_CASE_ID 侧消解（设计稿 §L2① 原口径）
        regions = [
            _region("A4418271500002023060027", "LEGAL_CASE_ID", 100, 100, 300, 20),
            _region("A4418271500002023060027", "CASE_NUMBER", 100, 100, 298, 19),
        ]
        got = bqp.dedupe_same_value_regions(regions)
        assert len(got) == 1
        assert got[0].entity_type == "CASE_NUMBER"

    def test_manual_source_never_deduped(self) -> None:
        # M-2：人工框永存（#280 铁律语义）——识别期结构上不出现，防御性豁免
        regions = [
            _region("（2026）桂06执155号", "LEGAL_CASE_ID", 350, 791, 270, 18),
            _region("（2026）桂06执155号", "CASE_NUMBER", 350, 791, 268, 17, source="manual"),
        ]
        assert len(bqp.dedupe_same_value_regions(regions)) == 2

    def test_merge_rejects_head_containing_complete_core(self) -> None:
        # M-3：头半含完整编号核心+叙述后缀 → 是完整值不是断裂头半，禁拼接
        left = _region("（2021）桂0603民初2237号确认", "LEGAL_CASE_ID", 689, 208, 260, 18)
        right = _region("57号", "LEGAL_CASE_ID", 139, 245, 60, 17)
        got = bqp.merge_wrapped_number_regions([left, right])
        assert [r.text for r in got] == ["（2021）桂0603民初2237号确认", "57号"]

    def test_merge_rejects_tail_with_residue(self) -> None:
        # M-4（MUT-2 变异存活证据）：尾半「号后带残渣」拼接不构成完整编号 → 不并
        left = _region("（2021）桂0603民", "LEGAL_CASE_ID", 689, 208, 181, 18)
        right = _region("初2237号文书", "LEGAL_CASE_ID", 139, 245, 130, 17)
        got = bqp.merge_wrapped_number_regions([left, right])
        assert [r.text for r in got] == ["（2021）桂0603民", "初2237号文书"]

    def test_amount_multi_same_signature_runs_no_region(self) -> None:
        # M-1 修复波实证修订：块内同签名游程 >1 时 `_is_same_amount_value_block`
        # 的全块签名前置（compact 总数字内容==实体签名）结构性不成立 → 块路径
        # 根本不触发 → 既不整块 over-mask 也不裁一半（0 区域；评审描述的
        # 「改前双覆盖」不成立，守卫保留为纵深防御）。钉当前真实行为。
        text = "款项￥20,1280元与备用金20,1280元分别核算"
        regions = self_block_regions(text, "201280元")
        assert regions == []

    def test_projection_nail_trimmed_case_id_covers_core_only(self) -> None:
        # M-5（A2′e 适配）：未并值框的投影净度——紧裁框的覆盖 ⊇ 核心且
        # 不越过核心±一字宽。并值双紧框在 A4′ 另有几何钉（合同内在张力
        # 的适配口径：投影钉只对未并值框生效）。
        text = "经审理查明：双方确认（2021）桂0603民初2237号调解书有效"
        blocks = [_block_with_chars_for(text)]
        regions = match_regions(blocks, [{"type": "LEGAL_CASE_ID", "text": "（2021）桂0603民初2237号"}])
        assert len(regions) == 1
        r = regions[0]
        core_start = text.index("（2021）")
        core_end = core_start + len("（2021）桂0603民初2237号")
        # 字符框每字 10px：核心起止像素
        assert r.left >= core_start * 10 - 2
        assert r.left + r.width <= core_end * 10 + 2


def self_block_regions(text: str, entity_text: str):
    """有 chars 的块经 match_entities_to_ocr 的 AMOUNT 区域（块路径用例）。"""
    block = _block_with_chars_for(text)
    return match_regions([block], [{"type": "AMOUNT", "text": entity_text}])


def _block_with_chars_for(text: str):
    from test_issue43_case_number_floor import _block_with_chars

    return _block_with_chars(text)


def match_regions(blocks, entities):
    from app.services.vision.ocr_entity_match import match_entities_to_ocr

    return match_entities_to_ocr(blocks, entities)
