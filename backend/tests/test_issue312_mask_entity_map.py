"""Issue#312：扫描件/图片打码链路补写「已打码」映射（entity_map）。

背景：图片链路（PDF_SCANNED / IMAGE，含文本型 PDF 的 MASK 栅格化）只做像素
遮挡、从不调用 ``RedactionContext.get_replacement``，于是 `entity_map` 恒为空，
右栏「映射记录」显示 0 行（86 页扫描卷 1598 框 / 655 个去重原文全丢）。

本文件钉住五件事：
1. 图片链路完成后 ``entity_map`` = {参与遮挡框的去重非空原文: "已打码"}；
2. 空/纯空白原文不入表；``selected=false`` 的框不入表且不计入 ``redacted_count``
   （``redacted_count`` 语义不变，与 map 同源）；
3. 文本替换链路 ``entity_map`` 与预览口径（``build_preview_entity_map``）逐键相等，
   哨兵词不出现——文本链路零漂移；
4. 编排层把同一份 map 写入文件快照与 ``redaction_history``（刷新/任务中心回场同源，
   不依赖 live 会话内存）；
5. 安全边界：mask map 不含化名/占位符形态（化名 CSV 门控源不被污染），
   restore 端点只消费请求体 ``mapping``，不读文件快照 ``entity_map``。
"""

from __future__ import annotations

import asyncio
from typing import Any

import fitz
import pytest
from PIL import Image as PILImage

from app.core.config import settings
from app.models.entity_schemas import BoundingBox, Entity
from app.models.redaction_schemas import RedactionConfig, RedactionRequest
from app.services import redaction_orchestrator as orch
from app.services.redaction.replacement_strategy import build_preview_entity_map
from app.services.redactor import Redactor

# Issue#312 契约：图片链路的映射值（不是替换词，是「此处已遮挡」的事实记录）
REDACTED = "已打码"


@pytest.fixture()
def _dirs(tmp_path, monkeypatch):
    up = tmp_path / "uploads"
    out = tmp_path / "outputs"
    up.mkdir()
    out.mkdir()
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(up))
    monkeypatch.setattr(settings, "OUTPUT_DIR", str(out))
    return up, out


def _box(
    box_id: str,
    text: str | None,
    *,
    page: int = 1,
    x: float = 0.10,
    y: float = 0.10,
    width: float = 0.20,
    height: float = 0.06,
    selected: bool = True,
) -> BoundingBox:
    return BoundingBox(
        id=box_id, x=x, y=y, width=width, height=height, page=page,
        type="PERSON", text=text, selected=selected,
    )


def _make_pdf(path) -> None:
    doc = fitz.open()
    page = doc.new_page(width=300, height=400)
    page.insert_text((24, 40), "scan canary", fontsize=10)
    doc.save(str(path))
    doc.close()


def _make_png(path) -> None:
    PILImage.new("RGB", (64, 64), (255, 255, 255)).save(path)


async def _redact(**kwargs):
    return await Redactor().redact(**kwargs)


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# R0：哨兵常量与 Issue 契约一致（防止实现另起字面量）
# ---------------------------------------------------------------------------


def test_sentinel_constant_matches_issue_contract() -> None:
    from app.services.redaction.image_redactor import IMAGE_REDACTED_LABEL

    assert IMAGE_REDACTED_LABEL == REDACTED


# ---------------------------------------------------------------------------
# R1：扫描件（PDF_SCANNED）—— 非空、去重、值恒哨兵、count 语义不变
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scanned_mask_entity_map_nonempty_and_dedup(_dirs) -> None:
    up, _ = _dirs
    src = up / "scan.pdf"
    _make_pdf(src)
    # 3 个遮挡框、2 个去重原文：map 2 行 / count 3 处（Issue 表头双口径）
    boxes = [
        _box("b1", "张三", y=0.10),
        _box("b2", "李四", y=0.20),
        _box("b3", "张三", y=0.30),
    ]
    result = await _redact(
        file_info={"file_path": str(src), "file_type": "pdf_scanned", "is_scanned": True},
        entities=[],
        bounding_boxes=boxes,
        config=RedactionConfig(replacement_mode="mask"),
    )
    assert result["entity_map"] == {"张三": REDACTED, "李四": REDACTED}
    assert result["redacted_count"] == 3, "count 仍是参与遮挡的框数（不去重）"
    assert len(result["entity_map"]) < result["redacted_count"], "去重后行数应少于处数"


# ---------------------------------------------------------------------------
# R2：单图（IMAGE）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_image_mask_entity_map_from_boxes(_dirs) -> None:
    up, _ = _dirs
    src = up / "id.png"
    _make_png(src)
    boxes = [_box("b1", "王五", width=0.30, height=0.20)]
    result = await _redact(
        file_info={"file_path": str(src), "file_type": "image"},
        entities=[],
        bounding_boxes=boxes,
        config=RedactionConfig(replacement_mode="mask"),
    )
    assert result["entity_map"] == {"王五": REDACTED}
    assert result["redacted_count"] == 1


