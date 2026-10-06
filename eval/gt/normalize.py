"""归一化与双面 span 映射（Issue#56 M1 / Task 2）。

GT 管线的归一化层：后续实体/比对阶段只面向归一化文本（norm）做检测，
命中区间经 FaceMap 双面映射回原始转录文本（raw）定位。

规则（与计划底稿一致）：
- 去全部空白（含全角空格 U+3000、换行、制表符）；
- 全角→半角：ASCII 可见区 U+FF01–FF5E 平移 0xFEE0 到 0x21–0x5E；
- 剥离 Markdown 标记：行首 ``#+`` 标题、``<table>``/``</table>``/``<br>``
  内联标签、``|`` 单元格竖线、HTML 注释 ``<!-- ... -->``（未闭合则吞到串尾）；
  行首判定为「行的第一个字符」（前导空白即失去行首资格），行中 ``#`` 存活。

映射语义：
- 存活字符逐一记录 ``map[i] = 原文下标``（严格递增）；被剥离的字符不产生映射位；
- ``to_original(n0, n1)``      → 原文半开区间，覆盖两端存活位之间的全部字符
  （含中间被剥离的标记，不含区间外的）；空区间落在下一个存活位之前；
- ``to_normalized(o0, o1)``    → 存活位计数映射，端点收缩到最近存活位；
  区间内无存活字符时收缩为零宽区间；
- 往返恒等：对全部 0 ≤ n0 ≤ n1 ≤ len(norm)，
  ``to_normalized(to_original(n0, n1)) == (n0, n1)``（单测全覆盖）。
"""
from __future__ import annotations

from bisect import bisect_left

# 全角→半角：U+FF01–FF5E → 0x21–0x5E（差 0xFEE0）；U+3000 全角空格走空白剥离
_FW_BEGIN = 0xFF01
_FW_END = 0xFF5E
_FW_OFFSET = 0xFEE0

# 剥离的内联标签（字面匹配，不剥 <tr> 等未列入规格的标记）
_STRIP_TAGS = ("<table>", "</table>", "<br>")
_COMMENT_BEGIN = "<!--"
_COMMENT_END = "-->"


def _match_tag(raw: str, i: int) -> str:
    """返回在 raw[i:] 处命中的剥离标签，未命中返回空串。"""
    for tag in _STRIP_TAGS:
        if raw.startswith(tag, i):
            return tag
    return ""


def _scan(raw: str) -> tuple[str, list[int]]:
    """逐字符扫描：返回 (归一化文本, norm→orig 映射表)。

    映射表第 i 项 = norm 第 i 个存活字符在 raw 中的原始下标；
    被剥离的字符（Markdown 标记、空白）不产生映射位。
    """
    out: list[str] = []
    mapping: list[int] = []
    at_line_start = True
    i, n = 0, len(raw)
    while i < n:
        ch = raw[i]
        if ch == "\n":
            i += 1
            at_line_start = True
            continue
        if at_line_start and ch == "#":  # 行首 ATX 标题：剥掉整串 '#'
            while i < n and raw[i] == "#":
                i += 1
            at_line_start = False
            continue
        if ch == "<" and raw.startswith(_COMMENT_BEGIN, i):
            end = raw.find(_COMMENT_END, i + len(_COMMENT_BEGIN))
            i = n if end < 0 else end + len(_COMMENT_END)
            at_line_start = False
            continue
        if ch == "<":
            tag = _match_tag(raw, i)
            if tag:
                i += len(tag)
                at_line_start = False
                continue
        at_line_start = False
        if ch == "|" or ch.isspace():  # 单元格竖线 + 全部空白（含 U+3000）：剥离
            i += 1
            continue
        if _FW_BEGIN <= ord(ch) <= _FW_END:
            out.append(chr(ord(ch) - _FW_OFFSET))
        else:
            out.append(ch)
        mapping.append(i)
        i += 1
    return "".join(out), mapping


def normalize_text(raw: str) -> str:
    """归一化：去全部空白、全角→半角、剥离 Markdown 标记。"""
    return _scan(raw)[0]


class FaceMap:
    """norm↔orig 双面偏移映射。

    属性：``norm`` 归一化文本（normalize_text 同款输出）、``raw`` 原始文本。
    区间一律半开 [start, end)，端点允许落在任意位置（含空区间、串尾越界端点），
    越界/倒序端点夹取到合法范围。
    """

    def __init__(self, raw: str, norm: str, mapping: list[int]):
        self.raw = raw
        self.norm = norm
        self._mapping = mapping  # _mapping[i] = norm 第 i 字符的 orig 下标（严格递增）

    @classmethod
    def from_raw(cls, raw: str) -> "FaceMap":
        """扫描 raw 构建 norm→orig 偏移映射。"""
        norm, mapping = _scan(raw)
        return cls(raw, norm, mapping)

    def to_original(self, n0: int, n1: int) -> tuple[int, int]:
        """norm 半开区间 [n0, n1) → raw 半开区间。

        非空区间覆盖两端存活位之间的全部原文（含中间被剥离的标记）；
        空区间映射为零宽区间，落在下一个存活位之前（norm 末尾则在其后）。
        """
        m = self._mapping
        n0 = min(max(n0, 0), len(m))
        n1 = min(max(n1, n0), len(m))
        if n0 == n1:
            if n0 < len(m):
                p = m[n0]
            elif m:
                p = m[-1] + 1
            else:
                p = 0
            return (p, p)
        return (m[n0], m[n1 - 1] + 1)

    def to_normalized(self, o0: int, o1: int) -> tuple[int, int]:
        """raw 半开区间 [o0, o1) → norm 半开区间。

        端点收缩到最近存活位：n = [0, o) 内存活字符数；
        区间内无存活字符时结果为零宽区间。
        """
        m = self._mapping
        o0 = min(max(o0, 0), len(self.raw))
        o1 = min(max(o1, o0), len(self.raw))
        return (bisect_left(m, o0), bisect_left(m, o1))
