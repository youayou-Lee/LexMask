"""Issue#279 A2.1 前置：docline 10 闸 → route_decided 事件逐闸钉（闸码/verdict/signals/快照）。

对 task-4-brief 示例测试的两处必要适配（实现事实核对后，断言语义不变）：
* `_build` 是 async 且 `ocr_has_types` 为必填位置参数 ⇒ 统一经 `asyncio.run` +
  显式传 `None` 调用（与 test_issue243 的 `_run_build` 同法）；
* 无上下文 fail-open 用例的丢弃计数是 **+2** 而非示例的 +1：提取点
  `text_layer_extracted` 与闸 `route_decided` 在同一无上下文构建里各试写一次，
  writer 各丢弃计 1（这正是「事件纯旁路、闸行为不变」的完整钉法）。
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.services import page_event_log as pel
from app.services import text_layer_doc_source as tds


@pytest.fixture()
def log_env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "PAGE_EVENT_LOG_ENABLED", True)
    monkeypatch.setattr(settings, "PAGE_EVENT_LOG_DIR", str(tmp_path / "logs"))
    pel.reset_for_tests()
    yield tmp_path / "logs"
    pel.reset_for_tests()


def _events(log_env, job_id="jobg", item_id="itemg"):
    path = log_env / job_id / f"{item_id}.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _snapshot(tmp_path, monkeypatch):
    """_build 前置依赖最小化：真实存在的文件路径（否则 :148 直接 return None 到不了闸）
    + 水印探针打桩为 miss（junk 文件过真探针行为不可控）。"""
    path = tmp_path / "fake.pdf"
    path.write_bytes(b"%PDF-1.4 fake")
    monkeypatch.setattr(tds.watermark_overlay, "render_source",
                        lambda p: (None, {"code": "miss"}))
    return {"file_path": str(path), "file_type": "pdf_scanned"}


def _pages(n):
    """TextLayerPage 替身：words 留空 ⇒ chars 计 0，no 1-based。"""
    return [SimpleNamespace(no=i + 1, words=[]) for i in range(n)]


def _run(snapshot, labels=None):
    return asyncio.run(tds._build("fileg", snapshot, labels or [], None, None))


def _patch_usable_downstream(monkeypatch, **ner_overrides):
    """verdict 之后才能到的闸：质量判定打桩 USABLE、重复片段 0、NER 返回可控替身。"""
    metrics = SimpleNamespace(ink_alignment=1.0,
                              as_dict=lambda: {"page_count": 2, "word_count": 10})
    verdict = SimpleNamespace(usable=True, severity="USABLE", reasons=())
    monkeypatch.setattr(tds.docline, "sample_ink_alignment", lambda *a, **k: 1.0)
    monkeypatch.setattr(tds.quality, "compute_metrics",
                        lambda page_texts, ink_alignment=None: metrics)
    monkeypatch.setattr(tds.quality, "classify", lambda m: verdict)
    monkeypatch.setattr(tds.docline, "measure_repeated_fragment_coverage",
                        lambda pages: {"coverage": 0.0, "fragment_kinds": 0,
                                       "max_fragment_pages": 0})
    result = SimpleNamespace(
        pages=2, chunks=ner_overrides.get("chunks", 1), failed_chunks=0,
        map_failure_ratio=0.0, http_errors=0, truncated_chunks=0,
        coverage_ratio=0.95, entities=[], ner_calls=1, residual_calls=0,
        map_failures=[],
    )
    for key, value in ner_overrides.items():
        setattr(result, key, value)

    async def _fake_ner(pages, labels, **kwargs):
        return result

    monkeypatch.setattr(tds.docline, "run_document_ner", _fake_ner)
    return result


def test_no_text_layer_gate_emits_fallback(log_env, monkeypatch, tmp_path):
    monkeypatch.setattr(tds.docline, "extract_text_layer_pages", lambda path: {})
    with pel.event_context("jobg", "itemg"):
        with pytest.raises(tds._TransientIneligible):
            _run(_snapshot(tmp_path, monkeypatch))
    evs = [e for e in _events(log_env) if e["event"] == "route_decided"]
    assert evs and evs[-1]["gate"] == "no_text_layer"
    assert evs[-1]["verdict"] == "fallback:no_text_layer"
    assert evs[-1]["page"] is None and evs[-1]["source"] == "docline_gate"


def test_no_context_gate_drops_not_raises(log_env, monkeypatch, tmp_path):
    """fail-open：无识别上下文（预览等路径）时事件丢弃、闸行为不变。

    同一次无上下文构建里试写两条事件（text_layer_extracted + route_decided），
    writer 各丢弃计 1 ⇒ dropped_no_context +2，闸照抛 _TransientIneligible。
    """
    monkeypatch.setattr(tds.docline, "extract_text_layer_pages", lambda path: {})
    before = pel.stats()["dropped_no_context"]
    with pytest.raises(tds._TransientIneligible):
        _run(_snapshot(tmp_path, monkeypatch))
    assert pel.stats()["dropped_no_context"] == before + 2


def test_watermark_hit_gate_emits_fallback(log_env, monkeypatch, tmp_path):
    snapshot = _snapshot(tmp_path, monkeypatch)
    monkeypatch.setattr(tds.watermark_overlay, "render_source",
                        lambda p: (None, {"code": "hit"}))
    with pel.event_context("jobg", "itemg"):
        with pytest.raises(tds._TransientIneligible) as excinfo:
            _run(snapshot)
    assert excinfo.value.code == "watermark_overlay_hit"
    evs = [e for e in _events(log_env) if e["event"] == "route_decided"]
    assert evs and evs[-1]["gate"] == "watermark_overlay_hit"
    assert evs[-1]["verdict"] == "fallback:watermark_overlay_hit"
    assert evs[-1]["signals"] == {"probe_code": "hit"}
    assert evs[-1]["thresholds_snapshot"] == {}
    assert evs[-1]["page"] is None and evs[-1]["source"] == "docline_gate"
    assert isinstance(evs[-1]["config_hash"], str) and evs[-1]["config_hash"]


def test_build_budget_gate_emits_fallback(log_env, monkeypatch, tmp_path):
    monkeypatch.setattr(tds.docline, "extract_text_layer_pages",
                        lambda path: _pages(10))
    monkeypatch.setattr(settings, "TEXT_LAYER_DOCLINE_MAX_BUILD_SEC", 1.0)
    with pel.event_context("jobg", "itemg"):
        with pytest.raises(tds._TransientIneligible) as excinfo:
            _run(_snapshot(tmp_path, monkeypatch))
    assert excinfo.value.code == "build_budget"
    evs = [e for e in _events(log_env) if e["event"] == "route_decided"]
    assert evs and evs[-1]["gate"] == "build_budget"
    assert evs[-1]["verdict"] == "fallback:build_budget"
    assert evs[-1]["signals"] == {"pages": 10, "estimated_sec": 16.0, "budget_sec": 1.0}
    assert evs[-1]["thresholds_snapshot"] == {
        "TEXT_LAYER_DOCLINE_MAX_BUILD_SEC": 1.0,
        "TEXT_LAYER_DOCLINE_BUILD_SEC_PER_PAGE": 1.6,
    }


def test_verdict_unusable_gate_emits_fallback(log_env, monkeypatch, tmp_path):
    monkeypatch.setattr(tds.docline, "extract_text_layer_pages",
                        lambda path: _pages(1))
    monkeypatch.setattr(tds.docline, "sample_ink_alignment", lambda *a, **k: 0.0)
    with pel.event_context("jobg", "itemg"):
        with pytest.raises(tds._TransientIneligible) as excinfo:
            _run(_snapshot(tmp_path, monkeypatch))
    assert excinfo.value.code == "verdict_unusable"
    evs = [e for e in _events(log_env) if e["event"] == "route_decided"]
    assert evs and evs[-1]["gate"] == "verdict_unusable"
    assert evs[-1]["verdict"] == "fallback:verdict_unusable"
    assert evs[-1]["signals"]["ink_alignment"] == 0.0
    assert evs[-1]["signals"]["word_count"] == 0
    assert set(evs[-1]["thresholds_snapshot"]) == {
        "TEXT_LAYER_MIN_CHARS_PER_PAGE", "TEXT_LAYER_MAX_SHORT_WORD_RATIO",
        "TEXT_LAYER_MAX_GARBAGE_RATIO", "TEXT_LAYER_MIN_AVG_WORD_LEN",
        "TEXT_LAYER_MAX_WORD_CHARS", "TEXT_LAYER_MIN_INK_ALIGNMENT",
    }


def test_repeated_fragment_gate_emits_fallback(log_env, monkeypatch, tmp_path):
    monkeypatch.setattr(tds.docline, "extract_text_layer_pages",
                        lambda path: _pages(2))
    monkeypatch.setattr(settings, "TEXT_LAYER_MAX_REPEATED_FRAGMENT_COVERAGE", 0.5)
    _patch_usable_downstream(monkeypatch)
    monkeypatch.setattr(tds.docline, "measure_repeated_fragment_coverage",
                        lambda pages: {"coverage": 0.9, "fragment_kinds": 2,
                                       "max_fragment_pages": 5})
    with pel.event_context("jobg", "itemg"):
        with pytest.raises(tds._TransientIneligible) as excinfo:
            _run(_snapshot(tmp_path, monkeypatch))
    assert excinfo.value.code == "repeated_fragment_coverage"
    evs = [e for e in _events(log_env) if e["event"] == "route_decided"]
    assert evs and evs[-1]["gate"] == "repeated_fragment_coverage"
    assert evs[-1]["verdict"] == "fallback:repeated_fragment_coverage"
    assert evs[-1]["signals"] == {"coverage": 0.9, "fragment_kinds": 2,
                                  "max_fragment_pages": 5}
    assert evs[-1]["thresholds_snapshot"] == {
        "TEXT_LAYER_MAX_REPEATED_FRAGMENT_COVERAGE": 0.5,
    }


def test_failed_chunks_gate_emits_fallback(log_env, monkeypatch, tmp_path):
    monkeypatch.setattr(tds.docline, "extract_text_layer_pages",
                        lambda path: _pages(2))
    _patch_usable_downstream(monkeypatch, failed_chunks=1, chunks=2)
    with pel.event_context("jobg", "itemg"):
        with pytest.raises(tds._TransientIneligible) as excinfo:
            _run(_snapshot(tmp_path, monkeypatch))
    assert excinfo.value.code == "failed_chunks"
    evs = [e for e in _events(log_env) if e["event"] == "route_decided"]
    assert evs and evs[-1]["gate"] == "failed_chunks"
    assert evs[-1]["verdict"] == "fallback:failed_chunks"
    assert evs[-1]["signals"] == {"failed_chunks": 1, "chunks": 2}
    assert evs[-1]["thresholds_snapshot"] == {}


def test_map_failure_gate_emits_fallback(log_env, monkeypatch, tmp_path):
    monkeypatch.setattr(tds.docline, "extract_text_layer_pages",
                        lambda path: _pages(2))
    monkeypatch.setattr(settings, "TEXT_LAYER_MAX_MAP_FAILURE_RATIO", 0.3)
    _patch_usable_downstream(monkeypatch, map_failure_ratio=0.9, chunks=1)
    with pel.event_context("jobg", "itemg"):
        with pytest.raises(tds._TransientIneligible) as excinfo:
            _run(_snapshot(tmp_path, monkeypatch))
    assert excinfo.value.code == "map_failure_ratio"
    evs = [e for e in _events(log_env) if e["event"] == "route_decided"]
    assert evs and evs[-1]["gate"] == "map_failure_ratio"
    assert evs[-1]["verdict"] == "fallback:map_failure_ratio"
    assert evs[-1]["signals"] == {"ratio": 0.9}
    assert evs[-1]["thresholds_snapshot"] == {"TEXT_LAYER_MAX_MAP_FAILURE_RATIO": 0.3}


def test_block_errors_gate_emits_fallback(log_env, monkeypatch, tmp_path):
    monkeypatch.setattr(tds.docline, "extract_text_layer_pages",
                        lambda path: _pages(2))
    monkeypatch.setattr(settings, "TEXT_LAYER_MAX_BLOCK_ERRORS", 0)
    _patch_usable_downstream(monkeypatch, http_errors=3, chunks=1)
    with pel.event_context("jobg", "itemg"):
        with pytest.raises(tds._TransientIneligible) as excinfo:
            _run(_snapshot(tmp_path, monkeypatch))
    assert excinfo.value.code == "block_errors"
    evs = [e for e in _events(log_env) if e["event"] == "route_decided"]
    assert evs and evs[-1]["gate"] == "block_errors"
    assert evs[-1]["verdict"] == "fallback:block_errors"
    assert evs[-1]["signals"] == {"http_errors": 3, "chunks": 1}
    assert evs[-1]["thresholds_snapshot"] == {"TEXT_LAYER_MAX_BLOCK_ERRORS": 0}


def test_truncated_chunks_gate_emits_fallback(log_env, monkeypatch, tmp_path):
    monkeypatch.setattr(tds.docline, "extract_text_layer_pages",
                        lambda path: _pages(2))
    monkeypatch.setattr(settings, "TEXT_LAYER_MAX_TRUNCATED_RATIO", 0.5)
    _patch_usable_downstream(monkeypatch, truncated_chunks=1, chunks=1)
    with pel.event_context("jobg", "itemg"):
        with pytest.raises(tds._TransientIneligible) as excinfo:
            _run(_snapshot(tmp_path, monkeypatch))
    assert excinfo.value.code == "truncated_chunks"
    evs = [e for e in _events(log_env) if e["event"] == "route_decided"]
    assert evs and evs[-1]["gate"] == "truncated_chunks"
    assert evs[-1]["verdict"] == "fallback:truncated_chunks"
    assert evs[-1]["signals"] == {"truncated": 1, "chunks": 1, "ratio": 1.0}
    assert evs[-1]["thresholds_snapshot"] == {"TEXT_LAYER_MAX_TRUNCATED_RATIO": 0.5}


def test_coverage_gate_emits_fallback(log_env, monkeypatch, tmp_path):
    monkeypatch.setattr(tds.docline, "extract_text_layer_pages",
                        lambda path: _pages(2))
    monkeypatch.setattr(settings, "TEXT_LAYER_MIN_COVERAGE", 0.9)
    _patch_usable_downstream(monkeypatch, coverage_ratio=0.5, chunks=1)
    with pel.event_context("jobg", "itemg"):
        with pytest.raises(tds._TransientIneligible) as excinfo:
            _run(_snapshot(tmp_path, monkeypatch))
    assert excinfo.value.code == "coverage"
    evs = [e for e in _events(log_env) if e["event"] == "route_decided"]
    assert evs and evs[-1]["gate"] == "coverage"
    assert evs[-1]["verdict"] == "fallback:coverage"
    assert evs[-1]["signals"] == {"coverage": 0.5}
    assert evs[-1]["thresholds_snapshot"] == {"TEXT_LAYER_MIN_COVERAGE": 0.9}


def test_accepted_emits_route_decided_accepted(log_env, monkeypatch, tmp_path):
    monkeypatch.setattr(tds.docline, "extract_text_layer_pages",
                        lambda path: _pages(2))
    monkeypatch.setattr(settings, "TEXT_LAYER_MIN_COVERAGE", 0.9)
    result = _patch_usable_downstream(
        monkeypatch, coverage_ratio=0.95, chunks=3,
        entities=[SimpleNamespace(page=1, type="人名", value="张三",
                                  bbox=(0.1, 0.1, 0.3, 0.3))],
    )
    with pel.event_context("jobg", "itemg"):
        memo = _run(_snapshot(tmp_path, monkeypatch))
    assert isinstance(memo, dict) and memo["coverage"] == 0.95
    assert len(memo["boxes_by_page"][1]) == 1
    evs = [e for e in _events(log_env) if e["event"] == "route_decided"]
    assert evs and evs[-1]["gate"] == "coverage"
    assert evs[-1]["verdict"] == "accepted"
    assert evs[-1]["page"] is None and evs[-1]["source"] == "docline_gate"
    assert evs[-1]["signals"]["coverage"] == pytest.approx(0.95)
    assert evs[-1]["signals"]["chunks"] == 3
    assert evs[-1]["signals"]["entities"] == len(result.entities)
    assert evs[-1]["thresholds_snapshot"] == {"TEXT_LAYER_MIN_COVERAGE": 0.9}
    extracted = [e for e in _events(log_env) if e["event"] == "text_layer_extracted"]
    assert len(extracted) == 1
    assert extracted[0]["pages"] == 2 and extracted[0]["blocks"] == 2
    assert extracted[0]["chars"] == 0 and extracted[0]["page"] is None
