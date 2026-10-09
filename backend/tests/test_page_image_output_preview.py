"""成品页图预览端点（Issue #83）。

page-image?redacted=true 是结果页「成品预览」的数据源（与历史对比共享）：
- 响应头 X-Page-Count 必须是**成品自身**真实页数（替换 docx 回转后可能与
  原卷不同，不能拿上传时的 page_count 顶替）；
- 页码越界收口 4xx（原实现漏捕 ValueError 成 500，成品翻页必踩）；
- 未执行（无 output_path）→ 400；加密卷仍走 #30 结构化错误码。

夹具全部用 PyMuPDF 现场生成，不依赖真实案卷。
"""

import os

import fitz
import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import app
from app.services import file_management_service as fms
from app.services.file_parser import FileParser, PdfEncryptedError

client = TestClient(app)


def _make_pdf(path: str, pages: int, text: str = "测试内容") -> None:
    doc = fitz.open()
    for index in range(pages):
        page = doc.new_page()
        page.insert_text((72, 100), f"{text} 第{index + 1}页", fontsize=12, fontname="china-s")
    doc.save(path)
    doc.close()


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    up = tmp_path / "uploads"
    out = tmp_path / "outputs"
    up.mkdir()
    out.mkdir()
    monkeypatch.setattr(settings, "AUTH_ENABLED", False)  # require_auth → anonymous
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(up))
    monkeypatch.setattr(settings, "OUTPUT_DIR", str(out))
    fms.file_store.clear()
    yield


def _register_with_output(
    original_pages: int, output_pages: int
) -> tuple[str, str, str]:
    """原文 N 页、成品 M 页（N≠M 用于证明页数取自成品而非原卷）。"""
    original = os.path.join(settings.UPLOAD_DIR, "case.pdf")
    output = os.path.join(settings.OUTPUT_DIR, "case-redacted.pdf")
    _make_pdf(original, original_pages, "原文")
    _make_pdf(output, output_pages, "化名成品")
    file_id = "case_83"
    fms.file_store[file_id] = {
        "file_id": file_id,
        "file_path": original,
        "file_type": "pdf",
        "original_filename": "case.pdf",
        "owner_id": "anonymous",
        "output_path": output,
    }
    return file_id, original, output


class TestOutputPageImage:
    def test_redacted_returns_png_with_output_page_count(self):
        # 原文 1 页、成品 3 页：头必须是 3（成品口径），不是原卷的 1
        file_id, _original, _output = _register_with_output(original_pages=1, output_pages=3)
        resp = client.get(f"/api/v1/files/{file_id}/page-image?page=1&redacted=true")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "image/png"
        assert resp.headers["x-page-count"] == "3"
        assert resp.content[:8] == b"\x89PNG\r\n\x1a\n"

    def test_original_branch_also_exposes_page_count(self):
        file_id, _original, _output = _register_with_output(original_pages=2, output_pages=1)
        resp = client.get(f"/api/v1/files/{file_id}/page-image?page=2&redacted=false")
        assert resp.status_code == 200
        assert resp.headers["x-page-count"] == "2"

    def test_redacted_page_out_of_range_400_not_500(self):
        file_id, _original, _output = _register_with_output(original_pages=1, output_pages=3)
        resp = client.get(f"/api/v1/files/{file_id}/page-image?page=999&redacted=true")
        assert resp.status_code == 400
        assert "页码超出范围" in resp.json()["message"]

    def test_original_page_out_of_range_400_not_500(self):
        file_id, _original, _output = _register_with_output(original_pages=2, output_pages=1)
        resp = client.get(f"/api/v1/files/{file_id}/page-image?page=99&redacted=false")
        assert resp.status_code == 400

    def test_redacted_before_execute_400(self):
        _make_pdf(os.path.join(settings.UPLOAD_DIR, "fresh.pdf"), 1)
        file_id = "fresh_83"
        fms.file_store[file_id] = {
            "file_id": file_id,
            "file_path": os.path.join(settings.UPLOAD_DIR, "fresh.pdf"),
            "file_type": "pdf",
            "original_filename": "fresh.pdf",
            "owner_id": "anonymous",
        }
        resp = client.get(f"/api/v1/files/{file_id}/page-image?page=1&redacted=true")
        assert resp.status_code == 400
        assert "尚未匿名化" in resp.json()["message"]

    def test_redacted_missing_file_404(self):
        _make_pdf(os.path.join(settings.UPLOAD_DIR, "ghost.pdf"), 1)
        file_id = "ghost_83"
        fms.file_store[file_id] = {
            "file_id": file_id,
            "file_path": os.path.join(settings.UPLOAD_DIR, "ghost.pdf"),
            "file_type": "pdf",
            "original_filename": "ghost.pdf",
            "owner_id": "anonymous",
            "output_path": os.path.join(settings.OUTPUT_DIR, "missing-output.pdf"),
        }
        resp = client.get(f"/api/v1/files/{file_id}/page-image?page=1&redacted=true")
        assert resp.status_code == 404


class TestGetPdfPageCount:
    def test_plain_pdf_count(self):
        # 解析器路径守卫只放行 UPLOAD_DIR/OUTPUT_DIR，夹具须落在其中
        path = os.path.join(settings.UPLOAD_DIR, "count.pdf")
        _make_pdf(path, 4)
        import asyncio

        assert asyncio.run(FileParser().get_pdf_page_count(path)) == 4

    def test_encrypted_pdf_raises_structured(self):
        # #30 路径保持：加密卷认证失败必须抛结构化错误，不得裸抛
        path = os.path.join(settings.UPLOAD_DIR, "locked.pdf")
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((72, 100), "机密", fontname="china-s")
        doc.save(path, encryption=fitz.PDF_ENCRYPT_AES_256, user_pw="pw123")
        doc.close()
        import asyncio

        with pytest.raises(PdfEncryptedError):
            asyncio.run(FileParser().get_pdf_page_count(path))
