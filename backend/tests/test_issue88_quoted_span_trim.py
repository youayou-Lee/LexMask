"""Issue#88:实体 span 含中文引号时引号被吞 + 姓氏派生退化「某人N」+ 同人异名。

验收方案:workspace docs/plans/2026-10-10-issue88-quoted-name-fix-and-acceptance.md。
T1-T5/T10-T13 为管线级集成测试(mock NER,纯逻辑本地跑);T6 为派生层直测;
T8/T9 为护栏(改动前后行为一致)。
"""

import re

from app.models.common import ReplacementMode
from app.models.entity_schemas import Entity
from app.services.redaction.replacement_strategy import RedactionContext
from app.services.vl_md_pipeline_service import VlMdPipelineService
from app.services.word_pool_service import load_default_pools

POOLS = load_default_pools()
assert POOLS["PERSON"]["strategy"] == "derived", "前提:PERSON 默认 derived 策略"

NAME_LIKE = re.compile(r"^(某[人公]|[\u4e00-\u9fff]某)\d+$")


class StubNER:
    """按规则上报实体;only_call 控制只在第 idx 次 extract 调用上报(0 起)。"""

    def __init__(self, rules):
        self.rules = rules
        self.call_idx = -1

    async def extract(self, text, types):
        self.call_idx += 1
        out = []
        for r in self.rules:
            if r.get("only_call") is not None and self.call_idx != r["only_call"]:
                continue
            i = text.find(r["name"])
            if i != -1 and all(o.text != r["name"] for o in out):
                out.append(Entity(id=f"s{len(out)}", text=r["name"], type=r.get("type", "PERSON"),
                                  start=i, end=i + len(r["name"]), source="has"))
        return out


def _svc(ner):
    return VlMdPipelineService(vl_client=object(), ner_service=ner, file_parser=object())


def _process(md, rules, raw_texts=None, context=None):
    import asyncio
    coro = _svc(StubNER(rules)).process(
        pages=[md], raw_texts=raw_texts or [], types=[],
        word_pools=POOLS,
        context=context or RedactionContext(ReplacementMode.PSEUDONYM, word_pools=POOLS))
    return asyncio.run(coro)


# ---------- T1/T2 引号保留 ----------

def test_t1_paired_quotes_preserved_and_surname_kept():
    res = _process('一、证人“李四”证实。', [{"name": "“李四”"}])
    assert res.desens_md == "一、证人“李某1”证实。", res.desens_md


def test_t2_trailing_quote_preserved():
    res = _process('二、被告人张三”供述。', [{"name": "张三”"}])
    assert res.desens_md == "二、被告人张某1”供述。", res.desens_md


# ---------- T3/T5 同人同名 / custom_map ----------

def test_t3_same_person_bare_and_quoted_merge_to_one_name():
    res = _process('张三到场。“张三”随后离开。', [{"name": "张三"}, {"name": "“张三”"}])
    md = res.desens_md
    names = set(re.findall(r"[\u4e00-\u9fff]某\d+", md))
    assert len(names) == 1, (md, names)
    entry = next(iter(res.mapping.values()))
    assert sorted(entry["texts"]) == ["张三"], entry
    assert all("“" not in t and "”" not in t for t in entry["texts"]), entry


def test_t5_custom_map_hit_for_quoted_span():
    pools = {"PERSON": {"words": [], "strategy": "derived",
                        "custom_map": {"李四": "李某"}}}
    ctx = RedactionContext(ReplacementMode.PSEUDONYM, word_pools=pools)
    res = _svc(StubNER([{"name": "“李四”"}])).process(
        pages=['证人“李四”出庭。'], raw_texts=[[]], types=[],
        word_pools=pools, context=ctx)
    import asyncio
    res = asyncio.run(res)
    assert res.desens_md == "证人“李某”出庭。", res.desens_md


# ---------- T6 派生层防御直测 ----------

