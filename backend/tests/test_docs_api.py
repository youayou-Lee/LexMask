"""文档中心 HTTP 契约：清单 / 详情 / 404 / 非法 slug。"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import app
from app.services import docs_service

client = TestClient(app)


@pytest.fixture(autouse=True)
def _env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "docs"
    root.mkdir()
    (root / "overview.md").write_text(
        "---\ntitle: 总览\ncategory: 项目总览\norder: 0\nupdated: 2026-11-26\n"
        "status: index\nsummary: 总纲\n---\n\n见 [[alpha|甲篇]]。\n",
        encoding="utf-8",
    )
    (root / "alpha.md").write_text(
        "---\ntitle: 甲篇\ncategory: 项目总览\norder: 1\nupdated: 2026-11-26\n"
        "status: canonical\ncanonical_for:\n  - definition\n---\n\n正文 [[overview|总览]]。\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "AUTH_ENABLED", False)
    monkeypatch.setattr(settings, "DOCS_ROOT", str(root))
    docs_service.reset_cache()
    yield
    docs_service.reset_cache()


def test_list_docs_returns_tree_and_index() -> None:
    response = client.get("/api/v1/docs")
    assert response.status_code == 200
    payload = response.json()
    assert payload["index"] == "overview"
    assert payload["sections"][0]["category"] == "项目总览"
    assert [item["slug"] for item in payload["sections"][0]["items"]] == ["overview", "alpha"]


def test_get_doc_returns_resolved_content_and_backlinks() -> None:
    response = client.get("/api/v1/docs/overview")
    assert response.status_code == 200
    payload = response.json()
    assert "[甲篇](/docs/alpha)" in payload["content"]
    assert payload["links"] == [{"slug": "alpha", "title": "甲篇"}]
    assert payload["backlinks"] == [{"slug": "alpha", "title": "甲篇"}]
    assert payload["status"] == "index"


def test_unknown_slug_is_404() -> None:
    assert client.get("/api/v1/docs/ghost").status_code == 404


def test_invalid_slug_is_404() -> None:
    # 路径穿越 / 大写 / 下划线都不进入服务层
    for slug in ("..", "A_b", "中文"):
        assert client.get(f"/api/v1/docs/{slug}").status_code == 404