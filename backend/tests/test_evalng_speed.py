"""eval-ng speedkit（Issue #131）单测：信号抽取/闭合校验/报告自足/驱动判定/CLI 用法/脱敏扫描。

零网络、零真端点：所有响应结构由 fixture 造。
"""

import json
from pathlib import Path

import evalng_bootstrap as boot
import pytest

collect = boot.load("engine/speedkit/collect.py")
driver = boot.load("engine/speedkit/driver.py")
report = boot.load("engine/speedkit/report.py")
arms = boot.load("engine/speedkit/arms.py")
cli = boot.load("cli.py")
redact = boot.load("scripts/check_report_redaction.py")

RING_LEAVES = {"prepare": 100.0, "ocr": 300.0, "has_ner": 400.0, "match": 80.0, "draw": 120.0}


def _single_resp(total: float = 1000.0, ring: dict | None = None) -> dict:
    ring = dict(ring or RING_LEAVES)
    ring["total"] = round(sum(ring.values()), 3)  # 管线 total = 环节之和（页根 total 独立）
    return {
        "page": 3,
        "duration_ms": {"total": total, "request_total_ms": total + 5.0},
        "pipeline_status": {"ocr_has": {"stage_duration_ms": ring}},
    }


def _batch_page(total: float = 1000.0, ring: dict | None = None, k: int = 3) -> dict:
    ring = dict(ring or RING_LEAVES)
    ring["total"] = total
    breakdown = {f"ocr_has.{key}": value for key, value in ring.items()}
    return {
        "page": 7,
        "duration_ms": total,
        "duration_breakdown_ms": breakdown,
        "cache_status": {"has_text_cache_status_hit_before_slot": 1},
        "page_concurrency": k,
        "page_concurrency_configured": k,
    }


def _signal(total: float = 1000.0, wall_s: float = 1.0, ring: dict | None = None) -> dict:
    return collect.page_signal(_single_resp(total, ring), wall_s=wall_s, page_no=3)


