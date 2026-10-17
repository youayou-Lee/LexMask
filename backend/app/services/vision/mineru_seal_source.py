"""MinerU 公章块提取与按文件缓存（Issue#96）。

设计要点（对应方案 v6 §2/§9）：

* MinerU sidecar 是**文件级**异步任务（``submit`` → 轮询 → ``wait_result``），
  而视觉管线是**页级**调用 ⇒ 每个文件只提交一次，把每页的章框切好后缓存，
  后续页直接读缓存（冷启一次约 70s，热态约 3s/页）。
* ``content_list_v2`` 是**外层按页的 list-of-lists**：外层下标即页下标。
  本环境 MinerU 版本的 ``page_idx`` **恒为 null**，因此页归属只能取外层下标。
* 章块两种形态：``{"type": "image", "sub_type": "seal", "bbox": [x0, y0, x1, y1]}``
  （bbox 为 0-1000 页归一化）与 ``{"type": "seal"}`` 变体。
* 六态（方案 §9）：``ok`` / ``missing_v2`` / ``shape_drift`` / ``failed``。
  前三者由本模块判定，``failed`` 由调用方（异常/超时）判定。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)

#: 章块 sub_type 标记
SEAL_SUBTYPE = "seal"
#: MinerU bbox 的页归一化分母
BBOX_SCALE = 1000.0

STATUS_OK = "ok"
STATUS_MISSING_V2 = "missing_v2"
STATUS_SHAPE_DRIFT = "shape_drift"
STATUS_FAILED = "failed"

SealBox = tuple[float, float, float, float]  # (x, y, width, height)，均 0-1


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _normalize_bbox(raw: Any) -> SealBox | None:
    """把 MinerU 的 0-1000 bbox 转成 0-1 的 (x, y, width, height)；非法返回 None。"""
    if not isinstance(raw, (list, tuple)) or len(raw) != 4:
        return None
    if not all(_is_number(v) for v in raw):
        return None
    x0, y0, x1, y1 = (float(v) / BBOX_SCALE for v in raw)
    left, right = min(x0, x1), max(x0, x1)
    top, bottom = min(y0, y1), max(y0, y1)
    left = min(max(left, 0.0), 1.0)
    top = min(max(top, 0.0), 1.0)
    right = min(max(right, 0.0), 1.0)
    bottom = min(max(bottom, 0.0), 1.0)
    width, height = right - left, bottom - top
    if width <= 0.0 or height <= 0.0:
        return None
    return (left, top, width, height)


def _is_seal_candidate(block: Any) -> bool:
    if not isinstance(block, dict):
        return False
    return block.get("sub_type") == SEAL_SUBTYPE or block.get("type") == SEAL_SUBTYPE


def extract_seal_boxes(
    content_list_v2: Any,
) -> tuple[dict[int, list[SealBox]], str, list[str]]:
    """从 ``content_list_v2`` 抽出 ``{页下标: [章框, ...]}``。

    返回 ``(boxes_by_page, status, warnings)``。``status`` 只可能是
    ``ok`` / ``missing_v2`` / ``shape_drift`` 三种；``failed`` 由调用方判定。
    """
    warnings: list[str] = []
    if content_list_v2 is None:
        return {}, STATUS_MISSING_V2, ["content_list_v2 is None"]
    if not isinstance(content_list_v2, list):
        return {}, STATUS_SHAPE_DRIFT, [
            f"content_list_v2 is {type(content_list_v2).__name__}, expected list"
        ]
    if not content_list_v2:
        # 空列表：解析成功但整份文件没有可归属的页内容
        return {}, STATUS_OK, []
    page_lists = [entry for entry in content_list_v2 if isinstance(entry, list)]
    if not page_lists:
        return {}, STATUS_SHAPE_DRIFT, [
            "content_list_v2 has no per-page list (expected list-of-lists)"
        ]
    if len(page_lists) != len(content_list_v2):
        warnings.append(
            f"{len(content_list_v2) - len(page_lists)} outer entrie(s) are not page lists"
        )
    boxes_by_page: dict[int, list[SealBox]] = {}
    for page_index, entry in enumerate(content_list_v2):
        if not isinstance(entry, list):
            continue
        for block in entry:
            if not _is_seal_candidate(block):
                continue
            box = _normalize_bbox(block.get("bbox"))
            if box is None:
                warnings.append(
                    f"page {page_index}: seal candidate without usable bbox "
                    f"({block.get('bbox')!r})"
                )
                continue
            boxes_by_page.setdefault(page_index, []).append(box)
    return boxes_by_page, STATUS_OK, warnings


class MineruSealSource:
    """按文件缓存 MinerU 章框（进程级），供页级视觉管线读取。"""

    def __init__(
        self,
        *,
        client_factory: Callable[[], Any] | None = None,
        timeout: float = 60.0,
    ) -> None:
        self._client_factory = client_factory or self._default_client_factory
        self._timeout = float(timeout)
        self._cache: dict[str, tuple[dict[int, list[SealBox]], str]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self.stats: dict[str, int] = {
            "files": 0,
            "ok": 0,
            "missing_v2": 0,
            "shape_drift": 0,
            "failed": 0,
            "pages_with_seals": 0,
        }

    @staticmethod
    def _default_client_factory() -> Any:
        from app.services.mineru_parse_client import MineruParseClient

        return MineruParseClient()

    def cached(self, cache_key: str) -> tuple[dict[int, list[SealBox]], str] | None:
        return self._cache.get(cache_key)

    def invalidate(self, cache_key: str | None = None) -> None:
        if cache_key is None:
            self._cache.clear()
        else:
            self._cache.pop(cache_key, None)

    async def page_seals(
        self,
        cache_key: str,
        load_bytes: Callable[[], Awaitable[bytes]],
        page: int,
        *,
        filename: str | None = None,
    ) -> tuple[list[SealBox], str]:
        """返回 ``(该页章框, 状态)``；状态见模块级常量。"""
        entry = self._cache.get(cache_key)
        if entry is None:
            lock = self._locks.setdefault(cache_key, asyncio.Lock())
            async with lock:
                entry = self._cache.get(cache_key)
                if entry is None:
                    entry = await self._fetch(cache_key, load_bytes, filename)
                    self._cache[cache_key] = entry
        boxes_by_page, status = entry
        return list(boxes_by_page.get(int(page), [])), status

    async def _fetch(
        self,
        cache_key: str,
        load_bytes: Callable[[], Awaitable[bytes]],
        filename: str | None,
    ) -> tuple[dict[int, list[SealBox]], str]:
        started = time.perf_counter()
        try:
            payload = await load_bytes()
        except Exception:
            logger.warning(
                "MinerU seal source: cannot read file bytes for %s", cache_key, exc_info=True
            )
            self.stats["failed"] += 1
            return {}, STATUS_FAILED
        try:
            client = self._client_factory()
            task_id = await asyncio.wait_for(
                client.submit(payload, filename or f"{cache_key}.pdf"), timeout=self._timeout
            )
            result = await asyncio.wait_for(client.wait_result(task_id), timeout=self._timeout)
            content_list_v2 = getattr(result, "content_list_v2", None)
        except Exception:
            logger.warning(
                "MinerU seal source: task failed for %s; treated as failure", cache_key, exc_info=True
            )
            self.stats["failed"] += 1
            return {}, STATUS_FAILED
        boxes_by_page, status, warnings = extract_seal_boxes(content_list_v2)
        for warning in warnings:
            logger.warning("MinerU seal source: %s", warning)
        if status == STATUS_OK and not boxes_by_page:
            logger.info(
                "MinerU seal source: %s parsed OK with no seal blocks (%.1fs)",
                cache_key,
                time.perf_counter() - started,
            )
        else:
            logger.info(
                "MinerU seal source: %s status=%s seal pages=%d (%.1fs)",
                cache_key,
                status,
                len(boxes_by_page),
                time.perf_counter() - started,
            )
        self.stats["files"] += 1
        self.stats[status] = self.stats.get(status, 0) + 1
        self.stats["pages_with_seals"] += len(boxes_by_page)
        return boxes_by_page, status


_source: MineruSealSource | None = None


def get_mineru_seal_source() -> MineruSealSource:
    """惰性单例（测试通过 ``MineruSealSource`` 直接构造替身）。"""
    global _source
    if _source is None:
        timeout = 60.0
        try:
            from app.core.config import get_settings

            timeout = float(getattr(get_settings(), "VISUAL_SEAL_MINERU_TIMEOUT", 60.0) or 60.0)
        except Exception:  # pragma: no cover - 配置不可读时退回默认
            timeout = 60.0
        _source = MineruSealSource(timeout=timeout)
    return _source


def reset_mineru_seal_source() -> None:
    global _source
    _source = None
