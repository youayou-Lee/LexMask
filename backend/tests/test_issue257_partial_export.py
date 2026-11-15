"""Issue#257 一期 · AC1.2 / AC1.4 / AC1.5：部分导出的产物正确性与选页契约。

Issue#278 统一导出改造后，本文件改用新名（``validate_export_pages`` /
``build_summary_lines`` / ``build_item_package``），判据本身零变化：

核心判据（门①.5 冷审后重写）：**逐页内嵌图像流 sha256 与「全量参照件」对照**。
参照件 = 对同一原件走既有 ``Redactor().redact`` 打码全卷（不经
``execute_redaction``、不写 file_store）。该判据一次覆盖：框页号重映射、产物页序、
渲染 DPI/JPEG 参数一致性、打码生效；再反向断言「产物页图像不等于参照件其它页」
防止页序错位却自洽的假绿。

另外钉住说明.txt 的内容（逐页列举、不得写「第 1-5 页」这种撒谎区间）、失败页永不进包、
以及 AC1.5 的错误码全集。说明页插入逻辑已随 #278 废除（说明退为可选 说明.txt）。
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import fitz
import pytest

from app.models.common import FileType, ReplacementMode
from app.models.entity_schemas import BoundingBox
from app.models.schemas import RedactionConfig
from app.services import partial_export_service as pes
from app.services.redactor import Redactor

PAGE_W, PAGE_H = 595.0, 842.0
RECT = (100.0, 200.0, 300.0, 260.0)  # x0,y0,x1,y1 (pt)


def _make_pdf(path, pages: int = 6) -> str:
    doc = fitz.open()
    for i in range(pages):
        page = doc.new_page(width=PAGE_W, height=PAGE_H)
        page.insert_text((72.0, 100.0), f"MARKER-P{i + 1}", fontname="china-s", fontsize=14)
        page.draw_rect(fitz.Rect(*RECT), color=(1, 0, 0), fill=(1, 0, 0))
    doc.save(str(path))
    doc.close()
    return str(path)


def _box(page: int, box_id: str) -> BoundingBox:
    x0, y0, x1, y1 = RECT
    return BoundingBox(
        id=box_id,
        x=x0 / PAGE_W,
        y=y0 / PAGE_H,
        width=(x1 - x0) / PAGE_W,
        height=(y1 - y0) / PAGE_H,
        page=page,
        type="text",
        text=f"secret-{page}",
        selected=True,
        confidence=None,
        source="manual",
    )


def _config() -> RedactionConfig:
    return RedactionConfig(
        replacement_mode=ReplacementMode.STRUCTURED,
        entity_types=["PERSON"],
        custom_replacements={},
        image_redaction_method="fill",
        image_redaction_strength=75,
        image_fill_color="#000000",
        watermark_text=None,
        watermark_delivery="keep",
    )


def _page_image_sha(pdf_path: str, index0: int) -> str:
    doc = fitz.open(pdf_path)
    try:
        images = doc[index0].get_images(full=True)
        assert images, f"page {index0} has no embedded image"
        raw = doc.extract_image(images[0][0])["image"]
        return hashlib.sha256(raw).hexdigest()
    finally:
        doc.close()


@pytest.fixture()
def synthetic_pdf(tmp_path):
    return _make_pdf(tmp_path / "src.pdf", 6)


@pytest.fixture()
def output_dir(tmp_path, monkeypatch):
    from app.core.config import settings

    out = tmp_path / "out"
    out.mkdir()
    monkeypatch.setattr(settings, "OUTPUT_DIR", str(out))
    # open_pdf_checked 只允许 UPLOAD_DIR/OUTPUT_DIR 下的路径，夹具 PDF 放在 tmp_path。
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path))
    return out


def _reference_pdf(src: str, boxes: list[BoundingBox], out_dir) -> str:
    info = {"file_path": src, "file_type": FileType.PDF_SCANNED, "is_scanned": True}
    result = asyncio.run(Redactor().redact(info, [], boxes, _config()))
    return result["output_path"]


# ---------------------------------------------------------------------------
# AC1.2 产物正确性
# ---------------------------------------------------------------------------

def test_partial_export_matches_reference_pages(synthetic_pdf, output_dir, tmp_path):
    """AC1.2：页数、逐页图像 sha256 全部对得上全量参照件；说明页废除（#278）。"""
    boxes = [_box(p, f"b{p}") for p in (1, 2, 3, 4, 5, 6)]
    reference = _reference_pdf(synthetic_pdf, boxes, output_dir)

    summary_lines = pes.build_summary_lines(
        pages=[1, 2, 3, 5],
        page_count=6,
        excluded=[(4, "inj")],
        snapshot_at="2026-11-11T00:40:00+00:00",
        generated_at="2026-11-11T00:41:00+00:00",
        export_id="0123456789abcdef",
        recognized_pages_at_export=4,
    )
    product = asyncio.run(
        pes.build_item_package(
            kind="partial",
            source_path=synthetic_pdf,
            pages=[1, 2, 3, 5],
            boxes=boxes,
            config=_config(),
            out_dir=str(tmp_path / "work"),
            final_name="部分导出.pdf",
            include_summary=True,
            summary_lines=summary_lines,
        )
    )

    doc = fitz.open(product["pdf_path"])
    try:
        assert doc.page_count == 4, "说明页废除（#278）：包内只有选中 4 页"
        texts = "".join(p.get_text() for p in doc)
        assert "MARKER-P6" not in texts
    finally:
        doc.close()
    assert product["summary_path"], "include_summary=True ⇒ 说明.txt 落盘"
    summary_text = Path(product["summary_path"]).read_text(encoding="utf-8")
    assert "第 1、2、3、5 页" in summary_text.replace("\n", "")
    assert "共 4 页" in summary_text
    assert "原卷总页数：6" in summary_text
    assert "第 4 页" in summary_text and "inj" in summary_text

    # 逐页图像流对照：产物第 k+1 页 == 参照件第 pages[k] 页
    ref_shas = {p: _page_image_sha(reference, p - 1) for p in (1, 2, 3, 4, 5, 6)}
    for idx, page_no in enumerate([1, 2, 3, 5]):
        got = _page_image_sha(product["pdf_path"], idx)  # 无说明页：产物页 k = 原卷 pages[k-1]
        assert got == ref_shas[page_no], f"page {page_no} image mismatch"
        others = [sha for p, sha in ref_shas.items() if p != page_no]
        assert got not in others, f"page {page_no} matches another reference page"


