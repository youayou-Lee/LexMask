"""单文件供料任务持久索引（Issue #282）。

轻量 SQLite 表，只承担两件事：
1. 任务元数据落盘，重启后仍能列出、恢复待审阅任务；
2. mapping_ready 期间把可确认的 payload（segs/entities/mapping）落盘，
   completed 后立即清空，避免长期保存中间脱敏数据。

写盘失败只记日志，不打断主流程——内存任务照旧可用，列表缺行属于降级。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, datetime
from functools import lru_cache

from app.core.config import settings
from app.core.sqlite_base import connect_sqlite, ensure_db_dir
from app.services.agent_md_types import AgentMdTask, TaskState

logger = logging.getLogger(__name__)

INTERRUPTED_MESSAGE = "interrupted: 服务重启，任务已中断，请重新发起"
PAYLOAD_CORRUPT_MESSAGE = "payload_corrupt: 任务数据不可恢复，请重新发起"

_LIST_COLUMNS = (
    "task_id",
    "filename",
    "state",
    "stage",
    "message",
    "pages_done",
    "pages_total",
    "output_file_id",
    "created_at",
    "updated_at",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_md_tasks (
    task_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    filename TEXT NOT NULL,
    state TEXT NOT NULL,
    stage TEXT,
    message TEXT,
    pages_done INTEGER NOT NULL DEFAULT 0,
    pages_total INTEGER NOT NULL DEFAULT 0,
    file_path TEXT,
    output_file_id TEXT,
    entity_types_used TEXT,
    payload_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_md_tasks_owner_updated
    ON agent_md_tasks(owner_id, updated_at DESC);
"""


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _dump_entity_types(task: AgentMdTask) -> str:
    return json.dumps(list(task.entity_types_used or []), ensure_ascii=False)


class AgentMdTaskStore:
    """SQLite-backed index for single-file agent-MD tasks."""

    def __init__(self, db_path: str) -> None:
        self._path = db_path
        self._init_db()

    # --- connection / schema ------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self._path, timeout=10.0, busy_timeout_ms=5000, wal=True)

    def _init_db(self) -> None:
        ensure_db_dir(self._path)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            conn.execute("PRAGMA wal_autocheckpoint = 1000")

    # --- writes -------------------------------------------------------------
    def upsert_meta(self, task: AgentMdTask) -> None:
        """Insert/update run metadata. Never touches payload_json."""
        now = _utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO agent_md_tasks (
                    task_id, owner_id, filename, state, stage, message,
                    pages_done, pages_total, file_path, output_file_id,
                    entity_types_used, payload_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    state = excluded.state,
                    stage = excluded.stage,
                    message = excluded.message,
                    pages_done = excluded.pages_done,
                    pages_total = excluded.pages_total,
                    file_path = excluded.file_path,
                    output_file_id = excluded.output_file_id,
                    entity_types_used = excluded.entity_types_used,
                    updated_at = excluded.updated_at
                """,
                (
                    task.task_id,
                    task.owner_id or "",
                    task.filename,
                    task.state.value,
                    task.stage,
                    task.message,
                    int(task.pages_done or 0),
                    int(task.pages_total or 0),
                    task.file_path,
                    task.output_file_id,
                    _dump_entity_types(task),
                    now,
                    now,
                ),
            )

    def save_payload(self, task: AgentMdTask, payload: dict) -> None:
        """Persist mapping_ready payload so a restart can resume review."""
        now = _utc_now()
        raw = json.dumps(payload, ensure_ascii=False)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO agent_md_tasks (
                    task_id, owner_id, filename, state, stage, message,
                    pages_done, pages_total, file_path, output_file_id,
                    entity_types_used, payload_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    state = excluded.state,
                    stage = excluded.stage,
                    message = excluded.message,
                    pages_done = excluded.pages_done,
                    pages_total = excluded.pages_total,
                    file_path = excluded.file_path,
                    output_file_id = excluded.output_file_id,
                    entity_types_used = excluded.entity_types_used,
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                """,
                (
                    task.task_id,
                    task.owner_id or "",
                    task.filename,
                    task.state.value,
                    task.stage,
                    task.message,
                    int(task.pages_done or 0),
                    int(task.pages_total or 0),
                    task.file_path,
                    task.output_file_id,
                    _dump_entity_types(task),
                    raw,
                    now,
                    now,
                ),
            )

    def mark_completed(self, task: AgentMdTask) -> None:
        """Set completed and clear payload in one atomic UPDATE."""
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE agent_md_tasks
                   SET state = ?, output_file_id = ?, payload_json = NULL,
                       message = ?, updated_at = ?
                 WHERE task_id = ?
                """,
                (
                    TaskState.COMPLETED.value,
                    task.output_file_id,
                    task.message,
                    _utc_now(),
                    task.task_id,
                ),
            )

    def mark_interrupted(self) -> int:
        """Fail all tasks that were running when the process stopped."""
        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE agent_md_tasks
                   SET state = ?, message = ?, updated_at = ?
                 WHERE state IN (?, ?, ?)
                """,
                (
                    TaskState.FAILED.value,
                    INTERRUPTED_MESSAGE,
                    _utc_now(),
                    TaskState.UPLOADED.value,
                    TaskState.PARSING.value,
                    TaskState.NER_RUNNING.value,
                ),
            )
            return int(cur.rowcount or 0)

    def mark_payload_corrupt(self, task_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE agent_md_tasks
                   SET state = ?, message = ?, payload_json = NULL, updated_at = ?
                 WHERE task_id = ?
                """,
                (TaskState.FAILED.value, PAYLOAD_CORRUPT_MESSAGE, _utc_now(), task_id),
            )

    # --- reads --------------------------------------------------------------
    def load(self, task_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM agent_md_tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
        return dict(row) if row is not None else None

    def list_for_owner(self, owner_id: str, limit: int = 50) -> list[dict]:
        limit = max(1, min(int(limit or 50), 200))
        columns = ", ".join(_LIST_COLUMNS)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT {columns} FROM agent_md_tasks
                 WHERE owner_id = ? AND state != ?
                 ORDER BY updated_at DESC, task_id DESC
                 LIMIT ?
                """,
                (owner_id, TaskState.COMPLETED.value, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def counts_for_owner(self, owner_id: str) -> dict:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    SUM(CASE WHEN state IN (?, ?, ?) THEN 1 ELSE 0 END) AS active,
                    SUM(CASE WHEN state = ? THEN 1 ELSE 0 END) AS awaiting_review
                  FROM agent_md_tasks
                 WHERE owner_id = ?
                """,
                (
                    TaskState.UPLOADED.value,
                    TaskState.PARSING.value,
                    TaskState.NER_RUNNING.value,
                    TaskState.MAPPING_READY.value,
                    owner_id,
                ),
            ).fetchone()
        return {
            "active": int(row["active"] or 0),
            "awaiting_review": int(row["awaiting_review"] or 0),
        }


# --- Singleton accessor ------------------------------------------------------
@lru_cache
def _singleton_store() -> AgentMdTaskStore:
    return AgentMdTaskStore(settings.AGENT_MD_TASK_DB_PATH)


def get_agent_md_task_store() -> AgentMdTaskStore:
    """Return the application-wide agent-MD task store singleton."""
    return _singleton_store()
