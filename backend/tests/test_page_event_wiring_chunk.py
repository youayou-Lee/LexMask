"""Issue#279 A2.2：chunk_failed 双点接线（docline / has_vision）+ 卷级 ner_done 逐钉。

两处生产接线（spec §2.1 事件源）：
* docline `run_document_ner`：`ask()` 异常与 ok=False 两路 → `chunk_failed`
  （stage="docline"，块号经 enumerate → chunk_index，kind ∈ main/residual）；
  gather 完成后一条卷级 `ner_done{calls, supplemented, ms, entities}`；
* hybrid_ner_service `extract`：`gather(return_exceptions=True)` 的块异常 →
  `chunk_failed`（stage="has_vision"）。

要点：
* 调用**真实** `run_document_ner`（非 Task 4 的整函数打桩）：页替身用模块自身的
  `TextLayerPage/TextLayerWord` 数据类，两页各一词 ⇒ 单块流，块序确定；
* hybrid 沿 test_has_text_ner_concurrency 的既有替身模式（子类覆写
  `_extract_has_chunk_entities` + 打桩 chunk 构建器）；
* fail-open 钉法：失败路径计数（http_errors/failed_chunks）与 gather 吞异常语义
  完全不变；无上下文时事件丢弃计数、主链返回不受影响；
* PII 钉法：块文本（张三/李四）不得出现在事件行任何字段里。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from app.core.config import settings
from app.services import page_event_log as pel
from app.services import text_layer_docline_service as docline
from app.services.hybrid_ner_service import HybridNERService


@pytest.fixture()
def log_env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "PAGE_EVENT_LOG_ENABLED", True)
    monkeypatch.setattr(settings, "PAGE_EVENT_LOG_DIR", str(tmp_path / "logs"))
    pel.reset_for_tests()
    yield tmp_path / "logs"
    pel.reset_for_tests()


def _events(log_env, job_id="jobc", item_id="itemc"):
    path = log_env / job_id / f"{item_id}.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _doc_pages():
    """TextLayerPage/TextLayerWord 真身替身：两页各一词 ⇒ build_stream 单块。"""
    return [
        docline.TextLayerPage(no=1, width=100.0, height=100.0,
                              words=[docline.TextLayerWord(0, 0, 10, 10, "张三")]),
        docline.TextLayerPage(no=2, width=100.0, height=100.0,
                              words=[docline.TextLayerWord(0, 0, 10, 10, "李四")]),
    ]


class _RaisingClient:
    """`ask()` 异常分支替身：ner_checked 抛 ValueError（经 asyncio.to_thread 传播）。"""

    def __init__(self):
        self.calls = 0

    def ner_checked(self, text, labels):
        self.calls += 1
        raise ValueError("upstream boom")


class _RaisingAfterFirstClient:
    """主查成功、残差查抛异常 ⇒ 钉 kind="residual" 的块失败事件。"""

    def __init__(self):
        self.calls = 0

    def ner_checked(self, text, labels):
        self.calls += 1
        if self.calls == 1:
            return {"人名": ["张三"]}, True
        raise ValueError("residual boom")


class _OkFalseClient:
    """Issue#148 显式结论缺失路径：ner_checked 返回 ok=False。"""

    def __init__(self):
        self.calls = 0

    def ner_checked(self, text, labels):
        self.calls += 1
        return {}, False


class _OkClient:
    def __init__(self, result):
        self.calls = 0
        self._result = result

    def ner_checked(self, text, labels):
        self.calls += 1
        return dict(self._result), True


def _run_docline(client, *, residual_pass=False, labels=("人名",)):
    return asyncio.run(docline.run_document_ner(
        _doc_pages(), list(labels), has_client=client,
        chunk_chars=200, overlap=0, residual_pass=residual_pass,
    ))


# ---------------------------------------------------------------- docline 异常路


def test_docline_chunk_exception_emits_chunk_failed(log_env):
    client = _RaisingClient()
    with pel.event_context("jobc", "itemc"):
        result = _run_docline(client)
    # 失败路径计数完全不变（fail-open：埋点不改行为）
    assert client.calls == 1  # 单块流 ⇒ 恰一次主查
    assert result.http_errors == 1
    assert result.failed_chunks == 1
    assert result.ner_calls == 0
    failed = [e for e in _events(log_env) if e["event"] == "chunk_failed"]
    assert len(failed) == 1
    event = failed[0]
    assert event["stage"] == "docline"
    assert event["chunk_index"] == 0
    assert event["kind"] == "main"
    assert event["error_type"] == "ValueError"
    assert event["message_sanitized"] == "upstream boom"
    # 代码帧堆栈：经 to_thread 传播，await 帧落在 docline 服务文件里
    assert any("text_layer_docline_service.py" in f for f in event["traceback_frames"])
    assert all("upstream boom" not in f for f in event["traceback_frames"])  # 帧不含消息
    assert event["page"] is None  # 卷级链路：不挂页（writer 上下文默认解析）
    # PII：块文本不得进事件任何字段
    assert "张三" not in json.dumps(event, ensure_ascii=False)
    assert "李四" not in json.dumps(event, ensure_ascii=False)


