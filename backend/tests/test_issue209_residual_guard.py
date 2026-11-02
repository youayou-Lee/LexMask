"""Issue#209 成本结构修复②：残差 payload 上限（禁止「整页重发」）。

根因（真实第四卷 133 页，2026-10-29 云实测 427s 的构成）：
主查撞 2048 完成帽 ⇒ 截断使几乎整页都"未被解释" ⇒ `_block_residual_ink` 认为
整页都是残差 ⇒ `residual_payload = _build_has_text_payload(residual_blocks,
max_chars=HAS_VISION_MAX_TEXT_CHARS /*=32000*/)` 拿到 **5372 字符整页**（主查
用的是 6000 上限，两套上限），再以 20 类型整页问第二遍 ⇒ 又截断 ⇒ 又 12 段补查。

修法：残差只许问"真正未被解释的墨迹"，且相对/绝对双闸——超过主查 payload 的
`HAS_NER_RESIDUAL_MAX_PAYLOAD_RATIO`（默认 35%）**且**不少于
`HAS_NER_RESIDUAL_MIN_CHARS`（默认 2000）⇒ 判为"截断伪影"，跳过整页重发，
让类型补查（修复③）去收敛。
"""

from __future__ import annotations

import asyncio

from app.core.config import settings
from app.services.ocr_has_vision_service import OCRTextBlock
from app.services.vision.has_text_analysis import run_has_text_analysis


def _block(text: str, top: int) -> OCRTextBlock:
    return OCRTextBlock(
        text=text,
        polygon=[[100, top], [600, top], [600, top + 30], [100, top + 30]],
        confidence=0.98,
    )


_DENSE = "370702199001014521"  # 唯一被主查认领的真值


def _dense_page_blocks(n_blocks: int = 20, chars: int = 200) -> list[OCRTextBlock]:
    """模拟页 133：密排数字表格（真值只出现一次，其余全是异于真值的数字）。"""
    blocks = [_block(f"报关单号 {_DENSE}", 100)]
    for i in range(n_blocks):
        filler = ("991122334455667788," * (chars // 19 + 2))[:chars]
        blocks.append(_block(f"第{i}项 {filler}", 140 + i * 40))
    return blocks


class _TruncatedMainHaS:
    """主查只认领第一个真值（模拟 2048 帽下的截断式作答），其余一概不答。

    残差若被重发，会看到一段"只含填充数字"的 payload ⇒ 返回 {}，从而暴露
    "整页重发"（calls 数量增加）。
    """

    def __init__(self):
        self.calls: list[str] = []

    def ner(self, text, labels=None, **_kwargs):
        self.calls.append(text)
        if _DENSE in "".join(text.split()):
            return {"身份证号": [_DENSE]}
        return {}


def test_truncated_page_does_not_reask_the_whole_page():
    """截断伪影的残差 ≥ 35% 主查且 ≥2000 字符 ⇒ 不再整页重发。"""
    client = _TruncatedMainHaS()
    stage_status: dict = {}
    blocks = _dense_page_blocks()
    entities = asyncio.run(
        run_has_text_analysis(blocks, client, vision_types=None, stage_status=stage_status)
    )

    assert {e["text"] for e in entities} == {_DENSE}
    assert len(client.calls) == 1, (
        f"截断伪影下不得整页重发（实际 {len(client.calls)} 次请求，最长 "
        f"{max((len(c) for c in client.calls), default=0)} 字符）"
    )
    assert stage_status.get("has_text_residual_skipped_oversize") == 1
    assert stage_status.get("has_text_residual_skipped_chars", 0) >= 2000


class _TwoColumnHaS:
    """小页两列合并块：主查只认领第一列，残差是"第二列"（绝对量很小）。"""

    def __init__(self):
        self.calls: list[str] = []

    def ner(self, text, labels=None, **_kwargs):
        self.calls.append(text)
        compact = "".join(text.split())
        if "11010119900101461X" in compact and "22020219850612345" in compact:
            return {"身份证": ["11010119900101461X"]}
        if "22020219850612345" in compact:
            return {"身份证": ["22020219850612345"]}
        return {}


def test_small_residual_is_still_reasked():
    """绝对量小的残差必须照旧重问（双闸的意义：不许用比例误杀小页）。"""
    blocks = [_block("身份证号码 11010119900101461X 身份证号码 22020219850612345", 100)]
    client = _TwoColumnHaS()
    stage_status: dict = {}
    entities = asyncio.run(
        run_has_text_analysis(blocks, client, vision_types=None, stage_status=stage_status)
    )
    assert {e["text"] for e in entities} == {"11010119900101461X", "22020219850612345"}
    assert len(client.calls) >= 2, "小页残差必须重问"
    assert not stage_status.get("has_text_residual_skipped_oversize")


def test_ratio_gate_can_be_disabled(monkeypatch):
    """ratio=0 ⇒ 关闸，退回旧行为（配置化回滚路径）。"""
    monkeypatch.setattr(settings, "HAS_NER_RESIDUAL_MAX_PAYLOAD_RATIO", 0.0, raising=False)
    client = _TruncatedMainHaS()
    stage_status: dict = {}
    asyncio.run(
        run_has_text_analysis(
            _dense_page_blocks(), client, vision_types=None, stage_status=stage_status
        )
    )
    assert not stage_status.get("has_text_residual_skipped_oversize")
    assert len(client.calls) >= 2, "关闸后应恢复整页重发（旧行为）"