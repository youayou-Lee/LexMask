"""数值/金额归一与判等（Issue#56 P0）。

配合方案 §7 三分法：``quantity`` 域（金额 / 年龄 / 生命体征）的放行规则是
「归一到 ``(数值, 单位)`` 后必须相等，**无粒度容忍**」——与 ``datetime`` 正相反。

- ``10000元`` 与 ``1万元`` 归一后**必须相等** ✅
- ``10000`` 与 ``100000`` 是**值差**，必须必查 ❌（差一个 0 就是漏遮蔽）

**货币单位一律换算到「元」**（``万元`` ×1e4、``亿元`` ×1e8、``角`` ×0.1），
但**外币不换算**（无汇率则不得判等）：``10000港币`` 与 ``10000元`` 归一后单位不同
→ 判 ``different``，保守正确。

本模块还对外提供 ``parse_chinese_number``——中文数字解析是日期与数值两处共用的
原语，``date_norm`` 从这里导入（依赖方向：``value_norm`` ← ``date_norm``）。

返回值统一用 **dict**（与 ``compare.CompareResult = dict`` 同风格），
``value`` 以**字符串**形态给出（``Decimal`` 精确、可 JSON 序列化，不落浮点误差）。
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from gt.normalize import normalize_text

VERDICTS = ("same", "different", "unparsable")

#: ``quantity`` 域的三种类型 → 归一化口径（须与 ``type_classes`` 的 quantity 域完全一致）
VALUE_KIND_BY_TYPE: dict[str, str] = {
    "金额": "amount",
    "年龄": "quantity",
    "生命体征": "quantity",
}

#: 金额单位 → 「元」的倍率。故意不含「分」——它与「分钟」歧义太大，宁可不折算。
_MONEY_MULTIPLIER: dict[str, Decimal] = {
    "元": Decimal(1),
    "块": Decimal(1),
    "圆": Decimal(1),
    "角": Decimal("0.1"),
    "毛": Decimal("0.1"),
    "千元": Decimal(1000),
    "万元": Decimal(10000),
    "万": Decimal(10000),
    "百万元": Decimal(1000000),
    "亿元": Decimal(100000000),
    "亿": Decimal(100000000),
}

#: 非金额单位同义归一（不做倍率换算，只把写法收敛到一个规范串）
_UNIT_ALIASES: dict[str, str] = {
    "岁": "岁", "周岁": "岁", "虚岁": "岁",
    "年": "年", "个月": "个月", "月": "个月",
    "天": "天", "日": "天",
    "℃": "℃", "度": "℃", "摄氏度": "℃",
    "mmHg": "mmHg", "毫米汞柱": "mmHg",
    "次/分": "次/分", "次每分钟": "次/分", "bpm": "次/分", "次分": "次/分",
    "%": "%", "百分之": "%",
    "kg": "kg", "公斤": "kg", "千克": "kg",
    "g": "g", "克": "g",
    "cm": "cm", "厘米": "cm", "mm": "mm", "毫米": "mm", "m": "m", "米": "m",
}

#: 尾部噪声（「10000元整」的「整」）
_TRAILING_NOISE = ("整", "正", "左右", "余")
#: 本币前缀（=「元」，剥离后不影响判等）
_CURRENCY_PREFIXES = ("人民币", "RMB", "¥", "￥")
#: 外币前缀（无汇率不得判等——前缀符号型外币同样适用，评审 Important#1）
_FOREIGN_CURRENCY_PREFIXES = ("US$", "$", "＄", "美元 ", "港元 ", "港币 ")

_CN_DIGITS: dict[str, int] = {
    "〇": 0, "零": 0,
    "一": 1, "壹": 1, "二": 2, "两": 2, "贰": 2,
    "三": 3, "叁": 3, "四": 4, "肆": 4,
    "五": 5, "伍": 5, "六": 6, "陆": 6,
    "七": 7, "柒": 7, "八": 8, "捌": 8, "九": 9, "玖": 9,
}
_CN_UNITS: dict[str, int] = {
    "十": 10, "拾": 10, "百": 100, "佰": 100, "千": 1000, "仟": 1000,
    "万": 10000, "亿": 100000000,
}

_SMALL_SCALES = "十百千拾佰仟"
_BIG_SCALES = "万亿"
_ANY_SCALES = _SMALL_SCALES + _BIG_SCALES
#: 量级字后面紧跟这些字时，它是**单位词头**（千克/千米/千瓦）而不是「×1000」
_UNIT_HEADS = "克米瓦帕焦卡赫伏安欧升"


def parse_chinese_number(tok: str) -> int | None:
    """中文数字 → 整数；无法解析返回 ``None``。

    - **带单位词**（十/百/千/万/亿 及其大写）：走分段累加，``三万五千`` → 35000
    - **纯数字串**：逐位拼接，``二〇二三`` → 2023（年份形态；若按分段累加会得 3）
    """
    if not tok:
        return None
    if any(ch in _CN_UNITS for ch in tok):
        total = section = number = 0
        for ch in tok:
            if ch in _CN_DIGITS:
                number = _CN_DIGITS[ch]
            elif ch in _CN_UNITS:
                unit = _CN_UNITS[ch]
                # 缺省乘数仅在全空时取 1（三亿五千万的万位不能凭空多 1 万）
                default = 1 if not (total or section) else 0
                if unit >= 100000000:
                    # 亿：更高量级，折入 total（三亿 → 3e8）
                    total = (total + section + number) * unit
                    section = 0
                elif unit >= 10000:
                    # 万：section 进位后并入 total（三万亿 = 3e4 * 1e8；
                    # 亿…万… 复合形态里万只并入 total、不再乘大）
                    section = (section + number) * unit
                    total += section
                    section = 0
                else:
                    section += (number or 1) * unit
                number = 0
            else:
                return None
        return total + section + number
    value = 0
    for ch in tok:
        if ch not in _CN_DIGITS:
            return None
        value = value * 10 + _CN_DIGITS[ch]
    return value


def _eval_scaled(tok: str) -> Decimal | None:
    """带量级字的数值表达式求值：中文、阿拉伯、**以及混写**。

    ``3万5千`` → 35000、``1.5万`` → 15000、``三万五千`` → 35000、``一百零五`` → 105、
    ``二〇二三`` → 2023（中文数字连续成串时按**逐位拼接**，否则年份会算成 3）。

    混写必须支持：真实案卷里「3万5千元」这类阿拉伯+量级+阿拉伯的写法很常见，
    早期只认纯阿拉伯前缀的实现会把它读成 ``3`` 配单位 ``万5千``。
    """
    total = Decimal(0)
    section = Decimal(0)
    number: Decimal | None = None
    i, n = 0, len(tok)
    while i < n:
        ch = tok[i]
        if ch.isdigit() or ch in ",.":
            j = i
            while j < n and (tok[j].isdigit() or tok[j] in ",."):
                j += 1
            try:
                number = Decimal(tok[i:j].replace(",", ""))
            except InvalidOperation:
                return None
            i = j
            continue
        if ch in _CN_DIGITS:
            j = i
            while j < n and tok[j] in _CN_DIGITS:
                j += 1
            value = 0
            for c in tok[i:j]:
                value = value * 10 + _CN_DIGITS[c]
            number = Decimal(value)
            i = j
            continue
        if ch in _CN_UNITS:
            unit = Decimal(_CN_UNITS[ch])
            default = Decimal(0) if (total or section) else Decimal(1)
            if unit >= 100000000:
                # 亿：折入 total（评审复审：亿…万… 复合形态里万不能再乘大）
                total = (total + section + (number if number is not None else default)) * unit
                section = Decimal(0)
            elif unit >= 10000:
                # 万：section 进位后并入 total
                section = (section + (number if number is not None else Decimal(1))) * unit
                total += section
                section = Decimal(0)
            else:
                section += (number if number is not None else Decimal(1)) * unit
            number = None
            i += 1
            continue
        return None
    return total + section + (number if number is not None else Decimal(0))


def _scan_number_prefix(s: str) -> tuple[Decimal | None, int]:
    """从 ``s`` 开头扫一个数值表达式，返回 ``(值, 消耗字符数)``。

    量级字是否并入数值，靠**回看下一个字**判断：``5千克`` 的「千」是单位词头
    （→ 5 kg），``3万5千`` 的「千」是量级（→ 35000）。不做这个区分，
    「千克/千米/千瓦」会被读成 ×1000。
    """
    i, n = 0, len(s)
    while i < n:
        ch = s[i]
        if ch.isdigit() or ch in ",.":
            i += 1
            continue
        if ch in _CN_DIGITS:
            i += 1
            continue
        if ch in _SMALL_SCALES:
            nxt = s[i + 1] if i + 1 < n else ""
            if nxt and nxt in _UNIT_HEADS:
                break
            i += 1
            continue
        if ch in _BIG_SCALES:
            i += 1
            continue
        break
    tok = s[:i].rstrip(",.")
    if not tok:
        return None, 0
    return _eval_scaled(tok), i


def _dec_str(value: Decimal) -> str:
    """``Decimal`` → 无科学计数法的精确字符串（``Decimal("1E+4")`` → ``"10000"``）。"""
    return format(value.normalize(), "f")


def normalize_number(raw: str, kind: str | None = None) -> dict | None:
    """把一段数值文本归一到 ``{"value": str, "unit": str | None}``；解析失败返回 ``None``。

    ``kind``：``"amount"``（金额域，无单位默认「元」）/ ``"quantity"`` / ``None``（通用）。
    货币单位无论 ``kind`` 一律换算到「元」（写法本身已无歧义）。

    **单位判定取严**：认不出的单位只有在 ``kind == "amount"`` 时才保留（外币靠它区分：
    ``10000港币`` → 单位「港币」，与「元」判不等）。其他情况下认不出的单位一律判
    ``None``（不可解析 → 必查），否则「一般情况」这类普通文本会被读成「数值 1 + 单位 般情况」。
    """
    s = normalize_text(raw)
    if not s:
        return None
    foreign_unit: str | None = None
    for prefix in _FOREIGN_CURRENCY_PREFIXES:
        if s.startswith(prefix):
            # 外币：符号转成独立单位保留，绝不折算成「元」（无汇率不得判等）
            foreign_unit = {"$": "美元", "US$": "美元", "＄": "美元",
                            "美元 ": "美元", "港元 ": "港元", "港币 ": "港元"}[prefix]
            s = s[len(prefix):]
            break
    if foreign_unit is None:
        for prefix in _CURRENCY_PREFIXES:
            if s.startswith(prefix):
                s = s[len(prefix):]
                break
    if not s:
        return None

    value, consumed = _scan_number_prefix(s)
    if value is None or consumed == 0:
        return None
    unit_raw = s[consumed:].replace("人民币", "")
    for noise in _TRAILING_NOISE:
        if unit_raw.endswith(noise):
            unit_raw = unit_raw[: -len(noise)]
            break

    unit: str | None = unit_raw or None
    if unit is not None and unit in _MONEY_MULTIPLIER:
        value *= _MONEY_MULTIPLIER[unit]
        unit = "元"
    elif unit is not None and unit in _UNIT_ALIASES:
        unit = _UNIT_ALIASES[unit]
    elif unit is not None and kind != "amount":
        # 认不出的单位：只有金额域保留（外币），其余按不可解析处理
        return None
    if unit is None and foreign_unit is not None:
        unit = foreign_unit
    elif unit is not None and foreign_unit is not None:
        unit = f"{foreign_unit}{unit}"  # 如 $100万 → 美元万（不折算，保序即可判不等）
    elif unit is None and kind == "amount":
        unit = "元"

    return {"value": _dec_str(value), "unit": unit}


def value_equal(a_text: str, b_text: str, kind: str | None = None) -> dict:
    """两侧数值读数判等。返回 dict（键：``verdict`` / ``equal`` / ``a`` / ``b`` / ``reason``）。

    ``unparsable`` 是**保守**结论：调用方（闸门）对它的处理是"必查"，不是"放行"。
    """
    na = normalize_number(a_text, kind=kind)
    nb = normalize_number(b_text, kind=kind)
    if na is None or nb is None:
        return {
            "verdict": "unparsable",
            "equal": False,
            "a": na,
            "b": nb,
            "reason": "至少一侧无法解析为数值",
        }
    equal = na["value"] == nb["value"] and na["unit"] == nb["unit"]
    if equal:
        reason = ""
    elif na["value"] != nb["value"]:
        reason = f"数值不等：{na['value']} ≠ {nb['value']}"
    else:
        reason = f"单位不等：{na['unit']} ≠ {nb['unit']}（无汇率，不得判等）"
    return {"verdict": "same" if equal else "different", "equal": equal,
            "a": na, "b": nb, "reason": reason}