"""Issue#273 Task 12：A3 性能判据 + A7 决策重放判据。

* **A3**：86 页混排真卷在文件级 memo 预热后，单页「L1 采集 + judge」中位耗时
  < 20ms——页级路由的决策开销必须远低于页墙（300s），否则路由自身成为瓶颈。
  预热方式 = 首调 ``collect_l1(path, 1)``（memo 整卷，冷启动计在第 1 页之外；
  ``signals._warm`` 这类专用入口**不实现**，memo 已天然覆盖）。CI 无私有
  testdata ⇒ skip（skipif 守卫）；本地与门④必跑。
* **A7**：决策可重放——``PageDecision.as_dict()`` 携带完整 signals +
  ``routing_version``，同 config（同阈值注入）下按账本记录回放 ``judge_page``
  必须得到同 ``(action, reason)``。这是「决策即落账」自洽性的判据：账本里的
  记录足以独立复现决策（行为版本钉在 routing_version，阈值随 settings 同源）。

卷样路径：容器根 ``testdata/真实案卷/扫描型/扫描型PDF误判为文本型-2.pdf``
（86 页混排卷；本文件按仓内惯例从 ``Path(__file__).parents`` 上爬定位，等价于
上述绝对路径），真实案卷本体只存私有 testdata 仓、不进主仓。
"""

from __future__ import annotations

import statistics
import time
from pathlib import Path

import pytest

from app.core.config import settings
from app.core.routing_thresholds import get_routing_thresholds
from app.services.page_routing import signals
from app.services.page_routing.judge import judge_page
from app.services.page_routing.types import PageSignals

# backend/tests → backend → worktree 根 → .worktrees → LexMask → 容器根
_CONTAINER_ROOT = Path(__file__).resolve().parents[5]
_MIXED_VOLUME = _CONTAINER_ROOT / "testdata" / "真实案卷" / "扫描型" / "扫描型PDF误判为文本型-2.pdf"
_MIXED_VOLUME_PAGES = 86


def _has_mixed_volume() -> bool:
    return _MIXED_VOLUME.is_file()


@pytest.mark.skipif(not _has_mixed_volume(), reason="私有 testdata 不在 CI")
def test_l1_judge_under_20ms_after_memo():
    """A3：86 页混排卷 memo 后单页「采集+judge」中位 < 20ms。"""
    path = str(_MIXED_VOLUME)
    signals.collect_l1(path, 1)  # 显式预热 memo（第 1 页计冷启动不计入）
    samples: list[float] = []
    for p in range(1, _MIXED_VOLUME_PAGES + 1):
        t0 = time.perf_counter()
        s = signals.collect_l1(path, p)
        d = judge_page(s, get_routing_thresholds(), routing_version=1)
        samples.append((time.perf_counter() - t0) * 1000)
        assert d.reason  # text_metrics 可解释
    assert statistics.median(samples) < 20.0, f"中位 {statistics.median(samples):.3f}ms 超 20ms 门限"


def test_decision_replay_is_deterministic():
    """A7：决策 JSON 含 signals+routing_version，重放同决策。"""
    s = PageSignals(page=3, chars=800, garbage_ratio=0.0, short_word_ratio=0.1,
                    avg_word_len=2.0, max_word_len=20, word_count=300, font_kinds=3,
                    embedded_ratio=1.0, type3_ratio=0.0, reading_order_agreement=0.95,
                    page_repeat_ratio=0.0)
    t = get_routing_thresholds()
    d1 = judge_page(s, t, routing_version=int(settings.ROUTING_VERSION))
    blob = d1.as_dict()
    assert blob["signals"] and blob["routing_version"] == int(settings.ROUTING_VERSION)
    replayed = judge_page(PageSignals(**blob["signals"] | {"page": 3}), t,
                          routing_version=blob["routing_version"])
    assert (replayed.action, replayed.reason) == (d1.action, d1.reason)


@pytest.mark.skipif(not _has_mixed_volume(), reason="私有 testdata 不在 CI")
def test_decision_replay_from_real_volume_signals():
    """A7 真卷加强：混排卷全部页的决策按账本 signals 原样回放同 (action, reason)。

    合成信号只钉「往返序列化不变形」；真卷 signals 含 None/非整 ratio 等
    实测形态（如 embedded_ratio=None、garbage=0.0031 舍入），是重放判据的
    承重样本——judge 的舍入只发生在决策 JSON 侧，重放以 JSON 为准必须同判。
    """
    path = str(_MIXED_VOLUME)
    t = get_routing_thresholds()
    for p in range(1, _MIXED_VOLUME_PAGES + 1):
        s = signals.collect_l1(path, p)
        d = judge_page(s, t, routing_version=1)
        blob = d.as_dict()
        replayed = judge_page(PageSignals(**blob["signals"]), t, routing_version=blob["routing_version"])
        assert (replayed.action, replayed.reason) == (d.action, d.reason), p
