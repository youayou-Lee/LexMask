"""日期/时间归一化与粒度容忍判等（Issue#56 P0，口径见方案 §7.3）。

**口径（2026-10-14 用户定稿）**

- 标准形态：``2023年6月15日``（中文、不补零、含年月日）
- 判等口径：**粒度差算同一实体**（``2023年`` ≡ ``2023年6月15日``）

**判等实现** ``date_equal`` 的四步：

1. 各自归一化到 ``(Y, M, D, h, m, s)`` 六元组（缺位记 ``None``；中文数字→阿拉伯、
   去分隔符、去补零）
2. 任一侧**有值位冲突** → ``different``
3. 逐位全等 → ``same``（严格）
4. 一侧的**有值位集合是另一侧的真子集** → ``same_granularity_gap``，并给出
   ``gap_direction``（``a_coarser`` / ``b_coarser``）

第 4 步用**子集**而不是"层级截断"，是为了让 ``6月15日``（无年）这类合法形态
也能参与比较；两侧集合互不包含（如只有「月」对只有「日」）→ ``different``，
标 ``字段集不可比``。

**为什么不能只出一个布尔值**：``granularity_gap`` 在两种场景下处理**相反**——

- **GT 生产**：**不自动放行**，必须过证据闸门。粒度差可能是 OCR 截断（一侧只读到
  「2023年」），也可能是**凭空补全**（另一侧多出月日）。方向决定危险程度：
  **更粗 = 丢细节，更细 = 可能凭空补全**。
- **评测打分**：GT 冻结后评产品输出，按口径**计为正确**。

所以 ``date_equal`` 同时返回 ``strict`` 与 ``lenient`` **两个分数**，
调用方各取所需；两者不合并，口径将来再变历史数据也不废。

返回值统一用 **dict**（与 ``compare.CompareResult = dict`` 同风格），可 JSON 序列化。
"""
from __future__ import annotations

import re
from datetime import date

from gt.normalize import normalize_text
from gt.value_norm import parse_chinese_number

#: 六元组的规范键序（子集比较与冲突枚举都依赖它）
PARTS_KEYS = ("year", "month", "day", "hour", "minute", "second")

VERDICTS = ("same", "same_granularity_gap", "different", "unparsable")
GAP_DIRECTIONS = ("a_coarser", "b_coarser")

STANDARD_FORM = "2023年6月15日"

# 数字 token：阿拉伯串或中文数字串
_N = r"(?:\d{1,4}|[〇零一二两三四五六七八九十百千万亿壹贰叁肆伍陆柒捌玖拾佰仟]+)"

_RE_PERIOD = re.compile(r"(上午|早上|凌晨|中午|下午|傍晚|晚上)")
_RE_TIME_HMS = re.compile(rf"(?P<h>{_N})[:：](?P<mi>{_N})[:：](?P<s>{_N})")
_RE_TIME_HM = re.compile(rf"(?P<h>{_N})[:：](?P<mi>{_N})")
_RE_TIME_CN = re.compile(
    rf"(?P<h>{_N})\s*[时時点點]\s*(?:(?P<mi>{_N})\s*分?)?\s*(?:(?P<s>{_N})\s*秒?)?"
)

_RE_SEP_YMD = re.compile(r"^(?P<y>\d{2,4})[-/.](?P<m>\d{1,2})[-/.](?P<d>\d{1,2})$")
_RE_SEP_YM = re.compile(r"^(?P<y>\d{2,4})[-/.](?P<m>\d{1,2})$")
_RE_COMPACT_YMD = re.compile(r"^(?P<y>\d{4})(?P<m>\d{2})(?P<d>\d{2})$")
_RE_BARE_Y = re.compile(r"^(?P<y>\d{4})$")
_RE_CN_Y = re.compile(rf"(?P<v>{_N})\s*年")
_RE_CN_M = re.compile(rf"(?P<v>{_N})\s*月")
_RE_CN_D = re.compile(rf"(?P<v>{_N})\s*[日号]")


def _to_int(tok: str | None) -> int | None:
    if tok is None:
        return None
    if tok.isdigit():
        return int(tok)
    return parse_chinese_number(tok)


def _extract_time(s: str) -> tuple[dict | None, str]:
    """摘出时间部分，返回 ``(时间 dict | None, 剩余串)``。

    支持 ``10:30:25`` / ``10:30`` / ``10时30分25秒`` / ``十时三十分``；
    ``下午3点`` 一类前缀会把 12 小时制补成 24 小时制。
    """
    period_match = _RE_PERIOD.search(s)
    period = period_match.group(1) if period_match else None
    for rx in (_RE_TIME_HMS, _RE_TIME_HM, _RE_TIME_CN):
        m = rx.search(s)
        if not m:
            continue
        hour = _to_int(m.group("h"))
        if hour is None:
            continue
        minute = _to_int(m.groupdict().get("mi"))
        second = _to_int(m.groupdict().get("s"))
        if period in ("下午", "傍晚", "晚上") and 0 < hour < 12:
            hour += 12
        rest = _RE_PERIOD.sub("", s[: m.start()] + s[m.end():])
        return {"hour": hour, "minute": minute, "second": second}, rest
    return None, _RE_PERIOD.sub("", s)


