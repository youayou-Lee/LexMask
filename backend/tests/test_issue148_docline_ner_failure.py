"""Issue#148 专项用例：文本层文档级链「单块 NER 失败不再被静默吞成 0 实体」。

对应验收方案 v4 §4（本机底稿，不入库）：

- **B 组**（失败识别，**打在真 `HaSClient`** 上，只把 `httpx` 传输换成 `MockTransport`）：
  AC-B0…B6 —— 证明 Issue 点名的三类上游故障（HTTP 4xx / 超时 / 输出不可解析·截断）
  *真的*都产出 `ok=False`。段1 质疑③ 的教训：只把 `({}, False)` 元组喂进去，
  等于把结论当既成事实，覆盖不了「只在 400 置 False」的半修。
- **E 组**（成立证据，**旧代码上必须 FAIL**）：AC-E1 多块混合覆盖率、AC-E3 入口负缓存
  marker、AC-E5 并发部分失败。
- **G 组**（回归护栏，恒真）：AC-G1 异常路径、AC-G2b 全失败终态、AC-E6 退化输入。

三条装置纪律（门①.5 段2/段3 处置）：

1. 每次 `HaSClient()` 都会探测 `GET /models`（`has_client.py:254`）⇒ MockTransport
   路由器必须接住它，且它**不计入** chat 调用次数（段3 N-5）。
2. `httpx.ReadTimeout` 在 `RETRYABLE_HTTPX`（`app/core/retry.py:89-95`）内 ⇒ 超时会被
   `retry_sync` 重试到 **3 次尝试**；`httpx.HTTPStatusError`（400）**不在**表内 ⇒
   **只 1 次**。两者互为对照（段3 N-1）。
3. `ner_breaker`（阈值 3）与 `HaSClient` 的类级缓存都是全局共享 ⇒ 由
   `tests/conftest.py` 的 autouse fixture 每例前后复位（段2 Critical C-1）。
"""
from __future__ import annotations

import asyncio
import threading
import time

import httpx

from app.core.config import settings
from app.services import text_layer_doc_source as source
from app.services import text_layer_docline_service as docline
from app.services.has_client import HaSClient

_TEXT = "张三是被告人，李四是证人。"


# ------------------------------------------------------------------ 装置


def _answer(content: str) -> httpx.Response:
    return httpx.Response(
        200, json={"choices": [{"message": {"role": "assistant", "content": content}}]}
    )


def _models_response() -> httpx.Response:
    model = str(getattr(settings, "HAS_TEXT_MODEL_NAME", "") or "mock-ner")
    return httpx.Response(200, json={"data": [{"id": model, "max_model_len": 4096}]})


def _fake_transport(monkeypatch, chat_handler) -> list[httpx.Request]:
    """把 `httpx.Client` 换成带 `MockTransport` 的实例，返回 chat 请求列表。"""
    calls: list[httpx.Request] = []
    real_client = httpx.Client

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return _models_response()
        calls.append(request)
        return chat_handler(request)

    monkeypatch.setattr(
        httpx,
        "Client",
        lambda *args, **kwargs: real_client(
            *args, **{**kwargs, "transport": httpx.MockTransport(handler)}
        ),
    )
    return calls


def _ha_client(monkeypatch, chat_handler, max_retries: int | None = None):
    calls = _fake_transport(monkeypatch, chat_handler)
    return HaSClient(base_url="http://mock-ner/v1", max_retries=max_retries), calls


def _record_error_labels(monkeypatch) -> list[dict]:
    """替下 `NER_ERRORS`（`has_client.py:1206` 在 except 内 import）以取证 error_type。"""
    recorded: list[dict] = []

    class _Recorder:
        def labels(self, **kwargs):
            recorded.append(kwargs)
            return self

        def inc(self):
            return None

    monkeypatch.setattr("app.core.metrics.NER_ERRORS", _Recorder())
    return recorded


