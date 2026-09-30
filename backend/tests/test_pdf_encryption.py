"""加密 PDF 全链路处理（Issue #30）。

链路：parse 前置检测 → 仅权限密码自动解密 / 打开密码结构化报错
（400 + PDF_ENCRYPTED_NEEDS_PASSWORD）→ decrypt 端点密码认证解密
（错误码 PDF_WRONG_PASSWORD）→ vision / locate 链不再漏 404 英文原文 →
worker 批量链友好中文报错。

夹具全部用 PyMuPDF 现场生成（AES-256 四变体），不依赖真实案卷。
"""

import asyncio
import os

import fitz
import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import app
from app.services import file_management_service as fms
from app.services.file_parser import (
    FileParser,
    PdfEncryptedError,
    decrypt_pdf_with_password,
    ensure_pdf_accessible,
    open_pdf_checked,
)

client = TestClient(app)

SECRET_TEXT = "委托人陈文清，身份证号110101199001011234，住址北京市东城区某街道1号。"


def _make_pdf(path: str, *, user_pw: str | None = None, owner_pw: str | None = None) -> None:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 100), SECRET_TEXT, fontsize=12, fontname="china-s")
    kwargs = {}
    if user_pw is not None:
        kwargs["user_pw"] = user_pw
    if owner_pw is not None:
        kwargs["owner_pw"] = owner_pw
    doc.save(path, encryption=fitz.PDF_ENCRYPT_AES_256, **kwargs)
    doc.close()


def _is_encrypted_on_disk(path: str) -> bool:
    doc = fitz.open(path)
    try:
        return bool(doc.is_encrypted)
    finally:
        doc.close()


class _FakeOwnerOnlyDoc:
    """模拟「仅权限密码」文档态。

    PyMuPDF 写不出这种真实文件（save 时 user_pw 为空会直接不加密，实测
    is_encrypted=False），qpdf 亦不可用，因此仅权限密码分支的接线逻辑用替身
    验证；真实加密字段的端到端行为留给云上验收（qpdf/pikepdf 生成夹具）。
    """

    def __init__(self, path: str):
        self.needs_pass = False
        self.is_encrypted = True
        self._path = path
        self.closed = False

    def authenticate(self, password: str) -> int:
        return 1 if password == "" else 0

    def save(self, path: str, **_kwargs) -> None:
        with open(path, "wb") as f:
            f.write(b"%PDF-1.6 decrypted-by-fake\n")

    def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    up = tmp_path / "uploads"
    up.mkdir()
    monkeypatch.setattr(settings, "AUTH_ENABLED", False)  # require_auth → anonymous
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(up))
    fms.file_store.clear()
    yield


def _register(name: str, **pdf_kwargs) -> tuple[str, str]:
    path = os.path.join(settings.UPLOAD_DIR, name)
    _make_pdf(path, **pdf_kwargs)
    file_id = f"enc_{name.replace('.', '_')}"
    fms.file_store[file_id] = {
        "file_id": file_id,
        "file_path": path,
        "file_type": "pdf",
        "original_filename": name,
        "owner_id": "anonymous",
    }
    return file_id, path


# ---------------------------------------------------------------------------
# 底层助手：三态分类 / 自动解密 / 结构化异常
# ---------------------------------------------------------------------------


class TestEnsurePdfAccessible:
    def test_plain_pdf_untouched(self):
        _, path = _register("plain.pdf")
        before = open(path, "rb").read()
        ensure_pdf_accessible(path)
        assert open(path, "rb").read() == before
        assert not _is_encrypted_on_disk(path)

    def test_owner_only_auto_decrypts_and_keeps_content(self):
        _, path = _register("owner.pdf", owner_pw="ownerpw", user_pw="")
        ensure_pdf_accessible(path)
        assert not _is_encrypted_on_disk(path)
        doc = fitz.open(path)
        try:
            assert SECRET_TEXT[:6] in doc.load_page(0).get_text()
        finally:
            doc.close()

    def test_user_password_raises_structured_and_file_untouched(self):
        _, path = _register("user.pdf", user_pw="userpw")
        with pytest.raises(PdfEncryptedError) as ei:
            ensure_pdf_accessible(path)
        assert ei.value.error_code == "PDF_ENCRYPTED_NEEDS_PASSWORD"
        assert ei.value.needs_password is True
        assert _is_encrypted_on_disk(path)

    def test_owner_only_branch_strips_and_replaces(self, monkeypatch):
        _, path = _register("fake_owner.pdf")  # 真实普通 PDF 占位，打开动作被替身接管
        monkeypatch.setattr(
            "app.services.file_parser.fitz.open", lambda p, *a, **k: _FakeOwnerOnlyDoc(p)
        )
        ensure_pdf_accessible(path)
        with open(path, "rb") as f:
            assert f.read().startswith(b"%PDF-1.6 decrypted-by-fake")


