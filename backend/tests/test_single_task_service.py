"""Issue#194: 单文件识别提交守门（同账号恰好一个「计算中」的单文件任务）。

守门规则（D2）：ACTIVE_SINGLE_STATUSES 内的状态挡新提交；
停在 awaiting_review（待用户确认）不挡；不同账号互不影响。
"""

import asyncio
import json

import pytest

from app.services import single_task_service as sts
from app.services.job_models import JobItemStatus, JobStatus, JobType
from app.services.job_store import JobStore


@pytest.fixture()
def store(tmp_path):
    return JobStore(str(tmp_path / "jobs.sqlite3"))


def _fake_file_info(monkeypatch, files):
    """替换服务模块内的 get_file_info（真实实现是 async，查 file_store 单例）。"""

    async def fake_get_file_info(fid):
        return files.get(fid)

    monkeypatch.setattr(sts, "get_file_info", fake_get_file_info)


@pytest.fixture()
def file_id(monkeypatch):
    """造一个属于 u1 的文件 f1（附带 u2 的 f2），并让 get_file_info 返回它们。"""
    files = {
        "f1": {"file_id": "f1", "filename": "a.pdf", "file_type": "txt", "owner_id": "u1"},
        "f2": {"file_id": "f2", "filename": "b.pdf", "file_type": "txt", "owner_id": "u2"},
    }
    _fake_file_info(monkeypatch, files)
    return "f1"


def _enqueue_capture(monkeypatch):
    seen = []

    class FakeQueue:
        def enqueue(self, task):
            seen.append(task)

    monkeypatch.setattr(sts, "get_task_queue", lambda: FakeQueue())
    return seen


def _settle_at_awaiting_review(store, job_id, item_id):
    """把刚提交的任务推到 awaiting_review（真实枚举状态机）。

    item：PENDING → AWAITING_REVIEW 直达合法；job：QUEUED → AWAITING_REVIEW
    不合法（job_store.py:64），须经 PROCESSING 中转。
    """
    store.update_item_status(item_id, JobItemStatus.AWAITING_REVIEW)
    store.update_job_status(job_id, JobStatus.PROCESSING)
    store.update_job_status(job_id, JobStatus.AWAITING_REVIEW)


@pytest.mark.asyncio
async def test_submit_creates_single_job_and_enqueues_recognition(store, file_id, monkeypatch):
    seen = _enqueue_capture(monkeypatch)
    ref = await sts.submit_single_recognition(
        store, file_id=file_id, entity_type_ids=["t1"], owner_id="u1"
    )
    job = store.get_job(ref["job_id"])
    assert job["job_type"] == "single"
    assert job["owner_id"] == "u1"
    assert job["status"] == "queued"
    items = store.list_items(ref["job_id"])
    assert len(items) == 1
    assert items[0]["file_id"] == file_id
    assert len(seen) == 1
    assert seen[0].task_type == "recognition"
    assert seen[0].job_id == ref["job_id"]
    assert seen[0].item_id == ref["item_id"]
    assert seen[0].file_id == file_id
    cfg = json.loads(job["config_json"])
    assert cfg["entity_type_ids"] == ["t1"]
    # 冲突提示要能报出「在跑的文件名」：首个 file_id 落 config（守门查询行无 file_id）
    assert cfg["single_file_id"] == file_id


@pytest.mark.asyncio
async def test_missing_file_raises_key_error(store, monkeypatch):
    _enqueue_capture(monkeypatch)
    _fake_file_info(monkeypatch, {})
    with pytest.raises(KeyError):
        await sts.submit_single_recognition(
            store, file_id="ghost", entity_type_ids=[], owner_id="u1"
        )


@pytest.mark.asyncio
async def test_foreign_file_rejected_without_side_effects(store, file_id, monkeypatch):
    seen = _enqueue_capture(monkeypatch)
    with pytest.raises(PermissionError):
        await sts.submit_single_recognition(
            store, file_id="f1", entity_type_ids=[], owner_id="u2"
        )
    # 拒绝不留痕：不建 job、不入队
    _, total = store.list_jobs()
    assert total == 0
    assert seen == []


@pytest.mark.asyncio
async def test_second_submit_conflicts_while_computing_but_not_awaiting_review(
    store, file_id, monkeypatch
):
    _enqueue_capture(monkeypatch)
    ref = await sts.submit_single_recognition(
        store, file_id=file_id, entity_type_ids=[], owner_id="u1"
    )
    with pytest.raises(sts.SingleTaskConflictError) as ei:
        await sts.submit_single_recognition(store, file_id="f1", entity_type_ids=[], owner_id="u1")
    # 冲突异常能报出在跑任务的 file_id 与文件名（端点层 409 detail 依赖）
    assert ei.value.running_file_id == "f1"
    assert ei.value.running_filename == "a.pdf"
    # 识别完成停在 awaiting_review：不挡新提交（D2）
    _settle_at_awaiting_review(store, ref["job_id"], ref["item_id"])
    ref2 = await sts.submit_single_recognition(store, file_id="f1", entity_type_ids=[], owner_id="u1")
    assert ref2["job_id"] != ref["job_id"]


@pytest.mark.asyncio
async def test_other_owner_not_blocked_by_someone_elses_task(store, file_id, monkeypatch):
    _enqueue_capture(monkeypatch)
    await sts.submit_single_recognition(store, file_id="f1", entity_type_ids=[], owner_id="u1")
    # u1 有计算中的任务，不挡 u2 提交自己的文件
    ref2 = await sts.submit_single_recognition(store, file_id="f2", entity_type_ids=[], owner_id="u2")
    job2 = store.get_job(ref2["job_id"])
    assert job2["owner_id"] == "u2"
    assert job2["status"] == "queued"


@pytest.mark.asyncio
async def test_concurrent_submissions_exactly_one_wins(store, file_id, monkeypatch):
    _enqueue_capture(monkeypatch)
    outs = await asyncio.gather(
        sts.submit_single_recognition(store, file_id="f1", entity_type_ids=[], owner_id="u1"),
        sts.submit_single_recognition(store, file_id="f1", entity_type_ids=[], owner_id="u1"),
        return_exceptions=True,
    )
    ok = [o for o in outs if isinstance(o, dict)]
    conflict = [o for o in outs if isinstance(o, sts.SingleTaskConflictError)]
    assert len(ok) == 1
    assert len(conflict) == 1
    # 落库同样只有一条 single 任务
    _, total = store.list_jobs(job_type=JobType.SINGLE, owner_id="u1")
    assert total == 1
