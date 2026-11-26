"""文档中心服务：frontmatter、链接/反链、缓存热更新、lint 红绿。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.services import docs_service


def _doc(
    title: str,
    *,
    category: str = "项目总览",
    section: str = "",
    order: int = 1,
    status: str = "canonical",
    summary: str = "",
    canonical: str | None = None,
    canonical_for: list[str] | None = None,
    extra: str = "",
) -> str:
    lines = [
        "---",
        f"title: {title}",
        f"category: {category}",
    ]
    if section:
        lines.append(f"section: {section}")
    lines.extend(
        [
            f"order: {order}",
            "updated: 2026-11-26",
            f"status: {status}",
        ]
    )
    if summary:
        lines.append(f"summary: {summary}")
    if canonical:
        lines.append(f"canonical: {canonical}")
    if canonical_for:
        lines.append("canonical_for:")
        lines.extend(f"  - {fact}" for fact in canonical_for)
    lines.extend(["---", "", extra])
    return "\n".join(lines)


def _write(root: Path, name: str, content: str) -> Path:
    path = root / f"{name}.md"
    path.write_text(content, encoding="utf-8")
    return path


@pytest.fixture
def docs_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "docs"
    root.mkdir()
    monkeypatch.setattr(docs_service.settings, "DOCS_ROOT", str(root))
    docs_service.reset_cache()
    yield root
    docs_service.reset_cache()


def test_list_detail_and_backlinks(docs_root: Path) -> None:
    _write(
        docs_root,
        "overview",
        _doc("总览", status="index", order=0, extra="见 [[alpha|甲篇]] 与 [[beta]]"),
    )
    _write(docs_root, "alpha", _doc("甲篇", order=1, extra="正文见 [[beta|乙篇]]"))
    _write(docs_root, "beta", _doc("乙篇", order=2, extra="乙篇正文"))

    listing = docs_service.list_docs()
    assert listing["index"] == "overview"
    assert [s["category"] for s in listing["sections"]] == ["项目总览"]
    assert [i["slug"] for i in listing["sections"][0]["items"]] == [
        "overview",
        "alpha",
        "beta",
    ]

    alpha = docs_service.get_doc("alpha")
    assert alpha is not None
    assert "[乙篇](/docs/beta)" in alpha["content"]
    assert alpha["links"] == [{"slug": "beta", "title": "乙篇"}]

    beta = docs_service.get_doc("beta")
    assert beta is not None
    assert {link["slug"] for link in beta["backlinks"]} == {"overview", "alpha"}


def test_links_in_code_spans_and_fences_are_not_resolved(docs_root: Path) -> None:
    _write(docs_root, "beta", _doc("乙篇"))
    _write(
        docs_root,
        "alpha",
        _doc(
            "甲篇",
            extra="行内示例 `[[beta]]`\n\n```\n[[beta]]\n```\n\n真实链接 [[beta]]",
        ),
    )

    alpha = docs_service.get_doc("alpha")
    assert alpha is not None
    # 代码里的两个保持原文，只有行尾那个被解析
    assert alpha["content"].count("[[beta]]") == 2
    assert alpha["content"].count("[乙篇](/docs/beta)") == 1
    assert alpha["links"] == [{"slug": "beta", "title": "乙篇"}]


def test_summary_requires_canonical(docs_root: Path) -> None:
    _write(docs_root, "overview", _doc("总览", status="index", order=0))
    _write(docs_root, "bad", _doc("坏摘要", status="summary"))

    problems = docs_service.lint(docs_root)
    assert any("status=summary 必须提供 canonical" in p for p in problems)


def test_duplicate_canonical_for_fails_lint(docs_root: Path) -> None:
    _write(docs_root, "index", _doc("索引", status="index", order=0))
    _write(docs_root, "a", _doc("A", canonical_for=["definition"]))
    _write(docs_root, "b", _doc("B", canonical_for=["definition"]))

    problems = docs_service.lint(docs_root)
    assert any("事实键重复：definition" in p for p in problems)


def test_duplicate_sentence_fails_lint(docs_root: Path) -> None:
    sentence = "这是一段长度超过四十个字符的重复句子，用来验证跨文档完全重复检测能够生效，请务必超过四十个字符。"
    _write(docs_root, "index", _doc("索引", status="index", order=0))
    _write(docs_root, "a", _doc("A", extra=sentence))
    _write(docs_root, "b", _doc("B", extra=sentence))

    problems = docs_service.lint(docs_root)
    assert any("跨文档重复句子" in p for p in problems)


def test_broken_link_fails_lint(docs_root: Path) -> None:
    _write(docs_root, "index", _doc("索引", status="index", order=0, extra="[[ghost]]"))

    problems = docs_service.lint(docs_root)
    assert any("断链：index -> [[ghost]]" in p for p in problems)


def test_store_reloads_on_file_change(docs_root: Path) -> None:
    path = _write(docs_root, "index", _doc("索引", status="index", order=0, extra="第一版"))
    assert "第一版" in (docs_service.get_doc("index") or {})["content"]

    path.write_text(_doc("索引", status="index", order=0, extra="第二版"), encoding="utf-8")
    os.utime(path, None)

    assert "第二版" in (docs_service.get_doc("index") or {})["content"]


def test_leading_h1_matching_title_is_stripped(docs_root: Path) -> None:
    _write(docs_root, "index", _doc("索引", status="index", order=0))
    _write(docs_root, "alpha", _doc("甲篇", extra="# 甲篇\n\n正文第一段。"))

    alpha = docs_service.get_doc("alpha")
    assert alpha is not None
    # 页面用 frontmatter title 渲染 h1；正文同标题 H1 必须剥掉，避免双标题。
    assert "# 甲篇" not in alpha["content"]
    assert "正文第一段。" in alpha["content"]


def test_list_exposes_section_for_nav_grouping(docs_root: Path) -> None:
    _write(docs_root, "index", _doc("索引", status="index", order=0))
    _write(docs_root, "alpha", _doc("甲篇", section="第一章 · 认识", order=1))
    _write(docs_root, "beta", _doc("乙篇", section="第二章 · 能力", order=2))

    items = docs_service.list_docs()["sections"][0]["items"]
    assert [item["section"] for item in items] == ["", "第一章 · 认识", "第二章 · 能力"]


def test_invalid_filename_is_reported(docs_root: Path) -> None:
    _write(docs_root, "overview", _doc("总览", status="index", order=0))
    (docs_root / "中文名.md").write_text(_doc("非法"), encoding="utf-8")

    problems = docs_service.lint(docs_root)
    assert any("文件名不是合法 slug" in p for p in problems)