"""Issue#275 B2：``run_has_text_analysis`` 邻居上下文注入点测试（设计稿 §2.4/§5）。

钉住：注入后首组文本含 prev_tail / 末组含 next_head；too_short 闸先于注入
（变异 6 正向钉）；metrics 五键落 stage_status（M-3/M-4）；组失败/负健康仍抛
``HasTextNerUnavailable``（#263 钉）；残差 pass 文本不含邻居（上下文只进主查）。
"""

from __future__ import annotations

import asyncio
import time

import pytest

from app.core.config import settings
from app.services import page_neighbor_context as pnc
from app.services.ocr_has_vision_service import OCRTextBlock
from app.services.vision.has_text_analysis import (
    HasTextNerUnavailable,
    run_has_text_analysis,
)


@pytest.fixture(autouse=True)
def _clean_registry():
    pnc.reset_for_tests()
    yield
    pnc.reset_for_tests()


def _block(text: str, top: int) -> OCRTextBlock:
    return OCRTextBlock(
        text=text,
        polygon=[[100, top], [600, top], [600, top + 30], [100, top + 30]],
        confidence=0.98,
    )


class _RecordingHaS:
    """记录探测文本与实发文本的最小桩（探测恒 miss）。"""

    def __init__(self):
        self.ner_texts: list[str] = []
        self.probe_texts: list[str] = []

    def get_cached_ner(self, text, types):
        self.probe_texts.append(text)
        return None

    def ner(self, text, labels=None, **_kwargs):
        self.ner_texts.append(text)
        return {"姓名": ["张三"]}


_OWN_TEXT = "张三"
_NEIGHBOR_PREV = "前页末尾当事人李四于二〇二四年五月签名"
_NEIGHBOR_NEXT = "后页开头案号（2024）京01民终1234号续页"


def _activate_with_neighbors(page: int, path: str):
    # G4：路径由调用方用 tmp_path 拼接（本机绝对路径字面量不得进公开树）；
    # 「nonexistent」语义 = tmp_path 下不创建该文件（registry/probe 不依赖真实文件）。
    pnc.store_page_text(path, page - 1, _NEIGHBOR_PREV, source="text_layer")
    pnc.store_page_text(path, page + 1, _NEIGHBOR_NEXT, source="text_layer")
    return pnc.activate(path, page)


def test_neighbor_injected_into_single_group_text(tmp_path):
    token = _activate_with_neighbors(2, str(tmp_path / "nonexistent-issue275.pdf"))
    try:
        client = _RecordingHaS()
        stage: dict = {}
        blocks = [_block(_OWN_TEXT, 100)]
        entities = asyncio.run(
            run_has_text_analysis(blocks, client, vision_types=None, stage_status=stage)
        )
    finally:
        pnc.clear(token)
    assert len(client.ner_texts) == 1
    group_text = client.ner_texts[0]
    assert group_text.startswith(_NEIGHBOR_PREV), "首组文本必须以 prev_tail 开头"
    assert group_text.endswith(_NEIGHBOR_NEXT), "单组文本必须以 next_head 结尾"
    assert _OWN_TEXT in group_text, "本页文本本体必须在组文本里"
    assert {e["text"] for e in entities} == {"张三"}


def test_neighbor_not_in_payload_texts_but_in_probe(tmp_path):
    token = _activate_with_neighbors(2, str(tmp_path / "nonexistent-issue275.pdf"))
    try:
        client = _RecordingHaS()
        stage: dict = {}
        blocks = [_block(_OWN_TEXT, 100)]
        asyncio.run(run_has_text_analysis(blocks, client, vision_types=None, stage_status=stage))
    finally:
        pnc.clear(token)
    # 缓存探测文本 = 实发组文本（F-1 逐字同源）
    assert client.probe_texts[0] == client.ner_texts[0]
    # metrics 五键（M-3/M-4）
    assert stage["has_text_neighbor_context"] == "neighbor"
    assert stage["has_text_neighbor_chars"] == len(_NEIGHBOR_PREV) + len(_NEIGHBOR_NEXT)
    assert stage["has_text_neighbor_prev_source"] == "registry:text_layer"
    assert stage["has_text_neighbor_next_source"] == "registry:text_layer"
    assert "has_text_neighbor_error" not in stage


def test_chars_zero_makes_off_and_no_injection(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "RECOGNITION_NEIGHBOR_CONTEXT_CHARS", 0, raising=False)
    token = _activate_with_neighbors(2, str(tmp_path / "nonexistent-issue275.pdf"))
    try:
        client = _RecordingHaS()
        stage: dict = {}
        blocks = [_block(_OWN_TEXT, 100)]
        asyncio.run(run_has_text_analysis(blocks, client, vision_types=None, stage_status=stage))
    finally:
        pnc.clear(token)
    assert client.ner_texts[0] == _OWN_TEXT
    assert stage["has_text_neighbor_context"] == "off"
    assert stage["has_text_neighbor_chars"] == 0