def test_partial_export_keeps_original_page_order(synthetic_pdf, output_dir, tmp_path):
    """D6：传入 [3,1] 也按原卷升序（产物第 2 页=原卷 1，第 3 页=原卷 3）。"""
    boxes = [_box(p, f"b{p}") for p in (1, 3)]
    reference = _reference_pdf(synthetic_pdf, boxes, output_dir)
    product = asyncio.run(
        pes.build_item_package(
            kind="partial",
            source_path=synthetic_pdf,
            pages=[3, 1],
            boxes=boxes,
            config=_config(),
            out_dir=str(tmp_path / "work"),
            final_name="p.pdf",
        )
    )
    assert product["summary_path"] is None, "include_summary 缺省关"
    assert _page_image_sha(product["pdf_path"], 0) == _page_image_sha(reference, 0)
    assert _page_image_sha(product["pdf_path"], 1) == _page_image_sha(reference, 2)


def test_boxes_are_remapped_to_subset_page_numbers():
    boxes = [_box(1, "a"), _box(3, "b"), _box(5, "c")]
    remapped = pes.remap_boxes_to_subset(boxes, [1, 3, 5])
    assert [b.page for b in remapped] == [1, 2, 3]
    # 未选中的框不进入包（selected=False 的语义沿用）
    boxes.append(BoundingBox(id="d", x=0.1, y=0.1, width=0.1, height=0.1, page=1, type="text", selected=False))
    remapped2 = pes.remap_boxes_to_subset(boxes, [1, 3, 5])
    assert [b.id for b in remapped2] == ["a", "b", "c"]
    # 原对象不被就地改写
    assert boxes[1].page == 3


# ---------------------------------------------------------------------------
# AC1.4 / AC1.5 选页契约
# ---------------------------------------------------------------------------

