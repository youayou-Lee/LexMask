"""eval-ng speedkit（Issue #131）单测：信号抽取/闭合校验/报告自足/驱动判定/CLI 用法/脱敏扫描。

零网络、零真端点：所有响应结构由 fixture 造。
"""

import json
import types
from pathlib import Path

import evalng_bootstrap as boot
import httpx
import pytest

collect = boot.load("engine/speedkit/collect.py")
driver = boot.load("engine/speedkit/driver.py")
report = boot.load("engine/speedkit/report.py")
arms = boot.load("engine/speedkit/arms.py")
cli = boot.load("cli.py")
redact = boot.load("scripts/check_report_redaction.py")
REAL_SPEED_API = driver.SpeedApi  # autouse fixture 会把模块属性替换成抛错版本，这里留真类引用
EVALNG_ROOT = Path(__file__).resolve().parents[2] / "eval-ng"

RING_LEAVES = {"prepare": 100.0, "ocr": 300.0, "has_ner": 400.0, "match": 80.0, "draw": 120.0}


@pytest.fixture(autouse=True)
def _no_real_backend(monkeypatch):
    """单元测试永不连真后端。

    云实例上 `127.0.0.1:8000` 是**活的**：若 CLI 用例走默认 `--api-base`，本地「连接被拒 ⇒ 秒退 BLOCKED」
    在实例上会变成「真的登录 + 真的上传 + 真的跑测量」——既不确定、又慢，还对真实后端做了意外写入
    （2026-10-16 在实例上跑本文件时实测：`test_admits_valid_command` 卡住不返回）。
    这里把 `SpeedApi` 的构造直接改成抛错：任何未显式替换 API 的用例都立刻走「装置不可得 ⇒ BLOCKED」。
    """
    attempted: list = []

    def _boom(*args, **kwargs):
        attempted.append((args, kwargs))
        raise ConnectionError("单元测试禁止真实建连（约定：零网络）")

    monkeypatch.setattr(driver, "SpeedApi", _boom)
    return attempted


