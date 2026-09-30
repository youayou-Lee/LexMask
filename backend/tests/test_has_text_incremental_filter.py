"""WS-1 按文件增量识别（Issue#37）：known_values 传递与整消费块剔除。

fake 契约：autouse 清 HaSClient 类级状态沿用 test_has_text_verify_concurrency
模式；fake 本身为子串谓词路由（先命中先用，记录每次收到的完整 payload），
不依赖内容键控路由——本文件用例不按缓存键区分。
"""

import asyncio
import threading

import pytest

from app.services.ocr_has_vision_service import OCRTextBlock
from app.services.vision.ocr_pipeline import run_has_text_analysis


@pytest.fixture(autouse=True)
def _clean_shared_client_state():
    from app.services.has_client import HaSClient

    for attr in ("_SHARED_NER_CACHE", "_SHARED_NER_INFLIGHT"):
        obj = getattr(HaSClient, attr, None)
        if obj is not None:
            obj.clear()
    yield
    for attr in ("_SHARED_NER_CACHE", "_SHARED_NER_INFLIGHT"):
        obj = getattr(HaSClient, attr, None)
        if obj is not None:
            obj.clear()


class _RulesHaS:
    """子串谓词路由 fake：先命中先用；记录每次收到的完整 payload。"""

    base_url = "http://stub:0"

    def __init__(self, rules):
        self.rules = rules
        self.seen_payloads: list[str] = []
        self._lock = threading.Lock()

    def ner(self, text, entity_types=None, **_kwargs):
        with self._lock:
            self.seen_payloads.append(text)
        for pattern, answer in self.rules:
            if pattern in text:
                return answer
        return {}


def _blocks(*lines: str) -> list[OCRTextBlock]:
    return [
        OCRTextBlock(
            text=t,
            polygon=[[100, 40 * i], [600, 40 * i], [600, 40 * i + 30], [100, 40 * i + 30]],
            confidence=0.98,
        )
        for i, t in enumerate(lines)
    ]


def _run(blocks, client, known_values=None):
    from app.core.config import settings

    stage: dict = {}
    try:
        settings.HAS_VISION_INCREMENTAL_KNOWN_FILTER = True
        entities = asyncio.run(
            run_has_text_analysis(blocks, client, vision_types=None, stage_status=stage, known_values=known_values)
        )
    finally:
        settings.HAS_VISION_INCREMENTAL_KNOWN_FILTER = False  # ①.5 评审：断言失败也不泄漏全局态
    return entities, stage


def test_known_entity_values_from_snapshot():
    """orchestrator 助手：从 snapshot.bounding_boxes 收集 type+text，跳过残缺项。"""
    from app.services.redaction_orchestrator import _known_entity_values

    snapshot = {
        "bounding_boxes": {
            "1": [{"type": "PERSON", "text": "徐汉勇"}, {"type": "", "text": "x"}, {"type": "AGE"}, "junk"],
        },
        "page_count": 3,
    }
    assert _known_entity_values(snapshot) == [{"type": "PERSON", "text": "徐汉勇"}]
    assert _known_entity_values({}) == []


def test_known_values_threads_through_service_chain(monkeypatch):
    """①.5 二轮：known_values 显式穿透 wrapper——转发丢失即 FAIL。"""
    from app.services.ocr_has_vision_service import get_ocr_has_vision_service

    svc = get_ocr_has_vision_service()
    captured: list = []

    async def _recorder(ocr_blocks, vision_types, stage_status=None, known_values=None):
        captured.append(known_values)
        return []

    monkeypatch.setattr(svc, "_run_has_text_analysis", _recorder)
    asyncio.run(
        svc._invoke_has_text_analysis(
            _blocks("徐汉勇"), None, {}, known_values=[{"type": "PERSON", "text": "徐汉勇"}]
        )
    )
    assert captured == [[{"type": "PERSON", "text": "徐汉勇"}]]


def test_no_new_singleton_state():
    """①.5 二轮守卫：不新增共享可变单例态——known_values 走显式参数。

    （守卫用例：实现未引入单例时本用例从创建起即 PASS，属预期。）
    """
    from app.services.ocr_has_vision_service import get_ocr_has_vision_service

    assert not hasattr(get_ocr_has_vision_service(), "known_entity_values")
