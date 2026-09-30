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


_FAKE_RULES = [("发票", {"金额": ["1,000.00"]})]


def test_fully_consumed_block_dropped():
    """纯值块整消费 → 不进主 payload（fake 收不到它）。

    ①.5 门实证：标签+值块（"收款人 徐汉勇"）残墨 ≥ 半锚线必保留——旗舰正例
    必须用纯值块，否则永假。
    """
    known = [{"type": "PERSON", "text": "徐汉勇"}]
    client = _RulesHaS(_FAKE_RULES)
    entities, stage = _run(_blocks("发票号 20260712", "徐汉勇"), client, known)
    joined = "\n".join(client.seen_payloads)
    assert "徐汉勇" not in joined
    assert stage["has_text_known_filter_blocks_dropped"] == 1
    assert stage["has_text_known_filter_chars_saved"] > 0


def test_dropped_block_occurrence_still_matched():
    """遮盖完整性（阻断级）：被剔块的已知值出现处经注入仍进实体表。"""
    known = [{"type": "PERSON", "text": "徐汉勇"}]
    client = _RulesHaS(_FAKE_RULES)  # fake 主调用只应答"发票"块
    entities, stage = _run(_blocks("发票号 20260712", "徐汉勇"), client, known)
    assert any(e["type"] == "PERSON" and e["text"] == "徐汉勇" for e in entities)
    assert stage["has_text_known_injected"] >= 1


def test_partial_consumed_block_kept():
    """部分消费必须整块保留——"转账给徐汉勇5000元"含未知值 5000 元。"""
    known = [{"type": "PERSON", "text": "徐汉勇"}]
    client = _RulesHaS(_FAKE_RULES)
    entities, stage = _run(_blocks("转账给徐汉勇5000元"), client, known)
    assert any("5000" in p for p in client.seen_payloads)
    assert stage["has_text_known_filter_blocks_dropped"] == 0


def test_label_keeps_block_alive():
    """字段标签不是值、不进 known 集合——"开户行：徐汉勇"保留。"""
    known = [{"type": "PERSON", "text": "徐汉勇"}]
    client = _RulesHaS(_FAKE_RULES)
    _entities, stage = _run(_blocks("开户行：徐汉勇"), client, known)
    assert stage["has_text_known_filter_blocks_dropped"] == 0


def test_substring_does_not_consume():
    """isolated-token：已知 张三 不吞 张三公司。"""
    known = [{"type": "PERSON", "text": "张三"}]
    client = _RulesHaS(_FAKE_RULES)
    _entities, stage = _run(_blocks("张三公司 统一信用代码"), client, known)
    assert stage["has_text_known_filter_blocks_dropped"] == 0


def test_short_value_does_not_consume():
    """min-len 门控：短值（"男"，len=1）不进 consumed 集合——否则纯值块"男"
    会因 block ⊂ value 被整块剔掉（residual 段防"男"吞块的同款门控）。"""
    known = [{"type": "UNKNOWN_TYPE", "text": "男"}]
    client = _RulesHaS(_FAKE_RULES)
    _entities, stage = _run(_blocks("男"), client, known)
    assert stage["has_text_known_filter_blocks_dropped"] == 0


def test_flag_off_is_byte_equivalent():
    """开关关：payload 序列、实体表、stage 计数三重一致（①.5 二轮扩展）。

    三变体互比证"关=不消费"；"关=改动前现状"由 e2e 判定①的基线分支输出
    文档逐框 diff 锚定。
    """
    known = [{"type": "PERSON", "text": "徐汉勇"}]
    blocks = _blocks("发票号 20260712", "收款人 徐汉勇")
    client_off = _RulesHaS(_FAKE_RULES); stage_off: dict = {}
    entities_off = asyncio.run(run_has_text_analysis(blocks, client_off, vision_types=None, stage_status=stage_off))
    client_none = _RulesHaS(_FAKE_RULES); stage_none: dict = {}
    entities_none = asyncio.run(
        run_has_text_analysis(blocks, client_none, vision_types=None, stage_status=stage_none, known_values=known)
    )
    client_empty = _RulesHaS(_FAKE_RULES); stage_empty: dict = {}
    entities_empty = asyncio.run(
        run_has_text_analysis(blocks, client_empty, vision_types=None, stage_status=stage_empty, known_values=[])
    )
    assert client_off.seen_payloads == client_none.seen_payloads == client_empty.seen_payloads
    assert entities_off == entities_none == entities_empty
    # 开关关：stage 观测面也与现状等价——known_filter 三键不得出现（终评审 Important#1）
    for stage in (stage_off, stage_none, stage_empty):
        for key in ("has_text_known_filter_blocks_dropped", "has_text_known_filter_chars_saved",
                    "has_text_known_filter_candidate_values", "has_text_known_injected"):
            assert key not in stage


def test_absent_value_cannot_consume_block():
    """页级预筛的真实作用（①.5 二轮重写——原用例恒真）：防他页长值幻影吞块。

    _block_residual_ink 的 block ⊂ value 分支不需要值在本页出现——若预筛缺失，
    他页的"徐汉勇有限公司"经该分支整吞本页纯值块"徐汉勇"，而注入侧该长值不在
    本页出现 → 无注入 → 块内容明文残留。有预筛：值不进核算，块保留。
    """
    known = [{"type": "ORG", "text": "徐汉勇有限公司"}]  # 不在本页出现
    client = _RulesHaS(_FAKE_RULES)
    _entities, stage = _run(_blocks("徐汉勇"), client, known)
    assert stage["has_text_known_filter_blocks_dropped"] == 0


def test_all_blocks_dropped_skips_ner_and_still_injects():
    """空载荷页（①.5 二轮补）：全块被剔 → 不发 NER 调用，注入照常回填。"""
    known = [{"type": "PERSON", "text": "徐汉勇"}]
    client = _RulesHaS(_FAKE_RULES)
    entities, stage = _run(_blocks("徐汉勇", "徐汉勇"), client, known)
    assert client.seen_payloads == []  # 零 NER 调用
    assert stage["has_text_known_filter_blocks_dropped"] == 2
    assert any(e["type"] == "PERSON" and e["text"] == "徐汉勇" for e in entities)
    assert stage["has_text_known_injected"] >= 1


def test_short_residual_punctuation_block_dropped():
    """短残墨块（①.5 二轮补，第二条收益路径）："徐汉勇，"残墨仅标点。

    _compact_text 只去空白保留标点（已核）——残墨 run=1 < 半锚线 1.5 → 判已
    解释 → 整剔；注入逐块 isolated-token（标点即边界）回填，遮盖不丢。
    """
    known = [{"type": "PERSON", "text": "徐汉勇"}]
    client = _RulesHaS(_FAKE_RULES)
    entities, stage = _run(_blocks("徐汉勇，"), client, known)
    assert stage["has_text_known_filter_blocks_dropped"] == 1
    assert any(e["type"] == "PERSON" and e["text"] == "徐汉勇" for e in entities)


def test_injection_dedups_against_ner_result():
    """注入去重（①.5 二轮补）：NER 已召回的值不再注入——实体表无重复、无双框。"""
    known = [{"type": "PERSON", "text": "徐汉勇"}]
    client = _RulesHaS([("徐汉勇", {"PERSON": ["徐汉勇"]})])  # 保留块里 NER 自己召回
    entities, stage = _run(_blocks("转账给徐汉勇5000元"), client, known)
    assert sum(1 for e in entities if e["text"] == "徐汉勇") == 1
