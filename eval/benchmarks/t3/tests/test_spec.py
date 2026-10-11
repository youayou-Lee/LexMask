"""spec.py 单测：IoU/贪心匹配/坐标换算/schema/判定阈值。合成数据，零外部服务。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))  # 仓库根，PYTHONPATH 免设

from eval.benchmarks.t3 import spec


def box(x, y, w, h):
    return {"x": x, "y": y, "w": w, "h": h}


def pred(x, y, w, h, conf=1.0):
    return {"box": box(x, y, w, h), "confidence": conf}


# ---- IoU -----------------------------------------------------------------


class TestIou:
    def test_identical(self):
        assert spec.iou(box(10, 10, 50, 50), box(10, 10, 50, 50)) == 1.0

    def test_disjoint(self):
        assert spec.iou(box(0, 0, 10, 10), box(100, 100, 10, 10)) == 0.0

    def test_half_overlap(self):
        # 交 50*50=2500，并 5000+5000-2500=7500
        assert spec.iou(box(0, 0, 100, 50), box(50, 0, 100, 50)) == 2500 / 7500

    def test_touching_edge(self):
        assert spec.iou(box(0, 0, 10, 10), box(10, 0, 10, 10)) == 0.0

    def test_degenerate(self):
        assert spec.iou(box(0, 0, 0, 0), box(0, 0, 10, 10)) == 0.0


# ---- 坐标换算 -------------------------------------------------------------


class TestCoordConversion:
    def test_la_1000_to_pixel(self):
        """LA 0-1000 制：x/1000*页宽（对齐 locate_anything_eval._parse_boxes）。"""
        from eval.benchmarks.t3.adapters import convert_1000_box

        assert convert_1000_box(100, 200, 300, 400, 1000, 500) == (100.0, 100.0, 200.0, 100.0)

    def test_la_1000_clamps(self):
        from eval.benchmarks.t3.adapters import convert_1000_box

        x, y, w, h = convert_1000_box(900, 900, 200, 200, 1000, 500)
        assert 0 <= x and x + w <= 1000
        assert 0 <= y and y + h <= 500

    def test_sigdet_01_to_pixel(self):
        from eval.benchmarks.t3.adapters import convert_01_box

        assert convert_01_box(0.5, 0.25, 0.25, 0.5, 800, 400) == (400.0, 100.0, 200.0, 200.0)

    def test_sigdet_clamps(self):
        from eval.benchmarks.t3.adapters import convert_01_box

        x, y, w, h = convert_01_box(0.9, 0.9, 0.5, 0.5, 100, 100)
        assert 0 <= x and x + w <= 100
        assert 0 <= y and y + h <= 100


# ---- 贪心匹配 -------------------------------------------------------------


class TestGreedyMatch:
    def test_perfect_match(self):
        p = [pred(0, 0, 10, 10)]
        g = [{"box": box(0, 0, 10, 10)}]
        matched, up, ug = spec.greedy_match(p, g)
        assert matched == [(0, 0)] and up == [] and ug == []

    def test_confidence_priority(self):
        """高置信预测先挑 GT；低置信的只能捡剩下或落空。"""
        g = [{"box": box(0, 0, 10, 10)}]
        low = pred(0, 0, 10, 10, conf=0.3)
        high = pred(0, 0, 10, 10, conf=0.9)
        matched, up, ug = spec.greedy_match([low, high], g)
        assert matched == [(1, 0)]
        assert up == [0] and ug == []

    def test_gt_occupied_once(self):
        """两个预测压同一 GT：只有一个命中。"""
        p = [pred(0, 0, 10, 10), pred(0, 0, 10, 10)]
        g = [{"box": box(0, 0, 10, 10)}]
        matched, up, ug = spec.greedy_match(p, g)
        assert len(matched) == 1 and len(up) == 1 and ug == []

    def test_below_threshold_no_match(self):
        p = [pred(0, 0, 10, 10)]
        g = [{"box": box(9, 0, 10, 10)}]  # IoU = 10*10 / 190 ≈ 0.09
        matched, up, ug = spec.greedy_match(p, g)
        assert matched == [] and up == [0] and ug == [0]

    def test_empty_sides(self):
        assert spec.greedy_match([], []) == ([], [], [])
        matched, up, ug = spec.greedy_match([], [{"box": box(0, 0, 1, 1)}])
        assert matched == [] and up == [] and ug == [0]
        matched, up, ug = spec.greedy_match([pred(0, 0, 1, 1)], [])
        assert matched == [] and up == [0] and ug == []

    def test_no_confidence_key_defaults_high(self):
        """MinerU/GT 无分数字段也参与匹配（预测侧缺 confidence 视为 1.0）。"""
        p = [{"box": box(0, 0, 10, 10)}]
        g = [{"box": box(0, 0, 10, 10)}]
        matched, _, _ = spec.greedy_match(p, g)
        assert matched == [(0, 0)]


# ---- schema 校验 ----------------------------------------------------------


class TestSchema:
    def valid_record(self, **over):
        record = {
            "page_id": "vol1#p3",
            "arm": "la",
            "kind": "seal",
            "box": box(1, 2, 3, 4),
            "confidence": 0.9,
            "raw": "",
        }
        record.update(over)
        return record

    def test_valid_record(self):
        assert spec.validate_record(self.valid_record()) == []

    def test_gt_arm_allowed(self):
        assert spec.validate_record(self.valid_record(arm="gt", confidence=1.0)) == []

    def test_missing_keys(self):
        errors = spec.validate_record({"page_id": "x"})
        assert any("arm" in e for e in errors) and any("box" in e for e in errors)

    def test_bad_arm(self):
        assert any("arm" in e for e in spec.validate_record(self.valid_record(arm="gpt")))

    def test_bad_kind(self):
        assert any("kind" in e for e in spec.validate_record(self.valid_record(kind="stamp")))

    def test_bad_box(self):
        assert spec.validate_record(self.valid_record(box={"x": 1, "y": 2, "w": -1, "h": 3}))
        assert spec.validate_record(self.valid_record(box={"x": 1, "y": 2, "w": 1}))

    def test_bad_confidence(self):
        assert spec.validate_record(self.valid_record(confidence=1.5))
        assert spec.validate_record(self.valid_record(confidence="0.9"))

    def test_manifest_valid(self):
        item = {
            "page_id": "v#p1",
            "volume": "a/b.pdf",
            "page": 1,
            "buckets": ["seal_clean"],
            "has_seal": True,
            "has_signature": False,
            "probe": {"la_boxes": 2, "mineru_seal_blocks": 1},
        }
        assert spec.validate_manifest([item]) == []

    def test_manifest_rejects(self):
        assert spec.validate_manifest("nope")
        assert spec.validate_manifest([{"page_id": "v#p1"}])
        dup = {
            "page_id": "v#p1", "volume": "", "page": 1,
            "buckets": ["bogus"], "has_seal": True, "has_signature": "yes",
        }
        errors = spec.validate_manifest([dup, dict(dup)])
        assert any("重复" in e for e in errors)
        assert any("buckets" in e for e in errors)
        assert any("has_signature" in e for e in errors)


# ---- 预注册阈值判定 --------------------------------------------------------


class TestJudgments:
    def test_collapse_vs_la_same_bucket(self):
        """预注册口径：候选臂桶召回较 **LA 同桶**召回下降 >15pp（同桶、LA 基线）。"""
        # LA 同桶 65%，候选 50% → 降 15pp 恰等 → 不算（>15 才算）
        assert spec.is_bucket_collapsed(50.0, 65.0) is False
        assert spec.is_bucket_collapsed(50.0, 65.1) is True
        assert spec.is_bucket_collapsed(80.0, 65.0) is False  # 候选更高不算塌方
        assert spec.is_bucket_collapsed(0.0, 100.0) is True

    def test_fp_cap_vs_la_baseline(self):
        """口径：候选臂误报/页 > 2 × LA 误报/页 → 突破；恰等不算。"""
        # LA 5 FP/20 页 = 0.25/页 → 上界 0.5/页；候选 9/20=0.45 过，11/20=0.55 破
        assert spec.is_fp_excessive(9, 20, 5, 20) is False
        assert spec.is_fp_excessive(11, 20, 5, 20) is True
        # 与 LA 同误报率 → 恰 2×，不算突破
        assert spec.is_fp_excessive(4, 20, 2, 20) is False
        # candidate_pages=0 → FP/页按 0 计
        assert spec.is_fp_excessive(0, 0, 5, 20) is False

    def test_fp_cap_la_zero_baseline_absolute_cap(self):
        """LA 误报/页=0 → 绝对上限 0.5/页（≤0.5 判过，>0.5 破）。"""
        assert spec.fp_cap_per_page(0, 20) == spec.FP_ABSOLUTE_CAP_PER_PAGE
        assert spec.is_fp_excessive(1, 20, 0, 20) is False  # 0.05 ≤ 0.5
        assert spec.is_fp_excessive(10, 20, 0, 20) is False  # 0.5 恰等上限 → 判过
        assert spec.is_fp_excessive(11, 20, 0, 20) is True  # 0.55 > 0.5
        assert spec.is_fp_excessive(9, 20, 0, 20) is False
        # LA 页数 0 → 基线按 0 → 绝对上限；0.5/4=0.5 恰等 → 判过
        assert spec.is_fp_excessive(2, 4, 0, 0) is False
        assert spec.is_fp_excessive(3, 4, 0, 0) is True

    def test_fp_per_page(self):
        assert spec.fp_per_page(6, 3) == 2.0
        assert spec.fp_per_page(6, 0) == 0.0

    def test_arm_invalid_at_10pct(self):
        assert spec.is_arm_invalid(11, 100) is True
        assert spec.is_arm_invalid(10, 100) is False  # 恰 10% 不算
        assert spec.is_arm_invalid(1, 9) is True
        assert spec.is_arm_invalid(0, 0) is True

    def test_score_arm_kind(self):
        p = [pred(0, 0, 10, 10), pred(100, 100, 10, 10)]
        g = [{"box": box(0, 0, 10, 10)}, {"box": box(50, 50, 10, 10)}]
        assert spec.score_arm_kind(p, g) == {"tp": 1, "fp": 1, "fn": 1}
