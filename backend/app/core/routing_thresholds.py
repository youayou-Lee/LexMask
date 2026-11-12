"""Issue#273：阈值组装（settings → 纯函数入参）。judge 不 import settings。"""
from __future__ import annotations

import hashlib
from dataclasses import asdict

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
        fullpage_image_coverage=float(settings.ROUTING_FULLPAGE_IMAGE_COVERAGE),
    )


def thresholds_fingerprint(t: RoutingThresholds) -> str:
    """阈值指纹（A7，终审 I-2）：asdict 键排序后 sha256 前 16 位。纯函数。

    用途有二：① ``PageDecision.thresholds_fp``（决策即落账时并进 file_store /
    pipeline_status，重放时可校验决策所用阈值口径）；② page 模式缓存签名
    ``routing_thresholds_fp``（redaction_orchestrator._vision_signature）——阈值
    翻 env 而不 bump ROUTING_VERSION 时签名仍变，不回放旧口径的框（语义同
    ner_governance 指纹先例）。file 模式签名不加该键（键集钉不破）。
    """
    canonical = sorted(asdict(t).items())
    payload = repr(canonical)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
