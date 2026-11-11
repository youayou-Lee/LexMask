"""Issue#273：阈值组装（settings → 纯函数入参）。judge 不 import settings。"""
from __future__ import annotations

from app.services.page_routing.types import RoutingThresholds


def get_routing_thresholds() -> RoutingThresholds:
    from app.core.config import settings  # 惰性：避免 import 环

    return RoutingThresholds(
        min_chars=int(settings.TEXT_LAYER_MIN_CHARS_PER_PAGE),
        max_garbage=float(settings.TEXT_LAYER_MAX_GARBAGE_RATIO),
        max_word_chars=int(settings.TEXT_LAYER_MAX_WORD_CHARS),
        min_reading_order=float(settings.ROUTING_MIN_READING_ORDER_AGREEMENT),
        max_page_repeat=float(settings.ROUTING_MAX_PAGE_REPEAT_RATIO),
        boundary_chars_band=float(settings.ROUTING_BOUNDARY_CHARS_BAND),
        max_type3_ratio=float(settings.ROUTING_MAX_TYPE3_RATIO),
        min_embedded_ratio=float(settings.ROUTING_MIN_EMBEDDED_RATIO),
        min_ink=float(settings.TEXT_LAYER_MIN_INK_ALIGNMENT),
        chars_multiple=float(settings.ROUTING_CHARS_MULTIPLE),
        entities_multiple=float(settings.ROUTING_ENTITIES_MULTIPLE),
    )
