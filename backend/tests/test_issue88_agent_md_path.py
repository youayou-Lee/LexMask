"""Issue#88 喂Agent新路径（agent_md_ner）回归：run_ner 剥引号 + 映射草稿姓氏不退化。

PR#77 后喂Agent实际管线走 agent_md_pipeline_service → agent_md_ner；
vl_md_pipeline_service 的剥引号收口覆盖不到这里，需在本路径同样生效。
"""

import logging

from app.models.schemas import Entity
from app.services.agent_md_ner import build_mapping_draft, run_ner
from app.services.agent_md_types import Chunk


class StubNER:
    def __init__(self, spans):
        self.spans = spans  # list[(text, type)]

    async def extract(self, text, types):
        out = []
        for t, ty in self.spans:
            i = text.find(t)
            if i != -1:
                out.append(Entity(id=f"s{len(out)}", text=t, type=ty,
                                  start=i, end=i + len(t), source="has"))
        return out


async def _run(chunks, spans):
    return await run_ner(chunks, [], StubNER(spans))


def test_run_ner_trims_quoted_span():
    import asyncio
    chunk = Chunk(base_offset=100, text='证人“李四”出庭。', page_idx=0)
    ents, _ = asyncio.run(_run([chunk], [("“李四”", "PERSON")]))
    assert len(ents) == 1
    e = ents[0]
    assert e.text == "李四"
    assert (e.start, e.end) == (100 + 3, 100 + 5)  # 剥后偏移+全局平移
    assert e.page == 1


def test_run_ner_trailing_quote_preserved():
    import asyncio
    chunk = Chunk(base_offset=0, text="被告人张三”供述。", page_idx=0)
    ents, _ = asyncio.run(_run([chunk], [("张三”", "PERSON")]))
    assert ents[0].text == "张三"
    assert chunk.text[ents[0].start:ents[0].end] == "张三"  # 收引号留在文本里


def test_run_ner_all_quote_span_dropped():
    import asyncio
    chunk = Chunk(base_offset=0, text="证人“”出庭。", page_idx=0)
    ents, _ = asyncio.run(_run([chunk], [("“”", "PERSON")]))
    assert ents == []


def test_run_ner_trim_logged(caplog):
    import asyncio
    chunk = Chunk(base_offset=0, text='证人“李四”出庭。', page_idx=0)
    with caplog.at_level(logging.INFO, logger="app.services.agent_md_ner"):
        asyncio.run(_run([chunk], [("“李四”", "PERSON")]))
    assert any("quoted span trimmed" in r.getMessage() for r in caplog.records)


def test_draft_surname_defensive_strip():
    # 防御层：即便上游漏剥，映射草稿姓氏也不取到引号
    items = build_mapping_draft([Entity(id="e1", text="“李四”", type="PERSON",
                                        start=0, end=4, source="has")])
    assert items[0].replacement == "[李某]", items[0].replacement


def test_pipeline_bare_and_quoted_merge_single_row():
    # 管线口径：run_ner 剥净后 “李四” 与 李四 同文本 → 映射表单行、同化名
    import asyncio
    chunk = Chunk(base_offset=0, text='证人“李四”与李四同来。', page_idx=0)
    ents, _ = asyncio.run(_run([chunk], [("“李四”", "PERSON"), ("李四", "PERSON")]))
    items = build_mapping_draft(ents)
    assert len(items) == 1, [(i.original_text, i.replacement) for i in items]
    assert items[0].original_text == "李四"
    assert items[0].replacement == "[李某]"
