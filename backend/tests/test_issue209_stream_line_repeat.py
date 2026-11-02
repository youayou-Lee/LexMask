"""Issue#209 D：流式「归一化行复读」早中止判据（默认关）。

背景：#202 的复读签名只抓**逐字节周期**；真实页 133 的病态是「同一行骨架、只差
编号」的枚举（水印碎片 `映峰1450120141` / `映峰1450120147` …），签名必漏，只能
烧满 max_tokens（实测单请求 83.4s / 65.5s）。本判据把完整行里的数字统一成 `#`
后计数，同一键累计 ≥ MIN_OCCURRENCES 次即断开。

**为什么默认关（门④实测后改的）**：模型输出的是 JSON 行
`{"text": "*721720240174000394", "type": "编号"}`（≈45 字符，含 CJK「编号」），
同一归一化键会重复 74 次——而这是**真实报关单号列**（页 133 真值：编号 83 个值、
车牌号 29 个）。判据在第 4 行就断开，截断恢复又只补查"整桶缺失"的类型 ⇒ 正在
枚举的类型只留切点前 ~4 个值，直接砍召回。故只作为「确认某类卷的枚举确为水印
碎片」时的可选开关；打开前必须复测召回。

变异靶：`_do_chat_request_stream` 里 `line_guard.feed(...)` 的接线——摘除后
（恒不命中）本文件「开启后掐断」用例变红。
"""

import json
import logging
from contextlib import contextmanager

import pytest

from app.core.config import settings
from app.services.has_client import HaSClient, _NormalizedLineRepeatGuard

# 水印碎片同款：行骨架相同、只差 17 位编号
FRAGMENT_LINES = [
    "映峰1450120141",
    "映峰1450120147",
    "映峰1450120153",
    "映峰1450120159",
    "映峰1450120165",
]

# 真实报关单号列（纯数字）：必须**不**被本判据计入
DIGIT_LINES = [
    "*721720240174000394",
    "*721720240174000395",
    "*721720240174000396",
    "*721720240174000397",
]


def _lines(*rows: str) -> str:
    return "".join(row + "\n" for row in rows)


def _sse(*deltas, finish=None, usage=None):
    lines = []
    for d in deltas:
        lines.append(
            "data: " + json.dumps({"choices": [{"delta": {"content": d}}]}, ensure_ascii=False)
        )
    if finish:
        lines.append(
            "data: " + json.dumps({"choices": [{"delta": {}, "finish_reason": finish}]})
        )
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


class _FakeHttp:
    def __init__(self, stream_script):
        self._stream_script = list(stream_script)
        self.stream_calls = 0

    def stream(self, method, url, json=None):
        self.stream_calls += 1
        lines = self._stream_script.pop(0) if self._stream_script else ["data: [DONE]"]

        @contextmanager
        def ctx():
            yield _FakeStreamResponse(lines)

        return ctx()


def make_client(stream_script):
    client = HaSClient()
    client._http_client = _FakeHttp(stream_script)
    return client


# ---------------------------------------------------------------- 默认关


def test_default_is_off():
    """默认关：这是门④实测后的拍板（打开会砍真实枚举召回）。"""
    assert settings.HAS_STREAM_LINE_REPEAT_ABORT is False


def test_guard_not_constructed_when_off(caplog):
    """关着的时候连计数对象都不建 ⇒ 通路与 #202 现状逐字节一致。"""
    built = []

    class _Spy(_NormalizedLineRepeatGuard):
        def __init__(self, **kwargs):
            built.append(kwargs)
            super().__init__(**kwargs)

    client = make_client([_sse(_lines(*FRAGMENT_LINES), finish="stop")])
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("app.services.has_client._NormalizedLineRepeatGuard", _Spy)
        with caplog.at_level(logging.WARNING, logger="app.services.has_client"):
            resp = client._do_chat_request_stream("http://x/v1", {"messages": []})

    assert built == [], "flag off must not even instantiate the guard"
    assert resp.stream_aborted is False
    assert "normalized line repetition" not in caplog.text


# ---------------------------------------------------------------- 开启后掐断


def _enable(mp, **overrides):
    mp.setattr(settings, "HAS_STREAM_LINE_REPEAT_ABORT", True, raising=False)
    mp.setattr(settings, "HAS_STREAM_LINE_REPEAT_MIN_OCCURRENCES", 4, raising=False)
    mp.setattr(settings, "HAS_STREAM_LINE_REPEAT_MAX_LINE_CHARS", 48, raising=False)
    mp.setattr(settings, "HAS_STREAM_LINE_REPEAT_MIN_CJK_CHARS", 1, raising=False)
    for key, value in overrides.items():
        mp.setattr(settings, key, value, raising=False)


def test_enabled_aborts_on_fragment_family(caplog):
    """开启后：同一「姓名碎片+编号」族到阈值即断开，按 length 收口。"""
    client = make_client([_sse(_lines(*FRAGMENT_LINES), finish=None)])
    with pytest.MonkeyPatch.context() as mp:
        _enable(mp)
        with caplog.at_level(logging.WARNING, logger="app.services.has_client"):
            resp = client._do_chat_request_stream("http://x/v1", {"messages": [], "max_tokens": 1992})

    assert resp.stream_aborted is True
    assert resp.json()["choices"][0]["finish_reason"] == "length"
    assert "normalized line repetition detected" in caplog.text


def test_enabled_but_pure_digit_enumeration_not_aborted(caplog):
    """CJK 闸：真实报关单号列（纯数字行）即使重复也不掐断（防砍真实召回）。"""
    client = make_client([_sse(_lines(*DIGIT_LINES * 3), finish="stop")])
    with pytest.MonkeyPatch.context() as mp:
        _enable(mp)
        with caplog.at_level(logging.WARNING, logger="app.services.has_client"):
            resp = client._do_chat_request_stream("http://x/v1", {"messages": []})

    assert resp.stream_aborted is False
    assert "normalized line repetition" not in caplog.text


def test_enabled_below_threshold_not_aborted(caplog):
    """族内不到阈值不断开（阈值 4，只给 3 行）。"""
    client = make_client([_sse(_lines(*FRAGMENT_LINES[:3]), finish="stop")])
    with pytest.MonkeyPatch.context() as mp:
        _enable(mp)
        with caplog.at_level(logging.WARNING, logger="app.services.has_client"):
            resp = client._do_chat_request_stream("http://x/v1", {"messages": []})

    assert resp.stream_aborted is False


# ---------------------------------------------------------------- 计数语义（纯函数）


def test_guard_counts_complete_lines_only():
    """半行不计一次出现：同一行被增量切开时不会被重复计数。"""
    guard = _NormalizedLineRepeatGuard(min_occurrences=2, max_line_chars=48, min_cjk_chars=1)
    assert guard.feed("映峰1450120141\n") is None
    # 第二行只喂了半行（无换行）→ 不能算第 2 次
    assert guard.feed("映峰1450120142") is None
    # 补上换行才算
    hit = guard.feed("\n")
    assert hit is not None and hit[1] == 2


def test_guard_normalizes_digits_and_skips_long_lines():
    guard = _NormalizedLineRepeatGuard(min_occurrences=3, max_line_chars=20, min_cjk_chars=1)
    assert guard.feed("映峰1450120141\n") is None
    # 超长行（> 20 字符）不计
    assert guard.feed("映峰" + "1450120141" * 3 + "\n") is None
    assert guard.feed("映峰1450120147\n") is None
    hit = guard.feed("映峰1450120153\n")
    assert hit == ("映峰##########", 3)