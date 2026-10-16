"""Issue#64 + Issue#65 落地：vision 兜底与文本链同粒度 + 超时预算放缩与可观测。

两条链共用 `safe_regex.match_items` 作为「一次 match → 待遮盖文本」的**唯一
事实源**；finditer 预算按文本长度放缩（Issue#65），超时既不抛出也不静默
（warning 带类型/长度/预算 + `HybridNERService._regex_timeout_events` 计数）。

本文件用**替身池**（in-process 复用 `_finditer_many_in_process` 的序列化语义）
与假 `safe_compile`/`safe_finditer`，故不依赖真实进程池、零计时、任何环境可跑。
真实池的端到端对照见 `test_issue43_case_number_floor.py` 的两条
`@requires_process_pool` 用例。
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from app.core.safe_regex import (
    RegexTimeoutError,
    _MatchProxy,
    _finditer_many_in_process,
    scaled_timeout,
)
from app.services import entity_type_service as ets
from app.services import hybrid_ner_service as hns
from app.services import ocr_has_vision_service as vision
from app.services.vision import ocr_entity_match, ocr_pipeline
from process_pool_guard import requires_process_pool
from test_issue43_case_number_floor import _block_with_chars

_PRESET = json.loads(
    (Path(__file__).resolve().parents[1] / "config" / "preset_entity_types.json").read_text("utf-8")
)


class _Type:
    def __init__(self, id: str, regex_pattern: str | None = None):
        self.id = id
        self.regex_pattern = regex_pattern
        self.enabled = True
        self.use_llm = True
        self.name = id


def _type(type_id: str) -> _Type:
    return _Type(type_id, (_PRESET.get(type_id) or {}).get("regex_pattern"))


def _inproc_safe_compile(pattern, timeout=2.0):
    """替身 `safe_compile`：只做语法编译，不跑子进程探针。"""
    return re.compile(pattern, re.IGNORECASE)


def _inproc_finditer_many(compiled, texts, timeout=5.0):
    """替身 `safe_finditer_many`：复用真实序列化逻辑，只是不跨进程。"""
    rows = _finditer_many_in_process(compiled.pattern, compiled.flags, list(texts))
    return [[_MatchProxy(t, s, e, named) for t, s, e, named in r] for r in rows]


# ── Issue#65：文本链预算放缩与超时可观测 ─────────────────────

def test_text_chain_budget_scales_with_document_length(monkeypatch):
    """长文本文档拿到的 finditer 预算必须按长度放缩，而不是写死 2.0s。"""
    captured: dict = {}

    def fake_finditer(compiled, text, timeout=5.0):
        captured["timeout"] = timeout
        captured["text_len"] = len(text)
        return []

    monkeypatch.setattr(hns, "safe_compile", _inproc_safe_compile)
    monkeypatch.setattr(hns, "safe_finditer", fake_finditer)

    text = "x" * 3_000_000
    svc = hns.HybridNERService(has_service_instance=None)

    assert svc._custom_regex_extract(text, [_type("DATE")]) == []
    assert captured["text_len"] == len(text)
    assert captured["timeout"] == scaled_timeout(text) == 6.0
    assert captured["timeout"] > 2.0, "长文本必须突破旧实现写死的 2.0s"


def test_text_chain_timeout_is_observable_and_never_raises(monkeypatch, caplog):
    """超时 ⇒ 不抛出，且「整篇该类型保证层被丢弃」可从警告与计数看到。"""
    def exploding_finditer(compiled, text, timeout=5.0):
        raise RegexTimeoutError("Regex finditer timed out after 2.0s")

    monkeypatch.setattr(hns, "safe_compile", _inproc_safe_compile)
    monkeypatch.setattr(hns, "safe_finditer", exploding_finditer)

    svc = hns.HybridNERService(has_service_instance=None)
    with caplog.at_level(logging.WARNING, logger="app.services.hybrid_ner_service"):
        entities = svc._custom_regex_extract("1980年1月1日", [_type("DATE"), _type("BIRTH_DATE")])

    assert entities == []
    assert svc._regex_timeout_events == 2, "两个类型各记一次超时"
    timeouts = [r.getMessage() for r in caplog.records if "TIMEOUT" in r.getMessage()]
    assert len(timeouts) == 2
    assert "DATE" in timeouts[0]
    assert "text_len=9" in timeouts[0]
    assert "budget=" in timeouts[0]
    assert "timeout_events=1" in timeouts[0]
    assert "timeout_events=2" in timeouts[1]


def test_text_chain_bad_pattern_keeps_legacy_warning(monkeypatch, caplog):
    """畸形 pattern 走旧文案，且不计入超时事件（它不是超时）。"""
    def bad_compile(pattern, timeout=2.0):
        raise re.error("bad pattern")

    monkeypatch.setattr(hns, "safe_compile", bad_compile)

    svc = hns.HybridNERService(has_service_instance=None)
    with caplog.at_level(logging.WARNING, logger="app.services.hybrid_ner_service"):
        assert svc._custom_regex_extract("2020年1月1日", [_type("DATE")]) == []

    assert svc._regex_timeout_events == 0
    assert any(
        "Custom regex skipped for DATE" in r.getMessage() for r in caplog.records
    )


def test_text_chain_probe_timeout_is_distinct_from_search_timeout(monkeypatch, caplog):
    """编译探针超时与检索超时必须区分（冷审 I2）。

    旧实现把 `safe_compile(timeout=1.0)` 与 `safe_finditer(timeout=budget)` 放同一
    个 try ⇒ 1.0s 探针超时被算成「文档级超时」，warning 打出的 `budget=…` 却是
    文档预算（误导排查）。
    """
    def probe_timeout_compile(pattern, timeout=2.0):
        raise RegexTimeoutError("Regex compile probe timed out after 1.0s")

    monkeypatch.setattr(hns, "safe_compile", probe_timeout_compile)

    svc = hns.HybridNERService(has_service_instance=None)
    with caplog.at_level(logging.WARNING, logger="app.services.hybrid_ner_service"):
        assert svc._custom_regex_extract("2020年1月1日", [_type("DATE")]) == []

    assert svc._regex_timeout_events == 1
    messages = [r.getMessage() for r in caplog.records]
    assert any("PROBE TIMEOUT" in m and "probe_budget=1.0s" in m for m in messages)
    assert any("timeout_events=1" in m for m in messages)
    assert not any("SEARCH TIMEOUT" in m for m in messages)
    assert not any(
        re.search(r"(?<!probe_)budget=", m) for m in messages
    ), "探针超时不得打出文档检索预算（`budget=…`）"


def test_text_chain_group_semantics_and_ids_survive_refactor(monkeypatch):
    """文本链语义与实体编号不变（Issue#52 已在 CI 钉住的契约）。"""
    monkeypatch.setattr(hns, "safe_compile", _inproc_safe_compile)
    monkeypatch.setattr(
        hns, "safe_finditer",
        lambda compiled, text, timeout=5.0: _inproc_finditer_many(compiled, [text])[0],
    )
    svc = hns.HybridNERService(has_service_instance=None)

    line = "本案于2022年1月1日至1月31日期间审理"
    interval = svc._custom_regex_extract(line, [_type("DATE")])
    assert [e.text for e in interval] == ["2022年1月1日", "1月31日"]
    assert [e.id for e in interval] == ["regex_DATE_0_0", "regex_DATE_0_1"]
    assert line[interval[0].start:interval[0].end] == "2022年1月1日"

    birth = svc._custom_regex_extract("出生日期：1992年10月5日。", [_type("BIRTH_DATE")])
    assert [e.text for e in birth] == ["1992年10月5日"]
    assert [e.id for e in birth] == ["regex_BIRTH_DATE_0_0"]

    plate = svc._custom_regex_extract("悬挂粤RH0472号牌", [_type("LICENSE_PLATE")])
    assert [e.text for e in plate] == ["粤RH0472号牌"], "无命名组 ⇒ 整 match（预设 pattern 含「号牌」）"
    assert [e.id for e in plate] == ["regex_LICENSE_PLATE_0"], "整 match 沿用无后缀编号"

    # 冷审 I1：空声明组**占位** —— 首个非空组的 sub_index 必须是 1（旧实现基准）。
    # 租户自定义 pattern（前端 preset-column 判 custom_ + regex_pattern）可达此形态，
    # 首版实现「先滤空再编号」会把已承诺的实体 id 漂移成 `_0_0`。
    custom = _Type("custom_x", r"(?P<opt>X*)(?P<date>\d{4})")
    dated = svc._custom_regex_extract("1992年", [custom])
    assert [(e.id, e.text) for e in dated] == [("regex_custom_x_0_1", "1992")], (
        "sub_index 以声明顺序为基准，空组照样占位（Issue#52 对外契约）"
    )


