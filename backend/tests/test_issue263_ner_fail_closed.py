"""Issue#263 —— HaS NER 全量不可用 fail-closed：组失败/负健康升格页级失败。

方案：LexMask-workspace/docs/plans/2026-11-13-issue263-ner-fail-closed.md（v3）
验收判据落点：AC1（组失败升格）、AC2（负健康四形态）、AC3（零实体/合法跳过反例钉）、
AC4（kill switch 回落）、reason token 格式。页级记账链在 test_issue263_page_accounting.py。

红性先证（两段红 + 反例钉红，方案 §4 / r2 I-1）：
- 红-①：本文件在 HEAD 093f944 上 ImportError（``HasTextNerUnavailable`` 不存在）；
- 红-②：仅加异常类定义后重跑 ⇒ 升格判据 ``DID NOT RAISE``（HEAD 组失败/负健康均 return []）；
- AC2 b/c/d：HEAD 上**同样为红**——红因=头部提前负健康检查（L667，本修复要移除的面）
  先于两道闸与缓存查找短路，返回 ``[]`` + ``skipped_recent_unavailable`` 与本文件
  的状态级强断言相撞（r2 I-1）。红输出全部留存验收记录。

夹具复用 test_issue104（``_ModelFake`` 只替换 ``_call_model``、真 ner() 照跑）。
"""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest
from test_issue104_group_dispatch import (
    _MARK_HEAD,
    _MARK_TAIL,
    _VT,
    _blocks,
    _cm_reply,
    _ModelFake,
    _two_group_page,
)

from app.core.config import settings
from app.services.has_client import HaSClient
from app.services.vision.has_text_analysis import (
    HasTextNerUnavailable,
    run_has_text_analysis,
)
from app.services.vision.has_text_payload import _build_has_text_payload

_VISION_TYPES = [_VT("ID_CARD", "身份证号"), _VT("PHONE", "电话")]
_TYPES = ["身份证号", "电话"]


@pytest.fixture(autouse=True)
def _clean_shared_caches():
    """同 test_issue104：HaSClient 的 NER 缓存类级共享，跨用例污染会让断言失真。"""
    HaSClient._context_probe_cache.clear()
    HaSClient._SHARED_NER_CACHE.clear()
    yield
    HaSClient._context_probe_cache.clear()
    HaSClient._SHARED_NER_CACHE.clear()


def _negative_health(client: _ModelFake) -> None:
    """把客户端置成「负健康 TTL 内」态（is_available() 探测失败后的真实残留）。"""
    client._health_checked_at = time.monotonic()
    client._health_ready = False


def _full_page_payload(blocks):
    return _build_has_text_payload(
        blocks,
        max_chars=int(settings.HAS_VISION_MAX_TEXT_CHARS),
        max_block_chars=int(settings.HAS_VISION_MAX_BLOCK_CHARS),
    )


# ---------------------------------------------------------------------------
# AC1：组失败升格
# ---------------------------------------------------------------------------

def test_ac1a_group_failure_raises_ner_unavailable(monkeypatch):
    """任一组失败 ⇒ 页抛 HasTextNerUnavailable；指标保留且尾部不覆写状态（F2）。"""
    monkeypatch.setattr(settings, "HAS_NER_SELF_CONSIST_SAMPLES", 1)
    _group_texts, blocks, _head, _tail = _two_group_page()
    client = _ModelFake(context_tokens=4096, reply=_cm_reply)  # 尾组 400
    stage: dict = {}
    with pytest.raises(HasTextNerUnavailable, match=r"^ner_unavailable: 1/\d+ group\(s\) failed"):
        asyncio.run(run_has_text_analysis(blocks, client, _VISION_TYPES, stage))
    assert stage["has_text_ner_failed_groups"] == 1
    # 状态最终值只由 raise 点决定（F2）：尾部 except 分支不得覆写成 failed 等
    assert stage["has_text_cache_status"] == "group_failed"


def test_ac1b_group_failure_raises_on_connect_error_family(monkeypatch):
    """连接类错误（RETRYABLE_HTTPX 耗尽后）同样升格——#263 的原始场景。"""
    monkeypatch.setattr(settings, "HAS_NER_SELF_CONSIST_SAMPLES", 1)
    _group_texts, blocks, _head, _tail = _two_group_page()

    def reply(text, types):
        if _MARK_HEAD in text:
            raise httpx.ConnectError("connection reset by peer")
        return {"身份证号": [_MARK_TAIL]} if _MARK_TAIL in text else {}

    client = _ModelFake(context_tokens=4096, reply=reply)
    stage: dict = {}
    with pytest.raises(HasTextNerUnavailable, match="ner_unavailable"):
        asyncio.run(run_has_text_analysis(blocks, client, _VISION_TYPES, stage))
    assert stage["has_text_ner_failed_groups"] == 1
    assert stage["has_text_cache_status"] == "group_failed"


