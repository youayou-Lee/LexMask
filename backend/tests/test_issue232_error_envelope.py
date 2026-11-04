"""Issue #232：拒收类错误信封机读化。

#229 把伪造扩展名拒收文案改成了「说清实际格式 + 可操作指引」，但信封里只有
中文自由文本——en locale 前端按既有决策只能显示通用兜底。本文件守住三件事：

1. I-1：拒收信封带机读 ``error_code=FILE_CONTENT_MISMATCH`` 与结构化
   ``detail``（``detected_format``/``extension``），前端按码出本地化文案；
2. 三条拒收路径同形：单文件上传、断点续传 complete、批量入库逐件明细；
3. ``detect_format_code`` 的机读格式码表（文案与码解耦的锚）。

中文 message 逐字保留（zh 可见性不回退），本文件只加机器可读字段。
"""

import os
import tempfile

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.file_validation import detect_format_code
from app.main import app

PDF_MAGIC = b"%PDF-1.7\nrest"
OLE_DOC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 20
UTF8_TEXT = "被告赵涛洋，身份证号110118197712242590。".encode()

client = TestClient(app)


@pytest.fixture(autouse=True)
def _scoped(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "AUTH_ENABLED", False)
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "IMPORT_INBOX_DIR", str(tmp_path / "inbox"))
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path / "data"))
    yield


def _upload(payload: bytes, filename: str):
    return client.post(
        "/api/v1/files/upload",
        files={"file": (filename, payload, "application/octet-stream")},
    )


# ---------------------------------------------------------------------------
# detect_format_code：机读格式码（i18n 文案键的原料）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (b"%PDF-1.7\nrest", "pdf"),
        (b"PK\x03\x04" + b"\x00" * 8, "zip"),
        (OLE_DOC, "ole"),
        (b"{\\rtf1", "rtf"),
        (b"\xff\xd8\xff\xe0", "jpeg"),
        (b"\x89PNG\r\n\x1a\n", "png"),
        (b"GIF89a", "gif"),
        (b"BM\x00\x00", "bmp"),
        (b"RIFF\x00\x00webp", "webp"),
        (b"II\x2a\x00\x00\x00", "tiff"),
        (UTF8_TEXT, "text"),
        (b"\x00\x01\x02\x03\x04\x05\x06\x07", "binary"),
    ],
)
def test_detect_format_code(payload, expected):
    path = os.path.join(tempfile.mkdtemp(), "probe.bin")
    with open(path, "wb") as f:
        f.write(payload)
    assert detect_format_code(path) == expected


def test_detect_format_code_unreadable(tmp_path):
    assert detect_format_code(str(tmp_path / "missing.bin")) == "unknown"


# ---------------------------------------------------------------------------
# 单文件上传：400 信封带码 + 结构化 detail
# ---------------------------------------------------------------------------


def test_upload_spoof_envelope_carries_error_code_and_detail():
    r = _upload(PDF_MAGIC, "x.doc")
    assert r.status_code == 400
    body = r.json()
    assert body["error_code"] == "FILE_CONTENT_MISMATCH"
    assert body["detail"]["detected_format"] == "pdf"
    assert body["detail"]["extension"] == ".doc"
    # zh 文案逐字保留在 message（可见性不回退）——门⑥ M-3：钉全字面量，
    # 路由层未来对 message 做截断/拼接等改写时本测试必须红。
    assert body["message"] == (
        "文件内容实际是PDF 文档，与扩展名 .doc 不匹配——"
        "文件可能在传输中损坏或被重命名过；请用原始文件重新导出/另存为后再上传"
    )


def test_upload_spoof_text_under_doc_extension():
    r = _upload(UTF8_TEXT, "x.doc")
    assert r.status_code == 400
    body = r.json()
    assert body["error_code"] == "FILE_CONTENT_MISMATCH"
    assert body["detail"]["detected_format"] == "text"
    assert body["detail"]["extension"] == ".doc"


def test_upload_spoof_error_code_matches_exception_constant():
    # 前后端契约锚：码值必须与 file_validation 暴露的常量逐字节一致
    from app.core.file_validation import FILE_CONTENT_MISMATCH_ERROR_CODE

    r = _upload(PDF_MAGIC, "x.doc")
    assert r.json()["error_code"] == FILE_CONTENT_MISMATCH_ERROR_CODE == "FILE_CONTENT_MISMATCH"


# ---------------------------------------------------------------------------
# 断点续传 complete：同一 process_upload 拒收，信封同形
# ---------------------------------------------------------------------------


def test_upload_resumable_spoof_envelope_carries_code():
    init = client.post(
        "/api/v1/files/upload/resumable/init",
        json={"filename": "x.doc", "file_size": len(PDF_MAGIC)},
    )
    assert init.status_code == 200
    upload_id = init.json()["upload_id"]
    chunk = client.put(
        f"/api/v1/files/upload/resumable/{upload_id}/chunk",
        params={"offset": 0},
        content=PDF_MAGIC,
        headers={"Content-Type": "application/octet-stream"},
    )
    assert chunk.status_code == 200
    done = client.post(f"/api/v1/files/upload/resumable/{upload_id}/complete")
    assert done.status_code == 400
    body = done.json()
    assert body["error_code"] == "FILE_CONTENT_MISMATCH"
    assert body["detail"]["detected_format"] == "pdf"
    assert body["detail"]["extension"] == ".doc"


# ---------------------------------------------------------------------------
# 批量入库（import_inbox）：逐件明细带同一机读码
# ---------------------------------------------------------------------------


def test_import_inbox_spoof_item_carries_error_code():
    import asyncio

    from app.services import file_management_service as fms

    inbox = fms.import_inbox_dir("tester")
    with open(os.path.join(inbox, "fake.doc"), "wb") as f:
        f.write(PDF_MAGIC)
    report = asyncio.run(fms.import_inbox_files("tester", ["fake.doc"]))
    assert report["imported"] == []
    assert len(report["failed"]) == 1
    item = report["failed"][0]
    assert item["error_code"] == "FILE_CONTENT_MISMATCH"
    assert item["detected_format"] == "pdf"
    # reason 文案不变（zh 可见性不回退）
    assert "PDF" in item["reason"]
