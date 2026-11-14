"""Issue#275 B2：缓存键隔离测试（设计稿 §3.2/F-1/变异 3）。

单一不变式：**缓存探测文本 ≡ 实发文本**（键 = sha256(text) 单点公式）。
判据：组数==1 时 probe_text 的 sha256 == 实发组文本 sha256；chars=0 时
probe_text == text_content 逐字相等（现状快路径回归钉）；同页同邻居二次
hit_before_slot；同页异邻居 miss；跨页同文本异邻居不串。
"""

from __future__ import annotations

import asyncio
import hashlib

import pytest

from app.core.config import settings
from app.services import page_neighbor_context as pnc
from app.services.ocr_has_vision_service import OCRTextBlock
from app.services.vision.has_text_analysis import run_has_text_analysis


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


_OWN_TEXT = "张三"  # 纯值块：主查认领后整块被消费（⊂ value），不触发残差重问噪声
_PREV_A = "前页邻居A" + "甲" * 60
_NEXT_A = "后页邻居A" + "乙" * 60
_PREV_B = "前页邻居B" + "丙" * 60


class _CachingHaS:
    """带真缓存语义的桩：get_cached_ner 按 (text, types) 命中；ner 回填缓存。"""

    def __init__(self):
        self.ner_texts: list[str] = []
        self.probe_texts: list[str] = []
        self._cache: dict[tuple[str, tuple[str, ...]], dict] = {}

    def get_cached_ner(self, text, types):
        self.probe_texts.append(text)
        return self._cache.get((text, tuple(types or ())))

    def ner(self, text, labels=None, **_kwargs):
        self.ner_texts.append(text)
        result = {"姓名": ["张三"]}
        self._cache[(text, tuple(labels or ()))] = result
        return result


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()


def _activate(path: str, page: int = 2, *, prev: str = _PREV_A, nxt: str = _NEXT_A):
    if prev:
        pnc.store_page_text(path, page - 1, prev, source="text_layer")
    if nxt:
        pnc.store_page_text(path, page + 1, nxt, source="text_layer")
    return pnc.activate(path, page)


def _run(client, blocks=None, path: str = "/tmp/nonexistent-issue275-cache.pdf", page: int = 2, prev=_PREV_A, nxt=_NEXT_A):
    token = _activate(path, page, prev=prev, nxt=nxt)
    stage: dict = {}
    try:
        entities = asyncio.run(
            run_has_text_analysis(
                blocks or [_block(_OWN_TEXT, 100)],
                client,
                vision_types=None,
                stage_status=stage,
            )
        )
    finally:
        pnc.clear(token)
    return entities, stage


def test_single_group_probe_text_sha_equals_sent_group_text_sha():
    """变异 3 主判据：组数==1 时 probe sha256 == 实发组文本 sha256。"""
    client = _CachingHaS()
    _run(client)
    assert len(client.ner_texts) == 1
    probe, sent = client.probe_texts[0], client.ner_texts[0]
    assert _sha(probe) == _sha(sent), "缓存探测文本与实发组文本必须逐字同源（F-1）"
    assert probe.startswith(_PREV_A) and probe.endswith(_NEXT_A)


def test_chars_zero_probe_equals_own_text_content(monkeypatch):
    """chars=0 ⇒ probe_text == text_content 逐字相等（现状快路径回归钉）。"""
    monkeypatch.setattr(settings, "RECOGNITION_NEIGHBOR_CONTEXT_CHARS", 0, raising=False)
    client = _CachingHaS()
    _run(client)
    assert client.probe_texts[0] == _OWN_TEXT
    assert client.probe_texts[0] == client.ner_texts[0]


def test_same_page_same_neighbor_second_run_hits_before_slot():
    client = _CachingHaS()
    _run(client)
    assert len(client.ner_texts) == 1
    # 第二跑：同页同邻居 ⇒ probe 命中 ⇒ hit_before_slot、不再发请求
    _entities, stage = _run(client)
    assert stage["has_text_cache_status"] == "hit_before_slot"
    assert len(client.ner_texts) == 1, "同页同邻居二次调用必须命中缓存"


def test_same_page_different_neighbor_misses():
    client = _CachingHaS()
    _run(client)
    _entities, stage = _run(client, prev=_PREV_B)
    # 终态 status 会被实发后的收尾覆盖成 model_call——钉「没有 hit」+ 两次实发。
    assert stage.get("has_text_cache_status") != "hit_before_slot"
    assert len(client.ner_texts) == 2, "邻居不同 ⇒ 组键不同 ⇒ 必然两次独立请求"


def test_cross_page_same_own_text_different_neighbors_do_not_share_key():
    """A 页文本 + X 邻居 与 B 页同文本 + Y 邻居：两组键不同，不串（§3.2）。"""
    client = _CachingHaS()
    _run(client, path="/tmp/issue275-pageA.pdf", page=2, prev=_PREV_A, nxt=_NEXT_A)
    _entities, stage = _run(client, path="/tmp/issue275-pageB.pdf", page=2, prev=_PREV_B, nxt=_NEXT_A)
    assert stage.get("has_text_cache_status") != "hit_before_slot"
    assert len(client.ner_texts) == 2, "跨页同文本异邻居不得假命中"


def test_neighbor_text_changes_key_even_when_own_text_identical():
    """「同页文本 + 不同邻居」必然两个键——B 页结果物理上进不了 A 页的键。"""
    client = _CachingHaS()
    _run(client, prev=_PREV_A)
    _run(client, prev=_PREV_B)
    # probe_texts 每次 get_cached_ner（整页探针 + 组探针 + 槽位复检）都会追加；
    # 按邻居标记分组比对，避免依赖调用次序。
    probes_a = [t for t in client.probe_texts if "前页邻居A" in t]
    probes_b = [t for t in client.probe_texts if "前页邻居B" in t]
    assert probes_a and probes_b
    assert all(_sha(t) != _sha(probes_a[0]) for t in probes_b)
