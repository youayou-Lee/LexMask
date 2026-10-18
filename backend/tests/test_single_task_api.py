"""Issue#194: 识别任务提交端点 POST /files/{file_id}/recognition-task。

端点契约：200 受理回执 / 409 冲突文案含在跑文件名 / 404 文件不存在 /
403 非本人 / 400 加密 PDF 预探测（error_code=PDF_ENCRYPTED_NEEDS_PASSWORD）/
X-Idempotency-Key 命中回缓存。守门本体在 single_task_service（Task 2），
这里 monkeypatch 该层，只验证端点接线与错误映射。
"""

import os

import fitz
import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.idempotency import _store as _idem_store
from app.main import app
from app.services import file_management_service as fms
from app.services import single_task_service as sts

client = TestClient(app)


def _make_pdf(path: str, *, user_pw: str | None = None) -> None:
    """PyMuPDF 现场生成 PDF（可选打开密码），不依赖外部夹具。"""
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 100), "委托人陈文清", fontsize=12, fontname="china-s")
    doc.save(path, encryption=fitz.PDF_ENCRYPT_AES_256, **({"user_pw": user_pw} if user_pw else {}))
    doc.close()


def _register(file_id: str = "f1", owner_id: str = "anonymous", file_type: str = "txt", path: str | None = None) -> str:
    """往 file_store 注册一个待识别文件（owner 默认同匿名主体）。"""
    fms.file_store[file_id] = {
        "id": file_id,
        "file_id": file_id,
        "original_filename": "卷宗.pdf" if file_type.startswith("pdf") else "记录.txt",
        "file_path": path or os.path.join(settings.UPLOAD_DIR, f"{file_id}.bin"),
        "file_type": file_type,
        "owner_id": owner_id,
    }
    return file_id


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    up = tmp_path / "uploads"
    up.mkdir()
    monkeypatch.setattr(settings, "AUTH_ENABLED", False)  # require_auth → anonymous
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(up))
    fms.file_store.clear()
    _idem_store.clear()
    yield


def test_recognition_task_happy_path_passes_body_and_title(monkeypatch):
    _register()
    seen: dict = {}

    async def fake_submit(store, *, file_id, entity_type_ids, owner_id, title=""):
        seen.update(
            file_id=file_id,
            entity_type_ids=list(entity_type_ids),
            owner_id=owner_id,
            title=title,
        )
        return {"job_id": "j1", "item_id": "i1", "file_id": file_id}

    monkeypatch.setattr(sts, "submit_single_recognition", fake_submit)
    resp = client.post(
        "/api/v1/files/f1/recognition-task",
        json={"entity_type_ids": ["t1", "t2"]},
    )
    assert resp.status_code == 200
    assert resp.json() == {"job_id": "j1", "item_id": "i1", "file_id": "f1"}
    assert seen["entity_type_ids"] == ["t1", "t2"]
    assert seen["owner_id"] == "anonymous"
    assert seen["title"] == "记录.txt"


def test_recognition_task_conflict_maps_409_with_running_filename(monkeypatch):
    _register()

    async def boom(*a, **k):
        raise sts.SingleTaskConflictError("f9", "running.pdf")

    monkeypatch.setattr(sts, "submit_single_recognition", boom)
    resp = client.post("/api/v1/files/f1/recognition-task", json={"entity_type_ids": []})
    assert resp.status_code == 409
    assert "running.pdf" in resp.json()["message"]


def test_recognition_task_unknown_file_404(monkeypatch):
    calls: list = []

    async def fake_submit(*a, **k):
        calls.append(1)
        return {}

    monkeypatch.setattr(sts, "submit_single_recognition", fake_submit)
    resp = client.post("/api/v1/files/no-such-file/recognition-task", json={"entity_type_ids": []})
    assert resp.status_code == 404
    assert resp.json()["message"] == "文件不存在"
    assert not calls  # 不存在即拒绝，守门层不被触达