def _page(no: int, words: list[str], width: float = 595.0, height: float = 842.0):
    boxes = []
    x = 50.0
    for word in words:
        boxes.append(docline.TextLayerWord(x, 700.0, x + 40.0, 720.0, word))
        x += 45.0
    return docline.TextLayerPage(no=no, width=width, height=height, words=boxes)


def _snapshot(path, file_type: str = "pdf_scanned") -> dict:
    return {"file_path": str(path), "file_type": file_type, "is_scanned": True}


class _ScriptedDoclineClient:
    """文档级链替身：**同时**实现 `ner`（旧语义）与 `ner_checked`（新语义）。

    这是 AC-E0 的硬要求（段2 Critical C-6）：只实现 `ner_checked` 时，旧代码会在
    `has_client.ner(...)` 处抛 `AttributeError` 被 `except Exception` 吞掉，
    于是「旧代码红」来自异常而不是我们要钉的值差异 —— 测的不是同一件事。

    - `ner`：旧语义下失败与「无实体」同形，一律静默返回 `{}`；
    - `ner_checked`：按 `fail_markers` 报 `({}, False)`，其余返回带值的结果。
    """

    def __init__(self, fail_markers: tuple[str, ...] = (), value_of=None):
        self.fail_markers = tuple(fail_markers)
        self.value_of = value_of
        self.seen: list[str] = []

    def ner(self, text, labels=None, **kwargs):
        return {}

    def ner_checked(self, text, labels=None, **kwargs):
        self.seen.append(text)
        if any(marker in text for marker in self.fail_markers):
            return {}, False
        if self.value_of is not None:
            return {"姓名": list(self.value_of(text))}, True
        return {"姓名": ["张三"]}, True


class _RaisingDoclineClient(_ScriptedDoclineClient):
    """走原生 `except Exception` 分支（AC-G1）。"""

    def ner(self, text, labels=None, **kwargs):
        raise RuntimeError("mock upstream boom")

    def ner_checked(self, text, labels=None, **kwargs):
        raise RuntimeError("mock upstream boom")


def _mixed_pages(marker: str = "失败标记词"):
    """两块文本、且 `marker` 恰好落在其中一块里的卷。"""
    return [
        _page(1, ["证据材料卷宗"] * 30),
        _page(2, [marker] + ["证据材料卷宗"] * 30),
    ]


def _chunk_layout(monkeypatch, pages, chunk_chars: int = 200):
    monkeypatch.setattr(settings, "TEXT_LAYER_DOCLINE_CHUNK_CHARS", chunk_chars)
    monkeypatch.setattr(settings, "TEXT_LAYER_DOCLINE_CHUNK_OVERLAP", 0)
    stream, _spans = docline.build_stream(pages)
    chunks = docline.iter_chunks(stream, chunk_chars=chunk_chars, overlap=0)
    return stream, chunks


# ------------------------------------------------------------------ B 组：真 HaSClient


def test_b1_http_400_is_failure_and_is_not_retried(monkeypatch):
    labels = _record_error_labels(monkeypatch)

    def chat(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "context length exceeded"}})

    client, calls = _ha_client(monkeypatch, chat)
    result, ok = client.ner_checked(_TEXT, ["姓名"])

    assert (result, ok) == ({}, False), "HTTP 400 必须报 ok=False（旧行为：静默 {}）"
    assert len(calls) == 1, "HTTPStatusError 不在 RETRYABLE_HTTPX 内 ⇒ 不得重试"
    assert labels and all(item.get("error_type") == "http_400" for item in labels), labels