@requires_process_pool
def test_real_pool_search_timeout_is_survivable(monkeypatch, caplog):
    """B10：**真实进程池**下的超时也必须是「不抛出 + 可观测」。

    冷审指出此前所有超时用例都靠 monkeypatch 注入 `RegexTimeoutError`，没人证明
    真实池会如期抛错、且被服务层接住。这里只把预算压到 0.05s，其余全走真实
    `safe_compile` / `safe_finditer` / 子进程；`(a+)+$` 在 40 个 `a` 上是经典
    ReDoS 形态，0.05s 内不可能完成。
    """
    monkeypatch.setattr(hns, "scaled_timeout", lambda text, base=2.0: 0.05)

    svc = hns.HybridNERService(has_service_instance=None)
    with caplog.at_level(logging.WARNING, logger="app.services.hybrid_ner_service"):
        entities = svc._custom_regex_extract("a" * 40 + "b", [_Type("custom_slow", r"(a+)+$")])

    assert entities == []
    assert svc._regex_timeout_events >= 1, "真实池超时必须计入事件"
    assert any("TIMEOUT" in r.getMessage() for r in caplog.records)


# ── Issue#64：vision 兜底与文本链同粒度 ─────────────────────

def _vision_fallback_entities(monkeypatch, line: str, type_id: str) -> list[dict]:
    """跑一次 vision 兜底，返回喂给 `match_entities_to_ocr` 的伪实体。

    冷审 N4：`_apply_regex_fallback` 内的 `safe_*` 目前是**函数体内 import**，
    若将来提到模块级，这里的 patch 会失效、用例静默变假绿。故本 helper 断言
    两个替身**确实各被调用一次**。
    """
    captured: list[dict] = []
    calls = {"compile": 0, "finditer": 0}

    def counting_compile(pattern, timeout=2.0):
        calls["compile"] += 1
        return _inproc_safe_compile(pattern, timeout)

    def counting_many(compiled, texts, timeout=5.0):
        calls["finditer"] += 1
        return _inproc_finditer_many(compiled, texts, timeout)

    monkeypatch.setattr(vision, "safe_compile", counting_compile)
    monkeypatch.setattr(vision, "safe_finditer_many", counting_many)
    monkeypatch.setattr(ets, "get_regex_types", lambda owner_id=None: [_type(type_id)])
    monkeypatch.setattr(
        ocr_pipeline, "match_entities_to_ocr",
        lambda blocks, entities: captured.extend(entities) or [],
    )
    monkeypatch.setattr(ocr_entity_match, "split_regions_across_lines", lambda regions, blocks: [])

    svc = vision.OcrHasVisionService()
    svc._apply_regex_fallback([_block_with_chars(line)], 10 * len(line), 20)
    assert calls == {"compile": 1, "finditer": 1}, (
        "替身未被调用 ⇒ patch 失效、用例会假绿（冷审 N4）"
    )
    return captured


