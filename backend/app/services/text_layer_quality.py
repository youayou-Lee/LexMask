"""内嵌文本层质量判定（Issue#115 C1）。

背景：`vision_service._detect_with_pdf_text_layer` 对「扫描页 + 内嵌文本层」一律
`raise ValueError`，理由是「扫描页的文本层是低质量 OCR 产物」。真实案卷抽检
（481 份 PDF / 256 份可判）显示 **229 份（89%）** 的扫描页带内嵌文本层，且其中相当
一部分字体统一、文本通顺、逐词坐标可用。一刀切拒绝把这些卷全推给逐页图像 OCR。

本模块把「一律拒绝」换成**可证伪的质量判定**：产出 `USABLE / SUSPECT / UNUSABLE`
与可解释的指标。纯函数、不 import fitz —— 墨迹一致性由调用方（docline service）
采样后作为入参传入，便于单测。

判定语义（不对称代价，用户 2026-10-15 拍板）：
    漏用可用层（判成 SUSPECT/UNUSABLE ⇒ 回落慢路径）代价可接受；
    **错用不可用层（判成 USABLE ⇒ 拿坏文本去识别）不可接受**。
因此阈值偏保守：任何一项质量指标越界即降级为 SUSPECT，只有全部达标才是 USABLE。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from app.core.config import settings

logger = logging.getLogger(__name__)

USABLE = "USABLE"
SUSPECT = "SUSPECT"
UNUSABLE = "UNUSABLE"

# 文本层里允许出现的字符：中日韩、拉丁字母、数字、常见标点/空白/货币。
# 其余（大量 U+FFFD、控制字符、私用区）是「层是垃圾」的强信号。
_ALLOWED_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbfA-Za-z0-9\s，。、；：？！“”‘’（）《》〈〉【】—…·\-–_/\\.,;:?!'\"()\[\]{}%$￥€£+*=<>@#&|~`^]")
_GARBAGE_RE = re.compile(r"[\ufffd\u0000-\u0008\u000b-\u001f\u007f\ue000-\uf8ff]")


@dataclass(frozen=True)
class LayerMetrics:
    """文档级文本层指标。`None` 表示该项未采样。"""

    page_count: int = 0
    word_count: int = 0
    char_count: int = 0
    avg_chars_per_page: float = 0.0
    avg_word_len: float = 0.0
    short_word_ratio: float = 0.0
    line_count: int = 0
    avg_line_len: float = 0.0
    garbage_char_ratio: float = 0.0
    ink_alignment: float | None = None

    def as_dict(self) -> dict:
        return {
            "page_count": self.page_count,
            "word_count": self.word_count,
            "char_count": self.char_count,
            "avg_chars_per_page": round(self.avg_chars_per_page, 2),
            "avg_word_len": round(self.avg_word_len, 3),
            "short_word_ratio": round(self.short_word_ratio, 4),
            "line_count": self.line_count,
            "avg_line_len": round(self.avg_line_len, 2),
            "garbage_char_ratio": round(self.garbage_char_ratio, 4),
            "ink_alignment": None if self.ink_alignment is None else round(self.ink_alignment, 4),
        }


@dataclass(frozen=True)
class LayerVerdict:
    severity: str
    reasons: tuple[str, ...] = field(default_factory=tuple)
    metrics: LayerMetrics = field(default_factory=LayerMetrics)

    @property
    def usable(self) -> bool:
        return self.severity == USABLE

    def as_dict(self) -> dict:
        return {
            "severity": self.severity,
            "reasons": list(self.reasons),
            "metrics": self.metrics.as_dict(),
        }


_MIN_WORD_LEN = 2


def compute_metrics(
    page_word_texts: list[list[str]],
    *,
    ink_alignment: float | None = None,
) -> LayerMetrics:
    """从「每页的词列表」算文档级指标。

    `page_word_texts` 是**纯数据**（不含坐标），便于单测与与 fitz 解耦。
    """
    page_count = len(page_word_texts)
    chars: list[str] = []
    word_lengths: list[int] = []
    short_words = 0
    line_count = 0
    line_lengths: list[int] = []
    for words in page_word_texts:
        for word in words:
            text = str(word or "")
            if not text:
                continue
            word_lengths.append(len(text))
            if len(text) < _MIN_WORD_LEN:
                short_words += 1
            chars.extend(text)
        # 文本层模型里「页」的行数不可得；用词数近似行数上界不成立，
        # 因此行级指标只统计**整页拼串后按换行切分**的结果。
        page_text = " ".join(str(w) for w in words if w)
        for line in page_text.splitlines():
            stripped = line.strip()
            if stripped:
                line_count += 1
                line_lengths.append(len(stripped))
    word_count = len(word_lengths)
    char_count = len(chars)
    garbage = sum(1 for c in chars if _GARBAGE_RE.match(c))
    return LayerMetrics(
        page_count=page_count,
        word_count=word_count,
        char_count=char_count,
        avg_chars_per_page=(char_count / page_count) if page_count else 0.0,
        avg_word_len=(sum(word_lengths) / word_count) if word_count else 0.0,
        short_word_ratio=(short_words / word_count) if word_count else 0.0,
        line_count=line_count,
        avg_line_len=(sum(line_lengths) / line_count) if line_count else 0.0,
        garbage_char_ratio=(garbage / char_count) if char_count else 0.0,
        ink_alignment=ink_alignment,
    )


def classify(metrics: LayerMetrics) -> LayerVerdict:
    """按配置阈值给出判定。任何一项不达标 ⇒ 至少降级为 SUSPECT。"""
    reasons: list[str] = []

    if metrics.page_count <= 0 or metrics.word_count <= 0:
        return LayerVerdict(UNUSABLE, ("empty_text_layer",), metrics)

    min_chars = float(settings.TEXT_LAYER_MIN_CHARS_PER_PAGE)
    if metrics.avg_chars_per_page < min_chars:
        return LayerVerdict(
            UNUSABLE,
            (f"sparse_text_layer:{metrics.avg_chars_per_page:.1f}<{min_chars:.0f}",),
            metrics,
        )

    max_garbage = float(settings.TEXT_LAYER_MAX_GARBAGE_RATIO)
    if metrics.garbage_char_ratio > max_garbage:
        reasons.append(
            f"garbage_chars:{metrics.garbage_char_ratio:.3f}>{max_garbage:.3f}"
        )

    max_short = float(settings.TEXT_LAYER_MAX_SHORT_WORD_RATIO)
    if metrics.short_word_ratio > max_short:
        reasons.append(f"short_words:{metrics.short_word_ratio:.3f}>{max_short:.3f}")

    min_word_len = float(settings.TEXT_LAYER_MIN_AVG_WORD_LEN)
    if metrics.avg_word_len < min_word_len:
        reasons.append(f"avg_word_len:{metrics.avg_word_len:.2f}<{min_word_len:.2f}")

    if metrics.ink_alignment is not None:
        min_ink = float(settings.TEXT_LAYER_MIN_INK_ALIGNMENT)
        if metrics.ink_alignment < min_ink:
            reasons.append(f"ink_alignment:{metrics.ink_alignment:.4f}<{min_ink:.4f}")

    if reasons:
        return LayerVerdict(SUSPECT, tuple(reasons), metrics)
    return LayerVerdict(USABLE, (), metrics)