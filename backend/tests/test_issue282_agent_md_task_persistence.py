"""Issue#282：单文件供料任务持久化 / 重启恢复 / 并发确认 / 索引 API。"""
import asyncio
import json
import sqlite3

import fitz
import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import app
from app.services import agent_md_pipeline_service as pipeline_mod
from app.services.agent_md_pipeline_service import AgentMdPipelineService
from app.services.agent_md_task_store import (
    INTERRUPTED_MESSAGE,
    PAYLOAD_CORRUPT_MESSAGE,
    AgentMdTaskStore,
)
from app.services.agent_md_types import AgentMdTask, MappingItem, Seg, TaskState
from app.services.mineru_parse_client import MineruResult

client = TestClient(app)


def _v2_nested_one_para():
    return [
        [
            {
                "type": "paragraph",
                "content": {
                    "paragraph_content": [{"type": "text", "content": "张三借李四人民币一万元"}]
                },
                "bbox": [112, 75, 566, 94],
            }
        ]
    ]


class _FakeMineru:
    async def submit(self, file_bytes, filename):
        return "job-1"

    async def wait_result(self, task_id, on_progress=None):
        if on_progress:
            on_progress(1, 1)
        return MineruResult(md="张三借李四人民币一万元", content_list_v2=_v2_nested_one_para(), images={})


class _FakeOcr:
    def extract_text_boxes(self, image_bytes):
        return []


class _FakeNer:
    async def extract(self, text, entity_types):
        from app.models.entity_schemas import Entity

        ents = []
        for name in ("张三", "李四"):
            idx = text.find(name)
            if idx >= 0:
                ents.append(Entity(id=name, text=name, type="PERSON", start=idx, end=idx + 2))
        return ents


@pytest.fixture
def sample_pdf(tmp_path):
    p = tmp_path / "a.pdf"
    doc = fitz.open()
    doc.new_page()
    doc.save(str(p))
    doc.close()
    return str(p)


def _service(tmp_path, store):
    return AgentMdPipelineService(
        mineru_client=_FakeMineru(),
        ocr=_FakeOcr(),
        ner=_FakeNer(),
        entity_types=[],
        output_dir=str(tmp_path),
        task_store=store,
    )


def _db_path(tmp_path):
    return str(tmp_path / "agent_md_tasks.sqlite3")


# --- store ---------------------------------------------------------------

def test_store_payload_roundtrip_and_counts(tmp_path):
    store = AgentMdTaskStore(_db_path(tmp_path))
    task = AgentMdTask(
        task_id="t-1", state=TaskState.MAPPING_READY, filename="a.pdf", owner_id="u1",
        segs=[Seg(text="张三", page_idx=0, source="text")],
        mapping=[MappingItem(id="e1", original_text="张三", entity_type="PERSON", replacement="[人名_1]")],
        structured_md="# 张三", warnings=["w"], dropped_blocks={"empty_table": 0}, chars=2,
        entity_types_used=["PERSON"],
    )
    store.save_payload(task, {"schema_version": 1, "segs": [], "chars": 2})

    row = store.load("t-1")
    assert row["owner_id"] == "u1"
    assert json.loads(row["payload_json"])["chars"] == 2
    assert store.counts_for_owner("u1") == {"active": 0, "awaiting_review": 1}
    assert store.counts_for_owner("u2") == {"active": 0, "awaiting_review": 0}
    assert store.list_for_owner("u2") == []
    assert store.list_for_owner("u1")[0]["task_id"] == "t-1"


def test_store_list_excludes_completed_and_orders_by_updated(tmp_path):
    store = AgentMdTaskStore(_db_path(tmp_path))
    for tid, state in (("a", TaskState.PARSING), ("b", TaskState.MAPPING_READY), ("c", TaskState.COMPLETED)):
        store.upsert_meta(AgentMdTask(task_id=tid, state=state, filename=f"{tid}.pdf", owner_id="u1"))
    assert [x["task_id"] for x in store.list_for_owner("u1")] == ["b", "a"]


def test_mark_interrupted_only_running_states(tmp_path):
    store = AgentMdTaskStore(_db_path(tmp_path))
    for tid, state in (
        ("uploaded", TaskState.UPLOADED), ("parsing", TaskState.PARSING),
        ("ner", TaskState.NER_RUNNING), ("ready", TaskState.MAPPING_READY),
        ("done", TaskState.COMPLETED), ("failed", TaskState.FAILED),
    ):
        store.upsert_meta(AgentMdTask(task_id=tid, state=state, filename="x.pdf", owner_id="u1"))
    assert store.mark_interrupted() == 3
    states = {tid: store.load(tid)["state"] for tid in ("uploaded", "parsing", "ner", "ready", "done", "failed")}
    assert states["parsing"] == "failed"
    assert states["ready"] == "mapping_ready"
    assert states["done"] == "completed"
    assert store.load("parsing")["message"] == INTERRUPTED_MESSAGE


