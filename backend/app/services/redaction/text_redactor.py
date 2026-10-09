"""
文本匿名化模块
处理 DOCX、PDF、TXT 文档的文本替换逻辑
"""
import asyncio
import json
import logging
import os
import shutil
import subprocess
import tempfile
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import fitz
from docx import Document
from docx.opc.constants import CONTENT_TYPE as CT
from lxml import etree

from app.core.config import settings
from app.models.schemas import Entity
from app.services.file_parser import open_pdf_checked
from app.services.redaction.replacement_strategy import RedactionContext

logger = logging.getLogger(__name__)

WORD_XML_NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}

# PyMuPDF save garbage-collection level (aggressive cleanup of unused objects)
PDF_SAVE_GARBAGE_LEVEL = 4
# Largest/default font size (pt) for inline PDF replacement labels
PDF_LABEL_FONT_SIZE_MAX = 10.0
# Smallest preferred font size (pt) before width-based shrinking kicks in
PDF_LABEL_FONT_SIZE_MIN = 6.0
# Absolute floor font size (pt) after width-based shrinking
PDF_LABEL_FONT_SIZE_FLOOR = 5.0
# Fraction of rect height used as the base label font size
PDF_LABEL_HEIGHT_FONT_RATIO = 0.78

# 空白归一匹配时两侧都压掉的字符（Issue #84：PyMuPDF 提取的实体文本带
# 空格伪影如 `2018 年3 月26 日`，与 pdf2docx 产出的 docx 空格位置不一致）
SQUEEZE_CHARS = " \t\n\r\u00a0\u3000\u200b"