def test_validation_rejects_empty_selection():
    with pytest.raises(pes.PartialExportError) as exc:
        pes.validate_export_pages(
            [], kind="partial", page_count=6, failed_pages=set(), recognized_pages={1, 2}
        )
    assert exc.value.code == "pages_empty"


def test_validation_partial_without_pages_is_400():
    """#278：partial 无 pages ⇒ 新错误码 pages_required_for_partial。"""
    with pytest.raises(pes.PartialExportError) as exc:
        pes.validate_export_pages(
            None, kind="partial", page_count=6, failed_pages=set(), recognized_pages={1, 2}
        )
    assert exc.value.code == "pages_required_for_partial"


@pytest.mark.parametrize("bad", [0, -1, 7])
def test_validation_rejects_out_of_range(bad):
    with pytest.raises(pes.PartialExportError) as exc:
        pes.validate_export_pages(
            [bad], kind="partial", page_count=6, failed_pages=set(), recognized_pages={1, 2}
        )
    assert exc.value.code == "pages_out_of_range"


def test_validation_dedupes_and_allows_unsorted():
    result = pes.validate_export_pages(
        [2, 1, 2], kind="partial", page_count=6, failed_pages=set(), recognized_pages={1, 2}
    )
    assert result["pages"] == [1, 2]
    assert result["excluded_failed_pages"] == []


def test_validation_rejects_explicit_failed_page():
    with pytest.raises(pes.PartialExportError) as exc:
        pes.validate_export_pages(
            [1, 4], kind="partial", page_count=6, failed_pages={4}, recognized_pages={1, 2, 3, 5}
        )
    assert exc.value.code == "failed_pages_not_allowed"


def test_validation_auto_excludes_failed_pages_within_span():
    """Issue#257 需求重对齐（2026-11-11）：失败页不再要求任何「知情确认」勾选，
    区间内的失败页直接自动排除，并如实回报给调用方（写进说明.txt 与 manifest）。"""
    result = pes.validate_export_pages(
        [1, 2, 3, 5, 6], kind="partial", page_count=6, failed_pages={4}, recognized_pages={1, 2, 3, 5, 6}
    )
    assert result["excluded_failed_pages"] == [4]
    assert result["pages"] == [1, 2, 3, 5, 6]
    assert 4 not in result["pages"]


def test_validation_rejects_unrecognized_page():
    with pytest.raises(pes.PartialExportError) as exc:
        pes.validate_export_pages(
            [1, 6], kind="partial", page_count=6, failed_pages=set(), recognized_pages={1}
        )
    assert exc.value.code == "pages_not_recognized"


def test_validation_rejects_unsupported_type_for_partial():
    with pytest.raises(pes.PartialExportError) as exc:
        pes.validate_export_pages(
            [1], kind="partial", page_count=6, failed_pages=set(), recognized_pages={1}, file_type="docx"
        )
    assert exc.value.code == "partial_export_unsupported_type"


def test_validation_full_skips_type_gate():
    """#278 r2 F6：full 走主产物直取，类型闸会误杀 agent md/docx ⇒ full 跳过类型闸。"""
    result = pes.validate_export_pages(
        None, kind="full", page_count=0, failed_pages=set(), recognized_pages=set(), file_type="md"
    )
    assert result["pages"] == []
    assert result["excluded_failed_pages"] == []


def test_validation_full_default_set_is_exportable_and_excludes_failed():
    result = pes.validate_export_pages(
        None, kind="full", page_count=6, failed_pages={4}, recognized_pages={1, 2, 3, 5, 6}
    )
    assert result["pages"] == [1, 2, 3, 5, 6]
    assert result["excluded_failed_pages"] == [4]
    # full 忽略传入 pages（缺省语义 = 全部可导出页）
    ignored = pes.validate_export_pages(
        [1], kind="full", page_count=6, failed_pages={4}, recognized_pages={1, 2, 3, 5, 6}
    )
    assert ignored == result


# ---------------------------------------------------------------------------
# 说明.txt 内容（AC1.2 / AC5 红线：只允许页号、时间、原因码、导出 ID）
# ---------------------------------------------------------------------------

