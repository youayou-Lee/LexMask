"""Issue#148 门①.5 段2 Critical C-1 的处置：每例前后复位 NER 全局状态。

为什么必须有这个装置：

* ``ner_breaker``（``app/core/circuit_breaker.py:48``）是**模块级单例**，阈值 3、
  复位窗口 30s；
* ``HaSClient._SHARED_NER_CACHE`` / ``_SHARED_NER_INFLIGHT`` 以及
  ``HaSClient._context_probe_cache``（``app/services/has_client.py:216-224``）是
  **类级共享状态**。

在 HTTP 边界注入失败的用例（HTTP 400 / 超时 / 不可解析）会经
``_do_chat_request`` → ``ner_breaker.call_sync`` 记失败；而 ``RETRYABLE_HTTPX``
（``app/core/retry.py:89-95``）包含 ``ReadTimeout``，``retry_sync`` 默认重试 2 次
⇒ **单跑一条超时用例就会把 breaker 打到 open**，其后 30 秒内所有用例改走「熔断开路」
分支（段2 评审实测：AC-B3/AC-B4 因此假 FAIL）。类级缓存则会把「第一次的结论」带进
后续用例，让「去重」与「重查」语义互相污染。

因此统一在这里每例前后复位，不依赖用例自觉。惰性 import 写在函数体内，避免在
收集期就把整个 services 包拖进来。
"""
from __future__ import annotations

import pytest


def _reset_ner_global_state() -> None:
    from app.core.circuit_breaker import ner_breaker
    from app.services.has_client import HaSClient

    ner_breaker._failure_count = 0
    ner_breaker._last_failure = 0.0
    ner_breaker._state = "closed"

    HaSClient._SHARED_NER_CACHE.clear()
    HaSClient._SHARED_NER_INFLIGHT.clear()
    HaSClient._context_probe_cache.clear()


@pytest.fixture(autouse=True)
def _isolate_ner_global_state():
    _reset_ner_global_state()
    yield
    _reset_ner_global_state()
