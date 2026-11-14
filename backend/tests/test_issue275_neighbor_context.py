"""Issue#275 B2：``page_neighbor_context`` 单元测试（设计稿 §2.2/§3.3/§5）。

钉住：_vol_key 指纹（M-1 加强变体）、store 拒空/双源 text_layer 优先/LRU 逐出、
resolve_neighbors 边界 1-8、resolve_neighbors_for_current 契约（M-2）、裁剪粒度、
chars=0 off 短路、异常 fail-open、contextvar 页间隔离（§3.5）。
"""

from __future__ import annotations

import asyncio
import os

import pytest

from app.core.config import settings
from app.services import page_neighbor_context as pnc


@pytest.fixture(autouse=True)
def _clean_registry():
    pnc.reset_for_tests()
    yield
    pnc.reset_for_tests()


def _touch(path: str, size: int = 32) -> None:
    with open(path, "wb") as fh:
        fh.write(b"A" * size)


# --------------------------------------------------------------------- _vol_key


def test_vol_key_distinguishes_same_path_content_swap(tmp_path):
    """M-1/变异 7：同路径同 size 换内容 ⇒ mtime_ns 使 vol_key 变化（无跨卷污染）。"""
    path = str(tmp_path / "doc.pdf")
    _touch(path, size=64)
    key1 = pnc._vol_key(path)
    # 同 size 覆写内容（mtime 自然变化）
    _touch(path, size=64)
    key2 = pnc._vol_key(path)
    assert key1 != key2, "同路径替换内容后 vol_key 必须变化（realpath+size+mtime_ns）"
    assert key1.startswith(os.path.realpath(path))


def test_vol_key_missing_file_is_stable(tmp_path):
    path = str(tmp_path / "nope.pdf")
    assert pnc._vol_key(path) == pnc._vol_key(path)
    assert "unknown" in pnc._vol_key(path)


# ------------------------------------------------------------------ store_page_text


def test_store_rejects_empty_and_whitespace_text(tmp_path):
    """§3.3-3 / 变异 4：空页/占位空白不得进 registry（失败页无文本=无邻居贡献）。"""
    path = str(tmp_path / "doc.pdf")
    pnc.store_page_text(path, 1, "", source="ocr")
    pnc.store_page_text(path, 1, None, source="ocr")
    pnc.store_page_text(path, 1, "          ", source="ocr")
    prev, nxt, meta = pnc.resolve_neighbors(path, 2, 200)
    assert prev == "" and nxt == ""
    assert meta["prev_source"] == "" and meta["next_source"] == ""


def test_store_rejects_unknown_source_and_bad_page(tmp_path):
    path = str(tmp_path / "doc.pdf")
    pnc.store_page_text(path, 1, "正文", source="bogus")
    pnc.store_page_text(path, 0, "正文", source="ocr")
    pnc.store_page_text(path, -1, "正文", source="ocr")
    assert pnc.resolve_neighbors(path, 2, 200)[0] == ""


def test_text_layer_source_wins_over_ocr(tmp_path):
    """§3.1：同页双源并存时 resolve 取 text_layer（确定性来源）。"""
    path = str(tmp_path / "doc.pdf")
    pnc.store_page_text(path, 1, "OCR读到的文本", source="ocr")
    pnc.store_page_text(path, 1, "文本层读到的文本", source="text_layer")
    prev, _nxt, meta = pnc.resolve_neighbors(path, 2, 200)
    assert prev == "文本层读到的文本"
    assert meta["prev_source"] == "registry:text_layer"


def test_ocr_only_source_recorded(tmp_path):
    path = str(tmp_path / "doc.pdf")
    pnc.store_page_text(path, 1, "纯OCR文本", source="ocr")
    prev, _n, meta = pnc.resolve_neighbors(path, 2, 200)
    assert prev == "纯OCR文本"
    assert meta["prev_source"] == "registry:ocr"


def test_lru_evicts_oldest_volume(tmp_path, monkeypatch):
    monkeypatch.setattr(pnc, "_MAX_VOLUMES", 2)
    paths = [str(tmp_path / f"v{i}.pdf") for i in range(3)]
    for i, path in enumerate(paths):
        pnc.store_page_text(path, 1, f"卷{i}", source="ocr")
    # 第 1 卷被逐出
    prev, _n, meta = pnc.resolve_neighbors(paths[0], 2, 200)
    assert prev == "" and meta["prev_source"] == ""
    prev3, _n2, _m3 = pnc.resolve_neighbors(paths[2], 2, 200)
    assert prev3 == "卷2"


def test_volume_page_cap_evicts_oldest_page(tmp_path, monkeypatch):
    monkeypatch.setattr(pnc, "_MAX_PAGES_PER_VOLUME", 3)
    path = str(tmp_path / "doc.pdf")
    for page in range(1, 6):
        pnc.store_page_text(path, page, f"页{page}", source="ocr")
    prev, _n, meta = pnc.resolve_neighbors(path, 5, 200)
    # 页 1/2 被逐出：页 5 的 prev（页 4）还在
    assert prev == "页4"
    _prev2, _n2, _m2 = pnc.resolve_neighbors(path, 2, 200)
    # 页 2 的 prev（页 1）已被逐出
    assert pnc.resolve_neighbors(path, 2, 200)[0] == ""


# -------------------------------------------------------------- resolve_neighbors


