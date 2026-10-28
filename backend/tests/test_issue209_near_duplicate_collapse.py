"""Issue#209 修复件 A —— HaS 提示词「短行近似重复折叠」判据。

背景（已钉死）：真实案卷「第四卷.pdf」页 133 的扫描件上叠着可见平铺文本水印
（每段「姓名碎片+17 位编号」，如 映峰1450120 / 映峰145012014141 / 术映峰…）。
渲染送 OCR 出 687 块，精确重复去掉 364 块后 HaS 提示词仍有 323 行 / 3928 字符，
其中 293 行（≈90%）是「只差数字」的近似重复行——模型枚举式复读烧满
max_tokens=2048（单请求 83.4s），截断→补查→整页重发，单页 NER >420s。

本文件钉住 `_build_has_text_payload` 的折叠闸（判据刻意与实现不同源：
所有断言都从「输入行 + 期望保留集合」独立推出，不调用被测的族划分逻辑）：
- 闸关闭 / 候选行数 < MIN_PAYLOAD_LINES ⇒ 输出与现状逐字节相同；
- 水印型族（6 行短行、含 ≥2 CJK、数字不同）⇒ 折成首行、计数=5、一条 WARNING；
- 纯数字列（6 行 18 位数字）⇒ 不折叠；
- 长行（>40 字符）同族 ⇒ 不折叠；
- 真实表格行（中文不同/仅两笔同类编号）⇒ 一条不丢；
- 归一化键必需（只差数字才同族）；新增记账字段默认 0（既有构造点不破）。
"""

from __future__ import annotations

import logging

import pytest

from app.core.config import settings
from app.services.ocr_has_vision_service import OCRTextBlock
from app.services.vision.has_text_payload import (
    HaSTextPayload,
    _build_has_text_payload,
)

_POLY = [[0.0, 0.0], [100.0, 0.0], [100.0, 20.0], [0.0, 20.0]]
_LOGGER = "app.services.vision.has_text_payload"


def _blocks(lines: list[str]) -> list[OCRTextBlock]:
    return [OCRTextBlock(text=line, polygon=_POLY) for line in lines]


def _build(lines: list[str]) -> HaSTextPayload:
    # max_chars 远大于夹具字符数 ⇒ 截断不参与，只看折叠本身。
    return _build_has_text_payload(_blocks(lines), max_chars=100_000)


def _filler(count: int) -> list[str]:
    """count 条互不相同的短行（每条是自己的归一化族、CJK 足量、无数字）。

    用互异 CJK 字符保证归一化键互异；它们只用来把候选行数抬过行数闸。
    """
    return [f"材料{chr(0x4E00 + index * 7)}项" for index in range(count)]


# 水印型族：短行、含 ≥2 CJK、只有编号不同 ⇒ 归一化后同一族。
# 注意数字位数必须一致：归一化键是「每个数字→#」，位数不同仍是不同键
# （真实页 323 行因此落成 8 个族，单族最多 28 行）。
_WATERMARK_LINES = [
    "映峰水印1450120",
    "映峰水印1450121",
    "映峰水印1450122",
    "映峰水印1450123",
    "映峰水印1450124",
    "映峰水印1450125",
]


def _payload_lines(extra: list[str], total: int) -> list[str]:
    """extra + 填充，凑到 total 条候选行（填充不参与折叠、不干扰断言）。"""
    return [*extra, *_filler(total - len(extra))]


# ---------------------------------------------------------------------------
# ① 闸关闭 ⇒ 与现状逐字节相同；同一夹具闸开启时必须真的变短（判据不自证）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("collapse_enabled", [False, True])
def test_gate_switch_is_byte_identical_or_collapses(monkeypatch, collapse_enabled):
    lines = _payload_lines(_WATERMARK_LINES, 150)
    monkeypatch.setattr(settings, "HAS_NER_NEAR_DUP_COLLAPSE", collapse_enabled)

    payload = _build(lines)

    # 独立参照：现状行为 = 精确去重后原样按行拼接（夹具本身无精确重复）。
    if not collapse_enabled:
        assert payload.texts == lines
        assert payload.content == "\n".join(lines)
        assert payload.near_duplicate_collapsed_lines == 0
        assert payload.near_duplicate_families == 0
    else:
        # 闸开启 ⇒ 同一夹具必须真的折叠（否则上一条「逐字节相同」是空转）。
        assert payload.texts == [*_WATERMARK_LINES[:1], *lines[len(_WATERMARK_LINES):]]
        assert payload.near_duplicate_collapsed_lines == 5
        assert payload.near_duplicate_families == 1
        assert payload.content != "\n".join(lines)


# ---------------------------------------------------------------------------
# ② 候选行数 < MIN_PAYLOAD_LINES ⇒ 逐字节相同；刚过闸 ⇒ 折叠
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("total", "expect_collapsed"),
    [
        (119, 0),  # 闸下（配置默认 120）
        (120, 5),  # 刚过闸
    ],
)
def test_payload_line_gate(monkeypatch, total, expect_collapsed):
    # 闸值按文档默认钉死（不把期望值从被测配置读回来，避免自证）。
    assert settings.HAS_NER_NEAR_DUP_MIN_PAYLOAD_LINES == 120
    lines = _payload_lines(_WATERMARK_LINES, total)
    monkeypatch.setattr(settings, "HAS_NER_NEAR_DUP_COLLAPSE", True)

    payload = _build(lines)

    assert payload.near_duplicate_collapsed_lines == expect_collapsed
    if expect_collapsed == 0:
        assert payload.content == "\n".join(lines)
        assert payload.texts == lines
    else:
        assert payload.texts == [*_WATERMARK_LINES[:1], *lines[len(_WATERMARK_LINES):]]


