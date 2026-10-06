"""日期形态正则（Issue#52）：法律文书高确定性日期形态的槽位组合生成器。

运行时唯一源头仍是 config/preset_entity_types.json 里 DATE / BIRTH_DATE
两条 regex_pattern（内置定义以源码为准、部署自动生效）；本模块是那两条
串的生成器——改形态先改这里，输出同步进 JSON，test_date_forms.py 的
一致性测试锁定两者，运行时零代码改动。

范围口径（2026-10-06 用户拍板，详见 Issue#52 与 #50 留痕）：
- 认：中文数字（〇系/十系/OCR 混淆零）、全角、混排、年月/月日缺省形态
  （含「份」）、区间首尾（命名组 seg_a/seg_b）、农历别称月（腊/冬/正）、
  出生语境（前缀/后缀，实体只含日期组 birth_date/birth_date2）。
- 不认：裸年份、相对日期词（同年/次月——替换反而破坏「同」关系）、
  农历日（初一/廿三）、斜杠/短横线日期（2022-10-05，观察项）。
"""

# ---- 槽位 ----

# 年：4 位。\d 覆盖半角/全角数字；另容忍 〇/零/○ 与 OCR 形近的 O/o/ο 等。
_YEAR_DIGITS = "〇零○OoΟοＯｏ一二三四五六七八九"
_YEAR = rf"(?:\d|[{_YEAR_DIGITS}]){{4}}"

_MONTH_AR = r"0?[1-9]|1[0-2]"
_DAY_AR = r"0?[1-9]|[12][0-9]|3[01]"
_MONTH_CJK = r"[一二三四五六七八九]|十[一二]?"
_DAY_CJK = r"三十[一]?|二十[一二三四五六七八九]?|十[一二三四五六七八九]?|[一二三四五六七八九]"


def _fullwidth(pattern: str) -> str:
    """槽位数字类翻全角版（OCR 全角日期不回退；\\d 不覆盖的字面类补齐）。"""
    return pattern.translate(str.maketrans("0123456789", "０１２３４５６７８９"))


_MONTH = rf"(?:{_MONTH_AR}|{_fullwidth(_MONTH_AR)}|{_MONTH_CJK})"
_DAY = rf"(?:{_DAY_AR}|{_fullwidth(_DAY_AR)}|{_DAY_CJK})"

# 组件间空隙：含跨行与全角空格（沿用既有 DATE 保证层的容忍口径）。
_GAP = r"\s*"

_FULL = rf"{_YEAR}{_GAP}年{_GAP}{_MONTH}{_GAP}月{_GAP}{_DAY}{_GAP}日"
_YEAR_MONTH = rf"{_YEAR}{_GAP}年{_GAP}{_MONTH}{_GAP}月(?:{_GAP}份)?"
_MONTH_DAY = rf"{_MONTH}{_GAP}月{_GAP}{_DAY}{_GAP}日"
_LUNAR_MONTH = r"[腊冬正]\s*月"

_DATE_FORMS = rf"(?:{_FULL}|{_YEAR_MONTH}|{_MONTH_DAY})"
# 区间尾段允许缺省到纯日/纯月（「至31日」「至3月」）。
_DATE_TAILS = rf"(?:{_FULL}|{_YEAR_MONTH}|{_MONTH_DAY}|{_DAY}{_GAP}日|{_MONTH}{_GAP}月)"

_RANGE_SEP = rf"{_GAP}[至到~～\-—－―]{_GAP}"

# 区间分支置首：同一起点下优先整段命中（左优先交替），首尾两组各出一个
# 实体；其余分支无命名组，整个 match 出一个实体（既有语义不变）。
DATE_FORMS_REGEX = (
    rf"(?:"
    rf"(?P<seg_a>{_DATE_FORMS}){_RANGE_SEP}(?P<seg_b>{_DATE_TAILS})"
    rf"|{_DATE_FORMS}"
    rf"|{_LUNAR_MONTH}"
    rf")"
)

# 出生语境：前缀（出生日期：/生于/生日…）或后缀（「……出生」），
# 两组同 named-group 机制下每个 match 只有一组参与，实体=日期部分，
# 语境词不入实体——保证「仅出生日期开」时不误框裸事件日期。
_BIRTH_LEAD = r"(?:出生(?:日期|时间|年月日?)?[于为是系在:：]*\s*|生日[于:：]*\s*|生于\s*)"
BIRTH_DATE_FORMS_REGEX = (
    rf"(?:{_BIRTH_LEAD}(?P<birth_date>{_FULL})|(?P<birth_date2>{_FULL}){_GAP}出生)"
)
