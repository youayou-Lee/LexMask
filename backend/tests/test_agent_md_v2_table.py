"""Issue#212：MinerU 3.4.5 v2 表格正文（content.html）/ title 映射 / 丢弃可观测。

夹具 `tests/fixtures/agent_md/*.json` 全部为脱敏合成内容（形状取自 2026-10-29 真实
80 页扫描件的 v1/v2 产物实测：79 table / 8 title / 80 page_number）。
判据用**字面 golden**（不由 v1 分支计算），确保"改坏 v1 去对齐 v2"不能蒙过。
"""
import json
from pathlib import Path

from app.services.agent_md_adapter import CleanStats, clean_segments, normalize_content_list

_FIXTURES = Path(__file__).parent / "fixtures" / "agent_md"

# v1 路径在未修代码上的实测输出（BASE=686c4c0，逐字节抄录）。v1 table_body 分支零改动。
GOLDEN_SEGS = [
    ("text", 0, "某市某区人民法院财产查询反馈信息表"),
    ("table", 0, "案号：（2026）某0102执恢100号 | 查询日期：2026-10-01\n"
                 "户名 | 张某某 | 证件号码\n"
                 "账号 | 6222020200012345678 | 余额\n"
                 "开户行 | 某银行某支行 | 134.37"),
    ("text", 0, "上述财产查询结果由协执单位于 2026 年 10 月 1 日反馈。"),
    ("table", 1, "序号 | 开户行 | 账号 | 余额（元）\n"
                 "1 | 某银行某支行 | 6222020200098765432 | 0.00\n"
                 "2 | 某银行某分行 | 6217000011112222333 | 52.10"),
]
GOLDEN_CHARS = 258


def _load(name: str):
    return json.loads((_FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def _clean(raw):
    stats = CleanStats()
    segs, warns = clean_segments(normalize_content_list(raw), stats=stats)
    return segs, warns, stats


# ---- AC0/AC1：v2 表格进正文 + 字面 golden ----


def test_v2_table_html_enters_body_golden():
    """v2 表格正文在 content["html"]：修复后必须与 v1 路径的字面 golden 逐段相同。

    未修代码实测：segs=1 / chars=33（两个表与标题块全丢）⇒ 本用例必红。
    """
    segs, warns, stats = _clean(_load("v2_table_scan"))

    assert [(s.source, s.page_idx, s.text) for s in segs] == GOLDEN_SEGS
    assert sum(len(s.text) for s in segs) == GOLDEN_CHARS
    assert stats.empty_table_blocks == 0
    assert stats.dropped_block_types == {}


def test_v2_title_block_maps_to_text_and_no_unhandled_warn():
    """v2 title_content 不得再走 unhandled（真实案卷里它装着机构名）。"""
    segs, warns, stats = _clean(_load("v2_table_scan"))

    assert any("某市某区人民法院财产查询反馈信息表" in s.text for s in segs)
    assert not any("unhandled block type: title" in w for w in warns)
    assert "title" not in stats.dropped_block_types


# ---- AC2：caption / footnote 上浮 ----


def test_v2_table_caption_and_footnote_appended_after_cells():
    """Issue 修复要求 1：table_caption / table_footnote 一并进正文，且在单元格文本之后。"""
    segs, _, _ = _clean(_load("v2_table_caption"))

    assert len(segs) == 1
    body = segs[0].text
    cells_end = body.index("6222020200012345678")
    caption_at = body.index("表1 财产查询结果")
    footnote_at = body.index("数据来源：协执单位")
    assert cells_end < caption_at < footnote_at


# ---- AC4：丢弃可观测（结构化计数，键恒存在）----


def test_empty_table_block_counted_and_warned():
    """空表格块：必须计入 stats 且给页码可读 warn（未修=完全静默）。"""
    nested = [[{
        "type": "table",
        "content": {"image_source": {"path": "images/x.jpg"},
                    "table_caption": [], "table_footnote": []},
        "bbox": [0, 0, 10, 10],
    }]]
    segs, warns, stats = _clean(nested)

    assert segs == []
    assert stats.empty_table_blocks == 1
    assert any("empty table block: page=0" in w for w in warns)


def test_dropped_block_types_counted_by_type():
    """未知块型：按类型聚合计数（可用于前端/日志按码取数）。"""
    nested = [[
        {"type": "equation", "content": {"equation_content": [{"type": "text", "content": "E=mc^2"}]},
         "bbox": [0, 0, 10, 10]},
        {"type": "equation", "content": {"equation_content": [{"type": "text", "content": "a+b"}]},
         "bbox": [0, 10, 10, 20]},
    ]]
    _, warns, stats = _clean(nested)

    assert stats.dropped_block_types == {"equation": 2}
    assert sum("unhandled block type: equation" in w for w in warns) == 2


def _shape_80_pages(*, with_html: bool) -> list[list[dict]]:
    """按真实 80 页产物形状合成：79 个表（分布 80 页内）+ 8 个 title + 80 个 page_number。"""

    def table(i: int) -> dict:
        content: dict = {"image_source": {"path": f"images/t{i}.jpg"},
                         "table_caption": [], "table_footnote": []}
        if with_html:
            content["html"] = (f"<table><tr><td>序号</td><td>账号</td></tr>"
                               f"<tr><td>{i}</td><td>6222020200{i:07d}</td></tr></table>")
        return {"type": "table", "content": content, "bbox": [0, 0, 100, 100]}

    pages: list[list[dict]] = [[] for _ in range(80)]
    for i in range(79):
        pages[i].append(table(i))
    for i in range(8):
        pages[i * 10].append({
            "type": "title",
            "content": {"title_content": [{"type": "text", "content": f"某表 第{i + 1}张"}]},
            "bbox": [0, 0, 100, 20],
        })
    for i in range(80):
        pages[i].append({
            "type": "page_number",
            "content": {"page_number_content": [{"type": "text", "content": f"第{i + 1}页"}]},
            "bbox": [0, 0, 100, 10],
        })
    return pages


def test_80_page_shape_all_tables_parsed():
    """真实形状（79 表/8 title/80 页码）全量正文：空表 0、无丢弃、87 段全部有正文。"""
    segs, warns, stats = _clean(_shape_80_pages(with_html=True))

    assert stats.empty_table_blocks == 0
    assert stats.dropped_block_types == {}
    assert len(segs) == 79 + 8
    assert sum(len(s.text) for s in segs) > 79 * 20
    assert not any("empty table block" in w for w in warns)


def test_80_page_shape_without_html_reports_all_tables_empty():
    """同形状但表格缺 html（= 事故形态）：必须显式报 79 个空表，不得静默。"""
    segs, warns, stats = _clean(_shape_80_pages(with_html=False))

    assert stats.empty_table_blocks == 79
    assert len([w for w in warns if "empty table block" in w]) == 79
    assert len(segs) == 8  # 只剩 title（已映射为 text）
