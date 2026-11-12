"""Issue#273 卷内路由状态（spec §2.4）：升档计数 / 抽样页 / 整卷升级。进程内存态。

- 决策在页分发后做出；升档只影响「尚未决策」的页（已分发页不改判）；
- 重启清零回落全卷 verify（安全方向）；不持久化；
- 抽样页不受升档豁免（持续哨兵）。
"""
from __future__ import annotations

import hashlib
from collections import OrderedDict
from dataclasses import dataclass, field


@dataclass
class VolumeRoutingState:
    consecutive_passes: int = 0
    upgraded: bool = False
    escalated_to_both: bool = False
    sample_pages: frozenset[int] = field(default_factory=frozenset)


def record_pass(s: VolumeRoutingState, upgrade_after: int) -> None:
    if s.upgraded:
        return
    s.consecutive_passes += 1
    if upgrade_after > 0 and s.consecutive_passes >= upgrade_after:
        s.upgraded = True


def record_switch(s: VolumeRoutingState) -> None:
    s.consecutive_passes = 0  # upgraded 粘性：升档后改判不回退（整卷升级机制兜底）


def record_volume_escalation(s: VolumeRoutingState) -> None:
    s.escalated_to_both = True


def is_sample_page(s: VolumeRoutingState, page: int) -> bool:
    return page in s.sample_pages


def pick_sample_pages(total_pages: int, count: int, content_sha: str) -> frozenset[int]:
    """确定性抽样：含首页，余量按内容哈希从 [2..total] 选取（跨进程可复现）。"""
    if count <= 0 or total_pages <= 0:
        return frozenset()
    picks = {1}
    digest = hashlib.sha256(f"{content_sha}:sample".encode()).digest()
    cursor = 0
    pool = list(range(2, total_pages + 1))
    while len(picks) < min(count, total_pages) and pool:
        cursor = (cursor + digest[cursor % len(digest)]) % len(pool)
        picks.add(pool.pop(cursor))
    return frozenset(picks)


_REGISTRY: OrderedDict[str, VolumeRoutingState] = OrderedDict()
_REGISTRY_MAX = 64


def get_state(key: str, *, total_pages: int, content_sha: str) -> VolumeRoutingState:
    hit = _REGISTRY.get(key)
    if hit is not None:
        _REGISTRY.move_to_end(key)
        return hit
    s = VolumeRoutingState(
        sample_pages=pick_sample_pages(total_pages, _sample_count(), content_sha)
    )
    _REGISTRY[key] = s
    while len(_REGISTRY) > _REGISTRY_MAX:
        _REGISTRY.popitem(last=False)
    return s


def _sample_count() -> int:
    from app.core.config import settings
    return int(settings.ROUTING_SAMPLE_PAGES)


def reset_registry() -> None:
    _REGISTRY.clear()
