"""页面渲染器（Issue#56 M3 / Task 4）：pypdfium2 可选依赖。

``render_page_png(pdf_path, page_no, out) -> bool``：把 PDF 第 ``page_no`` 页
（0 起）渲染为 PNG 写入 ``out``。pypdfium2 **延迟导入**——模块缺失或任何渲染
失败一律 ``return False``（不抛异常），工作台 ``/img`` 端点据此降级 404，
前端回落转录高亮视图。

缓存文件名（控制器裁定 6，确定性）::

    {stem}-p{page_no:03d}.png      # 由 png_name() 给出，缓存目录
                                   # = {pdf_root or work}/render_cache，按需创建

依赖注记：pypdfium2 不进任何 requirements（全局约束：不新增必装依赖），
仅真渲染路径 import。
"""
from __future__ import annotations

from pathlib import Path

RENDER_SCALE = 2.0  # 渲染倍率（默认页面的 2 倍分辨率，工作台可读性优先）


def png_name(pdf_path: str | Path, page_no: int) -> str:
    """缓存文件名：``{stem}-p{page_no:03d}.png``（确定性，供服务端复用）。"""
    return f"{Path(pdf_path).stem}-p{page_no:03d}.png"


def render_page_png(pdf_path: str, page_no: int, out: Path) -> bool:
    """渲染 PDF 指定页为 PNG。成功 True；任何失败 False（可选依赖降级）。"""
    try:
        import pypdfium2 as pdfium  # 延迟导入：缺失 → False
    except Exception:
        return False
    try:
        pdf = pdfium.PdfDocument(pdf_path)
        if not 0 <= page_no < len(pdf):
            return False
        bitmap = pdf[page_no].render(scale=RENDER_SCALE)
        pil = bitmap.to_pil()
        out = Path(out)
        out.parent.mkdir(parents=True, exist_ok=True)
        pil.save(str(out))
        return out.is_file()
    except Exception:
        return False
