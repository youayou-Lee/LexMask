"""report_t3 / gt_review 单测：合成明细 + GT，验证聚合、判读文本、HTML/导出。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from eval.benchmarks.t3 import gt_review, report_t3


def row(page_id, arm, kind, x, y, w, h, conf=1.0, status="ok"):
    return {
        "page_id": page_id, "arm": arm, "kind": kind,
        "box": {"x": x, "y": y, "w": w, "h": h},
        "confidence": conf, "raw": "", "status": status, "elapsed_ms": 100,
        "error": "",
    }


@pytest.fixture()
def synthetic(tmp_path):
    """3 页 2 桶：p1 GT seal（la 命中 + 1 误报）、p2 GT signature（la 全漏）、
    p3 GT seal（seal_hard 桶，la 全漏）——制造 seal_hard 桶塌方场景。"""
    gt = [
        row("v#p1", "gt", "seal", 10, 10, 50, 50),
        row("v#p2", "gt", "signature", 100, 100, 60, 30),
        row("v#p3", "gt", "seal", 40, 40, 50, 50),
    ]
    details = [
        row("v#p1", "la", "seal", 12, 12, 48, 48, conf=0.9),   # TP
        row("v#p1", "la", "seal", 500, 500, 30, 30),           # FP
        # p2 signature、p3 seal：la 一框都没有
    ]
    metrics = {
        "metrics": {
            "la": {"pages": 3, "ok_pages": 2, "failed_pages": 0, "failed_ratio": 0.0,
                   "arm_invalid": False, "total_elapsed_s": 0.2, "avg_page_elapsed_s": 0.1,
                   "boxes": 2, "by_kind": {"seal": 2, "signature": 0}},
        }
    }
    manifest = [{"page_id": "v#p1", "buckets": ["seal_clean"], "volume": "", "page": 1,
                 "has_seal": True, "has_signature": False},
                {"page_id": "v#p2", "buckets": ["sig_hard"], "volume": "", "page": 2,
                 "has_seal": False, "has_signature": True},
                {"page_id": "v#p3", "buckets": ["seal_hard"], "volume": "", "page": 3,
                 "has_seal": True, "has_signature": False}]
    return gt, details, metrics, manifest


class TestAggregate:
    def test_tp_fp_fn_counts(self, synthetic):
        gt, details, _, manifest = synthetic
        buckets = {m["page_id"]: m["buckets"] for m in manifest}
        cells, latency = report_t3.aggregate(details, gt, buckets)
        assert cells[("la", "seal_clean", "seal")] == {"tp": 1, "fp": 1, "fn": 0}
        assert cells[("la", "sig_hard", "signature")] == {"tp": 0, "fp": 0, "fn": 1}
        assert cells[("la", "seal_hard", "seal")] == {"tp": 0, "fp": 0, "fn": 1}
        assert cells[("la", "(all)", "seal")]["tp"] == 1
        assert latency["la"] == 100.0

    def test_failed_page_counts_all_fn(self, synthetic):
        gt, details, _, manifest = synthetic
        details = [
            row("v#p1", "la", "", 0, 0, 0, 0, status="failed", conf=0),  # 整页失败
        ]
        buckets = {m["page_id"]: m["buckets"] for m in manifest}
        cells, _ = report_t3.aggregate(details, gt, buckets)
        # seal：p1 整页失败 fn+1，p3 未跑（无预测）fn+1 → (all) seal fn=2
        assert cells[("la", "(all)", "seal")] == {"tp": 0, "fp": 0, "fn": 2}
        assert cells[("la", "(all)", "signature")] == {"tp": 0, "fp": 0, "fn": 1}


class TestRender:
    def test_verdicts_and_table(self, synthetic):
        gt, details, metrics, manifest = synthetic
        buckets = {m["page_id"]: m["buckets"] for m in manifest}
        cells, latency = report_t3.aggregate(details, gt, buckets)
        md = report_t3.render_markdown(
            cells, latency, metrics, buckets, "manifest.private.json"
        )
        assert "分桶 × 臂 × kind" in md
        assert "sig_hard" in md and "seal_clean" in md
        # 只有 la：LA 是基线，塌方/误报上界判读均跳过（只给数据行）
        assert "桶塌方" not in md
        assert "误报上界" not in md
        # 分桶 FP 只展示：seal_clean 桶行有 FP/页 列
        assert "FP/页" in md
        assert "每臂接入成本" in md and "**la**" in md

    def test_collapse_verdict_vs_la_same_bucket(self, synthetic):
        """塌方判读：候选臂 vs LA 同桶（>15pp 降）；LA 自身行不判。"""
        gt, details, metrics, manifest = synthetic
        # la 命中 p3（seal_hard 桶召回 100%），sigdet 全漏（seal_hard 0%）→ 降 100pp
        la_hit_p3 = row("v#p3", "la", "seal", 42, 42, 46, 46, conf=0.9)
        sigdet_details = [
            row("v#p1", "sigdet", "seal", 900, 900, 30, 30),
        ]
        metrics = {
            "metrics": {
                "la": {**metrics["metrics"]["la"], "avg_page_elapsed_s": 4.476},
                "sigdet": {**metrics["metrics"]["la"], "pages": 3, "avg_page_elapsed_s": 100.439},
            }
        }
        buckets = {m["page_id"]: m["buckets"] for m in manifest}
        cells, _ = report_t3.aggregate(sigdet_details + details + [la_hit_p3], gt, buckets)
        md = report_t3.render_markdown(cells, {"la": 1.0, "sigdet": 1.0}, metrics, buckets, None)
        assert "**桶塌方** seal/seal_hard" in md
        assert "较 LA 同桶 100.0% 下降 100.0pp" in md
        # LA 自身行只作基线（不判塌方）
        assert "- **la**：基线臂" in md
        # 接入成本：实测单页均值动态带出，sigdet 不再写死 ~ms 级
        assert "实测单页均值 4476 ms" in md
        assert "实测单页均值 100439 ms" in md
        assert "~ms 级" not in md

    def test_fp_verdict_against_la_baseline(self, synthetic):
        """候选臂 FP/页 > 2 × LA FP/页 → 判读触发；≤ 上界不触发。"""
        gt, details, metrics, manifest = synthetic
        # la: FP=1/3 页 = 0.333/页 → 上界 0.667；sigdet 给 3 FP/3 页 = 1.0 → 破
        sigdet_details = [
            row("v#p1", "sigdet", "seal", 900, 900, 30, 30),
            row("v#p2", "sigdet", "seal", 900, 900, 30, 30),
            row("v#p3", "sigdet", "seal", 900, 900, 30, 30),
        ]
        metrics = {
            "metrics": {
                "la": metrics["metrics"]["la"],
                "sigdet": {**metrics["metrics"]["la"], "pages": 3},
            }
        }
        buckets = {m["page_id"]: m["buckets"] for m in manifest}
        cells, _ = report_t3.aggregate(sigdet_details + details, gt, buckets)
        md = report_t3.render_markdown(cells, {"la": 1.0, "sigdet": 1.0}, metrics, buckets, None)
        assert "**误报上界** seal" in md and "sigdet" in md

        # sigdet 降到 1 FP/3 页 ≈ 0.333 ≤ 上界 0.667 → 不触发
        sigdet_details2 = sigdet_details[:1]
        cells2, _ = report_t3.aggregate(sigdet_details2 + details, gt, buckets)
        md2 = report_t3.render_markdown(cells2, {"la": 1.0, "sigdet": 1.0}, metrics, buckets, None)
        assert "**误报上界**" not in md2

    def test_end_to_end_cli(self, synthetic, tmp_path):
        gt, details, metrics, manifest = synthetic
        mdir = tmp_path / "out"
        mdir.mkdir()
        (mdir / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False), encoding="utf-8")
        (mdir / "details.jsonl").write_text(
            "".join(json.dumps(d, ensure_ascii=False) + "\n" for d in details), encoding="utf-8"
        )
        gt_path = tmp_path / "gt.jsonl"
        gt_path.write_text(
            "".join(json.dumps(d, ensure_ascii=False) + "\n" for d in gt), encoding="utf-8"
        )
        man_path = tmp_path / "manifest.json"
        man_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        rc = report_t3.main(
            ["--metrics", str(mdir / "metrics.json"), "--details", str(mdir / "details.jsonl"),
             "--gt", str(gt_path), "--manifest", str(man_path), "--out", str(mdir / "report.md")]
        )
        assert rc == 0
        md = (mdir / "report.md").read_text(encoding="utf-8")
        assert "成绩单" in md and "自动判读" in md


class TestArmNotes:
    """门⑥ 冷审 Critical/Important 的回归钉：C1/C2/I1/I2。"""

    def test_gtless_fp_counted_once_per_arm(self):
        """C1：无 GT 的 (page,kind) 预测只按**自己那一臂**记一次 FP。

        旧实现把该累加块放在 `for arm in arms:` 内，每个臂跑一遍 → 同一预测
        被记 N_arms 次（4 臂时 fp=4），且各臂放大倍数不同可让误报门翻面。
        """
        gt = [row("v#p1", "gt", "seal", 10, 10, 50, 50),
              row("v#p2", "gt", "signature", 100, 100, 60, 30)]
        details = [
            row("v#p1", "la", "seal", 12, 12, 48, 48, conf=0.9),          # TP
            row("v#p2", "la", "seal", 800, 400, 40, 40),                  # p2 无 seal GT → FP
            row("v#p2", "sigdet", "seal", 800, 400, 40, 40),              # 另一臂同页同 kind
            row("v#p2", "paddlex_seal", "seal", 800, 400, 40, 40),
            row("v#p2", "mineru_seal", "seal", 800, 400, 40, 40),
        ]
        buckets = {"v#p1": ["seal_clean"], "v#p2": ["sig_hard"]}
        cells, _ = report_t3.aggregate(details, gt, buckets)
        for arm in ("la", "sigdet", "paddlex_seal", "mineru_seal"):
            assert cells[(arm, "(all)", "seal")]["fp"] == 1, arm  # 旧代码 = 4
        # 分桶同样只记一次
        for arm in ("sigdet", "paddlex_seal", "mineru_seal"):
            assert cells[(arm, "sig_hard", "seal")]["fp"] == 1, arm

    def test_no_manifest_does_not_double_count_all(self, synthetic):
        """I1：缺 manifest / 无桶页时 (all) 不得被计两次（tp/fp/fn 翻倍）。"""
        gt, details, _, manifest = synthetic
        empty = report_t3.aggregate(details, gt, {})[0]
        unbucketed = report_t3.aggregate(
            details, gt, {m["page_id"]: [] for m in manifest}
        )[0]
        for cells in (empty, unbucketed):
            assert cells[("la", "(all)", "seal")] == {"tp": 1, "fp": 1, "fn": 1}
            assert cells[("la", "(all)", "signature")] == {"tp": 0, "fp": 0, "fn": 1}

    def test_all_row_renders_fp_per_page(self, synthetic):
        """I2：主表 (all) 行的 FP/页 必须渲染（误报门判据用的就是这个量）。"""
        gt, details, metrics, manifest = synthetic
        buckets = {m["page_id"]: m["buckets"] for m in manifest}
        cells, latency = report_t3.aggregate(details, gt, buckets)
        md = report_t3.render_markdown(cells, latency, metrics, buckets, "manifest.private.json")
        all_rows = [ln for ln in md.splitlines() if ln.startswith("| (all) |")]
        assert len(all_rows) == 2  # seal / signature 各一行
        seal_row = next(ln for ln in all_rows if "TP=1" in ln)
        assert " / 0.33 (" in seal_row  # la: 1 FP / 3 页 = 0.333 → 不再显示 "-"
        assert " / - (" not in seal_row

    def test_missing_la_baseline_never_passes(self):
        """C2：metrics 无 la 臂时不得打印「通过全部预注册判据」。

        注意用「候选臂有明细」的构造：否则会被「无明细记录」守卫遮住，
        测不出基线缺失分支本身（旧实现直接跳过两条检查照打「通过全部预注册阈值」）。
        """
        gt = [row("v#p1", "gt", "seal", 10, 10, 50, 50)]
        details = [row("v#p1", "mineru_seal", "seal", 11, 11, 49, 49)]
        metrics = {
            "metrics": {
                "mineru_seal": {"pages": 1, "ok_pages": 1, "failed_pages": 0,
                                "avg_page_elapsed_s": 3.4, "by_kind": {"seal": 1, "signature": 0}},
            }
        }
        buckets = {"v#p1": ["seal_clean"]}
        cells, latency = report_t3.aggregate(details, gt, buckets)
        notes = report_t3.arm_notes(
            "mineru_seal", cells, metrics["metrics"], ["seal_clean", "(all)"]
        )
        assert any("基线缺失" in n for n in notes)
        md = report_t3.render_markdown(cells, latency, metrics, buckets, None)
        assert "基线缺失" in md
        assert "通过全部预注册判据" not in md

    def test_recall_and_cost_criteria_encode_issue_rules(self):
        """C2：Issue 首要判据「候选召回 ≥ LA」「单页成本不高于 LA」必须落成判读。"""
        gt = [row("v#p1", "gt", "seal", 10, 10, 50, 50),
              row("v#p2", "gt", "seal", 200, 200, 50, 50)]
        details = [
            row("v#p1", "la", "seal", 12, 12, 48, 48),
            row("v#p2", "la", "seal", 202, 202, 48, 48),                 # la 召回 100%
            row("v#p1", "sigdet", "seal", 800, 400, 40, 40, conf=0.5),   # 召回 0% + 1 FP
        ]
        metrics = {"metrics": {
            "la": {"pages": 2, "failed_pages": 0, "avg_page_elapsed_s": 4.0,
                   "by_kind": {"seal": 2, "signature": 0}},
            "sigdet": {"pages": 2, "failed_pages": 0, "avg_page_elapsed_s": 100.0,
                       "by_kind": {"seal": 1, "signature": 0}},
        }}
        buckets = {"v#p1": ["seal_clean"], "v#p2": ["seal_clean"]}
        cells, latency = report_t3.aggregate(details, gt, buckets)
        md = report_t3.render_markdown(cells, latency, metrics, buckets, None)
        assert "**召回低于 LA** seal：0.0% < 100.0%" in md
        assert "（不满足「召回 ≥ LA」）" in md
        assert "**单页成本高于 LA**：100000 ms > 4000 ms" in md
        assert "通过全部预注册判据" not in md

    def test_clean_candidate_passes_all_criteria(self):
        """正例：召回≥LA、无塌方、误报≤上界、成本更低 → 判「通过全部预注册判据」。"""
        gt = [row("v#p1", "gt", "seal", 10, 10, 50, 50),
              row("v#p2", "gt", "seal", 200, 200, 50, 50)]
        details = [
            row("v#p1", "la", "seal", 12, 12, 48, 48),
            row("v#p1", "la", "seal", 900, 900, 20, 20),                 # la 1 FP/2 页
            row("v#p2", "la", "seal", 202, 202, 48, 48),
            row("v#p1", "mineru_seal", "seal", 11, 11, 49, 49),          # 召回 100%
            row("v#p2", "mineru_seal", "seal", 201, 201, 49, 49),
        ]
        metrics = {"metrics": {
            "la": {"pages": 2, "failed_pages": 0, "avg_page_elapsed_s": 4.476,
                   "by_kind": {"seal": 3, "signature": 0}},
            "mineru_seal": {"pages": 2, "failed_pages": 0, "avg_page_elapsed_s": 3.398,
                            "by_kind": {"seal": 2, "signature": 0}},
        }}
        buckets = {"v#p1": ["seal_clean"], "v#p2": ["seal_clean"]}
        cells, latency = report_t3.aggregate(details, gt, buckets)
        md = report_t3.render_markdown(cells, latency, metrics, buckets, None)
        assert "**mineru_seal**：通过全部预注册判据" in md
        assert "召回低于 LA" not in md and "桶塌方" not in md


class TestGtReview:
    def test_build_html_embeds_prefill(self, tmp_path, synthetic):
        gt, details, _, _ = synthetic
        pages = tmp_path / "pages"
        pages.mkdir()
        from PIL import Image

        Image.new("RGB", (200, 100), "white").save(pages / "v#p1.png")
        prefill_path = tmp_path / "prefill.jsonl"
        prefill_path.write_text(
            "".join(json.dumps(d, ensure_ascii=False) + "\n" for d in gt), encoding="utf-8"
        )
        out = tmp_path / "review.html"
        assert gt_review.main(["build", "--pages-dir", str(pages), "--prefill", str(prefill_path),
                               "--out", str(out)]) == 0
        html_text = out.read_text(encoding="utf-8")
        assert "v#p1" in html_text and '"seal"' in html_text
        assert "exportGt" in html_text

    def test_build_html_src_relative_to_out(self, tmp_path):
        """Important2：HTML 落 pages 目录外时 src 必须是相对 out 的路径（不 404）。"""
        from PIL import Image

        pages = tmp_path / "pages"
        pages.mkdir()
        Image.new("RGB", (100, 80), "white").save(pages / "v#p1.png")
        out = tmp_path / "review" / "gt_review.html"
        out.parent.mkdir()
        html_text = gt_review.build_html(str(pages), {}, out_path=str(out))
        assert '"src": "../pages/v#p1.png"' in html_text
        # 不传 out_path（向后兼容）：src 相对 pages 目录本身
        html2 = gt_review.build_html(str(pages), {})
        assert '"src": "v#p1.png"' in html2

    def test_emitted_html_css_is_valid(self, tmp_path):
        """I4：模板 CSS 必须是可用单花括号。

        旧模板用双花括号（`body {{ … }}`）却只做 `.replace('__PAGES_DATA__')`、
        从不 `.format()`，产出 HTML 里每条 CSS 都带 `{{ }}` → `svg.overlay
        { position: absolute }` 失效、叠加框与页图脱位。
        """
        from PIL import Image

        pages = tmp_path / "pages"
        pages.mkdir()
        Image.new("RGB", (100, 80), "white").save(pages / "v#p1.png")
        html_text = gt_review.build_html(str(pages), {}, out_path=str(tmp_path / "gt.html"))
        assert "{{" not in html_text and "}}" not in html_text
        assert "svg.overlay { position: absolute" in html_text
        assert ".wrap { position: relative" in html_text

    def test_prefill_arm_filter_skips_failed(self, tmp_path):
        rows = [
            row("v#p1", "la", "seal", 1, 1, 5, 5),
            row("v#p1", "sigdet", "signature", 2, 2, 5, 5),
            row("v#p2", "la", "seal", 3, 3, 5, 5, status="failed"),
        ]
        path = tmp_path / "d.jsonl"
        path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
        prefill = gt_review.collect_prefill(str(path), arm="la")
        assert list(prefill) == ["v#p1"] and len(prefill["v#p1"]) == 1

    def test_export_validates(self, tmp_path, synthetic):
        gt, _, _, _ = synthetic
        bad = tmp_path / "bad.jsonl"
        bad.write_text(
            json.dumps({"page_id": "x", "arm": "gpt", "kind": "seal",
                        "box": {"x": 1, "y": 1, "w": 1, "h": 1},
                        "confidence": 1.0, "raw": ""}, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        assert gt_review.main(["export", "--downloaded", str(bad), "--out", str(tmp_path / "gt.jsonl")]) == 1
        good = tmp_path / "good.jsonl"
        good.write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in gt), encoding="utf-8"
        )
        assert gt_review.main(["export", "--downloaded", str(good), "--out", str(tmp_path / "gt.jsonl")]) == 0