def test_t6_derived_base_strips_quotes():
    assert RedactionContext._derived_base("PERSON", "“王五”") == "王某"
    assert RedactionContext._derived_base("PERSON", "王五") == "王某"


# ---------- T7 ASCII 引号 ----------

def test_t7_ascii_quotes_stripped():
    res = _process('证人"李四"出庭。', [{"name": '"李四"'}])
    # 引号保留在产物里,人名本体替换化名
    assert res.desens_md == '证人"李某1"出庭。', res.desens_md


# ---------- T4 全引号空 span ----------

def test_t4_all_quote_span_dropped():
    res = _process('证人“”出庭。', [{"name": "“”", "type": "PERSON"}])
    assert "某人" not in res.desens_md and "李某" not in res.desens_md
    assert res.leaks == [] or all(not NAME_LIKE.match(x) for x in res.leaks)


# ---------- T8 书名号护栏(改动前后行为一致) ----------

def test_t8_book_brackets_not_trimmed():
    assert RedactionContext._derived_base("PERSON", "《王五》") == "某人"  # 现状即不剥,基线保持


# ---------- T9 与 regex 占位重叠(护栏) ----------

def test_t9_quoted_id_card_overlap_regex_claim_dropped():
    res = _process(
        '号码110101199003074258在册。',
        [{"name": "110101199003074258", "type": "ID_CARD"},
         {"name": "“110101199003074258”", "type": "PERSON", "only_call": 0}],
        )
    # 原号不残留;regex 占位胜出,化名档生成格式虚构号(非原文)
    assert "110101199003074258" not in res.desens_md
    assert re.search(r"(?<!\d)\d{17}[\dXx](?!\d)", res.desens_md), res.desens_md


# ---------- T10 replace() 路径两路一致 ----------

def test_t10_self_heal_path_preserves_quotes():
    # md 侧 NER 第一次调用不上报;raw_texts 侧才发现 → 走 diff self_heal(desens.replace)
    res = _process(
        '他说“李四”是证人。',
        [{"name": "“李四”", "only_call": 1}],
        raw_texts=[['他说“李四”是证人。']],
        )
    assert res.desens_md == "他说“李某1”是证人。", res.desens_md
    # diff 记录剥引号后的实体文本;引号留在产物中即为两路一致
    assert any(d["text"] == "李四" for d in res.diff["self_healed"]), res.diff


# ---------- T11 收敛不二次改写 ----------

def test_t11_convergence_does_not_rewrite_pseudonym():
    res = _process('“张三”到庭。', [{"name": "“张三”"}])
    assert "“张某1”" in res.desens_md
    assert res.leaks == [], res.leaks
    assert res.rounds <= 1, res.rounds


# ---------- T12 跨文件同人归并 ----------

def test_t12_cross_file_same_person_single_name():
    ctx = RedactionContext(ReplacementMode.PSEUDONYM, word_pools=POOLS)
    import asyncio
    r1 = asyncio.run(_svc(StubNER([{"name": "张三"}])).process(
        pages=['文件一:张三签名。'], raw_texts=[[]], types=[],
        word_pools=POOLS, context=ctx))
    r2 = asyncio.run(_svc(StubNER([{"name": "“张三”"}])).process(
        pages=['文件二:“张三”捺印。'], raw_texts=[[]], types=[],
        word_pools=POOLS, context=ctx))
    n1 = set(re.findall(r"[\u4e00-\u9fff]某\d+", r1.desens_md))
    n2 = set(re.findall(r"[\u4e00-\u9fff]某\d+", r2.desens_md))
    assert n1 == n2 and len(n1) == 1, (n1, n2)


# ---------- T13 序号不撞车 ----------

def test_t13_seq_no_collision():
    res = _process('张三丰与“张三”对峙。', [{"name": "张三丰"}, {"name": "“张三”"}])
    md = res.desens_md
    names = sorted(re.findall(r"张某\d+", md))
    assert len(names) == 2 and len(set(names)) == 2, md
