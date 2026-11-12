"""A5：删任务即清目录 + TTL 孤儿清扫（mtime 改老可操作）。"""
import json
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


def test_sweep_then_write_recreates_dir_no_void_write(log_env, monkeypatch):
    """终审 I-1：TTL 清扫必须先关 writer 缓存的 fd——否则同 job 再写事件时
    _open_fd 命中指向已 unlink inode 的陈旧 fd：written 照涨、返回 True，
    磁盘上目录却永不重建（写进虚空 + fd 只增不减）。"""
    with pel.event_context("jobA", "itemA"):
        assert pel.log_event("page_done", page=1, ms=1) is True
    written_before = pel.stats()["written"]
    stale = time.time() - (settings.PAGE_EVENT_LOG_TTL_DAYS + 1) * 86400
    os.utime(log_env / "jobA", (stale, stale))
    monkeypatch.setattr(retention_service, "_logs_root", lambda: log_env)
    assert retention_service.logs_sweep() == 1
    assert not (log_env / "jobA").exists()
    # 清扫后同 job 再写：fd 缓存已被清扫关掉 ⇒ 目录/文件重建、事件落盘可读
    with pel.event_context("jobA", "itemA"):
        assert pel.log_event("page_done", page=2, ms=2) is True
    assert pel.stats()["written"] == written_before + 1
    path = log_env / "jobA" / "itemA.jsonl"
    assert path.exists()
    lines = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [e["page"] for e in lines] == [2]