def test_summary_lines_list_pages_individually_and_never_leak_text():
    lines = pes.build_summary_lines(
        pages=[1, 2, 3, 5],
        page_count=6,
        excluded=[(4, "inj")],
        snapshot_at="2026-11-11T00:40:00+00:00",
        generated_at="2026-11-11T00:41:00+00:00",
        export_id="0123456789abcdef",
        recognized_pages_at_export=4,
    )
    text = "\n".join(lines)
    assert "第 1、2、3、5 页" in text
    assert "第 1-5 页" not in text  # 撒谎区间
    assert "共 4 页" in text
    assert "含说明页" not in text, "说明页废除后不得再出现 +1 语义（#278）"
    assert "原卷总页数：6" in text
    assert "0123456789abcdef" in text
    for line in lines:
        assert "secret-" not in line


# ---------------------------------------------------------------------------
# 评审 Important-1/2：失败清理 + 无框页拒收
# ---------------------------------------------------------------------------

def test_partial_export_cleans_unmasked_subset_when_render_fails(
    synthetic_pdf, output_dir, tmp_path, monkeypatch
):
    """Important-1：渲染任一环节抛错，未打码的选中页副本不得留在导出目录。"""
    from app.services import redactor as redactor_module

    boxes = [_box(p, f"b{p}") for p in (1, 2)]
    work = tmp_path / "work"

    async def _boom(self, *args, **kwargs):
        raise RuntimeError("render exploded")

    monkeypatch.setattr(redactor_module.Redactor, "redact", _boom)

    with pytest.raises(RuntimeError, match="render exploded"):
        asyncio.run(
            pes.build_item_package(
                kind="partial",
                source_path=synthetic_pdf,
                pages=[1, 2],
                boxes=boxes,
                config=_config(),
                out_dir=str(work),
                final_name="部分导出.pdf",
            )
        )

    leftovers = sorted(p.name for p in work.iterdir()) if work.exists() else []
    assert "subset.pdf" not in leftovers, f"未打码切片残留：{leftovers}"
    assert "部分导出.pdf" not in leftovers
    assert not [p for p in leftover_redacted(work)], "半成品打码件不得留下"


def leftover_redacted(work):
    if not work.exists():
        return []
    return [p for p in work.iterdir() if p.is_file() and p.name != "subset.pdf"]


def test_validation_rejects_pages_without_boxes():
    """Important-2：选中页没有框账条目 ⇒ 拒收（否则原样渲染、零遮蔽出厂）。"""
    with pytest.raises(pes.PartialExportError) as excinfo:
        pes.validate_export_pages(
            [1, 2],
            kind="partial",
            page_count=6,
            failed_pages=set(),
            recognized_pages={1, 2},
            file_type="pdf_scanned",
            boxes_pages={1},
        )
    assert excinfo.value.code == "pages_without_boxes"
    assert excinfo.value.detail["pages"] == [2]


def test_validation_accepts_pages_with_empty_boxes_entries():
    """空框列表（跑过视觉识别但零命中）仍然合法，不能被上面那条误杀。"""
    selection = pes.validate_export_pages(
        [1, 2],
        kind="partial",
        page_count=6,
        failed_pages=set(),
        recognized_pages={1, 2},
        file_type="pdf_scanned",
        boxes_pages={1, 2},
    )
    assert selection["pages"] == [1, 2]


def test_export_title_lists_pages_without_double_quantifier() -> None:
    """跳页标题：逐页列举，量词只出现一次（真实事故：界面出现过「等 9 页页」）。"""
    long_pages = [1, 2, 3, 5, 6, 7, 8, 9, 10]
    title = pes.build_export_title(filename="卷.pdf", pages=long_pages)
    assert title == "卷-部分导出(第1、2、3、5、6、7 等 9 页)"
    assert "页页" not in title

    short = pes.build_export_title(filename="卷.pdf", pages=[1, 3, 5])
    assert short == "卷-部分导出(第1、3、5页)"


def test_export_title_contiguous_stays_range() -> None:
    assert (
        pes.build_export_title(filename="卷.pdf", pages=range(1, 101))
        == "卷-部分导出(第1-100页)"
    )
    assert (
        pes.build_export_title(filename="卷.pdf", pages=[7])
        == "卷-部分导出(第7页)"
    )
