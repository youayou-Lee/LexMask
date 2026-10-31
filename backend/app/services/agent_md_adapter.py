"""Issue#75 适配器：MinerU content_list v2 → 清洁文本段。

规则依序（spec §4）：type 过滤 → seal 哨兵 → 表格剥行 → image 占位（Task 4 补 OCR）→
文本剥 Markdown/LaTeX → 保持 MinerU 阅读序。

形态归一化（终审 C1）：sidecar v2 ``content_list_v2`` 是外层按页的 list-of-lists
（Task 0 报告 §6c 实测），文本在 ``block["content"][f"{type}_content"][i]["content"]``、
块内无 page_idx；v1 则是扁平 ``{type,text,page_idx,...}``。``normalize_content_list``
把两种形态统一成扁平 v1 形态再喂 ``clean_segments``。
"""
import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any

from app.services.agent_md_types import Seg

_DROP_TYPES = {"header", "footer", "page_number", "aside_text"}
# v2 块 type 名 → v1 名（Task 0 §6c 实测 paragraph/page_header；title 为 Issue#212
# 实测补充——真实案卷的机构名/表名就在 title_content 里，此前当未知块型整块丢弃）。
_V2_TYPE_TO_V1 = {
    "paragraph": "text",
    "page_header": "header",
    "page_footer": "footer",
    "title": "text",
}


@dataclass
class CleanStats:
    """Issue#212：丢弃必须可观测——按原因结构化计数（不只靠 warnings 文本）。

    管线把它透出到任务 `status.dropped_blocks`，使"解析出正文但表全空"这类
    静默失败可被前端与运维按码取数（键恒存在，0 也显式返回）。
    """

    empty_table_blocks: int = 0
    dropped_block_types: dict[str, int] = field(default_factory=dict)
    # 门⑥ I1：只给总数无法安排人工复核页；页码清单单独留（有上限，防 80 页×N 撑爆）
    empty_table_pages: list[int] = field(default_factory=list)
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
        for _key, val in content.items():  # 防御：其他 *_content 变体键
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
    if isinstance(content, dict):
        # Issue#212：MinerU 3.4.5 v2 表格正文在 content["html"]（不是 table_body），
        # 表名/脚注在 content["table_caption"/"table_footnote"]（部件数组）。只在
        # table 块上上浮，避免把任意块里的 html 当表格正文。
        if btype == "table":
            if not out.get("table_body") and isinstance(content.get("html"), str):
                out["table_body"] = content["html"]
            for extra_key in ("table_caption", "table_footnote"):
                if not out.get(extra_key):
                    extra = _parts_text(content.get(extra_key))
                    if extra:
                        out[extra_key] = extra
    if parts and not out.get("text"):
        out["text"] = "\n".join(parts)
    return out


def _parts_text(raw: Any) -> str:
    """v2 部件数组（[{"type":"text","content":...}]）或纯字符串 → 一行文本。"""
    if isinstance(raw, str):
        return raw.strip()
    if not isinstance(raw, list):
        return ""
    out: list[str] = []
    for item in raw:
        if isinstance(item, str):
            if item.strip():
                out.append(item.strip())
        elif isinstance(item, dict):
            text = item.get("content")
            if isinstance(text, str) and text.strip():
                out.append(text.strip())
    return " | ".join(out)


def clean_segments(
    content_list: list[dict], stats: CleanStats | None = None
) -> tuple[list[Seg], list[str]]:
    """item 列表 → 清洁文本段 + 告警。`stats` 传入时按原因累计丢弃计数（Issue#212）。"""
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
                lines = [body]
                for extra_key in ("table_caption", "table_footnote"):
                    extra = (item.get(extra_key) or "").strip()
                    if extra:
                        lines.append(extra)
                segs.append(Seg(text="\n".join(lines), page_idx=page_idx, source="table"))
            else:
                # Issue#212：空表格块此前既不产段也不告警（静默丢表 = 事故本体）
                if stats is not None:
                    stats.empty_table_blocks += 1
                    if len(stats.empty_table_pages) < 100:
                        stats.empty_table_pages.append(page_idx)
                warns.append(f"empty table block: page={page_idx}")
        elif btype == "image":
            path = item.get("img_path", "")
            warns.append(f"image block pending ocr: {path}")
            segs.append(Seg(text="", page_idx=page_idx, source="img_ocr", img_path=path))
        elif btype == "text":
            body = strip_inline(item.get("text", ""))
            if body:
                segs.append(Seg(text=body, page_idx=page_idx, source="text"))
        elif btype is not None:
            if stats is not None:
                stats.dropped_block_types[str(btype)] = (
                    stats.dropped_block_types.get(str(btype), 0) + 1)
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


# ---- Issue#219：MinerU 4.x docvortex.middle schema 分流与归一 ----

# 4.x 文档级形状判定值（上游 schema 字段全等，不做前缀/包含匹配——形似值
# （如 "docvortex.middle.v3"）必须落到 unknown 走显式失败，见 AC1 负例）
_DOCVORTEX_SCHEMA = "docvortex.middle"
# 4.x 块 type → v1 名：paragraph_title 同 v2 title→text 口径（level 丢弃）
_DOCVORTEX_TEXT_BLOCK_TYPES = {"text", "paragraph_title"}


