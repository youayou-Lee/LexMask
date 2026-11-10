"""Issue#257 一期 · AC1.2 / AC1.4 / AC1.5：部分导出的产物正确性与选页契约。

核心判据（门①.5 冷审后重写）：**逐页内嵌图像流 sha256 与「全量参照件」对照**。
参照件 = 对同一原件走既有 ``Redactor().redact`` 打码全卷（不经
``execute_redaction``、不写 file_store）。该判据一次覆盖：框页号重映射、产物页序、
渲染 DPI/JPEG 参数一致性、打码生效；再反向断言「产物页图像不等于参照件其它页」
防止页序错位却自洽的假绿。

另外钉住说明页的内容（逐页列举、不得写「第 1-5 页」这种撒谎区间）、失败页永不进包、
以及 AC1.5 的错误码全集。
"""

from __future__ import annotations

import asyncio
import hashlib

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
    """AC1.2：页数、说明页、逐页图像 sha256 全部对得上全量参照件。"""
    boxes = [_box(p, f"b{p}") for p in (1, 2, 3, 4, 5, 6)]
    reference = _reference_pdf(synthetic_pdf, boxes, output_dir)

    cover_lines = pes.build_cover_lines(
        pages=[1, 2, 3, 5],
        page_count=6,
        excluded=[(4, "inj")],
        snapshot_at="2026-11-11T00:40:00+00:00",
        generated_at="2026-11-11T00:41:00+00:00",
        export_id="0123456789abcdef",
        recognized_pages_at_export=4,
    )
    product = asyncio.run(
        pes.build_partial_package(
            source_path=synthetic_pdf,
            pages=[1, 2, 3, 5],
            boxes=boxes,
            config=_config(),
            cover_lines=cover_lines,
            out_dir=str(tmp_path / "work"),
            final_name="部分导出.pdf",
        )
    )

    doc = fitz.open(product["pdf_path"])
    try:
        assert doc.page_count == 5, "说明页 1 + 选中 4"
        cover_text = doc[0].get_text()
        assert "第 1、2、3、5 页" in cover_text.replace("\n", "")
        assert "共 4 页" in cover_text
        assert "原卷总页数：6" in cover_text
        assert "第 4 页" in cover_text and "inj" in cover_text
        assert "生成时间" in cover_text and "快照时间" in cover_text
        assert "MARKER-P6" not in cover_text
    finally:
        doc.close()

    # 逐页图像流对照：产物第 k+1 页 == 参照件第 pages[k] 页
    ref_shas = {p: _page_image_sha(reference, p - 1) for p in (1, 2, 3, 4, 5, 6)}
    for idx, page_no in enumerate([1, 2, 3, 5]):
        got = _page_image_sha(product["pdf_path"], idx + 1)
        assert got == ref_shas[page_no], f"page {page_no} image mismatch"
        others = [sha for p, sha in ref_shas.items() if p != page_no]
        assert got not in others, f"page {page_no} matches another reference page"


def test_partial_export_keeps_original_page_order(synthetic_pdf, output_dir, tmp_path):
    """D6：传入 [3,1] 也按原卷升序（产物第 2 页=原卷 1，第 3 页=原卷 3）。"""
    boxes = [_box(p, f"b{p}") for p in (1, 3)]
    reference = _reference_pdf(synthetic_pdf, boxes, output_dir)
    product = asyncio.run(
        pes.build_partial_package(
            source_path=synthetic_pdf,
            pages=[3, 1],
            boxes=boxes,
            config=_config(),
            cover_lines=["第 1、3 页"],
            out_dir=str(tmp_path / "work"),
            final_name="p.pdf",
        )
    )
    assert _page_image_sha(product["pdf_path"], 1) == _page_image_sha(reference, 0)
    assert _page_image_sha(product["pdf_path"], 2) == _page_image_sha(reference, 2)


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
        pes.validate_partial_export_pages(
            [], page_count=6, failed_pages=set(), recognized_pages={1, 2}, acknowledge_failed_excluded=False
        )
    assert exc.value.code == "pages_empty"


@pytest.mark.parametrize("bad", [0, -1, 7])
def test_validation_rejects_out_of_range(bad):
    with pytest.raises(pes.PartialExportError) as exc:
        pes.validate_partial_export_pages(
            [bad], page_count=6, failed_pages=set(), recognized_pages={1, 2}, acknowledge_failed_excluded=False
        )
    assert exc.value.code == "pages_out_of_range"


def test_validation_dedupes_and_allows_unsorted():
    result = pes.validate_partial_export_pages(
        [2, 1, 2], page_count=6, failed_pages=set(), recognized_pages={1, 2}, acknowledge_failed_excluded=False
    )
    assert result["pages"] == [1, 2]
    assert result["excluded_failed_pages"] == []


def test_validation_rejects_explicit_failed_page():
    with pytest.raises(pes.PartialExportError) as exc:
        pes.validate_partial_export_pages(
            [1, 4], page_count=6, failed_pages={4}, recognized_pages={1, 2, 3, 5}, acknowledge_failed_excluded=True
        )
    assert exc.value.code == "failed_pages_not_allowed"


def test_validation_requires_ack_when_failed_pages_within_span():
    with pytest.raises(pes.PartialExportError) as exc:
        pes.validate_partial_export_pages(
            [1, 2, 3, 5, 6], page_count=6, failed_pages={4}, recognized_pages={1, 2, 3, 5, 6},
            acknowledge_failed_excluded=False,
        )
    assert exc.value.code == "failed_pages_ack_required"
    assert exc.value.detail["failed_pages"] == [4]

    ok = pes.validate_partial_export_pages(
        [1, 2, 3, 5, 6], page_count=6, failed_pages={4}, recognized_pages={1, 2, 3, 5, 6},
        acknowledge_failed_excluded=True,
    )
    assert ok["excluded_failed_pages"] == [4]
    assert 4 not in ok["pages"]


def test_validation_rejects_unrecognized_page():
    with pytest.raises(pes.PartialExportError) as exc:
        pes.validate_partial_export_pages(
            [1, 6], page_count=6, failed_pages=set(), recognized_pages={1}, acknowledge_failed_excluded=False
        )
    assert exc.value.code == "pages_not_recognized"


def test_validation_rejects_unsupported_type():
    with pytest.raises(pes.PartialExportError) as exc:
        pes.validate_partial_export_pages(
            [1], page_count=6, failed_pages=set(), recognized_pages={1},
            acknowledge_failed_excluded=False, file_type="docx",
        )
    assert exc.value.code == "partial_export_unsupported_type"


# ---------------------------------------------------------------------------
# 说明页内容（AC1.2 / AC5 红线：只允许页号、时间、原因码、导出 ID）
# ---------------------------------------------------------------------------

def test_cover_lines_list_pages_individually_and_never_leak_text():
    lines = pes.build_cover_lines(
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
    assert "原卷总页数：6" in text
    assert "0123456789abcdef" in text
    for line in lines:
        assert "secret-" not in line


def test_cover_page_renders_chinese(synthetic_pdf, tmp_path):
    """AC0 的服务层形态：china-s 写入后逐字符读回。"""
    out = tmp_path / "with_cover.pdf"
    pes.slice_pdf_pages(synthetic_pdf, [1, 2], str(out))
    lines = ["本包含原卷第 1、2 页（共 2 页，其中说明页 1 页）", "原卷总页数：6"]
    pes.insert_cover_page(str(out), lines)
    doc = fitz.open(str(out))
    try:
        assert doc.page_count == 3
        text = doc[0].get_text()
        for line in lines:
            assert line in text
    finally:
        doc.close()