def test_chars_zero_short_circuits_off(tmp_path):
    path = str(tmp_path / "doc.pdf")
    pnc.store_page_text(path, 1, "前页文本", source="ocr")
    prev, nxt, meta = pnc.resolve_neighbors(path, 2, 0)
    assert prev == "" and nxt == ""
    assert meta == {"off": True}


def test_negative_chars_treated_as_off(tmp_path):
    path = str(tmp_path / "doc.pdf")
    pnc.store_page_text(path, 1, "前页文本", source="ocr")
    assert pnc.resolve_neighbors(path, 2, -5) == ("", "", {"off": True})


def test_first_page_has_no_prev_but_next(tmp_path):
    path = str(tmp_path / "doc.pdf")
    pnc.store_page_text(path, 2, "次页文本", source="ocr")
    prev, nxt, meta = pnc.resolve_neighbors(path, 1, 200)
    assert prev == "" and meta["prev_source"] == ""
    assert nxt == "次页文本" and meta["next_source"] == "registry:ocr"


def test_prev_takes_tail_and_next_takes_head_with_strip(tmp_path):
    """裁剪 = 字符级切片 + strip：prev 取尾 N 字、next 取头 N 字。"""
    path = str(tmp_path / "doc.pdf")
    pnc.store_page_text(path, 1, "甲" * 150 + "前页尾巴" , source="ocr")
    pnc.store_page_text(path, 3, "后页开头" + "乙" * 150, source="ocr")
    prev, nxt, meta = pnc.resolve_neighbors(path, 2, 8)
    assert prev == "甲" * 4 + "前页尾巴"
    assert nxt == "后页开头" + "乙" * 4
    assert meta["prev_chars"] == len(prev)
    assert meta["next_chars"] == len(nxt)


def test_missing_neighbor_page_yields_empty(tmp_path):
    path = str(tmp_path / "doc.pdf")
    prev, nxt, meta = pnc.resolve_neighbors(path, 2, 200)
    assert prev == "" and nxt == ""
    assert meta["prev_source"] == "" and meta["next_source"] == ""


def test_resolve_never_raises_on_probe_failure(tmp_path, monkeypatch):
    """§3.3-7 / M-3：取文出错 fail-open——空侧 + error 类型名，绝不 raise。"""
    path = str(tmp_path / "doc.pdf")

    def _boom(file_path, page):
        raise RuntimeError("fitz exploded")

    monkeypatch.setattr(pnc, "_probe_text_layer", _boom)
    prev, nxt, meta = pnc.resolve_neighbors(path, 2, 200)
    assert prev == "" and nxt == ""
    assert meta.get("error") == "RuntimeError"


def test_probe_fallback_hits_text_layer_of_neighbor_page(tmp_path, monkeypatch):
    """§2.2 ②：registry miss 且邻页是 PDF ⇒ 文本层直取兜底（source=text_layer_probe）。"""
    path = str(tmp_path / "doc.pdf")

    def _fake_probe(file_path, page):
        # 两侧独立取文：只有 prev 侧（页 1）命中，next 侧（页 3）落空。
        return {1: "直取的文本层内容"}.get(page, "")

    monkeypatch.setattr(pnc, "_probe_text_layer", _fake_probe)
    prev, _n, meta = pnc.resolve_neighbors(path, 2, 200)
    assert prev == "直取的文本层内容"
    assert meta["prev_source"] == "text_layer_probe"


def test_probe_only_for_pdf_suffix(tmp_path, monkeypatch):
    path = str(tmp_path / "photo.jpg")

    def _unexpected(file_path, page):
        raise AssertionError("非 PDF 邻页不得触发文本层直取")

    monkeypatch.setattr(pnc, "_probe_text_layer", _unexpected)
    prev, nxt, meta = pnc.resolve_neighbors(path, 2, 200)
    assert prev == "" and nxt == ""


# ------------------------------------------------ resolve_neighbors_for_current


def test_for_current_inactive_yields_inactive_meta():
    prev, nxt, meta = pnc.resolve_neighbors_for_current(200)
    assert prev == "" and nxt == ""
    assert meta == {"inactive": True}


def test_for_current_reads_contextvar_and_default_chars(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "RECOGNITION_NEIGHBOR_CONTEXT_CHARS", 6, raising=False)
    path = str(tmp_path / "doc.pdf")
    pnc.store_page_text(path, 1, "前页文本", source="ocr")
    token = pnc.activate(path, 2)
    try:
        prev, _n, meta = pnc.resolve_neighbors_for_current()
        assert prev == "前页文本"
        assert meta["prev_source"] == "registry:ocr"
    finally:
        pnc.clear(token)


def test_contextvar_isolation_between_concurrent_pages(tmp_path):
    """§3.5：两「并发」任务各自 activate 不同页，互相读不到对方。"""
    path = str(tmp_path / "doc.pdf")
    pnc.store_page_text(path, 1, "第一页", source="ocr")
    pnc.store_page_text(path, 2, "第二页", source="ocr")
    pnc.store_page_text(path, 3, "第三页", source="ocr")
    results: dict[int, tuple[str, str]] = {}

    async def worker(page: int) -> None:
        token = pnc.activate(path, page)
        try:
            await asyncio.sleep(0.01)  # 让出控制权，制造交错
            prev, nxt, _meta = pnc.resolve_neighbors_for_current(200)
            results[page] = (prev, nxt)
        finally:
            pnc.clear(token)

    async def main() -> None:
        await asyncio.gather(worker(1), worker(3))

    asyncio.run(main())
    assert results[1] == ("", "第二页")
    assert results[3] == ("第二页", "")
