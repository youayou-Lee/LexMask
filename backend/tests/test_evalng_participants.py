"""eval-ng participants 单测：全部 mock HTTP（httpx MockTransport），零真端点。"""

import json

import httpx

import evalng_bootstrap as boot

has_mod = boot.load("participants/has.py")
glm_mod = boot.load("participants/glm_gen4.py")
bp_mod = boot.load("participants/backend_pipeline.py")
protocol = boot.load("engine/protocol.py")

AnswerForm = protocol.AnswerForm


def _ner_response(entities: dict, finish_reason="stop") -> dict:
    content = json.dumps(entities, ensure_ascii=False)
    return {"choices": [{"message": {"content": content}, "finish_reason": finish_reason}]}


def _mock_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _entity_item(types=None, text="张三，男。", gt=None):
    return protocol.ExamItem(id="p1", exam_id="ex", form=AnswerForm.ENTITY_TABLE,
                             payload={"text": text, "types": types or ["姓名"]}, gt=gt)


async def _probe_with(participant, item):
    return await participant.probe(item)


def test_has_sends_pinned_alignment_params():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["payload"] = json.loads(request.content)
        return httpx.Response(200, json=_ner_response({"姓名": ["张三"]}))

    p = has_mod.HasParticipant("http://ner-mock")
    p._client = _mock_client(handler)
    ans = p.probe_sync = None
    import asyncio

    answer = asyncio.run(p.probe(_entity_item(["姓名", "电话"])))
    assert answer.entities == {"姓名": ["张三"]}
    assert answer.latency_sec > 0
    body = captured["payload"]
    assert body["max_tokens"] == 4000                       # 对齐参数
    assert body["temperature"] == 0.0                       # nerq 内置常量
    assert "姓名" in body["messages"][0]["content"] and "电话" in body["messages"][0]["content"]
    assert captured["url"] == "http://ner-mock/chat/completions"


def test_has_truncation_warning():
    def handler(request):
        return httpx.Response(200, json=_ner_response({}, finish_reason="length"))

    import asyncio

    p = has_mod.HasParticipant("http://ner-mock")
    p._client = _mock_client(handler)
    answer = asyncio.run(p.probe(_entity_item()))
    assert any("截断" in w or "length" in w for w in answer.warnings)


def test_has_error_returns_empty_not_raise():
    def handler(request):
        raise httpx.ConnectError("boom")

    import asyncio

    p = has_mod.HasParticipant("http://ner-mock")
    p._client = _mock_client(handler)
    answer = asyncio.run(p.probe(_entity_item()))
    assert answer.entities == {} and answer.warnings


def test_glm_gen4_inherits_alignment():
    import asyncio

    p = glm_mod.GlmGen4Participant("http://glm-mock")
    assert p.name == "glm-gen4" and p.max_tokens == 4000 and p.timeout == 120.0


# ---------- backend pipeline ----------

class _ApiMock:
    """最小 backend API 模拟：登录/上传/vision/parse/ner/delete。"""

    def __init__(self, vision_status=200, ner_status="ok"):
        self.calls: list[tuple[str, str]] = []
        self.vision_status = vision_status
        self.ner_status = ner_status

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((request.method, path))
        if path.endswith("/auth/login"):
            return httpx.Response(200, json={"access_token": "tok"})
        if path.endswith("/files/upload"):
            return httpx.Response(200, json={"id": "f1"})
        if "/vision" in path:
            return httpx.Response(
                self.vision_status,
                json={"bounding_boxes": [{"type": "姓名", "original_text": "张三"}]},
            )
        if path.endswith("/parse"):
            return httpx.Response(200, json={"pages": []})
        if path.endswith("/ner/hybrid"):
            body = {"status": self.ner_status}
            return httpx.Response(200, json=body)
        if path.startswith("/api/v1/files/") and request.method == "DELETE":
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404, json={})


def test_backend_docx_walks_parse_and_hybrid():
    import asyncio
    import tempfile
    from pathlib import Path

    mock = _ApiMock()
    p = bp_mod.BackendPipelineParticipant("http://api-mock", "u", "p")
    p._client = _mock_client(mock.handler)
    with tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as fh:
        path = fh.name
    answer = asyncio.run(p.probe(
        protocol.ExamItem(id="d1", exam_id="ex", form=AnswerForm.ARTIFACT_FILE,
                          payload={"path": path})))
    assert answer.form == AnswerForm.ARTIFACT_FILE and answer.latency_sec >= 0
    flow = [path for _, path in mock.calls]
    assert any("/parse" in x for x in flow) and any("/ner/hybrid" in x for x in flow)
    assert any("DELETE" == m for m, _ in mock.calls)  # 尽力清理


def test_backend_pdf_pages_vision():
    import asyncio
    import tempfile

    mock = _ApiMock()
    p = bp_mod.BackendPipelineParticipant("http://api-mock", "u", "p")
    p._client = _mock_client(mock.handler)
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as fh:
        path = fh.name
    answer = asyncio.run(p.probe(
        protocol.ExamItem(id="f1", exam_id="ex", form=AnswerForm.ENTITY_TABLE,
                          payload={"path": path, "pages": 2, "carrier": "pdf"})))
    assert answer.entities == {"姓名": ["张三", "张三"]}  # 2 页聚合
    vision_calls = [1 for m, path in mock.calls if "/vision" in path]
    assert len(vision_calls) == 2


def test_backend_encrypted_pdf_reject():
    import asyncio
    import tempfile

    mock = _ApiMock(vision_status=422)
    p = bp_mod.BackendPipelineParticipant("http://api-mock", "u", "p")
    p._client = _mock_client(mock.handler)
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as fh:
        path = fh.name
    answer = asyncio.run(p.probe(
        protocol.ExamItem(id="e1", exam_id="ex", form=AnswerForm.ROBUSTNESS,
                          payload={"path": path, "expect_reject": True, "carrier": "pdf"})))
    assert answer.form == AnswerForm.ROBUSTNESS and answer.rejected is True


def test_backend_recognition_failed_raises_warning():
    import asyncio
    import tempfile

    mock = _ApiMock(ner_status="recognition_failed")
    p = bp_mod.BackendPipelineParticipant("http://api-mock", "u", "p")
    p._client = _mock_client(mock.handler)
    with tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as fh:
        path = fh.name
    answer = asyncio.run(p.probe(
        protocol.ExamItem(id="d2", exam_id="ex", form=AnswerForm.ARTIFACT_FILE,
                          payload={"path": path})))
    assert any("recognition_failed" in w for w in answer.warnings)
