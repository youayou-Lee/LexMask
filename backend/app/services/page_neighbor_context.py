"""页级邻居上下文（Issue#275 B2）——进程内页文本登记表 + 文本层直取兜底。

设计稿：``github-issues/2026-11-15-batch2/design-issue275.md`` §2.2/§3。

一句话：每页 NER 主查文本可附带「前页尾 N 字 + 后页首 N 字」（
``RECOGNITION_NEIGHBOR_CONTEXT_CHARS``，默认 200，0=关闭），**只辅助召回、
不参与判定**——回映仍只对本页 OCR/文本层块做 ``_match_entities_to_ocr``，
邻居独有的值结构上必然落空被丢。

契约要点（全部 fail-open，绝不向识别主链上抛）：

* **尽力而为、零等待**：不为他页阻塞、不新增请求前置依赖。页并发
  （``BATCH_RECOGNITION_PAGE_CONCURRENCY``）下首屏页常取不到后页 ⇒ 空侧；
  后页渐渐就绪 ⇒ neighbor。这正是账本（``text_context`` 三值）要如实记录的。
* **取文顺序**（每侧独立）：①登记表命中（text_layer 优先于 ocr）→
  ②registry miss 且邻页是 PDF ⇒ ``file_parser.get_pdf_page_text_blocks``
  直取文本层（毫秒级、带缓存；扫描页/越界/异常 ⇒ 空）→ ③仍无 ⇒ 空串。
* **登记的是原文不是结论**：登记点在扫描闸之前（扫描页碎片段对邻居仍是
  真实墨迹）、失败页只登记真实产出（OCR blocks / 文本层 blocks）；
  空文本页拒写（失败页无文本 = 无邻居贡献）。
* **并发正确性**：邻居请求 per-page 隔离用 ``contextvars.ContextVar``
  （不用模块级全局——邻居是页间异值，有串页风险；同族先例
  ``reset_page_ner_deadline`` 是页间同值才允许全局）。读动作发生在 async 段
  （``resolve_neighbors_for_current`` 在派发组前调用一次、结果作为参数传下去），
  不依赖 to_thread 线程内读取。

模块内全部纯函数 + 进程内 dict，无锁依赖事件循环单线程（与
``page_routing/state.py`` 同前提）；取文兜底除外（线程壳，见
``_probe_text_layer``）。
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import os
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from app.core.config import settings

logger = logging.getLogger(__name__)

# registry 卷容量（§3.3-⑥）：LRU，超出整卷逐出。
_MAX_VOLUMES = 8
# 每卷页数上限（0 = 跟随 settings.TEXT_LAYER_DOCLINE_MAX_PAGES 口径 1000）；
# 模块级常量仅测试可 monkeypatch 覆盖。
_MAX_PAGES_PER_VOLUME = 0
# 文本层直取 memo 容量（probe 只对未跑过的邻页发生；按 vol_key+页键 ⇒ 内容稳定）。
_PROBE_MEMO_MAX = 256
_PROBE_TIMEOUT_SEC = 10.0

_SOURCE_TEXT_LAYER = "text_layer"
_SOURCE_OCR = "ocr"
_REGISTRY_SOURCES = (_SOURCE_TEXT_LAYER, _SOURCE_OCR)

# vol_key -> (page -> {source: text})；OrderedDict 维护 LRU（卷级 + 页级）。
_registry: OrderedDict[str, OrderedDict[int, dict[str, str]]] = OrderedDict()
# probe 结果 memo（vol_key, page）-> text。
_probe_memo: OrderedDict[tuple[str, int], str] = OrderedDict()

# ---- contextvar（§3.5：页间异值，必须 per-task 隔离）----------------------


@dataclass(frozen=True)
class NeighborRequest:
    """当前页的邻居取文请求。``path`` 供文本层直取兜底；``file_key`` 供 registry。"""

    path: str
    file_key: str
    page: int


_current: contextvars.ContextVar[NeighborRequest | None] = contextvars.ContextVar(
    "page_neighbor_request", default=None
)


def activate(path: str, page: int) -> contextvars.Token:
    """页入口激活（``detect_with_dual_pipeline`` PDF 分支调用；finally 里 clear）。"""
    return _current.set(
        NeighborRequest(path=str(path), file_key=_vol_key(path), page=int(page))
    )


def clear(token: contextvars.Token) -> None:
    try:
        _current.reset(token)
    except ValueError:
        # 跨上下文 reset（token 不属于当前 context）——退化为显式清空。
        _current.set(None)


def reset_for_tests() -> None:
    _registry.clear()
    _probe_memo.clear()


# ---- registry -------------------------------------------------------------


def _vol_key(path: str) -> str:
    """f"{realpath}:{size}:{mtime_ns}"。

    对齐 ``text_layer_doc_source._file_fingerprint`` 的 stat 思路（该函数实为
    ``f"{size}:{mtime}"`` 秒级、无 realpath），另加 realpath 前缀与 mtime_ns
    精度——**加强变体而非复用**（M-1）：同路径快速替换内容时 ns 精度才够判异
    （§5 变异 7 判据）。stat 失败（文件已消失）⇒ realpath + "unknown"，
    与激活时同键（registry 仍可命中）。
    """
    resolved = os.path.realpath(str(path or ""))
    try:
        st = os.stat(resolved)
        return f"{resolved}:{int(st.st_size)}:{int(st.st_mtime_ns)}"
    except OSError:
        return f"{resolved}:unknown"


def _max_pages_per_volume() -> int:
    return int(_MAX_PAGES_PER_VOLUME or getattr(settings, "TEXT_LAYER_DOCLINE_MAX_PAGES", 1000) or 1000)


def store_page_text(path: str, page: int, text: str, *, source: str) -> None:
    """登记一页真实产出的文本（OCR / 文本层 blocks 拼接）。

    * ``source`` ∈ {"text_layer", "ocr"}；同页两源并存，resolve 时 text_layer 优先。
    * text 为空串/None/纯空白 ⇒ 不写（失败页无文本 = 无邻居贡献，§3.3-③④；
      变异 4 判据：占位空白不得充数）。
    * 任何异常吞掉记 debug——登记失败只损失邻居召回，绝不影响识别主链。
    """
    try:
        text = str(text or "")
        if not text.strip():
            return
        if source not in _REGISTRY_SOURCES:
            return
        page = int(page)
        if page < 1:
            return
        key = _vol_key(path)
        vol = _registry.get(key)
        if vol is None:
            while len(_registry) >= _MAX_VOLUMES:
                _registry.popitem(last=False)
            vol = _registry.setdefault(key, OrderedDict())
        entry = vol.setdefault(page, {})
        entry[source] = text
        vol.move_to_end(page)
        while len(vol) > _max_pages_per_volume():
            vol.popitem(last=False)
    except Exception:  # noqa: BLE001 - fail-open（登记失败不伤害识别主链）
        logger.debug("page_neighbor_context: store_page_text failed", exc_info=True)


def _page_text_from_registry(vol: dict[int, dict[str, str]], page: int) -> tuple[str, str]:
    """registry 取文：text_layer 优先（确定性来源，OCR 结果随渲染/DPI 抖动，§3.1）。"""
    entry = vol.get(page) or {}
    layer = str(entry.get(_SOURCE_TEXT_LAYER) or "")
    if layer.strip():
        return layer, "registry:text_layer"
    ocr = str(entry.get(_SOURCE_OCR) or "")
    if ocr.strip():
        return ocr, "registry:ocr"
    return "", ""


# ---- 文本层直取兜底（②）----------------------------------------------------
#
# ``FileParser.get_pdf_page_text_blocks`` 是 async def（内部无 await 的 CPU 活）。
# 本模块的取文契约是**同步**函数（注入点在 async 段同步调用一次），因此用独立
# 线程 + 独立事件循环执行该协程——绝不在运行中的主 loop 上嵌套等待。毫秒级、
# 自带 parser 内缓存与本项目 probe memo。任何失败 fail-open 返回空串。


def _probe_text_layer(file_path: str, page: int) -> str:
    if not str(file_path or "").lower().endswith(".pdf"):
        return ""
    key_hint = _vol_key(file_path)
    memo_key = (key_hint, int(page))
    cached = _probe_memo.get(memo_key)
    if cached is not None:
        _probe_memo.move_to_end(memo_key)
        return cached

    def _extract() -> str:
        async def _run() -> str:
            from app.services.file_parser import FileParser

            blocks, _width, _height = await FileParser().get_pdf_page_text_blocks(
                file_path, int(page)
            )
            return "\n".join(str(block.text or "") for block in blocks)

        return asyncio.run(_run())

    try:
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="pnc-probe") as pool:
            future = pool.submit(_extract)
            text = future.result(timeout=_PROBE_TIMEOUT_SEC)
    except Exception:  # noqa: BLE001 - 越界页/加密/损坏/超时一律 fail-open 空
        logger.debug(
            "page_neighbor_context: text-layer probe failed file=%s page=%s",
            os.path.basename(str(file_path or "")),
            page,
            exc_info=True,
        )
        text = ""
    text = str(text or "")
    _probe_memo[memo_key] = text
    while len(_probe_memo) > _PROBE_MEMO_MAX:
        _probe_memo.popitem(last=False)
    return text


# ---- resolve --------------------------------------------------------------


def _trim_tail(text: str, chars: int) -> str:
    return text[-chars:].strip() if chars > 0 else ""


def _trim_head(text: str, chars: int) -> str:
    return text[:chars].strip() if chars > 0 else ""


def resolve_neighbors(path: str, page: int, chars: int) -> tuple[str, str, dict]:
    """取 (prev_tail, next_head, meta)。

    meta 形态（§2.2/§3.6）：

    * 正常：``{"prev_source","next_source","prev_chars","next_chars"}``，
      source ∈ {"registry:text_layer","registry:ocr","text_layer_probe",""}；
    * ``chars<=0``：``{"off": True}``；
    * 任何异常：``{"error": 类型名}``——绝不 raise（M-3 fail-open 契约；
      ``text_context`` 值集不扩，error 由注入点的 ``has_text_neighbor_error``
      metric 单独区分）。
    """
    try:
        chars = int(chars)
    except Exception:  # noqa: BLE001
        chars = 0
    if chars <= 0:
        return "", "", {"off": True}
    try:
        page = int(page)
        key = _vol_key(path)
        vol = _registry.get(key) or {}
        meta: dict = {"prev_source": "", "next_source": "", "prev_chars": 0, "next_chars": 0}

        prev_text, prev_source = "", ""
        if page - 1 >= 1:
            prev_text, prev_source = _page_text_from_registry(vol, page - 1)
            if not prev_text:
                probed = _probe_text_layer(path, page - 1)
                if probed.strip():
                    prev_text, prev_source = probed, "text_layer_probe"
        next_text, next_source = _page_text_from_registry(vol, page + 1)
        if not next_text:
            probed = _probe_text_layer(path, page + 1)
            if probed.strip():
                next_text, next_source = probed, "text_layer_probe"

        prev_tail = _trim_tail(prev_text, chars)
        next_head = _trim_head(next_text, chars)
        # source 只在真实贡献了字符时记账（M-4：缺来源证据则复盘不可归因；
        # 反向钉：空白占位充数时 source 必须为空——§5 变异 4）。
        meta["prev_source"] = prev_source if prev_tail else ""
        meta["next_source"] = next_source if next_head else ""
        meta["prev_chars"] = len(prev_tail)
        meta["next_chars"] = len(next_head)
        return prev_tail, next_head, meta
    except Exception as exc:  # noqa: BLE001 - fail-open（绝不 raise）
        logger.debug("page_neighbor_context: resolve_neighbors failed", exc_info=True)
        return "", "", {"error": type(exc).__name__}


def resolve_neighbors_for_current(chars: int | None = None) -> tuple[str, str, dict]:
    """``resolve_neighbors`` 的 contextvar 包装（注入点实际调用的入口，M-2）。

    未激活（contextvar 为 None）⇒ ("", "", {"inactive": True})——与 off 同为
    空串路径，由 meta 区分；off/error/inactive 一律走 meta 不 raise。
    ``chars`` 缺省时读 ``settings.RECOGNITION_NEIGHBOR_CONTEXT_CHARS``。
    """
    req = _current.get()
    if req is None:
        return "", "", {"inactive": True}
    if chars is None:
        chars = int(getattr(settings, "RECOGNITION_NEIGHBOR_CONTEXT_CHARS", 0) or 0)
    return resolve_neighbors(req.path, req.page, chars)
