"""文档中心：Markdown 知识库（索引 / 原子文档 / 双向链接）。

设计见 `docs/superpowers/specs/2026-11-26-docs-backend-knowledge-base.md`。

要点：
- 文件名（去扩展名）= slug；正文用 `[[slug|标题]]` 链接；
- 后端解析前向链接并自动生成反链；
- 链接解析跳过围栏代码块与行内代码（文档里可以举例）；
- 目录内容按 mtime 缓存，改文件即生效，不需要重启或重建前端。
"""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core.config import settings

logger = logging.getLogger(__name__)

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
WIKILINK_RE = re.compile(r"\[\[([a-z0-9][a-z0-9-]*)(?:\|([^\]\n]*))?\]\]")
FENCE_RE = re.compile(r"^\s*(```|~~~)")
INLINE_CODE_RE = re.compile(r"(`[^`\n]*`)")
FRONTMATTER_RE = re.compile(r"^---\r?\n(.*?)\r?\n---(?:\r?\n|$)", re.S)

CATEGORY_ORDER = [
    "项目总览",
    "新手必读",
    "操作指南",
    "配置与管理",
    "常见问题与排障",
    "更新日志",
]
_VALID_STATUS = {"index", "canonical", "summary", "draft", "deprecated"}
_MIN_DUP_SENTENCE_LEN = 40


class DocsError(RuntimeError):
    """单篇文档的解析/配置错误（运行时跳过，lint 判红）。"""


@dataclass
class DocsDoc:
    slug: str
    title: str
    category: str
    order: int
    updated: str
    status: str
    summary: str
    canonical: str | None
    canonical_for: list[str]
    body: str
    path: Path


@dataclass
class DocsStore:
    root: Path
    mtime_ns: int
    docs: dict[str, DocsDoc]
    resolved: dict[str, str]
    forward: dict[str, list[str]]
    backlinks: dict[str, list[str]]
    missing_links: list[tuple[str, str]]
    errors: list[str]
    index_slug: str | None

    def category_rank(self, category: str) -> tuple[int, str]:
        if category in CATEGORY_ORDER:
            return (CATEGORY_ORDER.index(category), category)
        return (len(CATEGORY_ORDER), category)


def docs_root() -> Path:
    return Path(settings.DOCS_ROOT).expanduser().resolve()


# ─── Frontmatter ─────────────────────────────────────────────


def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """解析极简 frontmatter（title/category/order/updated/status/summary/...）。

    只支持本仓文档用到的 YAML 子集：`key: scalar`、`key: [a, b]`、`key:` 后跟 `- item`
    列表。不引入 PyYAML 依赖。
    """
    if text.startswith("\ufeff"):
        text = text[1:]
    if not text.startswith("---"):
        return {}, text
    match = FRONTMATTER_RE.match(text)
    if not match:
        return {}, text

    fields: dict[str, Any] = {}
    list_key: str | None = None
    for raw in match.group(1).splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if line[:1].isspace() and list_key:
            if stripped.startswith("- "):
                fields.setdefault(list_key, []).append(_scalar(stripped[2:].strip()))
            continue
        if ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        key = key.strip()
        value = value.strip()
        list_key = None
        if not value:
            fields[key] = []
            list_key = key
        elif value.startswith("[") and value.endswith("]"):
            inner = value[1:-1].strip()
            fields[key] = [_scalar(part.strip()) for part in inner.split(",") if part.strip()]
        else:
            fields[key] = _scalar(value)
    return fields, text[match.end():]


def _scalar(raw: str) -> Any:
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    return value


def _as_str(fields: dict[str, Any], key: str, *, required: bool = False) -> str:
    raw = fields.get(key)
    if raw is None or isinstance(raw, list):
        if required:
            raise DocsError(f"缺少 frontmatter 字段: {key}")
        return ""
    value = str(raw).strip()
    if required and not value:
        raise DocsError(f"frontmatter 字段为空: {key}")
    return value


def _as_order(fields: dict[str, Any]) -> int:
    raw = fields.get("order")
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str) and re.fullmatch(r"-?\d+", raw.strip()):
        return int(raw.strip())
    raise DocsError("缺少或非法 frontmatter 字段: order")


# ─── 链接 ────────────────────────────────────────────────────


