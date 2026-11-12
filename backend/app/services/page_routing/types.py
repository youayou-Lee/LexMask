"""Issue#273 页级路由类型：决策、信号、仲裁与阈值的唯一数据面。纯 dataclass，无 IO。"""
from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class PageSignals:
    page: int
    is_pdf: bool = True
    chars: int = 0
    garbage_ratio: float = 0.0
    short_word_ratio: float = 0.0
    avg_word_len: float = 0.0
    max_word_len: int = 0
    word_count: int = 0
    font_kinds: int = 0
    embedded_ratio: float | None = None
    type3_ratio: float | None = None
    reading_order_agreement: float | None = None
    page_repeat_ratio: float = 0.0
    ink_alignment: float | None = None      # L2；None=未采样
    image_coverage: float | None = None     # L1（最大单图未剪裁面积占比，判定序 5.5 输入面）；None=未采到
    probe_error: bool = False               # 采集器故障 ⇒ fail-closed 退 ocr


@dataclass(frozen=True)
class PageDecision:
    page: int
    action: str            # "text_layer"|"text_layer_verify"|"ocr"|"both"
    needs_verify: bool
    confidence: float      # 1.0 clear / 0.5 boundary / 0.0 fallback
    signals: dict          # asdict(PageSignals)
    reason: str
    routing_version: int
    thresholds_fp: str = ""  # A7 阈值指纹（终审 I-2）；空串=未注入（既有调用兼容）

    def as_dict(self) -> dict:
        # signals 字段本身已是 dict（judge 侧已做舍入），asdict 对普通 dict 原样深拷贝。
        return asdict(self)


@dataclass(frozen=True)
class SideResult:
    chars: int
    entities: int


@dataclass(frozen=True)
class ArbitrationVerdict:
    winner: str             # "text_layer"|"ocr"
    switched_from: str | None
    switch_reason: str | None   # ocr_evidence_higher_chars|ocr_evidence_higher_entities|text_layer_stage_error
    evidence_valid: bool
    metrics: dict


@dataclass(frozen=True)
class RoutingThresholds:
    min_chars: int
    max_garbage: float
    max_word_chars: int
    min_reading_order: float
    max_page_repeat: float
    boundary_chars_band: float
    max_type3_ratio: float
    min_embedded_ratio: float
    min_ink: float
    chars_multiple: float
    entities_multiple: float
    fullpage_image_coverage: float
