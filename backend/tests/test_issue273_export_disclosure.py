"""Issue#273 任务 11 · 导出说明页披露 verify_incomplete 页清单（用户拍板 b：交付+披露）。

判据（需求书 task-11-brief）：

* included 页里 ``file_store info["page_routing"][str(page)]["verify_incomplete"]``
  为真的页，说明页追加一行 ``未完成对账页：3、7、12（该页按文本层结果交付，建议复核）``
  ——页号升序、顿号分隔、**逐页列举不得区间缩写**（同既有说明页纪律）；
* 全部正常页 ⇒ 不加段（零噪声）；
* ``page_routing`` 键整体缺失（file 模式 / 旧任务）⇒ 不含该段且不崩
  ——说明页是交付物，不能因诊断信息崩，读取段整体 try/except 静默兜底。
"""

from __future__ import annotations

import asyncio
import json
import zipfile
from pathlib import Path

import fitz
import pytest

from app.services import partial_export_service as pes

# ---------------------------------------------------------------------------
# verify_incomplete_pages_of（file_info → included 页里的对账未完成页清单）
# ---------------------------------------------------------------------------

def _routing(*entries: tuple[int, bool]) -> dict[str, dict[str, bool]]:
    return {str(page): {"verify_incomplete": flag} for page, flag in entries}


def test_helper_picks_verify_incomplete_pages_among_included():
    info = {"page_routing": _routing((1, False), (2, True), (3, True))}
    assert pes.verify_incomplete_pages_of(info, [1, 2, 3]) == [2, 3]


def test_helper_filters_to_included_pages_only():
    """未选中的页即使账本记了 verify_incomplete 也不披露（只对交付内容负责）。"""
    info = {"page_routing": _routing((2, True), (7, True), (12, True))}
    assert pes.verify_incomplete_pages_of(info, [2, 7]) == [2, 7]


def test_helper_sorts_and_dedupes_pages():
    info = {"page_routing": _routing((12, True), (3, True), (7, True))}
    assert pes.verify_incomplete_pages_of(info, [12, 3, 7, 3]) == [3, 7, 12]


def test_helper_missing_page_routing_is_silent():
    """file 模式 / 旧任务：page_routing 键整体缺失 ⇒ 空、不崩。"""
    assert pes.verify_incomplete_pages_of({"file_path": "x.pdf"}, [1, 2]) == []
    assert pes.verify_incomplete_pages_of({}, [1]) == []
    assert pes.verify_incomplete_pages_of(None, [1]) == []


@pytest.mark.parametrize(
    "info",
    [
        {"page_routing": "not-a-dict"},
        {"page_routing": None},
        {"page_routing": {2: {"verify_incomplete": True}}},  # 键类型不对（int 键）
        {"page_routing": {"2": "not-a-dict-entry"}},
        {"page_routing": {"2": {"verify_incomplete": "yes-string"}}},
    ],
)
def test_helper_malformed_ledger_is_silent(info):
    """账本形状不对 ⇒ 静默不披露（宁可不披露也不能让导出崩）。

    最后一例钉住「真值语义」：账本写的是 bool（T7 finalize 契约），非 bool 的
    脏值按缺失处理。
    """
    assert pes.verify_incomplete_pages_of(info, [1, 2]) == []


def test_helper_tolerates_non_int_page_entries():
    assert pes.verify_incomplete_pages_of(
        {"page_routing": {"2": {"verify_incomplete": True}}}, ["2", "x"]
    ) == [2]


# ---------------------------------------------------------------------------
# 说明页披露段
# ---------------------------------------------------------------------------

def _cover_lines(**overrides):
    kwargs = dict(
        pages=[1, 2],
        page_count=2,
        excluded=[],
        snapshot_at="2026-11-12T00:00:00+00:00",
        generated_at="2026-11-12T00:01:00+00:00",
        export_id="0123456789abcdef",
        recognized_pages_at_export=2,
    )
    kwargs.update(overrides)
    return pes.build_cover_lines(**kwargs)


def test_cover_discloses_verify_incomplete_pages():
    """① 两页卷页 2 verify_incomplete=true ⇒ 文本含「未完成对账页：2」。"""
    lines = _cover_lines(verify_incomplete_pages=[2])
    text = "\n".join(lines)
    assert "未完成对账页：2" in text
    assert "该页按文本层结果交付，建议复核" in text
    # 逐页列举，不得区间缩写
    assert "未完成对账页：1-2" not in text


def test_cover_lists_multiple_pages_individually():
    lines = _cover_lines(verify_incomplete_pages=[3, 7, 12])
    text = "\n".join(lines)
    assert "未完成对账页：3、7、12" in text


def test_cover_no_disclosure_when_all_pages_ok():
    """② 全正常页 ⇒ 不含该段（零噪声）。"""
    lines = _cover_lines(verify_incomplete_pages=[])
    assert "未完成对账页" not in "\n".join(lines)


def test_cover_no_disclosure_when_param_absent():
    """不传参数（旧调用方）⇒ 同样零噪声，且不崩。"""
    lines = _cover_lines()
    assert "未完成对账页" not in "\n".join(lines)


def test_cover_disclosure_lines_survive_long_page_list():
    """页号很多时折行仍逐页列举（复用 _page_label_chunks 的宽度折行）。"""
    many = list(range(2, 41))  # 39 页
    lines = _cover_lines(pages=[1, *many], page_count=41, verify_incomplete_pages=many)
    joined = "".join(lines)
    for page in many:
        assert str(page) in joined
    assert "未完成对账页：2-40" not in joined