# ── collect：信号抽取与聚合 ────────────────────────────────────────────────
class TestCollect:
    def test_single_signal_keys(self):
        sig = _signal()
        assert sig["root_total_ms"] == 1000.0
        assert sig["request_total_ms"] == 1005.0
        assert sig["page"] == 3
        assert sig["ring_keys"] == sorted([f"ocr_has.{k}" for k in RING_LEAVES] + ["ocr_has.total"])
        assert sig["pipeline_totals_ms"] == {"ocr_has": 1000.0}

    def test_single_signal_harness_span(self):
        sig = collect.page_signal(_single_resp(), wall_s=1.0, harness_span=(10.0, 11.0), page_no=1)
        assert sig["harness_start"] == 10.0 and sig["harness_end"] == 11.0

    def test_cache_status_histogram(self):
        resp = _single_resp()
        resp["pipeline_status"]["ocr_has"]["stage_duration_ms"]["has_text_cache_status"] = "hit_before_slot"
        resp["pipeline_status"]["ha_s"] = {"stage_duration_ms": {"has_text_cache_status": "skipped_no_text"}}
        sig = collect.page_signal(resp, wall_s=1.0, page_no=1)
        assert sig["cache_hits"] == 1
        assert sig["cache_status"] == {"hit_before_slot": 1, "skipped_no_text": 1}
        assert sig["skipped_has"] is False  # 有非 skipped 状态 ⇒ 不是全 skipped

    def test_sum_pages_residual_source(self):
        agg = collect.sum_pages([_signal(total=1000.0, wall_s=1.0)])
        assert agg["unclassified_ms"] == 0.0
        assert agg["residual_ratio"] == 0.0
        assert agg["unclassified_source"] == collect.RESIDUAL_SOURCE
        assert agg["sum_rings_ms"] == 1000.0
        assert agg["harness_ratio"] == 1.0

    def test_sum_pages_positive_residual(self):
        ring = {k: v / 2 for k, v in RING_LEAVES.items()}  # 环和 500，页总 1000
        agg = collect.sum_pages([_signal(total=1000.0, wall_s=1.0, ring=ring)])
        assert agg["sum_rings_ms"] == 500.0
        assert agg["unclassified_ms"] == 500.0
        assert agg["residual_ratio"] == 0.5

    def test_sum_pages_negative_residual_is_not_clamped(self):
        ring = {k: v * 2 for k, v in RING_LEAVES.items()}  # 环和 2000 > 页总 1000
        agg = collect.sum_pages([_signal(total=1000.0, wall_s=1.0, ring=ring)])
        assert agg["unclassified_ms"] == -1000.0  # 负数必须如实暴露（由 closure_checks 判 FAIL）

    def test_closure_checks_same_clock(self):
        agg = collect.sum_pages([_signal(total=1000.0, wall_s=1.0)])
        checks = {c["id"]: c for c in collect.closure_checks(
            agg, harness_seq=[1005.0], backend_seq=[1000.0])}
        assert checks["D3.closure_same_clock"]["status"] == "PASS"
        assert checks["D3.residual_two_sided"]["status"] == "PASS"

    def test_closure_checks_flags_copied_series(self):
        agg = collect.sum_pages([_signal(total=1000.0, wall_s=1.0)])
        checks = {c["id"]: c for c in collect.closure_checks(
            agg, harness_seq=[1000.0], backend_seq=[1000.0])}
        assert checks["D3.cross_clock_not_copied"]["status"] == "FAIL"

    def test_closure_checks_flags_negative_residual(self):
        ring = {k: v * 2 for k, v in RING_LEAVES.items()}
        agg = collect.sum_pages([_signal(total=1000.0, wall_s=1.0, ring=ring)])
        checks = {c["id"]: c for c in collect.closure_checks(agg)}
        assert checks["D3.residual_two_sided"]["status"] == "FAIL"
        assert checks["D3.cross_clock_not_copied"]["status"] == "UNVERIFIED"

    def test_closure_checks_flags_harness_out_of_band(self):
        agg = collect.sum_pages([_signal(total=1000.0, wall_s=2.0)])  # harness 2000ms vs 1000ms
        checks = {c["id"]: c for c in collect.closure_checks(
            agg, harness_seq=[2000.0], backend_seq=[1000.0])}
        assert checks["D3.cross_clock_not_copied"]["status"] == "FAIL"

    def test_ring_keys_present(self):
        agg = collect.sum_pages([_signal()])
        checks = {c["id"]: c for c in collect.key_placement_checks(agg, "single")}
        assert checks["D3.ring_keys_present"]["status"] == "PASS"
        ring = {"prepare": 1.0, "total": 1.0}
        agg2 = collect.sum_pages([_signal(total=1.0, wall_s=0.001, ring=ring)])
        checks2 = {c["id"]: c for c in collect.key_placement_checks(agg2, "single")}
        assert checks2["D3.ring_keys_present"]["status"] == "FAIL"

    def test_key_placement_skips_batch(self):
        agg = collect.sum_pages([collect.batch_page_signal(_batch_page())])
        assert collect.key_placement_checks(agg, "batch") == []

    def test_batch_signal(self):
        sig = collect.batch_page_signal(_batch_page())
        assert sig["root_total_ms"] == 1000.0
        assert sig["pipeline_totals_ms"] == {"ocr_has": 1000.0}
        assert sig["cache_hits"] == 1
        assert sig["page_concurrency"] == 3
        assert sig["wall_s"] is None

    def test_batch_peak_concurrency(self):
        pages = [collect.batch_page_signal(_batch_page(k=2)), collect.batch_page_signal(_batch_page(k=4))]
        agg = collect.sum_pages(pages)
        assert agg["page_concurrency_peak"] == 4
        assert agg["page_concurrency_values"] == [2, 4]

    def test_recorded_pair_sequences_two_columns(self):
        harness, backend = collect.recorded_pair_sequences([
            [_signal(total=1000.0, wall_s=1.0), _signal(total=1000.0, wall_s=1.1)],
            [_signal(total=1000.0, wall_s=1.2)],
        ])
        assert backend == [1000.0, 1000.0, 1000.0]
        assert harness == [1000.0, 1100.0, 1200.0]

    def test_numeric_leaves_excludes_bool(self):
        leaves = collect.numeric_leaves({"a": True, "b": 1, "c": {"d": 2.5}, "e": [1, "x"]})
        assert leaves == {"b": 1.0, "c.d": 2.5, "e[0]": 1.0}

    def test_criteria_refs_roundtrip(self):
        doc = {"summary": {"closure_ratio": 0.001, "file_wall_s": {"p50": 12.5}}}
        spec = {"D3.closure_same_clock": "summary.closure_ratio",
                "D4.file_wall_p50": "summary.file_wall_s.p50"}
        refs = collect.criteria_refs(doc, spec)
        assert refs["D3.closure_same_clock"]["value"] == 0.001
        assert refs["D4.file_wall_p50"]["value"] == 12.5
        assert collect.validate_criteria_refs({**doc, "criteria_refs": refs}) == []


