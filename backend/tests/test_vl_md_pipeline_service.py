"""VL-MD 管线服务测试(Issue#66 T04-T09):stub NER/VL 于服务边界,不触真实外部服务。

覆盖:成对联动(T04)/队列分派与存量路由不变(T05)/混合 PDF 页级分流(T06)/
T5 diff 四类甄别(T07)/收敛自检≤3轮(T08)/产物三件套与登记(T09)。
"""

import httpx
import json
from types import SimpleNamespace

import pytest

from app.models.entity_schemas import Entity
from app.services import vl_md_pipeline_service as vlm
from app.services.vl_md_pipeline_service import VlMdPipelineService

# ---------- stubs ----------

class StubNER:
    """按调用序号控制上报的 NER 桩。rule: {type, name, only_call: 只在第 idx 次调用上报}"""

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
                out.append(Entity(id=f"s{len(out)}", text=r["name"], type=r["type"],
                                  start=i, end=i + len(r["name"]), source="llm"))
        return out


def _types(*ids):
    return [SimpleNamespace(id=i) for i in ids]


# ---------- T04 成对联动 ----------

def test_linkage_id_card_pulls_birth_date():
    svc = VlMdPipelineService()
    types = svc.resolve_types({"entity_type_ids": ["ID_CARD"]}, None)
    ids = {t.id for t in types}
    assert "ID_CARD" in ids and "BIRTH_DATE" in ids


def test_linkage_not_duplicated_when_birth_date_present():
    svc = VlMdPipelineService()
    types = svc.resolve_types({"entity_type_ids": ["ID_CARD", "BIRTH_DATE"]}, None)
    assert sum(1 for t in types if t.id == "BIRTH_DATE") == 1


def test_default_types_include_all_nine():
    svc = VlMdPipelineService()
    ids = {t.id for t in svc.resolve_types({}, None)}
    assert ids >= {"PERSON", "ORG", "ADDRESS", "CASE_NUMBER", "BIRTH_DATE",
                   "ID_CARD", "PHONE", "BANK_CARD", "LICENSE_PLATE"}


# ---------- T05 队列分派 ----------

@pytest.mark.asyncio
async def test_dispatch_vl_md_mode_completes_item(monkeypatch, tmp_path):
    from app.services.task_queue_pipelines import RecognitionPipelineMixin
    from app.services.job_models import JobItemStatus, JobStatus

    calls = {"vl_md": 0, "ner_or_vision": 0, "completed": 0}

    class FakeStore:
        def get_job(self, job_id):
            return {"id": job_id, "status": JobStatus.PROCESSING.value, "config_json": "{}"}

        def get_item(self, item_id):
            return {"id": item_id, "status": JobItemStatus.PROCESSING.value}

        def update_item_status(self, *a, **k):
            calls["item_status"] = a[1]

        def update_item_progress(self, *a, **k):
            pass

        def update_job_status(self, *a, **k):
            pass

        def touch_job_updated(self, *a, **k):
            pass

        def _clear_outputs_for_file_ids(self, *a, **k):
            pass

    shared_store = FakeStore()

    class Dummy(RecognitionPipelineMixin):
        def _get_store(self):
            return shared_store

        def _record_task_started(self, *a, **k):
            pass

        def _try_update_job_status(self, *a, **k):
            pass

        def _record_item_performance(self, *a, **k):
            pass

        def _refresh_job_status(self, *a, **k):
            pass

        async def _parse_file(self, task):
            pass

        async def _run_vl_md(self, task, cfg):
            calls["vl_md"] += 1

        async def _run_ner_or_vision(self, task, cfg):
            calls["ner_or_vision"] += 1

        def _mark_recognition_complete(self, task, job, store):
            calls["completed"] += 1

    dummy = Dummy()
    task = SimpleNamespace(job_id="j1", item_id="i1", file_id="f1", task_type="recognition")

    # vl_md 模式:走新分支,不进存量链路,条目直达 COMPLETED
    import json as _json
    shared_store.get_job = lambda job_id: {"id": job_id, "status": JobStatus.PROCESSING.value,
                                           "config_json": _json.dumps({"pipeline_mode": "vl_md"})}
    await dummy._run_recognition(task)
    assert calls["vl_md"] == 1 and calls["ner_or_vision"] == 0 and calls["completed"] == 0
    # COMPLETED 由真实 _run_vl_md 内部落状态(此处被 stub),分派层不再走审阅/回写
    assert calls.get("item_status") == JobItemStatus.PROCESSING

    # 存量模式:路由不变
    calls.update({"vl_md": 0, "ner_or_vision": 0, "completed": 0})
    shared_store.get_job = lambda job_id: {"id": job_id, "status": JobStatus.PROCESSING.value,
                                           "config_json": "{}"}
    await dummy._run_recognition(task)
    assert calls["vl_md"] == 0 and calls["ner_or_vision"] == 1 and calls["completed"] == 1


