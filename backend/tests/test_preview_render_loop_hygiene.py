"""Issue#207: 预览渲染链事件循环卫生（to_thread + 同键单飞）。

根因（方案文档 docs/issue-207-preview-flip-perf.md §1.2）：get_pdf_page_image 与
preview_redaction 在 async def 内裸跑同步 CPU 活（PyMuPDF 渲染 + PIL 编码），
每次未命中缓存的预览翻页把整个事件循环冻结渲染时长，识别期间放大为全局卡顿。

判别性说明（AC4）：call_soon 探针断言在**改前代码上红**——await 一个纯同步协程
不让出事件循环，渲染前排队的回调直到断言时都没跑；改后（to_thread）回调在
await 期间获得执行机会，断言绿。改前红跑留痕见 PR 描述。
"""

import asyncio
import io

import fitz
import pytest
from PIL import Image

from app.core.config import settings
from app.models.schemas import FileType
from app.services import file_parser as fp_module
from app.services.file_parser import FileParser
from app.services.vision_service import VisionService


pytestmark = pytest.mark.asyncio


def _make_pdf(tmp_path, pages: int = 3) -> str:
    """生成自包含测试 PDF（写入 UPLOAD_DIR，过 _validate_path 白名单）。"""
    doc = fitz.open()
    for i in range(pages):
        page = doc.new_page(width=595, height=842)
        page.insert_text((72, 100), f"issue207 loop hygiene test page {i + 1}")
    path = str(tmp_path / "uploads" / f"loop-hygiene-{pages}p.pdf")
    doc.save(path)
    doc.close()
    return path


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    up = tmp_path / "uploads"
    up.mkdir()
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(up))
    # _pdf_page_image_cache / _pdf_page_render_inflight 都是类级状态，必须清掉才算冷
    FileParser._pdf_page_image_cache.clear()
    FileParser._pdf_page_render_inflight.clear()
    yield
    FileParser._pdf_page_image_cache.clear()
    FileParser._pdf_page_render_inflight.clear()


@pytest.fixture()
def pdf_path(tmp_path):
    return _make_pdf(tmp_path)


def _probe_will_run_during(awaitable_coro) -> bool:
    """在当前事件循环上排队 call_soon 标记位，然后 await 目标协程，
    返回「await 期间标记位是否已置位」。

    目标协程若全程不让出事件循环（同步 CPU 活内联），标记位在 await 返回时
    必然未置位；只要存在 to_thread/真正的 await 点，循环就有机会跑回调。
    """

    async def run() -> bool:
        loop = asyncio.get_running_loop()
        flagged = asyncio.Event()
        loop.call_soon(flagged.set)
        await awaitable_coro
        return flagged.is_set()

    return run()


class TestLoopNotBlocked:
    async def test_get_pdf_page_image_cold_render_yields_to_loop(self, pdf_path):
        """AC4a：get_pdf_page_image 冷缓存渲染期间，排队的 call_soon 回调能执行。"""
        result = await _probe_will_run_during(FileParser().get_pdf_page_image(pdf_path, 1))
        assert result is True, (
            "get_pdf_page_image 全程未让出事件循环：同步渲染阻塞了循环"
            "（Issue#207 根因，应移入 asyncio.to_thread）"
        )

    async def test_preview_redaction_yields_to_loop(self, pdf_path):
        """AC4b：preview_redaction（PDF 路径 = 渲染 + PIL 编码）期间循环能响应。"""
        svc = VisionService()
        result = await _probe_will_run_during(
            svc.preview_redaction(
                file_path=pdf_path,
                file_type=FileType.PDF,
                bounding_boxes=[],
                page=1,
            )
        )
        assert result is True, (
            "preview_redaction 全程未让出事件循环：PIL 编码段阻塞了循环"
            "（Issue#207 根因，应移入 asyncio.to_thread）"
        )


class TestBehaviorUnchanged:
    async def test_preview_redaction_bytes_match_inline_reference(self, pdf_path):
        """AC5：to_thread 迁移不改产物——与测试内复刻的改前内联序列逐字节一致。

        参考实现 = 改前 preview_redaction 的原调用序列（get_pdf_page_image 的
        fitz 渲染 + PIL open/convert/save），同进程同库版本，天然确定性。
        """
        svc = VisionService()
        result = await svc.preview_redaction(
            file_path=pdf_path,
            file_type=FileType.PDF,
            bounding_boxes=[],
            page=1,
        )

        doc = fitz.open(pdf_path)
        pix = doc.load_page(0).get_pixmap(matrix=fitz.Matrix(150 / 72, 150 / 72))
        raw = pix.tobytes("png")
        doc.close()
        image = Image.open(io.BytesIO(raw)).convert("RGB")
        out = io.BytesIO()
        image.save(out, format="PNG")
        reference = out.getvalue()

        assert result == reference

    async def test_second_call_cache_hit_identical_bytes(self, pdf_path):
        """缓存幂等子判据：二次调用命中缓存返回同字节。"""
        svc = VisionService()
        first = await svc.preview_redaction(
            file_path=pdf_path, file_type=FileType.PDF, bounding_boxes=[], page=1
        )
        second = await svc.preview_redaction(
            file_path=pdf_path, file_type=FileType.PDF, bounding_boxes=[], page=1
        )
        assert first == second


class TestSingleflight:
    async def test_concurrent_same_page_renders_once(self, pdf_path, monkeypatch):
        """AC10c（2.2-3 同键单飞）：同页并发冷请求只做一次 fitz 渲染。

        客户端 abort 不终止已进线程池的渲染；无单飞层时连点风暴=每点一次
        一个线程池渲染任务。单飞把服务端工作收敛为「每唯一页一次」。
        判别性注记：本测试在**改前代码上假绿**——旧实现不让出事件循环，
        gather 的 6 个调用被串行化、后 5 个命中缓存（opens==1 碰巧成立）；
        to_thread 落地后调用才真并发，此时若去掉单飞本测试即红（opens==6）。
        """
        real_open = fp_module.open_pdf_checked
        opens = {"n": 0}

        def counting_open(path):
            opens["n"] += 1
            return real_open(path)

        monkeypatch.setattr(fp_module, "open_pdf_checked", counting_open)

        parser = FileParser()
        results = await asyncio.gather(
            *(parser.get_pdf_page_image(pdf_path, 1) for _ in range(6))
        )

        assert opens["n"] == 1, (
            f"同页 6 个并发冷请求触发了 {opens['n']} 次文档打开/渲染，"
            "缺少同键单飞（Issue#207 2.2-3）"
        )
        assert all(r == results[0] for r in results)
