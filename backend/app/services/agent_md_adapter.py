"""Issue#75 适配器：MinerU content_list v2 → 清洁文本段。

规则依序（spec §4）：type 过滤 → seal 哨兵 → 表格剥行 → image 占位（Task 4 补 OCR）→
文本剥 Markdown/LaTeX → 保持 MinerU 阅读序。
"""
import re
from html.parser import HTMLParser

from app.services.agent_md_types import Seg

_DROP_TYPES = {"header", "footer", "page_number", "aside_text"}
_LATEX_DISPLAY_SQ = re.compile(r"\\\[(.*?)\\\]")
_LATEX_DISPLAY_RQ = re.compile(r"\\\((.*?)\\\)")
_LATEX_DOLLAR_PAIR = re.compile(r"\$([^$]*)\$")
_INLINE_MD = re.compile(r"(\*\*|\*|`)")
_LEADING_MD = re.compile(r"^\s{0,3}#{1,6}\s*")
_ESCAPES = re.compile(r"\\([;:!,.])")


class _TableText(HTMLParser):
    """把 table_body HTML 剥成「单元格按行拼接」的纯文本，不信结构语义（spec §6.3）。"""

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th") and self._cell is not None:
            self._row.append("".join(self._cell).strip())
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)


def table_to_text(table_body: str) -> str:
    p = _TableText()
    p.feed(table_body or "")
    return "\n".join(" | ".join(cells) for cells in p.rows if cells)


def strip_inline(text: str) -> str:
    out = _LEADING_MD.sub("", text or "")
    out = _LATEX_DISPLAY_SQ.sub(r"\1", out)
    out = _LATEX_DISPLAY_RQ.sub(r"\1", out)
    out = _LATEX_DOLLAR_PAIR.sub(r"\1", out)
    out = _INLINE_MD.sub("", out)
    out = _ESCAPES.sub("", out)
    return out.strip()


def clean_segments(content_list: list[dict]) -> tuple[list[Seg], list[str]]:
    segs: list[Seg] = []
    warns: list[str] = []
    for item in content_list or []:
        btype = item.get("type")
        if btype in _DROP_TYPES:
            continue
        page_idx = int(item.get("page_idx") or 0)
        if btype == "image" and item.get("sub_type") == "seal":
            segs.append(Seg(text="[公章]", page_idx=page_idx, source="sentinel"))
        elif btype == "table":
            body = table_to_text(item.get("table_body", ""))
            if body:
                segs.append(Seg(text=body, page_idx=page_idx, source="table"))
        elif btype == "image":
            path = item.get("img_path", "")
            warns.append(f"image block pending ocr: {path}")
            segs.append(Seg(text="", page_idx=page_idx, source="img_ocr", img_path=path))
        elif btype == "text":
            body = strip_inline(item.get("text", ""))
            if body:
                segs.append(Seg(text=body, page_idx=page_idx, source="text"))
        elif btype is not None:
            warns.append(f"unhandled block type: {btype}")
    return segs, warns