class TextRedactorMixin:
    """
    文本匿名化方法集合
    设计为 mixin，由 Redactor 类继承使用
    """

    async def _redact_docx(
        self,
        input_path: str,
        output_path: str,
        entities: list[Entity],
        context: RedactionContext,
    ) -> int:
        """Word 文档匿名化"""
        doc = Document(input_path)
        redacted_count = 0

        # 构建替换映射
        replacements = {}
        for entity in entities:
            if entity.text not in replacements:
                replacements[entity.text] = context.get_replacement(entity)

        trace_enabled = self._is_docx_font_trace_enabled()
        trace_path = self._get_docx_font_trace_path() if trace_enabled else None
        if trace_enabled and trace_path:
            self._init_docx_font_trace(trace_path, input_path, output_path, replacements)

        # 存元素引用而非 id()：lxml 代理对象无引用时会被回收，同节点再遍历
        # 会分配新代理导致 id 对不上；保引用可保证 pass2 拿到同一代理对象
        processed_elements: set = set()
        for para_idx, para in enumerate(self._iter_all_paragraphs(doc)):
            # 按出现位置分流：完全落在直接 run 区域的键走 run 级替换（保格式），
            # 含嵌套节点（超链接/修订插入/smartTag/delText/instrText）字符或
            # 跨「直接 run ↔ 嵌套节点」边界的键走整段 XML 替换。两趟键集
            # 不相交，避免 run 级写入的替换词被第二趟当原文再改写。
            run_keys, union_keys = self._split_paragraph_replacement_keys(para, replacements)
            if run_keys and union_keys:
                # 混合段落整体走单一 XML 趟：run 趟先写入的替换值若与 union
                # 键的原文相同（词池回灌），union 趟会在跑完 run 趟后的文本上
                # 把它再改写一次（错误化名）。单趟非重叠匹配无此问题
                redacted_count += self._replace_in_docx_xml_paragraph(
                    para._p, {**run_keys, **union_keys}
                )
            elif run_keys:
                redacted_count += self._replace_in_paragraph(
                    para,
                    run_keys,
                    para_idx=para_idx,
                    trace_enabled=trace_enabled,
                    trace_path=trace_path,
                )
            elif union_keys:
                redacted_count += self._replace_in_docx_xml_paragraph(para._p, union_keys)
            processed_elements.add(para._p)

        redacted_count += self._replace_in_docx_xml_parts(
            doc, replacements, skip_elements=processed_elements
        )
        doc.save(output_path)
        return redacted_count

    @staticmethod
    def _squeeze_with_index(text: str) -> tuple[str, list[int]]:
        """压掉空白后的文本 + squeezed 偏移→原文本偏移映射。"""
        chars: list[str] = []
        index_map: list[int] = []
        for i, ch in enumerate(text):
            if ch in SQUEEZE_CHARS:
                continue
            chars.append(ch)
            index_map.append(i)
        return "".join(chars), index_map

    @classmethod
    def _find_replacement_matches(
        cls, full_text: str, replacements: dict[str, str]
    ) -> list[tuple[int, int, str]]:
        """空白归一匹配（Issue #84）：实体键与文档文本两侧压掉空白后定位，
        再映射回真实偏移区间。优先长匹配、过滤重叠，语义与原精确匹配一致，
        仅匹配口径放宽——空格伪影不再导致漏替。"""
        squeezed, idx = cls._squeeze_with_index(full_text)
        if not squeezed:
            return []
        matches: list[tuple[int, int, str]] = []
        for old_text, new_text in replacements.items():
            sq = "".join(ch for ch in old_text if ch not in SQUEEZE_CHARS)
            if not sq:
                continue
            start = 0
            while True:
                pos = squeezed.find(sq, start)
                if pos < 0:
                    break
                matches.append((idx[pos], idx[pos + len(sq) - 1] + 1, new_text))
                start = pos + len(sq)
        matches.sort(key=lambda x: (x[0], -(x[1] - x[0])))
        filtered: list[tuple[int, int, str]] = []
        last_end = -1
        for start, end, replacement in matches:
            if start < last_end:
                continue
            filtered.append((start, end, replacement))
            last_end = end
        return filtered

    def _split_paragraph_replacement_keys(
        self, para, replacements: dict[str, str]
    ) -> tuple[dict[str, str], dict[str, str]]:
        """把替换键分流为（run 级 / 整段 XML 级）两组，键集互斥。"""
        direct_nodes: list = []
        for run in para.runs:
            direct_nodes.extend(
                t for t in self._docx_xpath(run._r, "./w:t") if t is not None
            )
        direct_refs = {id(t) for t in direct_nodes}
        all_nodes = list(
            self._docx_xpath(para._p, ".//w:t | .//w:delText | .//w:instrText")
        )
        if not all_nodes:
            return (dict(replacements), {})

        pieces: list[str] = []
        is_direct_flags: list[bool] = []
        for node in all_nodes:
            text = node.text or ""
            pieces.append(text)
            is_direct_flags.extend([id(node) in direct_refs] * len(text))
        full_text = "".join(pieces)
        if not full_text:
            return (dict(replacements), {})

        # 空白归一口径分流（与 _find_replacement_matches 一致，Issue #84）
        squeezed, idx = self._squeeze_with_index(full_text)
        sq_direct = [is_direct_flags[i] for i in idx]

        run_keys: dict[str, str] = {}
        union_keys: dict[str, str] = {}
        for old_text, new_text in replacements.items():
            sq = "".join(ch for ch in old_text if ch not in SQUEEZE_CHARS)
            if not sq:
                continue
            needs_union = False
            pos = squeezed.find(sq)
            if pos < 0:
                continue
            while True:
                found = squeezed.find(sq, pos)
                if found < 0:
                    break
                if not all(sq_direct[found : found + len(sq)]):
                    needs_union = True
                    break
                pos = found + len(sq)
            if needs_union:
                union_keys[old_text] = new_text
            else:
                run_keys[old_text] = new_text
        return (run_keys, union_keys)

    def _replace_in_docx_xml_parts(
        self,
        doc: Document,
        replacements: dict[str, str],
        skip_elements: set | None = None,
    ) -> int:
        """Replace text in DOCX XML parts.

        pass1（python-docx 对象）只覆盖正文顶层/表格单元格/默认页眉页脚；
        本趟按 XML 全量扫（文本框、嵌套表格、SDT、首页/奇偶页眉页脚、
        批注/脚注/尾注都在内），跳过 pass1 已处理过的段落元素——
        重复处理会在替换词恰为另一实体原文时改写刚写入的替换词。
        """
        skip = skip_elements or set()
        if not replacements:
            return 0
        target_content_types = {
            CT.WML_DOCUMENT_MAIN,
            CT.WML_HEADER,
            CT.WML_FOOTER,
            CT.WML_COMMENTS,
            CT.WML_FOOTNOTES,
            CT.WML_ENDNOTES,
        }
        replaced_count = 0
        for part in doc.part.package.parts:
            if getattr(part, "content_type", None) not in target_content_types:
                continue
            root = getattr(part, "element", None)
            if root is None and hasattr(part, "blob"):
                try:
                    root = etree.fromstring(part.blob)
                except (etree.XMLSyntaxError, TypeError, ValueError):
                    root = None
            if root is None:
                continue
            part_replaced_count = 0
            for paragraph in self._docx_xpath(root, ".//w:p"):
                if paragraph in skip:
                    continue
                part_replaced_count += self._replace_in_docx_xml_paragraph(paragraph, replacements)
            if part_replaced_count and getattr(part, "element", None) is None:
                part._blob = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
            replaced_count += part_replaced_count
        return replaced_count

    def _replace_in_docx_xml_paragraph(
        self, paragraph, replacements: dict[str, str],
        node_query: str = ".//w:t | .//w:delText | .//w:instrText",
    ) -> int:
        # w:delText 是追踪修订「已删除」的内容，仍留在文档修订历史里，同样是敏感源
        text_nodes = list(self._docx_xpath(paragraph, node_query))
        if not text_nodes:
            return 0
        full_text = "".join(node.text or "" for node in text_nodes)
        if not full_text:
            return 0

        node_ids: list[int] = []
        for index, node in enumerate(text_nodes):
            node_ids.extend([index] * len(node.text or ""))
        if not node_ids:
            return 0

        # 空白归一匹配（Issue #84），返回原文本偏移区间
        matches = self._find_replacement_matches(full_text, replacements)
        if not matches:
            return 0

        replace_map: dict[int, tuple[int, str, int]] = {}
        for start, end, replacement in matches:
            span_node_ids = node_ids[start:end] if end <= len(node_ids) else node_ids[start:]
            target_node_idx = Counter(span_node_ids).most_common(1)[0][0] if span_node_ids else node_ids[start]
            replace_map[start] = (end, replacement, target_node_idx)

        outputs: list[list[str]] = [[] for _ in text_nodes]
        i = 0
        replaced_count = 0
        while i < len(full_text):
            replacement = replace_map.get(i)
            if replacement:
                end, replacement_text, target_node_idx = replacement
                outputs[target_node_idx].append(replacement_text)
                i = end
                replaced_count += 1
            else:
                node_idx = node_ids[i]
                outputs[node_idx].append(full_text[i])
                i += 1

        for index, node in enumerate(text_nodes):
            node.text = "".join(outputs[index])
        return replaced_count

    @staticmethod
    def _docx_xpath(element, query: str):
        try:
            return element.xpath(query)
        except etree.XPathError:
            return element.xpath(query, namespaces=WORD_XML_NS)

    def _iter_all_paragraphs(self, doc: Document):
        """遍历正文/表格/页眉页脚中的所有段落"""
        for para in doc.paragraphs:
            yield para
        for table in doc.tables:
            for row in table.rows:
                for cell in row.cells:
                    for para in cell.paragraphs:
                        yield para
        for section in doc.sections:
            for para in section.header.paragraphs:
                yield para
            for para in section.footer.paragraphs:
                yield para
            for table in section.header.tables:
                for row in table.rows:
                    for cell in row.cells:
                        for para in cell.paragraphs:
                            yield para
            for table in section.footer.tables:
                for row in table.rows:
                    for cell in row.cells:
                        for para in cell.paragraphs:
                            yield para

    def _replace_in_paragraph(
        self,
        para,
        replacements: dict[str, str],
        para_idx: int | None = None,
        trace_enabled: bool = False,
        trace_path: str | None = None,
    ) -> int:
        """在段落内进行 run 级替换，尽量保留原始格式"""
        if not replacements:
            return 0
        runs = list(para.runs)
        if not runs:
            return 0

        full_text = "".join(run.text for run in runs)
        if not full_text:
            return 0

        # 记录每个字符所属的 run 索引
        style_ids: list[int] = []
        for idx, run in enumerate(runs):
            style_ids.extend([idx] * len(run.text))

        if not style_ids:
            return 0

        # 空白归一匹配（Issue #84），返回原文本偏移区间
        matches = self._find_replacement_matches(full_text, replacements)
        if not matches:
            return 0

        before_snapshot = None
        if trace_enabled and trace_path:
            before_snapshot = self._collect_runs_font_snapshot(runs)

        # 构建替换起点索引：start -> (end, replacement, target_run_idx)
        # target_run_idx 使用区间内主样式 run，避免跨 run 时字体错位
        replace_map: dict[int, tuple[int, str, int]] = {}
        for start, end, replacement in matches:
            span_style_ids = style_ids[start:end] if end <= len(style_ids) else style_ids[start:]
            if span_style_ids:
                target_run_idx = Counter(span_style_ids).most_common(1)[0][0]
            else:
                target_run_idx = style_ids[start] if start < len(style_ids) else style_ids[-1]
            replace_map[start] = (end, replacement, target_run_idx)

        # 按全局文本顺序重建"各 run 的文本内容"
        run_outputs: list[list[str]] = [[] for _ in runs]
        i = 0
        replaced_count = 0
        while i < len(full_text):
            repl = replace_map.get(i)
            if repl:
                end, replacement, target_run_idx = repl
                run_outputs[target_run_idx].append(replacement)
                i = end
                replaced_count += 1
            else:
                run_idx = style_ids[i]
                run_outputs[run_idx].append(full_text[i])
                i += 1

        # 就地更新 run 文本：不新增/删除 run，最大化保留原始字体与样式继承链
        for idx, run in enumerate(runs):
            new_text = "".join(run_outputs[idx])
            if run.text != new_text:
                run.text = new_text

        if trace_enabled and trace_path:
            after_snapshot = self._collect_runs_font_snapshot(runs)
            self._append_docx_font_trace(
                trace_path,
                {
                    "timestamp": datetime.now().isoformat(),
                    "paragraph_index": para_idx,
                    "paragraph_text_before": full_text,
                    "matches": [
                        {
                            "start": s,
                            "end": e,
                            "original": full_text[s:e],
                            "replacement": rep,
                        }
                        for (s, e, rep) in matches
                    ],
                    "runs_before": before_snapshot,
                    "runs_after": after_snapshot,
                },
            )

        return replaced_count

    def _is_docx_font_trace_enabled(self) -> bool:
        """是否启用 docx 字体调试导出"""
        raw = os.getenv("DOCX_FONT_TRACE", "0").strip().lower()
        return raw in {"1", "true", "yes", "on"}

    def _get_docx_font_trace_path(self) -> str:
        """获取 docx 字体调试导出文件路径（JSONL）"""
        custom_path = os.getenv("DOCX_FONT_TRACE_PATH", "").strip()
        if custom_path:
            return custom_path
        return os.path.join(settings.DATA_DIR, "docx_font_trace.jsonl")

    def _init_docx_font_trace(
        self,
        trace_path: str,
        input_path: str,
        output_path: str,
        replacements: dict[str, str],
    ) -> None:
        """初始化调试导出文件并写入会话头"""
        try:
            trace_dir = os.path.dirname(trace_path)
            if trace_dir:
                os.makedirs(trace_dir, exist_ok=True)
            session_header = {
                "type": "session",
                "timestamp": datetime.now().isoformat(),
                "input_path": input_path,
                "output_path": output_path,
                "replacement_count": len(replacements),
            }
            with open(trace_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(session_header, ensure_ascii=False) + "\n")
        except (OSError, ValueError, TypeError) as e:
            logger.error("DOCX_TRACE 初始化失败: %s", e)

    def _append_docx_font_trace(self, trace_path: str, record: dict[str, Any]) -> None:
        """追加一条调试记录到 JSONL"""
        try:
            with open(trace_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except (OSError, ValueError, TypeError) as e:
            logger.error("DOCX_TRACE 写入失败: %s", e)

    def _collect_runs_font_snapshot(self, runs) -> list[dict[str, Any]]:
        """采集 run 的字体链快照（rPr/rFonts/字号/样式）"""
        from docx.oxml.ns import qn

        result: list[dict[str, Any]] = []
        for idx, run in enumerate(runs):
            r = run._element
            rPr = r.rPr
            rFonts = rPr.find(qn("w:rFonts")) if rPr is not None else None
            sz = rPr.find(qn("w:sz")) if rPr is not None else None
            szCs = rPr.find(qn("w:szCs")) if rPr is not None else None

            result.append(
                {
                    "run_index": idx,
                    "text": run.text,
                    "style_id": getattr(run.style, "style_id", None) if run.style else None,
                    "style_name": getattr(run.style, "name", None) if run.style else None,
                    "font_name_api": run.font.name,
                    "font_size_api_pt": float(run.font.size.pt) if run.font.size else None,
                    "rFonts": {
                        "ascii": rFonts.get(qn("w:ascii")) if rFonts is not None else None,
                        "hAnsi": rFonts.get(qn("w:hAnsi")) if rFonts is not None else None,
                        "eastAsia": rFonts.get(qn("w:eastAsia")) if rFonts is not None else None,
                        "cs": rFonts.get(qn("w:cs")) if rFonts is not None else None,
                        "asciiTheme": rFonts.get(qn("w:asciiTheme")) if rFonts is not None else None,
                        "hAnsiTheme": rFonts.get(qn("w:hAnsiTheme")) if rFonts is not None else None,
                        "eastAsiaTheme": rFonts.get(qn("w:eastAsiaTheme")) if rFonts is not None else None,
                        "csTheme": rFonts.get(qn("w:csTheme")) if rFonts is not None else None,
                        "hint": rFonts.get(qn("w:hint")) if rFonts is not None else None,
                    },
                    "rPr_size": {
                        "w:sz": sz.get(qn("w:val")) if sz is not None else None,
                        "w:szCs": szCs.get(qn("w:val")) if szCs is not None else None,
                    },
                    "rPr_xml": rPr.xml if rPr is not None else None,
                }
            )
        return result

    async def _redact_txt(
        self,
        input_path: str,
        output_path: str,
        entities: list[Entity],
        context: RedactionContext,
    ) -> int:
        """纯文本文件匿名化（.txt, .md, .html, .rtf）— 单次正则替换，O(n) 遍历"""
        import re as _re

        # 读取原文（兼容多种编码）
        content = None
        for enc in ("utf-8", "gbk", "gb2312", "latin-1"):
            try:
                with open(input_path, encoding=enc) as f:
                    content = f.read()
                break
            except (UnicodeDecodeError, ValueError):
                continue
        if content is None:
            with open(input_path, encoding="utf-8", errors="replace") as f:
                content = f.read()

        # 构建替换映射
        replacements: dict[str, str] = {}
        for entity in entities:
            if entity.text and entity.text not in replacements:
                replacements[entity.text] = context.get_replacement(entity)

        if not replacements:
            # 无需替换，直接拷贝
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(content)
            return 0

        # 构建一个联合正则：按长度降序排列，用 | 连接，单次遍历完成所有替换
        # 比逐个 str.replace 更高效（避免多次全文扫描）
        sorted_keys = sorted(replacements.keys(), key=len, reverse=True)
        pattern = _re.compile("|".join(_re.escape(k) for k in sorted_keys))
        redacted_count = 0

        def _replace_match(m: _re.Match) -> str:
            nonlocal redacted_count
            redacted_count += 1
            return replacements[m.group(0)]

        content = pattern.sub(_replace_match, content)

        with open(output_path, "w", encoding="utf-8") as f:
            f.write(content)

        return redacted_count

    def _promote_docx_page_footers(self, docx_path: str) -> int:
        """伪页码→真页脚（Issue #84 四轮，用户实测孤页问题）。

        pdf2docx 把原卷每页的页脚页码转成「每节末尾的孤立纯数字段落」
        （每原页一个分节）。docx 回转重排后这些段落被挤出节尾、单独成页
        → 页码孤页、成品页数近乎翻倍。本趟：删掉与页序对齐的伪页码段落，
        并挂上带 PAGE 域的真页脚——页码恒在页底，随重排自洽递增。
        仅当纯数字、且数值与页序对齐（允许封面无页码的整体偏移）才删，
        正文里恰好以数字结尾的数据不受影响。
        """
        from docx.enum.text import WD_ALIGN_PARAGRAPH
        from docx.oxml import OxmlElement
        from docx.oxml.ns import qn

        doc = Document(docx_path)
        W = "{" + WORD_XML_NS["w"] + "}"
        body = doc.element.body
        # 按分节归组段落（节末标志：段落 pPr 内含 sectPr；末节由 body 级 sectPr 收口）
        sections: list[list] = [[]]
        for child in body.iterchildren():
            if child.tag == f"{W}p":
                sections[-1].append(child)
                pPr = child.find(f"{W}pPr")
                if pPr is not None and pPr.find(f"{W}sectPr") is not None:
                    sections.append([])
            elif child.tag == f"{W}sectPr":
                sections.append([])  # body 级 sectPr 之后无正文
            elif child.tag == f"{W}tbl":
                sections[-1].append(child)
        if len(sections) > 1 and not sections[-1]:
            sections.pop()

        # 每节最后一个非空段落（含表格内嵌段落——末页页码可能挂在表格里），
        # 若为纯数字短段则记为伪页码候选
        candidates: list[tuple[int, object, int]] = []  # (节序1基, 段落元素, 数值)
        for idx, elements in enumerate(sections, start=1):
            last_nonempty = None
            for el in elements:
                if el.tag == f"{W}tbl":
                    paras_iter = el.findall(f".//{W}p")
                elif el.tag == f"{W}p":
                    paras_iter = [el]
                else:
                    continue
                for p in paras_iter:
                    text = "".join(t.text or "" for t in p.findall(f".//{W}t")).strip()
                    if text:
                        last_nonempty = (p, text)
            if last_nonempty and last_nonempty[1].isdigit() and len(last_nonempty[1]) <= 4:
                candidates.append((idx, last_nonempty[0], int(last_nonempty[1])))
        if not candidates:
            return 0

        # 页序偏移：候选值 - 节序须全体一致（封面无页码等整体偏移可容忍）；
        # 不一致说明候选里混着正文数据，保守起见一个都不删
        offsets = {value - ordinal for ordinal, _, value in candidates}
        if len(offsets) > 1:
            logger.warning(
                "[redact:pdf-docx] 伪页码候选页序偏移不一致 %s，放弃删除", sorted(offsets)
            )
            return 0
        removed = 0
        removed_texts: list[str] = []
        for _ordinal, p, _value in candidates:
            removed_texts.append(
                "".join(t.text or "" for t in p.findall(f".//{W}t"))
            )
            p.getparent().remove(p)
            removed += 1
        if not removed:
            return 0
        # 删除清单落日志供审计（评审 Important#4：promote 在校验之后执行，
        # 误删数字段无安全网兜底，至少留痕）
        logger.info(
            "[redact:pdf-docx] 伪页码删除 %d 段: %s", removed, removed_texts[:10]
        )

        # 挂真页脚：首页节建页脚（居中 PAGE 域），其余节默认链接同页脚
        footer = doc.sections[0].footer
        footer.is_linked_to_previous = False
        para = footer.paragraphs[0]
        para.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = para.add_run()
        fld_begin = OxmlElement("w:fldChar")
        fld_begin.set(qn("w:fldCharType"), "begin")
        instr = OxmlElement("w:instrText")
        instr.set(qn("xml:space"), "preserve")
        instr.text = " PAGE "
        fld_sep = OxmlElement("w:fldChar")
        fld_sep.set(qn("w:fldCharType"), "separate")
        sample = OxmlElement("w:t")
        sample.text = "1"
        fld_end = OxmlElement("w:fldChar")
        fld_end.set(qn("w:fldCharType"), "end")
        for el in (fld_begin, instr, fld_sep, sample, fld_end):
            run._r.append(el)
        # 其余节显式链接首页页脚（不依赖 pdf2docx 是否给后续节建独立 footer）
        for section in doc.sections[1:]:
            if not section.footer.is_linked_to_previous:
                section.footer.is_linked_to_previous = True
        doc.save(docx_path)
        return removed

    async def _redact_pdf_via_docx(
        self,
        input_path: str,
        output_path: str,
        entities: list[Entity],
        context: RedactionContext,
    ) -> int:
        """PDF 文档匿名化（文本型，替换模式主链路，Issue #61）。

        PDF→docx→文本替换→docx→PDF：docx 段落级替换的版面质量远好于
        PDF 原位替换。任一转换环节失败即回退原位替换（原文仍会从内容流
        删除，只是版面质量差），不允许因此交付失败或原文泄露。
        """
        unique_texts = {e.text for e in entities if e.selected and e.text}
        if not unique_texts:
            # 零实体：可访问（含权限密码自动认证）才原样拷贝——跑 docx 回转只
            # 会白白重排版面（评审 I3）。需打开密码的加密原件绝不能 copyfile 当
            # 「成品」输出（Issue #32，评审 P2-3）：抛 PdfEncryptedError，端点
            # 映射 400+错误码，与既有契约一致。
            doc = open_pdf_checked(input_path)
            doc.close()
            shutil.copyfile(input_path, output_path)
            return 0

        workdir = tempfile.mkdtemp(prefix="pdf_redact_")
        try:
            # pdf2docx 是 CPU 密集同步转换，丢线程跑避免卡事件循环
            docx_path = await asyncio.to_thread(self._pdf_to_docx, input_path, workdir)
            redacted_docx_path = os.path.join(workdir, "redacted.docx")
            if docx_path:
                count = await self._redact_docx(
                    docx_path, redacted_docx_path, entities, context
                )
                # 逐实体校验（评审 I1）：聚合计数会被「A 替换多次+B 整体
                # 丢失」凑数骗过。两类失败都回退原位替换——
                #   ①残留：redacted docx 里仍有原文（拆 run 替换不完整）
                #   ②转换丢失：源 docx 里就没有该实体（pdf2docx 丢内容）
                # 设计性保留实体（公共机构原文保留，org_rules #56：替换词
                # ==原文）豁免残留判定，否则含机关名的文书永远回退原位替换
                # 期望值对照校验（评审 I1 演进，Issue #84 二/三轮）：以同一替换
                # 语义干跑源 docx 得「应有成品」，实际成品按出现次数对照——
                #   residual：次数超出期望 = 真漏替（泄密方向）
                #   lost_absent：源 docx 就没有 = pdf2docx 丢/改写了该实体文本
                #   lost_dropped：应保留的内容变少 = 替换/转换丢内容
                entity_by_text = {
                    e.text: e for e in entities if e.selected and e.text
                }
                full_map = {
                    t: context.get_replacement(entity_by_text[t])
                    for t in unique_texts
                }
                residual, lost_absent, lost_dropped, cross_pure, cross_adj = (
                    self._docx_verify_replacements(
                        docx_path, redacted_docx_path, full_map
                    )
                )
                if lost_dropped:
                    logger.warning(
                        "[redact:pdf-docx] 应保留内容丢失 lost=%s，回退原位替换: %s",
                        sorted(lost_dropped)[:5], input_path,
                    )
                    return await self._redact_pdf_text(
                        input_path, output_path, entities, context
                    )
                if residual or lost_absent or cross_pure:
                    # 少量漏网实体（跨行碎片在 docx 里不连续、个别处漏替）：
                    # 不再整档回退兜底（排版劣化惩罚全文档），照常回转 PDF，
                    # 对漏网实体做一次逐字流补删（Issue #84 三轮）——
                    # 找得到就原位删除，找不到说明成品里本来就没有。
                    # 灾难性缺失（大量实体不在源 docx）= pdf2docx 不可信，
                    # 仍整档回退（评审 I1 语义保留）。
                    catastrophic = len(lost_absent) > max(5, len(unique_texts) // 20)
                    if catastrophic:
                        logger.warning(
                            "[redact:pdf-docx] 转换实体缺失 %d 个，回退原位替换: %s",
                            len(lost_absent), input_path,
                        )
                        return await self._redact_pdf_text(
                            input_path, output_path, entities, context
                        )
                    # 伪页码→真页脚（#84 四轮）：须在回转前处理 docx
                    await asyncio.to_thread(
                        self._promote_docx_page_footers, redacted_docx_path
                    )
                    if not await self._docx_to_pdf(redacted_docx_path, output_path):
                        logger.warning(
                            "[redact:pdf-docx] docx→PDF 回转失败，回退原位替换: %s",
                            input_path,
                        )
                        return await self._redact_pdf_text(
                            input_path, output_path, entities, context
                        )
                    patch_keys = {
                        t: full_map[t] for t in residual | lost_absent | cross_pure
                    }
                    patched, matched_keys = await self._targeted_pdf_redact(
                        output_path, patch_keys
                    )
                    # 补删后复检（评审 Important#3）：残留键落空（跨页等形态
                    # 逐字流找不到）或成品仍超替换值自含次数 → 整档回退兜底
                    # residual/cross_pure 落空=原文应删而未删，不得交付；
                    # lost_absent 落空属正常（成品里本来就没有）
                    unpatched = {
                        t for t in residual | cross_pure if t not in matched_keys
                    }
                    leak_keys = self._post_patch_leak_keys(output_path, patch_keys)
                    if unpatched or leak_keys:
                        logger.warning(
                            "[redact:pdf-docx] 补删复检失败 unpatched=%s leak=%s，回退原位替换: %s",
                            sorted(unpatched), sorted(leak_keys), input_path,
                        )
                        return await self._redact_pdf_text(
                            input_path, output_path, entities, context
                        )
                    logger.warning(
                        "[redact:pdf-docx] 校验 residual=%s lost_absent=%s，已定向补删 %d 处: %s",
                        sorted(residual)[:5], sorted(lost_absent)[:5], patched, input_path,
                    )
                    return count + patched
                # 伪页码→真页脚（#84 四轮）：须在回转前处理 docx
                await asyncio.to_thread(
                    self._promote_docx_page_footers, redacted_docx_path
                )
                if not await self._docx_to_pdf(redacted_docx_path, output_path):
                    logger.warning(
                        "[redact:pdf-docx] docx→PDF 回转失败，回退原位替换: %s", input_path
                    )
                    return await self._redact_pdf_text(
                        input_path, output_path, entities, context
                    )
                return count
            logger.warning(
                "[redact:pdf-docx] pdf→docx 转换失败，回退原位替换: %s", input_path
            )
        except Exception:
            logger.exception(
                "[redact:pdf-docx] docx 回转链路异常，回退原位替换: %s", input_path
            )
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
        return await self._redact_pdf_text(input_path, output_path, entities, context)

    def _docx_paragraph_texts(self, docx_path: str) -> list[str]:
        """读取 docx 全部段落文本（正文/表格/页眉页脚，与替换趟覆盖面一致）。"""
        doc = Document(docx_path)
        parts = [p.text or "" for p in self._iter_all_paragraphs(doc)]
        return parts

    def _simulate_replaced_text(self, text: str, replacements: dict[str, str]) -> str:
        """文本级干跑：按与真实替换相同的空白归一匹配语义算出替换后文本。"""
        matches = self._find_replacement_matches(text, replacements)
        if not matches:
            return text
        out: list[str] = []
        i = 0
        for start, end, replacement in matches:
            out.append(text[i:start])
            out.append(replacement)
            i = end
        out.append(text[i:])
        return "".join(out)

    def _docx_verify_replacements(
        self,
        source_docx_path: str,
        redacted_docx_path: str,
        replacements: dict[str, str],
    ) -> tuple[set[str], set[str], set[str], set[str], set[str]]:
        """残留校验（期望值对照，Issue #84 二轮；粒度修订见四轮评审）。

        按「段粒度干跑期望 vs 段粒度实际」判三类——
          residual：次数超出期望 = 段内真漏替（泄密方向）
          lost_absent：源 docx 就没有 = pdf2docx 丢/改写该实体
          lost_dropped：应保留内容变少 = 替换/转换丢内容
        另返回两类跨段键（源拼接出现次数 > 段内出现次数之和，评审
        Critical#1）——
          cross_para_pure：源里只有跨段形态（段内 0 命中），是真实体，
            逐段替换天然无法处理，调用方必须 PDF 补删并严格复检；
          cross_para_adjacent：段内也有实例，拼接处的相邻读属于无关
            文本的巧合相邻（原文档同样存在），不补不查——补删会误伤
            生成值里的相同字符（如 [姓名22] 的 22）。

        不在拼接文本上判 residual 的原因：相邻段落会在拼接处产生幻影
        相邻读（如「…7 月」+「17 日…」），并非实体真实例，会误回退。
        子串实体落在保留机构名/替换生成值里的两类误判由期望值对照解决。
        """
        try:
            src_parts = self._docx_paragraph_texts(source_docx_path)
            act_parts = self._docx_paragraph_texts(redacted_docx_path)
        except Exception:
            logger.exception("[redact:pdf-docx] docx 残留校验读取失败，按全量残留处理")
            return set(replacements), set(), set()

        def _squeeze_all(parts: list[str]) -> str:
            return "".join(
                self._squeeze_with_index(t)[0] for t in parts
            )

        src_squeezed = _squeeze_all(src_parts)
        src_squeezed_list = [self._squeeze_with_index(t)[0] for t in src_parts]
        act_squeezed_list = [self._squeeze_with_index(t)[0] for t in act_parts]
        exp_squeezed_list = [
            self._squeeze_with_index(
                self._simulate_replaced_text(t, replacements)
            )[0]
            for t in src_parts
        ]

        residual: set[str] = set()
        lost_absent: set[str] = set()
        lost_dropped: set[str] = set()
        cross_para_pure: set[str] = set()
        cross_para_adjacent: set[str] = set()
        for old_text in replacements:
            sq = "".join(ch for ch in old_text if ch not in SQUEEZE_CHARS)
            if not sq:
                continue
            # 段粒度计数：拼接计数会把相邻段落的幻影相邻读（如「…7 月」+
            # 「17 日…」）误判为漏替实体（四轮评审修复时实测踩中）
            exp_count = sum(t.count(sq) for t in exp_squeezed_list)
            act_count = sum(t.count(sq) for t in act_squeezed_list)
            src_count = src_squeezed.count(sq)
            src_perpara = sum(t.count(sq) for t in src_squeezed_list)
            if act_count > exp_count:
                residual.add(old_text)
            if src_count == 0:
                lost_absent.add(old_text)
            elif act_count < exp_count and len(sq) >= 3:
                # 长度阈值：1-2 字符碎片的出现次数对生成值/多趟替换的
                # 相互作用极其敏感（实测 [姓名22] 类值导致 ±1 抖动），
                # 「应保留内容变少」信号对它们无意义
                lost_dropped.add(old_text)
            # 跨段实体（评审 Critical#1）：源拼接出现 > 段内出现之和
            if src_count > src_perpara:
                if src_perpara == 0:
                    cross_para_pure.add(old_text)
                else:
                    cross_para_adjacent.add(old_text)
        return residual, lost_absent, lost_dropped, cross_para_pure, cross_para_adjacent

    def _post_patch_leak_keys(
        self, pdf_path: str, keys: dict[str, str]
    ) -> set[str]:
        """补删后全卷复检（评审 Important#3）。

        在成品 PDF 的压空白全拼接文本上数键的出现次数；替换值自含该键
        （生成值撞碎片，如 [编号22] 含 22）允许同等次数。超出即判泄漏。
        口径注意：跨页拼接会引入假阳性方向的误报（保守方向，宁可回退）。
        """
        doc = fitz.open(pdf_path)
        try:
            full = "".join(
                self._squeeze_with_index(page.get_text())[0] for page in doc
            )
        finally:
            doc.close()
        leaks: set[str] = set()
        for old_text, new_text in keys.items():
            sq = "".join(ch for ch in old_text if ch not in SQUEEZE_CHARS)
            if not sq:
                continue
            allowed = new_text.count(sq)
            if full.count(sq) > allowed:
                leaks.add(old_text)
        return leaks

    async def _targeted_pdf_redact(
        self, pdf_path: str, keys: dict[str, str]
    ) -> tuple[int, set[str]]:
        """对回转产物定向补删漏网实体（Issue #84 三轮）。

        逐字流匹配（同兜底路），只处理给定键；键文本找得到就涂红删除，
        找不到（pdf2docx 把文本改写/丢了）说明成品里没有，无需处理。
        返回 (补删处数, 实际命中并删除的键集合)——命中空集是补删落空的
        信号，由调用方决定是否回退（评审 Important#3）。
        """
        matched_keys: set[str] = set()
        if not keys:
            return 0, set()
        # 注意：pdf_path 是我们自己刚回转的产物（非用户上传），不走上传白名单
        doc = fitz.open(pdf_path)
        try:
            patched = 0
            for page in doc:
                inserts = self._redact_pdf_page_chars(page, keys)
                patched += sum(1 for _, t, _ in inserts if t)
                matched_keys.update(t for _, t, _ in inserts if t)
                if inserts:
                    page.apply_redactions()
                    for rect, new_text, orig_size in inserts:
                        if not new_text:
                            continue
                        has_cjk = any(ord(ch) > 127 for ch in new_text)
                        fontname = "china-s" if has_cjk else "helv"
                        self._insert_pdf_replacement(
                            page, rect, new_text, fontname,
                            self._fit_pdf_replacement_font_size(
                                rect, new_text, fontname, original_size=orig_size
                            ),
                        )
            tmp_out: str | None = None
            if patched:
                # 全量保存（评审 Important#2）：增量保存会在文件里物理保留
                # 被删原文的旧字节，朴素对象扫描可还原——交付物必须物理性删除。
                # PyMuPDF 不允许非增量保存到原路径，落临时文件后原子替换
                tmp_out = pdf_path + ".patched"
                doc.save(
                    tmp_out, garbage=PDF_SAVE_GARBAGE_LEVEL, deflate=True, clean=True
                )
        finally:
            doc.close()
        if tmp_out is not None:
            os.replace(tmp_out, pdf_path)
        return patched, matched_keys

    @staticmethod
    def _pdf_to_docx(input_path: str, workdir: str) -> str | None:
        """pdf2docx 转换；依赖缺失或转换失败返回 None（调用方回退）。"""
        try:
            from pdf2docx import Converter
        except ImportError:
            logger.warning("[redact:pdf-docx] pdf2docx 未安装，回退原位替换")
            return None
        docx_path = os.path.join(workdir, "source.docx")
        try:
            cv = Converter(input_path)
            try:
                cv.convert(docx_path)
            finally:
                cv.close()
        except Exception:
            logger.exception("[redact:pdf-docx] pdf→docx 转换异常: %s", input_path)
            return None
        return docx_path if os.path.exists(docx_path) and os.path.getsize(docx_path) > 0 else None

    @staticmethod
    def _soffice_staging_root(soffice: str) -> str:
        """soffice 暂存根目录（评审 C2）。

        snap 打包的 LibreOffice 沙箱只能访问 $HOME 下**非隐藏**目录——
        /tmp 和 ~/.cache 都读不了（隐藏顶级目录被 home 接口阻断），
        所以 snap 用 ~/redaction-soffice-tmp；非 snap（apt/Docker）用 /tmp。
        """
        if "/snap/" in soffice:
            return os.path.join(os.path.expanduser("~"), "redaction-soffice-tmp")
        return os.path.join(tempfile.gettempdir(), "redaction-soffice")

    @staticmethod
    async def _docx_to_pdf(docx_path: str, output_pdf_path: str) -> bool:
        """LibreOffice 回转 docx→PDF（doc→docx 先例同一转换器）。

        暂存目录选择见 _soffice_staging_root；产物再搬回目标位置。
        """
        soffice_candidates = [
            os.environ.get("SOFFICE_PATH", ""),
            shutil.which("soffice") or "",
            shutil.which("libreoffice") or "",
            "/snap/bin/libreoffice",
            "/usr/bin/soffice",
            "/usr/local/bin/soffice",
            "/opt/libreoffice/program/soffice",
            r"C:\Program Files\LibreOffice\program\soffice.exe",
        ]
        soffice = next((p for p in soffice_candidates if p and os.path.exists(p)), None)
        if not soffice:
            logger.warning("[redact:pdf-docx] 未找到 LibreOffice (soffice)")
            return False
        staging_root = TextRedactorMixin._soffice_staging_root(soffice)
        try:
            os.makedirs(staging_root, exist_ok=True)
            staging = tempfile.mkdtemp(prefix="conv_", dir=staging_root)
        except OSError:
            staging = tempfile.mkdtemp(prefix="redaction_conv_")
        staged_docx = os.path.join(staging, os.path.basename(docx_path))
        try:
            shutil.copy2(docx_path, staged_docx)
        except OSError:
            logger.exception("[redact:pdf-docx] 暂存拷贝失败: %s", docx_path)
            shutil.rmtree(staging, ignore_errors=True)
            return False
        try:
            # soffice 是阻塞进程，丢进线程跑避免卡事件循环；超时防挂死
            # profile URI 走 as_uri 百分号编码（用户名含空格等不再炸）
            proc = await asyncio.to_thread(
                subprocess.run,
                [soffice, "--headless", "--norestore",
                 # 独立 profile：并发转换互不抢锁，否则第二个实例会把参数
                 # 转发给第一个后立即退出 0，产物丢失
                 f"-env:UserInstallation={Path(os.path.join(staging, 'profile')).as_uri()}",
                 "--convert-to", "pdf",
                 "--outdir", staging, staged_docx],
                capture_output=True,
                timeout=120,
            )
            if proc.returncode != 0:
                logger.warning(
                    "[redact:pdf-docx] LibreOffice 退出码 %d: %s",
                    proc.returncode, (proc.stderr or b"")[:200],
                )
        except Exception:
            logger.exception("[redact:pdf-docx] LibreOffice 转换异常: %s", docx_path)
            shutil.rmtree(staging, ignore_errors=True)
            return False
        produced = os.path.join(
            staging, os.path.splitext(os.path.basename(staged_docx))[0] + ".pdf"
        )
        ok = proc.returncode == 0 and os.path.exists(produced) and os.path.getsize(produced) > 0
        if ok:
            shutil.move(produced, output_pdf_path)
        else:
            logger.warning(
                "[redact:pdf-docx] LibreOffice 未产出 PDF: %s",
                (proc.stderr or b"")[:200],
            )
        shutil.rmtree(staging, ignore_errors=True)
        return ok

    async def _redact_pdf_text(
        self,
        input_path: str,
        output_path: str,
        entities: list[Entity],
        context: RedactionContext,
    ) -> int:
        """PDF 文档匿名化（文本型）——原位替换（兜底链路）"""
        # Issue #30：需打开密码的 PDF 抛 PdfEncryptedError（端点映射 400+错误码）
        doc = open_pdf_checked(input_path)
        try:
            redacted_count = 0

            # 构建替换映射
            replacements = {}
            for entity in entities:
                if entity.text not in replacements:
                    replacements[entity.text] = context.get_replacement(entity)

            # 对每一页进行处理。逐字流匹配（Issue #84 三轮）：rawdict 给出每个
            # 字符的坐标与 span 字号，空白归一后整页匹配一次、映射回字符下标——
            #   ① search_for 对跨行/表格/带空格伪影文本会漏 → 泄漏；
            #   ② 矩形相交去重在同行相邻实体接缝处（零点几像素重叠）整只误跳
            #   → 同样泄漏（用户成品 79 实体泄漏实锤）。
            # 字符级区间天然精确：嵌套实体由最长匹配优先过滤，无需矩形近似。
            for page_num in range(len(doc)):
                page = doc.load_page(page_num)
                replacement_inserts = self._redact_pdf_page_chars(
                    page, replacements
                )
                redacted_count += sum(1 for _, t, _ in replacement_inserts if t)

                if replacement_inserts:
                    page.apply_redactions()
                    for rect, new_text, orig_size in replacement_inserts:
                        # 默认 Helvetica 无 CJK 字形（中文会写成 ???），中文替换词用内置 china-s
                        has_cjk = any(ord(ch) > 127 for ch in new_text)
                        fontname = "china-s" if has_cjk else "helv"
                        self._insert_pdf_replacement(
                            page, rect, new_text, fontname,
                            self._fit_pdf_replacement_font_size(
                                rect, new_text, fontname, original_size=orig_size
                            ),
                        )

            doc.save(output_path, garbage=PDF_SAVE_GARBAGE_LEVEL, deflate=True, clean=True)
        finally:
            doc.close()

        return redacted_count

    def _redact_pdf_page_chars(
        self, page: "fitz.Page", replacements: dict[str, str]
    ) -> list[tuple[fitz.Rect, str, float]]:
        """单页逐字流匹配：返回 [(标签矩形, 替换文本, 原字号)]，并就地添加
        涂红注释（apply_redactions 由调用方执行）。

        匹配在「压空白字符流」上做（字符流只存非空白字符，squeezed 下标即
        字符下标），命中区间按基线分行，每行合并一个涂红矩形；标签写在
        首行矩形处。跨行实体整体命中（多行各自涂红），不再依赖 search_for。
        """
        stream: list[str] = []
        char_rects: list[fitz.Rect] = []
        char_sizes: list[float] = []
        try:
            raw = page.get_text("rawdict")
        except Exception:
            logger.exception("[redact] rawdict 读取失败，跳过该页: page=%d", page.number)
            return []
        for block in raw.get("blocks", []):
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    size = float(span.get("size") or 0.0)
                    for ch in span.get("chars", []):
                        c = ch.get("c") or ""
                        if not c or c in SQUEEZE_CHARS:
                            continue
                        stream.append(c)
                        char_rects.append(fitz.Rect(ch["bbox"]))
                        char_sizes.append(size)
        if not stream:
            return []
        matches = self._find_replacement_matches("".join(stream), replacements)
        if not matches:
            return []

        inserts: list[tuple[fitz.Rect, str, float]] = []
        for start, end, new_text in matches:
            seg = list(zip(char_rects[start:end], char_sizes[start:end], strict=True))
            # 按基线分行（同行字符 y0 相差 <2pt）
            lines: list[list[tuple[fitz.Rect, float]]] = []
            for rect, size in seg:
                if lines and abs(rect.y0 - lines[-1][-1][0].y0) < 2.0:
                    lines[-1].append((rect, size))
                else:
                    lines.append([(rect, size)])
            first = True
            for line in lines:
                union = fitz.Rect(line[0][0])
                for rect, _ in line[1:]:
                    union |= rect
                if first:
                    orig_size = max(size for _, size in line) or None
                    inserts.append((union, new_text, orig_size))
                    first = False
                else:
                    inserts.append((union, "", None))
                page.add_redact_annot(union, fill=(1, 1, 1))
        return inserts

    @staticmethod
    def _insert_pdf_replacement(
        page: "fitz.Page",
        rect: "fitz.Rect",
        text: str,
        fontname: str,
        fontsize: float,
    ) -> None:
        """insert_textbox 放不下时（rc<0）整体不写入，逐级降字号重试到 4pt。"""
        size = fontsize
        while True:
            rc = page.insert_textbox(
                rect, text, fontname=fontname, fontsize=size,
                color=(0, 0, 0), align=fitz.TEXT_ALIGN_LEFT,
            )
            if rc >= 0 or size <= 4.0:
                if rc < 0:
                    logger.warning(
                        "PDF replacement text still does not fit at %.1fpt, dropped: %r rect=%s",
                        size, text[:40], rect,
                    )
                return
            size = max(4.0, size - 1.0)

    @staticmethod
    def _fit_pdf_replacement_font_size(
        rect: fitz.Rect, text: str, fontname: str = "helv",
        original_size: float | None = None,
    ) -> float:
        """替换标签字号：优先沿用原文字号（Issue #84，不再钳 10pt 帽），
        超宽按矩形自适应缩放；读不到原文信息时退回高度估算的保守值。"""
        if not text:
            return PDF_LABEL_FONT_SIZE_MAX
        if original_size and original_size > 0:
            base_size = original_size
        else:
            base_size = max(
                PDF_LABEL_FONT_SIZE_MIN,
                min(PDF_LABEL_FONT_SIZE_MAX, rect.height * PDF_LABEL_HEIGHT_FONT_RATIO),
            )
        estimated_width = fitz.get_text_length(text, fontname=fontname, fontsize=base_size)
        if estimated_width <= max(1.0, rect.width):
            return base_size
        return max(
            PDF_LABEL_FONT_SIZE_FLOOR,
            min(base_size, base_size * rect.width / max(1.0, estimated_width)),
        )

    def _extract_docx_text(self, file_path: str) -> str:
        """提取 Word 文档文本（含表格，与 FileParser._parse_docx 结构一致）"""
        doc = Document(file_path)
        paragraphs = []
        for para in doc.paragraphs:
            text = para.text.strip()
            if text:
                paragraphs.append(text)
        for table in doc.tables:
            for row in table.rows:
                row_text = []
                for cell in row.cells:
                    cell_text = cell.text.strip()
                    if cell_text:
                        row_text.append(cell_text)
                if row_text:
                    paragraphs.append(" | ".join(row_text))
        return "\n".join(paragraphs)

    def _extract_pdf_text(self, file_path: str) -> str:
        """提取 PDF 文档文本"""
        doc = open_pdf_checked(file_path)
        try:
            text = ""
            for page in doc:
                text += page.get_text() + "\n"
        finally:
            doc.close()
        return text

    def _read_txt(self, file_path: str) -> str:
        """读取纯文本文件（兼容多编码）"""
        for enc in ("utf-8", "gbk", "gb2312", "latin-1"):
            try:
                with open(file_path, encoding=enc) as f:
                    return f.read()
            except (UnicodeDecodeError, ValueError):
                continue
        with open(file_path, encoding="utf-8", errors="replace") as f:
            return f.read()

    def _safe_extract_text(self, file_path: str, ft: str) -> str:
        """安全提取文本，兼容 .doc 和 .docx"""
        if ft == "doc":
            # .doc 文件不能直接用 python-docx 打开
            # 尝试查找转换后的 .docx 文件
            docx_path = file_path.rsplit(".", 1)[0] + ".docx"
            if os.path.exists(docx_path):
                return self._extract_docx_text(docx_path)
            # 尝试从临时目录找
            import glob
            tmp_pattern = os.path.join(os.path.dirname(file_path), "*.docx")
            docx_files = glob.glob(tmp_pattern)
            for f in docx_files:
                if os.path.basename(file_path).rsplit(".", 1)[0] in os.path.basename(f):
                    return self._extract_docx_text(f)
            return "[.doc 文件无法直接提取文本，请查看原始文档]"
        else:
            return self._extract_docx_text(file_path)
