"""Issue#244：水印工具 API（独立去水印小模块的用户面）。

三个端点（files.py）：`GET watermark-status`（只读检测报告）、
`POST watermark-strip`（执行摘除）、`GET watermark-clean`（净化件下载）。
夹具现场合成双保险水印卷（同一水印画两遍，#245 形态），不依赖 testdata。

红线：端点只读原件；归属校验（owner）；非 PDF 显式拒绝；未命中/摘不了给机读码。
"""

from __future__ import annotations

import fitz
import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import app
from app.services import file_management_service as fms

client = TestClient(app)

WM_TEXT = "COURT-ZHANGSAN-450102199001011234"  # ASCII：内容流 literal string 不经字体编码


def _dual_watermark_pdf(path: str, pages: int = 3) -> None:
    """合成双保险水印卷：复用 test_watermark_overlay_strip 的已验证夹具构建器。"""
    from test_watermark_overlay_strip import build_pdf

    build_pdf(path, pages=pages, wm_streams=2)


def _clean_pdf(path: str) -> None:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 100), "普通文本卷 无水印", fontsize=12, fontname="china-s")
    doc.save(path)
    doc.close()


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    up = tmp_path / "uploads"
    out = tmp_path / "outputs"
    up.mkdir()
    out.mkdir()
    monkeypatch.setattr(settings, "AUTH_ENABLED", False)
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(up))
    monkeypatch.setattr(settings, "OUTPUT_DIR", str(out))
    monkeypatch.setattr(settings, "WATERMARK_OVERLAY_STRIP_ENABLED", True)
    monkeypatch.setattr(settings, "WATERMARK_OVERLAY_COPY_TTL_HOURS", 24)
    fms.file_store.clear()
    yield
    fms.file_store.clear()


def _register(name: str, src_path: str) -> str:
    file_id = name.replace(".", "_")
    fms.file_store[file_id] = {
        "file_id": file_id,
        "file_path": src_path,
        "file_type": "pdf_scanned",
        "is_scanned": True,
        "original_filename": name,
        "owner_id": "anonymous",
        "page_count": 3,
    }
    return file_id


def test_status_reports_hit_for_dual_watermark(tmp_path):
    src = str(tmp_path / "dual.pdf")
    _dual_watermark_pdf(src)
    fid = _register("dual.pdf", src)
    r = client.get(f"/api/v1/files/{fid}/watermark-status")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["code"] == "hit"
    assert data["pages_hit"] == data["pages_probed"]
    # 报告不泄漏文件系统路径
    assert "copy_path" not in data and str(tmp_path) not in r.text


def test_status_reports_no_wm_for_clean_volume(tmp_path):
    src = str(tmp_path / "clean.pdf")
    _clean_pdf(src)
    fid = _register("clean.pdf", src)
    r = client.get(f"/api/v1/files/{fid}/watermark-status")
    assert r.status_code == 200
    assert r.json()["code"] in {"no_wm_spans", "no_isolated_overlay", "below_min_pages"}


def test_status_rejects_missing_and_foreign(tmp_path):
    src = str(tmp_path / "x.pdf")
    _clean_pdf(src)
    fid = _register("x.pdf", src)
    assert client.get("/api/v1/files/ghost/watermark-status").status_code == 404
    # 归属校验：owner 不同 ⇒ 404（不泄漏存在性）。file_store 是 sqlite 后端对象，
    # 直接改取出 dict 的字段不会落库——必须整条重写。
    fms.file_store[fid] = {**dict(fms.file_store[fid]), "owner_id": "someone-else"}
    assert client.get(f"/api/v1/files/{fid}/watermark-status").status_code == 404


def test_strip_produces_report_and_clean_download(tmp_path):
    src = str(tmp_path / "dual.pdf")
    _dual_watermark_pdf(src)
    fid = _register("dual.pdf", src)
    r = client.post(f"/api/v1/files/{fid}/watermark-strip")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["code"] == "hit"  # 状态面 code=探针码；stripped 体现在 pages_stripped
    assert data["pages_stripped"] == 3
    assert data["clean_available"] is True

    r2 = client.get(f"/api/v1/files/{fid}/watermark-clean")
    assert r2.status_code == 200
    assert r2.headers["content-type"].startswith("application/pdf")
    body = r2.content
    # 净化件不含水印令牌（memoryview 安全读）
    doc = fitz.open(stream=body, filetype="pdf")
    try:
        text = "".join(p.get_text() for p in doc)
        assert "450102199001011234" not in text
        assert "ZHANGSAN" not in text
        assert len(doc) == 3
    finally:
        doc.close()


def test_strip_clean_download_400_when_no_copy(tmp_path):
    src = str(tmp_path / "clean.pdf")
    _clean_pdf(src)
    fid = _register("clean.pdf", src)
    r = client.get(f"/api/v1/files/{fid}/watermark-clean")
    assert r.status_code == 400
    body = r.json()
    msg = str(body.get("message") or body.get("detail") or "")
    assert "code=" in msg or "净化" in msg, body


def test_tool_rejects_non_pdf(tmp_path):
    src = str(tmp_path / "note.txt")
    with open(src, "w", encoding="utf-8") as fh:
        fh.write("plain")
    fid = _register("note.txt", src)
    fms.file_store[fid] = {**dict(fms.file_store[fid]), "file_type": "txt"}  # sqlite 后端：整条重写才落库
    assert client.get(f"/api/v1/files/{fid}/watermark-status").status_code in {400, 422}
    assert client.post(f"/api/v1/files/{fid}/watermark-strip").status_code in {400, 422}
