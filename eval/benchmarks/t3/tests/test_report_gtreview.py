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
        # seal_hard 召回 0% vs (all) 50% → 差 50pp ≥15 → 塌方判读出现
        assert "桶塌方" in md and "seal_hard" in md
        # FP=1, TP=1 → ratio 1.0 ≤ 2.0 不触发
        assert "误报上界" not in md
        assert "每臂接入成本" in md and "**la**" in md

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
