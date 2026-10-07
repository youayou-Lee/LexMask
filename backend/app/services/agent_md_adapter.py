"""Issue#75 适配器：MinerU content_list v2 → 清洁文本段。

规则依序（spec §4）：type 过滤 → seal 哨兵 → 表格剥行 → image 占位（Task 4 补 OCR）→
文本剥 Markdown/LaTeX → 保持 MinerU 阅读序。

形态归一化（终审 C1）：sidecar v2 ``content_list_v2`` 是外层按页的 list-of-lists
（Task 0 报告 §6c 实测），文本在 ``block["content"][f"{type}_content"][i]["content"]``、
块内无 page_idx；v1 则是扁平 ``{type,text,page_idx,...}``。``normalize_content_list``
把两种形态统一成扁平 v1 形态再喂 ``clean_segments``。
"""
import re
from html.parser import HTMLParser
from typing import Any

from app.services.agent_md_types import Seg

_DROP_TYPES = {"header", "footer", "page_number", "aside_text"}
# v2 块 type 名 → v1 名（Task 0 §6c 实测 paragraph/page_header；其余形态未观测，
# 不认识的保持原样由 clean_segments 走 unhandled 警告，不臆造）。
_V2_TYPE_TO_V1 = {"paragraph": "text", "page_header": "header", "page_footer": "footer"}
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


def normalize_content_list(raw: Any) -> list[dict]:
    """MinerU content_list v1/v2 双形态 → 统一扁平 v1 形态（终审 C1）。

    探测：首元素是 list → v2 嵌套（外层按页索引）；否则按 v1 扁平透传。
    v2 块文本取值路径 ``block["content"][f"{type}_content"][i]["content"]``；
    兼容 ``content`` 直接是纯字符串/部件列表的变体。页码 = 外层下标（v2 块内
    无 page_idx，块自带则不覆写）。seal/table/image 信息（sub_type / table_body /
    img_path）从 content 部件上浮到扁平块，供 clean_segments 保真处理。
    """
    if not isinstance(raw, list):
        return []
    if not raw or not isinstance(raw[0], list):
        return [dict(item) for item in raw if isinstance(item, dict)]
    flat: list[dict] = []
    for page_idx, blocks in enumerate(raw):
        if isinstance(blocks, dict):  # 防御：页槽位直接放了块
            flat.append(_v2_block_to_flat(blocks, page_idx))
            continue
        if not isinstance(blocks, list):
            continue
        for block in blocks:
            if isinstance(block, dict):
                flat.append(_v2_block_to_flat(block, page_idx))
    return flat


def _v2_block_to_flat(block: dict, page_idx: int) -> dict:
    """单个 v2 块 → 扁平 v1 形态字典（保序、保 seal/table/image 信息、type 映射 v1 名）。"""
    out = dict(block)
    btype = block.get("type")
    if btype in _V2_TYPE_TO_V1:
        out["type"] = _V2_TYPE_TO_V1[btype]
    out.setdefault("page_idx", page_idx)
    content = block.get("content")
    if isinstance(content, str):  # 变体：content 即纯文本
        out.setdefault("text", content)
        return out
    parts: list[str] = []
    sections: list[Any] = []
    if isinstance(content, dict):
        primary = content.get(f"{btype}_content") if btype else None
        if isinstance(primary, list):
            sections.append(primary)
        for key, val in content.items():  # 防御：其他 *_content 变体键
            if isinstance(val, list) and val is not primary:
                sections.append(val)
    elif isinstance(content, list):  # 变体：content 直接是部件列表
        sections.append(content)
    for section in sections:
        for item in section:
            if isinstance(item, str):
                parts.append(item)
                continue
            if not isinstance(item, dict):
                continue
            text = item.get("content")
            if isinstance(text, str) and text.strip():
                parts.append(text)
            for src_key in ("sub_type", "img_path", "table_body"):
                if item.get(src_key) and not out.get(src_key):
                    out[src_key] = item[src_key]
    if parts and not out.get("text"):
        out["text"] = "\n".join(parts)
    return out


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


def _find_image_bytes(images: dict[str, bytes], path: str) -> bytes | None:
    """images 键匹配：精确 → 后缀唯一（zip 前缀键 <stem>/<parse_method>/images/…）
    → basename 唯一 → 放弃（终审 M1：content_list img_path 与 zip 键前缀形态不保证一致）。"""
    if path in images:
        return images[path]
    if not path:
        return None
    suffix_hits = [k for k in images if k.endswith(path)]
    if len(suffix_hits) == 1:
        return images[suffix_hits[0]]
    base = path.rsplit("/", 1)[-1]
    base_hits = [k for k in images if k.rsplit("/", 1)[-1] == base]
    if len(base_hits) == 1:
        return images[base_hits[0]]
    return None


def enrich_image_blocks(
    segs: list[Seg],
    images: dict[str, bytes],
    ocr: Any,  # OCRService（测试注入 fake，生产代码不 import OCR 服务）
) -> tuple[list[Seg], list[str]]:
    """用 zip 自带 image 切图补 OCR（spec §4.4）。失败降级 [图片] 哨兵。"""
    warns: list[str] = []
    out: list[Seg] = []
    for seg in segs:
        if seg.source != "img_ocr":
            out.append(seg)
            continue
        path = seg.img_path
        try:
            image_bytes = _find_image_bytes(images, path)
            if not image_bytes:
                raise ValueError(f"image not in zip: {path}")
            items = ocr.extract_text_boxes(image_bytes)
            texts = [it.text.strip() for it in items if it.text and it.text.strip()]
        except Exception as exc:  # noqa: BLE001 —— 单图失败不拖垮整卷
            warns.append(f"image ocr failed: {path}: {exc}")
            out.append(Seg(text="[图片]", page_idx=seg.page_idx, source="sentinel"))
            continue
        if texts:
            out.append(Seg(text="\n".join(texts), page_idx=seg.page_idx, source="img_ocr"))
        else:
            warns.append(f"image ocr empty: {path}")
            out.append(Seg(text="[图片]", page_idx=seg.page_idx, source="sentinel"))
    return out, warns