def test_ddl_idempotent(tmp_path):
    path = _db_path(tmp_path)
    AgentMdTaskStore(path)
    AgentMdTaskStore(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM agent_md_tasks").fetchone()[0] == 0


# --- pipeline persistence ------------------------------------------------

def test_mapping_ready_survives_new_pipeline_instance(tmp_path, sample_pdf):
    store = AgentMdTaskStore(_db_path(tmp_path))
    svc1 = _service(tmp_path, store)
    task = svc1.create_task_nowait(sample_pdf, "a.pdf")
    assert task.state == TaskState.MAPPING_READY
    assert store.load(task.task_id)["payload_json"]

    svc2 = _service(tmp_path, store)
    restored = svc2.get_task(task.task_id)
    assert restored is not None and restored.state == TaskState.MAPPING_READY
    assert [m.replacement for m in restored.mapping] == ["张某", "李某"]
    assert restored.structured_md == "张三借李四人民币一万元"

    done = svc2.confirm_nowait(task.task_id, [])
    assert done.state == TaskState.COMPLETED
    assert store.load(task.task_id)["payload_json"] is None
    assert len(list(tmp_path.glob("a_脱敏MD_*.md"))) == 1


def test_completed_task_reload_and_confirm_is_idempotent(tmp_path, sample_pdf):
    store = AgentMdTaskStore(_db_path(tmp_path))
    svc1 = _service(tmp_path, store)
    task = svc1.create_task_nowait(sample_pdf, "a.pdf")
    done = svc1.confirm_nowait(task.task_id, [])
    output_id = done.output_file_id
    try:
        svc2 = _service(tmp_path, store)
        reloaded = svc2.get_task(task.task_id)
        assert reloaded is not None and reloaded.state == TaskState.COMPLETED
        assert reloaded.output_file_id == output_id
        assert svc2.confirm_nowait(task.task_id, []) is reloaded
        assert len(list(tmp_path.glob("a_脱敏MD_*.md"))) == 1
    finally:
        from app.services import file_management_service as fms
        fms.file_store.pop(output_id, None)


def test_corrupt_payload_fails_closed(tmp_path, sample_pdf):
    store = AgentMdTaskStore(_db_path(tmp_path))
    svc1 = _service(tmp_path, store)
    task = svc1.create_task_nowait(sample_pdf, "a.pdf")
    with sqlite3.connect(store._path) as conn:
        conn.execute(
            "UPDATE agent_md_tasks SET payload_json=? WHERE task_id=?",
            ("{not json", task.task_id),
        )
    svc2 = _service(tmp_path, store)
    restored = svc2.get_task(task.task_id)
    assert restored.state == TaskState.FAILED
    assert restored.message == PAYLOAD_CORRUPT_MESSAGE
    assert store.load(task.task_id)["state"] == "failed"


def test_concurrent_confirm_returns_same_result(tmp_path, sample_pdf):
    store = AgentMdTaskStore(_db_path(tmp_path))
    svc = _service(tmp_path, store)
    task = svc.create_task_nowait(sample_pdf, "a.pdf")
    try:
        results = asyncio.run(_gather_confirm(svc, task.task_id))
        assert all(r.state == TaskState.COMPLETED for r in results)
        assert results[0].output_file_id == results[1].output_file_id
        assert store.load(task.task_id)["state"] == "completed"
        assert len(list(tmp_path.glob("a_脱敏MD_*.md"))) == 1
    finally:
        from app.services import file_management_service as fms
        fms.file_store.pop(results[0].output_file_id, None)


def test_confirm_retry_reuses_pending_output_id(tmp_path, sample_pdf):
    """崩溃在 mark_output_pending 之后：重试必须复用同一 output_file_id，
    且 file_store 只登记一条产物（评审 I3 证据）。"""
    from app.services import file_management_service as fms

    store = AgentMdTaskStore(_db_path(tmp_path))
    svc = _service(tmp_path, store)
    task = svc.create_task_nowait(sample_pdf, "a.pdf")
    task.output_file_id = "fixed-output-0001"
    store.mark_output_pending(task)
    svc2 = _service(tmp_path, store)  # 模拟崩溃后重启：id 必须从 DB 恢复
    restored = svc2.get_task(task.task_id)
    assert restored is not None and restored.output_file_id == "fixed-output-0001"
    try:
        done = svc2.confirm_nowait(task.task_id, [])
        assert done.state == TaskState.COMPLETED
        assert done.output_file_id == "fixed-output-0001"
        rows = [
            rec for rec in fms.file_store.values()
            if isinstance(rec, dict) and rec.get("agent_md_task_id") == task.task_id
        ]
        assert len(rows) == 1
        assert store.load(task.task_id)["payload_json"] is None
    finally:
        for fid in [k for k, rec in fms.file_store.items()
                    if isinstance(rec, dict) and rec.get("agent_md_task_id") == task.task_id]:
            fms.file_store.pop(fid, None)


def test_confirm_aborts_when_pending_id_persist_fails(tmp_path, sample_pdf, monkeypatch):
    """mark_output_pending 失败必须在写产物前中止，避免换新 id 造成重复产物。"""
    store = AgentMdTaskStore(_db_path(tmp_path))
    svc = _service(tmp_path, store)
    task = svc.create_task_nowait(sample_pdf, "a.pdf")

    def boom(_task):
        raise RuntimeError("disk full")

    monkeypatch.setattr(store, "mark_output_pending", boom)
    with pytest.raises(RuntimeError, match="disk full"):
        svc.confirm_nowait(task.task_id, [])
    assert not list(tmp_path.glob("a_脱敏MD_*.md"))
    assert not list(tmp_path.glob("a_映射表_*.json"))
    assert not list(tmp_path.glob("a_保留清单_*.json"))
    from app.services import file_management_service as fms
    assert not [
        rec for rec in fms.file_store.values()
        if isinstance(rec, dict) and rec.get("agent_md_task_id") == task.task_id
    ]
    assert store.load(task.task_id)["state"] == "mapping_ready"


async def _gather_confirm(svc, task_id):
    return await asyncio.gather(svc.confirm(task_id, []), svc.confirm(task_id, []))


# --- list API ------------------------------------------------------------

@pytest.fixture
def store_api(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "AUTH_ENABLED", False)
    from app.services import agent_md_task_store as store_mod

    store = AgentMdTaskStore(_db_path(tmp_path))
    monkeypatch.setattr(store_mod, "_singleton_store", lambda: store)
    return store


def test_list_tasks_owner_scoped_and_401(store_api):
    store_api.upsert_meta(
        AgentMdTask(task_id="mine", state=TaskState.MAPPING_READY, filename="mine.pdf", owner_id="anonymous")
    )
    store_api.upsert_meta(
        AgentMdTask(task_id="other", state=TaskState.PARSING, filename="other.pdf", owner_id="u2")
    )
    resp = client.get(f"{settings.API_PREFIX}/agent-md/tasks")
    assert resp.status_code == 200
    body = resp.json()
    assert [x["task_id"] for x in body["items"]] == ["mine"]
    assert body["counts"] == {"active": 0, "awaiting_review": 1}

    settings.AUTH_ENABLED = True
    try:
        assert client.get(f"{settings.API_PREFIX}/agent-md/tasks").status_code == 401
    finally:
        settings.AUTH_ENABLED = False

# --- source classification ----------------------------------------------

def test_effective_upload_source_agent_md_and_legacy():
    from app.services.file_management_service import (
        effective_upload_source,
        sanitize_upload_source,
    )

    legacy = {
        "vl_md_meta": {"mapping_path": "/x.mapping.json"},
        "original_filename": "借条_脱敏MD_20261008_1407_9789.md",
    }
    assert effective_upload_source(legacy) == "agent_md"
    # VL-MD 产物也写 vl_md_meta，但保留原上传名，不得误判为供料
    assert effective_upload_source(
        {"vl_md_meta": {}, "original_filename": "卷宗.pdf", "filename": "abc.desens.md"}
    ) == "playground"
    # 批量供料带 job/group，仍归 batch
    assert effective_upload_source({**legacy, "job_id": "j1"}) == "batch"
    assert effective_upload_source({**legacy, "upload_source": "agent_md"}) == "agent_md"
    # 客户端不能伪造 agent_md 来源
    assert sanitize_upload_source("agent_md") is None


# --- review fixes: counts / schema version / two-phase commit / heal --------

def test_counts_include_failed_as_active(tmp_path):
    store = AgentMdTaskStore(_db_path(tmp_path))
    for tid, state in (
        ("run", TaskState.NER_RUNNING),
        ("bad", TaskState.FAILED),
        ("rev", TaskState.MAPPING_READY),
        ("done", TaskState.COMPLETED),
    ):
        store.upsert_meta(AgentMdTask(task_id=tid, state=state, filename=f"{tid}.pdf", owner_id="u1"))
    assert store.counts_for_owner("u1") == {"active": 2, "awaiting_review": 1}


def test_unknown_payload_schema_version_fails_closed(tmp_path):
    svc = _service(tmp_path, AgentMdTaskStore(_db_path(tmp_path)))
    row = {"task_id": "t", "payload_json": json.dumps({"schema_version": 999})}
    assert svc._load_payload(row) is None


def test_mark_output_pending_keeps_payload(tmp_path):
    store = AgentMdTaskStore(_db_path(tmp_path))
    task = AgentMdTask(
        task_id="t-1", state=TaskState.MAPPING_READY, filename="a.pdf", owner_id="u1",
        output_file_id="out-1",
    )
    store.save_payload(task, {"schema_version": 1, "segs": [], "chars": 0})
    store.mark_output_pending(task)
    row = store.load("t-1")
    assert row["output_file_id"] == "out-1"
    assert row["payload_json"] is not None  # 崩溃后重试仍需 payload


def test_restore_heals_crash_between_register_and_complete(tmp_path, monkeypatch):
    """AC7：产物已登记但 completed UPDATE 前崩溃 → 重启用同一 output id 自愈。"""
    from app.services import file_management_service as fms

    store = AgentMdTaskStore(_db_path(tmp_path))
    task = AgentMdTask(
        task_id="t-crash", state=TaskState.MAPPING_READY, filename="a.pdf", owner_id="u1",
        output_file_id="out-9", structured_md="# 张三",
        mapping=[MappingItem(id="e1", original_text="张三", entity_type="PERSON",
                             replacement="[人名_1]")],
    )
    store.save_payload(task, _service(tmp_path, store)._serialize_payload(task))
    md = tmp_path / "a_脱敏MD_20260101_0000_0009.md"
    mapping = tmp_path / "a_映射表_20260101_0000_0009.json"
    retained = tmp_path / "a_保留清单_20260101_0000_0009.json"
    for p in (md, mapping, retained):
        p.write_text("{}", encoding="utf-8")

    class _FakeFileStore:
        def get(self, file_id):
            if file_id != "out-9":
                return None
            return {
                "output_path": str(md),
                "vl_md_meta": {"mapping_path": str(mapping), "retained_path": str(retained)},
            }

    monkeypatch.setattr(fms, "file_store", _FakeFileStore())
    svc = _service(tmp_path, store)
    restored = svc.get_task("t-crash")
    assert restored is not None
    assert restored.state == TaskState.COMPLETED
    assert store.load("t-crash")["payload_json"] is None


def test_status_of_other_owner_is_404(tmp_path, monkeypatch):
    from app.core.auth import require_auth

    class _P:
        def get_task(self, task_id):
            return AgentMdTask(task_id="tk", state=TaskState.MAPPING_READY, filename="a.pdf",
                               owner_id="u1")

    monkeypatch.setattr(pipeline_mod, "get_agent_md_pipeline", lambda: _P())
    app.dependency_overrides[require_auth] = lambda: "u2"
    try:
        assert client.get("/api/v1/agent-md/tk/status").status_code == 404
    finally:
        app.dependency_overrides.pop(require_auth, None)


def test_files_source_filter_and_legacy_compat(tmp_path, monkeypatch):
    """AC3：/files 四来源隔离 + 旧供料/ VL-MD / 批量不串台。"""
    from app.api.jobs import get_job_store
    from app.services import file_management_service as fms

    def rec(fid, name, **extra):
        return (fid, {"file_id": fid, "owner_id": "anonymous", "original_filename": name,
                      "file_size": 10, "created_at": "2026-11-12T00:00:00", **extra})

    entries = [
        rec("new", "新_脱敏MD_20261112_0000_aaaa.md", output_path=str(tmp_path / "new.md"),
            file_type="txt", upload_source="agent_md", agent_md_task_id="t-new"),
        rec("legacy", "旧_脱敏MD_20260101_0000_bbbb.md",
            vl_md_meta={"mapping_path": "m.json", "retained_path": "r.json"}),
        rec("vlmd", "合同.pdf", vl_md_meta={"mapping_path": "m.json", "retained_path": "r.json"}),
        rec("batch", "批量.pdf", upload_source="batch", job_id="j1", batch_group_id="j1"),
    ]

    class _FS:
        def items_for_owner(self, owner_id):
            return entries if owner_id == "anonymous" else []

    class _JS:
        def batch_find_item_statuses(self, ids):
            return {}

    monkeypatch.setattr(settings, "AUTH_ENABLED", False)
    monkeypatch.setattr(fms, "get_file_store", lambda: _FS())
    app.dependency_overrides[get_job_store] = lambda: _JS()
    try:
        def ids(source):
            r = client.get(f"/api/v1/files?source={source}&page_size=100")
            assert r.status_code == 200, r.text
            return {x["file_id"]: x["upload_source"] for x in r.json()["files"]}

        assert ids("agent_md") == {"new": "agent_md", "legacy": "agent_md"}
        assert ids("playground") == {"vlmd": "playground"}
        assert ids("batch") == {"batch": "batch"}
        assert client.get("/api/v1/files?source=bogus").status_code == 400
    finally:
        app.dependency_overrides.pop(get_job_store, None)
