"""agent-md API 集成测试：fake 管线注入，全链 HTTP 断言。合成内容。

fake 通过 monkeypatch `agent_md_pipeline_service.get_agent_md_pipeline` 注入——
router 层必须经模块属性调用（`pipeline_mod.get_agent_md_pipeline()`）才能被拦截。
加密 PDF 三态（未给密码/密码错/密码对）复用 Issue#30 file_parser 链路，行为
对齐既有 decrypt 端点：结构化错误码由 AppError envelope 携带（前端按码弹框）。
"""
import io
import json as _json
from pathlib import Path

import fitz
import pytest
from fastapi.testclient import TestClient

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
    def __init__(self):
        self.t = AgentMdTask(task_id="tk-1", state=TaskState.MAPPING_READY, pages_total=3, pages_done=3,
                             filename="a.pdf", file_path="x")
        self.t.mapping = [MappingItem(id="e1", original_text="张三", entity_type="PERSON", replacement="[人名_1]")]
        self.created_with: tuple[str, str] | None = None

    async def create_task(self, file_path, filename, owner_id="local_user"):
        self.created_with = (file_path, filename)
        return self.t

    def get_task(self, task_id):
        return self.t if task_id == "tk-1" else None

    async def confirm(self, task_id, decisions):
        self.t.state = TaskState.COMPLETED
        self.t.output_file_id = "out-1"
        out = Path(settings.OUTPUT_DIR)
        out.mkdir(parents=True, exist_ok=True)
        (out / "out-1.md").write_text("脱敏MD", encoding="utf-8")
        (out / "out-1.mapping.json").write_text(_json.dumps({"items": []}), encoding="utf-8")
        (out / "out-1.retained_fields.json").write_text(_json.dumps({"retained_fields": []}), encoding="utf-8")
        return self.t


@pytest.fixture
def fake_pipeline(monkeypatch):
    fp = _FakePipeline()
    monkeypatch.setattr(pipeline_mod, "get_agent_md_pipeline", lambda: fp)
    return fp


def _pdf_bytes() -> bytes:
    doc = fitz.open()
    doc.new_page().insert_text((72, 72), "Synthetic")
    buf = io.BytesIO()
    doc.save(buf)
    doc.close()
    return buf.getvalue()


def _encrypted_pdf_bytes() -> bytes:
    doc = fitz.open()
    doc.new_page().insert_text((72, 72), "Secret")
    buf = io.BytesIO()
    doc.save(buf, encryption=fitz.PDF_ENCRYPT_AES_256, owner_pw="owner-pw", user_pw="user-pw")
    doc.close()
    return buf.getvalue()


def test_upload_returns_task_id(fake_pipeline):
    r = client.post("/api/v1/agent-md/upload",
                    files={"file": ("a.pdf", _pdf_bytes(), "application/pdf")})
    assert r.status_code == 200 and r.json()["task_id"] == "tk-1"


class _FakeStore:
    """fms.file_store 桩：只实现 upload(file_id) 路径用到的 get。"""

    def __init__(self, entries):
        self._entries = entries

    def get(self, file_id):
        return self._entries.get(file_id)


def test_upload_with_file_id_reuses_stored_file(fake_pipeline, tmp_path, monkeypatch):
    from app.services import file_management_service as fms

    uploads = tmp_path / "uploads"
    uploads.mkdir()
    src = uploads / "src.pdf"
    src.write_bytes(_pdf_bytes())
    monkeypatch.setattr(fms, "file_store", _FakeStore(
        {"fid-1": {"file_path": str(src), "original_filename": "案卷.pdf"}}))
    r = client.post("/api/v1/agent-md/upload", data={"file_id": "fid-1"})
    assert r.status_code == 200 and r.json()["task_id"] == "tk-1"
    assert fake_pipeline.created_with == (str(src), "案卷.pdf")
    # 未写新的上传副本
    assert [p for p in uploads.glob("*.pdf") if p.name != "src.pdf"] == []


def test_upload_with_file_id_unknown_404(fake_pipeline, monkeypatch):
    from app.services import file_management_service as fms

    monkeypatch.setattr(fms, "file_store", _FakeStore({}))
    r = client.post("/api/v1/agent-md/upload", data={"file_id": "nope"})
    assert r.status_code == 404
    assert r.json()["error_code"] == "FILE_NOT_FOUND"


