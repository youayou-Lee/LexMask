"""NER 分块/平移/映射草稿测试。Entity/EntityTypeConfig 合成。"""
import asyncio

from app.models.entity_schemas import Entity
from app.services.agent_md_ner import build_mapping_draft, chunk_segments, run_ner
from app.services.agent_md_types import Seg


def test_chunk_segments_never_splits_a_segment():
    segs = [Seg(text="A" * 10, page_idx=0, source="text"),
            Seg(text="B" * 10, page_idx=1, source="text")]
    chunks = chunk_segments(segs, max_chars=25)  # 10+2+10=22 放得下
    assert len(chunks) == 1
    assert chunks[0].text.count("\n\n") == 1


def test_chunk_segments_splits_on_budget():
    segs = [Seg(text="A" * 20, page_idx=0, source="text"),
            Seg(text="B" * 20, page_idx=1, source="text")]
    chunks = chunk_segments(segs, max_chars=30)
    assert len(chunks) == 2 and chunks[1].text == "B" * 20


class _FakeNer:
    def __init__(self, per_call):
        self.per_call = per_call
        self.calls = []

    async def extract(self, text, entity_types):
        self.calls.append(text)
        return self.per_call[len(self.calls) - 1]


def test_run_ner_shifts_offsets_and_sets_page():
    # max_chars=25 使 20+2+4=26 超预算 → 两块，第二块 base_offset=20+2=22
    chunks = chunk_segments([Seg(text="A" * 20, page_idx=2, source="text"),
                             Seg(text="张三欠款", page_idx=3, source="text")], max_chars=25)
    ent = Entity(id="x", text="张三", type="PERSON", start=0, end=2)
    ner = _FakeNer([[], [ent]])  # 第一块无实体，第二块返回张三
    entities, _ = asyncio.run(run_ner(chunks, [], ner))
    assert len(entities) == 1
    assert entities[0].start == 20 + 2  # 上一块 20 字 + 分隔符 2
    assert entities[0].end == 24
    assert entities[0].page == 4


def test_build_mapping_draft_occurrence_order_and_merge():
    """4 实体含重复（张三×2）→ 去重后 3 行唯一 (text,type)；PERSON 化名 [姓某]
    （张三/张三同文去重后张姓唯一→[张某]，李四→[李某]），其余类型 [类型_N] 占位符。"""
    e1 = Entity(id="a", text="张三", type="PERSON", start=0, end=2)
    e2 = Entity(id="b", text="李四", type="PERSON", start=5, end=7)
    e3 = Entity(id="c", text="张三", type="PERSON", start=9, end=11)
    e4 = Entity(id="d", text="6222", type="BANK_CARD", start=20, end=24)
    items = build_mapping_draft([e1, e2, e3, e4])
    assert len(items) == 3
    assert [m.replacement for m in items] == ["[张某]", "[李某]", "[银行卡_1]"]
    # 行唯一且行 id 唯一：render_outputs 查找 dict 不再有 last-row-wins 覆写
    keys = {(m.original_text, m.entity_type) for m in items}
    assert len(keys) == 3
    assert len({m.id for m in items}) == 3


def test_cn_numeral():
    from app.services.agent_md_ner import _cn_numeral
    assert _cn_numeral(1) == "一"
    assert _cn_numeral(9) == "九"
    assert _cn_numeral(10) == "十"
    assert _cn_numeral(11) == "十一"
    assert _cn_numeral(20) == "二十"
    assert _cn_numeral(21) == "二十一"
    assert _cn_numeral(99) == "九十九"
    assert _cn_numeral(100) == "100"  # ≥100 回退阿拉伯数字


def test_build_mapping_draft_same_surname_multiple_get_cn_numerals():
    """同姓多个实体：唯一→[袁某] 不加序号；多个→[袁某一]/[袁某二] 汉字序号；
    张三/李四异姓各自唯一→[张某]/[李某]。"""
    ents = [
        Entity(id="a", text="袁吃霄", type="PERSON", start=0, end=3),
        Entity(id="b", text="张三", type="PERSON", start=5, end=7),
        Entity(id="c", text="袁飞", type="PERSON", start=9, end=11),
        Entity(id="d", text="李四", type="PERSON", start=13, end=15),
        Entity(id="e", text="袁吃霄", type="PERSON", start=17, end=20),  # 重复文本去重
    ]
    items = build_mapping_draft(ents)
    got = {m.original_text: m.replacement for m in items}
    assert got["袁吃霄"] == "[袁某一]"
    assert got["袁飞"] == "[袁某二]"
    assert got["张三"] == "[张某]"
    assert got["李四"] == "[李某]"
