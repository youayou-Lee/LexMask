"""Issue#321：流式页子集匿名化（已处理页即可开始，产物只含该子集）。"""

from __future__ import annotations

import asyncio
import hashlib

import fitz
import pytest

from app.core.config import settings
from app.models.entity_schemas import BoundingBox
from app.models.redaction_schemas import RedactionConfig, RedactionRequest
from app.services import redaction_orchestrator as orch
from app.services.redaction_orchestrator import SubsetRedactionError, execute_redaction


class _StubStore(dict):
    def set(self, key, value):
        self[key] = value


def _make_pdf(path, pages: int = 3) -> None:
    doc = fitz.open()
    for i in range(pages):
        page = doc.new_page(width=200, height=280)
        page.insert_text((20, 30 + i * 24), f"P{i + 1} UNIQUE-{i + 1}", fontsize=12)
        page.draw_rect(fitz.Rect(120, 60 + i * 40, 180, 90 + i * 40), color=(1, 0, 0), fill=(1, 0, 0))
    doc.save(str(path))
    doc.close()


def _box(page: int, text: str) -> BoundingBox:
    return BoundingBox(
        id=f"b{page}", x=0.1, y=0.5, width=0.2, height=0.1,
        page=page, type="PERSON", text=text, selected=True,
    )


@pytest.fixture()
def env(tmp_path, monkeypatch):
    up = tmp_path / "up"
    out = tmp_path / "out"
    up.mkdir()
    out.mkdir()
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(up))
    monkeypatch.setattr(settings, "OUTPUT_DIR", str(out))
    src = up / "src.pdf"
    _make_pdf(src, 3)
    store = _StubStore(
        {
            "f-1": {
                "file_id": "f-1",
                "file_path": str(src),
                "file_type": "pdf_scanned",
                "is_scanned": True,
                "page_count": 3,
                "owner_id": "admin",
            }
        }
    )
    monkeypatch.setattr(orch, "_get_file_store", lambda: store)
    monkeypatch.setattr(orch, "_get_file_store_lock", lambda: asyncio.Lock())
    return store, src


def _page_shas(pdf_path: str) -> list[str]:
    shas = []
    with fitz.open(pdf_path) as doc:
        for page in doc:
            shas.append(hashlib.sha256(page.get_pixmap(dpi=72).samples).hexdigest())
    return shas


@pytest.mark.asyncio
async def test_subset_outputs_only_selected_pages_and_records_map(env) -> None:
    store, _ = env
    boxes = [_box(1, "甲"), _box(2, "乙"), _box(3, "丙")]
    # 先全卷跑一次，取页 1/3 的参照像素（同框同 config，子集应逐像素相同）
    full = await execute_redaction(
        RedactionRequest(
            file_id="f-1", entities=[], bounding_boxes=boxes,
            config=RedactionConfig(replacement_mode="mask"),
        )
    )
    full_shas = _page_shas(store["f-1"]["output_path"])

    result = await execute_redaction(
        RedactionRequest(
            file_id="f-1",
            entities=[],
            bounding_boxes=boxes,
            config=RedactionConfig(replacement_mode="mask"),
            pages=[3, 1],
        )
    )

    out = store["f-1"]["output_path"]
    with fitz.open(out) as doc:
        assert doc.page_count == 2, "产物必须只含子集页"

    # 原卷升序：产物页 1 = 原卷页 1、产物页 2 = 原卷页 3，与全卷同页逐像素一致
    assert _page_shas(out) == [full_shas[0], full_shas[2]]

    info = store["f-1"]
    assert info["processed_pages"] == [1, 3]
    assert info["page_map"] == {"1": 1, "2": 3}
    assert info["entity_map"] == {"甲": "已打码", "丙": "已打码"}, "子集外的页 2 不得入映射"
    assert info["redacted_count"] == 2
    # 框账保留原卷页号（供后续继续处理/导出）
    assert set(info["bounding_boxes"]) == {1, 2, 3}
    last = info["redaction_history"][-1]
    assert last["processed_pages"] == [1, 3]
    assert last["page_map"] == {"1": 1, "2": 3}
    assert result.processed_pages == [1, 3]
    assert result.page_map == {1: 1, 2: 3}


@pytest.mark.asyncio
async def test_full_redaction_clears_subset_metadata(env) -> None:
    store, _ = env
    boxes = [_box(1, "甲"), _box(2, "乙"), _box(3, "丙")]
    await execute_redaction(
        RedactionRequest(
            file_id="f-1", entities=[], bounding_boxes=boxes,
            config=RedactionConfig(replacement_mode="mask"), pages=[1],
        )
    )
    assert store["f-1"]["processed_pages"] == [1]

    await execute_redaction(
        RedactionRequest(
            file_id="f-1", entities=[], bounding_boxes=boxes,
            config=RedactionConfig(replacement_mode="mask"),
        )
    )
    info = store["f-1"]
    assert info["processed_pages"] == []
    assert info["page_map"] == {}
    with fitz.open(info["output_path"]) as doc:
        assert doc.page_count == 3


@pytest.mark.asyncio
async def test_subset_rejects_out_of_range(env) -> None:
    _, _ = env
    with pytest.raises(SubsetRedactionError):
        await execute_redaction(
            RedactionRequest(
                file_id="f-1", entities=[], bounding_boxes=[_box(1, "甲")],
                config=RedactionConfig(replacement_mode="mask"), pages=[4],
            )
        )


@pytest.mark.asyncio
async def test_subset_rejects_replace_mode(env) -> None:
    _, _ = env
    with pytest.raises(SubsetRedactionError):
        await execute_redaction(
            RedactionRequest(
                file_id="f-1", entities=[], bounding_boxes=[_box(1, "甲")],
                config=RedactionConfig(replacement_mode="smart"), pages=[1],
            )
        )