# ── report：自足/白名单/判定 ───────────────────────────────────────────────
def _footnote(**overrides) -> dict:
    doc = {
        "instance": "local",
        "hardware": "cpu-8c",
        "image_dtk": "dev-image/no-dtk",
        "code_rev": "0" * 40,
        "switches": "TEXT_LAYER_DOCLINE_ENABLED=false",
        "manifest_hash": "abc123",
        "window": "2026-10-16 10:00-11:00 local",
        "readiness": "ready=30s settle=10s",
    }
    doc.update(overrides)
    return doc


def _report_envelope() -> dict:
    return {
        "schema_version": report.SCHEMA_VERSION,
        "module": {"name": "eval-ng speedkit", "entry": "eval-ng/cli.py speed"},
        "generated_at": report.utcnow(),
        "started_at": "2026-10-16T10:00:00Z",
        "finished_at": "2026-10-16T11:00:00Z",
        "exam": {"exam_id": "unit", "suite": "synthetic", "corpus": []},
        "no_gt": True,
        "quality_eval": "not_run",
        "mode": {"driver": "single", "page_concurrency": 3, "cache_policy": "cold",
                 "rounds": 4, "warmup_rounds": 1, "timing_rounds": 3,
                 "api_base": "http://127.0.0.1:8000", "suite": "synthetic",
                 "install": "unit-branch@deadbeef"},
        "arm": {"name": "unit-arm", "kind": "env"},
        "rounds": [],
        "cold_evidence": [],
        "readiness": {"ready_wait_sec": 30.0, "settle_sec": 10.0},
        "summary": {"file_wall_s": {"mean": 1.0, "p50": 1.0, "p95": 1.0, "n": 3},
                    "pages_per_min": 60.0, "harness_ratio": 1.05,
                    "cache_hits": 0, "closure_ratio": 0.001, "residual_ratio": 0.01,
                    "wall_agreement": {"max_abs_ratio_diff": 0.01},
                    "parallel_efficiency": 0.9},
        "cache": {"histogram": {}, "hits": 0},
        "footnote": _footnote(),
        "fingerprints": report.build_fingerprints(api_base="http://127.0.0.1:8000"),
        "checks": [],
        "not_measured": [],
        "reproduce": {"command": ["python", "eval-ng/cli.py", "speed", "exam.json"],
                      "code_rev": "0" * 40, "cwd": "repo"},
        "verdict": "PASS",
        "criteria_refs": {},
    }