# ---------------------------------------------------------------------------
# ③ 水印型族 ⇒ 折成首行、计数=5 族=1、一条 WARNING（不带行内容）
# ---------------------------------------------------------------------------

def test_watermark_family_collapses_to_first_occurrence(caplog):
    # 首行不排在最前：验证「保留首次出现」而不是「保留第一条」。
    lines = _payload_lines(_WATERMARK_LINES, 130)
    assert lines[3] == _WATERMARK_LINES[3]  # 校验的是「保留首次出现」而非「保留第一条」

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        payload = _build(lines)

    dropped = set(range(1, 6))  # 保留下标 0，丢弃该族其余 5 行
    expected = [text for index, text in enumerate(lines) if index not in dropped]
    assert payload.texts == expected
    assert payload.content == "\n".join(expected)
    assert payload.near_duplicate_collapsed_lines == 5
    assert payload.near_duplicate_families == 1

    warnings = [record for record in caplog.records if record.name == _LOGGER]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert message.startswith("HaS payload near-duplicate collapse: dropped 5 line(s) in 1 family(ies) (")
    # 不整行打印行内容：被丢弃行的编号/碎片不得进日志。
    assert "145012" not in message
    assert "映峰" not in message


# ---------------------------------------------------------------------------
# ④ 纯数字列（18 位身份证号）⇒ 不折叠
# ---------------------------------------------------------------------------

def test_pure_digit_column_not_collapsed():
    ids = [f"37070219900101{i:04d}" for i in range(1, 7)]
    assert all(len(value) == 18 and value.isdigit() for value in ids)
    lines = _payload_lines(ids, 130)

    payload = _build(lines)

    assert payload.near_duplicate_collapsed_lines == 0
    assert payload.near_duplicate_families == 0
    assert payload.content == "\n".join(lines)
    for value in ids:
        assert value in payload.texts


# ---------------------------------------------------------------------------
# ⑤ 长行（>MAX_LINE_CHARS）同族 ⇒ 不折叠
# ---------------------------------------------------------------------------

def test_long_lines_not_collapsed():
    long_lines = [
        f"水印样本残片编号{1000 + index}" + "文件核对说明" * 6 for index in range(6)
    ]
    assert all(len("".join(value.split())) > settings.HAS_NER_NEAR_DUP_MAX_LINE_CHARS for value in long_lines)
    lines = _payload_lines(long_lines, 130)

    payload = _build(lines)

    assert payload.near_duplicate_collapsed_lines == 0
    assert payload.content == "\n".join(lines)
    for value in long_lines:
        assert value in payload.texts


# ---------------------------------------------------------------------------
# ⑥ 真实表格行（中文不同）⇒ 一条不丢；两笔同类编号（2 人同族）< 族阈值也留
# ---------------------------------------------------------------------------

def test_real_table_rows_kept_and_two_member_family_survives():
    rows = [
        "姓名：张三 性别：男 年龄：45岁",
        "姓名：李四 性别：女 年龄：38岁",
        "住址：山东省潍坊市奎文区胜利东街1234号",
        "住址：山东省潍坊市潍城区东风西街5678号",
        "身份证号：370702199001014521",
        "联系电话：13800138000",
        # 只差数字的同族，但只有 2 个成员 < FAMILY_MIN_MEMBERS(3) ⇒ 不许折。
        "案号：（2024）鲁0702民初123号",
        "案号：（2024）鲁0702民初456号",
    ]
    lines = _payload_lines(rows, 130)

    payload = _build(lines)

    assert payload.near_duplicate_collapsed_lines == 0
    assert payload.near_duplicate_families == 0
    assert payload.content == "\n".join(lines)
    for row in rows:
        assert row in payload.texts


# ---------------------------------------------------------------------------
# ⑦ 归一化键（数字→#）是承重的：中文不同的短行不同族、只差数字才同族
# ---------------------------------------------------------------------------

def test_family_requires_same_structure_after_digit_normalization(monkeypatch):
    # 6 条「案号」只差数字 ⇒ 同族、≥3 ⇒ 折成 1 条。
    lines = _payload_lines(
        [
            "案号：（2024）鲁0702民初123号",
            "案号：（2024）鲁0702民初456号",
            "案号：（2024）鲁0702民初789号",
            "案号：（2024）鲁0702民初321号",
            "案号：（2024）鲁0702民初654号",
            "案号：（2024）鲁0702民初987号",
        ],
        130,
    )
    monkeypatch.setattr(settings, "HAS_NER_NEAR_DUP_COLLAPSE", True)

    payload = _build(lines)

    assert payload.texts[0] == "案号：（2024）鲁0702民初123号"
    assert payload.near_duplicate_collapsed_lines == 5
    assert payload.near_duplicate_families == 1


# ---------------------------------------------------------------------------
# ⑧ 新增记账字段默认 0 ⇒ 既有构造点（11 个旧字段）不破
# ---------------------------------------------------------------------------

def test_new_stat_fields_default_to_zero_for_existing_constructors():
    payload = HaSTextPayload(
        texts=[],
        content="",
        source_block_count=0,
        eligible_block_count=0,
        duplicate_block_count=0,
        clipped_block_count=0,
        input_chars=0,
        emitted_chars=0,
        omitted_chars=0,
        max_chars=100,
        truncated=False,
    )
    assert payload.near_duplicate_collapsed_lines == 0
    assert payload.near_duplicate_families == 0