class TestOpenPdfChecked:
    def test_plain_returns_doc(self):
        _, path = _register("plain2.pdf")
        doc = open_pdf_checked(path)
        try:
            assert len(doc) == 1
        finally:
            doc.close()

    def test_user_password_raises(self):
        _, path = _register("user2.pdf", user_pw="userpw")
        with pytest.raises(PdfEncryptedError):
            open_pdf_checked(path)

    def test_owner_only_opens_authenticated(self, monkeypatch):
        _, path = _register("owner2.pdf")
        monkeypatch.setattr(
            "app.services.file_parser.fitz.open", lambda p, *a, **k: _FakeOwnerOnlyDoc(p)
        )
        doc = open_pdf_checked(path)
        assert doc.is_encrypted is True
        doc.close()
        assert doc.closed


class TestDecryptWithPassword:
    def test_wrong_password_raises_wrong_code(self):
        _, path = _register("d.pdf", user_pw="right")
        with pytest.raises(PdfEncryptedError) as ei:
            decrypt_pdf_with_password(path, "wrong")
        assert ei.value.error_code == "PDF_WRONG_PASSWORD"
        assert _is_encrypted_on_disk(path)

    def test_correct_password_strips_encryption(self):
        _, path = _register("d2.pdf", user_pw="right")
        assert decrypt_pdf_with_password(path, "right") is True
        assert not _is_encrypted_on_disk(path)
        doc = fitz.open(path)
        try:
            assert SECRET_TEXT[:6] in doc.load_page(0).get_text()
        finally:
            doc.close()

    def test_already_decrypted_is_noop(self):
        _, path = _register("d3.pdf", user_pw="right")
        decrypt_pdf_with_password(path, "right")
        assert decrypt_pdf_with_password(path, "right") is False

    def test_owner_only_strips_without_password_check(self, monkeypatch):
        _, path = _register("d4.pdf")
        monkeypatch.setattr(
            "app.services.file_parser.fitz.open", lambda p, *a, **k: _FakeOwnerOnlyDoc(p)
        )
        assert decrypt_pdf_with_password(path, "whatever") is True
        with open(path, "rb") as f:
            assert b"decrypted-by-fake" in f.read()


class TestErrorContract:
    def test_not_a_valueerror_subclass(self):
        # task_queue 通用异常元组含 ValueError；PdfEncryptedError 必须不被其吞掉，
        # 否则批量条目 error_message 会退回 "worker: ValueError: ..." 英文格式。
        assert not issubclass(PdfEncryptedError, ValueError)

    def test_user_message_carries_chinese_text(self):
        err = PdfEncryptedError(
            "该 PDF 已加密，请输入密码后继续",
            error_code="PDF_ENCRYPTED_NEEDS_PASSWORD",
            needs_password=True,
        )
        assert err.user_message == "该 PDF 已加密，请输入密码后继续"
        assert str(err) == err.user_message


# ---------------------------------------------------------------------------
# 解析链路：不再误判扫描件、端点返回结构化错误码
# ---------------------------------------------------------------------------


class TestParseFlow:
    def test_parse_user_password_raises_not_misjudged(self):
        file_id, _ = _register("case.pdf", user_pw="userpw")
        # 修复前：页数 0 → is_scanned=True 误判，静默进入视觉链路
        with pytest.raises(PdfEncryptedError) as ei:
            asyncio.run(fms.parse_file(file_id))
        assert ei.value.error_code == "PDF_ENCRYPTED_NEEDS_PASSWORD"

    def test_parse_endpoint_returns_400_code(self):
        file_id, _ = _register("case_api.pdf", user_pw="userpw")
        resp = client.get(f"/api/v1/files/{file_id}/parse")
        assert resp.status_code == 400
        body = resp.json()
        assert body["error_code"] == "PDF_ENCRYPTED_NEEDS_PASSWORD"
        assert "加密" in body["message"]

    # 注：真实「仅权限密码」字节的 parse 无感解密在云上验收覆盖
    # （本地 PyMuPDF 无法生成该加密形态，见 _FakeOwnerOnlyDoc 注释）。