# ---------------------------------------------------------------------------
# R3：空文本过滤（None / 空串 / 半角空白 / 全角空白）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mask_entity_map_skips_blank_text(_dirs) -> None:
    up, _ = _dirs
    src = up / "scan.pdf"
    _make_pdf(src)
    boxes = [
        _box("b1", None, y=0.05),
        _box("b2", "", y=0.10),
        _box("b3", "   ", y=0.15),
        _box("b4", "\u3000\t", y=0.20),
        _box("b5", "  王五  ", y=0.25),
    ]
    result = await _redact(
        file_info={"file_path": str(src), "file_type": "pdf_scanned", "is_scanned": True},
        entities=[],
        bounding_boxes=boxes,
        config=RedactionConfig(replacement_mode="mask"),
    )
    assert result["entity_map"] == {"王五": REDACTED}, "空白/None 原文不入表，非空原文 strip 后入表"
    assert result["redacted_count"] == 5, "空白框仍被遮挡，count 不受 map 过滤影响"


# ---------------------------------------------------------------------------
# R4：未参与遮挡（selected=false）的框不入表
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mask_entity_map_excludes_unselected_boxes(_dirs) -> None:
    up, _ = _dirs
    src = up / "scan.pdf"
    _make_pdf(src)
    boxes = [
        _box("b1", "张三", y=0.10, selected=True),
        _box("b2", "李四", y=0.20, selected=False),
    ]
    result = await _redact(
        file_info={"file_path": str(src), "file_type": "pdf_scanned", "is_scanned": True},
        entities=[],
        bounding_boxes=boxes,
        config=RedactionConfig(replacement_mode="mask"),
    )
    assert result["entity_map"] == {"张三": REDACTED}
    assert result["redacted_count"] == 1, "未选中框不遮挡、不计 count（既有语义）"


# ---------------------------------------------------------------------------
# R5：文本型 PDF 的 MASK 栅格化路径（实体定位框也入表）
# ---------------------------------------------------------------------------


def _entity_pdf(path) -> None:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text(
        (72, 130), "委托人：陈文清，身份证号 110101199001019999", fontsize=12, fontname="china-s"
    )
    doc.save(str(path))
    doc.close()


@pytest.mark.asyncio
async def test_text_pdf_mask_rasterized_entity_map(_dirs) -> None:
    up, _ = _dirs
    src = up / "t.pdf"
    _entity_pdf(src)
    entities = [
        Entity(id="e1", text="陈文清", type="PERSON", start=0, end=3, page=1, selected=True),
        Entity(id="e2", text="110101199001019999", type="ID_CARD", start=0, end=18, page=1, selected=True),
    ]
    result = await _redact(
        file_info={"file_path": str(src), "file_type": "pdf"},
        entities=entities,
        bounding_boxes=[],
        config=RedactionConfig(replacement_mode="mask"),
    )
    assert set(result["entity_map"]) == {"陈文清", "110101199001019999"}
    assert set(result["entity_map"].values()) == {REDACTED}
    assert result["redacted_count"] >= 2, "实体定位框仍按框计数（语义不变）"
    doc = fitz.open(result["output_path"])
    try:
        text = doc.load_page(0).get_text()
    finally:
        doc.close()
    assert "陈文清" not in text and "110101199001019999" not in text, "栅格化后原文不在文本层"


# ---------------------------------------------------------------------------
# R6：文本替换链路零漂移 —— map 与预览口径逐键相等，哨兵不出现
# ---------------------------------------------------------------------------


def _make_txt(path) -> str:
    content = "委托人：陈文清，身份证号 110101199001019999\n"
    path.write_text(content, encoding="utf-8")
    return content


def _text_entities() -> list[Entity]:
    return [
        Entity(id="e1", text="陈文清", type="PERSON", start=0, end=3, page=1, selected=True),
        Entity(id="e2", text="110101199001019999", type="ID_CARD", start=0, end=18, page=1, selected=True),
    ]


@pytest.mark.asyncio
async def test_text_path_entity_map_unchanged(_dirs) -> None:
    up, _ = _dirs
    src = up / "a.txt"
    _make_txt(src)
    entities = _text_entities()
    config = RedactionConfig(replacement_mode="smart")
    result = await _redact(
        file_info={"file_path": str(src), "file_type": "txt"},
        entities=entities,
        bounding_boxes=[],
        config=config,
    )
    preview_map = build_preview_entity_map(entities, config)
    assert result["entity_map"] == preview_map, "文本链路的执行映射必须与预览口径逐键相等"
    assert set(result["entity_map"]) == {"陈文清", "110101199001019999"}
    assert REDACTED not in result["entity_map"].values(), "图片链路的哨兵不得泄漏到文本链路"
    assert result["redacted_count"] == 2


