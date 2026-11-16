"""Issue#286 P0：类型校验层（`agent_md_type_guard`）+ 映射草稿接线。

病根（律师 80 页真卷复核）：模型回的是「这串可能是编号」，产物却把它当「案号」——
168 条「案号」里符合法院案号格式的 **0** 条、75 条「银行卡」里过 Luhn 的 **18** 条。
假类型比缺省类型更糟：下游 Agent 会按类型语义处理（把 19 位账号当案号引用），
而产物上看不出任何异常。

本文件钉三件事：
1. 校验器本身（案号/银行卡/身份证/手机/微信），含**反例**（不得把普通数字判成案号）；
2. `identify()` 的处置口径：案号族声明不符 ⇒ 降级 `GEN_NUMBER_CODE`；
   具体强类型（银行卡/身份证/手机/微信）校验不过 ⇒ **类型保留 + `format_error` 标记**
   （OCR 噪声导致的校验失败不该把「这是银行卡」这件事抹掉）；
3. 映射草稿接线：类型与占位符按**校验后**类型走，`type_status`/`type_reason` 随映射表出。
"""
import pytest

from app.models.schemas import Entity
from app.services.agent_md_ner import build_mapping_draft
from app.services.agent_md_type_guard import identify, is_bank_card, is_case_number

# 真卷形态（脱敏合成，仅保留长度/字符集特征）：
# 86 页扫描件 168 条「案号」里 0 条符合法院案号格式——多为账号/流水号。
_CASE_OK = [
    "（2021）桂0603民初30号",
    "(2025)桂0103执恢769号",
    "（2026）示0101民初219号",
    "（2023）最高法民申1234号",
    "（2020）京01民终123号",
    "（2019）桂0103执异12号",
]
_CASE_BAD = [
    "6236683370001196422",   # 19 位账号/流水
    "6236683370001196",      # 16 位卡号
    "2022年1月25日",          # 日期
    "（2025）桂0103执恢769",   # 缺「号」
    "桂0103民初30号",         # 缺年份
    "2025桂0103民初30号",     # 缺括号（宽松形态也接受，见下）
]


@pytest.mark.parametrize("text", _CASE_OK)
def test_is_case_number_accepts_court_case_number_shapes(text):
    assert is_case_number(text) is True


@pytest.mark.parametrize("text", _CASE_BAD[:5])
def test_is_case_number_rejects_non_case_number_shapes(text):
    assert is_case_number(text) is False


def test_is_case_number_accepts_bracketless_year_form():
    """（2025）与 2025 都放行：OCR 常吃掉括号，但年份+代字+序号+号骨架还在。"""
    assert is_case_number("2025桂0103民初30号") is True


def test_is_bank_card_luhn():
    # 经典 Luhn 通过样本（非真实卡号）
    assert is_bank_card("6222021234567890128") is True      # 19 位，Luhn 通过（实算）
    assert is_bank_card("6222021234567890124") is False     # 改一位，Luhn 失败（实算）


def test_declared_case_number_that_is_really_an_account_is_downgraded():
    """核心病根：声明「案号」但格式不符 ⇒ 降级为通用编号，而不是继续假称案号。"""
    chk = identify("6236683370001196423", "CASE_NUMBER")
    assert chk.declared == "CASE_NUMBER"
    assert chk.final == "GEN_NUMBER_CODE"
    assert chk.status == "downgraded"
    assert chk.reason == "case_number_format_mismatch"


def test_declared_case_number_that_is_really_a_case_number_is_promoted():
    chk = identify("（2021）桂0603民初30号", "CASE_NUMBER")
    assert chk.final == "LEGAL_CASE_ID"
    assert chk.status == "pass"
    assert chk.reason == "case_number_ok"


def test_declared_case_number_that_is_really_a_card_or_id_is_recognised():
    assert identify("6236683370001196422", "CASE_NUMBER").final == "BANK_CARD"
    assert identify("6222021234567890128", "CASE_NUMBER").final == "BANK_CARD"
    chk_id = identify("110101199003071233", "CASE_NUMBER")
    assert chk_id.final == "ID_CARD"
    assert chk_id.reason == "id_card_checksum_ok"


def test_declared_strong_type_failing_check_keeps_type_and_flags():
    """银行卡/身份证是「声明即具体」的类型：校验不过只标 format_error，不改类型。

    依据：真卷 75 条银行卡里 57 条 Luhn 不过，绝大多数是 OCR 看错一位——
    抹掉「这是银行卡」会把一个已知的强信号降级成噪声。
    """
    chk = identify("6222021234567890124", "BANK_CARD")  # Luhn 实算不过
    assert chk.final == "BANK_CARD"
    assert chk.status == "format_error"
    assert chk.reason == "bank_card_luhn_fail"