class TestReport:
    def test_footnote_eight_keys_pass(self):
        fp = report.build_fingerprints(api_base="http://127.0.0.1:8000")
        errors = report.validate_footnote(
            _footnote(), api_base="http://127.0.0.1:8000", live_code_rev="0" * 40,
            fingerprints=fp, readiness={"ready_wait_sec": 30.0, "settle_sec": 10.0},
        )
        assert errors == []

    def test_footnote_missing_key_and_placeholder(self):
        errors = report.validate_footnote(
            _footnote(hardware="N/A"), api_base="http://127.0.0.1:8000", live_code_rev="0" * 40)
        assert any("hardware" in err for err in errors)
        errors2 = report.validate_footnote(
            {k: v for k, v in _footnote().items() if k != "window"},
            api_base="http://127.0.0.1:8000", live_code_rev="0" * 40)
        assert any("window" in err for err in errors2)

    def test_footnote_instance_must_be_local_on_loopback(self):
        errors = report.validate_footnote(
            _footnote(instance="cloud-box"), api_base="http://127.0.0.1:8000", live_code_rev="0" * 40)
        assert any("local" in err for err in errors)

    def test_footnote_code_rev_must_match_live(self):
        errors = report.validate_footnote(
            _footnote(code_rev="1" * 40), api_base="http://127.0.0.1:8000", live_code_rev="0" * 40)
        assert any("code_rev" in err for err in errors)

    def test_footnote_requires_readiness_measurements(self):
        errors = report.validate_footnote(
            _footnote(readiness="ready=30s"), api_base="http://127.0.0.1:8000", live_code_rev="0" * 40,
            readiness={"ready_wait_sec": 30.0})
        assert any("静置" in err or "settle" in err for err in errors)

    def test_fingerprints_shape_and_validation(self, monkeypatch):
        monkeypatch.setattr(report, "hardware_raw", lambda *a, **k: "cpu model x")
        monkeypatch.setattr(report, "dtk_version_raw", lambda: "DTK 25.04")
        fp = report.build_fingerprints(api_base="http://127.0.0.1:8000")
        assert set(report.FINGERPRINT_KEYS) <= set(fp)
        assert len(fp["sha256"]) == 64
        assert report.validate_fingerprints(fp) == []
        assert report.validate_fingerprints({}) != []
        empty = dict(fp, dtk_version_raw="")
        assert report.validate_fingerprints(empty) != []   # 空 DTK 必须被点名，不得静默通过

    def test_hardware_raw_is_machine_derived_not_empty(self):
        assert report.hardware_raw().strip() != ""    # 无 GPU 工具时必须退到 CPU/机器原始输出

    def test_verdict_order(self):
        assert report.verdict_from_checks([]) == "PASS"
        assert report.verdict_from_checks([{"status": "UNVERIFIED"}]) == "UNVERIFIED"
        assert report.verdict_from_checks(
            [{"status": "UNVERIFIED"}, {"status": "BLOCKED"}]) == "BLOCKED"
        assert report.verdict_from_checks(
            [{"status": "BLOCKED"}, {"status": "FAIL"}]) == "FAIL"

    def test_numeric_whitelist_flags_unknown_leaf(self):
        doc = {"summary": {"file_wall_s": 1.0, "mystery_metric": 42.0}}
        errors = report.numeric_whitelist_errors(doc)
        assert any("mystery_metric" in err for err in errors)

    def test_quality_key_errors(self):
        doc = {"metrics": {"f1": 0.9}}
        assert report.quality_key_errors(doc) != []
        assert report.quality_key_errors({"summary": {"file_wall_s": 1.0}}) == []

    def test_markdown_render_and_consistency(self):
        doc = _report_envelope()
        md = report.render_markdown(doc)
        assert "speedkit 报告" in md
        assert "k=`3`" in md
        assert report.md_consistency_errors(doc, md) == []

    def test_write_outputs(self, tmp_path):
        doc = _report_envelope()
        json_path, md_path = report.write_outputs(doc, str(tmp_path / "unit"))
        payload = json.loads(Path(json_path).read_text(encoding="utf-8"))
        assert payload["verdict"] == "PASS"
        assert "unit-arm" in Path(md_path).read_text(encoding="utf-8")