def test_b2_read_timeout_is_failure_and_retries_three_times(monkeypatch):
    labels = _record_error_labels(monkeypatch)
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)  # retry_sync 的退避

    def chat(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("mock read timeout")

    client, calls = _ha_client(monkeypatch, chat)
    result, ok = client.ner_checked(_TEXT, ["姓名"])

    assert (result, ok) == ({}, False)
    assert len(calls) == 3, "ReadTimeout ∈ RETRYABLE_HTTPX ⇒ max_retries=2 ⇒ 共 3 次尝试"
    assert labels and all(item.get("error_type") == "timeout" for item in labels), labels

    # 段2 Critical C-1 的现场证据：3 次 record_failure ≥ threshold(3)
    # ⇒ 模块级单例被打到 open。这就是 conftest 必须复位它的原因。
    from app.core.circuit_breaker import ner_breaker

    assert ner_breaker.is_open is True


def test_b3a_unparseable_single_batch_is_failure(monkeypatch, caplog):
    def chat(_request: httpx.Request) -> httpx.Response:
        return _answer("模型今天不想输出 JSON")

    client, calls = _ha_client(monkeypatch, chat)
    with caplog.at_level("WARNING"):
        result, ok = client.ner_checked(_TEXT, ["姓名"])

    assert (result, ok) == ({}, False), "整段输出不可解析 ≠ 模型答「无实体」"
    assert len(calls) == 1, "不可解析不重试，也不走 rebatch（标签数 ≤ 24）"
    messages = " | ".join(record.getMessage() for record in caplog.records)
    assert "could not be parsed as JSON" in messages, messages
    assert "HaS NER 失败" not in messages, "走的是解析失败分支（:1127），不是 :1223 except 分支"


def test_b3b_repaired_truncated_result_lacking_all_types_is_failure(monkeypatch, caplog):
    def chat(_request: httpx.Request) -> httpx.Response:
        # 截断/损坏 ⇒ 走 json_repair 修复；修出的桶里没有任何请求类型。
        return _answer('{"地址": ["北京市朝阳区"], "备注": ["截断的')

    client, calls = _ha_client(monkeypatch, chat)
    with caplog.at_level("WARNING"):
        result, ok = client.ner_checked(_TEXT, ["姓名"])

    assert ok is False, "截断家族：修复后的残缺结果不得被当成完整结论"
    assert "姓名" not in result
    assert len(calls) == 1, "该支明确不自我补查（死锁规避）"
    messages = " | ".join(record.getMessage() for record in caplog.records)
    assert "lacks every requested type" in messages, messages


def test_b4_valid_answer_is_ok_true(monkeypatch):
    def chat(_request: httpx.Request) -> httpx.Response:
        return _answer('{"姓名": ["张三"]}')

    client, calls = _ha_client(monkeypatch, chat)
    result, ok = client.ner_checked(_TEXT, ["姓名"])

    assert ok is True, "正向对照：B 组用例的判别力必须建立在「真成功仍报 True」之上"
    assert result.get("姓名") == ["张三"]
    assert len(calls) == 1


def test_b5_deduplicated_request_without_cacheable_result_is_failure(monkeypatch):
    """并发同 payload 去重：第二者不得把「无结论」当成成功。"""
    release = threading.Event()
    started = threading.Event()

    def chat(_request: httpx.Request) -> httpx.Response:
        started.set()
        release.wait(timeout=10.0)  # 让第二者进入 inflight 等待后再返回
        return _answer("不是 JSON")

    client, calls = _ha_client(monkeypatch, chat)

    results: list[tuple[dict, bool]] = []

    def run():
        results.append(client.ner_checked(_TEXT, ["姓名"]))

    first = threading.Thread(target=run, daemon=True)
    first.start()
    assert started.wait(timeout=10.0), "首个请求未发出"
    second = threading.Thread(target=run, daemon=True)
    second.start()
    time.sleep(0.3)  # 让第二者进入 inflight 等待（此处不 mock sleep）
    release.set()
    first.join(timeout=10.0)
    second.join(timeout=10.0)

    assert len(calls) == 1, "同 payload 必须被 inflight 去重，不得发第二次 HTTP"
    assert len(results) == 2
    assert any(ok is False for _result, ok in results), "去重后的第二者必须拿到 ok=False"


def test_b6_rebatch_aggregation_flip_is_failure(monkeypatch):
    """>24 类 + 长文本 ⇒ 顶层单批、解析失败后重分批，聚合结果必须翻转 ok。"""
    types = [f"类型{index:02d}" for index in range(1, 31)]
    long_text = "被告人陈述事实。" * 800

    planned = HaSClient.plan_completion_batches(types, long_text)
    assert len(planned) == 1, "顶层必须是单批，否则走的是预分批路径（:991/:1035），不是 :1124"

    def chat(_request: httpx.Request) -> httpx.Response:
        return _answer("依然不是 JSON")

    client, calls = _ha_client(monkeypatch, chat)
    result, ok = client.ner_checked(long_text, types)

    assert (result, ok) == ({}, False)
    assert len(calls) > 1, "必须发生「解析失败后重分批」（1 次顶层 + N 次子批）"


# ------------------------------------------------------------------ E 组：成立证据


def test_e1_mixed_chunks_count_failure_and_shrink_coverage(monkeypatch):
    pages = _mixed_pages()
    stream, chunks = _chunk_layout(monkeypatch, pages)
    assert len(chunks) >= 2, "本用例需要 ≥2 块才有判别力（段1 质疑②：单块覆盖率会退化）"
    failing = [index for index, chunk in enumerate(chunks) if "失败标记词" in chunk[0]]
    assert len(failing) == 1, "失败标记词必须恰好落在一块里"

    client = _ScriptedDoclineClient(fail_markers=("失败标记词",))
    result = asyncio.run(docline.run_document_ner(pages, ["姓名"], has_client=client))

    assert result.chunks == len(chunks)
    assert result.failed_chunks == 1, "修复前这里恒为 0（旧代码上本断言必红）"
    assert result.http_errors == 1
    ok_chunks = [chunk for index, chunk in enumerate(chunks) if index not in failing]
    assert result.covered_chars == docline._union_coverage(ok_chunks, len(stream))
    assert 0.0 < result.coverage_ratio < 1.0, "失败块必须从覆盖率里真实扣除"


def test_e5_concurrent_partial_failure_keeps_surviving_values(monkeypatch):
    pages = [
        _page(1, ["第一页内容"] * 20),
        _page(2, ["失败标记词"] + ["第二页内容"] * 20),
        _page(3, ["第三页内容"] * 20),
    ]
    _stream, chunks = _chunk_layout(monkeypatch, pages)
    assert len(chunks) >= 2

    def value_of(text: str) -> list[str]:
        return [word for word in ("第一页内容", "第二页内容", "第三页内容") if word in text]

    client = _ScriptedDoclineClient(fail_markers=("失败标记词",), value_of=value_of)
    result = asyncio.run(docline.run_document_ner(pages, ["姓名"], has_client=client))

    assert result.failed_chunks == 1, "并发下必须只记那一块失败"
    values = {entity.value for entity in result.entities}
    assert "失败标记词" not in values
    surviving = {"第一页内容", "第三页内容"} & {word for word in ("第一页内容", "第二页内容", "第三页内容")}
    assert values & surviving, f"成功块的值不得因同批失败而丢失（拿到 {values}）"


def test_e3_entry_turns_failed_chunk_into_negative_marker(monkeypatch, tmp_path):
    """入口层：失败块 ⇒ `_TransientIneligible` ⇒ `boxes_for_page` 落负缓存 marker。"""
    monkeypatch.setattr(settings, "TEXT_LAYER_DOCLINE_ENABLED", True)
    pages = _mixed_pages()
    monkeypatch.setattr(source.docline, "extract_text_layer_pages", lambda _path: pages)
    monkeypatch.setattr(source.docline, "sample_ink_alignment", lambda _path, _pages: 0.18)
    monkeypatch.setattr(source, "has_client", _ScriptedDoclineClient(fail_markers=("失败标记词",)))
    source.reset_memo()

    path = tmp_path / "scan.pdf"
    path.write_bytes(b"%PDF-1.4 stub")

    boxes, memo = asyncio.run(
        source.boxes_for_page(
            file_id="e" * 8, snapshot=_snapshot(path), page=1, ocr_has_types=None
        )
    )

    assert boxes is None and memo is None, "整卷不得被判为「文档级链已处理」"
    reasons = [str(entry.get("reason") or "") for entry in source._MEMO.values()]
    assert any(reason.startswith("failed_chunks:") for reason in reasons), reasons
    # 旧代码上失败块静默成功 ⇒ 不会抛 _TransientIneligible ⇒ 上方断言必红。


def test_e4_fallback_is_memoized_and_does_not_rehit_upstream(monkeypatch, tmp_path):
    """终态行为：失败卷 ⇒ 回落旧链（不产出文本层框），且 TTL 内不再重复打上游。

    诚实边界：这里钉的是 `text_layer_doc_source` 这一层接缝——负缓存 ⇒ 不产出框 ⇒
    调用方回落经典 OCR+NER 链。真正的 orchestrator 端到端终态与
    `pipeline_status["text_layer_doc"]` 观测面须在云门④ canary 上验（本机无 GPU/HaS）。
    """
    monkeypatch.setattr(settings, "TEXT_LAYER_DOCLINE_ENABLED", True)
    pages = _mixed_pages()
    calls = {"n": 0}

    original_run = docline.run_document_ner  # 先取原函数，避免补丁后自递归

    def _counting_run(*args, **kwargs):
        calls["n"] += 1
        return original_run(*args, **kwargs)

    monkeypatch.setattr(source.docline, "extract_text_layer_pages", lambda _path: pages)
    monkeypatch.setattr(source.docline, "sample_ink_alignment", lambda _path, _pages: 0.18)
    monkeypatch.setattr(source.docline, "run_document_ner", _counting_run)
    monkeypatch.setattr(source, "has_client", _ScriptedDoclineClient(fail_markers=("失败标记词",)))
    source.reset_memo()

    path = tmp_path / "scan.pdf"
    path.write_bytes(b"%PDF-1.4 stub")
    snapshot = _snapshot(path)

    first = asyncio.run(
        source.boxes_for_page(file_id="f" * 8, snapshot=snapshot, page=1, ocr_has_types=None)
    )
    second = asyncio.run(
        source.boxes_for_page(file_id="f" * 8, snapshot=snapshot, page=2, ocr_has_types=None)
    )

    assert first == (None, None) and second == (None, None), "失败卷不得产出文本层框（须回落旧链）"
    assert calls["n"] == 1, "TTL 内重复访问不得再次构建（否则每次翻页都会重打一遍上游 NER）"
    markers = [entry for entry in source._MEMO.values() if entry.get("unusable")]
    assert markers, "失败块必须落负缓存 marker"
    assert markers[0]["retry_at"] != float("inf"), "失败块是 TTL 负缓存，不是永久不可用"


# ------------------------------------------------------------------ G 组：护栏


def test_g1_exception_path_still_counts_failure(monkeypatch):
    pages = [_page(1, ["证据材料卷宗"] * 30)]
    client = _RaisingDoclineClient()
    result = asyncio.run(docline.run_document_ner(pages, ["姓名"], has_client=client))

    assert result.failed_chunks == 1
    assert result.http_errors == 1
    assert result.entities == []


def test_g2b_all_chunks_failing_keeps_ratio_defined(monkeypatch):
    pages = [_page(1, ["失败标记词"] * 30)]
    _stream, chunks = _chunk_layout(monkeypatch, pages)
    assert chunks

    client = _ScriptedDoclineClient(fail_markers=("失败标记词",))
    result = asyncio.run(docline.run_document_ner(pages, ["姓名"], has_client=client))

    assert result.failed_chunks == result.chunks >= 1
    assert result.covered_chars == 0
    assert result.coverage_ratio == 0.0, "全失败必须给出有定义的 0 覆盖率，不得 ZeroDivisionError"


def test_e6_degenerate_inputs_keep_ratio_defined():
    client = _ScriptedDoclineClient()
    empty = asyncio.run(docline.run_document_ner([], ["姓名"], has_client=client))
    assert empty.chunks == 0
    assert empty.coverage_ratio == 0.0

    blank = asyncio.run(docline.run_document_ner([_page(1, [])], ["姓名"], has_client=client))
    assert blank.coverage_ratio == 0.0