# ---------- T06 混合 PDF 页级分流 ----------

@pytest.mark.asyncio
async def test_build_markdown_mixed_pdf_routes_per_page(tmp_path, monkeypatch):
    import fitz

    pdf_path = tmp_path / "mixed.pdf"
    doc = fitz.open()
    p1 = doc.new_page()
    p1.insert_text((72, 72), "SCAN_PAGE_IMAGE_PLACEHOLDER")
    p2 = doc.new_page()
    p2.insert_text((72, 72), "text layer page two lisi")
    doc.save(str(pdf_path))
    doc.close()

    class StubParser:
        async def is_pdf_page_scanned(self, path, page):
            return page == 1

        async def get_pdf_page_image(self, path, page):
            assert page == 1
            return b"png-bytes"

    class StubVL:
        async def parse(self, path, *, raw=True):
            assert path.endswith(".png")
            with open(path, "rb") as f:
                assert f.read() == b"png-bytes"
            return SimpleNamespace(markdown="# VL 页一", raw_texts=[["块A", "块B"]], elapsed_s=1.0)

    svc = VlMdPipelineService(vl_client=StubVL(), file_parser=StubParser())
    pages, raw_texts, sources = await svc.build_markdown(
        {"file_path": str(pdf_path), "file_type": "pdf"},
    )
    assert pages == ["# VL 页一", "text layer page two lisi\n"]
    assert sources == ["vl", "text"]
    assert raw_texts == [["块A", "块B"]]


# ---------- T07 T5 diff 四类甄别 ----------

@pytest.mark.asyncio
async def test_diff_classifies_covered_superseded_lost_and_heals():
    md = "被告人成龙飞到庭。证人钱七作证。签名:孙八。"
    raw_blocks = [["公诉人成龙到庭。"], ["审判长 周雨晴"], ["证人钱七作证。"]]
    svc = VlMdPipelineService(ner_service=StubNER([
        {"type": "PERSON", "name": "成龙飞", "only_call": 0},
        {"type": "PERSON", "name": "钱七", "only_call": 0},
        {"type": "PERSON", "name": "孙八", "only_call": 0},
        {"type": "PERSON", "name": "成龙", "only_call": 1},      # ⊂成龙飞 → covered
        {"type": "PERSON", "name": "证人钱七", "only_call": 1},  # 含钱七 → superseded
        {"type": "PERSON", "name": "周雨晴", "only_call": 1},    # md 无 → lost 补映射
    ]))
    result = await svc.process(pages=[md], raw_texts=raw_blocks, types=_types("PERSON"))
    diff = {k: sorted(x["text"] for x in v) for k, v in result.diff.items()}
    assert diff["covered"] == ["成龙"]
    assert diff["superseded"] == ["证人钱七"]
    assert diff["lost"] == ["周雨晴"]
    assert diff["self_healed"] == []
    # lost 实体已补进映射表(还原链保全)
    assert "周雨晴" in {t for v in result.mapping.values() for t in v["texts"]}


# ---------- T08 收敛自检 ----------