# ── driver：载荷/统计/证据判定 ─────────────────────────────────────────────
class TestDriver:
    def test_default_payload_is_pinned(self):
        payload, source, sha = driver.effective_payload(None)
        assert source == driver.PAYLOAD_SOURCE_DEFAULT
        assert payload["selected_ocr_has_types"] == list(driver.ANCHOR_OCR_TYPES)
        assert payload["selected_visual_feature_types"] == list(driver.ANCHOR_VISUAL_TYPES)
        assert len(sha) == 64

    def test_payload_file_source_and_empty_rejected(self, tmp_path):
        path = tmp_path / "payload.json"
        path.write_text(json.dumps({
            "selected_ocr_has_types": ["PERSON"], "selected_visual_feature_types": ["official_seal"],
        }), encoding="utf-8")
        payload, source, _ = driver.effective_payload(str(path))
        assert source == driver.PAYLOAD_SOURCE_FILE and payload["selected_ocr_has_types"] == ["PERSON"]
        path.write_text(json.dumps({"selected_ocr_has_types": [], "selected_visual_feature_types": []}),
                        encoding="utf-8")
        with pytest.raises(ValueError):
            driver.effective_payload(str(path))

    def test_wall_stats(self):
        stats = driver.wall_stats([1.0, 2.0, 3.0])
        assert stats["n"] == 3 and stats["p50"] == 2.0 and stats["mean"] == 2.0
        assert driver.wall_stats([])["n"] == 0

    def test_timing_rounds_needed(self):
        assert driver.timing_rounds_needed({"n": 2, "discretion_ratio": 0.0})[0] is True
        assert driver.timing_rounds_needed({"n": 3, "discretion_ratio": 0.05})[0] is False
        assert driver.timing_rounds_needed({"n": 3, "discretion_ratio": 0.3})[0] is True

    def test_wall_agreement(self):
        ok = driver.wall_agreement([(1.0, 1.01), (2.0, 1.98)])
        assert ok["within_tolerance"] is True
        bad = driver.wall_agreement([(1.0, 1.5)])
        assert bad["within_tolerance"] is False

    def test_round_valid(self):
        ok, reason = driver.round_valid({"pages": [{}] * 10}, driver="single", expected_pages=10)
        assert ok is True and reason is None
        bad, reason = driver.round_valid({"pages": []}, driver="single", expected_pages=10)
        assert bad is False and reason
        short, reason = driver.round_valid({"pages": [{}] * 3}, driver="batch", expected_pages=10)
        assert short is False and reason
        unsupported, reason = driver.round_valid(
            {"pages": [], "rejected_items": [{"unsupported": True}]}, driver="single", expected_pages=10)
        assert unsupported is True and reason is None

    def test_cold_evidence_pass_and_pid_reuse(self):
        evidence = {"rounds": [{"round": 2, "pid": 111, "start_time": "s1", "timestamp": "t1"},
                               {"round": 3, "pid": 222, "start_time": "s2", "timestamp": "t2"}],
                    "env": {"TEXT_LAYER_DOCLINE_ENABLED": "false"},
                    "ready_wait_sec": 30.0, "settle_sec": 10.0}
        rounds = [{"round": 2}, {"round": 3}]
        checks = {c["id"]: c for c in driver.cold_evidence_checks(
            evidence, rounds, cache_policy="cold", driver="single",
            api_base="http://127.0.0.1:8000", restart_cmd=None)}
        assert checks["D2.cold_evidence"]["status"] == "PASS"
        assert checks["D5.readiness_probe"]["status"] == "PASS"
        assert checks["D2.restart_cmd"]["status"] == "UNVERIFIED"

        evidence["rounds"][1]["pid"] = 111
        checks = {c["id"]: c for c in driver.cold_evidence_checks(
            evidence, rounds, cache_policy="cold", driver="single",
            api_base="http://127.0.0.1:8000", restart_cmd="x")}
        assert checks["D2.cold_evidence"]["status"] == "FAIL"

    def test_cold_evidence_warm_policy_not_a_cold_verdict(self):
        checks = driver.cold_evidence_checks(
            {}, [], cache_policy="warm", driver="batch",
            api_base="http://127.0.0.1:8000", restart_cmd=None)
        ids = {c["id"]: c["status"] for c in checks}
        assert ids["D2.cache_policy"] == "UNVERIFIED"
        assert "D2.cold_evidence" not in ids
        assert ids["D5.readiness_probe"] == "FAIL"

    def test_env_matches_arm(self):
        arm = {"env": {"A": "1"}}
        assert driver.env_matches_arm({"env": {"A": "1"}}, arm)[0]["status"] == "PASS"
        assert driver.env_matches_arm({"env": {"A": "2"}}, arm)[0]["status"] == "FAIL"
        assert driver.env_matches_arm({"env": {}}, arm)[0]["status"] == "FAIL"
        assert driver.env_matches_arm({"env": {}}, {"env": {}}) == []

    def test_concurrency_dual_evidence(self):
        rounds = [{"agg": {"observed_page_concurrency": 3}}]
        assert driver.concurrency_dual_evidence(rounds, 3, "single")[0]["status"] == "PASS"
        assert driver.concurrency_dual_evidence(rounds, 2, "single")[0]["status"] == "FAIL"
        assert driver.concurrency_dual_evidence([], 3, "batch")[0]["status"] == "UNVERIFIED"
        rounds2 = [{"agg": {"page_concurrency_peak": 4}}]
        assert driver.concurrency_dual_evidence(rounds2, 4, "batch")[0]["status"] == "PASS"

    def test_expect_reject_checks(self):
        corpus = [{"id": "enc"}, {"id": "ok"}]
        corpus[0]["expect_reject"] = True
        rounds = [{"rejected_items": [{"id": "enc", "unsupported": True, "http_status": 400,
                                       "reason": "encrypted pdf"}]}]
        assert driver.expect_reject_checks(rounds, corpus)[0]["status"] == "PASS"
        assert driver.expect_reject_checks([], corpus)[0]["status"] == "FAIL"
        rounds2 = [{"rejected_items": [{"id": "enc", "unsupported": False, "reason": "parsed"}]}]
        assert driver.expect_reject_checks(rounds2, corpus)[0]["status"] == "FAIL"
        assert driver.expect_reject_checks([], [{"id": "ok"}]) == []

    def test_quality_sidecar_and_response_extraction(self):
        sidecar = driver.sidecar_from_response({"bounding_boxes": [{"text": "张三"}, {"text": ""}, {}]})
        assert sidecar["box_count"] == 3 and sidecar["entity_count"] == 1
        assert driver.quality_sidecar_check(sidecar, [])[0]["status"] == "PASS"
        assert driver.quality_sidecar_check({}, [])[0]["status"] == "UNVERIFIED"

    def test_load_round_evidence(self, tmp_path):
        assert driver.load_round_evidence(None) == {}
        bad = tmp_path / "ev.json"
        bad.write_text(json.dumps({"nope": []}), encoding="utf-8")
        with pytest.raises(ValueError):
            driver.load_round_evidence(str(bad))
        good = tmp_path / "ev-good.json"
        good.write_text(json.dumps({"rounds": [{"round": 2}]}), encoding="utf-8")
        assert driver.load_round_evidence(str(good))["rounds"][0]["round"] == 2

    def test_dig_number(self):
        assert driver._dig_number({"a": {"b": 2}}, ("a", "b")) == 2.0
        assert driver._dig_number({"a": {"b": "x"}}, ("a", "b")) is None
        assert driver._dig_number({"a": 1}, ("a", "b")) is None