def test_recognition_task_not_owner_403(monkeypatch):
    _register(owner_id="someone-else")
    calls: list = []

    async def fake_submit(*a, **k):
        calls.append(1)
        return {}

    monkeypatch.setattr(sts, "submit_single_recognition", fake_submit)
    resp = client.post("/api/v1/files/f1/recognition-task", json={"entity_type_ids": []})
    assert resp.status_code == 403
    assert not calls  # 非本人文件不进守门层


def test_recognition_task_file_deleted_between_precheck_and_submit_maps_404(monkeypatch):
    """TOCTOU：预检与提交之间文件被并发删除 → 守门层 KeyError 映射 404（不 500）。"""
    _register()

    async def gone(*a, **k):
        raise KeyError("f1")

    monkeypatch.setattr(sts, "submit_single_recognition", gone)
    resp = client.post("/api/v1/files/f1/recognition-task", json={"entity_type_ids": []})
    assert resp.status_code == 404
    assert resp.json()["message"] == "文件不存在"


def test_recognition_task_permission_lost_between_precheck_and_submit_maps_403(monkeypatch):
    """TOCTOU：预检与提交之间归属变化 → 守门层 PermissionError 映射 403（不 500）。

    文件登记为本人（anonymous），预检放行；异常从守门层抛出，验证的是
    submit 之后的 PermissionError 映射，而非既有预检 403。
    """
    _register()

    async def denied(*a, **k):
        raise PermissionError("f1")

    monkeypatch.setattr(sts, "submit_single_recognition", denied)
    resp = client.post("/api/v1/files/f1/recognition-task", json={"entity_type_ids": []})
    assert resp.status_code == 403
    assert resp.json()["message"] == "无权操作该文件"


def test_recognition_task_encrypted_pdf_maps_400_needs_password(monkeypatch):
    path = os.path.join(settings.UPLOAD_DIR, "enc.pdf")
    _make_pdf(path, user_pw="secret")
    _register(file_type="pdf", path=path)
    calls: list = []

    async def fake_submit(*a, **k):
        calls.append(1)
        return {}

    monkeypatch.setattr(sts, "submit_single_recognition", fake_submit)
    resp = client.post("/api/v1/files/f1/recognition-task", json={"entity_type_ids": []})
    assert resp.status_code == 400
    assert resp.json()["error_code"] == "PDF_ENCRYPTED_NEEDS_PASSWORD"
    assert not calls  # 加密卷在预探测即拒绝，不进守门/队列


def test_recognition_task_plain_pdf_passes_precheck(monkeypatch):
    path = os.path.join(settings.UPLOAD_DIR, "plain.pdf")
    _make_pdf(path)
    _register(file_type="pdf", path=path)

    async def fake_submit(store, *, file_id, entity_type_ids, owner_id, title=""):
        return {"job_id": "j2", "item_id": "i2", "file_id": file_id}

    monkeypatch.setattr(sts, "submit_single_recognition", fake_submit)
    resp = client.post("/api/v1/files/f1/recognition-task", json={"entity_type_ids": []})
    assert resp.status_code == 200
    assert resp.json()["job_id"] == "j2"


def test_recognition_task_idempotency_key_hit_returns_cached(monkeypatch):
    _register()
    calls: list = []

    async def fake_submit(store, *, file_id, entity_type_ids, owner_id, title=""):
        calls.append(1)
        return {"job_id": "j3", "item_id": "i3", "file_id": file_id}

    monkeypatch.setattr(sts, "submit_single_recognition", fake_submit)
    headers = {"X-Idempotency-Key": "idem-cache-1"}
    first = client.post("/api/v1/files/f1/recognition-task", json={"entity_type_ids": []}, headers=headers)
    second = client.post("/api/v1/files/f1/recognition-task", json={"entity_type_ids": []}, headers=headers)
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json() == second.json() == {"job_id": "j3", "item_id": "i3", "file_id": "f1"}
    assert len(calls) == 1  # 第二次命中缓存，守门层只执行一次
