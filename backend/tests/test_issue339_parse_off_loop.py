"""Issue #339：`_parse_pdf` 的逐页工作必须离开事件循环线程（asyncio.to_thread）。

修前 `_parse_pdf` 是 `async def` 但整段同步跑在事件循环上：多页扫描卷的逐页
inline 水印探针（#250 `overlay_filtered_text`）+ 扫描页检测会冻住事件循环
~8.4s（86 页扫描卷实测），导致——

1. worker 领任务即在提交瞬间开跑同一解析，识别提交 POST 的响应写不出去，
   预览页阻塞弹窗滞留 ~9s 才翻 passive（非阻塞设计 #194 在该窗口失效）；
2. GET /parse 在途期间所有并发请求停摆。

修后整段循环经 `asyncio.to_thread` 执行；`parse_golden.json` 为修前代码对
同字节夹具固化的黄金快照（2026-11-17 采集于 preview `6f58db8`），保证解析
结果零漂移。夹具为同字节入库件（tests/fixtures/issue339/），fitz 大版本
升级导致快照漂移时须先确认算法未变再重采并注明。
"""

import asyncio
import json
import threading
import time
from pathlib import Path

import pytest

from app.core.config import settings
from app.models.common import FileType
from app.services.file_parser import FileParser, PdfEncryptedError

FIXTURES = Path(__file__).parent / "fixtures" / "issue339"
GOLDEN = json.loads((FIXTURES / "parse_golden.json").read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def _allow_fixtures_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    """_validate_path 只放行 UPLOAD_DIR 内路径——测试夹具目录整体放行。"""
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(FIXTURES))


@pytest.fixture(autouse=True)
def _clean_scan_cache() -> None:
    with FileParser._pdf_page_scan_cache_lock:
        FileParser._pdf_page_scan_cache.clear()


@pytest.mark.asyncio
async def test_parse_pdf_runs_off_event_loop_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """逐页探针不得在事件循环线程上执行（修前红：全程在 loop 线程）。"""
    import app.services.watermark_overlay as wm

    loop_thread = threading.get_ident()
    seen_threads: list[int] = []

    real_filter = wm.overlay_filtered_text

    def recording_filter(page, **kwargs):
        seen_threads.append(threading.get_ident())
        return real_filter(page, **kwargs)

    monkeypatch.setattr(wm, "overlay_filtered_text", recording_filter)

    await FileParser().parse(str(FIXTURES / "text.pdf"), FileType.PDF)

    assert seen_threads, "逐页探针未被调用（夹具/接线断了）"
    assert all(tid != loop_thread for tid in seen_threads), (
        "overlay_filtered_text 仍在事件循环线程上执行——提交回执会被解析冻住"
    )


@pytest.mark.asyncio
async def test_event_loop_stays_schedulable_during_parse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """解析期间事件循环必须可调度（修前红：心跳 0 拍；修后 ≥3 拍）。

    monkeypatch 注入每页 40ms 真阻塞，text.pdf 两页 ⇒ ~80ms 冻结窗口；
    心跳每 10ms 一拍。修前循环被同步工作冻满全窗，心跳 0 拍。
    """
    import app.services.watermark_overlay as wm

    real_filter = wm.overlay_filtered_text

    def slow_filter(page, **kwargs):
        time.sleep(0.04)  # 模拟 fitz/探针的同步耗时（真阻塞，非 await）
        return real_filter(page, **kwargs)

    monkeypatch.setattr(wm, "overlay_filtered_text", slow_filter)

    beats = 0

    async def heartbeat() -> None:
        nonlocal beats
        while True:
            await asyncio.sleep(0.01)
            beats += 1

    hb = asyncio.create_task(heartbeat())
    try:
        await FileParser().parse(str(FIXTURES / "text.pdf"), FileType.PDF)
    finally:
        hb.cancel()
        try:
            await hb
        except asyncio.CancelledError:
            pass

    assert beats >= 3, (
        f"解析 {beats} 拍心跳 <3——事件循环在解析期间被同步工作冻住"
    )


@pytest.mark.asyncio
async def test_parse_results_match_golden_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """三形态夹具（纯扫描/文本型/水印卷）逐字段对比修前黄金快照——零漂移。"""
    fp = FileParser()
    for name, expected in GOLDEN.items():
        monkeypatch.setattr(
            settings, "WATERMARK_OVERLAY_STRIP_ENABLED", name == "watermark"
        )
        result = await fp.parse(str(FIXTURES / f"{name}.pdf"), FileType.PDF)
        actual = {
            "is_scanned": result.is_scanned,
            "page_count": result.page_count,
            "content": result.content,
            "pages": result.pages,
            "watermark_inline_filtered": result.watermark_inline_filtered,
            "file_type": (
                result.file_type.value
                if hasattr(result.file_type, "value")
                else str(result.file_type)
            ),
        }
        assert actual == expected, f"{name}.pdf 解析结果与修前黄金快照漂移"


@pytest.mark.asyncio
async def test_encrypted_pdf_error_propagates_from_thread() -> None:
    """加密 PDF 经线程后仍抛结构化 PdfEncryptedError（异常契约不被线程吞）。"""
    with pytest.raises(PdfEncryptedError):
        await FileParser().parse(str(FIXTURES / "encrypted.pdf"), FileType.PDF)


@pytest.mark.asyncio
async def test_concurrent_parses_are_independent_and_correct(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """两路解析真并发（to_thread 化后的新形态）：各自结果正确、互不串染。"""
    fp = FileParser()
    results = await asyncio.gather(
        fp.parse(str(FIXTURES / "text.pdf"), FileType.PDF),
        fp.parse(str(FIXTURES / "scan.pdf"), FileType.PDF),
    )
    text_result, scan_result = results
    assert text_result.is_scanned is False
    assert text_result.page_count == GOLDEN["text"]["page_count"]
    assert text_result.content == GOLDEN["text"]["content"]
    assert scan_result.is_scanned is True
    assert scan_result.page_count == GOLDEN["scan"]["page_count"]
    assert scan_result.content == ""