def test_inactive_yields_none_status():
    client = _RecordingHaS()
    stage: dict = {}
    blocks = [_block(_OWN_TEXT, 100)]
    asyncio.run(run_has_text_analysis(blocks, client, vision_types=None, stage_status=stage))
    assert stage["has_text_neighbor_context"] == "none"
    assert _OWN_TEXT in client.ner_texts[0]
    assert _NEIGHBOR_PREV not in client.ner_texts[0]


def test_too_short_gate_precedes_injection(tmp_path, monkeypatch):
    """变异 6 正向钉：空/过短页 + 肥邻居 ⇒ 仍判 skipped_too_short、不发请求。"""
    monkeypatch.setattr(settings, "HAS_VISION_MIN_TEXT_CHARS_FOR_NER", 50, raising=False)
    token = _activate_with_neighbors(2, str(tmp_path / "nonexistent-issue275.pdf"))
    try:
        client = _RecordingHaS()
        stage: dict = {}
        blocks = [_block("短文本", 100)]
        entities = asyncio.run(
            run_has_text_analysis(blocks, client, vision_types=None, stage_status=stage)
        )
    finally:
        pnc.clear(token)
    assert entities == []
    assert stage["has_text_cache_status"] == "skipped_too_short"
    assert client.ner_texts == [], "too_short 页不得因邻居文本被顶过闸"
    assert "has_text_neighbor_context" not in stage, "注入点在 too_short 闸之后"


def test_no_eligible_text_page_skips_before_injection(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "HAS_VISION_MIN_TEXT_CHARS_FOR_NER", 50, raising=False)
    token = _activate_with_neighbors(2, str(tmp_path / "nonexistent-issue275.pdf"))
    try:
        client = _RecordingHaS()
        stage: dict = {}
        blocks = [_block("", 100)]
        entities = asyncio.run(
            run_has_text_analysis(blocks, client, vision_types=None, stage_status=stage)
        )
    finally:
        pnc.clear(token)
    assert entities == []
    assert stage["has_text_cache_status"] == "skipped_no_eligible_text"
    assert client.ner_texts == []


def test_negative_health_still_raises_with_neighbor_attached(tmp_path):
    """#263 钉：邻居附着页的负健康升格不降级——仍抛 HasTextNerUnavailable。"""
    token = _activate_with_neighbors(2, str(tmp_path / "nonexistent-issue275.pdf"))
    try:
        client = _RecordingHaS()
        client._health_checked_at = time.monotonic()
        client._health_ready = False
        stage: dict = {}
        blocks = [_block(_OWN_TEXT, 100)]
        with pytest.raises(HasTextNerUnavailable, match="ner_unavailable"):
            asyncio.run(
                run_has_text_analysis(blocks, client, vision_types=None, stage_status=stage)
            )
    finally:
        pnc.clear(token)
    assert stage["has_text_cache_status"] == "skipped_recent_unavailable"


_DENSE = "370702199001014521"


class _TruncatedMainHaS:
    """issue209 同款：主查只认领第一个真值，其余墨迹成为残差。"""

    def __init__(self):
        self.calls: list[str] = []

    def ner(self, text, labels=None, **_kwargs):
        self.calls.append(text)
        if _DENSE in "".join(text.split()):
            return {"身份证号": [_DENSE]}
        return {}


def _small_residual_page_blocks() -> list[OCRTextBlock]:
    """主查可认领第一块，残差 = 第二块（绝对量小，残差双闸放行 ⇒ 必重问）。"""
    return [
        _block(f"报关单号 {_DENSE}", 100),
        _block("备注栏其他无关墨迹内容若干", 140),
    ]


def test_residual_pass_text_excludes_neighbor(tmp_path):
    """上下文只进主查：残差 pass 重问的文本不含邻居（捕获全部 client 文本断言）。"""
    path = str(tmp_path / "nonexistent-issue275-residual.pdf")
    marker_prev = "NEIGHBOR_MARK_前页" + "邻" * 180
    pnc.store_page_text(path, 1, marker_prev, source="text_layer")
    token = pnc.activate(path, 2)
    try:
        client = _TruncatedMainHaS()
        stage: dict = {}
        asyncio.run(
            run_has_text_analysis(_small_residual_page_blocks(), client, vision_types=None, stage_status=stage)
        )
    finally:
        pnc.clear(token)
    assert len(client.calls) >= 2, "小残差页必须重问残差 pass"
    with_neighbor = [t for t in client.calls if "NEIGHBOR_MARK_前页" in t]
    assert len(with_neighbor) == 1, (
        f"邻居文本只准进主查一处（实际 {len(with_neighbor)} 处）；"
        f"各调用长度={[len(t) for t in client.calls]}"
    )
    assert client.calls[0] == with_neighbor[0]
    # 残差 pass（第二次起的调用）不含邻居
    assert all("NEIGHBOR_MARK_前页" not in t for t in client.calls[1:])