# ---------------------------------------------------------------------------
# R7：编排层同一 map 落快照 + redaction_history（刷新/回场同源）
# ---------------------------------------------------------------------------


class _StubStore(dict):
    def set(self, key, value):  # noqa: A003 - 与真实 file_store 契约同名
        self[key] = value


@pytest.mark.asyncio
async def test_redaction_history_carries_same_map(_dirs, monkeypatch) -> None:
    up, _ = _dirs
    src = up / "scan.pdf"
    _make_pdf(src)
    boxes = [_box("b1", "张三", y=0.10), _box("b2", "李四", y=0.20)]
    store = _StubStore(
        {"f312": {"file_type": "pdf_scanned", "file_path": str(src), "is_scanned": True}}
    )
    monkeypatch.setattr(orch, "_get_file_store", lambda: store)
    monkeypatch.setattr(orch, "_get_file_store_lock", lambda: asyncio.Lock())

    response = await orch.execute_redaction(
        RedactionRequest(
            file_id="f312", entities=[], bounding_boxes=boxes,
            config=RedactionConfig(replacement_mode="mask"),
        )
    )
    info = store["f312"]
    expected = {"张三": REDACTED, "李四": REDACTED}
    assert response.entity_map == expected, "执行响应必须带同一份 map"
    assert info["entity_map"] == expected, "文件快照落库同源（GET /files/{id} 唯一口径）"
    history = info["redaction_history"]
    assert len(history) == 1
    assert history[-1]["entity_map"] == expected, "redaction_history 与快照同一 map"
    assert history[-1]["mode"] == "mask"


# ---------------------------------------------------------------------------
# R8：安全边界 —— mask map 无化名形态；restore 只吃请求体 mapping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_restore_does_not_consume_mask_map(_dirs, monkeypatch):
    from app.services import restore_service

    up, _ = _dirs
    src = up / "scan.pdf"
    _make_pdf(src)
    result = await _redact(
        file_info={"file_path": str(src), "file_type": "pdf_scanned", "is_scanned": True},
        entities=[],
        bounding_boxes=[_box("b1", "张三", y=0.10), _box("b2", "110101199001019999", y=0.20)],
        config=RedactionConfig(replacement_mode="mask"),
    )
    mask_map = result["entity_map"]
    assert mask_map, "前置条件：mask 链路确实产出了映射"
    # 化名 CSV 的判据源：值若是化名/占位符形态，前端 CSV 才可能被误用
    for value in mask_map.values():
        assert value == REDACTED
        assert not restore_service.PLACEHOLDER_RE.fullmatch(value)
        assert not restore_service.PSEUDONYM_RE.fullmatch(value)
        assert not restore_service._BARE_PLACEHOLDER_RE.fullmatch(value)

    # restore 的唯一输入是请求体 mapping（结构钉 + 行为钉）
    import inspect

    import app.api.vlmd as vlmd

    source = inspect.getsource(vlmd.restore_text)
    assert "entity_map" not in source and "file_store" not in source, "restore 不得读文件快照"

    captured: dict[str, Any] = {}
    real_normalize = vlmd.normalize_mapping

    def _spy(raw):
        captured["raw"] = raw
        return real_normalize(raw)

    monkeypatch.setattr(vlmd, "normalize_mapping", _spy)
    uploaded = {"[人名一]": {"texts": ["张三"]}}
    response = await vlmd.restore_text(
        vlmd.RestoreBody(text="嫌疑人[人名一]供述", mapping=uploaded), owner_id="tester"
    )
    assert captured["raw"] == uploaded, "normalize_mapping 只收到请求体 mapping（不并入快照 map）"
    assert set(captured["raw"]) == {"[人名一]"}, "入参键集必须与请求体一致，无附加来源"
    assert response.restored_text == "嫌疑人张三供述"
    assert response.restored_count == 1


# ---------------------------------------------------------------------------
# R9：反向对照 —— 化名模式 map 值仍是化名词（CSV 门控源未受污染）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pseudonym_text_path_map_not_sentinel(_dirs) -> None:
    up, _ = _dirs
    src = up / "a.txt"
    _make_txt(src)
    pools = {"PERSON": {"words": ["王某"], "strategy": "numbered", "custom_map": {}}}
    result = await _redact(
        file_info={"file_path": str(src), "file_type": "txt"},
        entities=_text_entities(),
        bounding_boxes=[],
        config=RedactionConfig(replacement_mode="pseudonym", word_pools=pools),
    )
    assert result["entity_map"]["陈文清"] == "王某"
    assert REDACTED not in result["entity_map"].values()
