"""benchmark_t3 与 build_sample 单测：合成 manifest + mock 页图 + mock run_arm。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from eval.benchmarks.t3 import benchmark_t3, build_sample, spec


def make_manifest(tmp_path: Path, n: int = 2) -> list[dict]:
    return [
        {
            "page_id": f"vol{i}#p{i + 1}",
            "volume": f"volumes/vol{i}.pdf",
            "page": i + 1,
            "buckets": ["seal_clean"],
            "has_seal": True,
            "has_signature": False,
            "probe": {"la_boxes": 1, "mineru_seal_blocks": 1},
        }
        for i in range(n)
    ]


@pytest.fixture()
def pages_dir(tmp_path):
    from PIL import Image

    d = tmp_path / "pages"
    d.mkdir()
    for i in range(2):
        Image.new("RGB", (100, 80), "white").save(d / f"vol{i}#p{i + 1}.png")
    return d


class TestRunBenchmark:
    def test_ok_pages_and_metrics(self, pages_dir, monkeypatch):
        def fake_run(arm, image_path):
            assert Path(image_path).is_file()
            return [
                {"kind": "seal", "box": {"x": 1, "y": 1, "w": 10, "h": 10}, "confidence": 0.9, "raw": ""},
            ]

        monkeypatch.setattr(benchmark_t3, "run_arm", fake_run)
        metrics, details = benchmark_t3.run_benchmark(make_manifest(Path("/x")), pages_dir, ["la"])
        assert metrics["la"]["pages"] == 2
        assert metrics["la"]["failed_pages"] == 0
        assert metrics["la"]["boxes"] == 2
        assert metrics["la"]["by_kind"]["seal"] == 2
        assert metrics["la"]["arm_invalid"] is False
        assert all(d["status"] == "ok" for d in details)
        # 契约字段齐备
        for d in details:
            assert spec.validate_record(d) == []

    def test_failed_page_recorded_not_skipped(self, pages_dir, monkeypatch):
        def fake_run(arm, image_path):
            if "#p2" in image_path:
                raise RuntimeError("boom")
            return []

        monkeypatch.setattr(benchmark_t3, "run_arm", fake_run)
        metrics, details = benchmark_t3.run_benchmark(make_manifest(Path("/x")), pages_dir, ["la"])
        assert metrics["la"]["failed_pages"] == 1
        # 1/2=50% 失败 > 10%，必须判臂无效
        assert metrics["la"]["arm_invalid"] is True
        failed = [d for d in details if d["status"] == "failed"]
        assert len(failed) == 1 and "boom" in failed[0]["error"]

    def test_missing_page_image_is_failed(self, pages_dir, monkeypatch):
        manifest = make_manifest(Path("/x"))
        manifest.append(
            {**manifest[0], "page_id": "ghost#p9"}
        )
        monkeypatch.setattr(benchmark_t3, "run_arm", lambda a, p: [])
        metrics, details = benchmark_t3.run_benchmark(manifest, pages_dir, ["la"])
        failed = [d for d in details if d["status"] == "failed"]
        assert any("页图缺失" in d["error"] for d in failed)

    def test_limit_smoke(self, pages_dir, monkeypatch):
        monkeypatch.setattr(benchmark_t3, "run_arm", lambda a, p: [])
        metrics, details = benchmark_t3.run_benchmark(make_manifest(Path("/x")), pages_dir, ["la"], limit=1)
        assert metrics["la"]["pages"] == 1

    def test_empty_page_ok_record(self, pages_dir, monkeypatch):
        """零框页也是 ok 页（不是 failed）。"""
        monkeypatch.setattr(benchmark_t3, "run_arm", lambda a, p: [])
        metrics, details = benchmark_t3.run_benchmark(make_manifest(Path("/x")), pages_dir, ["la"])
        assert metrics["la"]["ok_pages"] == 2 and metrics["la"]["boxes"] == 0


class TestFindPageImage:
    def test_png_and_missing(self, pages_dir):
        assert benchmark_t3.find_page_image(pages_dir, "vol0#p1") is not None
        assert benchmark_t3.find_page_image(pages_dir, "ghost#p1") is None


class TestBuildSample:
    def test_from_la_eval_style(self, tmp_path):
        probe = tmp_path / "probe"
        probe.mkdir()
        (probe / "卷A#p3.json").write_text(
            json.dumps(
                {
                    "source": "testdata/vol/卷A.pdf",
                    "page_index": 3,
                    "width": 1000,
                    "height": 500,
                    "tasks": {
                        "seal": {"boxes": [{"x": 1}]},
                        "signature": {"boxes": []},
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        manifest = build_sample.build_manifest(probe)
        assert len(manifest) == 1
        entry = manifest[0]
        assert entry["page_id"] == "卷A#p3" and entry["page"] == 3
        assert entry["has_seal"] is True and entry["has_signature"] is False
        assert entry["buckets"] == ["seal_clean"]
        assert entry["probe"]["la_boxes"] == 1
        assert spec.validate_manifest(manifest) == []

    def test_from_generic_probe(self, tmp_path):
        probe = tmp_path / "probe"
        probe.mkdir()
        (probe / "summary.json").write_text(
            json.dumps(
                [
                    {"page_id": "卷B#p7", "volume": "vols/卷B.pdf", "page": 7,
                     "has_seal": True, "has_signature": True,
                     "la_boxes": 4, "mineru_seal_blocks": 2},
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        manifest = build_sample.build_manifest(probe)
        assert manifest[0]["buckets"] == ["seal_hard", "sig_hard"]
        assert manifest[0]["probe"] == {"la_boxes": 4, "mineru_seal_blocks": 2}
        assert spec.validate_manifest(manifest) == []

    def test_dedupe_and_unbucketed(self, tmp_path):
        probe = tmp_path / "probe"
        probe.mkdir()
        (probe / "a.json").write_text(
            json.dumps([{"page_id": "卷C#p1", "page": 1}], ensure_ascii=False), encoding="utf-8"
        )
        (probe / "b.json").write_text(
            json.dumps([{"page_id": "卷C#p1", "page": 1}], ensure_ascii=False), encoding="utf-8"
        )
        manifest = build_sample.build_manifest(probe)
        assert len(manifest) == 1
        assert manifest[0]["buckets"] == []  # 无命中 → 无桶

    def test_distribution(self):
        manifest = [
            {"buckets": ["seal_clean"]},
            {"buckets": ["seal_hard", "sig_hard"]},
            {"buckets": []},
        ]
        assert build_sample.bucket_distribution(manifest) == {
            "seal_clean": 1, "seal_hard": 1, "sig_hard": 1, "(unbucketed)": 1,
        }

    def test_validate_cli(self, tmp_path, capsys):
        good = tmp_path / "m.json"
        good.write_text(
            json.dumps(
                [{
                    "page_id": "v#p1", "volume": "", "page": 1, "buckets": ["seal_clean"],
                    "has_seal": True, "has_signature": False,
                }],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        assert build_sample.main(["--validate", str(good)]) == 0
        bad = tmp_path / "bad.json"
        bad.write_text(json.dumps([{"page_id": "v#p1"}]), encoding="utf-8")
        assert build_sample.main(["--validate", str(bad)]) == 1