# ── arms：臂 schema ────────────────────────────────────────────────────────
def _write_json(tmp_path, doc, name="arm.json"):
    path = tmp_path / name
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


class TestArms:
    def test_load_arm_minimal(self, tmp_path):
        arm = arms.load_arm(str(_write_json(tmp_path, {"name": "baseline", "kind": "env"})))
        assert arm["name"] == "baseline" and arm["kind"] == "env"
        assert arm["env"] == {} and arm["probe"] is None

    def test_load_arm_env_stringified(self, tmp_path):
        arm = arms.load_arm(str(_write_json(
            tmp_path, {"name": "k1", "kind": "env", "env": {"PAGE_CONCURRENCY": 1, "FLAG": True}})))
        assert arm["env"] == {"PAGE_CONCURRENCY": "1", "FLAG": "True"}

    def test_load_arm_rejects_broken_definitions(self, tmp_path):
        with pytest.raises(ValueError):
            arms.load_arm(str(_write_json(tmp_path, {"kind": "env"})))  # 缺 name
        with pytest.raises(ValueError):
            arms.load_arm(str(_write_json(tmp_path, {"name": "x", "kind": "magic"})))  # kind 非法
        with pytest.raises(ValueError):
            arms.load_arm(str(_write_json(tmp_path, {"name": "x", "kind": "env", "extra": 1})))  # 未知键
        with pytest.raises(ValueError):
            arms.load_arm(str(_write_json(tmp_path, {"name": "x", "kind": "patch"})))  # patch 臂无 patches/expect
        with pytest.raises(ValueError):
            arms.load_arm(str(_write_json(tmp_path, {"name": "x", "kind": "env", "probe": "bash"})))

    def test_arm_report_block(self, tmp_path):
        arm = arms.load_arm(str(_write_json(
            tmp_path, {"name": "k1", "kind": "env", "env": {"A": "1"}, "notes": "note"})))
        block = arms.arm_report_block(arm)
        assert block["name"] == "k1" and block["env"] == {"A": "1"}
        assert "checkout_root" in block

    def test_arm_checks_shape(self, tmp_path, monkeypatch):
        arm = arms.load_arm(str(_write_json(tmp_path, {"name": "k1", "kind": "env"})))
        monkeypatch.setattr(arms, "git_porcelain", lambda *a, **k: [])
        checks = arms.arm_checks(arm, phase="pre")
        assert checks and all(c["status"] in ("PASS", "FAIL", "BLOCKED", "UNVERIFIED") for c in checks)
        monkeypatch.setattr(arms, "git_porcelain", lambda *a, **k: [" M eval-ng/cli.py"])
        patch_arm = arms.load_arm(str(_write_json(
            tmp_path, {"name": "k1", "kind": "patch", "patches": [
                {"file": "frontend/src/constants/timing.ts", "from": "= 3;", "to": "= 1;"}]})))
        blocked = arms.arm_checks(patch_arm, phase="pre")
        assert any(c["status"] == "BLOCKED" for c in blocked)


