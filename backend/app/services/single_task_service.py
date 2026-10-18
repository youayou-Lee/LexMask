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
from app.services.job_management_service import refresh_job_status, seed_file_store_from_draft_payload
from app.services.job_models import JobItemStatus, JobStatus, JobType
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


async def submit_single_redaction(
    store: JobStore,
    *,
    job_id: str,
    item_id: str,
    entities: list[dict],
    bounding_boxes: list[dict],
    redact_config: dict[str, Any],
    owner_id: str,
) -> dict[str, str]:
    """提交单文件打码：确认后的实体 seed 进 file_store，item 推进入队打码。

    镜像批量线 approve_review 的「迁移 + 刷新」写法（store.approve_item_review
    → touch → refresh_job_status），入队打码复用批量线 redaction 工人。
    守门（D2）与识别提交同源（``_active_single_task``）但**排除自身 job_id**：
    本任务自身处于「计算中」状态不构成自拦。

    评审 Critical-1：提交即把 job 升 PROCESSING（awaiting_review→PROCESSING
    合法迁移）——打码工人不像识别工人那样升 job 状态，不升则整个打码生命周期
    job 停在 awaiting_review、D2 门对「打码在跑」失明；其后 refresh_job_status
    无 review_approved 分支不会回退，收尾由 worker 既有 finally 刷成
    COMPLETED/FAILED、守门自动重开。

    job/item 不存在或非本人抛 KeyError；item 不在 awaiting_review 抛
    ValueError；他 single 任务「计算中」抛 :class:`SingleTaskConflictError`。
    pseudonym 在单文件允许（批量守卫 ``_reject_batch_pseudonym`` 不适用）。
    """
    job = store.get_job(job_id)
    if not job or str(job.get("owner_id") or "local_user") != owner_id:
        raise KeyError(job_id)
    items = [it for it in store.list_items(job_id) if it["id"] == item_id]
    if not items:
        raise KeyError(item_id)
    if items[0]["status"] != JobItemStatus.AWAITING_REVIEW.value:
        raise ValueError(f"item not awaiting_review: {items[0]['status']}")
    async with _submit_lock:
        running = _active_single_task(store, owner_id)
        if running and running["id"] != job_id:
            cfg = _config_dict(running)
            raise SingleTaskConflictError(
                str(cfg.get("single_file_id") or ""),
                str(running.get("title") or cfg.get("single_file_id") or running.get("id") or ""),
            )
        file_id = str(items[0]["file_id"])
        # 前端确认后的实体 seed 进 file_store（扁平 list，打码工人自适配 per-page dict）
        await seed_file_store_from_draft_payload(file_id, {"entities": entities, "bounding_boxes": bounding_boxes})
        # 打码工人读 job config 的键（replacement_mode/custom_replacements/...）：
        # body 的 config merge 进 config_json，既有键不丢
        store.merge_job_config(job_id, redact_config)
        # ↓ 镜像 approve_review：item 迁移 REVIEW_APPROVED（含清输出）+ 刷新 job 状态
        store.approve_item_review(item_id, reviewer=owner_id)
        # 评审 Critical-1：打码工人不升 job 状态，提交即升 PROCESSING 让 D2 门看见打码在跑
        store.update_job_status(job_id, JobStatus.PROCESSING)
        store.touch_job_updated(job_id)
        refresh_job_status(store, job_id)
    logger.info("single redaction submitted: job=%s item=%s owner=%s", job_id[:8], item_id, owner_id)
    # 入队在锁外，与 submit_single_recognition 一致（锁只护「查守门 → 落库」）
    get_task_queue().enqueue(TaskItem(job_id=job_id, item_id=item_id, file_id=file_id, task_type="redaction"))
    return {"job_id": job_id, "item_id": item_id, "status": "queued"}