def test_declared_strong_type_passing_check_is_quiet():
    chk = identify("6222021234567890128", "BANK_CARD")
    assert (chk.status, chk.reason) == ("pass", "bank_card_ok")


def test_non_numeric_types_are_not_format_checked():
    """PERSON/ADDRESS/ORG 没有可校验的格式：一律 pass，类型原样。"""
    for text, declared in [("张某", "PERSON"), ("某市某区某路1号", "ADDRESS"),
                           ("某银行某支行", "ORG"), ("桂B12345", "LICENSE_PLATE")]:
        chk = identify(text, declared)
        assert chk.final == declared
        assert chk.status == "pass"


def test_mapping_draft_uses_checked_type_and_reports_status():
    """接线：映射草稿的 entity_type/占位符按校验后类型走，且带出校验结论。"""
    items = build_mapping_draft([
        Entity(id="a", text="（2021）桂0603民初30号", type="CASE_NUMBER", start=0, end=18, page=1),
        Entity(id="b", text="6236683370001196423", type="CASE_NUMBER", start=30, end=49, page=1),
    ])
    by_text = {m.original_text: m for m in items}
    ok = by_text["（2021）桂0603民初30号"]
    bad = by_text["6236683370001196423"]
    assert ok.entity_type == "LEGAL_CASE_ID"
    assert ok.replacement == "案号1"
    assert (ok.type_status, ok.type_reason) == ("pass", "case_number_ok")
    assert bad.entity_type == "GEN_NUMBER_CODE"
    # 占位符不得回落英文 type id
    assert bad.replacement == "编号1"
    assert (bad.type_status, bad.type_reason) == ("downgraded", "case_number_format_mismatch")


def test_mapping_draft_keeps_declared_type_for_traceability():
    """律师要能看出「模型原来说这是什么」：declared_type 保留声明值。"""
    items = build_mapping_draft([
        Entity(id="a", text="6236683370001196423", type="CASE_NUMBER", start=0, end=19, page=1),
    ])
    assert items[0].declared_type == "CASE_NUMBER"
    assert items[0].entity_type == "GEN_NUMBER_CODE"
    assert items[0].entity_type == "GEN_NUMBER_CODE"


def test_mapping_draft_numbers_by_checked_type_not_declared():
    """编号按**校验后**类型分组：案号与编号各自从 1 起，不混号。"""
    items = build_mapping_draft([
        Entity(id="a", text="（2021）桂0603民初30号", type="CASE_NUMBER", start=0, end=18, page=1),
        Entity(id="b", text="6236683370001196423", type="CASE_NUMBER", start=30, end=49, page=1),
        Entity(id="c", text="6236683370001196425", type="CASE_NUMBER", start=60, end=79, page=1),
    ])
    got = {m.original_text: m.replacement for m in items}
    assert got["（2021）桂0603民初30号"] == "案号1"
    assert got["6236683370001196423"] == "编号1"
    assert got["6236683370001196425"] == "编号2"


def test_mapping_draft_merges_same_text_same_checked_type():
    """同一原文经两种声明落到同一实际类型时只出一行（避免 last-row-wins 丢决策）。"""
    items = build_mapping_draft([
        Entity(id="a", text="6222021234567890128", type="CASE_NUMBER", start=0, end=19, page=1),
        Entity(id="b", text="6222021234567890128", type="BANK_CARD", start=40, end=59, page=1),
    ])
    assert len(items) == 1
    assert items[0].entity_type == "BANK_CARD"
    assert items[0].replacement == "银行卡1"


def test_mapping_draft_excluded_rules_unchanged():
    """本次不改「保留/打码」判定口径：单字符、≤4 位纯数字、ORG 仍默认保留。"""
    items = build_mapping_draft([
        Entity(id="a", text="4", type="CASE_NUMBER", start=0, end=1, page=1),
        Entity(id="b", text="某银行某支行", type="ORG", start=5, end=11, page=1),
        Entity(id="c", text="（2021）桂0603民初30号", type="CASE_NUMBER", start=20, end=38, page=1),
    ])
    by_text = {m.original_text: m for m in items}
    assert by_text["4"].excluded is True
    assert by_text["某银行某支行"].excluded is True
    assert by_text["（2021）桂0603民初30号"].excluded is False


def test_placeholder_labels_have_no_english_type_id():
    """占位符中文短名表补齐：GEN_NUMBER_CODE/LEGAL_CASE_ID 不得回落英文 id。"""
    from app.services.agent_md_ner import _TYPE_ZH

    assert _TYPE_ZH["GEN_NUMBER_CODE"] == "编号"
    assert _TYPE_ZH["LEGAL_CASE_ID"] == "案号"
    items = build_mapping_draft([
        Entity(id="a", text="6236683370001196423", type="CASE_NUMBER", start=0, end=19, page=1),
    ])
    assert items[0].replacement.startswith("编号")
    assert "GEN_NUMBER_CODE" not in items[0].replacement
