"""VlParseClient 测试(Issue#66 T01):MockTransport 注入,断言请求契约与显式失败语义。"""

import httpx
import pytest

from app.services.vl_parse_client import VlParseClient, VlParseError


def _client(handler) -> VlParseClient:
    return VlParseClient(
        base_url="http://127.0.0.1:8095/", timeout=5,
        transport=httpx.MockTransport(handler), max_retries=0,
    )


@pytest.mark.asyncio
async def test_parse_success_with_raw():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = request.read()
        return httpx.Response(200, json={
            "markdown": "# 判决书\n被告人[PERSON_1]……",
            "elapsed_s": 12.3,
            "raw_texts": [["块一", "块二"], []],
        })

    result = await _client(handler).parse("/tmp/page.png")
    assert seen["url"] == "http://127.0.0.1:8095/parse"  # 尾斜杠被 rstrip
    assert b'"raw": true' in seen["body"].replace(b" ", b" ") or b'"raw":true' in seen["body"]
    assert result.markdown.startswith("# 判决书")
    assert result.raw_texts == [["块一", "块二"], []]
    assert result.elapsed_s == 12.3


@pytest.mark.asyncio
async def test_parse_success_without_raw_field_backcompat():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"markdown": "正文", "elapsed_s": 1.0})

    result = await _client(handler).parse("/tmp/page.png", raw=False)
    assert result.markdown == "正文"
    assert result.raw_texts == []


@pytest.mark.asyncio
async def test_parse_http_error_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    with pytest.raises(VlParseError, match="500"):
        await _client(handler).parse("/tmp/page.png")


@pytest.mark.asyncio
async def test_parse_unreachable_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(VlParseError, match="不可达"):
        await _client(handler).parse("/tmp/page.png")


@pytest.mark.asyncio
async def test_parse_missing_markdown_field_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"elapsed_s": 1.0})

    with pytest.raises(VlParseError, match="markdown"):
        await _client(handler).parse("/tmp/page.png")


@pytest.mark.asyncio
async def test_is_available_true_and_false():
    ok = await _client(lambda r: httpx.Response(200, json={"status": "ok"})).is_available()
    assert ok is True

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    assert await _client(down).is_available() is False


@pytest.mark.asyncio
async def test_parse_retries_on_5xx_then_succeeds(monkeypatch):
    calls = {"n": 0}
    sleeps = []

    async def fake_sleep(sec):
        sleeps.append(sec)

    monkeypatch.setattr("app.services.vl_parse_client.asyncio.sleep", fake_sleep)

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(502, text="bad gateway")
        return httpx.Response(200, json={"markdown": "ok", "elapsed_s": 1.0})

    client = VlParseClient(base_url="http://127.0.0.1:8095", timeout=5,
                           transport=httpx.MockTransport(handler))
    result = await client.parse("/tmp/page.png")
    assert result.markdown == "ok"
    assert calls["n"] == 2 and sleeps == [15.0]


@pytest.mark.asyncio
async def test_parse_retries_on_connect_error_then_raises():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectError("refused")

    client = VlParseClient(base_url="http://127.0.0.1:8095", timeout=5,
                           transport=httpx.MockTransport(handler), max_retries=1,
                           retry_backoff=0.0)
    with pytest.raises(VlParseError, match="不可达"):
        await client.parse("/tmp/page.png")
    assert calls["n"] == 2
