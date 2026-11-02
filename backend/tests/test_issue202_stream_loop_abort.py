"""Issue#202：流式复读掐断（生成侧止损）。

背景（#198 门④实测）：病理页上 temp=0 种子调用必然复读、每次烧满 max_tokens
（~60-84s × 3 类型批 ≈ 200s，占页耗时 85%）。合法回答的长度上限=预算帽，复
读也烧满帽子——时间上不可分；内容上可分（事故日志实锤 24 字符周期循环签名）。
修复：_call_model 走流式增量解码，每 256 字符跑循环签名检测（尾部 1KB 内
≥24 字符片段出现 ≥3 次），命中即断开连接（vLLM 随之中止），finish_reason 按
length 收口 ⇒ repair→分块补查链路自动接手；通道异常自动回落非流式。

变异靶：has_client._do_chat_request_stream 内 `self._looks_like_loop(joined)`
判定——摘除该调用（恒 False）应令本文件流掐用例变红（跑整个文件）。
"""

import json
import logging
from contextlib import contextmanager

from app.services.has_client import HaSClient

LOOP_UNIT = ',"21199912255270","2HA0110'  # 事故日志同款 24 字符周期签名

VARIED_JSON = json.dumps(
    {
        "姓名": ["张三丰", "李四光", "王五洲"],
        "证件号码": ["110101199001011234", "110101199102022345"],
        "日期": ["2021-03-10", "2021-10-18", "2021-12-05"],
        "地址": ["滨江大道 88 号航运大厦 12 层", "云澜路 6 号保税仓 A7 库"],
        "电话": ["13800138001", "13800138002"],
    },
    ensure_ascii=False,
)


def _sse(*deltas, finish=None, usage=None):
    lines = []
    for d in deltas:
        lines.append("data: " + json.dumps({"choices": [{"delta": {"content": d}}]}, ensure_ascii=False))
    if finish:
        lines.append("data: " + json.dumps({"choices": [{"delta": {}, "finish_reason": finish}]}))
    if usage:
        lines.append("data: " + json.dumps({"usage": usage, "choices": []}))
    lines.append("data: [DONE]")
    return lines


class _FakeStreamResponse:
    def __init__(self, lines):
        self._lines = lines

    def raise_for_status(self):
        pass

    def iter_lines(self):
        return iter(self._lines)


class FakeHttp:
    """stream 出队脚本式响应；post 出队脚本式 JSON 体（回落通道）。"""

    def __init__(self, stream_script=None, post_script=None):
        self._stream_script = list(stream_script or [])
        self._post_script = list(post_script or [])
        self.stream_calls = 0
        self.post_calls = 0

    def stream(self, method, url, json=None):
        self.stream_calls += 1
        lines = self._stream_script.pop(0) if self._stream_script else ["data: [DONE]"]
        return _ctx(_FakeStreamResponse(lines))

    def post(self, url, json=None):
        self.post_calls += 1
        body = self._post_script.pop(0)

        class _R:
            def raise_for_status(self):
                pass

            def json(self):
                return body

        return _R()


@contextmanager
def _ctx(resp):
    yield resp


def make_client(stream_script=None, post_script=None):
    client = HaSClient()
    client._http_client = FakeHttp(stream_script, post_script)
    return client


# ---- 检测器 ----


def test_looks_like_loop_examples():
    looped = json.dumps({"姓名": ["焦先生"]}, ensure_ascii=False) + LOOP_UNIT * 12
    assert HaSClient._looks_like_loop(looped) is True
    assert HaSClient._looks_like_loop(VARIED_JSON) is False
    assert HaSClient._looks_like_loop("太短") is False


# ---- 流装配 ----


def test_stream_normal_answer_not_aborted():
    deltas = [VARIED_JSON[i : i + 200] for i in range(0, len(VARIED_JSON), 200)]
    client = make_client(stream_script=[_sse(*deltas, finish="stop", usage={"completion_tokens": 210})])
    resp = client._do_chat_request_stream("http://x/v1", {"messages": [], "max_tokens": 1992})
    data = resp.json()
    assert resp.stream_aborted is False
    assert data["choices"][0]["finish_reason"] == "stop"
    assert data["choices"][0]["message"]["content"] == VARIED_JSON
    assert data["usage"] == {"completion_tokens": 210}


def test_stream_loop_detected_aborts_with_length_semantics(caplog):
    pre = '{"姓名":["张三丰"],"日期":["2021-03-10"]'
    client = make_client(
        stream_script=[_sse(pre, LOOP_UNIT * 4, LOOP_UNIT * 4, LOOP_UNIT * 4, LOOP_UNIT * 4)]
    )
    with caplog.at_level(logging.WARNING, logger="app.services.has_client"):
        resp = client._do_chat_request_stream("http://x/v1", {"messages": [], "max_tokens": 1992})
    data = resp.json()
    assert resp.stream_aborted is True
    assert data["choices"][0]["finish_reason"] == "length", "掐断按 length 收口接恢复链路"
    assert "HaS stream loop-abort" in caplog.text


# ---- 回落 ----


def test_fallback_to_non_stream_on_stream_exception():
    # 流处理自身缺陷（非状态/传输错误）：回落非流式，post 恰好接管一次
    body = {"choices": [{"finish_reason": "stop", "message": {"content": "{}"}}]}

    class FakeHttpStreamRaises(FakeHttp):
        def stream(self, method, url, json=None):
            self.stream_calls += 1
            raise RuntimeError("broken")

    client = HaSClient()
    client._http_client = FakeHttpStreamRaises(post_script=[body])
    resp = client._do_chat_request("http://x/v1", {"messages": []})
    assert client._http_client.stream_calls == 1
    assert client._http_client.post_calls == 1
    assert resp.json() == body


# ---- 恢复链集成 ----


def test_ner_with_stream_abort_recovers_via_requery(caplog):
    # 种子流式输出：合法前缀 + 复读（检测命中即掐，finish=length）
    pre = '{"姓名":["张三丰"]'
    loop_part = LOOP_UNIT * 8
    text = "焦先生 男 36岁 咨询项目经理 2016.04 - 至今 2012.07 - 2013.07"
    client = make_client(
        stream_script=[
            _sse(pre, loop_part, loop_part, loop_part),  # 主查：复读→掐
            _sse('{"日期":["2016.04 - 至今"]}', finish="stop"),  # 补查（text<600 单块）
        ]
    )
    with caplog.at_level(logging.WARNING, logger="app.services.has_client"):
        result = client.ner(text, ["姓名", "日期"], type_guidance=[], temperature=0.0)

    assert client._http_client.stream_calls == 2, "主查 1 次 + 补查 1 次"
    assert result.get("姓名"), "种子修复出的桶保留"
    assert result.get("日期") == ["2016.04 - 至今"], "缺失类型由补查闭合"
    assert "HaS stream loop-abort" in caplog.text