def test_upload_with_file_id_not_pdf_400(fake_pipeline, tmp_path, monkeypatch):
    from app.services import file_management_service as fms

    txt = tmp_path / "a.txt"
    txt.write_text("x")
    monkeypatch.setattr(fms, "file_store", _FakeStore({"fid-2": {"file_path": str(txt)}}))
    r = client.post("/api/v1/agent-md/upload", data={"file_id": "fid-2"})
    assert r.status_code == 400
    assert r.json()["error_code"] == "UNSUPPORTED_FILE_TYPE"


def test_upload_with_file_id_missing_disk_404(fake_pipeline, tmp_path, monkeypatch):
    from app.services import file_management_service as fms

    monkeypatch.setattr(fms, "file_store", _FakeStore(
        {"fid-3": {"file_path": str(tmp_path / "gone.pdf")}}))
    r = client.post("/api/v1/agent-md/upload", data={"file_id": "fid-3"})
    assert r.status_code == 404
    assert r.json()["error_code"] == "FILE_NOT_FOUND"


def test_upload_without_file_or_file_id_400(fake_pipeline):
    r = client.post("/api/v1/agent-md/upload")
    assert r.status_code == 400
    assert r.json()["error_code"] == "MISSING_FILE"


def test_upload_rejects_non_pdf(fake_pipeline):
    r = client.post("/api/v1/agent-md/upload", files={"file": ("a.txt", b"x", "text/plain")})
    assert r.status_code == 400
    assert r.json()["error_code"] == "UNSUPPORTED_FILE_TYPE"


def test_upload_encrypted_without_password_409(fake_pipeline):
    r = client.post("/api/v1/agent-md/upload",
                    files={"file": ("a.pdf", _encrypted_pdf_bytes(), "application/pdf")})
    assert r.status_code == 409
    assert r.json()["error_code"] == "PDF_ENCRYPTED_NEEDS_PASSWORD"


def test_upload_encrypted_wrong_password_409(fake_pipeline):
    r = client.post("/api/v1/agent-md/upload",
                    files={"file": ("a.pdf", _encrypted_pdf_bytes(), "application/pdf")},
                    data={"password": "wrong"})
    assert r.status_code == 409
    assert r.json()["error_code"] == "PDF_WRONG_PASSWORD"


def test_upload_encrypted_with_password_ok(fake_pipeline):
    r = client.post("/api/v1/agent-md/upload",
                    files={"file": ("a.pdf", _encrypted_pdf_bytes(), "application/pdf")},
                    data={"password": "user-pw"})
    assert r.status_code == 200 and r.json()["task_id"] == "tk-1"
    # 落盘副本已解密（needs_pass=False），后续管线可直接读取
    stored = list(Path(settings.UPLOAD_DIR).glob("*.pdf"))
    assert len(stored) == 1
    with fitz.open(str(stored[0])) as doc:
        assert not doc.needs_pass


def test_status_and_mapping(fake_pipeline):
    assert client.get("/api/v1/agent-md/tk-1/status").json()["state"] == "mapping_ready"
    items = client.get("/api/v1/agent-md/tk-1/mapping").json()["items"]
    assert items[0]["original_text"] == "张三"
    assert client.get("/api/v1/agent-md/nope/status").status_code == 404


def test_mapping_not_ready_409(fake_pipeline):
    fake_pipeline.t.state = TaskState.PARSING
    r = client.get("/api/v1/agent-md/tk-1/mapping")
    assert r.status_code == 409
    assert r.json()["error_code"] == "TASK_NOT_READY"


def test_confirm_and_download(fake_pipeline):
    r = client.post("/api/v1/agent-md/tk-1/confirm", json={"decisions": []})
    assert r.status_code == 200 and r.json()["output_file_id"] == "out-1"
    downloads = r.json()["downloads"]
    assert downloads["md"].endswith("/agent-md/tk-1/artifacts/md")
    assert client.get("/api/v1/agent-md/tk-1/artifacts/md").content == "脱敏MD".encode("utf-8")
    assert client.get("/api/v1/agent-md/tk-1/artifacts/mapping").status_code == 200
    assert client.get("/api/v1/agent-md/tk-1/artifacts/retained").status_code == 200
    assert client.get("/api/v1/agent-md/tk-1/artifacts/exe").status_code == 404


def test_confirm_unknown_task_404(fake_pipeline):
    assert client.post("/api/v1/agent-md/nope/confirm", json={"decisions": []}).status_code == 404


def test_artifacts_before_complete_404(fake_pipeline):
    r = client.get("/api/v1/agent-md/tk-1/artifacts/md")  # MAPPING_READY 尚未出稿
    assert r.status_code == 404
    assert r.json()["error_code"] == "ARTIFACT_NOT_FOUND"