def test_vision_fallback_keeps_birth_context_words_out(monkeypatch):
    """出生语境：进伪实体的只有日期部分（旧实现取 group(0) 会带「出生日期：」）。"""
    entities = _vision_fallback_entities(monkeypatch, "出生日期：1992年10月5日。", "BIRTH_DATE")

    assert [e["text"] for e in entities] == ["1992年10月5日"]
    assert all(e["type"] == "BIRTH_DATE" for e in entities)


def test_vision_fallback_splits_interval_without_separator(monkeypatch):
    """区间：拆成首尾两个伪实体，分隔符「至」不入任何一项。"""
    entities = _vision_fallback_entities(monkeypatch, "本案于2022年1月1日至1月31日期间审理", "DATE")

    assert [e["text"] for e in entities] == ["2022年1月1日", "1月31日"]
    assert all("至" not in e["text"] for e in entities)


def test_vision_fallback_keeps_whole_match_for_ungrouped_pattern(monkeypatch):
    """无命名组 pattern（车牌）保持整 match 一项——新语义不得伤旧行为。"""
    entities = _vision_fallback_entities(monkeypatch, "悬挂粤RH0472号牌的普通二轮摩托车", "LICENSE_PLATE")

    assert [e["text"] for e in entities] == ["粤RH0472号牌"]


def test_vision_fallback_budget_covers_all_block_texts(monkeypatch):
    """vision 侧预算按本页所有 block 的总文本长度放缩（Issue#65）。"""
    captured: dict = {}

    def fake_many(compiled, texts, timeout=5.0):
        captured["timeout"] = timeout
        captured["n_texts"] = len(texts)
        return [[] for _ in texts]

    monkeypatch.setattr(vision, "safe_compile", _inproc_safe_compile)
    monkeypatch.setattr(vision, "safe_finditer_many", fake_many)
    monkeypatch.setattr(ets, "get_regex_types", lambda owner_id=None: [_type("DATE")])

    class _StubBlock:
        def __init__(self, text: str):
            self.text = text

    short_line = "2022年1月1日" * 100_000   # 0.9M 字符 ⇒ 单独算只有 base 2.0s
    long_line = "2022年1月1日" * 250_000    # 2.25M 字符
    svc = vision.OcrHasVisionService()
    svc._apply_regex_fallback([_StubBlock(short_line), _StubBlock(long_line)], 10, 20)

    expected = scaled_timeout(short_line + long_line)  # 3.15M ⇒ 6.3s，按整页文本合计
    assert captured["n_texts"] == 2
    assert captured["timeout"] == expected == 6.3
    assert captured["timeout"] > scaled_timeout(long_line), "预算必须覆盖本页全部 block 文本，而非逐块各算"


