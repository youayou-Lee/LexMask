"""VL-MD 管线(Issue#66/#50 T1)替换层测试:PLACEHOLDER 模式 + BIRTH_DATE/ADDRESS 泛化 + ORG 化名复用。

口径依据 #50 门⓪拍板默认表:
- PERSON/ID_CARD/PHONE/BANK_CARD/LICENSE_PLATE/CASE_NUMBER → [TYPE_N] 占位
- BIRTH_DATE → 泛化留年份(1976年9月1日 → 1976年;无年份回退占位)
- ADDRESS → 泛化(保留省+市,其后行政名词打"某")
- ORG → 复用化名口径(词池/派生/公共机构保留)
- 一致性:同一原文(文本键,不信 NER coref)→ 同一占位符;跨 context 实例由调用方持表
"""

import pytest

from app.models.common import ReplacementMode
from app.models.entity_schemas import Entity
from app.services.redaction.replacement_strategy import (
    RedactionContext,
    _generalize_address,
    _generalize_birth_date,
)

ORG_POOLS = {
    "GOVERNMENT_AGENCY": {"words": ["某法院"], "strategy": "numbered", "custom_map": {}},
    "INSTITUTION_NAME": {"words": ["某公司"], "strategy": "numbered", "custom_map": {}},
}


def _entity(text: str, type_: str = "PERSON", coref: str | None = None) -> Entity:
    return Entity(
        id=f"e-{type_}-{text}", text=text, type=type_, start=0, end=len(text),
        page=1, source="regex", coref_id=coref,
    )


def _ctx() -> RedactionContext:
    return RedactionContext(ReplacementMode.PLACEHOLDER)


# ---------- 占位替换 ----------

def test_placeholder_person_numbering_and_reuse():
    ctx = _ctx()
    assert ctx.get_replacement(_entity("张三")) == "[PERSON_1]"
    assert ctx.get_replacement(_entity("李四")) == "[PERSON_2]"
    assert ctx.get_replacement(_entity("张三", coref="coref-x")) == "[PERSON_1]"


def test_placeholder_text_keyed_not_ner_coref():
    # 不同人名被 NER 误并同一 coref 组:不得共享占位符(POC/真实案卷教训)
    ctx = _ctx()
    a = ctx.get_replacement(_entity("周雨晴", coref="g1"))
    b = ctx.get_replacement(_entity("蒋淑云", coref="g1"))
    assert a == "[PERSON_1]" and b == "[PERSON_2]"


def test_placeholder_numeric_types():
    ctx = _ctx()
    assert ctx.get_replacement(_entity("441811197609016414", "ID_CARD")) == "[ID_CARD_1]"
    assert ctx.get_replacement(_entity("14863794026", "PHONE")) == "[PHONE_1]"
    assert ctx.get_replacement(_entity("6222351161559407", "BANK_CARD")) == "[BANK_CARD_1]"
    assert ctx.get_replacement(_entity("粤R12345", "LICENSE_PLATE")) == "[LICENSE_PLATE_1]"
    assert ctx.get_replacement(_entity("（2024）粤1802刑初131号", "CASE_NUMBER")) == "[CASE_NUMBER_1]"


def test_placeholder_per_type_counters_independent():
    ctx = _ctx()
    ctx.get_replacement(_entity("张三"))
    ctx.get_replacement(_entity("441811197609016414", "ID_CARD"))
    assert ctx.get_replacement(_entity("李四")) == "[PERSON_2]"
    assert ctx.get_replacement(_entity("441811197609016741", "ID_CARD")) == "[ID_CARD_2]"


# ---------- BIRTH_DATE 泛化留年份 ----------

def test_birth_date_keeps_year_only():
    assert _generalize_birth_date("1976年9月1日") == "1976年"
    assert _generalize_birth_date("1985-03-15") == "1985年"
    assert _generalize_birth_date("一九七六年九月初一") is None  # 中文数字年份规则版不认,回退占位


def test_birth_date_fallback_to_placeholder_when_no_year():
    ctx = _ctx()
    r = ctx.get_replacement(_entity("出生日期不详", "BIRTH_DATE"))
    assert r == "[BIRTH_DATE_1]"


def test_birth_date_via_context_consistent():
    ctx = _ctx()
    assert ctx.get_replacement(_entity("1976年9月1日", "BIRTH_DATE")) == "1976年"
    assert ctx.get_replacement(_entity("1976年9月1日", "BIRTH_DATE")) == "1976年"
    assert ctx.get_replacement(_entity("1985年3月15日", "BIRTH_DATE")) == "1985年"


# ---------- ADDRESS 泛化(省+市+某) ----------

def test_address_full_province_city():
    assert _generalize_address("广东省清远市清新区龙颈镇龙北村委会下鹤山村27号") == "广东省清远市某区某镇某村"
    assert _generalize_address("广西壮族自治区南宁市青秀区民族大道88号") == "广西壮族自治区南宁市某区"


def test_address_municipality_no_province_prefix():
    assert _generalize_address("北京市海淀区中关村大街1号") == "北京市某区"


def test_address_city_only_prefix():
    assert _generalize_address("清远市清新区某镇某村") == "清远市某区某镇某村"


def test_address_only_province_city_keeps_as_is():
    assert _generalize_address("广东省清远市") == "广东省清远市"


def test_address_unparseable_returns_none_for_placeholder_fallback():
    assert _generalize_address("龙北村27号") is None
    assert _generalize_address("") is None


def test_address_via_context_fallback_placeholder():
    ctx = _ctx()
    assert ctx.get_replacement(_entity("龙北村27号", "ADDRESS")) == "[ADDRESS_1]"
    assert ctx.get_replacement(_entity("龙北村27号", "ADDRESS")) == "[ADDRESS_1]"


# ---------- ORG 走化名口径 ----------

def test_org_routes_through_pseudonym_pools():
    ctx = RedactionContext(ReplacementMode.PLACEHOLDER, word_pools=ORG_POOLS)
    r = ctx.get_replacement(_entity("清远市清城区人民法院", "ORG"))
    assert r == "某法院"


def test_org_two_entities_distinct_pseudonyms():
    ctx = RedactionContext(ReplacementMode.PLACEHOLDER, word_pools=ORG_POOLS)
    a = ctx.get_replacement(_entity("清远市清城区人民法院", "ORG"))
    b = ctx.get_replacement(_entity("广东省清远市人民检察院", "ORG"))
    assert a != b
    # 同一原文复用同一化名
    assert ctx.get_replacement(_entity("清远市清城区人民法院", "ORG")) == a


def test_custom_type_gets_placeholder():
    ctx = _ctx()
    assert ctx.get_replacement(_entity("某网络平台", "custom_abc123")) == "[custom_abc123_1]"