def _resolve_links(
    body: str, docs: dict[str, DocsDoc]
) -> tuple[str, list[str], list[str]]:
    """把 `[[slug|标题]]` 转成标准 Markdown 链接，返回 (正文, 目标列表, 缺失列表)。"""
    out: list[str] = []
    targets: list[str] = []
    missing: list[str] = []
    in_fence = False
    for line in body.splitlines(keepends=True):
        if FENCE_RE.match(line):
            in_fence = not in_fence
            out.append(line)
            continue
        if in_fence:
            out.append(line)
            continue
        parts = INLINE_CODE_RE.split(line)
        for index in range(0, len(parts), 2):
            parts[index] = WIKILINK_RE.sub(
                lambda m: _render_link(m, docs, targets, missing), parts[index]
            )
        out.append("".join(parts))
    return "".join(out), targets, missing


def _render_link(
    match: re.Match[str], docs: dict[str, DocsDoc], targets: list[str], missing: list[str]
) -> str:
    slug = match.group(1)
    doc = docs.get(slug)
    if doc is None:
        missing.append(slug)
        return match.group(0)
    label = (match.group(2) or doc.title).strip() or doc.title
    targets.append(slug)
    return f"[{label}](/docs/{slug})"


# ─── 构建与缓存 ──────────────────────────────────────────────


def _documents_mtime(root: Path) -> int:
    try:
        mtime = root.stat().st_mtime_ns
    except OSError:
        return 0
    for path in root.rglob("*.md"):
        try:
            mtime = max(mtime, path.stat().st_mtime_ns)
        except OSError:
            continue
    return mtime


def _build_store(root: Path, mtime_ns: int) -> DocsStore:
    docs: dict[str, DocsDoc] = {}
    errors: list[str] = []
    paths = sorted(root.rglob("*.md")) if root.is_dir() else []
    for path in paths:
        try:
            doc = _load_doc(path)
        except DocsError as exc:
            message = f"{path.relative_to(root) if root in path.parents else path}: {exc}"
            errors.append(message)
            logger.error("文档加载失败：%s", message)
            continue
        if doc.slug in docs:
            message = f"{doc.path}: slug 重复：{doc.slug}"
            errors.append(message)
            logger.error(message)
            continue
        docs[doc.slug] = doc

    resolved: dict[str, str] = {}
    forward: dict[str, list[str]] = {}
    missing_links: list[tuple[str, str]] = []
    for slug, doc in docs.items():
        body, targets, missing = _resolve_links(doc.body, docs)
        resolved[slug] = body
        forward[slug] = list(dict.fromkeys(targets))
        missing_links.extend((slug, target) for target in missing)

    backlinks: dict[str, list[str]] = {}
    for source, targets in forward.items():
        for target in targets:
            backlinks.setdefault(target, []).append(source)

    index_candidates = [
        doc for doc in docs.values() if doc.status == "index"
    ]
    index_slug = (
        sorted(index_candidates, key=lambda d: (d.order, d.slug))[0].slug
        if index_candidates
        else None
    )
    return DocsStore(
        root=root,
        mtime_ns=mtime_ns,
        docs=docs,
        resolved=resolved,
        forward=forward,
        backlinks=backlinks,
        missing_links=missing_links,
        errors=errors,
        index_slug=index_slug,
    )


def _load_doc(path: Path) -> DocsDoc:
    slug = path.stem
    if not SLUG_RE.fullmatch(slug):
        raise DocsError(f"文件名不是合法 slug（只允许小写字母/数字/连字符）：{path.name}")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise DocsError(f"读取失败：{exc}") from exc

    fields, body = split_frontmatter(text)
    title = _as_str(fields, "title", required=True)
    category = _as_str(fields, "category", required=True)
    order = _as_order(fields)
    status = _as_str(fields, "status") or "canonical"
    if status not in _VALID_STATUS:
        raise DocsError(f"非法 status：{status}")
    summary = _as_str(fields, "summary")
    canonical = _as_str(fields, "canonical") or None
    canonical_for = fields.get("canonical_for") or []
    if not isinstance(canonical_for, list):
        canonical_for = [str(canonical_for)]
    if status == "summary" and not canonical:
        raise DocsError("status=summary 必须提供 canonical")
    return DocsDoc(
        slug=slug,
        title=title,
        category=category,
        order=order,
        updated=_as_str(fields, "updated", required=True),
        status=status,
        summary=summary,
        canonical=canonical,
        canonical_for=[str(item) for item in canonical_for],
        body=body,
        path=path,
    )


_store: DocsStore | None = None
_store_lock = threading.Lock()