def test_ac1c_reason_token_is_stable_message_prefix(monkeypatch):
    """机读 token ``ner_unavailable`` 必须是异常消息首段（[:200] 截断后仍存活）。"""
    monkeypatch.setattr(settings, "HAS_NER_SELF_CONSIST_SAMPLES", 1)
    _group_texts, blocks, _head, _tail = _two_group_page()
    client = _ModelFake(context_tokens=4096, reply=_cm_reply)
    with pytest.raises(HasTextNerUnavailable) as ei:
        asyncio.run(run_has_text_analysis(blocks, client, _VISION_TYPES, {}))
    assert str(ei.value).startswith("ner_unavailable"), str(ei.value)


# ---------------------------------------------------------------------------
# AC2：负健康四形态（a 升格；b/c/d 反例钉——HEAD 上同样为红，r2 I-1）
# ---------------------------------------------------------------------------

def test_ac2a_negative_health_with_eligible_text_raises(monkeypatch):
    """负健康 TTL 内 + 有合格文本 + 整页缓存 miss ⇒ 抛（该页没被扫，非「没扫出」）。"""
    monkeypatch.setattr(settings, "HAS_NER_SELF_CONSIST_SAMPLES", 1)
    _group_texts, blocks, _head, _tail = _two_group_page()
    client = _ModelFake(context_tokens=4096, reply=lambda text, types: {})
    _negative_health(client)
    stage: dict = {}
    with pytest.raises(HasTextNerUnavailable, match="ner_unavailable"):
        asyncio.run(run_has_text_analysis(blocks, client, _VISION_TYPES, stage))
    assert stage["has_text_cache_status"] == "skipped_recent_unavailable"
    assert client.calls == [], "负健康升格发生在组规划之前，不得发出任何模型请求"


def test_ac2b_negative_health_no_eligible_text_returns_empty(monkeypatch):
    """反例钉：无合格文本页在负健康下仍合法 return []（根本到不了检查区）。"""
    client = _ModelFake(context_tokens=4096, reply=lambda text, types: {})
    _negative_health(client)
    stage: dict = {}
    page = asyncio.run(run_has_text_analysis(_blocks("   "), client, _VISION_TYPES, stage))
    assert page == []
    assert stage["has_text_cache_status"] == "skipped_no_eligible_text"
    assert client.calls == []


def test_ac2c_negative_health_too_short_returns_empty(monkeypatch):
    """反例钉：文本低于最短阈值的页在负健康下仍合法 return []（零实体≠失败）。"""
    monkeypatch.setattr(settings, "HAS_VISION_MIN_TEXT_CHARS_FOR_NER", 500)
    client = _ModelFake(context_tokens=4096, reply=lambda text, types: {})
    _negative_health(client)
    stage: dict = {}
    page = asyncio.run(
        run_has_text_analysis(_blocks("370702199001014521，张三"), client, _VISION_TYPES, stage)
    )
    assert page == []
    assert stage["has_text_cache_status"] == "skipped_too_short"
    assert client.calls == []


def test_ac2d_negative_health_cache_hit_uses_cache(monkeypatch):
    """反例钉（评审 F1 核心）：整页缓存命中 + 负健康 ⇒ 走缓存产出实体，不抛不记失败。

    此前成功的页结果就在本地缓存里，NER 无需服务即可产出正确实体——reject→整卷
    重跑落在故障窗口时不得误杀这类页（否则本修复自己犯 #263 同类病）。
    """
    monkeypatch.setattr(settings, "HAS_NER_SELF_CONSIST_SAMPLES", 1)
    _group_texts, blocks, _head, _tail = _two_group_page()
    payload = _full_page_payload(blocks)
    client = _ModelFake(context_tokens=4096, reply=lambda text, types: {})
    # 预填整页缓存（写侧 set_cached_ner 与读侧 get_cached_ner 同键公式，Issue#108）
    client.set_cached_ner(payload.content, _TYPES, {"身份证号": [_MARK_HEAD]})
    _negative_health(client)
    stage: dict = {}
    page = asyncio.run(run_has_text_analysis(blocks, client, _VISION_TYPES, stage))
    assert stage["has_text_cache_status"] == "hit_before_slot"
    assert client.exact_calls(payload.content) == 0, "缓存命中不得重发主查"
    assert page, "缓存命中页必须产出实体而非静默零实体"
    assert any(e.get("text") == _MARK_HEAD for e in page), page


# ---------------------------------------------------------------------------
# AC3：防假阳性反例钉（零实体≠失败；NER 健康基线态，与 AC2b/c 互为对照）
# ---------------------------------------------------------------------------

