"""密码规则：仅 6-16 位长度校验，不做大小写/数字/符号复杂度要求。

钉两层：`validate_password_strength` 纯函数 + `/auth/setup`、
`/auth/change-password` 两个端点的实际行为（弱密码可设、边界长度拒绝）。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api import auth as auth_api
from app.core import auth as auth_core
from app.core.auth import validate_password_strength
from app.core.config import settings
from app.core.rate_limit import RateLimiter
from app.main import app

client = TestClient(app)


@pytest.fixture
def _fresh_auth(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "AUTH_ENABLED", True)
    monkeypatch.setattr(auth_core, "_AUTH_FILE", str(tmp_path / "auth.json"))
    # 模块级限流器跨用例残留计数（负例用例会吃掉配额），按 test_ldap_auth 惯例换大额度
    monkeypatch.setattr(auth_api, "_auth_limiter", RateLimiter(max_requests=10_000, window_seconds=60))
    yield tmp_path


# ─── (1) 纯函数：6-16 位通过，越界拒绝，复杂度不再要求 ───
@pytest.mark.parametrize(
    "password,expect_ok",
    [
        ("123456", True),  # 纯数字弱密码
        ("abcdef", True),  # 纯小写
        ("ABCDEF", True),  # 纯大写
        ("aB1!x9", True),  # 全复杂度
        ("a" * 6, True),  # 下边界
        ("a" * 16, True),  # 上边界
        ("a" * 5, False),  # 少于 6
        ("a" * 17, False),  # 超过 16
        ("", False),
    ],
)
def test_validate_password_strength_length_only(password: str, expect_ok: bool):
    errors = validate_password_strength(password)
    if expect_ok:
        assert errors == []
    else:
        assert errors == ["Password must be 6-16 characters long."]


def test_setup_accepts_weak_password(_fresh_auth):
    resp = client.post("/api/v1/auth/setup", json={"password": "123456"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["access_token"]
    # 落库后可登录
    login = client.post("/api/v1/auth/login", json={"password": "123456"})
    assert login.status_code == 200, login.text


@pytest.mark.parametrize("bad", ["12345", "a" * 17])
def test_setup_rejects_length_out_of_bounds(_fresh_auth, bad):
    resp = client.post("/api/v1/auth/setup", json={"password": bad})
    assert resp.status_code == 400, resp.text
    assert "6-16" in resp.json()["message"]
    # 拒绝后不得留下任何已设状态
    assert auth_core.is_password_set() is False


def test_change_password_accepts_weak_new_password(_fresh_auth):
    setup = client.post("/api/v1/auth/setup", json={"password": "abcdef"})
    assert setup.status_code == 200, setup.text
    headers = {"Authorization": f"Bearer {setup.json()['access_token']}"}
    resp = client.post(
        "/api/v1/auth/change-password",
        json={"old_password": "abcdef", "new_password": "654321"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert auth_core.check_password("654321", username="local_user") == "local_user"
