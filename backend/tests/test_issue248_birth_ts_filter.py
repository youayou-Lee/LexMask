"""用户 2026-11-05 场景裁定：带时分秒的时间戳不是出生日期。

执行背调真卷 80 页里 26 项银行申请/反馈时间戳（yyyy-MM-dd HH:mm:ss）被
BIRTH_DATE 误检替换，时间线被毁。规则=出生日期从不含 HH:mm——形态过滤
收在 _cross_validate（普适，所有线受益）。"""
from app.models.entity_schemas import Entity
from app.services.hybrid_ner_service import HybridNERService


def _cross(entities, text):
    return HybridNERService()._cross_validate(entities, text, set())


def test_birth_date_with_time_component_dropped():
    text = "申请日期 2025-12-01 14:26:32 反馈完毕"
    ents = [Entity(id="a", text="2025-12-01 14:26:32", type="BIRTH_DATE",
                   start=5, end=24, source="has")]
    out = _cross(ents, text)
    assert not [e for e in out if e.type == "BIRTH_DATE"]


def test_birth_date_plain_date_kept():
    text = "出生日期 1990年1月1日 户籍地"
    ents = [Entity(id="a", text="1990年1月1日", type="BIRTH_DATE",
                   start=5, end=12, source="has")]
    out = _cross(ents, text)
    assert [e for e in out if e.type == "BIRTH_DATE"]  # 纯日期保留


def test_birth_date_iso_with_time_dropped():
    text = "2026-09-05 11:47:49 提起的查询申请"
    ents = [Entity(id="a", text="2026-09-05 11:47:49", type="BIRTH_DATE",
                   start=0, end=19, source="has")]
    out = _cross(ents, text)
    assert not [e for e in out if e.type == "BIRTH_DATE"]