def test_ac3a_empty_ocr_blocks_return_empty():
    """空白页（无 OCR 块）合法零实体，不抛不记失败。"""
    client = _ModelFake(context_tokens=4096, reply=lambda text, types: {})
    stage: dict = {}
    page = asyncio.run(run_has_text_analysis([], client, _VISION_TYPES, stage))
    assert page == []
    assert stage["has_text_cache_status"] == "skipped_empty_ocr"


def test_ac3b_no_eligible_text_healthy_ner_returns_empty():
    """健康 NER 下无合格文本页走既有 skip 闸（与 AC2b 的负健康态互为对照）。"""
    client = _ModelFake(context_tokens=4096, reply=lambda text, types: {})
    stage: dict = {}
    page = asyncio.run(run_has_text_analysis(_blocks("   "), client, _VISION_TYPES, stage))
    assert page == []
    assert stage["has_text_cache_status"] == "skipped_no_eligible_text"


def test_ac3c_too_short_healthy_ner_returns_empty(monkeypatch):
    """健康 NER 下文本过短页走既有 skip 闸（与 AC2c 的负健康态互为对照）。"""
    monkeypatch.setattr(settings, "HAS_VISION_MIN_TEXT_CHARS_FOR_NER", 500)
    client = _ModelFake(context_tokens=4096, reply=lambda text, types: {})
    stage: dict = {}
    page = asyncio.run(
        run_has_text_analysis(_blocks("370702199001014521，张三"), client, _VISION_TYPES, stage)
    )
    assert page == []
    assert stage["has_text_cache_status"] == "skipped_too_short"


def test_ac3d_all_groups_succeed_legitimate_zero_entities(monkeypatch):
    """组全部成功但模型合法答无实体 ⇒ return [] 且**绝不**判失败（判据=组失败，非零实体）。"""
    monkeypatch.setattr(settings, "HAS_NER_SELF_CONSIST_SAMPLES", 1)
    _group_texts, blocks, _head, _tail = _two_group_page()
    client = _ModelFake(context_tokens=4096, reply=lambda text, types: {})
    stage: dict = {}
    page = asyncio.run(run_has_text_analysis(blocks, client, _VISION_TYPES, stage))
    assert page == []
    assert stage.get("has_text_ner_failed_groups", 0) == 0
    assert stage["has_text_cache_status"] not in ("group_failed", "skipped_recent_unavailable")
    assert stage["has_text_cache_status"] == "model_call"
    assert stage["has_text_entity_count"] == 0


# ---------------------------------------------------------------------------
# AC4：kill switch 回落阀（两条升格路径都回落现状）
# ---------------------------------------------------------------------------

def test_ac4a_kill_switch_group_failure_falls_back(monkeypatch):
    monkeypatch.setattr(settings, "HAS_NER_UNAVAILABLE_PAGE_FAIL", False)
    monkeypatch.setattr(settings, "HAS_NER_SELF_CONSIST_SAMPLES", 1)
    _group_texts, blocks, _head, _tail = _two_group_page()
    client = _ModelFake(context_tokens=4096, reply=_cm_reply)
    stage: dict = {}
    page = asyncio.run(run_has_text_analysis(blocks, client, _VISION_TYPES, stage))
    assert page == []
    assert stage["has_text_cache_status"] == "group_failed"
    assert stage["has_text_ner_failed_groups"] == 1


def test_ac4b_kill_switch_negative_health_falls_back(monkeypatch):
    monkeypatch.setattr(settings, "HAS_NER_UNAVAILABLE_PAGE_FAIL", False)
    _group_texts, blocks, _head, _tail = _two_group_page()
    client = _ModelFake(context_tokens=4096, reply=lambda text, types: {})
    _negative_health(client)
    stage: dict = {}
    page = asyncio.run(run_has_text_analysis(blocks, client, _VISION_TYPES, stage))
    assert page == []
    assert stage["has_text_cache_status"] == "skipped_recent_unavailable"
    assert client.calls == []


# ---------------------------------------------------------------------------
# 红性先证自检（防止夹具写错导致红性证据空转）
# ---------------------------------------------------------------------------

def test_t0_fixture_preconditions():
    """两段红口径的前提钉：_cm_reply 尾组必失败；负健康替身必生效。"""
    _group_texts, blocks, _head, _tail = _two_group_page()
    assert len(_group_texts) >= 2
    client = _ModelFake(context_tokens=4096, reply=lambda text, types: {})
    _negative_health(client)
    from app.services.vision.ocr_cache import _has_recent_negative_health

    assert _has_recent_negative_health(client), "负健康替身必须让检查函数判真"