@pytest.mark.asyncio
async def test_convergence_heals_within_three_rounds():
    # 孙八在 md 中,但 NER 首轮漏报、复检才上报——模拟单轮漏检(POC 实证场景)
    md = "被告人张三,男。证人孙八作证。案号(2024)粤1802刑初131号。"
    rules = [
        {"type": "PERSON", "name": "张三"},
        {"type": "PERSON", "name": "孙八", "only_call": 1},
    ]
    svc = VlMdPipelineService(ner_service=StubNER(rules))
    result = await svc.process(pages=[md], raw_texts=[], types=_types("PERSON", "CASE_NUMBER"))
    assert result.rounds >= 1 and result.rounds <= 3
    assert result.leaks == []
    assert "孙八" not in result.desens_md
    assert "张三" not in result.desens_md


@pytest.mark.asyncio
async def test_no_convergence_needed_rounds_zero():
    md = "被告人张三,男。"
    svc = VlMdPipelineService(ner_service=StubNER([{"type": "PERSON", "name": "张三"}]))
    result = await svc.process(pages=[md], raw_texts=[], types=_types("PERSON"))
    assert result.rounds == 0 and result.leaks == []
    assert result.entity_count == 1


# ---------- T09 产物三件套 ----------

@pytest.mark.asyncio
async def test_process_file_writes_artifacts_and_registers(monkeypatch, tmp_path):
    from app.core.config import settings
    from app.services import file_management_service as fms

    captured = {}

    class FakeStore:
        def update_fields(self, file_id, updates):
            captured["file_id"] = file_id
            captured["updates"] = updates

    monkeypatch.setattr(fms, "file_store", FakeStore())
    monkeypatch.setattr(settings, "OUTPUT_DIR", str(tmp_path))

    md = "被告人张三,身份证号441811197609016414,电话14863794026。"
    svc = VlMdPipelineService(ner_service=StubNER([
        {"type": "PERSON", "name": "张三"},
    ]))
    file_info = {"id": "f1", "file_path": "x.txt", "file_type": "txt",
                 "content": md, "owner_id": "local_user"}
    cfg = {"entity_type_ids": ["PERSON", "ID_CARD", "PHONE"]}
    summary = await svc.process_file(file_info, cfg)

    assert summary["verdict"] == "ZERO-LEAK OK"
    md_out = open(summary["md_path"], encoding="utf-8").read()
    assert "张三" not in md_out and "[PERSON_1]" in md_out
    assert "441811197609016414" not in md_out and "[ID_CARD_1]" in md_out
    mapping = json.load(open(summary["mapping_path"], encoding="utf-8"))
    assert "[PERSON_1]" in mapping and mapping["[PERSON_1]"]["texts"] == ["张三"]
    assert mapping["[PERSON_1]"]["type"] == "PERSON"
    retained = json.load(open(summary["retained_path"], encoding="utf-8"))
    assert retained["policy"] == "issue50-default"
    assert captured["file_id"] == "f1"
    assert captured["updates"]["output_file_id"] == summary["output_file_id"]
    assert captured["updates"]["entity_map"]["[ID_CARD_1]"]["texts"] == ["441811197609016414"]


@pytest.mark.asyncio
async def test_process_file_raises_on_leak(monkeypatch, tmp_path):
    from app.core.config import settings
    from app.services import file_management_service as fms

    class FakeStore:
        def update_fields(self, *a, **k):
            pass

    monkeypatch.setattr(fms, "file_store", FakeStore())
    monkeypatch.setattr(settings, "OUTPUT_DIR", str(tmp_path))

    md = "被告人张三,男。证人赵六到庭。"
    # 赵六只在终态复检(第 3 次调用)上报:收敛轮(第 2 次)没报 → 未替换 → 终态判 LEAK
    svc = VlMdPipelineService(ner_service=StubNER([
        {"type": "PERSON", "name": "张三", "only_call": 0},
        {"type": "PERSON", "name": "赵六", "only_call": 2},
    ]))
    file_info = {"id": "f2", "file_path": "x.txt", "file_type": "txt",
                 "content": md, "owner_id": "local_user"}
    from app.services.vl_md_pipeline_service import VlMdLeakError
    with pytest.raises(VlMdLeakError, match="零泄漏自检未通过"):
        await svc.process_file(file_info, {"entity_type_ids": ["PERSON"]})


