"""还原工具 API/CLI 测试(Issue#70 T05/T06/T08):鉴权、校验、一致性。"""

import json
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import app

client = TestClient(app)

MAPPING = {"[PERSON_1]": {"type": "PERSON", "texts": ["张三"]}, "某公司1": {"texts": ["某科技有限公司"]}}


@pytest.fixture(autouse=True)
def _auth(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_ENABLED", False)  # 匿名放行测业务;鉴权开关系由 T08 单测覆盖


def test_restore_api_ok():
    resp = client.post("/api/v1/vlmd/restore", json={
        "text": "[PERSON_1]任职于某公司1。",
        "mapping": MAPPING,
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["restored_text"] == "张三任职于某科技有限公司。"
    assert data["restored_count"] == 2
    assert data["unknown"] == []


def test_restore_api_bad_policy_400():
    resp = client.post("/api/v1/vlmd/restore", json={
        "text": "x", "mapping": MAPPING, "policy": "guess",
    })
    assert resp.status_code == 400


def test_restore_api_missing_fields_422():
    assert client.post("/api/v1/vlmd/restore", json={"mapping": MAPPING}).status_code == 422
    assert client.post("/api/v1/vlmd/restore", json={"text": "x"}).status_code == 422


def test_restore_api_empty_text_and_plain():
    r0 = client.post("/api/v1/vlmd/restore", json={"text": "", "mapping": MAPPING})
    assert r0.status_code == 200 and r0.json()["restored_text"] == ""
    r1 = client.post("/api/v1/vlmd/restore", json={"text": "普通文本。", "mapping": MAPPING})
    assert r1.status_code == 200
    d = r1.json()
    assert d["restored_count"] == 0 and d["ambiguous"] == [] and d["unknown"] == []


def test_restore_api_parse_warnings_returned():
    resp = client.post("/api/v1/vlmd/restore", json={
        "text": "[PERSON_1]。", "mapping": {"[BAD_1]": {}, "[PERSON_1]": {"texts": ["张三"]}},
    })
    assert resp.status_code == 200
    assert len(resp.json()["parse_warnings"]) == 1


def test_restore_cli_matches_api(tmp_path):
    md = tmp_path / "in.md"
    mp = tmp_path / "mapping.json"
    md.write_text("[PERSON_1]任职于某公司1。", encoding="utf-8")
    mp.write_text(json.dumps(MAPPING, ensure_ascii=False), encoding="utf-8")

    api = client.post("/api/v1/vlmd/restore", json={"text": md.read_text(encoding="utf-8"), "mapping": MAPPING})
    out = subprocess.run(
        [sys.executable, "scripts/restore_md.py", str(md), "--mapping", str(mp)],
        capture_output=True, text=True, check=True,
        cwd=".",
    )
    assert out.stdout.strip() == api.json()["restored_text"]


def test_restore_cli_report(tmp_path):
    md = tmp_path / "in.md"
    mp = tmp_path / "mapping.json"
    md.write_text("[PERSON_1]与[PERSON_999]。", encoding="utf-8")
    mp.write_text(json.dumps(MAPPING, ensure_ascii=False), encoding="utf-8")
    report = tmp_path / "report.json"
    out = subprocess.run(
        [sys.executable, "scripts/restore_md.py", str(md), "--mapping", str(mp),
         "--report", str(report)],
        capture_output=True, text=True, check=True, cwd=".",
    )
    rep = json.loads(report.read_text(encoding="utf-8"))
    assert rep["unknown"] == ["[PERSON_999]"]
    assert rep["restored_count"] == 1


def test_restore_requires_auth_when_enabled(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_ENABLED", True)
    resp = client.post("/api/v1/vlmd/restore", json={"text": "x", "mapping": MAPPING})
    assert resp.status_code in (401, 403), resp.status_code
