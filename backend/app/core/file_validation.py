"""
文件校验工具 — 魔术字节验证、扩展名检查、文件类型推断、路径安全校验。

从 services/file_management_service.py 和 services/file_service.py 合并提取，
作为唯一权威来源 (single source of truth)。
"""
from __future__ import annotations

import os

from app.core.config import settings
from app.models.schemas import FileType

# ---------------------------------------------------------------------------
# Magic-byte signatures → allowed extensions
# ---------------------------------------------------------------------------
MAGIC_BYTES: dict[bytes, set[str]] = {
    b"%PDF": {".pdf"},
    b"PK\x03\x04": {".docx", ".doc"},
    b"\xff\xd8\xff": {".jpg", ".jpeg"},
    b"\x89PNG": {".png"},
    b"GIF8": {".gif"},
    b"BM": {".bmp"},
    b"RIFF": {".webp"},
    b"\xd0\xcf\x11\xe0": {".doc", ".rtf"},
    b"II\x2a\x00": {".tif", ".tiff"},
    b"MM\x00\x2a": {".tif", ".tiff"},
    b"{\\rtf": {".rtf"},
}

# Text-like extensions that have no fixed magic bytes
TEXT_EXTENSIONS: frozenset[str] = frozenset({".txt", ".md", ".rtf", ".html", ".htm"})

# Image extensions are decoded by content downstream (PIL Image.open), never by
# extension. A mislabeled image — e.g. JPEG bytes saved as ``.webp`` — is safe:
# it is still a valid image the pipeline can read. So any image content is
# accepted under any image extension; non-image binaries stay rejected.
IMAGE_EXTENSIONS: frozenset[str] = frozenset(
    {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp", ".tif", ".tiff"}
)


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def validate_magic_bytes(file_path: str, ext: str) -> bool:
    """Validate file magic bytes match *ext*. Reject unknown binary signatures.

    Returns ``True`` when the file header matches the extension, or when the
    extension is a known text type (text files have no fixed magic bytes).
    """
    try:
        with open(file_path, "rb") as f:
            header = f.read(8)
        for magic, exts in MAGIC_BYTES.items():
            if header.startswith(magic):
                if ext in exts:
                    return True
                # Cross-format image mislabels (e.g. JPEG bytes named .webp)
                # are harmless — the content is a real image and decodes fine.
                if ext in IMAGE_EXTENSIONS and exts & IMAGE_EXTENSIONS:
                    return True
                return False
        # No known magic-byte match — only allow text extensions through
        if ext in TEXT_EXTENSIONS:
            return True
        # Non-text extension with no matching signature → reject
        return False
    except OSError:
        return False


def validate_extension(ext: str) -> bool:
    """Check whether *ext* is in the server's allowed-extension list."""
    return ext in settings.ALLOWED_EXTENSIONS


# 人类可读的实际格式名（面向用户的报错文案；键与 MAGIC_BYTES 对齐）
_MAGIC_TYPE_NAMES: dict[bytes, str] = {
    b"%PDF": "PDF 文档",
    b"PK\x03\x04": "zip 容器（docx 等 Office 新格式）",
    b"\xd0\xcf\x11\xe0": "旧版 Word/RTF 复合文档（OLE）",
    b"{\\rtf": "RTF 文档",
    b"\xff\xd8\xff": "JPEG 图片",
    b"\x89PNG": "PNG 图片",
    b"GIF8": "GIF 图片",
    b"BM": "BMP 图片",
    b"RIFF": "WebP 图片",
    b"II\x2a\x00": "TIFF 图片",
    b"MM\x00\x2a": "TIFF 图片",
}


def detect_content_type(file_path: str) -> str:
    """按文件头给出人类可读的实际格式名，供报错文案引用。

    与 :func:`validate_magic_bytes` 的判定无关——即使扩展名不合法也照样描述
    实际内容（纯文本按可解码即认定，与 TEXT_EXTENSIONS 语义一致）。
    """
    try:
        with open(file_path, "rb") as f:
            header = f.read(8)
    except OSError:
        return "未知格式"
    for magic, name in _MAGIC_TYPE_NAMES.items():
        if header.startswith(magic):
            return name
    try:
        with open(file_path, "rb") as f:
            data = f.read(4096)
        # NUL 字节是二进制的强信号（UTF-8 里虽合法，文本文件不该有）
        if b"\x00" in data:
            return "未知二进制格式"
        data.decode("utf-8")
        return "纯文本"
    except (OSError, UnicodeDecodeError):
        pass
    return "未知二进制格式"


def spoof_reject_message(file_path: str, ext: str) -> str:
    """扩展名与内容不符的用户文案：说清实际格式并给可操作指引（2026-11-03 用户反馈）。"""
    return (
        f"文件内容实际是{detect_content_type(file_path)}，与扩展名 {ext} 不匹配——"
        "文件可能在传输中损坏或被重命名过；请用原始文件重新导出/另存为后再上传"
    )


def get_file_type(filename: str) -> FileType | None:
    """Infer :class:`FileType` from file extension. Returns ``None`` for
    unsupported types (caller should raise an appropriate error)."""
    ext = os.path.splitext(filename)[1].lower()
    if ext == ".doc":
        return FileType.DOC
    elif ext == ".docx":
        return FileType.DOCX
    elif ext in (".txt", ".md", ".rtf", ".html", ".htm"):
        return FileType.TXT
    elif ext == ".pdf":
        return FileType.PDF
    elif ext in (".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp", ".tif", ".tiff"):
        return FileType.IMAGE
    return None


def safe_path_in_dir(file_path: str, allowed_dir: str) -> bool:
    """Path-traversal guard: ensure *file_path* is inside *allowed_dir*.

    Uses pathlib.Path.resolve() for symlink resolution and
    is_relative_to() for safe containment check.
    """
    from pathlib import Path

    try:
        resolved_file = Path(file_path).resolve(strict=False)
        resolved_dir = Path(allowed_dir).resolve(strict=False)
        return resolved_file == resolved_dir or resolved_file.is_relative_to(resolved_dir)
    except (OSError, ValueError):
        return False
