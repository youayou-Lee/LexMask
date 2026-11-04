"""Issue#243/#245：水印预处理命中 ⇒ 文档级链（docline）弃权，回落逐页线。

62（证据材料卷51）实测形态（2026-11-05）：探针命中（可见水印流已摘）但文字层里
仍有可读水印文字（4 字「百色市院」×65/页，恰低于 #203 重复片段闸的 ≥10 字长词
判据 ⇒ 闸放行）⇒ docline 吃原件文字层建链（872 实体 / 650 个水印框）。
判据：探针 `code=hit` 的卷，docline 必须在 NER 之前拒绝（机读码
`watermark_overlay_hit`、permanent 负缓存）；未命中/关闭时 docline 行为不变。
"""

from __future__ import annotations

import asyncio

import pytest

from app.core.config import settings
from app.services import text_layer_doc_source as source
from app.services import watermark_overlay


class _RecordingClient:
    def __init__(self):
        self.ner_calls = 0

    def ner(self, text, labels=None, **kwargs):
        self.ner_calls += 1
        return {}

    def ner_checked(self, text, labels=None, **kwargs):
        self.ner_calls += 1
        return {}, True


def _patch_extract(monkeypatch):
    monkeypatch.setattr(source.docline, "extract_text_layer_pages", lambda _p, **_k: [])
    monkeypatch.setattr(source.docline, "sample_ink_alignment", lambda *a, **k: 0.5)


def _patch_render(monkeypatch, status: dict, copy_path: str | None = None):
    monkeypatch.setattr(
        watermark_overlay, "render_source", lambda _path: (copy_path, dict(status))
    )


# 假副本路径不写成绝对路径字面量（G4 abs_path 规则扫源码）；运行时用 tmp_path 拼。
def _fake_copy(tmp_path) -> str:
    return str(tmp_path / "clean-copy.pdf")


def _run_build(monkeypatch, tmp_path, client):
    _patch_extract(monkeypatch)
    monkeypatch.setattr(source, "has_client", client)
    path = tmp_path / "scan.pdf"
    path.write_bytes(b"%PDF-1.4 stub")
    return asyncio.run(
        source._build(
            "a" * 8,
            {"file_path": str(path), "file_type": "pdf_scanned", "is_scanned": True},
            ["人名", "编号"],
            None,
            None,
        )
    )


def test_probe_hit_refuses_docline_before_ner(monkeypatch, tmp_path):
    """探针命中 ⇒ docline 在 NER 之前拒绝，NER 调用数 = 0，回落逐页线。"""
    monkeypatch.setattr(settings, "WATERMARK_OVERLAY_STRIP_ENABLED", True)
    _patch_render(monkeypatch, {"code": "hit", "source": "copy"}, copy_path=_fake_copy(tmp_path))
    client = _RecordingClient()
    with pytest.raises(source._TransientIneligible) as excinfo:
        _run_build(monkeypatch, tmp_path, client)
    assert excinfo.value.code == "watermark_overlay_hit"
    assert client.ner_calls == 0, "闸必须在 NER 之前生效"


def test_probe_hit_refusal_is_permanent_negative_cache(monkeypatch, tmp_path):
    """拒绝落 permanent 负缓存（retry_at=inf）：重试不重建、回落逐页线照常工作。"""
    monkeypatch.setattr(settings, "WATERMARK_OVERLAY_STRIP_ENABLED", True)
    _patch_render(monkeypatch, {"code": "hit", "source": "copy"}, copy_path=_fake_copy(tmp_path))
    path = tmp_path / "scan.pdf"
    path.write_bytes(b"%PDF-1.4 stub")
    source.reset_memo()
    boxes, memo = asyncio.run(
        source.boxes_for_page(
            file_id="b" * 8,
            snapshot={"file_path": str(path), "file_type": "pdf_scanned", "is_scanned": True},
            page=1,
            ocr_has_types=None,
        )
    )
    assert boxes is None
    assert memo is not None and memo.get("unusable") is True
    assert memo.get("reason_code") == "watermark_overlay_hit"
    assert memo.get("retry_at") == float("inf")


def test_no_hit_keeps_docline_routing(monkeypatch, tmp_path):
    """未命中（no_wm_spans）⇒ 闸放行，docline 走既有资格链（此处落到无文字层）。"""
    monkeypatch.setattr(settings, "WATERMARK_OVERLAY_STRIP_ENABLED", True)
    _patch_render(monkeypatch, {"code": "no_wm_spans", "source": "original"})
    client = _RecordingClient()
    with pytest.raises(source._TransientIneligible) as excinfo:
        _run_build(monkeypatch, tmp_path, client)
    assert excinfo.value.code != "watermark_overlay_hit"


def test_strip_disabled_gate_is_inert(monkeypatch, tmp_path):
    """识别面关闭 ⇒ render_source 返回 disabled ⇒ 闸不生效（docline 行为不变）。"""
    monkeypatch.setattr(settings, "WATERMARK_OVERLAY_STRIP_ENABLED", False)
    _patch_render(monkeypatch, {"code": "disabled", "source": "original"})
    client = _RecordingClient()
    with pytest.raises(source._TransientIneligible) as excinfo:
        _run_build(monkeypatch, tmp_path, client)
    assert excinfo.value.code != "watermark_overlay_hit"


def test_render_source_failure_is_fail_open(monkeypatch, tmp_path):
    """render_source 异常 ⇒ 闸放行（fail-open，不改变 docline 既有路由）。"""
    monkeypatch.setattr(settings, "WATERMARK_OVERLAY_STRIP_ENABLED", True)

    def _boom(_path):
        raise RuntimeError("disk unavailable")

    monkeypatch.setattr(watermark_overlay, "render_source", _boom)
    client = _RecordingClient()
    with pytest.raises(source._TransientIneligible) as excinfo:
        _run_build(monkeypatch, tmp_path, client)
    assert excinfo.value.code != "watermark_overlay_hit"
