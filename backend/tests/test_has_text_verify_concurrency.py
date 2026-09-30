"""residual verify 段并发化（Issue#33 PR-B）。

fake 契约（评审 M2）：① 按 (text, types) 内容键控路由，不按调用序号——
并发化后调用顺序不确定；② 计数器带 threading.Lock；③ 延迟用 time.sleep
在 to_thread 内确定性持槽，峰值断言不依赖真实时序；④ autouse 清 HaSClient
类级缓存/inflight，防跨测试污染；⑤ 每测 asyncio.run 新 loop，闸门随 loop 重建。
"""
import asyncio
import threading
import time

import pytest

from app.services.ocr_has_vision_service import OCRTextBlock
from app.services.vision.has_text_analysis import run_has_text_analysis


@pytest.fixture(autouse=True)
def _clean_shared_client_state():
    from app.services.has_client import HaSClient

    cache = getattr(HaSClient, "_SHARED_NER_CACHE", None)
    inflight = getattr(HaSClient, "_SHARED_NER_INFLIGHT", None)
    if cache is not None:
        cache.clear()
    if inflight is not None:
        inflight.clear()
    yield
    if cache is not None:
        cache.clear()
    if inflight is not None:
        inflight.clear()


class _ContentKeyedHaS:
    """按规则表（子串谓词 → 应答）路由，先命中先用；记录峰值/调用数/错误注入。"""

    base_url = "http://stub:0"

    def __init__(self, rules, hold_sec: float = 0.0, raise_on_text: str | None = None):
        # rules: list[tuple[str, dict]] —— 子串谓词, 应答。verify 单值规则的
        # 谓词必须排在宽匹配（整页 payload）之前。
        self.rules = rules
        self.hold_sec = hold_sec
        self.raise_on_text = raise_on_text
        self.calls = 0
        self.peak = 0
        self.seen_texts: list[str] = []
        self._current = 0
        self._lock = threading.Lock()

    def ner(self, text, entity_types=None, **_kwargs):
        with self._lock:
            self.calls += 1
            self._current += 1
            self.peak = max(self.peak, self._current)
            self.seen_texts.append(text)
        try:
            if self.raise_on_text and self.raise_on_text in text:
                raise RuntimeError("injected verify failure")
            if self.hold_sec:
                time.sleep(self.hold_sec)
            for pattern, answer in self.rules:
                if pattern in text:
                    return answer
            return {}
        finally:
            with self._lock:
                self._current -= 1


def _dense_blocks():
    """复刻 0712 病理（参照 test_residual_reask_pass.py）：同块两金额，主调用只召回一个。

    金额间用 "/" 分隔——compact 后 USD4,700.00/USD125.00 保持 token 可分，
    residual 的 isolated-token 扣除才能算出未解释墨迹（实案即斜杠分栏样式）。
    """
    lines = [
        "发票号 20260712 付款单位 南宁市宏发贸易有限公司 备注 第A栏",
        "USD 4,700.00 / USD 125.00 合计两栏",
        "收款人 张三 联系电话 13800000000 开户行 南宁支行",
    ]
    return [
        OCRTextBlock(
            text=t,
            polygon=[[100, 40 * i], [600, 40 * i], [600, 40 * i + 30], [100, 40 * i + 30]],
            confidence=0.98,
        )
        for i, t in enumerate(lines)
    ]


def _run_analysis(blocks, client, stage_status=None):
    stage = stage_status if stage_status is not None else {}
    entities = asyncio.run(run_has_text_analysis(blocks, client, vision_types=None, stage_status=stage))
    return entities, stage


def _verify_entities(entities, text):
    # AMOUNT 实体出管道前经 _compact_text（去空白），期望值同样归一后比较
    from app.services.vision.has_text_analysis import _compact_text

    return any(e["type"] == "AMOUNT" and e["text"] == _compact_text(text) for e in entities)


# 主调用部分召回 → residual 捞回第二个金额 → verify 确认。
# 谓词顺序即路由：整页原始 payload（带空格）> 残差/verify（compact 无空格形态）。
# 若断言失败，先打印 client.seen_texts 核对 _build_has_text_payload 的实际
# 拼接文本，微调谓词。
_FAKE_RULES = [
    ("USD 4,700.00 / USD 125.00", {"金额": ["USD 4,700.00"]}),  # 原始整页 payload：部分召回
    ("USD125.00", {"金额": ["USD125.00"]}),  # 残差段与 verify 单值（compact 形态）
]


def test_residual_verify_recovers_value_and_records_metrics():
    client = _ContentKeyedHaS(_FAKE_RULES)
    entities, stage = _run_analysis(_dense_blocks(), client)
    assert _verify_entities(entities, "USD 4,700.00")
    assert _verify_entities(entities, "USD 125.00")
    assert stage.get("has_text_verify_calls", 0) >= 1
    assert stage["has_text_verify_peak_concurrency"] >= 1
    assert stage["has_text_verify_errors"] == 0


def test_fully_recalled_page_has_zero_verify_calls():
    """主调用全召回 → residual_new 为空 → verify 零执行、零指标、结果不变（Review Focus 1）。

    主调用应答用 compact 形态（与 OCR 文本一致——真实模型应答必须匹配 OCR
    原文才能被 matcher 配回框），与 residual 侧的 already-existing 判断同形态。
    """
    rules = [("发票号", {"金额": ["USD4,700.00", "USD125.00"]})]
    client = _ContentKeyedHaS(rules)
    entities, stage = _run_analysis(_dense_blocks(), client)
    assert _verify_entities(entities, "USD 125.00")
    assert "has_text_verify_calls" not in stage or stage["has_text_verify_calls"] == 0