def _single_resp(total: float = 1000.0, ring: dict | None = None, *,
                 stage_total: float | None = None, block_total: float | None = None) -> dict:
    """单管线 canned 响应。

    `stage_total` = `stage_duration_ms["total"]`（**部分阶段**合计；真实后端上常远小于环）；
    `block_total` = `pipeline_status[*].duration_ms`（**块级**管线耗时，D3①a 闭合用的环）。
    默认两者相等，便于旧用例继续用「按比例缩放环」造闭合偏差；真实形状见
    `test_single_pipeline_ring_uses_block_duration_against_mixed_ledger`。
    """
    ring = dict(ring or RING_LEAVES)
    if stage_total is None:
        stage_total = round(sum(ring.values()), 3)
    ring["total"] = stage_total
    if block_total is None:
        block_total = stage_total
    return {
        "page": 3,
        "duration_ms": {"total": total, "request_total_ms": total + 5.0},
        "pipeline_status": {"ocr_has": {"stage_duration_ms": ring, "duration_ms": block_total}},
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


def _signal(total: float = 1000.0, wall_s: float = 1.0, ring: dict | None = None, **kwargs) -> dict:
    return collect.page_signal(_single_resp(total, ring, **kwargs), wall_s=wall_s, page_no=3)


# ── collect：信号抽取与聚合 ────────────────────────────────────────────────
class TestCollect:
    def test_single_signal_keys(self):
        sig = _signal()
        assert sig["root_total_ms"] == 1000.0
        assert sig["request_total_ms"] == 1005.0
        assert sig["page"] == 3
        assert sig["ring_keys"] == sorted(f"ocr_has.{k}" for k in RING_LEAVES)
        assert sig["pipeline_totals_ms"] == {"ocr_has": 1000.0}

    def test_duration_key_rejects_counters_and_total(self):
        assert collect.is_duration_key("ocr")
        assert collect.is_duration_key("has_text_prepare_ms")
        assert collect.is_duration_key("prepare")
        assert not collect.is_duration_key("total")
        assert not collect.is_duration_key("has_text_input_chars")
        assert not collect.is_duration_key("has_text_type_count")
        assert not collect.is_duration_key("ocr_cache_misses")

    def test_single_pipeline_ring_uses_block_duration_against_mixed_ledger(self):
        """④云门实测口径：环 = 块级 `duration_ms`，叶子只收耗时键（计数不入环）。

        真实后端第 3 页（探针实测）：ocr_has 块 1853ms / stage total 849ms、visual_features 块 3944ms /
        stage total 3895ms、页根 total 5832ms；旧口径拿 stage total 求和 ⇒ 闭合差 29% 恒 FAIL。
        """
        resp = {
            "page": 3,
            "duration_ms": {"ocr_has": 1853.0, "visual_features": 3944.0, "pdf_render_ms": 871.0,
                            "total": 5832.0, "request_total_ms": 5856.0},
            "pipeline_status": {
                "ocr_has": {
                    "duration_ms": 1853.0,
                    "stage_duration_ms": {
                        "prepare": 69.0, "ocr": 762.0, "has_ner": 3.0, "match": 7.0, "draw": 0.0,
                        "total": 849.0,
                        "has_text_input_chars": 357, "has_text_type_count": 20, "has_text_total_ms": 3.0,
                        "ocr_cache_hits": 1, "has_text_truncated": False,
                    },
                },
                "visual_features": {
                    "duration_ms": 3944.0,
                    "stage_duration_ms": {"model": 3283.0, "prepare": 0.0, "draw": 0.0, "total": 3895.0},
                },
            },
        }
        sig = collect.page_signal(resp, wall_s=1.0, page_no=3)
        assert sig["pipeline_totals_ms"] == {"ocr_has": 1853.0, "visual_features": 3944.0}
        assert "ocr_has.total" not in sig["ring_keys"]
        assert "ocr_has.has_text_input_chars" not in sig["ring_keys"]
        assert "ocr_has.ocr" in sig["ring_keys"] and "visual_features.model" in sig["ring_keys"]
        agg = collect.sum_pages([sig])
        assert agg["residual_ratio"] < 0.02  # 5832 vs 5797 ⇒ 0.6%，旧口径 29% 会 FAIL
        assert agg["sum_rings_ms"] == 5797.0
        statuses = {check["id"]: check["status"] for check in collect.closure_checks(agg)}
        assert statuses["D3.closure_same_clock"] == "PASS"
        assert statuses["D3.residual_two_sided"] == "PASS"

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

    def test_no_suffix_merge_is_falsifiable(self):
        """C1：`D3.no_suffix_merge` 必须真读输入——两管线同名叶子各成独立键，归并即 FAIL。

        判别性来自「把 aggs 手动改成归并形态」这一负例：只断言正例 PASS 是自证
        （③ 第一轮冷评审实测：把该行改成写死 PASS 后 64 条单测全绿）。
        """
        two = {
            "page": 1,
            "duration_ms": {"total": 2000.0},
            "pipeline_status": {
                "ocr_has": {"stage_duration_ms": {"prepare": 100.0, "ocr": 300.0, "has_ner": 400.0,
                                                  "match": 80.0, "draw": 120.0, "total": 1000.0}},
                "text_layer_doc": {"stage_duration_ms": {"prepare": 0.0, "ocr": 0.0, "has_ner": 800.0,
                                                         "match": 80.0, "draw": 120.0, "total": 1000.0}},
            },
        }
        agg = collect.sum_pages([collect.page_signal(two, wall_s=2.0, page_no=1)])
        assert agg["pipelines_seen"] == ["ocr_has", "text_layer_doc"]
        assert {"ocr_has.ocr", "text_layer_doc.ocr"} <= set(agg["rings_by_key_ms"])
        ok = {c["id"]: c for c in collect.key_placement_checks(agg, "single")}
        assert ok["D3.no_suffix_merge"]["status"] == "PASS", ok["D3.no_suffix_merge"]

        merged = dict(agg)
        merged["rings_by_key_ms"] = {  # 模拟「跨管线按同名叶子归并」的坏实现
            "ocr_has.prepare": 100.0, "ocr_has.ocr": 0.0, "ocr_has.has_ner": 400.0,
            "ocr_has.match": 80.0, "ocr_has.draw": 120.0,
        }
        bad = {c["id"]: c for c in collect.key_placement_checks(merged, "single")}
        assert bad["D3.no_suffix_merge"]["status"] == "FAIL"
        assert "text_layer_doc" in bad["D3.no_suffix_merge"]["detail"]

        flat = dict(agg)
        flat["rings_by_key_ms"] = {"prepare": 100.0, "ocr": 300.0, "has_ner": 1200.0,
                                   "match": 160.0, "draw": 240.0}
        bare = {c["id"]: c for c in collect.key_placement_checks(flat, "single")}
        assert bare["D3.no_suffix_merge"]["status"] == "FAIL"
        assert "无管线前缀" in bare["D3.no_suffix_merge"]["detail"]

    def test_closure_checks_enforces_two_percent_bound(self):
        """CLOSURE_TOLERANCE=0.02 的上界必须有判别力（改大阈值 ⇒ M-C 变异逃生）。"""
        ring = {k: v * 0.97 for k, v in RING_LEAVES.items()}  # 环和 970 vs 页总 1000 ⇒ 3%
        agg = collect.sum_pages([_signal(total=1000.0, wall_s=1.0, ring=ring)])
        checks = {c["id"]: c for c in collect.closure_checks(agg)}
        assert checks["D3.closure_same_clock"]["status"] == "FAIL"
        assert checks["D3.closure_same_clock"]["value"] == 0.03
        assert checks["D3.residual_two_sided"]["status"] == "PASS", "3% 仍在 10% 残差带内"

    def test_closure_checks_enforces_residual_ten_percent_bound(self):
        """MAX_RESIDUAL_RATIO=0.10 两侧都要钉住：9% 放行、11% 判 FAIL。"""
        for factor, status in ((0.91, "PASS"), (0.89, "FAIL")):
            ring = {k: v * factor for k, v in RING_LEAVES.items()}
            agg = collect.sum_pages([_signal(total=1000.0, wall_s=1.0, ring=ring)])
            checks = {c["id"]: c for c in collect.closure_checks(agg)}
            assert checks["D3.residual_two_sided"]["status"] == status, (factor, checks)

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

    def test_criteria_refs_validator_names_bad_path_and_stale_value(self):
        """I3/M15 反向用例：引用表校验必须**真会**报错（短路成 [] 即红）。"""
        doc = {"summary": {"closure_ratio": 0.02},
               "criteria_refs": {
                   "D3.closure_same_clock": {"path": "summary.closure_ratio", "value": 0.02},
                   "D4.file_wall_p50": {"path": "summary.file_wall_s.p50", "value": 12.5},
                   "D2.grid_k": {"path": "summary.page_concurrency_peak", "value": 3}}}
        errors = collect.validate_criteria_refs(doc)
        assert any("D4.file_wall_p50" in e and "不可取" in e for e in errors), errors  # 路径取不到
        assert any("D2.grid_k" in e and "不可取" in e for e in errors), errors  # 同上（缺 summary 键）
        doc["criteria_refs"]["D4.file_wall_p50"] = {"path": "summary.closure_ratio", "value": 0.99}
        stale = collect.validate_criteria_refs(doc)
        assert any("D4.file_wall_p50" in e and "≠" in e for e in stale), stale  # 值不等（快照过期）
        clean = {"summary": {"closure_ratio": 0.02},
                 "criteria_refs": {"D3.closure_same_clock": {"path": "summary.closure_ratio",
                                                             "value": 0.02}}}
        assert collect.validate_criteria_refs(clean) == []
        assert collect.validate_criteria_refs({"summary": {}}) == ["报告缺 criteria_refs（D3⑦）"]


# ── report：自足/白名单/判定 ───────────────────────────────────────────────
def _check_status(checks: list[dict], check_id: str) -> str | None:
    """按 id 取检查项状态（模块级：多个测试类共用）。"""
    return next((c["status"] for c in checks if c["id"] == check_id), None)


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
    def test_footnote_eight_keys_pass(self, monkeypatch):
        # C5：脚注 hardware/image_dtk 必须与指纹交叉一致 ⇒ 本用例把指纹源钉成与脚注同值
        monkeypatch.setattr(report, "hardware_raw", lambda *a, **kw: "cpu-8c")
        monkeypatch.setattr(report, "dtk_version_raw", lambda *a, **kw: "dev-image/no-dtk")
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
        assert report.verdict_from_checks([]) == "FAIL", "空报告不得伪装通过（I9）"
        assert report.verdict_from_checks([{"status": "WAT"}]) == "FAIL", "未知状态同样不得读作通过"
        assert report.verdict_from_checks([{"status": "UNVERIFIED"}]) == "UNVERIFIED"
        assert report.verdict_from_checks(
            [{"status": "UNVERIFIED"}, {"status": "BLOCKED"}]) == "BLOCKED"
        assert report.verdict_from_checks(
            [{"status": "BLOCKED"}, {"status": "FAIL"}]) == "FAIL"

    def test_numeric_whitelist_flags_unknown_leaf(self):
        doc = {"summary": {"file_wall_s": 1.0, "mystery_metric": 42.0}}
        errors = report.numeric_whitelist_errors(doc)
        assert any("mystery_metric" in err for err in errors)

    def test_whitelist_catches_path_level_rename(self):
        """D5① 反「改名绕过」：把已放行叶子改成同语义别名，路径表必须点名拦住。"""
        doc = {"rounds": [{"round": 2, "agg": {"pages_measured": 2, "agg_value": 1.0}}]}
        errors = report.numeric_whitelist_errors(doc)
        assert any("agg_value" in err for err in errors), errors
        # 未改名时同一路径不得报错（判别性来自改名本身，不是「有数值就报」）
        ok = {"rounds": [{"round": 2, "agg": {"pages_measured": 2}}]}
        assert report.numeric_whitelist_errors(ok) == []

    def test_dedupe_checks_keeps_worst_status_per_id(self):
        """同一 id 出现两条时按四态取最严（pre/post 两相臂检查天然重名）。

        ④云门 k=3 实测：`D8.env_arm_no_file_write` 落了两次 PASS——机器消费者按 id 取值会歧义。
        """
        checks = [
            {"id": "D8.env_arm_no_file_write", "status": "PASS", "detail": "pre 相"},
            {"id": "D2.timing_rounds", "status": "PASS", "detail": "n=3"},
            {"id": "D8.env_arm_no_file_write", "status": "FAIL", "detail": "post 相：臂改了工作树"},
        ]
        out = report.dedupe_checks(checks)
        assert [c["id"] for c in out] == ["D8.env_arm_no_file_write", "D2.timing_rounds"]  # 保留首次位置
        assert out[0]["status"] == "FAIL" and "post 相" in out[0]["detail"]
        assert "同 id 2 条，取最严" in out[0]["detail"]
        assert out[1]["detail"] == "n=3"  # 无重复的条目不得被改写
        assert report.dedupe_checks([]) == []
        # N4（②轮 Nit）：白名单是**路径级**的，换族改名同样要被抓（不是只钉 rounds[].agg.*_value）
        cases = [
            ({"summary": {"agg_value": 0.87}}, "agg_value"),
            ({"mode": {"mystery_value": 0.87}}, "mystery_value"),
            ({"rounds": [{"items": [{"agg_value": 0.87}]}]}, "agg_value"),
            ({"rounds": [{"agg": {"score_value": 0.87}}]}, "score_value"),
        ]
        for doc, leaf in cases:
            errs = report.numeric_whitelist_errors(doc)
            assert errs and any(leaf in err for err in errs), (doc, errs)

    def test_whitelist_allows_observations_anchor_band_only(self):
        """P4 观测里的锚点墙钟秒数放行；同子树里换个名字（哪怕是同样的数）必须被点名。

        ④云门实测：`observations.anchor_band.declared_p50_s` 是我加 P4 观测时漏放的**自己人**，
        被自家闸门抓成 FAIL（闸门有效性的现场证明）。这里把「只放行两条具名路径」钉死。
        """
        doc = {"observations": {"anchor_band": {"declared_p50_s": 198.13, "measured_p50_s": 217.172,
                                                "out_of_band": False, "deviation": "9.6%"}}}
        assert report.numeric_whitelist_errors(doc) == []
        doc["observations"]["anchor_band"]["f1"] = 0.99
        errors = report.numeric_whitelist_errors(doc)
        assert any("f1" in err for err in errors), errors
        doc2 = {"observations": {"anchor_band": {"measured_p50_s": 217.172, "p50_ms": 217172.0}}}
        assert any("p50_ms" in err for err in report.numeric_whitelist_errors(doc2))

    def test_whitelist_allows_rejected_http_status(self):
        """D6③ 的拒收证据（HTTP 状态码）是声明载体，必须放行；同位置的新增指标仍须被抓。"""
        doc = {"rounds": [{"rejected_items": [
            {"id": "f1", "unsupported": True, "reason": "HTTP 413", "http_status": 413}]}]}
        assert report.numeric_whitelist_errors(doc) == []
        doc["rounds"][0]["rejected_items"][0]["bytes_sent"] = 9000000
        assert any("bytes_sent" in err for err in report.numeric_whitelist_errors(doc))

    def test_d7_parity_has_single_exit(self):
        """D7.parity 只有 `_speed_baseline_checks` 一个出口（④云门实测两套实现会落两条同 id）。"""
        summary = {"file_wall_s": {"p50": 90.0}, "pages_per_min": 6.5, "page_ms_p50": 24000.0}
        none_case = cli._speed_baseline_checks(None, mode={"page_concurrency": 3}, summary=summary)
        assert [c["id"] for c in none_case] == ["D7.parity"] and none_case[0]["status"] == "UNVERIFIED"

        arm_report = {"mode": {"page_concurrency": 1}, "summary": {"file_wall_s": {"p50": 217.0}}}
        pair = cli._speed_baseline_checks(arm_report, mode={"page_concurrency": 3}, summary=summary)
        assert [c["id"] for c in pair] == ["D1.anchor_ratio", "D7.parity"]
        assert pair[0]["status"] == "PASS" and "D7 parity 需交 benchmark" in pair[1]["detail"]

        bench = {"pages_per_min": 6.4, "page_ms_p50": 24500.0}
        only_d7 = cli._speed_baseline_checks(bench, mode={"page_concurrency": 3}, summary=summary)
        assert [c["id"] for c in only_d7] == ["D7.parity"] and only_d7[0]["status"] == "PASS"

        far = {"pages_per_min": 1.0, "page_ms_p50": 24500.0}
        assert cli._speed_baseline_checks(far, mode={"page_concurrency": 3}, summary=summary)[0]["status"] == "FAIL"

    def test_no_data_checks_downgraded_without_timing_rounds(self):
        """E1：装置不可得（0 计时轮）时「无数据可判」类 FAIL 降 BLOCKED；有轮次则保留 FAIL。"""
        checks = [{"id": "D3.closure_same_clock", "status": "FAIL", "detail": "无数据", "value": None}]
        downgraded = report.downgrade_no_data_checks(checks, measured=False)
        assert downgraded[0]["status"] == "BLOCKED"
        assert "装置不可得" in downgraded[0]["detail"]
        assert report.downgrade_no_data_checks(checks, measured=True)[0]["status"] == "FAIL"
        assert report.has_timing_rounds({"rounds": [{"warmup": False, "invalid": False}]}) is True
        assert report.has_timing_rounds({"rounds": [{"warmup": True}, {"invalid": True}]}) is False

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

    def test_markdown_consistency_flags_stale_md(self):
        """D4③ 的反向用例：md 与 JSON 不一致必须点名报错（只测正例等于恒真判据）。"""
        doc = _report_envelope()
        md = report.render_markdown(doc)
        assert report.md_consistency_errors(doc, md) == []
        doc["summary"]["pages_per_min"] = 999.0  # JSON 改了而 md 没重渲染 ⇒ 必须点名
        assert any("pages_per_min" in err for err in report.md_consistency_errors(doc, md))
        stale_note = _report_envelope()
        stale_note["footnote"]["manifest_hash"] = "f" * 12
        assert any("manifest_hash" in err
                   for err in report.md_consistency_errors(stale_note, md))
        assert report.md_consistency_errors(_report_envelope(), "") == ["md 产物为空（D4③）"]

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

    def test_ac24_payload_captured_on_the_wire(self, tmp_path, monkeypatch):
        """AC24：`single` 线真正发出的 body/query 必须与声明载荷同一（禁落到回落默认类型集）。

        判别性来自 MockTransport 抓到的**原始请求**：只断言 `mode.payload_*` 等于常量是自证——
        把 `SpeedApi.vision(json=payload)` 改成 `json=None` 时，本用例必须红。
        """
        import httpx  # 局部导入：只有这条用例需要 mock transport

        seen: list = []

        def handler(request):
            seen.append(request)
            path = request.url.path
            if path.endswith("/auth/login"):
                return httpx.Response(200, json={"access_token": "tok"})
            if path.endswith("/files/upload"):
                return httpx.Response(200, json={"file_id": "fid-1"})
            if path.endswith("/vision"):
                return httpx.Response(200, json=_single_resp(1000.0))
            if path.endswith("/parse"):
                return httpx.Response(200, json={"file_type": "pdf", "is_scanned": True, "total_pages": 1})
            if request.method == "DELETE":
                return httpx.Response(200, json={})
            return httpx.Response(404, json={})

        real_client = httpx.Client
        monkeypatch.setattr(driver, "SpeedApi", REAL_SPEED_API)  # 撤销 autouse 的「禁建连」替换
        monkeypatch.setattr(
            httpx, "Client",
            lambda *a, **kw: real_client(*a, **{**kw, "transport": httpx.MockTransport(handler)}),
        )
        corpus = tmp_path / "卷.txt"
        corpus.write_text("x" * 10, encoding="utf-8")
        payload, _source, payload_sha = driver.effective_payload(None)
        api = driver.SpeedApi(base_url="http://127.0.0.1:8000", username="u", password="p")
        try:
            record = driver.measure_single_round(
                api, [{"id": "c1", "path": str(corpus), "pages": 1, "carrier": "text"}],
                payload=payload, page_concurrency=1, cleanup=True,
            )
        finally:
            api.close()

        vision = [r for r in seen if r.url.path.endswith("/vision")]
        assert len(vision) == 1
        assert vision[0].content, "请求必须带 body —— 空 body 即 has_request=False 回落路径"
        body = json.loads(vision[0].content.decode("utf-8"))
        assert body["selected_ocr_has_types"] == list(driver.ANCHOR_OCR_TYPES)
        assert body["selected_visual_feature_types"] == list(driver.ANCHOR_VISUAL_TYPES)
        params = dict(vision[0].url.params)
        assert params["force"] == "true"
        assert params["include_result_image"] == "false"
        assert len(payload_sha) == 64
        assert record["pages"][0]["root_total_ms"] == 1000.0

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

    def test_concurrency_dual_evidence_accounts_for_small_corpus(self):
        """④云门备料实测：单轮页数 < k 时物理上到不了 k（1 页卷 + k=2 的批线作业 peak 只能是 1）。

        期望值 = min(声明 k, 该轮页数)；否则会把「语料太小」误报成「声明造假」。
        """
        small = [{"round": 2, "agg": {"page_concurrency_peak": 1, "pages_measured": 1}}]
        check = driver.concurrency_dual_evidence(small, 2, "batch")[0]
        assert check["status"] == "PASS" and "期望 min(k,页)=1" in check["detail"]
        # 页面够多却仍达不到声明值 ⇒ 仍是 FAIL（判别力不得被「小语料豁免」稀释）
        big = [{"round": 2, "agg": {"page_concurrency_peak": 1, "pages_measured": 10}}]
        assert driver.concurrency_dual_evidence(big, 2, "batch")[0]["status"] == "FAIL"
        # single 线同规则
        one_page = [{"round": 2, "agg": {"observed_page_concurrency": 1}, "pages": [{}]}]
        assert driver.concurrency_dual_evidence(one_page, 3, "single")[0]["status"] == "PASS"

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

    def test_sidecar_keeps_numeric_counters_and_merges(self):
        """I1 反向用例：响应回吐的**数值计数**不得被结构性丢弃（改 `is_number` 判定即红）。"""
        resp = {
            "bounding_boxes": [{"text": "张三"}, {"text": ""}],
            "pipeline_status": {"ocr_has": {"stage_duration_ms": {
                "has_text_ner_finish_reason": "length",
                "has_text_ner_truncation_retries": 2,
                "has_text_ner_rebatch_batches": 1,
                "has_text_cache_status": "miss"}}},
        }
        side = driver.sidecar_from_response(resp)
        assert side["has_text_ner_truncation_retries"] == 2, "数值计数必须保留（否则 D8① 无旁证可判）"
        assert side["has_text_ner_rebatch_batches"] == 1
        assert side["has_text_ner_finish_reason"] == "length"
        assert side["has_text_cache_status"] == "miss"
        # 计数类跨轮累加、状态类取最近值
        acc: dict = {}
        driver._merge_sidecar(acc, side)
        driver._merge_sidecar(acc, {**side, "has_text_ner_truncation_retries": 3,
                                   "has_text_cache_status": "hit"})
        assert acc["has_text_ner_truncation_retries"] == 5.0, acc
        assert acc["has_text_ner_rebatch_batches"] == 2.0
        assert acc["has_text_cache_status"] == "hit"
        # 旁证取不到 ⇒ UNVERIFIED（不得判 PASS）
        assert driver.quality_sidecar_check({}, [])[0]["status"] == "UNVERIFIED"
        assert driver.quality_sidecar_check(acc, [{"sidecar": acc}])[0]["status"] == "PASS"
        dropped = driver.quality_sidecar_check({}, [{"sidecar": acc}])
        assert dropped[0]["status"] == "FAIL", "响应回吐过的旁证在聚合里丢了必须 FAIL"

    def test_measurement_failure_kind_distinguishes_rejection_from_unreachable(self):
        """I7/D6④ 反向用例：413/504 是**被测对象拒收** ⇒ FAIL；连不上/500 ⇒ 装置不可得 ⇒ BLOCKED。"""
        assert driver.measurement_failure_kind(ConnectionError("refused"))[0] == "BLOCKED"
        reject_413 = driver.VisionRejected(413, "payload too large")
        kind, detail = driver.measurement_failure_kind(reject_413)
        assert (kind, "413" in detail) == ("FAIL", True), detail
        assert driver.measurement_failure_kind(driver.VisionRejected(504, "gateway timeout"))[0] == "FAIL"
        assert driver.measurement_failure_kind(driver.VisionRejected(500, "boom"))[0] == "BLOCKED"
        request = httpx.Request("POST", "http://127.0.0.1:8000/api/v1/x")
        resp_502 = httpx.Response(502, request=request)
        err = httpx.HTTPStatusError("bad gateway", request=request, response=resp_502)
        assert driver.measurement_failure_kind(err)[0] == "BLOCKED"
        assert driver.measurement_failure_kind(
            driver.VisionRejected(413, "big"))[1].startswith("HTTP 413")

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


def test_patch_effect_checks_require_marks_on_disk(tmp_path):
    """D8：臂声明「已生效」必须真在文件里——静默未生效是 FAIL，不是放过。

    判别性说明：`patches[].to` 的语义是「打完补丁后文件里应有这段」，所以文档里
    用**现树真含的标记**（`timing.ts:57` 的 `= 3;`）构造 PASS 例，用幻影标记构造 FAIL 例。
    """
    env_arm = arms.load_arm(str(_write_json(tmp_path, {"name": "env", "kind": "env"})))
    assert [c["status"] for c in arms.patch_effect_checks(env_arm)] == ["UNVERIFIED"]
    timing = "frontend/src/constants/timing.ts"
    present = arms.load_arm(str(_write_json(tmp_path, {
        "name": "k3", "kind": "patch",
        "patches": [{"file": timing, "from": "= 1;", "to": "PLAYGROUND_VISION_PAGE_CONCURRENCY = 3;"}],
        "expect": [{"file": timing, "contains": "= 3;"}]})))
    assert [c["status"] for c in arms.patch_effect_checks(present)] == ["PASS", "PASS"]
    absent = arms.load_arm(str(_write_json(tmp_path, {
        "name": "k1", "kind": "patch",
        "patches": [{"file": timing, "from": "= 3;", "to": "PLAYGROUND_VISION_PAGE_CONCURRENCY = 1;"}],
        # 幻影标记必须写全：`= 1;` 这种短串会撞上同文件的其它常量（本轮实测撞过，改用全名）
        "expect": [{"file": timing, "contains": "PLAYGROUND_VISION_PAGE_CONCURRENCY = 1;"}]})))
    checks = arms.patch_effect_checks(absent)
    assert [c["status"] for c in checks] == ["FAIL", "FAIL"]
    assert all("静默未生效" in c["detail"] for c in checks)
    ghost = arms.load_arm(str(_write_json(tmp_path, {
        "name": "ghost", "kind": "patch",
        "patches": [{"file": "frontend/src/nope.ts", "from": "a", "to": "b"}]})))
    assert [c["status"] for c in arms.patch_effect_checks(ghost)] == ["FAIL"]


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
    def test_admits_valid_command(self, tmp_path, capsys, _no_real_backend):
        """合法命令行必须一路走到**测量阶段**，然后因装置不可得判 BLOCKED（exit 3）。

        判别性来自 `_no_real_backend.attempted`：**构造被调用过**才算走到了测量阶段——
        只看 exit != 2 的话，「在前面某个前置校验里早退」也能骗过这条断言。
        """
        corpus, exam, arm, evidence = _cli_fixtures(tmp_path)
        code = cli.main(_speed_argv(exam, arm, evidence, tmp_path))
        assert _no_real_backend, "未走到测量阶段：SpeedApi 从未被构造"
        assert code == 3  # 装置不可得 ⇒ BLOCKED（四态里的第三态，不是用法错、也不是 FAIL）
        captured = capsys.readouterr()
        assert "BLOCKED" in (captured.out + captured.err)

    def test_page_concurrency_zero(self, tmp_path):
        _, exam, arm, evidence = _cli_fixtures(tmp_path)
        assert cli.main(_speed_argv(exam, arm, evidence, tmp_path, page_concurrency=0)) == 2

    def test_rounds_out_of_range(self, tmp_path):
        _, exam, arm, evidence = _cli_fixtures(tmp_path)
        assert cli.main(_speed_argv(exam, arm, evidence, tmp_path, rounds=9)) == 2

    def test_missing_exam(self, tmp_path):
        _, exam, arm, evidence = _cli_fixtures(tmp_path)
        assert cli.main(_speed_argv(tmp_path / "nope.json", arm, evidence, tmp_path)) == 2

    def test_missing_precondition_files_are_usage_errors(self, tmp_path, capsys):
        """缺失的载荷/臂/证据文件必须落成**用法错误 exit 2**（带人话），不得冒 traceback。

        判别性来自「不冒泡」：删掉 `_run_speed` 前置加载处的 `except OSError` 即红
        （会变成 traceback ⇒ 非 2，且把作者的用法错误洗成测量失败）。
        """
        _, exam, arm, evidence = _cli_fixtures(tmp_path)
        missing = tmp_path / "nope.json"
        cases = {
            "payload_file": lambda: _speed_argv(exam, arm, evidence, tmp_path, payload_file=missing),
            "arm": lambda: _speed_argv(exam, missing, evidence, tmp_path),
            "round_evidence": lambda: _speed_argv(exam, arm, evidence, tmp_path, round_evidence=missing),
        }
        for key, build in cases.items():
            capsys.readouterr()
            assert cli.main(build()) == 2, f"--{key} 缺失未判用法错误"
            captured = capsys.readouterr()
            text = captured.out + captured.err
            assert "用法错误" in text and "Traceback" not in text, f"--{key} 缺失报错不合格：{text!r}"

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


# ── CLI：真跑装配（假驱动，零网络）——白名单/引用表在两线报告上必须自足 ─────
def _canned_pages(pages: int = 2) -> list[dict]:
    out = []
    for page_no in range(1, pages + 1):
        resp = {
            "page": page_no,
            "duration_ms": {"total": 1000.0, "request_total_ms": 1010.0},
            "pipeline_status": {"ocr_has": {"stage_duration_ms": {
                "prepare": 100.0, "ocr": 200.0, "has_ner": 600.0, "match": 50.0, "draw": 40.0,
                "total": 990.0, "has_text_cache_status": "miss"}}},
        }
        out.append(collect.page_signal(resp, wall_s=1.2, harness_span=(0.0, 1.2), page_no=page_no))
    return out


def _canned_round(*, with_batch: bool = False) -> dict:
    pages = _canned_pages()
    agg = collect.sum_pages(pages)
    agg["observed_page_concurrency"] = 2  # driver 在 single 线真实写入（driver.py:347）
    record = {
        "pages": pages, "agg": agg, "file_wall_s": 1.2, "driver_self_wall_s": 1.21,
        "items": [{  # 形状对齐 driver._parse_view + items_view，逐个数值叶子都会被白名单过一遍
            "id": "f1", "pages": 2, "file_id": "fid-1", "unsupported": False, "reason": None,
            "md5": None, "parse": {"file_type": "pdf", "is_scanned": True, "total_pages": 2},
            "box_count": 12.0, "entity_count": 3.0,
        }],
        "rejected_items": [],
        "sidecar": {"box_count": 12.0, "entity_count": 3.0},
        "cache": {"histogram": agg.get("cache_histogram"), "hits": agg.get("cache_hits")},
    }
    if with_batch:
        record["batch"] = {
            "job_id": "job-1", "job_status": "completed", "item_statuses": {"completed": 1},
            "items_total": 1, "items_terminal": 1, "failed_items": 0, "upload_sec": 0.5,
            "max_upload_bytes": 9000000, "recognition_page_concurrency": 3.0,
            "recognition_page_concurrency_configured": 3.0, "recognition_wall_ms": 2400.0,
            "recognition_page_duration_sum_ms": 2200.0, "recognition_parallelism_ratio": 1.5,
            "job_queue_wait_ms": 12.0,
        }
    return record


class _StubApi:
    """替身客户端：不建连；只补 CLI 会调用的两个面。"""

    def __init__(self, *args, **kwargs):
        # ②冷评审 I15：D2.api_base 现在读客户端的 trust_env（是否为绕过环境代理的证据）
        self.client = types.SimpleNamespace(trust_env=False)

    def close(self):
        pass

    def default_type_ids(self):
        return ["type-1"]


def _run_cli_with_stub(monkeypatch, tmp_path, *, driver_name: str, with_batch: bool = False,
                       records: list[dict] | None = None, rounds: int = 2, warmup_rounds: int = 1):
    """跑一遍 CLI 装配链。`records` 非空时按轮次依次吐出（用于造非法轮/离散度场景）。"""
    _, exam, arm, evidence = _cli_fixtures(tmp_path)
    monkeypatch.setattr(driver, "SpeedApi", _StubApi)
    canned = _canned_round(with_batch=with_batch)
    queue = [dict(r) for r in records] if records else []
    target = "measure_single_round" if driver_name == "single" else "measure_batch_round"

    def _measure(*_args, **_kwargs):
        return queue.pop(0) if queue else dict(canned)

    monkeypatch.setattr(driver, target, _measure)
    code = cli.main(_speed_argv(exam, arm, evidence, tmp_path, driver=driver_name,
                                cache_policy="warm", rounds=rounds, warmup_rounds=warmup_rounds))
    doc = json.loads((tmp_path / "report" / "unit.json").read_text(encoding="utf-8"))
    return code, doc


class TestCliReportCompleteness:
    """整条装配链（cli → build_report → finalize）跑通后，白名单/引用表必须自足。

    这两条的价值在于：**逐页/逐轮/批作业的每个数值叶子都真的过一遍路径表**——
    单测 fixture 手搓的报告结构覆盖不到 cli 现场拼出来的字段，漏一个就在门④ 变 FAIL。
    """

    @staticmethod
    def _failed(doc: dict) -> set[str]:
        return {c["id"] for c in doc["checks"] if c["status"] == "FAIL"}

    @staticmethod
    def _status(doc: dict, check_id: str) -> str | None:
        return _check_status(doc["checks"], check_id)

    def test_invalid_timing_round_is_marked_and_excluded(self, tmp_path, monkeypatch):
        """C2 反向用例：零页计时轮必须标记 invalid 并**从计时聚合里剔除**（改 `invalid=False` 即红）。"""
        empty = _canned_round()
        empty.update({"pages": [], "agg": collect.sum_pages([]), "items": [], "sidecar": {},
                      "file_wall_s": 99.0, "driver_self_wall_s": 99.0})
        code, doc = _run_cli_with_stub(monkeypatch, tmp_path, driver_name="single",
                                       records=[empty, _canned_round()], rounds=2, warmup_rounds=0)
        assert code != 2
        assert doc["rounds"][0]["invalid"] is True, doc["rounds"][0]
        assert doc["rounds"][0]["invalid_reason"], "非法轮必须记录原因"
        assert "D1.round_validity" in self._failed(doc)
        assert doc["summary"]["file_wall_s"]["n"] == 1, (
            "非法轮不得进入计时聚合", doc["summary"]["file_wall_s"])
        assert doc["summary"]["pages_measured_timing"] == 2

    def test_timing_discretion_status_comes_from_driver(self, tmp_path, monkeypatch):
        """C3 反向用例：`D2.timing_rounds` 状态必须取自 driver 三态（映射成 PASS 即红）。"""
        tight = []
        for wall in (1.00, 1.00, 1.02):
            rec = _canned_round()
            rec.update({"file_wall_s": wall, "driver_self_wall_s": wall})
            tight.append(rec)
        _code, doc = _run_cli_with_stub(monkeypatch, tmp_path, driver_name="single",
                                        records=[_canned_round(), *tight], rounds=4, warmup_rounds=1)
        assert doc["summary"]["file_wall_s"]["n"] == 3
        assert self._status(doc, "D2.timing_rounds") == "PASS", (
            self._status(doc, "D2.timing_rounds"), doc["summary"]["file_wall_s"])
        wide = []
        for wall in (1.0, 3.0, 9.0, 20.0):
            rec = _canned_round()
            rec.update({"file_wall_s": wall, "driver_self_wall_s": wall})
            wide.append(rec)
        _code2, doc2 = _run_cli_with_stub(monkeypatch, tmp_path, driver_name="single",
                                          records=[_canned_round(), *wide], rounds=5, warmup_rounds=1)
        assert doc2["summary"]["file_wall_s"]["n"] == 4
        assert self._status(doc2, "D2.timing_rounds") == "UNVERIFIED", (
            "补满 4 个计时轮仍高离散只能是 UNVERIFIED", self._status(doc2, "D2.timing_rounds"))

    def test_single_report_is_whitelist_clean(self, tmp_path, monkeypatch):
        code, doc = _run_cli_with_stub(monkeypatch, tmp_path, driver_name="single")
        assert code != 2, "合法命令不得判用法错"
        assert doc["rounds"], "报告必须落进轮次"
        assert doc["rounds"][-1]["sidecar"]["box_count"] == 12.0, "D8① 旁证必须落进报告"
        assert "batch" not in doc["rounds"][-1], "single 报告不得含 batch 键（D2④）"
        failed = self._failed(doc)
        assert "D5.numeric_whitelist" not in failed, [c for c in doc["checks"]
                                                     if c["id"] == "D5.numeric_whitelist"]
        assert "D4.criteria_refs" not in failed
        assert "D5.no_quality_fields" not in failed

    def test_batch_report_keeps_job_evidence_and_is_whitelist_clean(self, tmp_path, monkeypatch):
        code, doc = _run_cli_with_stub(monkeypatch, tmp_path, driver_name="batch", with_batch=True)
        assert code != 2
        batch = doc["rounds"][-1]["batch"]
        assert batch["max_upload_bytes"] == 9000000, "D6④ 请求体上限证据必须落进报告"
        assert batch["job_queue_wait_ms"] == 12.0, "D2④ 排队等待必须落进报告"
        failed = self._failed(doc)
        assert "D5.numeric_whitelist" not in failed, [c for c in doc["checks"]
                                                     if c["id"] == "D5.numeric_whitelist"]
        assert "D2.batch_key_set" not in failed
        assert "D6.batch_upload_bytes" not in failed, [c for c in doc["checks"]
                                                      if c["id"] == "D6.batch_upload_bytes"]

    def test_batch_upload_bytes_check_is_falsifiable(self):
        """D6④：缺键 / 体量小于语料最大件 / 正常三种情形必须三态分明。"""
        cli = boot.load("cli.py")
        good = [{"batch": {"max_upload_bytes": 9000000}}]
        missing = [{"batch": {"job_id": "job-1"}}]
        truncated = [{"batch": {"max_upload_bytes": 1024}}]

        passed = cli._batch_upload_checks(good, corpus_max_bytes=8388608)[0]
        assert passed["status"] == "PASS" and passed["value"] == 9000000.0

        absent = cli._batch_upload_checks(missing, corpus_max_bytes=8388608)[0]
        assert absent["status"] == "FAIL" and "缺 max_upload_bytes" in absent["detail"]

        cut = cli._batch_upload_checks(truncated, corpus_max_bytes=8388608)[0]
        assert cut["status"] == "FAIL" and "被代理截断" in cut["detail"]

        # 无计时轮 ⇒ 不产判据（由 no-data 降级统一处置），不得伪装 PASS
        assert cli._batch_upload_checks([{"warmup": True, "batch": {"max_upload_bytes": 9000000}}],
                                        corpus_max_bytes=8388608) == []


    def test_footnote_must_cross_check_image_dtk_and_hardware(self):
        """②冷评审 C5：手填型号/DTK 版本必须被指纹交叉核验抓住（原来只交叉 instance）。"""
        fp = report.build_fingerprints(api_base="http://127.0.0.1:8000")
        fp["hardware_raw"] = "Hygon C86 7185 32-core Processor"
        fp["dtk_version_raw"] = "DTK-24.04.1"
        good = _footnote(hardware=fp["hardware_raw"], image_dtk=fp["dtk_version_raw"])
        errors = report.validate_footnote(good, api_base="http://127.0.0.1:8000",
                                          live_code_rev="0" * 40, fingerprints=fp)
        assert not [e for e in errors if "image_dtk" in e or "hardware" in e], errors

        fake = _footnote(hardware="假型号", image_dtk="DTK-9.99.9-FAKE")
        errors = report.validate_footnote(fake, api_base="http://127.0.0.1:8000",
                                          live_code_rev="0" * 40, fingerprints=fp)
        assert any("image_dtk" in e and "dtk_version_raw" in e for e in errors), errors
        assert any("hardware" in e and "hardware_raw" in e for e in errors), errors

    def test_fingerprint_sha256_tamper_is_caught(self):
        """I14 守卫之一：指纹内容被改但 sha256 未同步 ⇒ 必须报错。"""
        fp = report.build_fingerprints(api_base="http://127.0.0.1:8000")
        fp["hostname"] = "tampered-host"
        errors = report.validate_fingerprints(fp)
        assert any("sha256" in e for e in errors), errors

    def test_quality_eval_must_stay_not_run(self):
        """I14 守卫之一：`quality_eval` 是声明位，值必须恒为 'not_run'。"""
        assert report.quality_key_errors({"quality_eval": "not_run"}) == []
        errors = report.quality_key_errors({"quality_eval": "f1=0.9"})
        assert any("not_run" in e for e in errors), errors

    def test_redact_path_covers_home_and_unc(self):
        """I13：`~` 与 UNC 同属绝对路径，必须压前缀；普通相对路径保持可重放。"""
        assert report.redact_path("/root/ab/exam_d1.json") == "<abs>/…/exam_d1.json"  # public-tree-scan: ignore-line
        assert report.redact_path("~/private/x.json") == "<abs>/…/x.json"
        assert report.redact_path(r"\\srv\share\report.json") == "<abs>/…/report.json"
        assert report.redact_path("out/report.json") == "out/report.json"

    def test_redact_command_masks_credentials_and_paths(self):
        """I13：口令类开关的后一个 token 整体替换；值内 Bearer/口令字面量抹掉。"""
        masked = report.redact_command(
            ["python", "eval-ng/cli.py", "speed", "/root/ab/exam.json", "--api-pass", "EvalUser!2026",  # public-tree-scan: ignore-line
             "--api-base", "http://127.0.0.1:8000"]
        )
        joined = " ".join(masked)
        assert "EvalUser!2026" not in joined, joined
        assert "<redacted>" in masked
        assert "/root/ab" not in joined, joined  # public-tree-scan: ignore-line
        assert any(report.scrub_secrets(v) == v for v in ("plain",))  # 无凭据值原样

        env = report.redact_env({"API_PASS": "EvalUser!2026", "HAS_NER_GLOBAL_MAX_INFLIGHT": "8",
                                 "NOTE": "Bearer abcdefghijklmnopqrstuvwxyz012345"})  # public-tree-scan: ignore-line
        assert env["API_PASS"] == "<redacted>"
        assert env["HAS_NER_GLOBAL_MAX_INFLIGHT"] == "8"
        assert "abcdefghijklmnopqrstuvwxyz" not in env["NOTE"]

    def test_hardware_raw_skips_banner_lines(self):
        """I17：hy-smi 首行是横幅，取证力为零 ⇒ 必须取第一条承载信息的行。"""
        assert report._first_meaningful_line("=== System Management Interface ===\n") == ""
        assert report._first_meaningful_line("----\nHygon C86 7185 32-core Processor\n") == (
            "Hygon C86 7185 32-core Processor")

    def test_hardware_capture_prefers_device_identity_over_table_header(self):
        """④云门实测：DCU 上 `hy-smi` 首条可用行是**表头**，当「硬件」证据价值为零。

        `hy-smi --showhw` 才给设备身份（DID/SSID/VBIOS/BUS）⇒ 捕获应跳过表头、取设备行并保留表头做注。
        """
        usage = (
            "================================= System Management Interface "
            "==================================\n"
            "=============================================================\n"
            "HCU     Temp     AvgPwr     Perf     PwrCap     VRAM%      HCU%      Mode\n"
            "0       45.0     120.0      Normal   300.0      10%        0%        Normal\n"
        )
        assert report._looks_like_table_header(
            "HCU     Temp     AvgPwr     Perf     PwrCap     VRAM%      HCU%      Mode") is True
        assert report._looks_like_table_header("Hygon C86 7185 32-core Processor") is False
        got = report._hardware_capture(usage)
        assert "Mode" in got and "45.0" in got and "｜" in got, got
        # 表头被跳过、设备行成为「第一条承载信息的行」
        assert report._first_meaningful_line(usage).startswith("0       45.0")
        # 只有表头（无设备行）⇒ 不得把表头当硬件证据
        assert report._hardware_capture("HCU DID SSID GFX RAS SDMA RAS UMC RAS VBIOS BUS") != (
            "HCU DID SSID GFX RAS SDMA RAS UMC RAS VBIOS BUS")
        # 形如 `nvidia-smi -L` 的型号行（不以序号开头）仍要能取到
        assert report._hardware_capture("GPU 0: NVIDIA GeForce RTX 5090 (UUID: x)\n") == (
            "GPU 0: NVIDIA GeForce RTX 5090 (UUID: x)")

    def test_api_base_check_is_discriminative(self):
        """I15：`D2.api_base` 原来无条件 PASS；现在必须由 trust_env 决定。"""
        ev = {"rounds": [{"round": 1, "pid": 1, "start_time": "1",
                          "timestamp": "2026-10-16T01:00:00+0800"}], "env": {"A": "1"},
              "ready_wait_sec": 20.0, "settle_sec": 10.0}
        ok = driver.cold_evidence_checks(ev, [{"round": 1, "file_wall_s": 1.0}], cache_policy="cold",
                                         driver="single", api_base="http://127.0.0.1:8000",
                                         restart_cmd="bash r.sh", trust_env=False)
        bad = driver.cold_evidence_checks(ev, [{"round": 1, "file_wall_s": 1.0}], cache_policy="cold",
                                          driver="single", api_base="http://127.0.0.1:8000",
                                          restart_cmd="bash r.sh", trust_env=True)
        unknown = driver.cold_evidence_checks(ev, [{"round": 1, "file_wall_s": 1.0}], cache_policy="cold",
                                              driver="single", api_base="http://127.0.0.1:8000",
                                              restart_cmd="bash r.sh")
        assert _check_status(ok, "D2.api_base") == "PASS"
        assert _check_status(bad, "D2.api_base") == "FAIL"
        assert _check_status(unknown, "D2.api_base") == "UNVERIFIED"

    def test_cold_start_time_must_be_unique(self):
        """I14 守卫之一：pid 不同但 start_time 全同 ⇒ 不是真的重启，必须 FAIL。"""
        rounds = [{"round": 1, "file_wall_s": 1.0}, {"round": 2, "file_wall_s": 1.0}]
        ev = {"rounds": [
            {"round": 1, "pid": 11, "start_time": "1905766371", "timestamp": "t1"},
            {"round": 2, "pid": 22, "start_time": "1905766371", "timestamp": "t2"},
        ], "env": {"A": "1"}, "ready_wait_sec": 1.0, "settle_sec": 1.0}
        checks = driver.cold_evidence_checks(ev, rounds, cache_policy="cold", driver="single",
                                             api_base="http://127.0.0.1:8000", restart_cmd="bash r.sh",
                                             trust_env=False)
        assert _check_status(checks, "D2.cold_evidence") == "FAIL", checks
        ev["rounds"][1]["start_time"] = "1905766399"
        checks = driver.cold_evidence_checks(ev, rounds, cache_policy="cold", driver="single",
                                            api_base="http://127.0.0.1:8000", restart_cmd="bash r.sh",
                                            trust_env=False)
        assert _check_status(checks, "D2.cold_evidence") != "FAIL", checks

    def test_arm_post_dirty_fails(self, monkeypatch):
        """I14 守卫之一：patch 臂跑完工作树必须复原，脏即 FAIL（不许静默继续）。"""
        arm = arms.load_arm(str(EVALNG_ROOT / "arms" / "example-patch-k1.json"))
        monkeypatch.setattr(arms, "git_porcelain",
                            lambda *args, **kwargs: [" M frontend/src/constants/timing.ts"])
        checks = arms.arm_checks(arm, phase="post", ignore_paths=[])
        assert _check_status(checks, "D8.patch_arm_revert") == "FAIL", checks

    def test_anchor_band_is_observation_not_criterion(self):
        """方案 §7：历史绝对带只做 P4 **观测**（out_of_band 标记），绝不进 checks、不改判定。"""
        cli = boot.load("cli.py")
        arm = {"name": "k3", "expect_p50_s": 86.65}
        obs = cli._speed_anchor_observation(arm, {"file_wall_s": {"p50": 92.9}})
        assert obs["anchor_band"]["out_of_band"] is False, obs
        assert obs["anchor_band"]["measured_p50_s"] == 92.9
        out = cli._speed_anchor_observation(arm, {"file_wall_s": {"p50": 200.0}})
        assert out["anchor_band"]["out_of_band"] is True, out
        assert cli._speed_anchor_observation({"name": "k3"}, {"file_wall_s": {"p50": 92.9}}) == {}
        assert cli._speed_anchor_observation(arm, {"file_wall_s": {"p50": None}}) == {}

    def test_baseline_report_gives_anchor_ratio(self, tmp_path):
        """I16/AC1：`--baseline` 交本模块另一臂报告 ⇒ D1① 比值 ≥1.8× 有机器落点。"""
        cli = boot.load("cli.py")
        base = tmp_path / "k1.json"
        base.write_text(json.dumps({"mode": {"page_concurrency": 1},
                                    "summary": {"file_wall_s": {"p50": 180.0}}}), encoding="utf-8")
        doc = cli._load_baseline(str(base))
        ok = cli._speed_baseline_checks(doc, mode={"page_concurrency": 3},
                                        summary={"file_wall_s": {"p50": 90.0}})
        assert _check_status(ok, "D1.anchor_ratio") == "PASS", ok
        bad = cli._speed_baseline_checks(doc, mode={"page_concurrency": 3},
                                         summary={"file_wall_s": {"p50": 130.0}})
        assert _check_status(bad, "D1.anchor_ratio") == "FAIL", bad
        same = cli._speed_baseline_checks(doc, mode={"page_concurrency": 1},
                                         summary={"file_wall_s": {"p50": 95.0}})
        assert _check_status(same, "D1.anchor_ratio") == "UNVERIFIED", same  # 同 k ⇒ 比值无判别力
        # 无基线时也必须明确表态（单一出口：不再有第二套实现重复落 D7.parity）
        none_case = cli._speed_baseline_checks(None, mode={"page_concurrency": 3}, summary={})
        assert [c["id"] for c in none_case] == ["D7.parity"]
        assert none_case[0]["status"] == "UNVERIFIED"

    def test_baseline_benchmark_report_gives_parity(self, tmp_path):
        """D7：`--baseline` 交 benchmark_throughput.py 产物 ⇒ 口径差 ≤20% 机判。"""
        cli = boot.load("cli.py")
        bench = tmp_path / "bench.json"
        bench.write_text(json.dumps({"pages_per_min": 2.4, "page_ms_p50": 24900.0}), encoding="utf-8")
        bench_doc = cli._load_baseline(str(bench))
        ok = cli._speed_baseline_checks(bench_doc, mode={"page_concurrency": 2},
                                        summary={"pages_per_min": 2.5, "page_ms_p50": 25000.0})
        assert _check_status(ok, "D7.parity") == "PASS", ok
        off = cli._speed_baseline_checks(bench_doc, mode={"page_concurrency": 2},
                                         summary={"pages_per_min": 4.0, "page_ms_p50": 25000.0})
        assert _check_status(off, "D7.parity") == "FAIL", off
        alien = cli._speed_baseline_checks(bench_doc, mode={"page_concurrency": 2},
                                          summary={"pages_per_min": None, "page_ms_p50": None})
        assert _check_status(alien, "D7.parity") == "UNVERIFIED", alien
        broken = tmp_path / "broken.json"
        broken.write_text("{not json", encoding="utf-8")
        with pytest.raises(cli._Usage):  # 测量前就失败（CLI 转 exit 2），不冒 traceback
            cli._load_baseline(str(broken))
        with pytest.raises(cli._Usage):
            cli._load_baseline(str(tmp_path / "missing.json"))

    def test_self_sufficiency_names_missing_fields(self):
        """I16：报告自足性缺项必须点名 FAIL；warm 轮不要求 cold_evidence。"""
        cli = boot.load("cli.py")
        mode = {"driver": "single", "page_concurrency": 3, "cache_policy": "cold",
                "warmup_rounds": 1, "timing_rounds": 3, "payload_sha256": "a" * 64,
                "api_base": "http://127.0.0.1:8000"}
        summary = {"file_wall_s": {"p50": 1.0}, "pages_per_min": 6.0, "page_ms_p50": 25000.0,
                   "pages_measured_timing": 30}
        arm = {"name": "k3", "kind": "env"}
        ok = cli._speed_self_sufficiency_checks(mode=mode, arm=arm, summary=summary,
                                                evidence={"rounds": [{"round": 1}]})
        assert ok[0]["status"] == "PASS", ok
        bad = cli._speed_self_sufficiency_checks(mode={**mode, "payload_sha256": None}, arm=arm,
                                                 summary={**summary, "pages_per_min": None},
                                                 evidence={"rounds": []})
        assert bad[0]["status"] == "FAIL" and "payload_sha256" in bad[0]["detail"], bad
        warm = cli._speed_self_sufficiency_checks(mode={**mode, "cache_policy": "warm"}, arm=arm,
                                                  summary=summary, evidence={"rounds": []})
        assert warm[0]["status"] == "PASS", warm

# ── 脱敏扫描器（D5④） ──────────────────────────────────────────────────────
class TestRedactionScanner:
    def test_self_test_positive_control(self):
        assert redact.self_test() == []
        assert redact.main(["--self-test"]) == 0

    def test_scan_rules(self):
        hits = redact.scan("ip 10.1.2.3 path /root/ab/d3.json win C:\\Users\\dev\\a.txt",  # public-tree-scan: ignore-line
                           denylist=[], allow_prefixes=[])
        rules = {rule for rule, _ in hits}
        assert {"ip", "abs_path", "win_path"} <= rules
        assert redact.scan("ip 127.0.0.1 ok", denylist=[], allow_prefixes=[]) == []
        assert redact.scan("path /root/ab/d3.json", denylist=[],  # public-tree-scan: ignore-line
                           allow_prefixes=["/root/ab"]) == []  # public-tree-scan: ignore-line
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
