"""Retry utility with exponential backoff for HTTP microservice calls."""
import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


async def retry_async(
    fn: Callable[..., Awaitable[T]],
    *args,
    max_retries: int = 2,
    base_delay: float = 1.0,
    max_delay: float = 10.0,
    retryable_exceptions: tuple = (Exception,),
    **kwargs,
) -> T:
    """
    Retry an async function with exponential backoff.

    Args:
        fn: Async function to retry
        max_retries: Maximum number of retry attempts (0 = no retries)
        base_delay: Initial delay in seconds
        max_delay: Maximum delay cap in seconds
        retryable_exceptions: Tuple of exception types to retry on
    """
    last_exc = None
    for attempt in range(max_retries + 1):
        try:
            return await fn(*args, **kwargs)
        except retryable_exceptions as e:
            last_exc = e
            if attempt < max_retries:
                delay = min(base_delay * (2 ** attempt), max_delay)
                logger.warning(
                    "Retry %d/%d for %s after %.1fs: %s",
                    attempt + 1, max_retries, fn.__name__, delay, e,
                )
                await asyncio.sleep(delay)
            else:
                logger.error("All %d retries exhausted for %s: %s", max_retries, fn.__name__, e)
    raise last_exc  # type: ignore


def retry_sync(
    fn: Callable[..., T],
    *args,
    max_retries: int = 2,
    base_delay: float = 1.0,
    max_delay: float = 10.0,
    retryable_exceptions: tuple = (Exception,),
    **kwargs,
) -> T:
    """
    Retry a sync function with exponential backoff.

    Args:
        fn: Sync function to retry
        max_retries: Maximum number of retry attempts (0 = no retries)
        base_delay: Initial delay in seconds
        max_delay: Maximum delay cap in seconds
        retryable_exceptions: Tuple of exception types to retry on
    """
    import time

    last_exc = None
    for attempt in range(max_retries + 1):
        try:
            return fn(*args, **kwargs)
        except retryable_exceptions as e:
            last_exc = e
            if attempt < max_retries:
                delay = min(base_delay * (2 ** attempt), max_delay)
                logger.warning(
                    "Retry %d/%d for %s after %.1fs: %s",
                    attempt + 1, max_retries, fn.__name__, delay, e,
                )
                time.sleep(delay)
            else:
                logger.error("All %d retries exhausted for %s: %s", max_retries, fn.__name__, e)
    raise last_exc  # type: ignore


# Exception types safe to retry (connection / timeout only, NOT 4xx client errors)
#
# Issue#257：NEO001 事故里 HaS 侧是「连接被重置/半包」——httpx 抛的是
# ``RemoteProtocolError``/``ReadError``/``WriteError``，都不在旧集合里，一次闪断
# 就被当成确定性失败（进而整卷熔断）。这四类与超时同族、重试可收敛，补进集合。
# 注意：**不**把 ``httpx.TransportError`` 父类整个拉进来——``ProxyError``/
# ``UnsupportedProtocol`` 是本地配置写错，重试只会白烧退避并掩盖真因。
_HX = __import__("httpx")

RETRYABLE_HTTPX = (
    _HX.ConnectError,
    _HX.ReadError,
    _HX.WriteError,
    _HX.RemoteProtocolError,
    _HX.PoolTimeout,
    _HX.ReadTimeout,
    _HX.ConnectTimeout,
    ConnectionError,
    OSError,
)
