"""Issue#212 API 判据：status 可观测字段 / owner 隔离 / 零实体出稿闸。

harness 与 test_agent_md_api.py 同构（fake 管线注入 + AUTH 关闭 ⇒ owner="anonymous"）。
"""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import agent_md as agent_md_api
from app.core.config import settings
from app.main import app
from app.services import agent_md_pipeline_service as pipeline_mod
from app.services.agent_md_types import AgentMdTask, MappingItem, TaskState

client = TestClient(app)


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "AUTH_ENABLED", False)
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setattr(settings, "OUTPUT_DIR", str(tmp_path / "outputs"))


class _FakePipeline:
    def __init__(self, **task_kwargs):
        defaults = dict(
            task_id="tk-1", state=TaskState.MAPPING_READY, pages_total=3, pages_done=3,
            filename="a.pdf", file_path="x", owner_id="anonymous",
        )
        defaults.update(task_kwargs)
        self.t = AgentMdTask(**defaults)
        self.confirm_calls = 0

    async def create_task(self, file_path, filename, owner_id="local_user"):
        return self.t

    def get_task(self, task_id):
        return self.t if task_id == "tk-1" else None

    async def confirm(self, task_id, decisions):
        self.confirm_calls += 1
        self.t.state = TaskState.COMPLETED
        self.t.output_file_id = "out-1"
        out = Path(settings.OUTPUT_DIR)
        out.mkdir(parents=True, exist_ok=True)
        (out / "a_脱敏MD_20261029_1700_0001.md").write_text("脱敏MD", encoding="utf-8")
        return self.t


@pytest.fixture
def fake(monkeypatch):
    def _make(**kw):
        fp = _FakePipeline(**kw)
        monkeypatch.setattr(pipeline_mod, "get_agent_md_pipeline", lambda: fp)
        return fp

    return _make


# ---- AC4：status 可观测（键恒存在、0 显式、warning 截断）----


def test_status_exposes_dropped_blocks_and_chars(fake):
    """API 级**非零**例：79 个空表必须能从 status 读到（防默认值/常量归一蒙过）。"""
    fake(
        dropped_blocks={"empty_table": 79, "unhandled": {"title": 8},
                        "source": "content_list_v1"},
        chars=138, warnings=["w"] * 21,
    )

    body = client.get("/api/v1/agent-md/tk-1/status").json()

    assert body["dropped_blocks"] == {
        "empty_table": 79, "unhandled": {"title": 8}, "source": "content_list_v1"}
    assert body["chars"] == 138
    assert body["warnings_total"] == 21
    assert len(body["warnings"]) == 20


def test_status_zero_counts_are_explicit_not_missing(fake):
    """未实现时键缺失 ⇒ KeyError；修复后必须显式 0/空 dict。"""
    fake()

    body = client.get("/api/v1/agent-md/tk-1/status").json()

    assert body["dropped_blocks"] == {"empty_table": 0, "unhandled": {},
                                      "source": "none"}
    assert body["chars"] == 0


# ---- AC5：owner 隔离 ----


@pytest.mark.parametrize("path", ["status", "mapping"])
def test_other_owner_cannot_read(fake, path):
    fp = fake()
    fp.t.owner_id = "someone-else"

    assert client.get(f"/api/v1/agent-md/tk-1/{path}").status_code == 404


def test_other_owner_cannot_confirm_or_download(fake):
    fp = fake()
    fp.t.owner_id = "someone-else"

    assert client.post("/api/v1/agent-md/tk-1/confirm", json={"decisions": []}).status_code == 404
    assert client.get("/api/v1/agent-md/tk-1/artifacts/md").status_code == 404
    assert fp.confirm_calls == 0


# ---- AC5：零实体出稿闸 ----


def test_zero_entities_requires_ack_and_produces_nothing(fake):
    fp = fake(mapping=[], entity_types_used=["PERSON", "ID_CARD", "BANK_CARD"])

    resp = client.post("/api/v1/agent-md/tk-1/confirm", json={"decisions": []})

    assert resp.status_code == 409
    assert resp.json()["error_code"] == "ZERO_ENTITIES_NOT_ACKNOWLEDGED"
    assert fp.confirm_calls == 0
    assert client.get("/api/v1/agent-md/tk-1/artifacts/md").status_code == 404


def test_zero_entities_with_ack_passes_and_audits(fake, monkeypatch):
    fp = fake(mapping=[], entity_types_used=["PERSON"], chars=143, pages_total=80)
    audited: list[tuple] = []
    monkeypatch.setattr(agent_md_api, "audit_log",
                        lambda *a, **kw: audited.append((a, kw)))

    resp = client.post("/api/v1/agent-md/tk-1/confirm",
                       json={"decisions": [], "acknowledge_zero_entities": True})

    assert resp.status_code == 200
    assert fp.confirm_calls == 1
    assert len(audited) == 1
    action, resource_type, resource_id = audited[0][0]
    assert action == "agent_md_confirm_zero_entities"
    assert resource_id == "tk-1"
    assert audited[0][1]["user"] == "anonymous"
    assert audited[0][1]["detail"]["chars"] == 143


def test_all_entity_types_disabled_is_exempt(fake):
    """用户主动停用全部类型 ⇒ 不是"没识别出来"，不该拦（避免误报）。"""
    fp = fake(mapping=[], entity_types_used=[])

    resp = client.post("/api/v1/agent-md/tk-1/confirm", json={"decisions": []})

    assert resp.status_code == 200
    assert fp.confirm_calls == 1


def test_non_zero_mapping_confirm_unchanged(fake):
    """正向对照：有映射且无 ack ⇒ 200（防"所有 confirm 都要求 ack"的假绿）。"""
    fp = fake(mapping=[MappingItem(id="e1", original_text="张三", entity_type="PERSON",
                                   replacement="[人名_1]")],
              entity_types_used=["PERSON"])

    resp = client.post("/api/v1/agent-md/tk-1/confirm", json={"decisions": []})

    assert resp.status_code == 200
    assert resp.json()["output_file_id"] == "out-1"
    assert fp.confirm_calls == 1


def test_zero_entity_gate_requires_mapping_ready_state(fake):
    """前置状态校验照旧生效：非 mapping_ready 回 TASK_NOT_READY（不是零实体码）。"""
    fp = fake(mapping=[], entity_types_used=["PERSON"])
    fp.t.state = TaskState.NER_RUNNING

    resp = client.post("/api/v1/agent-md/tk-1/confirm", json={"decisions": []})

    assert resp.status_code == 409
    assert resp.json()["error_code"] == "TASK_NOT_READY"
    assert fp.confirm_calls == 0