def test_docline_ok_false_emits_no_usable_result(log_env):
    client = _OkFalseClient()
    with pel.event_context("jobc", "itemc"):
        result = _run_docline(client)
    assert client.calls == 1
    assert result.http_errors == 1 and result.failed_chunks == 1
    failed = [e for e in _events(log_env) if e["event"] == "chunk_failed"]
    assert len(failed) == 1
    event = failed[0]
    assert event["stage"] == "docline" and event["chunk_index"] == 0
    assert event["kind"] == "main"
    assert event["error_type"] == "NoUsableResult"
    assert event["traceback_frames"] == []  # 非异常路径：无堆栈
    assert "no usable result" in event["message_sanitized"]


def test_docline_residual_failure_kinds_residual(log_env, monkeypatch):
    monkeypatch.setattr(docline, "_residual_text",
                        lambda text, hits: "残余未覆盖片段内容填充" * 15)
    client = _RaisingAfterFirstClient()
    with pel.event_context("jobc", "itemc"):
        result = _run_docline(client, residual_pass=True)
    assert client.calls == 2  # 主查 + 残差
    assert result.ner_calls == 1 and result.residual_calls == 0
    failed = [e for e in _events(log_env) if e["event"] == "chunk_failed"]
    assert len(failed) == 1
    assert failed[0]["kind"] == "residual"
    assert failed[0]["chunk_index"] == 0
    assert failed[0]["error_type"] == "ValueError"
    assert failed[0]["stage"] == "docline"


# ---------------------------------------------------------------- docline ner_done


def test_docline_ner_done_on_success(log_env):
    client = _OkClient({"人名": ["张三"]})
    with pel.event_context("jobc", "itemc"):
        result = _run_docline(client)
    assert result.ner_calls == 1
    done = [e for e in _events(log_env) if e["event"] == "ner_done"]
    assert len(done) == 1
    event = done[0]
    assert event["calls"] == 1
    assert event["supplemented"] == 0
    assert event["entities"] == 1  # values_by_type 非空去重值计数
    assert isinstance(event["ms"], int) and event["ms"] >= 0
    assert event["page"] is None  # 卷级事件不挂页


def test_docline_no_context_drops_but_run_succeeds(log_env):
    """fail-open：无识别上下文时事件丢弃计数、主链返回不受影响。"""
    client = _OkClient({"人名": ["张三"]})
    before = pel.stats()["dropped_no_context"]
    result = _run_docline(client)  # 不包 event_context
    assert result.ner_calls == 1  # 主链结果不受影响
    assert pel.stats()["dropped_no_context"] == before + 1  # 只有 ner_done 试写一次
    assert _events(log_env) == []


# ---------------------------------------------------------------- hybrid has_vision


class _NameType:
    id = "PERSON"
    name = "姓名"


class _ChunkFailService(HybridNERService):
    """c1 块抛异常、其余块成功的替身（沿 test_has_text_ner_concurrency 模式）。"""

    def __init__(self):
        super().__init__()
        self.failed_chunk = "c1"

    async def _extract_has_chunk_entities(self, chunk, text, semantic_entity_types,
                                          enabled_type_ids):
        if chunk == self.failed_chunk:
            raise RuntimeError("vision boom")
        return []


def _patch_hybrid_common(monkeypatch, service):
    monkeypatch.setattr(service, "_build_has_candidate_chunks", lambda text: ["c0", "c1"])
    monkeypatch.setattr(service, "_select_has_semantic_types", lambda types: types)
    monkeypatch.setattr(service, "_cross_validate", lambda entities, text, ids: entities)
    monkeypatch.setattr(service.has_service, "is_available", lambda: True)


def test_hybrid_chunk_exception_emits_chunk_failed(log_env, monkeypatch):
    monkeypatch.setattr(settings, "HAS_NER_MAX_PARALLEL_REQUESTS", 3)
    service = _ChunkFailService()
    _patch_hybrid_common(monkeypatch, service)
    with pel.event_context("jobc", "itemc"):
        entities = asyncio.run(service.extract("x" * 100, [_NameType()]))
    assert entities == []  # gather(return_exceptions=True) 语义不变：异常被吞、不外抛
    failed = [e for e in _events(log_env) if e["event"] == "chunk_failed"]
    assert len(failed) == 1
    event = failed[0]
    assert event["stage"] == "has_vision"
    assert event["chunk_index"] == 1  # gather 保序 ⇒ c1 是第 1 号
    assert event["error_type"] == "RuntimeError"
    assert event["message_sanitized"] == "vision boom"
    assert any("hybrid_ner_service.py" in f for f in event["traceback_frames"])
    assert event["page"] is None


def test_hybrid_success_emits_no_chunk_failed(log_env, monkeypatch):
    """成功路径零 chunk_failed（负样本：埋点只挂在失败分支）。"""
    monkeypatch.setattr(settings, "HAS_NER_MAX_PARALLEL_REQUESTS", 3)
    service = _ChunkFailService()
    service.failed_chunk = None
    _patch_hybrid_common(monkeypatch, service)
    with pel.event_context("jobc", "itemc"):
        asyncio.run(service.extract("x" * 100, [_NameType()]))
    assert [e for e in _events(log_env) if e["event"] == "chunk_failed"] == []
