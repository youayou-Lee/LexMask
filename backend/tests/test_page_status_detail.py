"""Issue#279 Task 8 · A4.1/A4.2 数据面：page-status 响应带 per-page detail 与 route_summary。

口径（plan 绑定语义，两测自洽）：

* 卷级 ``route_summary``：任一页 ``text_layer_doc.ran=True`` →
  ``{"verdict":"docline","fallback_reason_code":None}``；否则取首个非空
  ``fallback_reason_code`` → ``{"verdict":"page_pipeline","fallback_reason_code":<码>}``；
  两无 → ``None``。
* per-page ``detail.route_verdict``：该页 ``text_layer_doc.ran=True`` → ``"docline"``，
  否则 ``"page_pipeline"``；``fallback_reason_code``/``coverage`` 透传；
  ``stage_ms`` = 页记录 ``duration_breakdown_ms``；``error_type`` 来自页记录
  （成功页 None）；``attempts = page_retry_counts[str(p)] + 1``（无账 = 1）。

A4.3：这是既有 page-status 响应的纯只读扩展——不新增任何 JSONL 读取接口。
"""

from __future__ import annotations

from app.services import page_status_service


def _item_with_pages(perf: dict) -> dict:
    return {
        "id": "item-detail-1",
        "job_id": "job-1",
        "status": "awaiting_review",
        "performance": perf,
        "file_type": "pdf_scanned",
        "file_id": "f1",
    }


def test_page_detail_exposes_route_and_error_type():
    item = _item_with_pages(
        {
            "recognition": {
                "pages": {
                    "1": {
                        "page": 1,
                        "status": "completed",
                        "duration_ms": 100,
                        "duration_breakdown_ms": {"ocr_has": 90},
                        "pipeline_status": {"text_layer_doc": {"ran": False, "fallback_reason_code": "no_text_layer"}},
                    },
                    "2": {
                        "page": 2,
                        "status": "failed",
                        "duration_ms": 50,
                        "error": "ValueError: boom",
                        "error_type": "ValueError",
                        "pipeline_status": {"text_layer_doc": {"ran": False, "fallback_reason_code": "no_text_layer"}},
                    },
                },
                "page_retry_counts": {"2": 2},
                "failed_pages": {"2": "ValueError: boom"},
            }
        }
    )
    out = page_status_service.build_page_status(item, {"page_count": 2, "file_type": "pdf_scanned"})
    d1 = next(p for p in out["pages"] if p["page"] == 1)["detail"]
    assert d1["route_verdict"] == "page_pipeline" and d1["stage_ms"] == {"ocr_has": 90}
    assert d1["attempts"] == 1 and d1["error_type"] is None
    d2 = next(p for p in out["pages"] if p["page"] == 2)["detail"]
    assert d2["route_verdict"] == "page_pipeline" and d2["fallback_reason_code"] == "no_text_layer"
    assert d2["error_type"] == "ValueError" and d2["attempts"] == 3
    assert out["route_summary"] == {"verdict": "page_pipeline", "fallback_reason_code": "no_text_layer"}


def test_route_summary_docline_takeover():
    item = _item_with_pages(
        {
            "recognition": {
                "pages": {
                    "1": {
                        "page": 1,
                        "status": "completed",
                        "duration_ms": 100,
                        "pipeline_status": {"text_layer_doc": {"ran": True, "verdict": "docline", "coverage": 0.9}},
                    },
                },
            }
        }
    )
    out = page_status_service.build_page_status(item, {"page_count": 1, "file_type": "pdf_scanned"})
    assert out["route_summary"] == {"verdict": "docline", "fallback_reason_code": None}


def test_route_summary_none_when_no_route_evidence():
    """两无（无 docline 接管、无 fallback 码）→ route_summary 为 None。"""
    item = _item_with_pages(
        {
            "recognition": {
                "pages": {
                    "1": {"page": 1, "status": "completed", "duration_ms": 100},
                },
            }
        }
    )
    out = page_status_service.build_page_status(item, {"page_count": 1, "file_type": "pdf_scanned"})
    assert out["route_summary"] is None
