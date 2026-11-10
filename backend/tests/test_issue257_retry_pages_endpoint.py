# Copyright 2026 LexMask Contributors

"""Issue#257 一期 · 单页重试端点契约（AC2.3）。

与 ``test_issue257_retry_pages_api.py``（服务层）互补：这里走真实端点函数，
钉 HTTP 面语义——403/404 归属、409 的两种忙、400 的参数错、以及 202 落标记+投递。
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.api.jobs import retry_item_pages
from app.core.errors import AppError
from app.services import file_operations, task_queue
from app.services.job_models import JobItemStatus
from app.services.job_store import JobStore, JobType


def _make_job(store: JobStore):
    job_id = store.create_job(job_type=JobType.TEXT_BATCH, title="t", owner_id="admin")
    item_id = store.add_item(job_id, "file-1")
    store.update_item_status(item_id, JobItemStatus.PROCESSING)
    store.update_item_status(item_id, JobItemStatus.AWAITING_REVIEW)
    store.update_item_performance(
        item_id,
        {
            "recognition": {
                "page_count": 6,
                "failed_pages": {"4": "ocr boom"},
                "pages": {"4": {"status": "failed", "error": "ocr boom"}},
            }
        },
    )
    return job_id, item_id


@pytest.fixture()
def store(tmp_path):
    return JobStore(str(tmp_path / "jobs.sqlite3"))


@pytest.fixture()
def enqueued(monkeypatch: pytest.MonkeyPatch) -> list:
    calls: list = []

    class _Queue:
        def enqueue(self, task):
            calls.append(task)

    monkeypatch.setattr(task_queue, "get_task_queue", lambda: _Queue())
    return calls


def _patch_file(monkeypatch: pytest.MonkeyPatch, *, file_type: str = "pdf_scanned") -> None:
    monkeypatch.setattr(
        file_operations,
        "get_file_info",
        lambda file_id: {
            "file_id": file_id,
            "file_type": file_type,
            "page_count": 6,
            "original_filename": "卷.pdf",
        },
    )


def test_accepted_marks_and_enqueues(store, enqueued, monkeypatch):
    _patch_file(monkeypatch)
    job_id, item_id = _make_job(store)

    result = _run(retry_item_pages(job_id, item_id, [4], store=store, owner_id="admin"))

    assert result["queued_pages"] == [4]
    assert store.get_item(item_id)["status"] == JobItemStatus.PROCESSING.value
    perf = store.get_item_performance(item_id)
    assert perf["recognition"]["page_retry_pages"] == [4]
    assert len(enqueued) == 1
    assert enqueued[0].task_type == "page_retry"


def test_busy_returns_409_item_busy(store, enqueued, monkeypatch):
    _patch_file(monkeypatch)
    job_id, item_id = _make_job(store)
    store.update_item_performance(item_id, {"recognition": {"page_retry_pages": [4]}})
    store.update_item_status(item_id, JobItemStatus.PROCESSING)

    with pytest.raises(AppError) as info:
        _run(retry_item_pages(job_id, item_id, [4], store=store, owner_id="admin"))

    assert info.value.status_code == 409
    assert info.value.error_code == "item_busy"
    assert enqueued == []


def test_completed_item_returns_409_with_phase_two_hint(store, enqueued, monkeypatch):
    _patch_file(monkeypatch)
    job_id, item_id = _make_job(store)
    store.update_item_status(item_id, JobItemStatus.COMPLETED)

    with pytest.raises(AppError) as info:
        _run(retry_item_pages(job_id, item_id, [4], store=store, owner_id="admin"))

    assert info.value.status_code == 409
    assert info.value.error_code == "retry_requires_awaiting_review"


@pytest.mark.parametrize(
    ("pages", "expected"),
    [
        ([5], "page_not_failed"),
        ([], "pages_empty"),
        ([99], "pages_out_of_range"),
    ],
)
def test_bad_pages_return_400(store, enqueued, monkeypatch, pages, expected):
    _patch_file(monkeypatch)
    job_id, item_id = _make_job(store)

    with pytest.raises(AppError) as info:
        _run(retry_item_pages(job_id, item_id, pages, store=store, owner_id="admin"))

    assert info.value.status_code == 400
    assert info.value.error_code == expected
    assert enqueued == []


def test_non_visual_type_returns_400(store, enqueued, monkeypatch):
    _patch_file(monkeypatch, file_type="docx")
    job_id, item_id = _make_job(store)

    with pytest.raises(AppError) as info:
        _run(retry_item_pages(job_id, item_id, [4], store=store, owner_id="admin"))

    assert info.value.status_code == 400
    assert info.value.error_code == "partial_retry_unsupported_type"


def test_unknown_item_returns_404(store, enqueued, monkeypatch):
    _patch_file(monkeypatch)
    job_id, _item_id = _make_job(store)

    with pytest.raises(HTTPException) as info:
        _run(retry_item_pages(job_id, "nope", [4], store=store, owner_id="admin"))

    assert info.value.status_code == 404


def _run(coro):
    import asyncio

    return asyncio.run(coro)