# ── CLI：用法错误与退出码（零网络） ────────────────────────────────────────
def _cli_fixtures(tmp_path):
    corpus = tmp_path / "卷.txt"
    corpus.write_text("x" * 10, encoding="utf-8")
    exam = _write_json(tmp_path, {
        "exam_id": "unit-exam", "suite": "synthetic",
        "corpus": [{"id": "c1", "path": str(corpus), "pages": 2, "carrier": "text"}],
    }, "exam.json")
    arm = _write_json(tmp_path, {"name": "k1", "kind": "env"}, "arm.json")
    evidence = _write_json(tmp_path, {"rounds": []}, "evidence.json")
    return corpus, exam, arm, evidence


def _speed_argv(exam, arm, evidence, tmp_path, **overrides):
    argv = ["speed", str(exam), "--driver", "batch", "--arm", str(arm),
            "--out", str(tmp_path / "report" / "unit"),
            "--page-concurrency", "2", "--cache-policy", "cold", "--suite", "synthetic",
            "--round-evidence", str(evidence)]
    for key, value in overrides.items():
        argv += [f"--{key.replace('_', '-')}", str(value)]
    return argv


class TestCliUsage:
    def test_admits_valid_command(self, tmp_path, capsys):
        corpus, exam, arm, evidence = _cli_fixtures(tmp_path)
        code = cli.main(_speed_argv(exam, arm, evidence, tmp_path))
        assert code in (0, 1, 3)  # 进到测量阶段后因无后端而 BLOCKED（非用法错）
        assert code != 2

    def test_page_concurrency_zero(self, tmp_path):
        _, exam, arm, evidence = _cli_fixtures(tmp_path)
        assert cli.main(_speed_argv(exam, arm, evidence, tmp_path, page_concurrency=0)) == 2

    def test_rounds_out_of_range(self, tmp_path):
        _, exam, arm, evidence = _cli_fixtures(tmp_path)
        assert cli.main(_speed_argv(exam, arm, evidence, tmp_path, rounds=9)) == 2

    def test_missing_exam(self, tmp_path):
        _, exam, arm, evidence = _cli_fixtures(tmp_path)
        assert cli.main(_speed_argv(tmp_path / "nope.json", arm, evidence, tmp_path)) == 2

    def test_item_without_pages(self, tmp_path):
        corpus, exam, arm, evidence = _cli_fixtures(tmp_path)
        _write_json(tmp_path, {"exam_id": "e", "corpus": [{"id": "c1", "path": str(corpus)}]}, "exam.json")
        assert cli.main(_speed_argv(exam, arm, evidence, tmp_path)) == 2

    def test_md5_drift_rejected(self, tmp_path):
        corpus, exam, arm, evidence = _cli_fixtures(tmp_path)
        _write_json(tmp_path, {"exam_id": "e", "corpus": [
            {"id": "c1", "path": str(corpus), "pages": 2, "md5": "0" * 32, "expect_md5": "1" * 32}]},
            "exam.json")
        assert cli.main(_speed_argv(exam, arm, evidence, tmp_path)) == 2

    def test_suite_real_needs_real_exam(self, tmp_path):
        _, exam, arm, evidence = _cli_fixtures(tmp_path)
        assert cli.main(_speed_argv(exam, arm, evidence, tmp_path, suite="real")) == 2

    def test_real_exam_cannot_be_run_as_synthetic(self, tmp_path):
        corpus, exam, arm, evidence = _cli_fixtures(tmp_path)
        _write_json(tmp_path, {"exam_id": "e", "suite": "real", "corpus": [
            {"id": "c1", "path": str(corpus), "pages": 2, "carrier": "text"}]}, "exam.json")
        assert cli.main(_speed_argv(exam, arm, evidence, tmp_path)) == 2

    def test_unknown_driver_rejected_by_argparse(self, tmp_path):
        _, exam, arm, evidence = _cli_fixtures(tmp_path)
        with pytest.raises(SystemExit):
            cli.main(_speed_argv(exam, arm, evidence, tmp_path, driver="turbo"))

    def test_concurrency_above_total_pages(self, tmp_path):
        _, exam, arm, evidence = _cli_fixtures(tmp_path)
        assert cli.main(_speed_argv(exam, arm, evidence, tmp_path, page_concurrency=99)) == 2

    def test_speed_exit_mapping(self):
        assert cli._speed_exit("PASS") == 0
        assert cli._speed_exit("FAIL") == 1
        assert cli._speed_exit("BLOCKED") == 3
        assert cli._speed_exit("UNVERIFIED") == 3

    def test_exam_view_redacts_real_paths(self, tmp_path):
        corpus, exam, arm, evidence = _cli_fixtures(tmp_path)
        real_exam = _write_json(tmp_path, {"exam_id": "e", "suite": "real", "corpus": [
            {"id": "c1", "path": str(corpus), "pages": 2, "carrier": "scanned_pdf"}]}, "exam.json")
        args = cli._parse_args(_speed_argv(real_exam, arm, evidence, tmp_path, suite="real"))
        _, view = cli._load_speed_corpus(args)
        assert view["corpus"][0]["path"].startswith("<redacted:")
        assert str(corpus) not in json.dumps(view, ensure_ascii=False)


