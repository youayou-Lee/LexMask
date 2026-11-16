"""Issue #342：同一份字节的重复解析必须复用首遍结果（parse_file 指纹缓存）。

上传时 GET /parse 已把解析结果落 file_store，识别任务的 _parse_file 对同一
份字节原样再解析一遍（86 页扫描卷 ~8.4s 白烧）。修后 parse_file 在快照含
解析字段且解析指纹（size+mtime_ns）与当前文件一致时，直接用快照字段重建
ParseResult 返回；字节变化（解密落盘/摘水印替换等一切 os.replace 写入必改
mtime）⇒ 指纹失配 ⇒ 真重解析。解析失败不落指纹——失败态不可缓存。

修前基线（preview 4f5babd + #339 cherry-pick 381005e）：仅「同字节跳重解析
计数器」一条修前红；mtime 失配/缺字段/失败不缓存三条在修前本来就绿，靠
变异验证（三条变异全 KILLED）保证判据有效。
"""

import asyncio
import os
from pathlib import Path

import pytest

from app.core.config import settings
from app.models.common import FileType
from app.services import file_processing_service
from app.services.file_management_service import file_store
from app.services.file_parser import FileParser, PdfEncryptedError

FIXTURES = Path(__file__).parent / "fixtures" / "issue339"


@pytest.fixture(autouse=True)
def _allow_fixtures_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(FIXTURES))


@pytest.fixture(autouse=True)
def _clean_caches() -> None:
    with FileParser._pdf_page_scan_cache_lock:
        FileParser._pdf_page_scan_cache.clear()
    file_store.pop("reuse_text", None)
    file_store.pop("reuse_scan", None)
    yield
    file_store.pop("reuse_text", None)
    file_store.pop("reuse_scan", None)


def _seed(file_id: str, path: Path) -> None:
    file_store[file_id] = {
        "file_id": file_id,
        "file_path": str(path),
        "file_type": "pdf",
        "original_filename": path.name,
        "owner_id": "diag",
    }


def _counting_parser(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """把 FileParser.parse 包一层计数器（真解析照常执行），返回计数列表。"""
    calls: list[int] = []
    real = FileParser.parse

    async def counting(self, file_path, file_type):
        calls.append(len(calls) + 1)
        return await real(self, file_path, file_type)

    monkeypatch.setattr(FileParser, "parse", counting)
    return calls


@pytest.mark.asyncio
async def test_same_bytes_second_parse_reuses_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同字节第二次 parse_file：FileParser.parse 零调用 + 返回与首遍逐字段一致（修前红）。"""
    calls = _counting_parser(monkeypatch)
    _seed("reuse_text", FIXTURES / "text.pdf")

    first = await file_processing_service.parse_file("reuse_text")
    assert calls == [1]
    second = await file_processing_service.parse_file("reuse_text")
    assert calls == [1], "同字节第二次 parse_file 仍真解析——重复劳动没有消除"

    for field in (
        "is_scanned",
        "page_count",
        "content",
        "pages",
        "watermark_inline_filtered",
        "file_type",
    ):
        fv = getattr(first, field)
        fv = fv.value if hasattr(fv, "value") else fv
        sv = getattr(second, field)
        sv = sv.value if hasattr(sv, "value") else sv
        assert sv == fv, f"复用结果的 {field} 与首遍漂移"
    # 文本型夹具：pages 必须非空（防占位/恒等假复用）
    assert second.pages
    assert second.file_id == "reuse_text"


@pytest.mark.asyncio
async def test_mtime_change_forces_reparse(monkeypatch: pytest.MonkeyPatch) -> None:
    """mtime 失配（模拟解密/摘水印替换落盘）⇒ 指纹失配 ⇒ 真重解析。"""
    calls = _counting_parser(monkeypatch)
    _seed("reuse_text", FIXTURES / "text.pdf")

    await file_processing_service.parse_file("reuse_text")
    assert calls == [1]

    st = os.stat(FIXTURES / "text.pdf")
    os.utime(FIXTURES / "text.pdf", ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))

    await file_processing_service.parse_file("reuse_text")
    assert calls == [1, 2], "mtime 失配后未真重解析——失效逻辑失效"


@pytest.mark.asyncio
async def test_missing_fields_or_fingerprint_parses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """存量文件形态：只有指纹没有解析字段 ⇒ 真解析（不崩溃不误判命中）。"""
    calls = _counting_parser(monkeypatch)
    _seed("reuse_text", FIXTURES / "text.pdf")
    st = os.stat(FIXTURES / "text.pdf")
    # 必须走 update_fields：file_store[file_id] 下标读出的是副本，直接赋值存不进去
    file_store.update_fields("reuse_text", {"parse_fingerprint": f"{st.st_size}:{st.st_mtime_ns}"})

    await file_processing_service.parse_file("reuse_text")
    assert calls == [1], "有指纹但缺解析字段时应真解析"


@pytest.mark.asyncio
async def test_parse_failure_never_caches_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """解析异常（含泛化形态）不落指纹——失败态不可缓存，下次真解析。"""
    # 加密实例：结构化异常且无指纹
    _seed("reuse_text", FIXTURES / "encrypted.pdf")
    with pytest.raises(PdfEncryptedError):
        await file_processing_service.parse_file("reuse_text")
    assert "parse_fingerprint" not in file_store["reuse_text"]
    assert "page_count" not in file_store["reuse_text"]

    # 泛化实例：任意解析异常同样不落指纹
    calls = _counting_parser(monkeypatch)
    counting = FileParser.parse  # 计数包装版（_counting_parser 刚装上的）
    _seed("reuse_text", FIXTURES / "text.pdf")

    async def broken(self, file_path, file_type):
        raise RuntimeError("boom")

    monkeypatch.setattr(FileParser, "parse", broken)
    with pytest.raises(RuntimeError):
        await file_processing_service.parse_file("reuse_text")
    assert "parse_fingerprint" not in file_store["reuse_text"]
    assert calls == []  # broken 直接抛、绕过计数器——本轮不计数

    # 恢复计数真解析后（字节未变）：因为失败未落指纹，本次真解析并落指纹
    monkeypatch.setattr(FileParser, "parse", counting)
    result = await file_processing_service.parse_file("reuse_text")
    assert calls == [1]
    assert result.page_count == 2
    assert "parse_fingerprint" in file_store["reuse_text"]


@pytest.mark.asyncio
async def test_concurrent_first_parses_are_consistent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """两路并发冷解析：均真解析、后写覆盖、结果一致（边界假设钉测）。"""
    calls = _counting_parser(monkeypatch)
    _seed("reuse_text", FIXTURES / "text.pdf")

    results = await asyncio.gather(
        file_processing_service.parse_file("reuse_text"),
        file_processing_service.parse_file("reuse_text"),
    )
    assert calls == [1, 2]
    a, b = results
    assert a.page_count == b.page_count
    assert a.content == b.content
    assert a.is_scanned == b.is_scanned
