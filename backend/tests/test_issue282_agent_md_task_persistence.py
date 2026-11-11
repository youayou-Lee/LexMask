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
