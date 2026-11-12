"""Data retention sweep (Phase 1c data governance).

Enterprise compliance: uploads and their outputs are deleted after
``DATA_RETENTION_DAYS`` days (0 = disabled, the default — nothing changes for
existing deployments unless an operator opts in). Deletion goes through the
same ``delete_file`` used by the API so uploads, outputs, store entries and
job links are removed together, and every removal leaves an audit entry.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.core.audit import audit_log
from app.core.config import settings

logger = logging.getLogger(__name__)

_SWEEP_INTERVAL_SECONDS = 6 * 3600
_FIRST_SWEEP_DELAY_SECONDS = 120


def _parse_created_at(raw: object) -> datetime | None:
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=UTC)
    if isinstance(raw, str) and raw:
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            return None
    return None


async def retention_sweep() -> int:
    """Delete files older than the retention window. Returns deleted count."""
    days = int(settings.DATA_RETENTION_DAYS or 0)
    if days <= 0:
        return 0
    from app.services.file_management_service import delete_file, file_store

    cutoff = datetime.now(UTC) - timedelta(days=days)
    expired: list[str] = []
    for file_id, info in file_store.items():
        if not isinstance(info, dict):
            continue
        created = _parse_created_at(info.get("created_at"))
        if created is not None and created < cutoff:
            expired.append(str(file_id))

    deleted = 0
    for file_id in expired:
        try:
            await delete_file(file_id)
            audit_log(
                "retention_delete",
                "file",
                file_id,
                user="system",
                detail={"retention_days": days},
            )
            deleted += 1
        except Exception as exc:  # keep sweeping; one stuck file must not stop the rest
            logger.warning("retention sweep: unable to delete %s: %s", file_id, exc)
    if deleted:
        logger.info("retention sweep: deleted %d file(s) older than %d days", deleted, days)
    return deleted


async def trash_sweep() -> int:
    """清空超期回收站（R1-4）：deleted_at 超过 TRASH_RETENTION_DAYS 的真删。

    与 DATA_RETENTION_DAYS 独立——回收站清扫始终开启（软删不清扫=磁盘只增不减）。
    """
    days = int(settings.TRASH_RETENTION_DAYS or 7)
    from app.services.file_management_service import delete_file, file_store

    cutoff = datetime.now(UTC) - timedelta(days=days)
    expired: list[str] = []
    for file_id, info in file_store.items():
        if not isinstance(info, dict) or not info.get("deleted_at"):
            continue
        deleted_at = _parse_created_at(info.get("deleted_at"))
        if deleted_at is not None and deleted_at < cutoff:
            expired.append(str(file_id))

    purged = 0
    for file_id in expired:
        try:
            await delete_file(file_id)
            audit_log(
                "trash_purge",
                "file",
                file_id,
                user="system",
                detail={"trash_retention_days": days},
            )
            purged += 1
        except Exception as exc:
            logger.warning("trash sweep: unable to purge %s: %s", file_id, exc)

    # 结构化数据集回收站同窗清扫（F1-1）
    try:
        from app.services.structured_store import get_structured_store

        ds_purged = get_structured_store().purge_expired_trashed_datasets(
            older_than_iso=cutoff.isoformat()
        )
        if ds_purged:
            audit_log(
                "trash_purge",
                "structured_dataset",
                f"{ds_purged} datasets",
                user="system",
                detail={"trash_retention_days": days},
            )
            purged += ds_purged
    except Exception:
        logger.exception("trash sweep: dataset purge failed")

    if purged:
        logger.info("trash sweep: purged %d item(s) past %d-day window", purged, days)
    return purged


def _logs_root() -> Path:
    """日志根目录（测试注入点；与 page_event_log._log_root 同源推导）。"""
    from app.core.config import settings
    configured = str(getattr(settings, "PAGE_EVENT_LOG_DIR", "") or "").strip()
    return Path(configured) if configured else Path(str(settings.DATA_DIR)) / "logs"


def logs_sweep() -> int:
    """孤儿任务日志清扫（A5.2）：mtime 超 TTL 的 job 目录整删；TTL=0 关。

    终审 I-1：rmtree 前先关 writer 在**本进程**缓存的该 job fd——否则 worker
    里指向已 unlink inode 的陈旧 fd 会让后续 log_event 照常「成功」计数却写进
    虚空（目录永不重建）。child.name 已是消毒后的目录名，writer 的 close_job
    同消毒口径，恰好命中。
    """
    import shutil

    from app.core.config import settings
    from app.services.page_event_log import close_job
    ttl_days = int(getattr(settings, "PAGE_EVENT_LOG_TTL_DAYS", 14) or 0)
    if ttl_days <= 0:
        return 0
    root = _logs_root()
    if not root.is_dir():
        return 0
    cutoff = time.time() - ttl_days * 86400
    removed = 0
    for child in root.iterdir():
        try:
            if child.is_dir() and child.stat().st_mtime < cutoff:
                close_job(child.name)
                shutil.rmtree(child)
                removed += 1
        except OSError:
            logger.debug("logs sweep skip %s", child, exc_info=True)
    return removed


async def retention_loop() -> None:
    await asyncio.sleep(_FIRST_SWEEP_DELAY_SECONDS)
    while True:
        try:
            await retention_sweep()
        except Exception:
            logger.exception("retention sweep failed")
        try:
            await trash_sweep()
        except Exception:
            logger.exception("trash sweep failed")
        try:
            logs_sweep()
        except Exception:
            logger.exception("page event logs sweep failed")
        await asyncio.sleep(_SWEEP_INTERVAL_SECONDS)
