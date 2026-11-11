"""A2.5：summary 全字段——注入已知事件断言字段值，防「抄 DB 三样交差」。"""
import json
from pathlib import Path

from app.services.page_event_summary import compute_summary


def _write_events(job_dir: Path, item_id: str, events: list[dict]) -> None:
    d = job_dir
    d.mkdir(parents=True, exist_ok=True)
    with (d / f"{item_id}.jsonl").open("w", encoding="utf-8") as fh:
        for e in events:
            base = {"job_id": "j1", "item_id": item_id, "page": None, "event": "x", "ts": "2026-11-12T00:00:00Z"}
            base.update(e)
            fh.write(json.dumps(base, ensure_ascii=False) + "\n")


def test_summary_full_fields_from_injected_events(tmp_path):
    job = tmp_path / "j1"
    _write_events(job, "item1", [
        {"event": "route_decided", "source": "docline_gate", "gate": "coverage", "verdict": "accepted"},
        {"event": "text_layer_extracted", "chars": 10, "blocks": 3, "pages": 2, "ms": 5},
        {"event": "ner_done", "calls": 6, "ms": 100, "entities": 8},
        {"event": "page_done", "page": 1, "ms": 500, "regions": 3, "source": "docline"},
        {"event": "page_done", "page": 2, "ms": 1500, "regions": 5, "source": "docline"},
        {"event": "page_failed", "page": 3, "stage": "ner", "error_type": "BudgetExceeded",
         "message_sanitized": "m", "traceback_frames": [], "attempts": 1, "ms": 2000},
    ])
    _write_events(job, "item2", [
        {"event": "route_decided", "source": "docline_gate", "gate": "no_text_layer", "verdict": "fallback:no_text_layer"},
        {"event": "ocr_done", "page": 1, "ms": 300, "regions": 2, "source": "ocr"},
        {"event": "page_done", "page": 1, "ms": 700, "regions": 2, "source": "page_pipeline"},
        {"event": "retry_started", "page": 1, "attempt": 2, "trigger": "manual"},
        {"event": "retry_done", "page": 1, "attempt": 2, "outcome": "success", "ms": 600},
        {"event": "page_done", "page": 1, "ms": 600, "regions": 2, "source": "retry"},
    ])
    s = compute_summary(job)
    assert s["page_count"] == 3
    assert s["pages_by_source"] == {"docline": 2, "page_pipeline": 0, "retry": 1}
    assert s["failed_pages"] == {"3": "BudgetExceeded"}
    assert s["slowest_page"] == {"page": 3, "ms": 2000}
    assert s["avg_page_ms"] == round((500 + 1500 + 2000) / 3)
    assert s["ner_calls_total"] == 6 and s["ner_ms_total"] == 100 and s["ocr_ms_total"] == 300
    assert s["retry_count"] == 1
    assert s["saved_by_accelerator"] == 0 and s["upgraded_pages"] == 0 and s["arbitrated_pages"] == 0
    assert "generated_at" in s


def test_summary_pages_by_source_counts_latest_page_done(tmp_path):
    """同一页多次 page_done（重试翻绿）按最后一条计 source。"""
    job = tmp_path / "j2"
    _write_events(job, "item1", [
        {"event": "page_done", "page": 1, "ms": 100, "regions": 1, "source": "page_pipeline"},
        {"event": "retry_started", "page": 1, "attempt": 2, "trigger": "manual"},
        {"event": "retry_done", "page": 1, "attempt": 2, "outcome": "success", "ms": 50},
        {"event": "page_done", "page": 1, "ms": 50, "regions": 1, "source": "retry"},
    ])
    s = compute_summary(job)
    assert s["pages_by_source"] == {"docline": 0, "page_pipeline": 0, "retry": 1}
    assert s["retry_count"] == 1