def _extract_date(s: str) -> dict | None:
    """从串里抽出年月日；抽不到返回 ``None``。"""
    s = s.strip()
    if not s:
        return None
    for rx in (_RE_SEP_YMD, _RE_COMPACT_YMD):
        m = rx.match(s)
        if m:
            return {"year": int(m.group("y")), "month": int(m.group("m")),
                    "day": int(m.group("d"))}
    m = _RE_SEP_YM.match(s)
    if m:
        return {"year": int(m.group("y")), "month": int(m.group("m"))}
    m = _RE_BARE_Y.match(s)
    if m:
        return {"year": int(m.group("y"))}

    parts: dict[str, int | None] = {}
    m = _RE_CN_Y.search(s)
    if m:
        parts["year"] = _to_int(m.group("v"))
        s = s[m.end():]
    m = _RE_CN_M.search(s)
    if m:
        parts["month"] = _to_int(m.group("v"))
        s = s[m.end():]
    m = _RE_CN_D.search(s)
    if m:
        parts["day"] = _to_int(m.group("v"))
    return parts or None


def _valid(p: dict[str, int | None]) -> bool:
    y, m, d = p["year"], p["month"], p["day"]
    h, mi, sec = p["hour"], p["minute"], p["second"]
    if y is not None and not 1000 <= y <= 2999:
        return False
    if m is not None and not 1 <= m <= 12:
        return False
    if d is not None and not 1 <= d <= 31:
        return False
    if y is not None and m is not None and d is not None:
        try:
            date(y, m, d)
        except ValueError:
            return False
    if h is not None and not 0 <= h <= 23:
        return False
    if mi is not None and not 0 <= mi <= 59:
        return False
    if sec is not None and not 0 <= sec <= 59:
        return False
    # 结构自洽：有低一级必有高一级
    if mi is not None and h is None:
        return False
    if sec is not None and mi is None:
        return False
    if d is not None and m is None:
        return False
    return True


def normalize_date(raw: str) -> dict | None:
    """日期/时间文本 → 六元组 dict（缺位为 ``None``）；无法解析返回 ``None``。"""
    s = normalize_text(raw)
    if not s:
        return None
    time_part, rest = _extract_time(s)
    date_part = _extract_date(rest)
    if date_part is None and time_part is None:
        return None
    parts: dict[str, int | None] = dict.fromkeys(PARTS_KEYS)
    if date_part:
        parts.update(date_part)
    if time_part:
        parts.update(time_part)
    if not _valid(parts):
        return None
    return parts


def render_date(parts: dict[str, int | None] | None) -> str:
    """六元组 → 标准形态（``2023年6月15日`` / ``10时30分``）。"""
    if not parts:
        return ""
    out = ""
    if parts.get("year") is not None:
        out += f"{parts['year']}年"
    if parts.get("month") is not None:
        out += f"{parts['month']}月"
    if parts.get("day") is not None:
        out += f"{parts['day']}日"
    if parts.get("hour") is not None:
        out += f"{parts['hour']}时"
    if parts.get("minute") is not None:
        out += f"{parts['minute']}分"
    if parts.get("second") is not None:
        out += f"{parts['second']}秒"
    return out


def date_equal(a_text: str, b_text: str) -> dict:
    """两侧日期读数判等。

    返回 dict：``verdict``（∈ VERDICTS）/ ``strict`` / ``lenient`` /
    ``granularity_gap`` / ``gap_direction`` / ``conflicts`` / ``a`` / ``b`` / ``reason``。

    ``unparsable`` 是**保守**结论：调用方（闸门）对它的处理是"必查"，不是"放行"。
    """
    pa = normalize_date(a_text)
    pb = normalize_date(b_text)
    base: dict = {"a": pa, "b": pb, "granularity_gap": False,
                  "gap_direction": None, "conflicts": []}
    if pa is None or pb is None:
        return {**base, "verdict": "unparsable", "strict": False, "lenient": False,
                "reason": "至少一侧无法解析为日期"}
    conflicts = [k for k in PARTS_KEYS
                 if pa[k] is not None and pb[k] is not None and pa[k] != pb[k]]
    if conflicts:
        return {**base, "verdict": "different", "strict": False, "lenient": False,
                "conflicts": conflicts,
                "reason": "有值位冲突：" + "、".join(conflicts)}
    if all(pa[k] == pb[k] for k in PARTS_KEYS):
        return {**base, "verdict": "same", "strict": True, "lenient": True, "reason": ""}

    set_a = frozenset(k for k in PARTS_KEYS if pa[k] is not None)
    set_b = frozenset(k for k in PARTS_KEYS if pb[k] is not None)
    if set_a < set_b:
        direction = "a_coarser"
    elif set_b < set_a:
        direction = "b_coarser"
    else:
        return {**base, "verdict": "different", "strict": False, "lenient": False,
                "reason": "字段集不可比（非粒度差）"}
    return {**base, "verdict": "same_granularity_gap", "strict": False, "lenient": True,
            "granularity_gap": True, "gap_direction": direction,
            "reason": "粒度差：一方为另一方的粗化形态"}


def date_scores(a_text: str, b_text: str) -> dict[str, bool]:
    """口径双分数（评测打分用；看似冗余，但**两个都出、不合并**）。

    - ``date_strict``：逐位全等
    - ``date_lenient``：含粒度差（= 用户定稿口径）
    """
    verdict = date_equal(a_text, b_text)
    return {"date_strict": verdict["strict"], "date_lenient": verdict["lenient"]}