# Issue#56 M1 Task 2 —— 归一化与双面 span 映射 normalize.py 的离线单测。
# 纯字符串变换：零网络、零 IO、零真实案卷数据（全部合成占位符）。
# 评审点名高危项：往返映射（norm→orig→norm）对全部区间（含空区间、端点）恒等。
# 与计划底稿的两处脚手架差异（实现 API 不变，见 task-2-report）：
#   1) 底稿只给了两个样例函数，这里补 import/REPO 接线使其可独立收集；
#   2) 底稿 test_roundtrip_with_drift 的 startswith 分支恒 False（全角串对 ASCII "1"），
#      由 or 右支承担断言，按原文保留未改。
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "eval"))
from gt import normalize  # noqa: E402

RAWS = [
    "委托人：钱某，身份证号：１１０１２２…。## 附注 <table>|a|b|</table>",
    "钱 明 涛，男",
    "# 甲\n<table>|1|2|</table>\n<!-- 丙丁 -->戊<br>己",
    "a<table>b",
    "<!--只注释",
    "ＡＢＣ１２３",
    "",
]


# ---- 计划底稿样例（逐字保留） ------------------------------------------------

def test_roundtrip_with_drift():
    raw = "委托人：钱某，身份证号：１１０１２２…。## 附注 <table>|a|b|</table>"
    fm = normalize.FaceMap.from_raw(raw)
    n = fm.norm
    i = n.find("110122")
    o0, o1 = fm.to_original(i, i + 6)
    assert raw[o0:o1].startswith("１１０１２２".replace("１", "1")[:1]) or raw[o0:o1] == "１１０１２２"[:6]
    assert fm.to_normalized(o0, o1) == (i, i + 6)


def test_whitespace_squash_mapping():
    raw = "钱 明 涛，男"
    fm = normalize.FaceMap.from_raw(raw)
    i = fm.norm.find("钱明涛")
    assert fm.to_original(i, i + 3) == (0, 5)


# ---- 归一化规则 --------------------------------------------------------------

def test_fullwidth_to_halfwidth():
    # ASCII 区 U+FF01–FF5E 平移到 0x21–0x5E；全角空格 U+3000 归入空白剥离
    assert normalize.normalize_text("Ａｂｃ１２３：！？") == "Abc123:!?"
    assert normalize.normalize_text("钱\u3000明") == "钱明"
    # 全角区之外的字符原样保留
    assert normalize.normalize_text("…。") == "…。"


def test_whitespace_removed_everywhere():
    assert normalize.normalize_text("a b\tc\nd\u3000e") == "abcde"


def test_markdown_stripped():
    raw = "# 甲\n<table>|1|2|</table>\n<!-- 丙丁 -->戊<br>己"
    assert normalize.normalize_text(raw) == "甲12戊己"


def test_heading_only_at_line_start():
    assert normalize.normalize_text("## 甲\n### 乙\n丙#丁") == "甲乙丙#丁"
    # 规格：仅行首 #+ 剥离；行中 # 存活
    assert normalize.normalize_text("甲##乙") == "甲##乙"


def test_html_comment_stripped():
    fm = normalize.FaceMap.from_raw("甲<!--乙丙-->丁")
    assert fm.norm == "甲丁"
    # 注释内容已剥离不进 norm；落回原文时夹在两端存活位之间（半开区间语义）
    assert fm.to_original(0, 2) == (0, 11)


def test_unterminated_comment_consumes_to_end():
    assert normalize.normalize_text("甲<!--乙丙") == "甲"


def test_normalize_text_matches_facemap_norm():
    for raw in RAWS:
        assert normalize.normalize_text(raw) == normalize.FaceMap.from_raw(raw).norm


# ---- 双面映射 ----------------------------------------------------------------

def test_to_original_covers_interior_stripped_only():
    raw = "甲<table>乙"  # 甲(0) <table>(1-7) 乙(8)，len=9
    fm = normalize.FaceMap.from_raw(raw)
    assert fm.norm == "甲乙"
    # 跨存活区间的原文 span 包含中间被剥离的标记
    o0, o1 = fm.to_original(0, 2)
    assert (o0, o1) == (0, 9) and raw[o0:o1] == "甲<table>乙"
    # 单字符 span 不捎带前导剥离区
    o0, o1 = fm.to_original(1, 2)
    assert (o0, o1) == (8, 9) and raw[o0:o1] == "乙"


def test_to_normalized_maps_to_nearest_surviving():
    fm = normalize.FaceMap.from_raw("a<table>b")
    # 区间只覆盖被剥离的标记 → 收缩为零宽区间（最近存活位）
    assert fm.to_normalized(1, 7) == (1, 1)
    # 端点落进剥离区 → 收缩到存活端
    assert fm.to_normalized(0, 9) == (0, 2)
    assert fm.to_normalized(2, 8) == (1, 1)


def test_empty_and_edge_spans():
    fm = normalize.FaceMap.from_raw("甲<table>乙")  # m = [0, 8]，len(raw)=9
    assert fm.to_original(0, 0) == (0, 0)     # 串首空区间
    assert fm.to_original(1, 1) == (8, 8)     # 空区间 → 下一个存活位（乙）之前
    assert fm.to_original(2, 2) == (9, 9)     # norm 末尾空区间 = 原文末尾
    assert fm.to_normalized(0, 0) == (0, 0)
    assert fm.to_normalized(9, 9) == (2, 2)   # 原文末尾空区间
    assert fm.to_normalized(8, 8) == (1, 1)   # 乙 位置前的空区间 → norm 中乙之前
    assert fm.to_normalized(4, 4) == (1, 1)   # 剥离区内空区间 → 最近存活边界


def test_empty_raw():
    fm = normalize.FaceMap.from_raw("")
    assert fm.norm == "" and fm.raw == ""
    assert fm.to_original(0, 0) == (0, 0)
    assert fm.to_normalized(0, 0) == (0, 0)


def test_roundtrip_all_spans():
    # 评审点名高危：任意 norm 区间（含空区间）orig→回映射必须恒等，且落在原文界内
    for raw in RAWS:
        fm = normalize.FaceMap.from_raw(raw)
        m = len(fm.norm)
        for n0 in range(m + 1):
            for n1 in range(n0, m + 1):
                o0, o1 = fm.to_original(n0, n1)
                assert 0 <= o0 <= o1 <= len(raw), (raw, n0, n1, o0, o1)
                assert fm.to_normalized(o0, o1) == (n0, n1), (raw, n0, n1)
