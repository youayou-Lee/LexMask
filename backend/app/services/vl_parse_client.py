"""VL-MD 管线(Issue#66/#50 T1):PaddleOCR-VL 转 MD 服务客户端。

POST {VL_PARSE_BASE_URL}/parse  body {"path": <服务侧绝对路径>, "raw": true}
  -> {"markdown": str, "raw_texts": [[块文本...]...], "elapsed_s": float}
服务部署与接口契约见 Issue#40 实录(8095,paddle 原生管线,raw 返回 parsing_res_list 块文本)。
"""

import asyncio
import logging
from dataclasses import dataclass, field

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)


class VlParseError(RuntimeError):
    """VL 转 MD 服务调用失败(不可达/超时/业务错误)。"""


@dataclass
class VlParseResult:
    markdown: str
    raw_texts: list[list[str]] = field(default_factory=list)
    elapsed_s: float = 0.0


class VlParseClient:
    """8095 转 MD 服务的薄客户端:单页(图/PDF 路径)调用,失败显式抛错,不静默回退。"""

    def __init__(self, base_url: str | None = None, timeout: float | None = None,
                 transport: httpx.AsyncBaseTransport | None = None,
                 max_retries: int = 2, retry_backoff: float = 15.0):
        self.base_url = (base_url or settings.VL_PARSE_BASE_URL).rstrip("/")
        self.timeout = timeout or settings.VL_PARSE_TIMEOUT
        self._transport = transport  # 测试注入 MockTransport 用,生产为 None
        self.max_retries = max_retries    # 看门狗重启窗口 ~50s,15s/30s 两次退避基本覆盖
        self.retry_backoff = retry_backoff

    def _async_client(self) -> httpx.AsyncClient:
        kwargs: dict = {"timeout": self.timeout}
        if self._transport is not None:
            kwargs["transport"] = self._transport
        return httpx.AsyncClient(**kwargs)

    async def parse(self, path: str, *, raw: bool = True) -> VlParseResult:
        """调用 /parse;网络错误/5xx 重试(实例侧看门狗重启窗口 ~10s 停机 + ~40s 热载)。"""
        payload = {"path": path, "raw": raw}
        attempts = 1 + self.max_retries
        last_exc: Exception | None = None
        for attempt in range(attempts):
            if attempt:
                await asyncio.sleep(self.retry_backoff * attempt)
            try:
                async with self._async_client() as client:
                    resp = await client.post(f"{self.base_url}/parse", json=payload)
            except httpx.HTTPError as exc:
                last_exc = VlParseError(f"VL 转 MD 服务不可达({self.base_url}): {exc!r}")
                continue
            if resp.status_code >= 500 and attempt < attempts - 1:
                last_exc = VlParseError(f"VL 转 MD 服务返回 {resp.status_code}: {resp.text[:200]}")
                continue
            if resp.status_code != 200:
                raise VlParseError(f"VL 转 MD 服务返回 {resp.status_code}: {resp.text[:200]}")
            try:
                data = resp.json()
            except ValueError as exc:
                raise VlParseError(f"VL 转 MD 服务响应非 JSON: {resp.text[:200]}") from exc
            if "markdown" not in data:
                raise VlParseError(f"VL 转 MD 服务响应缺 markdown 字段: {list(data)}")
            return VlParseResult(
                markdown=data.get("markdown") or "",
                raw_texts=[list(pg) for pg in (data.get("raw_texts") or []) if isinstance(pg, list)],
                elapsed_s=float(data.get("elapsed_s") or 0.0),
            )
        assert last_exc is not None
        raise last_exc

    async def is_available(self) -> bool:
        try:
            async with self._async_client() as client:
                resp = await client.get(f"{self.base_url}/health")
            return resp.status_code == 200
        except httpx.HTTPError:
            return False