def get_store(force: bool = False) -> DocsStore:
    global _store
    root = docs_root()
    mtime_ns = _documents_mtime(root)
    with _store_lock:
        if (
            not force
            and _store is not None
            and _store.root == root
            and _store.mtime_ns == mtime_ns
        ):
            return _store
        _store = _build_store(root, mtime_ns)
        return _store


def reset_cache() -> None:
    global _store
    with _store_lock:
        _store = None


# ─── 对外查询 ────────────────────────────────────────────────


def list_docs() -> dict[str, Any]:
    store = get_store()
    grouped: dict[str, list[DocsDoc]] = {}
    for doc in store.docs.values():
        if doc.status == "deprecated":
            continue
        grouped.setdefault(doc.category, []).append(doc)

    sections = []
    for category in sorted(grouped, key=store.category_rank):
        items = sorted(grouped[category], key=lambda d: (d.order, d.slug))
        sections.append(
            {
                "category": category,
                "items": [
                    {
                        "slug": doc.slug,
                        "title": doc.title,
                        "summary": doc.summary,
                        "status": doc.status,
                        "order": doc.order,
                        "updated": doc.updated,
                    }
                    for doc in items
                ],
            }
        )
    return {"index": store.index_slug, "sections": sections}


def get_doc(slug: str) -> dict[str, Any] | None:
    store = get_store()
    doc = store.docs.get(slug)
    if doc is None:
        return None
    return {
        "slug": doc.slug,
        "title": doc.title,
        "category": doc.category,
        "status": doc.status,
        "updated": doc.updated,
        "summary": doc.summary,
        "content": store.resolved[slug],
        "links": [
            {"slug": target, "title": store.docs[target].title}
            for target in store.forward.get(slug, [])
        ],
        "backlinks": [
            {"slug": source, "title": store.docs[source].title}
            for source in store.backlinks.get(slug, [])
        ],
        "canonical": doc.canonical,
    }


# ─── Lint ────────────────────────────────────────────────────


def _sentences(body: str) -> Iterator[str]:
    text = re.sub(r"```.*?```", "", body, flags=re.S)
    text = re.sub(r"`[^`\n]*`", "", text)
    for raw in re.split(r"[。！？!?；;\n]+", text):
        sentence = re.sub(r"\s+", "", raw).strip(" \t-*#>|")
        if len(sentence) >= _MIN_DUP_SENTENCE_LEN:
            yield sentence


def lint(root: Path | None = None) -> list[str]:
    """返回问题列表；以 `ERROR:` 开头的是失败项，`WARN:` 为提示。"""
    root = root or docs_root()
    store = _build_store(root, 0)
    problems: list[str] = [f"ERROR: {message}" for message in store.errors]

    owners: dict[str, str] = {}
    for doc in store.docs.values():
        for fact in doc.canonical_for:
            if fact in owners:
                problems.append(
                    f"ERROR: 事实键重复：{fact}（{owners[fact]} / {doc.slug}）"
                )
            else:
                owners[fact] = doc.slug

    if store.index_slug is None:
        problems.append("ERROR: 缺少 status=index 的索引页")
    for source, target in store.missing_links:
        problems.append(f"ERROR: 断链：{source} -> [[{target}]]")

    for doc in store.docs.values():
        if doc.status == "summary":
            if not doc.canonical or doc.canonical not in store.docs:
                problems.append(f"ERROR: 摘要页 {doc.slug} 的 canonical 不存在")
            if len(doc.body.strip()) > 800:
                problems.append(f"ERROR: 摘要页 {doc.slug} 正文超过 800 字，应链回正本")

    duplicates: dict[str, list[str]] = {}
    for doc in store.docs.values():
        for sentence in _sentences(doc.body):
            duplicates.setdefault(sentence, []).append(doc.slug)
    for sentence, slugs in duplicates.items():
        unique = sorted(set(slugs))
        if len(unique) > 1:
            problems.append(
                f"ERROR: 跨文档重复句子（{'/'.join(unique)}）：{sentence[:60]}"
            )

    if store.index_slug and store.index_slug in store.docs:
        reachable = _reachable(store, store.index_slug)
        for doc in store.docs.values():
            if doc.status in {"deprecated", "index"}:
                continue
            if doc.slug not in reachable:
                problems.append(f"WARN: 索引不可达：{doc.slug}")
    return problems


def _reachable(store: DocsStore, start: str) -> set[str]:
    seen = {start}
    queue = [start]
    while queue:
        current = queue.pop()
        for target in store.forward.get(current, []):
            if target not in seen:
                seen.add(target)
                queue.append(target)
    return seen