# ---------------------------------------------------------------------------
# 端到端：说明页落进 PDF 且可读回
# ---------------------------------------------------------------------------

def test_cover_page_renders_disclosure(tmp_path):
    src = tmp_path / "src.pdf"
    doc = fitz.open()
    for _ in range(2):
        doc.new_page(width=595.0, height=842.0)
    doc.save(src)
    doc.close()

    lines = _cover_lines(verify_incomplete_pages=[2])
    pes.insert_cover_page(str(src), lines)
    with fitz.open(str(src)) as rendered:
        assert rendered.page_count == 3
        text = rendered[0].get_text().replace("\n", "")
    assert "未完成对账页：2（该页按文本层结果交付，建议复核）" in text


# ---------------------------------------------------------------------------
# submit_partial_export 快照透传（真 manager 全链，沿用 #257 任务级打桩法）
# ---------------------------------------------------------------------------

def _file_info_with_routing(src, entries) -> dict:
    boxes = {}
    for page in (1, 2):
        boxes[page] = [
            {
                "id": f"b{page}",
                "x": 0.17,
                "y": 0.24,
                "width": 0.34,
                "height": 0.072,
                "type": "PERSON",
                "page": page,
                "source": "manual",
            }
        ]
    return {
        "file_id": "file-1",
        "file_type": "pdf_scanned",
        "page_count": 3,
        "original_filename": "卷.pdf",
        "file_path": str(src),
        "bounding_boxes": boxes,
        "page_routing": _routing(*entries),
    }


def _make_pdf(tmp_path, pages: int):
    doc = fitz.open()
    for _ in range(pages):
        page = doc.new_page(width=595.0, height=842.0)
        page.draw_rect(fitz.Rect(100, 200, 300, 260), color=(1, 0, 0), fill=(1, 0, 0))
    src = tmp_path / "src.pdf"
    doc.save(src)
    doc.close()
    return src


def _run_submit(tmp_path, monkeypatch, info, pages):
    from app.core.config import settings
    from app.services.export_service import ExportTaskManager
    from app.services.partial_export_service import config_from_job_config

    out_root = tmp_path / "out"
    out_root.mkdir(exist_ok=True)
    monkeypatch.setattr(settings, "OUTPUT_DIR", str(out_root))
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path))

    async def _run():
        task = await pes.submit_partial_export(
            owner_id="admin",
            item_row={"job_id": "j1", "id": "i1", "file_id": "file-1"},
            file_info=info,
            config=config_from_job_config({"watermark_delivery": "keep"}),
            pages=pages,
            excluded_failed_pages=[],
            snapshot_at="2026-11-12T00:00:00+00:00",
            manager=ExportTaskManager(),
        )
        for _ in range(300):
            if task.status in {"completed", "failed"}:
                break
            await asyncio.sleep(0.02)
        return task

    return asyncio.run(_run())


def test_submit_partial_export_cover_contains_disclosure(tmp_path, monkeypatch):
    """submit 链路：included 页（页 2）的对账未完成写进说明页与 manifest cover。"""
    src = _make_pdf(tmp_path, 3)
    info = _file_info_with_routing(src, [(1, False), (2, True), (3, True)])

    task = _run_submit(tmp_path, monkeypatch, info, pages=[1, 2])
    assert task.status == "completed", task.error

    manifest = json.loads(
        (Path(task.out_dir) / "manifest.json").read_text(encoding="utf-8")
    )
    cover_text = "\n".join(manifest["cover"])
    assert "未完成对账页：2（该页按文本层结果交付，建议复核）" in cover_text

    zip_path = Path(task.out_dir) / "part001.zip"
    with zipfile.ZipFile(zip_path) as zf:
        pdf_name = zf.namelist()[0]
        pdf_path = tmp_path / "extracted.pdf"
        pdf_path.write_bytes(zf.read(pdf_name))
    with fitz.open(pdf_path) as rendered:
        text = rendered[0].get_text().replace("\n", "")
    assert "未完成对账页：2" in text


def test_submit_partial_export_without_routing_stays_quiet(tmp_path, monkeypatch):
    """③ page_routing 键整体缺失（file 模式旧数据）⇒ 说明页不含该段且任务照常完成。"""
    from app.core.config import settings
    from app.services.export_service import ExportTaskManager
    from app.services.partial_export_service import config_from_job_config

    src = _make_pdf(tmp_path, 2)
    info = _file_info_with_routing(src, [(1, False), (2, False)])
    info.pop("page_routing")

    out_root = tmp_path / "out"
    out_root.mkdir(exist_ok=True)
    monkeypatch.setattr(settings, "OUTPUT_DIR", str(out_root))
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path))

    async def _run():
        task = await pes.submit_partial_export(
            owner_id="admin",
            item_row={"job_id": "j1", "id": "i1", "file_id": "file-1"},
            file_info=info,
            config=config_from_job_config({"watermark_delivery": "keep"}),
            pages=[1, 2],
            excluded_failed_pages=[],
            snapshot_at="2026-11-12T00:00:00+00:00",
            manager=ExportTaskManager(),
        )
        for _ in range(300):
            if task.status in {"completed", "failed"}:
                break
            await asyncio.sleep(0.02)
        return task

    task = asyncio.run(_run())
    assert task.status == "completed", task.error

    manifest = json.loads(
        (Path(task.out_dir) / "manifest.json").read_text(encoding="utf-8")
    )
    assert "未完成对账页" not in "\n".join(manifest["cover"])
