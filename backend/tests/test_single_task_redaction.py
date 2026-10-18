"""Issue#194 Task 4: 单文件打码提交（确认后 seed + item 推进 + 入队打码）。

服务契约（submit_single_redaction）：
- awaiting_review 的 single item：前端确认后的实体 seed 进 file_store、
  item 推进离 awaiting_review、redaction TaskItem 入队、job config 合并更新；
- item 不在 awaiting_review → ValueError；
- 他 single 任务「计算中」→ SingleTaskConflictError，但**自身 job 不自拦**
  （守门查询排除 job_id 本身）；
- job/item 不存在或非本人 → KeyError。
端点 POST /jobs/{job_id}/items/{item_id}/redact 只做接线与错误映射
（ValueError→400 / SingleTaskConflictError→409 / KeyError→404）。
"""

import json

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.idempotency import _store as _idem_store
from app.main import app
from app.services import file_management_service as fms
from app.services import job_management_service as jms
from app.services import single_task_service as sts
from app.services.file_management_service import get_file_info
from app.services.job_models import JobItemStatus, JobStatus, JobType
from app.services.job_store import JobStore

client = TestClient(app)


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    """匿名主体 + 隔离 file_store（端点接线用例与服务用例共用）。"""
    up = tmp_path / "uploads"
    up.mkdir()
    monkeypatch.setattr(settings, "AUTH_ENABLED", False)  # require_auth → anonymous
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(up))
    fms.file_store.clear()
    _idem_store.clear()
    yield
    fms.file_store.clear()


@pytest.fixture()
def store(tmp_path):
    return JobStore(str(tmp_path / "jobs.sqlite3"))


def _enqueue_capture(monkeypatch):
    seen = []

    class FakeQueue:
        def enqueue(self, task):
            seen.append(task)

    monkeypatch.setattr(sts, "get_task_queue", lambda: FakeQueue())
    return seen


def _seed_file(file_id: str = "f1", owner_id: str = "u1") -> str:
    """往 file_store 注册一个已完成识别的文件（打码 seed 的落点）。"""
    fms.file_store[file_id] = {
        "file_id": file_id,
        "filename": "a.pdf",
        "file_type": "txt",
        "owner_id": owner_id,
    }
    return file_id


def _make_awaiting_single_job(store: JobStore, file_id: str = "f1", owner_id: str = "u1"):
    """造一个停在 awaiting_review 的单文件任务（真实枚举状态机）。

    item：PENDING → AWAITING_REVIEW 直达合法；job：QUEUED → AWAITING_REVIEW
    不合法，须经 PROCESSING 中转（与 Task 2 测试同一套写法）。
    """
    job_id = store.create_job(
        job_type=JobType.SINGLE,
        title="a.pdf",
        config={"entity_type_ids": ["t1"], "single_file_id": file_id},
        owner_id=owner_id,
    )
    item_id = store.add_item(job_id, file_id)
    store.update_item_status(item_id, JobItemStatus.AWAITING_REVIEW)
    store.update_job_status(job_id, JobStatus.PROCESSING)
    store.update_job_status(job_id, JobStatus.AWAITING_REVIEW)
    return job_id, item_id


# ---------------------------------------------------------------------------
# 服务层
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_redaction_seeds_store_advances_item_and_enqueues(store, monkeypatch):
    """(a) 提交后：file_store 被 seed、item 离开 awaiting_review、打码入队、config 更新。"""
    seen = _enqueue_capture(monkeypatch)
    file_id = _seed_file()
    job_id, item_id = _make_awaiting_single_job(store, file_id)
    entities = [{"entity_type": "NAME", "text": "陈文清", "start": 0, "end": 3}]
    boxes = [{"page": 1, "x0": 1.0, "y0": 2.0, "x1": 3.0, "y1": 4.0}]

    out = await sts.submit_single_redaction(
        store,
        job_id=job_id,
        item_id=item_id,
        entities=entities,
        bounding_boxes=boxes,
        redact_config={"replacement_mode": "mask"},
        owner_id="u1",
    )

    assert out == {"job_id": job_id, "item_id": item_id, "status": "queued"}
    # seed：确认后的实体落到 file_store（打码工人从这里读）
    info = await get_file_info(file_id)
    assert info["entities"] == entities
    assert info["bounding_boxes"] == boxes
    # item 推进离 awaiting_review（批量线 approve_item_review 的落点）
    item = store.get_item(item_id)
    assert item["status"] == JobItemStatus.REVIEW_APPROVED.value
    # redaction TaskItem 入队
    assert len(seen) == 1
    assert seen[0].task_type == "redaction"
    assert seen[0].job_id == job_id
    assert seen[0].item_id == item_id
    assert seen[0].file_id == file_id
    # job config 合并更新：新键写入，既有键不丢
    cfg = json.loads(store.get_job(job_id)["config_json"])
    assert cfg["replacement_mode"] == "mask"
    assert cfg["single_file_id"] == file_id
    assert cfg["entity_type_ids"] == ["t1"]
    # 打码生命周期进 ACTIVE 门：提交即升 PROCESSING（评审 Critical-1）
    assert store.get_job(job_id)["status"] == JobStatus.PROCESSING.value