# ---------- A4 跨文件一致映射 ----------

@pytest.mark.asyncio
async def test_same_job_cross_file_mapping_consistent(monkeypatch, tmp_path):
    from app.core.config import settings
    from app.services import file_management_service as fms

    captured = {}

    class FakeStore:
        def update_fields(self, file_id, updates):
            captured[file_id] = updates

    monkeypatch.setattr(fms, "file_store", FakeStore())
    monkeypatch.setattr(settings, "OUTPUT_DIR", str(tmp_path))

    svc = VlMdPipelineService(ner_service=StubNER([
        {"type": "PERSON", "name": "张三"},
    ]))
    cfg = {"entity_type_ids": ["PERSON"]}
    for fid, content in (("f1", "被告人张三,男。"), ("f2", "证人张三陈述。另有人名李四。")):
        await svc.process_file({"id": fid, "file_path": "x.txt", "file_type": "txt",
                                "content": content, "owner_id": "local_user"}, cfg, job_id="jobA")
    m1 = captured["f1"]["entity_map"]
    m2 = captured["f2"]["entity_map"]
    # 同一实体跨文件同占位符
    ph = [k for k, v in m1.items() if "张三" in v["texts"]][0]
    assert "张三" in m2[ph]["texts"]
    md2 = open(captured["f2"]["output_path"], encoding="utf-8").read()
    assert ph in md2 and "张三" not in md2


@pytest.mark.asyncio
async def test_different_jobs_get_independent_mappings(monkeypatch, tmp_path):
    from app.core.config import settings
    from app.services import file_management_service as fms

    class FakeStore:
        def update_fields(self, file_id, updates):
            self.last = (file_id, updates)

    store = FakeStore()
    monkeypatch.setattr(fms, "file_store", store)
    monkeypatch.setattr(settings, "OUTPUT_DIR", str(tmp_path))

    svc = VlMdPipelineService(ner_service=StubNER([{"type": "PERSON", "name": "张三"}]))
    info = {"id": "f1", "file_path": "x.txt", "file_type": "txt",
            "content": "被告人张三,男。", "owner_id": "local_user"}
    await svc.process_file(info, {"entity_type_ids": ["PERSON"]}, job_id="jobA")
    ph_a = [k for k, v in store.last[1]["entity_map"].items() if "张三" in v["texts"]][0]
    await svc.process_file(info, {"entity_type_ids": ["PERSON"]}, job_id="jobB")
    ph_b = [k for k, v in store.last[1]["entity_map"].items() if "张三" in v["texts"]][0]
    assert ph_a == ph_b  # 编号从 1 开始,不同 job 同实体同号互不冲突即可


# ---------- 化名词二次识别套娃修复(真实冒烟实证) ----------

@pytest.mark.asyncio
async def test_pseudonym_words_not_re_collected():
    # 法院全名与「人民法院」碎片都被 NER 上报;「人民法院」化名为「某人民法院1」后,
    # 收敛自检不得把化名词再次当实体替换(冒烟实证:法院→某人民法院1→某公司8)
    md = "广东省清远市清城区人民法院刑事判决书。经广东省清远市清城区人民法院审理。"
    svc = VlMdPipelineService(ner_service=StubNER([
        {"type": "ORG", "name": "广东省清远市清城区人民法院"},
        {"type": "ORG", "name": "人民法院", "only_call": 0},
    ]))
    result = await svc.process(pages=[md], raw_texts=[], types=_types("ORG"))
    # 原文与化名合成词都不得被再次收进映射的原文侧(套娃实证:某人民法院1→某公司8)
    originals = [t for v in result.mapping.values() for t in v["texts"]]
    assert not [t for t in originals if t.startswith("某")], f"化名词被二次收集: {originals}"
    # 原文法院名与化名词都不残留在脱敏文
    assert "清城区人民法院" not in result.desens_md
    assert "某公司" not in result.desens_md
    # 法院的替换词(keys)是派生化名「某人民法院N」而非某公司
    keys = [k for k, v in result.mapping.items() if any("人民法院" in t for t in v["texts"])]
    assert keys and all(k.startswith("某人民法院") for k in keys), result.mapping


