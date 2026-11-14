"""整卷链压力阀（Issue#275 A）——本期**只留缝不实现**（设计稿 §2.5）。

已裁定（2026-11-12 用户拍板 + #275 任务书）：加速器不再是默认路径，A 后置不进
#275 本支；本模块只提供唯一旁路点 ``accelerator_should_fill`` 的 stub——恒
``False``，``off|auto|on`` 三态行为逐字节相同（B 行为不变）。``auto/on`` 时记
一条 warning：显式留痕防「配了没生效」静默（与「配了出事」两个方向都要防）。

未来 A 落地时的缝契约（写进消费点注释，``redaction_orchestrator.detect_vision``
page 分支）：

1. A 结果让位页级——页级已出结果以页级为准；
2. 块级降级——块失败只影响该块覆盖页，不整卷弃权；
3. 取消/失败落负缓存（承接 #204）；
4. 签名必须加 accelerator 键（同 ``neighbor_context_chars`` 键先例，杜绝
   「翻了 env 不 bump 版本 ⇒ 回放旧框」）。
"""

from __future__ import annotations

import logging

from app.core.config import settings

logger = logging.getLogger(__name__)


def accelerator_should_fill(file_id: str) -> bool:
    """A 接入缝的开关读数。本期恒 False——stub，无消费者语义。

    返回 True 的唯一含义：允许 page 分支走 ``text_layer_doc_boxes_for_page``
    用整卷链填页（未来 A 的执行面）。``file_id`` 参数为 A 落地时的按文件判定
    入口预留（队列压力/卷页数阈值），本期未消费。
    """
    mode = str(getattr(settings, "RECOGNITION_ACCELERATOR_ENABLED", "off") or "off")
    if mode == "off":
        return False
    logger.warning(
        "accelerator reserved, not implemented (Issue#275 A): "
        "RECOGNITION_ACCELERATOR_ENABLED=%s has no consumer; treating as off (file_id=%s)",
        mode,
        str(file_id or "")[:8],
    )
    return False
