"""MineruParseClient 测试（Issue#75 喂Agent并行模式）：httpx MockTransport 回放，无真实网络。

夹具按 Task 0 实测契约构造（.superpowers/sdd/.../task-0-report.md）：
- 提交 POST /tasks → 202 {"task_id": ...}（multipart 字段名 files，需显式 backend=pipeline）；
- 轮询 GET /tasks/{id} → status ∈ {pending, processing, completed, failed}，无进度字段；
- 取件 GET /tasks/{id}/result → application/zip，路径 <stem>/<parse_method>/<stem>*；
  content_list_v2 需提交时 return_content_list=true，images/ 需 return_images=true。
"""

import io
import json
import zipfile

import httpx
import pytest

from app.services.mineru_parse_client import MineruParseClient, MineruParseError


def _result_zip(with_images: bool = True) -> bytes:
    """合成产物 zip：路径对照实测（stem=out, parse_method=auto），含 v1 干扰项。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("out/auto/out.md", "# 合成\n正文一段")
        # v2 = 外层按页的 list-of-lists（Task 0 §6c）
        zf.writestr(
            "out/auto/out_content_list_v2.json",
            json.dumps([
                [{"type": "page_header",
                  "content": {"page_header_content": [{"type": "text", "content": "页眉一"}]},
                  "bbox": [1, 2, 3, 4]}],
                [{"type": "paragraph",
                  "content": {"paragraph_content": [{"type": "text", "content": "正文一段"}]},
                  "bbox": [5, 6, 7, 8]}],
            ]),
        )
        # v1 扁平 list 干扰项：提取器必须只认 *_content_list_v2.json
        zf.writestr(
            "out/auto/out_content_list.json",
            json.dumps([{"type": "text", "text": "v1 decoy", "page_idx": 0}]),
        )
        zf.writestr("out/auto/out_middle.json", json.dumps({"middle": True}))
        if with_images:
            zf.writestr("out/auto/images/p0.jpg", b"fakejpg")
    return buf.getvalue()


def _transport(submit_status=202, submit_json=None, states=None, result=None,
               result_status=200, poll_status=200):
    """states: 轮询响应队列（原始 sidecar JSON，依次弹出，耗尽后复用末项）。"""
    states = list(states or [])
    zip_bytes = _result_zip() if result is None else result
    seen = {"requests": []}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["requests"].append(request)
        if request.method == "POST" and request.url.path == "/tasks":
            if submit_status != 200 and submit_status != 202:
                return httpx.Response(submit_status, text="boom")
            return httpx.Response(
                submit_status, json=submit_json or {"task_id": "job-1", "status": "pending"}
            )
        if request.method == "GET" and request.url.path == "/tasks/job-1":
            if poll_status != 200:
                return httpx.Response(poll_status, json={"detail": "Task not found"})
            st = states.pop(0) if states else {"status": "processing"}
            return httpx.Response(200, json=st)
        if request.method == "GET" and request.url.path == "/tasks/job-1/result":
            if result_status != 200:
                return httpx.Response(result_status, json={"detail": "Conflict"})
            return httpx.Response(
                200, content=zip_bytes,
                headers={"content-type": "application/zip",
                         "content-disposition": 'attachment; filename="job-1.zip"'},
            )
        return httpx.Response(404, json={"detail": "Task not found"})

    handler.seen = seen  # type: ignore[attr-defined]
    return handler


def _client(handler, **kw) -> MineruParseClient:
    kw.setdefault("poll_interval", 0.0)
    return MineruParseClient(base_url="http://127.0.0.1:16581/", timeout=kw.pop("timeout", 5),
                             transport=httpx.MockTransport(handler), **kw)


@pytest.mark.asyncio
async def test_submit_returns_task_id_and_sends_required_flags():
    handler = _transport()
    task_id = await _client(handler).submit(b"pdf-bytes", "a.pdf")
    assert task_id == "job-1"

    req = handler.seen["requests"][0]
    assert req.method == "POST" and req.url.path == "/tasks"
    body = req.read()
    # Task 0 §4/§8：默认 hybrid-engine 在实例上必炸，必须显式 pipeline；
    # 取件走 zip + content_list_v2 + images，三个开关必须带上
    assert b'name="files"' in body and b'filename="a.pdf"' in body
    assert b'name="backend"\r\n\r\npipeline' in body
    assert b'name="response_format_zip"\r\n\r\ntrue' in body
    assert b'name="return_content_list"\r\n\r\ntrue' in body
    assert b'name="return_images"\r\n\r\ntrue' in body


@pytest.mark.asyncio
async def test_submit_5xx_is_transient():
    with pytest.raises(MineruParseError) as ei:
        await _client(_transport(submit_status=500)).submit(b"pdf", "a.pdf")
    assert ei.value.transient is True


@pytest.mark.asyncio
async def test_submit_unreachable_is_transient():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(MineruParseError, match="不可达") as ei:
        await _client(handler).submit(b"pdf", "a.pdf")
    assert ei.value.transient is True


@pytest.mark.asyncio
async def test_poll_normalizes_states_and_keeps_zero_progress():
    handler = _transport(states=[
        {"status": "pending", "queued_ahead": 2},
        {"status": "processing"},
        {"status": "completed"},
        {"status": "failed", "error": "x"},
    ])
    client = _client(handler)
    assert await client.poll("job-1") == {"state": "running", "done": 0, "total": 0}
    assert await client.poll("job-1") == {"state": "running", "done": 0, "total": 0}
    assert await client.poll("job-1") == {"state": "done", "done": 0, "total": 0}
    assert await client.poll("job-1") == {"state": "failed", "done": 0, "total": 0}


@pytest.mark.asyncio
async def test_poll_unknown_task_404_not_transient():
    with pytest.raises(MineruParseError, match="not found") as ei:
        await _client(_transport(poll_status=404)).poll("job-1")
    assert ei.value.transient is False


@pytest.mark.asyncio
async def test_wait_result_polls_until_done_and_parses_zip():
    handler = _transport(states=[{"status": "processing"}, {"status": "completed"}])
    seen_progress = []

    result = await _client(handler).wait_result(
        "job-1", on_progress=lambda done, total: seen_progress.append((done, total))
    )
    assert result.md.startswith("# 合成")
    # 认 v2（list-of-lists）而非 v1 干扰项
    assert result.content_list_v2[0][0]["type"] == "page_header"
    assert result.content_list_v2[1][0]["content"]["paragraph_content"][0]["content"] == "正文一段"
    assert result.images == {"out/auto/images/p0.jpg": b"fakejpg"}
    # sidecar 无进度字段（Task 0 §8.2）：归一化进度恒 0，页计数由管线层本地负责
    assert seen_progress == [(0, 0), (0, 0)]


@pytest.mark.asyncio
async def test_wait_result_without_images_zip_yields_empty_dict():
    handler = _transport(states=[{"status": "completed"}], result=_result_zip(with_images=False))
    result = await _client(handler).wait_result("job-1")
    assert result.md.startswith("# 合成")
    assert result.images == {}


@pytest.mark.asyncio
async def test_wait_result_raises_on_failed_state():
    with pytest.raises(MineruParseError, match="failed"):
        await _client(_transport(states=[{"status": "failed"}])).wait_result("job-1")


@pytest.mark.asyncio
async def test_wait_result_timeout_is_transient():
    with pytest.raises(MineruParseError, match="timeout") as ei:
        await _client(_transport(), timeout=0.0).wait_result("job-1")
    assert ei.value.transient is True


@pytest.mark.asyncio
async def test_result_409_task_failed_not_transient():
    handler = _transport(states=[{"status": "completed"}], result_status=409)
    with pytest.raises(MineruParseError, match="failed or not retrievable") as ei:
        await _client(handler).wait_result("job-1")
    assert ei.value.transient is False


@pytest.mark.asyncio
async def test_is_available_true_and_false():
    def ok(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/health"
        return httpx.Response(200, json={"status": "healthy", "version": "3.4.5"})

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    assert await _client(ok).is_available() is True
    assert await _client(down).is_available() is False
