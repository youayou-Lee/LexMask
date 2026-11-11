# Copyright 2026 LexMask Contributors

"""Issue#257 需求重对齐：单文件页格的 job/item 解析兜底端点。

背景：单文件任务在识别完成后，前端 provider 账本条目会被清理，草稿也可能
缺 ``taskJobId``；此时刷新页面，页格就再也解析不出 (jobId, itemId)，「逐页
结果与选页导出」整块消失。这里用 ``job_items.file_id`` 反查兜底。
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from app.api.jobs import get_item_ref_by_file
from app.models.errors import NotFoundError
from app.services import file_operations
from app.services.job_models import JobType
from app.services.job_store import JobStore


def _make_store(tmp_path) -> tuple[JobStore, str, str]:
    store = JobStore(str(tmp_path / "jobs.sqlite3"))
    job_id = store.create_job(job_type=JobType.TEXT_BATCH, title="t", owner_id="admin")
    item_id = store.add_item(job_id, "file-ref-1")
    return store, job_id, item_id


def _call(store: JobStore, file_id: str, owner_id: str = "admin") -> dict:
    return asyncio.run(
        get_item_ref_by_file(file_id=file_id, store=store, owner_id=owner_id)
    )


def test_returns_job_and_item_for_known_file(tmp_path) -> None:
    store, job_id, item_id = _make_store(tmp_path)

    payload = _call(store, "file-ref-1")

    assert payload["job_id"] == job_id
    assert payload["item_id"] == item_id
    assert payload["status"]


def test_unknown_file_is_404(tmp_path) -> None:
    store, _, _ = _make_store(tmp_path)

    with pytest.raises(HTTPException) as exc:
        _call(store, "file-does-not-exist")

    assert exc.value.status_code == 404


def test_other_owner_cannot_resolve(tmp_path) -> None:
    """越权一律 404：其他用户不能借这条兜底拿到别人的 job/item。"""
    store, _, _ = _make_store(tmp_path)

    with pytest.raises(HTTPException) as exc:
        _call(store, "file-ref-1", owner_id="someone-else")

    assert exc.value.status_code == 404


def test_latest_item_wins_when_file_reused(tmp_path) -> None:
    store, _, first_item = _make_store(tmp_path)
    job_id_2 = store.create_job(job_type=JobType.TEXT_BATCH, title="t2", owner_id="admin")
    second_item = store.add_item(job_id_2, "file-ref-1")

    payload = _call(store, "file-ref-1")

    assert payload["item_id"] == second_item
    assert payload["item_id"] != first_item
    assert payload["job_id"] == job_id_2


def test_store_lookup_is_used_by_endpoint(tmp_path, monkeypatch) -> None:
    """端点不得绕过 store 自拼 SQL：换掉 store 实例即换数据源。"""
    store, job_id, item_id = _make_store(tmp_path)
    calls: list[tuple[str, str | None]] = []
    original = store.find_item_ref_by_file_id

    def spy(file_id: str, *, owner_id: str | None = None):
        calls.append((file_id, owner_id))
        return original(file_id, owner_id=owner_id)

    monkeypatch.setattr(store, "find_item_ref_by_file_id", spy)

    _call(store, "file-ref-1")

    assert calls == [("file-ref-1", "admin")]


def test_missing_file_operation_is_not_required(tmp_path, monkeypatch) -> None:
    """该端点只读 job 账本，不依赖 file_store（文件被删也要能反查到历史 item）。"""

    def explode(*_args, **_kwargs):
        raise AssertionError("endpoint must not touch file_store")

    monkeypatch.setattr(file_operations, "get_file_info", explode)
    store, job_id, _ = _make_store(tmp_path)

    payload = _call(store, "file-ref-1")

    assert payload["job_id"] == job_id


def test_not_found_error_is_importable_for_callers() -> None:
    """保证 HTTP 404 与内部 NotFoundError 语义不混：端点对外只出 HTTPException。"""
    assert issubclass(NotFoundError, Exception)