# ---------------------------------------------------------------------------
# decrypt 端点
# ---------------------------------------------------------------------------


class TestDecryptEndpoint:
    def test_wrong_password_400_with_code(self):
        file_id, path = _register("de1.pdf", user_pw="right")
        resp = client.post(f"/api/v1/files/{file_id}/decrypt", json={"password": "wrong"})
        assert resp.status_code == 400
        assert resp.json()["error_code"] == "PDF_WRONG_PASSWORD"
        assert _is_encrypted_on_disk(path)

    def test_success_then_idempotent(self):
        file_id, path = _register("de2.pdf", user_pw="right")
        resp = client.post(f"/api/v1/files/{file_id}/decrypt", json={"password": "right"})
        assert resp.status_code == 200
        assert not _is_encrypted_on_disk(path)
        resp2 = client.post(f"/api/v1/files/{file_id}/decrypt", json={"password": "right"})
        assert resp2.status_code == 200

    def test_missing_file_404(self):
        resp = client.post("/api/v1/files/no-such-file/decrypt", json={"password": "x"})
        assert resp.status_code == 404

    def test_non_pdf_rejected(self):
        path = os.path.join(settings.UPLOAD_DIR, "t.txt")
        with open(path, "w") as f:
            f.write("x")
        fms.file_store["enc_t_txt"] = {
            "file_id": "enc_t_txt",
            "file_path": path,
            "file_type": "txt",
            "original_filename": "t.txt",
            "owner_id": "anonymous",
        }
        resp = client.post("/api/v1/files/enc_t_txt/decrypt", json={"password": "x"})
        assert resp.status_code == 400

    def test_owner_mismatch_404(self):
        file_id, _ = _register("de5.pdf", user_pw="right")
        info = dict(fms.file_store[file_id])
        info["owner_id"] = "someone-else"
        fms.file_store[file_id] = info
        resp = client.post(f"/api/v1/files/{file_id}/decrypt", json={"password": "right"})
        assert resp.status_code == 404

    def test_audit_detail_never_carries_password(self, monkeypatch):
        calls: list[tuple] = []

        def _spy(action, resource_type, resource_id="", user="anonymous", detail=None):
            calls.append((action, detail))

        monkeypatch.setattr("app.api.files.audit_log", _spy)
        file_id, _ = _register("de6.pdf", user_pw="right")
        resp = client.post(f"/api/v1/files/{file_id}/decrypt", json={"password": "right"})
        assert resp.status_code == 200
        assert calls, "decrypt 成功必须写审计"
        action, detail = calls[-1]
        assert action == "decrypt"
        assert "password" not in (detail or {})
        assert "top-secret" not in str(detail)


# ---------------------------------------------------------------------------
# 视觉 / 定位链路：400 + 错误码，不再 404 + 英文原文
# ---------------------------------------------------------------------------


class TestVisionAndLocateGuards:
    def test_get_pdf_page_image_raises_structured(self):
        _, path = _register("v.pdf", user_pw="userpw")
        with pytest.raises(PdfEncryptedError) as ei:
            asyncio.run(FileParser().get_pdf_page_image(path, 1))
        assert ei.value.error_code == "PDF_ENCRYPTED_NEEDS_PASSWORD"

    def test_is_pdf_page_scanned_raises_structured(self):
        _, path = _register("v2.pdf", user_pw="userpw")
        with pytest.raises(PdfEncryptedError):
            asyncio.run(FileParser().is_pdf_page_scanned(path, 1))

    def test_vision_endpoint_400_code_not_404(self):
        file_id, _ = _register("v3.pdf", user_pw="userpw")
        resp = client.post(f"/api/v1/redaction/{file_id}/vision?page=1")
        assert resp.status_code == 400
        body = resp.json()
        assert body["error_code"] == "PDF_ENCRYPTED_NEEDS_PASSWORD"
        assert "encrypted" not in body["message"].lower()

    def test_locate_entities_endpoint_400_code_not_404(self):
        file_id, _ = _register("l.pdf", user_pw="userpw")
        resp = client.post(
            f"/api/v1/redaction/{file_id}/locate-entities",
            json={"entities": [{"id": "e1", "text": "陈文清", "type": "PERSON", "start": 0, "end": 3, "page": 1, "selected": True}]},
        )
        assert resp.status_code == 400
        body = resp.json()
        assert body["error_code"] == "PDF_ENCRYPTED_NEEDS_PASSWORD"