# ── 脱敏扫描器（D5④） ──────────────────────────────────────────────────────
class TestRedactionScanner:
    def test_self_test_positive_control(self):
        assert redact.self_test() == []
        assert redact.main(["--self-test"]) == 0

    def test_scan_rules(self):
        hits = redact.scan("ip 10.1.2.3 path /root/ab/d3.json win C:\\Users\\dev\\a.txt",
                           denylist=[], allow_prefixes=[])
        rules = {rule for rule, _ in hits}
        assert {"ip", "abs_path", "win_path"} <= rules
        assert redact.scan("ip 127.0.0.1 ok", denylist=[], allow_prefixes=[]) == []
        assert redact.scan("path /root/ab/d3.json", denylist=[],
                           allow_prefixes=["/root/ab"]) == []
        assert redact.scan("卷 苏A-某某公司", denylist=["苏A-某某公司"], allow_prefixes=[])
        assert redact.scan("rev abc123", denylist=["re:^rev"], allow_prefixes=[])

    def test_main_exit_codes(self, tmp_path):
        dirty = tmp_path / "dirty.md"
        dirty.write_text("instance 203.0.113.9\n", encoding="utf-8")
        assert redact.main([str(dirty)]) == 1
        clean = tmp_path / "clean.md"
        clean.write_text("instance local, api 127.0.0.1\n", encoding="utf-8")
        assert redact.main([str(clean)]) == 0
        assert redact.main([]) == 2

    def test_denylist_file(self, tmp_path):
        words = tmp_path / "deny.txt"
        words.write_text("苏A-某某公司-2021\n", encoding="utf-8")
        target = tmp_path / "report.md"
        target.write_text("卷名 苏A-某某公司-2021\n", encoding="utf-8")
        assert redact.main([str(target), "--denylist", str(words)]) == 1
