"""Issue#194 单文件任务：识别提交守门与编排。

单文件任务 = JobType.SINGLE 的 job（一条 item），识别/打码复用现有
recognition/redaction 管道；本模块只负责「能否提交」与「提交编排」。

守门规则（D2）：同账号同时至多一个「计算中」的单文件任务
（ACTIVE_SINGLE_STATUSES）；停在 awaiting_review（待用户确认）不挡新提交；
不同账号互不影响。
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from app.services.file_management_service import file_owner_id, get_file_info
from app.services.job_models import JobType
from app.services.job_store import JobStore
from app.services.task_queue import TaskItem, get_task_queue

logger = logging.getLogger(__name__)

# 「计算中」= 挡新提交的状态；awaiting_review 待用户确认，不挡（D2）。
# running/redacting 为旧数据兼容状态，读取面一并计入。
ACTIVE_SINGLE_STATUSES: tuple[str, ...] = ("queued", "processing", "running", "redacting")

# 后端为单进程 FastAPI + asyncio（SimpleTaskQueue 同进程）：
# 模块级锁保证「查守门 → 建任务 → 提交」对并发提交原子，恰好一胜。
_submit_lock = asyncio.Lock()


class SingleTaskConflictError(Exception):
    """同账号已有计算中的单文件任务（D2）。

    running_file_id / running_filename 供端点层拼 409 文案（报出在跑的文件名）。
    """

    def __init__(self, running_file_id: str, running_filename: str):
        self.running_file_id = running_file_id
        self.running_filename = running_filename
        super().__init__(f"single task active: {running_filename or running_file_id}")


def _config_dict(job_row: dict[str, Any]) -> dict[str, Any]:
    """job 行的 config_json → dict（损坏/缺失按空处理）。"""
    raw = job_row.get("config_json")
    if not raw:
        return {}
    try:
        cfg = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        return {}
    return cfg if isinstance(cfg, dict) else {}


def _active_single_task(store: JobStore, owner_id: str) -> dict[str, Any] | None:
    """该账号最近一个「计算中」的单文件任务行；无则 None。

    返回行不带 file_id——在跑文件的身份取行内 config_json 的
    ``single_file_id``（提交时写入）与 title。
    """
    jobs, _ = store.list_jobs(
        job_type=JobType.SINGLE,
        status_values=list(ACTIVE_SINGLE_STATUSES),
        owner_id=owner_id,
        page=1,
        page_size=1,
    )
    return jobs[0] if jobs else None


async def submit_single_recognition(
    store: JobStore,
    *,
    file_id: str,
    entity_type_ids: list[str],
    owner_id: str,
    title: str = "",
) -> dict[str, str]:
    """提交单文件识别：守门（D2 唯一性 + 归属）→ 建 SINGLE job → 入识别队列。

    冲突抛 :class:`SingleTaskConflictError`（带在跑文件的 file_id/文件名）；
    文件不存在抛 KeyError；非本人文件抛 PermissionError；
    PDF 加密态由调用方（端点层预探测）与识别管道处理，本层不吞
    :class:`~app.services.file_parser.PdfEncryptedError`。
    """
    info = await get_file_info(file_id)
    if not info:
        raise KeyError(file_id)
    # 归属口径与既有 assert_file_owner 一致（无 owner 的旧文件视作 local_user），
    # 但按本服务契约区分「不存在 → KeyError」与「非本人 → PermissionError」。
    if file_owner_id(info) != owner_id:
        raise PermissionError(file_id)
    async with _submit_lock:
        running = _active_single_task(store, owner_id)
        if running:
            cfg = _config_dict(running)
            raise SingleTaskConflictError(
                str(cfg.get("single_file_id") or ""),
                str(running.get("title") or cfg.get("single_file_id") or running.get("id") or ""),
            )
        job_id = store.create_job(
            job_type=JobType.SINGLE,
            title=title or str(info.get("filename") or "") or file_id,
            # single_file_id：list_jobs 行无 file_id，冲突时靠它报「在跑的文件」
            config={"entity_type_ids": list(entity_type_ids), "single_file_id": file_id},
            owner_id=owner_id,
        )
        item_id = store.add_item(job_id, file_id)
        store.submit_job(job_id)
    logger.info("single recognition submitted: job=%s file=%s owner=%s", job_id[:8], file_id, owner_id)
    get_task_queue().enqueue(
        TaskItem(job_id=job_id, item_id=item_id, file_id=file_id, task_type="recognition")
    )
    return {"job_id": job_id, "item_id": item_id, "file_id": file_id}
