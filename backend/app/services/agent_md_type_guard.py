"""Issue#286 P0：识别结果类型校验层（供料脱敏专用）。

病根（律师 80 页真卷复核，2026-11-12）：模型回的是**声明类型**——「这串可能是编号」；
产物却把声明当事实——映射表 168 条「案号」里符合法院案号格式的 **0** 条、75 条
「银行卡」里过 Luhn 的 **18** 条。假类型比缺省类型更糟：下游 Agent 会按类型语义
处理（把 19 位账号当案号引用、把流水号当卡号对账），而产物上看不出任何异常。

## 处置口径（两档，不对称，理由见各函数）

| 声明类型 | 校验结果 | 处置 |
|---|---|---|
| `CASE_NUMBER`（模型来自「编号」桶，格式是唯一证据） | 符合案号格式 | 提升为 `LEGAL_CASE_ID`，`pass` |
| 同上 | 实为卡号/身份证/手机/微信 | 改判为该具体类型，`pass` |
| 同上 | 都不像 | 降级 `GEN_NUMBER_CODE`，`downgraded` |
| `BANK_CARD` / `ID_CARD` / `PHONE` / `WECHAT_ALIPAY`（声明即具体） | 校验不过 | **类型保留 + `format_error`**（真卷 57/75 卡号 Luhn 不过，绝大多数是 OCR 看错一位；抹掉「这是卡号」等于把一个强信号降级成噪声） |
| 其他类型（PERSON/ADDRESS/ORG…） | 无可校验格式 | `pass`，类型原样 |

## 不做的事（本层边界）

- **不改打码与否**：校验只影响「这叫什么」和占位符文字，不影响这一项是否替换。
- **不做泛数字兜底**：12–25 位未识别数字串归 Issue#286 的 E 批（需先定召回策略与误报面）。
- **不做机构分级**：D 批，需先与用户确认反转「机构名保留」裁定。
- **不阻塞**：校验器永不抛异常；任何异常都退化为「类型原样 + `format_error`」。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# 法院案号骨架：年份（可带括号）+ 法院代字（1–3 汉字 + 0–4 位数字）+ 类型代字 + 序号 + 「号」。
# 括号可有可无：OCR 常吃掉全角括号，但骨架还在（「2025桂0103民初30号」）。
_CASE_NO_RE = re.compile(
    r"^[（(]?\s*(?P<year>(?:19|20)\d{2})\s*[)）]?\s*"
    r"(?P<court>[\u4e00-\u9fff]{1,3}\d{0,4})\s*"
    r"(?P<kind>[\u4e00-\u9fff]{1,4})\s*"
    r"(?P<seq>\d{1,7})\s*号\s*$"
)

# 类型代字白名单（《人民法院案件案号标准》常见值）。白名单外一律不认：
# 宁可把真案号降级成「编号」，也不能把 19 位账号升成「案号」。
_CASE_KINDS = frozenset({
    "民初", "民终", "民再", "民申", "民特", "民辖", "民保", "民催", "民督",
    "民调", "民确", "民他",
    "刑初", "刑终", "刑再", "刑申",
    "行初", "行终", "行再", "行申", "行辖", "行他",
    "执", "执异", "执复", "执监", "执他", "执恢", "强执", "恢",
    "财保", "破", "商初", "商终", "商再",
    "知民初", "知民终", "知民再", "知刑初",
    "赔", "赔监", "赔他", "司法确认",
})

_ID_CARD_RE = re.compile(r"^\d{17}[\dXx]$")
_ID_CARD_WEIGHTS = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
_ID_CARD_CHECK = "10X98765432"

_PHONE_RE = re.compile(r"^1[3-9]\d{9}$")

# 微信/支付账号：wxid_ 前缀（OCR 常把下划线写成各种形态）或字母开头的 6–20 位账号。
_WECHAT_RE = re.compile(r"^(?:wxid_[0-9A-Za-z_\-]{4,}|[A-Za-z][A-Za-z0-9_\-]{5,19})$")

_BANK_CARD_MIN, _BANK_CARD_MAX = 12, 19


@dataclass(frozen=True)
class TypeCheck:
    """一条实体的类型校验结论。

    declared —— 模型/正则给的声明类型（留痕：律师要能看出「模型原来说这是什么」）；
    final    —— 校验后的最终类型（映射表与占位符都用它）；
    status   —— ``pass`` / ``downgraded`` / ``format_error``；
    reason   —— 机器可读原因码（供 UI 与可观测用）。
    """

    declared: str
    final: str
    status: str
    reason: str


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = ord(ch) - 48
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def is_case_number(text: str) -> bool:
    """是否符合中国法院案号形态（年份 + 法院代字 + 类型代字 + 序号 + 号）。"""
    mo = _CASE_NO_RE.match((text or "").strip())
    return bool(mo) and mo.group("kind") in _CASE_KINDS


def is_bank_card(text: str) -> bool:
    """卡号形态：12–19 位纯数字且过 Luhn 校验（含 6222 系，但不限 6222）。"""
    s = (text or "").strip()
    if not (_BANK_CARD_MIN <= len(s) <= _BANK_CARD_MAX) or not s.isdigit():
        return False
    return _luhn_ok(s)


def is_id_card(text: str) -> bool:
    """身份证形态：18 位且校验位正确（含 X 结尾）。"""
    s = (text or "").strip()
    if not _ID_CARD_RE.match(s):
        return False
    try:
        total = sum(int(s[i]) * _ID_CARD_WEIGHTS[i] for i in range(17))
    except ValueError:  # pragma: no cover - 正则已保证前 17 位是数字
        return False
    return _ID_CARD_CHECK[total % 11] == s[17].upper()


def is_phone(text: str) -> bool:
    """手机号形态：11 位、1[3-9] 开头。"""
    return bool(_PHONE_RE.match((text or "").strip()))


def is_wechat(text: str) -> bool:
    """微信/支付账号形态。"""
    return bool(_WECHAT_RE.match((text or "").strip()))


# (判定函数, 改判类型, reason 码)。CASE_NUMBER 声明下的「其实是什么」判定顺序：
# 案号 → 身份证 → 卡号 → 手机 → 微信。身份证优先于卡号（18 位两者皆可时取更强的校验）。
_STRONG_FORM_CHECKS: tuple[tuple[str, str, str], ...] = (
    ("id_card", "ID_CARD", "id_card_checksum_ok"),
    ("bank_card", "BANK_CARD", "bank_card_ok"),
    ("phone", "PHONE", "phone_ok"),
    ("wechat", "WECHAT_ALIPAY", "wechat_ok"),
)
_STRONG_CHECK_FNS = {
    "id_card": is_id_card,
    "bank_card": is_bank_card,
    "phone": is_phone,
    "wechat": is_wechat,
}

# 声明即具体的强类型：校验不过只标记，不改类型（理由见模块 docstring）。
_STRONG_TYPES: dict[str, tuple[str, str]] = {
    "BANK_CARD": (is_bank_card, "bank_card_luhn_fail"),
    "ID_CARD": (is_id_card, "id_card_checksum_fail"),
    "PHONE": (is_phone, "phone_format_fail"),
    "WECHAT_ALIPAY": (is_wechat, "wechat_format_fail"),
}


def identify(text: str, declared: str) -> TypeCheck:
    """校验一条实体的声明类型，返回处置结论（永不抛异常）。

    校验器的职责边界是「这串到底是什么」，不是「要不要打码」——
    ``build_mapping_draft`` 仍按原有规则决定 excluded。
    """
    try:
        return _identify(text, declared)
    except Exception:  # noqa: BLE001 - 校验层永不阻塞主链路（fail-open）
        return TypeCheck(declared, declared, "format_error", "guard_error")


def _identify(text: str, declared: str) -> TypeCheck:
    s = (text or "").strip()
    if declared == "CASE_NUMBER":
        if is_case_number(s):
            return TypeCheck(declared, "LEGAL_CASE_ID", "pass", "case_number_ok")
        for name, final, reason in _STRONG_FORM_CHECKS:
            if _STRONG_CHECK_FNS[name](s):
                return TypeCheck(declared, final, "pass", reason)
        return TypeCheck(declared, "GEN_NUMBER_CODE", "downgraded",
                         "case_number_format_mismatch")
    strong = _STRONG_TYPES.get(declared)
    if strong is not None:
        fn, reason = strong
        if not fn(s):
            return TypeCheck(declared, declared, "format_error", reason)
        return TypeCheck(declared, declared, "pass", f"{declared.lower()}_ok")
    return TypeCheck(declared, declared, "pass", "")