def detect_schema(raw: Any) -> str:
    """产物形状判定（Issue#219 分流的判据本体，客户端拆包与管线选择共用）。

    - dict 且 ``schema`` 严格等于 docvortex.middle → "v4"；
    - list 且首元素是 list → "v2"（外层按页嵌套）；
    - list（含空表）→ "v1"（扁平）；
    - 其余（非 list 非 docvortex 的 dict、标量、None）→ "unknown"。
    """
    if isinstance(raw, dict) and raw.get("schema") == _DOCVORTEX_SCHEMA:
        return "v4"
    if isinstance(raw, list):
        if raw and isinstance(raw[0], list):
            return "v2"
        return "v1"
    return "unknown"


def normalize_docvortex(docs: list[dict]) -> tuple[list[dict], list[str]]:
    """4.x docvortex.middle 文档列表 → (扁平 v1 形态列表, 告警)，喂 clean_segments。

    映射对照表见设计文档 §2（workspace acceptance/2026-11-01-issue219）：
    text/paragraph_title→text（text 部件 "\n" 拼接，同 v2 口径）；table 的
    table_body 部件上浮、text 部件作 table_caption（实测未出现，防御映射）；
    image 不带 img_path（4.x zip 无 images/，enrich 降级 [图片] 哨兵）；
    未知块/部件 type 透传并告警（不静默）。

    去重（R1/C1）：doclib 双文件是 4.x 常态（探针两份逐字节相同），键取
    ``pages`` 数组 + ``schema_version`` 的 canonical 序列化——metadata 带时间戳
    等差异但 pages 相同时必须去重成功；去重后 pages 仍不同 ⇒ 告警
    ``mineru4_multiple_docs``（调用方 fail-closed，盲拼会把导出 md 全文翻倍）。
    """
    warns: list[str] = []
    unique: list[dict] = []
    seen_pages: set[str] = set()
    for doc in docs or []:
        if not isinstance(doc, dict):
            continue
        key = json.dumps(
            {"schema_version": doc.get("schema_version"), "pages": doc.get("pages")},
            sort_keys=True, ensure_ascii=False,
        )
        if key in seen_pages:
            continue
        seen_pages.add(key)
        unique.append(doc)
    if len(unique) > 1:
        warns.append(f"mineru4_multiple_docs: {len(unique)} 份 docvortex 文档 pages 不同")
    flat: list[dict] = []
    for doc in unique:
        pages = doc.get("pages")
        if not isinstance(pages, list):
            continue
        for page in pages:
            if not isinstance(page, dict):
                continue
            page_idx = page.get("page_idx")
            page_idx = int(page_idx) if isinstance(page_idx, (int, float)) else 0
            blocks = page.get("blocks")
            if not isinstance(blocks, list):
                continue
            for block in blocks:
                if isinstance(block, dict):
                    flat.append(_docvortex_block_to_flat(block, page_idx, warns))
    return flat, warns


def _docvortex_block_to_flat(block: dict, page_idx: int, warns: list[str]) -> dict:
    """单个 4.x 块 → 扁平 v1 形态字典（保序、page_idx setdefault 不覆写块自带值）。"""
    out: dict = {"page_idx": page_idx}
    if block.get("page_idx") is not None:
        out["page_idx"] = block["page_idx"]
    btype = block.get("type")
    out["type"] = str(btype) if btype is not None else "unknown"
    texts: list[str] = []
    table_body: str | None = None
    content = block.get("content")
    if isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                continue
            ptype = part.get("type")
            pval = part.get("content")
            if ptype == "text":
                if isinstance(pval, str) and pval.strip():
                    texts.append(pval)
            elif ptype == "table_body":
                if isinstance(pval, str) and pval.strip():
                    table_body = pval
            elif ptype == "image_body":
                pass  # 4.x 图块正文恒空（实测），语义由块 type=image 承载
            elif ptype is not None:
                warns.append(f"unhandled docvortex part type: {ptype}")
    if btype in _DOCVORTEX_TEXT_BLOCK_TYPES:
        out["type"] = "text"
        if texts:
            out["text"] = "\n".join(texts)
    elif btype == "table":
        if table_body:
            out["table_body"] = table_body
        if texts:  # 防御映射：实测 table 块只有 table_body 部件；text 部件按表名/脚注处理
            out["table_caption"] = "\n".join(texts)
    # 其余 type（image/未知）原样透传：clean_segments 按 v1 语义处理并计数告警
    return out


def markdown_body_chars(md_text: str) -> int:
    """zip 内官方 .md 的正文字符参照口径（I2 docvortex_shortfall 守卫的锚）。

    剥 markdown 标记：表格 HTML（表格字符由 v4 table_body 侧计入）、图片/链接
    语法、标题井号、行内强调符；页注释 ``<!-- -->`` 剥除；空白不计数。
    探针锚点：canary-issue219 三页样本 v4=429 / md=443 → ratio 0.9684。
    """
    text = md_text or ""
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = re.sub(r"<table>.*?</table>", "", text, flags=re.S)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.M)
    text = re.sub(r"[*`_>|]", "", text)
    return len(re.sub(r"\s", "", text))