@pytest.mark.asyncio
async def test_submit_redaction_rejects_item_not_awaiting_review(store, monkeypatch):
    """(b) item 不在 awaiting_review → ValueError，且不入队不留痕。"""
    seen = _enqueue_capture(monkeypatch)
    file_id = _seed_file()
    job_id, item_id = _make_awaiting_single_job(store, file_id)
    store.update_item_status(item_id, JobItemStatus.COMPLETED)  # AWAITING_REVIEW → COMPLETED 合法

    with pytest.raises(ValueError):
        await sts.submit_single_redaction(
            store,
            job_id=job_id,
            item_id=item_id,
            entities=[],
            bounding_boxes=[],
            redact_config={},
            owner_id="u1",
        )
    assert seen == []  # 拒绝不入队
    assert await get_file_info(file_id) is not None
    assert fms.file_store[file_id].get("entities") is None  # 拒绝不 seed


@pytest.mark.asyncio
async def test_submit_redaction_conflicts_with_other_active_single_task(store, monkeypatch):
    """(c1) 他 single 任务「计算中」→ SingleTaskConflictError，能报出在跑文件。"""
    seen = _enqueue_capture(monkeypatch)
    file_id = _seed_file()
    job_id, item_id = _make_awaiting_single_job(store, file_id)
    # 另一个 u1 的 single 任务正处「计算中」（QUEUED ∈ ACTIVE_SINGLE_STATUSES；
    # create_job 落 DRAFT，须 submit_job 入队才进入「计算中」——与识别提交同流程）
    running_job = store.create_job(
        job_type=JobType.SINGLE,
        title="running.pdf",
        config={"single_file_id": "f9"},
        owner_id="u1",
    )
    store.add_item(running_job, "f9")
    store.submit_job(running_job)

    with pytest.raises(sts.SingleTaskConflictError) as ei:
        await sts.submit_single_redaction(
            store,
            job_id=job_id,
            item_id=item_id,
            entities=[],
            bounding_boxes=[],
            redact_config={},
            owner_id="u1",
        )
    assert ei.value.running_file_id == "f9"
    assert ei.value.running_filename == "running.pdf"
    assert seen == []  # 冲突不入队
    assert store.get_item(item_id)["status"] == JobItemStatus.AWAITING_REVIEW.value  # 不推进


@pytest.mark.asyncio
async def test_submit_redaction_excludes_itself_from_gate(store, monkeypatch):
    """(c2) 守门排除自身 job：本任务自身处于「计算中」状态不构成自拦。"""
    _enqueue_capture(monkeypatch)
    file_id = _seed_file()
    job_id, item_id = _make_awaiting_single_job(store, file_id)
    # 自身落入 ACTIVE_SINGLE_STATUSES（processing），守门查询会查到它——必须排除
    store.update_job_status(job_id, JobStatus.PROCESSING)

    out = await sts.submit_single_redaction(
        store,
        job_id=job_id,
        item_id=item_id,
        entities=[],
        bounding_boxes=[],
        redact_config={"replacement_mode": "pseudonym"},
        owner_id="u1",
    )
    assert out["status"] == "queued"
    assert store.get_item(item_id)["status"] == JobItemStatus.REVIEW_APPROVED.value


@pytest.mark.asyncio
async def test_submit_redaction_blocks_new_recognition_until_finished(store, monkeypatch):
    """（评审 Critical-1）打码生命周期对 D2 门可见：提交打码后同账号再提交识别 → 409 语义冲突；
    打码收尾（worker 刷 COMPLETED）后守门自动重开。"""
    _enqueue_capture(monkeypatch)
    file_id = _seed_file()
    _seed_file("f2", "u1")  # 下一单识别的素材
    job_id, item_id = _make_awaiting_single_job(store, file_id)

    await sts.submit_single_redaction(
        store,
        job_id=job_id,
        item_id=item_id,
        entities=[],
        bounding_boxes=[],
        redact_config={"replacement_mode": "mask"},
        owner_id="u1",
    )

    # 打码在跑：同账号新识别提交被守门拦下，且能报出在跑文件（f1/a.pdf）
    with pytest.raises(sts.SingleTaskConflictError) as ei:
        await sts.submit_single_recognition(
            store, file_id="f2", entity_type_ids=[], owner_id="u1"
        )
    assert ei.value.running_file_id == "f1"
    assert ei.value.running_filename == "a.pdf"

    # 模拟打码工人收尾：item COMPLETED → worker finally 刷 job（PROCESSING→COMPLETED 合法）
    store.update_item_status(item_id, JobItemStatus.COMPLETED)
    jms.refresh_job_status(store, job_id)
    assert store.get_job(job_id)["status"] == JobStatus.COMPLETED.value
    # 守门重开：识别提交放行
    ref = await sts.submit_single_recognition(
        store, file_id="f2", entity_type_ids=[], owner_id="u1"
    )
    assert ref["job_id"] != job_id


