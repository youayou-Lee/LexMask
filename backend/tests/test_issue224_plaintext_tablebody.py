"""Issue#224：MinerU 4.x 纯文本 table_body 全量丢失。

根因（真卷逐层实证，见私有仓 docs/acceptance/2026-11-03-*-根因报告.md）：
4.x flash 对扫描表格输出的 ``table_body`` 是纯文本版面文字（无 HTML 标签），
``table_to_text`` 只收集 <td>/<th> 单元格 → 解析 0 行返回空 → ``clean_segments``
记 ``empty table block`` → 表格正文全量丢弃（fail-open）。

判据（冷审 2026-11-03 修订）：
- HTML 解析结果字符串非空 ⇒ 原样返回（既有行为逐字节不变）；
- 解析结果为空 **且** 输入不含 HTML 表格标签 ⇒ 纯文本回落（按行拆/空白折叠/strip_inline）；
- 含表格标签但解析不出行（畸形 HTML）⇒ 维持空 + 告警（不把标签喂给 NER，
  守住 #212 双表示守卫「丢表 ⇒ empty_table_blocks>0 ⇒ 回落 v1」语义链）。

全部夹具为合成数据（红线：真实案卷零入库）。
"""
from __future__ import annotations

import asyncio

from app.models.schemas import Entity
from app.services.agent_md_adapter import (
    CleanStats,
    clean_segments,
    normalize_docvortex,
    table_to_text,
)

# ---------------------------------------------------------------------------
# AC1：table_to_text 直测
# ---------------------------------------------------------------------------


class TestTableToTextPlaintext:
    def test_plaintext_body_returns_lines(self):
        body = "案号          （2026）合成0101执1号\n被查询对象    张某某     证件号  110101199001011234"
        out = table_to_text(body)
        assert out, "纯文本 table_body 不得返回空（Issue#224 本体）"
        assert "（2026）合成0101执1号" in out
        assert "110101199001011234" in out

    def test_plaintext_collapses_whitespace_keeps_line_order(self):
        body = "甲       乙          丙\n\n\n丁              戊"
        out = table_to_text(body)
        lines = [ln for ln in out.split("\n") if ln.strip()]
        assert lines == ["甲 乙 丙", "丁 戊"], out

    def test_plaintext_strips_inline_marks(self):
        out = table_to_text("**余额**  100")
        assert "**" not in out
        assert "余额" in out and "100" in out

    def test_plaintext_numbers_and_amounts_verbatim(self):
        """冷审 B4 负向钉：日期/金额逐字保留，strip_inline 不得吞内容。"""
        body = "日期  2023年12月31日\n金额  1,234,567.89元"
        out = table_to_text(body)
        assert "2023年12月31日" in out
        assert "1,234,567.89元" in out

    def test_blank_inputs_stay_empty(self):
        assert table_to_text("") == ""
        assert table_to_text("   \n  \t ") == ""

    def test_malformed_html_does_not_fall_back(self):
        """冷审 B2 钉：含表格标签但解析不出行 → 维持空（不把标签喂给 NER）。"""
        assert table_to_text("<table><tr></tr></table>") == ""
        assert table_to_text("<table><tr><td></td></tr></table>") == ""

    def test_html_still_wins_byte_for_byte(self):
        body = "<table><tr><td>甲</td><td>乙</td></tr></table>"
        assert table_to_text(body) == "甲 | 乙"

    def test_mixed_html_and_plaintext_prefers_html(self):
        """HTML 行非空即原样返回，尾巴上的纯文本不参与。"""
        body = "<table><tr><td>甲</td></tr></table>\n附注纯文本"
        assert table_to_text(body) == "甲"


# ---------------------------------------------------------------------------
# AC2：clean_segments 集成
# ---------------------------------------------------------------------------


def _v4_doc(blocks_by_page):
    """docvortex.middle 形状合成（与 #219 夹具同构）。"""
    return {
        "schema": "docvortex.middle", "schema_version": "2.0",
        "metadata": {"producer": {"name": "mineru", "version": "4.1.3"}},
        "pages": [{"page_idx": idx, "blocks": blocks} for idx, blocks in blocks_by_page],
        "is_full_document": True,
    }