def test_vision_fallback_bad_pattern_warns_and_continues(monkeypatch, caplog):
    """畸形 pattern 只警告不抛出（旧行为「never raise on bad pattern」保留）。"""
    def bad_compile(pattern, timeout=1.0):
        raise re.error("bad pattern")

    monkeypatch.setattr(vision, "safe_compile", bad_compile)
    monkeypatch.setattr(ets, "get_regex_types", lambda owner_id=None: [_type("DATE")])

    svc = vision.OcrHasVisionService()
    with caplog.at_level(logging.WARNING, logger="app.services.ocr_has_vision_service"):
        assert svc._apply_regex_fallback([_block_with_chars("2022年1月1日")], 120, 20) == []

    assert any("bad pattern" in r.getMessage() for r in caplog.records)
    # 语法错误不是超时：不得计入超时事件、不得打 PROBE TIMEOUT（冷审 I2）
    assert svc._regex_timeout_events == 0
    assert not any("PROBE TIMEOUT" in r.getMessage() for r in caplog.records)


def test_vision_fallback_probe_timeout_is_distinct_and_counted(monkeypatch, caplog):
    """编译探针（固定 1.0s）超时不得被误标成 bad pattern，且必须可观测（冷审 I2）。

    旧实现把 `safe_compile` 的 `RegexTimeoutError` 与 `re.error` 归为同一条
    「bad pattern」warning，排查时分不清「pattern 写错」与「探针超时」，且 vision
    侧全程没有计数器。
    """
    def probe_timeout_compile(pattern, timeout=1.0):
        raise RegexTimeoutError("Regex compile probe timed out after 1.0s")

    monkeypatch.setattr(vision, "safe_compile", probe_timeout_compile)
    monkeypatch.setattr(ets, "get_regex_types", lambda owner_id=None: [_type("DATE")])

    svc = vision.OcrHasVisionService()
    with caplog.at_level(logging.WARNING, logger="app.services.ocr_has_vision_service"):
        assert svc._apply_regex_fallback([_block_with_chars("2022年1月1日")], 120, 20) == []

    assert svc._regex_timeout_events == 1
    messages = [r.getMessage() for r in caplog.records]
    assert any("PROBE TIMEOUT" in m and "probe_budget=1.0s" in m for m in messages)
    assert any("timeout_events=1" in m for m in messages)
    assert not any("bad pattern" in m for m in messages), "探针超时不是语法错误"


def test_vision_fallback_search_timeout_warns_and_continues(monkeypatch, caplog):
    """vision 侧超时同样只警告不抛出（ReDoS 预算失效时不得打断整页）。"""
    def exploding_many(compiled, texts, timeout=5.0):
        raise RegexTimeoutError("Regex finditer timed out")

    monkeypatch.setattr(vision, "safe_compile", _inproc_safe_compile)
    monkeypatch.setattr(vision, "safe_finditer_many", exploding_many)
    monkeypatch.setattr(ets, "get_regex_types", lambda owner_id=None: [_type("DATE")])

    svc = vision.OcrHasVisionService()
    with caplog.at_level(logging.WARNING, logger="app.services.ocr_has_vision_service"):
        assert svc._apply_regex_fallback([_block_with_chars("2022年1月1日")], 120, 20) == []

    assert any("timed out" in r.getMessage() for r in caplog.records)
    # 检索超时必须与探针超时区分、且计入同一计数器（冷审 I2）
    assert svc._regex_timeout_events == 1
    message = next(r.getMessage() for r in caplog.records if "SEARCH TIMEOUT" in r.getMessage())
    assert "blocks=1" in message
    assert "chars=9" in message
    assert "timeout_events=1" in message


def test_vision_fallback_regions_are_tagged_as_regex_fallback(monkeypatch):
    """命中区域必须打上 `source="regex_fallback"`（冷审 N3：该边界此前无用例）。"""
    from types import SimpleNamespace

    monkeypatch.setattr(vision, "safe_compile", _inproc_safe_compile)
    monkeypatch.setattr(vision, "safe_finditer_many", _inproc_finditer_many)
    monkeypatch.setattr(ets, "get_regex_types", lambda owner_id=None: [_type("DATE")])
    monkeypatch.setattr(
        ocr_pipeline, "match_entities_to_ocr",
        lambda blocks, entities: [SimpleNamespace(source="")],
    )
    monkeypatch.setattr(
        ocr_entity_match, "split_regions_across_lines",
        lambda regions, blocks: list(regions),
    )

    svc = vision.OcrHasVisionService()
    regions = svc._apply_regex_fallback([_block_with_chars("2022年1月1日")], 120, 20)

    assert len(regions) == 1
    assert regions[0].source == "regex_fallback"