# ---------- 评审修复回归 ----------

def test_job_context_lru_cap_and_release():
    svc = VlMdPipelineService()
    for n in range(svc.MAX_CACHED_JOB_CONTEXTS + 5):
        svc.context_for_job(f"job{n}", None)
    assert len(svc._job_contexts) == svc.MAX_CACHED_JOB_CONTEXTS
    assert "job0" not in svc._job_contexts  # 最老的被逐出
    svc.release_job_context("job1")
    assert "job1" not in svc._job_contexts


@pytest.mark.asyncio
async def test_same_year_birth_dates_not_collapsed(monkeypatch, tmp_path):
    from app.core.config import settings
    from app.services import file_management_service as fms

    class FakeStore:
        def __init__(self):
            self.last = None

        def update_fields(self, file_id, updates):
            self.last = updates

    store_inst = FakeStore()
    monkeypatch.setattr(fms, "file_store", store_inst)
    monkeypatch.setattr(settings, "OUTPUT_DIR", str(tmp_path))

    md = "甲1976年3月2日出生；乙1976年11月8日出生。"
    svc = VlMdPipelineService(ner_service=StubNER([
        {"type": "BIRTH_DATE", "name": "1976年3月2日"},
        {"type": "BIRTH_DATE", "name": "1976年11月8日"},
    ]))
    info = {"id": "f1", "file_path": "x.txt", "file_type": "txt",
            "content": md, "owner_id": "local_user"}
    await svc.process_file(info, {"entity_type_ids": ["BIRTH_DATE"]}, job_id="jobX")
    mapping = store_inst.last["entity_map"]
    entry = mapping.get("1976年")
    assert entry is not None and len(entry["texts"]) == 2, mapping  # 同值不互相覆盖


@pytest.mark.asyncio
async def test_empty_document_raises(monkeypatch, tmp_path):
    from app.services.vl_md_pipeline_service import VlMdPipelineService as S
    svc = S(ner_service=StubNER([]))
    info = {"id": "f9", "file_path": "x.txt", "file_type": "txt",
            "content": "   ", "owner_id": "local_user"}
    with pytest.raises(ValueError, match="无可解析文本"):
        await svc.process_file(info, {"entity_type_ids": ["PERSON"]}, job_id="j9")


@pytest.mark.asyncio
async def test_parse_429_retried():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, text="slow down")
        return httpx.Response(200, json={"markdown": "ok", "elapsed_s": 1.0})

    async def nosleep(sec):
        return None

    from app.services import vl_parse_client as vpc

    class _NoSleepClient(vpc.VlParseClient):
        async def _sleep(self, sec):
            return None

    orig_sleep = vpc.asyncio.sleep
    vpc.asyncio.sleep = nosleep
    try:
        client = vpc.VlParseClient(base_url="http://127.0.0.1:8095", timeout=5,
                                   transport=httpx.MockTransport(handler))
        result = await client.parse("/tmp/page.png")
        assert result.markdown == "ok" and calls["n"] == 2
    finally:
        vpc.asyncio.sleep = orig_sleep


@pytest.mark.asyncio
async def test_drifted_entities_counted_as_leak():
    # 偏移漂移被跳过的实体=未替换,必须计入泄漏面而非静默豁免
    md = "被告人张三,男。"
    svc = VlMdPipelineService(ner_service=StubNER([{"type": "PERSON", "name": "张三"}]))
    # 构造漂移:给实体的 start 指向错误位置
    class OffsetNER(StubNER):
        async def extract(self, text, types):
            ents = await StubNER.extract(self, text, types)
            return [Entity(id=e.id+"d", text=e.text, type=e.type, start=99, end=99+len(e.text), source="llm") for e in ents]
    svc2 = VlMdPipelineService(ner_service=OffsetNER([{"type": "PERSON", "name": "张三"}]))
    result = await svc2.process(pages=[md], raw_texts=[], types=_types("PERSON"))
    assert "张三" in result.leaks
