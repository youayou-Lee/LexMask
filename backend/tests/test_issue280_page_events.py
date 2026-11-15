# Copyright 2026 LexMask Contributors

"""Issue#280 · E2：``manual_verified`` / ``manual_revoked`` 事件 schema 白名单。

口径（设计稿 §2.6 唯一出处）：白名单制下未声明字段=拒写**整事件**+计数；
``operator`` 有意不入事件日志（人名走审计面 ``audit_log.user`` 与 ledger）。
"""

from __future__ import annotations

import json

import pytest

from app.core.config import settings
from app.services import page_event_log as pel


@pytest.fixture()
def log_env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "PAGE_EVENT_LOG_ENABLED", True)
    monkeypatch.setattr(settings, "PAGE_EVENT_LOG_DIR", str(tmp_path / "logs"))
    pel.reset_for_tests()
    yield tmp_path / "logs"
    pel.reset_for_tests()


def _read_lines(log_dir, job_id, item_id):
    path = log_dir / job_id / f"{item_id}.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_manual_events_schema_whitelisted():
    """两事件必须先入白名单（schema 未声明 ⇒ 整事件拒写，所以顺序不可倒）。"""
    schema = pel.EVENT_SCHEMAS["manual_verified"]
    assert schema.required == ("box_count",) and schema.optional == ()
    schema_revoked = pel.EVENT_SCHEMAS["manual_revoked"]
    assert schema_revoked.required == () and schema_revoked.optional == ("box_count",)


def test_manual_events_pass_whitelist(log_env):
    with pel.event_context("job1", "item1"):
        assert pel.log_event("manual_verified", page=5, box_count=3) is True
        assert pel.log_event("manual_revoked", page=5, box_count=3) is True
    lines = _read_lines(log_env, "job1", "item1")
    assert [entry["event"] for entry in lines] == ["manual_verified", "manual_revoked"]
    assert lines[0]["box_count"] == 3 and lines[0]["page"] == 5


def test_manual_event_with_undeclared_field_rejected_whole(log_env):
    """带未声明字段（operator）⇒ 整事件拒写 + dropped_unknown_field 计数。

    operator 是人名，走审计面 audit_log.user 与 ledger；本日志是诊断面，
    白名单不声明该字段，误传即整事件拒写（设计稿 §2.6 唯一口径）。
    """
    with pel.event_context("job2", "item2"):
        assert pel.log_event("manual_verified", page=5, box_count=3, operator="apitester") is False
    assert _read_lines(log_env, "job2", "item2") == []
    s = pel.stats()
    assert s["dropped_unknown_field"] == 1
