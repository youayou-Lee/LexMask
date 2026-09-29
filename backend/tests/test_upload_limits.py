"""上传限制语义回归锚（PR #11 评审修复）：0=不限制、可配上限、负数配置启动即拒。

覆盖独立评审指出的两个边界：
1. DICOM _save_upload 的 remaining 哨兵——None=不限、0=预算恰好用尽（必须 413 而非绕过）、
   正数=剩余预算（边界值恰好相等应放行）；
2. MAX_FILE_SIZE 负数在配置加载时即 ValidationError，杜绝「同一变量在不同路径语义相反」。
"""
import asyncio
import hashlib

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api.dicom import DicomWorkflowError, _save_upload
from app.core.config import Settings, settings
from app.main import app

client = TestClient(app)


class _FakeUpload:
    """按固定块大小吐字节的 UploadFile 替身（_save_upload 只依赖 .read）。"""

    def __init__(self, payload: bytes, chunk_size: int = 4):
        self._payload = payload
        self._chunk_size = chunk_size
        self._offset = 0

    async def read(self, size: int) -> bytes:
        data = self._payload[self._offset:self._offset + size]
        self._offset += len(data)
        return data


def _save(payload: bytes, remaining, tmp_path):
    dest = tmp_path / "upload.bin"
    return asyncio.run(_save_upload(_FakeUpload(payload), str(dest), remaining))


def test_save_upload_none_means_unlimited(tmp_path):
    size, digest = _save(b"A" * 10, None, tmp_path)
    assert size == 10 and digest == hashlib.sha256(b"A" * 10).hexdigest()


def test_save_upload_zero_budget_is_hard_limit(tmp_path):
    # 评审指出的边界：预算恰好用尽（remaining=0）必须拒绝，不得被当成「不限」绕过
    with pytest.raises(DicomWorkflowError) as exc:
        _save(b"A" * 10, 0, tmp_path)
    assert exc.value.status_code == 413


def test_save_upload_exact_budget_passes(tmp_path):
    size, _ = _save(b"A" * 10, 10, tmp_path)
    assert size == 10


def test_save_upload_over_budget_rejected(tmp_path):
    with pytest.raises(DicomWorkflowError) as exc:
        _save(b"A" * 11, 10, tmp_path)
    assert exc.value.status_code == 413


def test_negative_max_file_size_rejected_at_startup():
    with pytest.raises(ValidationError):
        Settings(MAX_FILE_SIZE=-1)


def test_zero_and_positive_max_file_size_accepted():
    assert Settings(MAX_FILE_SIZE=0).MAX_FILE_SIZE == 0
    assert Settings(MAX_FILE_SIZE=52428800).MAX_FILE_SIZE == 52428800


# ---------------------------------------------------------------------------
# MaxBodySizeMiddleware：非 JSON 上限跟随 MAX_FILE_SIZE（0=不拦截），JSON 恒 1MB
# （Issue #27：旧实现硬编码 60MB，与 MAX_FILE_SIZE 脱钩，100MB 整包上传秒 413）
# ---------------------------------------------------------------------------
@pytest.fixture()
def _mw_env(monkeypatch, tmp_path):
    from app.main import MaxBodySizeMiddleware

    monkeypatch.setattr(settings, "AUTH_ENABLED", False)
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path))
    # 把 multipart 开销调小，便于用小请求体触发上限分支
    monkeypatch.setattr(MaxBodySizeMiddleware, "MULTIPART_OVERHEAD_BYTES", 4)
    return MaxBodySizeMiddleware


def _init_session(size=10):
    return client.post(
        "/api/v1/files/upload/resumable/init",
        json={"filename": "mw.txt", "file_size": size},
    )


def _chunk(upload_id, payload: bytes):
    return client.put(
        f"/api/v1/files/upload/resumable/{upload_id}/chunk",
        params={"offset": 0},
        content=payload,
        headers={"Content-Type": "application/octet-stream"},
    )


def test_middleware_unlimited_when_max_file_size_zero(_mw_env, monkeypatch):
    monkeypatch.setattr(settings, "MAX_FILE_SIZE", 0)
    assert _init_session(size=20).status_code == 200
    upload_id = _init_session(size=20).json()["upload_id"]
    r = _chunk(upload_id, b"A" * 20)
    assert r.status_code == 200 and r.json()["received_bytes"] == 20


def test_middleware_rejects_over_configured_cap(_mw_env, monkeypatch):
    monkeypatch.setattr(settings, "MAX_FILE_SIZE", 10)  # 生效上限 = 10 + 4 = 14
    upload_id = _init_session(size=10).json()["upload_id"]
    r = _chunk(upload_id, b"A" * 15)
    assert r.status_code == 413 and r.json()["error_code"] == "BODY_TOO_LARGE"
    # 上限内的请求正常通过（10 字节 ≤ 声明尺寸 10，也 ≤ 中间件上限 14）
    upload_id2 = _init_session(size=10).json()["upload_id"]
    r2 = _chunk(upload_id2, b"A" * 10)
    assert r2.status_code == 200 and r2.json()["received_bytes"] == 10


def test_middleware_json_cap_independent_of_max_file_size(_mw_env, monkeypatch):
    monkeypatch.setattr(settings, "MAX_FILE_SIZE", 0)
    big_name = "x" * (1 * 1024 * 1024 + 1024)
    r = client.post(
        "/api/v1/files/upload/resumable/init",
        json={"filename": big_name, "file_size": 1},
    )
    assert r.status_code == 413 and r.json()["error_code"] == "BODY_TOO_LARGE"
