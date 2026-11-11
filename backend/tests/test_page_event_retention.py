"""A5：删任务即清目录 + TTL 孤儿清扫（mtime 改老可操作）。"""
import os
import time

import pytest

from app.core.config import settings
from app.services import page_event_log as pel
from app.services import retention_service


@pytest.fixture()
def log_env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "PAGE_EVENT_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr(settings, "PAGE_EVENT_LOG_ENABLED", True)
    pel.reset_for_tests()
    yield tmp_path / "logs"
    pel.reset_for_tests()


def test_delete_job_logs_removes_directory(log_env):
    with pel.event_context("jobdel", "itemdel"):
        pel.log_event("page_done", page=1, ms=1)
    assert (log_env / "jobdel").exists()
    pel.delete_job_logs("jobdel")
    assert not (log_env / "jobdel").exists()


def test_logs_sweep_removes_stale_only(log_env, monkeypatch):
    for job in ("fresh", "stale"):
        with pel.event_context(job, "item"):
            pel.log_event("page_done", page=1, ms=1)
    stale_dir = log_env / "stale"
    old = time.time() - (settings.PAGE_EVENT_LOG_TTL_DAYS + 1) * 86400
    os.utime(stale_dir, (old, old))
    monkeypatch.setattr(retention_service, "_logs_root", lambda: log_env)
    removed = retention_service.logs_sweep()
    assert removed == 1
    assert (log_env / "fresh").exists() and not stale_dir.exists()


def test_logs_sweep_ttl_zero_disables(log_env, monkeypatch):
    with pel.event_context("jobx", "itemx"):
        pel.log_event("page_done", page=1, ms=1)
    os.utime(log_env / "jobx", (0, 0))
    monkeypatch.setattr(settings, "PAGE_EVENT_LOG_TTL_DAYS", 0)
    monkeypatch.setattr(retention_service, "_logs_root", lambda: log_env)
    assert retention_service.logs_sweep() == 0
    assert (log_env / "jobx").exists()