@pytest.mark.asyncio
async def test_submit_redaction_missing_or_foreign_job_raises_key_error(store, monkeypatch):
    """job 不存在 / 非本人 / item 不属于该 job → KeyError（端点层映射 404）。"""
    _enqueue_capture(monkeypatch)
    file_id = _seed_file()
    job_id, item_id = _make_awaiting_single_job(store, file_id)
    kwargs = dict(entities=[], bounding_boxes=[], redact_config={}, owner_id="u1")
    foreign = {k: v for k, v in kwargs.items() if k != "owner_id"}
    with pytest.raises(KeyError):
        await sts.submit_single_redaction(store, job_id="ghost", item_id=item_id, **kwargs)
    with pytest.raises(KeyError):
        await sts.submit_single_redaction(store, job_id=job_id, item_id=item_id, owner_id="u2", **foreign)
    with pytest.raises(KeyError):
        await sts.submit_single_redaction(store, job_id=job_id, item_id="ghost-item", **kwargs)


# ---------------------------------------------------------------------------
# 端点接线（POST /api/v1/jobs/{job_id}/items/{item_id}/redact）
# ---------------------------------------------------------------------------


def test_redact_endpoint_wiring_and_error_mapping(monkeypatch):
    """端点只做接线：body 透传 + 200/409/404/400 错误映射。"""
    seen: dict = {}

    async def fake_submit(store, *, job_id, item_id, entities, bounding_boxes, redact_config, owner_id):
        seen.update(
            job_id=job_id,
            item_id=item_id,
            entities=list(entities),
            bounding_boxes=list(bounding_boxes),
            redact_config=dict(redact_config),
            owner_id=owner_id,
        )
        return {"job_id": job_id, "item_id": item_id, "status": "queued"}

    monkeypatch.setattr(sts, "submit_single_redaction", fake_submit)
    # 端点接线用例不落真实 job 行：归属 assert 打桩（服务层归属用例单独覆盖）
    monkeypatch.setattr(jms, "assert_job_owner", lambda row, owner: None)

    resp = client.post(
        "/api/v1/jobs/j1/items/i1/redact",
        json={
            "entities": [{"entity_type": "NAME", "text": "陈文清"}],
            "bounding_boxes": [],
            "config": {"replacement_mode": "mask"},
        },
    )
    assert resp.status_code == 200
    assert resp.json() == {"job_id": "j1", "item_id": "i1", "status": "queued"}
    assert seen["entities"] == [{"entity_type": "NAME", "text": "陈文清"}]
    assert seen["redact_config"] == {"replacement_mode": "mask"}
    assert seen["owner_id"] == "anonymous"

    async def boom(*a, **k):
        raise sts.SingleTaskConflictError("f9", "running.pdf")

    monkeypatch.setattr(sts, "submit_single_redaction", boom)
    resp = client.post("/api/v1/jobs/j1/items/i1/redact", json={})
    assert resp.status_code == 409
    assert "running.pdf" in resp.json()["message"]

    async def missing(*a, **k):
        raise KeyError("j1")

    monkeypatch.setattr(sts, "submit_single_redaction", missing)
    resp = client.post("/api/v1/jobs/j1/items/i1/redact", json={})
    assert resp.status_code == 404

    async def bad_state(*a, **k):
        raise ValueError("item not awaiting_review: completed")

    monkeypatch.setattr(sts, "submit_single_redaction", bad_state)
    resp = client.post("/api/v1/jobs/j1/items/i1/redact", json={})
    assert resp.status_code == 400


def test_redact_endpoint_missing_job_404_without_stubs(store):
    """（评审 Important-2）缺失 job 不打桩走真实 assert_job_owner 路径 → 404（非 400）。

    NotFoundError 是 ValueError 子类，except 链必须先接 NotFoundError→404。
    """
    resp = client.post("/api/v1/jobs/ghost-job/items/i1/redact", json={})
    assert resp.status_code == 404