class TestCleanSegmentsPlaintextTable:
    def test_plaintext_table_body_yields_table_seg(self):
        doc = _v4_doc([(0, [{
            "type": "table", "index": 0, "bbox": [0, 0, 1, 1],
            "content": [
                {"type": "text", "content": "表：合成财产反馈"},
                {"type": "table_body", "content": "案号  （2026）合成0101执1号\n证件号  110101199001011234"},
            ],
        }])])
        flat, _ = normalize_docvortex([doc])
        stats = CleanStats()
        segs, warns = clean_segments(flat, stats=stats)
        table_segs = [s for s in segs if s.source == "table"]
        assert len(table_segs) == 1
        assert "（2026）合成0101执1号" in table_segs[0].text
        assert "表：合成财产反馈" in table_segs[0].text  # caption 随非空 body 拼入
        assert stats.empty_table_blocks == 0
        assert not any("empty table block" in w for w in warns)

    def test_real_empty_table_still_warns(self):
        doc = _v4_doc([(0, [{
            "type": "table", "index": 0, "bbox": [0, 0, 1, 1],
            "content": [{"type": "table_body", "content": ""}],
        }])])
        flat, _ = normalize_docvortex([doc])
        stats = CleanStats()
        segs, warns = clean_segments(flat, stats=stats)
        assert not any(s.source == "table" for s in segs)
        assert stats.empty_table_blocks == 1
        assert any("empty table block" in w for w in warns)

    def test_malformed_html_table_still_warns_not_fed_to_ner(self):
        """冷审 B2 集成侧：畸形 HTML 表维持空+告警，标签不进段。"""
        doc = _v4_doc([(0, [{
            "type": "table", "index": 0, "bbox": [0, 0, 1, 1],
            "content": [{"type": "table_body", "content": "<table><tr></tr></table>"}],
        }])])
        flat, _ = normalize_docvortex([doc])
        stats = CleanStats()
        segs, warns = clean_segments(flat, stats=stats)
        assert not any(s.source == "table" for s in segs)
        assert stats.empty_table_blocks == 1
        assert not any("<table>" in s.text for s in segs)


# ---------------------------------------------------------------------------
# AC3：管线级——纯文本表格 4.x 产物走表示选择 → 出段出实体、无 shortfall 告警
# ---------------------------------------------------------------------------


class _FakeMineruResult:
    def __init__(self, *, md="", mineru4_docs=None):
        self.md = md
        self.content_list_v1 = []
        self.content_list_v2 = []
        self.images = {}
        self.mineru4_docs = mineru4_docs or []
        self.unknown_jsons = []


def test_choose_representation_keeps_plaintext_tables():
    """修后 v4 字符数正常：segs 非空、source=docvortex_middle、无 shortfall。"""
    from app.services.agent_md_pipeline_service import AgentMdPipelineService

    doc = _v4_doc([(0, [{
        "type": "table", "index": 0, "bbox": [0, 0, 1, 1],
        "content": [
            {"type": "text", "content": "表：合成财产反馈"},
            {"type": "table_body", "content":
                "案号  （2026）合成0101执1号\n证件号  110101199001011234\n余额  1,234,567.89元"},
        ],
    }])])
    result = _FakeMineruResult(mineru4_docs=[doc], md="表：合成财产反馈\n案号 （2026）合成0101执1号")
    segs, warns, _stats, source = AgentMdPipelineService._choose_representation(result)
    assert segs is not None and source == "docvortex_middle"
    joined = "\n".join(s.text for s in segs)
    assert "（2026）合成0101执1号" in joined
    assert "110101199001011234" in joined
    assert not any("docvortex_shortfall" in w for w in warns)


def test_stage1_mapping_contains_table_entities(tmp_path, monkeypatch):
    """run_agent_md_stage1：纯文本表格内容进 NER → 映射表含表格内实体。"""

    from app.services.agent_md_batch import run_agent_md_stage1
    from tests.test_issue196_agent_batch import FakeNER  # 复用既有假件

    class _FakeClient:
        def __init__(self, result):
            self._result = result

        async def submit(self, *, file_bytes, filename):
            return "task-1"

        async def wait_result(self, job_id, on_progress=None):
            return self._result

    doc = _v4_doc([(0, [{
        "type": "table", "index": 0, "bbox": [0, 0, 1, 1],
        "content": [{"type": "table_body", "content":
            "被查询对象  张某某\n证件号  110101199001011234\n电话  13800001234"}],
    }])])
    pdf = tmp_path / "probe.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    file_info = {"file_id": "f1", "filename": "probe.pdf", "original_filename": "probe.pdf",
                 "file_path": str(pdf), "file_type": "pdf", "owner_id": "u1"}
    monkeypatch.setattr(
        "app.services.mineru_parse_client.MineruParseClient",
        lambda *a, **k: _FakeClient(_FakeMineruResult(mineru4_docs=[doc])),
    )

    class _NopNER(FakeNER):
        def __init__(self):
            super().__init__([[("PERSON", "", 0, 3), ("PHONE", "", 0, 11)]])

        async def extract(self, text, entity_types):
            self.calls.append(text)
            ents = []
            if "张某某" in text:
                s = text.index("张某某")
                ents.append(Entity(id="e1", text="张某某", type="PERSON", start=s, end=s + 3, page=1))
            if "13800001234" in text:
                s = text.index("13800001234")
                ents.append(Entity(id="e2", text="13800001234", type="PHONE", start=s, end=s + 11, page=1))
            return ents

    monkeypatch.setattr(
        "app.services.hybrid_ner_service.HybridNERService", lambda: _NopNER())
    draft = asyncio.run(run_agent_md_stage1(file_info, {}, owner_id="u1"))
    texts = [m["original_text"] for m in draft["mapping"]]
    assert "张某某" in texts and "13800001234" in texts
    assert draft["carrier"] == "mineru"
