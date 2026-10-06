"""Issue#52 Stage 2 集成：区间拆两实体、出生语境实体只含日期、
双语境开关、相对词钉住、BIRTH/DATE 同 span 撞车保留更具体者。

pattern 全部来自 preset_entity_types.json（运行时同源），不走模块常量。
"""

import json
from pathlib import Path

from app.services.hybrid_ner_service import HybridNERService

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


def _service() -> HybridNERService:
    return HybridNERService(has_service_instance=None)


def _type(type_id: str) -> _Type:
    return _Type(type_id, (_PRESET.get(type_id) or {}).get("regex_pattern"))


def test_interval_yields_two_entities_without_separator():
    """区间首尾各出一个实体，分隔符「至」不进任何实体。"""
    svc = _service()
    text = "本案于2022年1月1日至1月31日期间审理"
    entities = svc._custom_regex_extract(text, [_type("DATE")])
    texts = [e.text for e in entities]
    assert texts == ["2022年1月1日", "1月31日"]
    assert all("至" not in t for t in texts)
    for e in entities:
        assert text[e.start:e.end] == e.text
        assert e.source == "regex"


def test_birth_context_entity_excludes_context_words():
    """出生语境命中：实体=日期部分，「出生日期：」语境词不入实体。"""
    svc = _service()
    text = "出生日期：1992年10月5日。"
    entities = svc._custom_regex_extract(text, [_type("BIRTH_DATE")])
    assert len(entities) == 1
    e = entities[0]
    assert e.type == "BIRTH_DATE"
    assert e.text == "1992年10月5日"
    assert "出生" not in e.text
    assert text[e.start:e.end] == e.text


def test_birth_only_leaves_bare_event_dates_alone():
    """律师动线（仅开出生日期）：裸事件日期不得被误框为出生日期。"""
    svc = _service()
    text = "1992年10月5日双方签订协议；本院于2022年1月25日受理"
    entities = svc._custom_regex_extract(text, [_type("BIRTH_DATE")])
    assert entities == []


def test_relative_words_produce_no_entities():
    """相对日期词不产生实体（2026-10-06 拍板：本期不认，钉住防误加）。"""
    svc = _service()
    text = "同年次月当月同日次年"
    entities = svc._custom_regex_extract(text, [_type("DATE")])
    assert entities == []


def test_chinese_numeral_date_framed_when_has_misses():
    """HaS 全漏时，中文数字日期由保证层补出（本 Issue 主目标）。"""
    svc = _service()
    text = "本院于二〇二二年一月二十五日立案受理"
    entities = svc._custom_regex_extract(text, [_type("DATE")])
    assert [e.text for e in entities] == ["二〇二二年一月二十五日"]


def test_same_span_birth_outranks_date_in_cross_validate():
    """出生语境与全形态同 span 撞车 → 交叉验证保留 BIRTH_DATE（更具体）。"""
    svc = _service()
    text = "张三，出生于1992年10月5日，汉族"
    raw = svc._custom_regex_extract(text, [_type("DATE"), _type("BIRTH_DATE")])
    assert {(e.type, e.text) for e in raw} == {
        ("DATE", "1992年10月5日"),
        ("BIRTH_DATE", "1992年10月5日"),
    }

    validated = svc._cross_validate(raw, text, {"DATE", "BIRTH_DATE"})
    hits = [e for e in validated if e.text == "1992年10月5日"]
    assert hits, "日期实体不应丢失"
    assert {e.type for e in hits} == {"BIRTH_DATE"}, "同 span 只应保留出生日期类型"


def test_has_hit_interval_not_duplicated_by_regex_split():
    """组合场景钉子：HaS 已把整段区间框为单实体时，正则拆分不再补框
    （HaS 命中→HaS 实体独占；HaS 漏→正则拆首尾补位，二者不叠加）。"""
    from app.models.schemas import Entity

    svc = _service()
    text = "羁押期间为2022年1月1日至1月31日止"
    start = text.index("2022")
    has_entity = Entity(
        id="e_has_1",
        text="2022年1月1日至1月31日",
        type="DATE",
        start=start,
        end=start + len("2022年1月1日至1月31日"),
        page=1,
        source="has",
    )
    raw = svc._custom_regex_extract(text, [_type("DATE")])
    assert len(raw) == 2, "HaS 漏检时正则拆首尾两个"

    kept = HybridNERService._drop_overlapping(raw, [has_entity])
    assert kept == [], "HaS 已命中整段区间时正则不得重复出框"


def test_large_document_extraction_performance():
    """性能冒烟：1MB 高密度日期文书，多分支交替不得回溯爆炸。"""
    import time

    svc = _service()
    chunk = "本院于2022年1月25日立案，同年次月审理；另于二〇二二年三月十五日宣判，2022版编号不变。"
    text = chunk * 5200  # ~1MB
    t0 = time.perf_counter()
    entities = svc._custom_regex_extract(text, [_type("DATE")])
    elapsed = time.perf_counter() - t0
    assert len(entities) == 5200 * 2, "每段应命中 2 个日期、相对词与「2022版」不框"
    assert elapsed < 20, f"1MB 抽取耗时 {elapsed:.1f}s，疑似回溯爆炸"
