# Copyright 2026 LexMask Contributors

"""Issue#257 一期快项：NER 连接/传输类瞬时错误纳入可重试集合（AC2.5 单测部分）。

NEO001 事故形态：HaS 侧网络闪断抛 ``httpx.ReadError``/``RemoteProtocolError``，
当时不在 ``RETRYABLE_HTTPX`` 里 ⇒ 一次闪断直接判页失败、进而整卷熔断。这里钉三件事：

1. 四类瞬时错误进集合（``ReadError``/``WriteError``/``RemoteProtocolError``/``PoolTimeout``），
   **不**把 ``TransportError`` 父类整个拉进来（``ProxyError`` 等配置类错误不该白烧退避）；
2. 端到端接线：``HaSClient._call_model`` 真走重试（前 2 次 ReadError → 第 3 次成功，恰 3 次调用）；
3. 不重试面：HTTP 400（客户端错误）与 ``ProxyError``/``UnsupportedProtocol`` 各只调用 1 次。
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from app.core.retry import RETRYABLE_HTTPX
from app.services.has_client import HaSClient, _classify_ner_error


class _FakeResponse:
    def json(self) -> dict[str, Any]:
        return {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}


def test_transient_transport_errors_are_retryable() -> None:
    for exc_type in (
        httpx.ReadError,
        httpx.WriteError,
        httpx.RemoteProtocolError,
        httpx.PoolTimeout,
    ):
        assert exc_type in RETRYABLE_HTTPX, exc_type


def test_configuration_errors_stay_non_retryable() -> None:
    # 父类不能被整个拉进来：ProxyError/UnsupportedProtocol 是配置问题，重试只会掩盖它。
    assert httpx.TransportError not in RETRYABLE_HTTPX
    assert httpx.ProxyError not in RETRYABLE_HTTPX
    assert httpx.UnsupportedProtocol not in RETRYABLE_HTTPX


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    # ``retry_sync`` 在函数体内 ``import time``，只能打标准库模块本身。
    import time

    monkeypatch.setattr(time, "sleep", lambda _s: None)


def _client() -> HaSClient:
    return HaSClient(base_url="http://ner.invalid", max_retries=2)


def test_call_model_retries_read_error_three_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client()
    calls = {"n": 0}

    def _flaky(base: str, payload: dict[str, Any]) -> _FakeResponse:
        calls["n"] += 1
        if calls["n"] <= 2:
            raise httpx.ReadError("connection reset by peer")
        return _FakeResponse()

    monkeypatch.setattr(client, "_do_chat_request", _flaky)

    assert client._call_model([{"role": "user", "content": "x"}]) == "ok"
    assert calls["n"] == 3


def test_call_model_does_not_retry_http_400(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client()
    calls = {"n": 0}
    request = httpx.Request("POST", "http://ner.invalid/v1/chat/completions")

    def _bad_request(base: str, payload: dict[str, Any]) -> _FakeResponse:
        calls["n"] += 1
        response = httpx.Response(400, request=request, text="bad payload")
        raise httpx.HTTPStatusError("400", request=request, response=response)

    monkeypatch.setattr(client, "_do_chat_request", _bad_request)

    with pytest.raises(httpx.HTTPStatusError):
        client._call_model([{"role": "user", "content": "x"}])
    assert calls["n"] == 1


def test_call_model_does_not_retry_proxy_error(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client()
    calls = {"n": 0}

    def _proxy(base: str, payload: dict[str, Any]) -> _FakeResponse:
        calls["n"] += 1
        raise httpx.ProxyError("bad proxy")

    monkeypatch.setattr(client, "_do_chat_request", _proxy)

    with pytest.raises(httpx.ProxyError):
        client._call_model([{"role": "user", "content": "x"}])
    assert calls["n"] == 1


def test_classify_ner_error_exposes_new_buckets() -> None:
    assert _classify_ner_error(httpx.ReadError("x")) == "read_error"
    assert _classify_ner_error(httpx.WriteError("x")) == "write_error"
    assert _classify_ner_error(httpx.RemoteProtocolError("x")) == "remote_protocol"
    assert _classify_ner_error(httpx.PoolTimeout("x")) == "timeout"
