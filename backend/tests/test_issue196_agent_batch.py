"""Issue#196 供料批量（agent_batch）：管线 / 确认 / 导出 / 迁移 / 门控。

判据对应验收方案（LexMask-workspace/docs/acceptance/2026-10-19-issue196-供料模式批量-
设计与验收方案.md）§5：校验、迁移、工人管线、确认（含产物内容正确性）、导出、负样本。

隔离策略：auth/file_store 单例整体替换为 tmp_path 新实例；MinerU/NER 用假件
（本地无 GPU——MinerU 正确性由云冒烟 AC8 覆盖，本地只证接线）。
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from app.models.schemas import Entity
from app.services import job_management_service as jms
from app.services import file_management_service as fms
from app.services.agent_md_batch import (
    AGENT_MD_DRAFT_KEY,
    build_text_segments,
    commit_agent_md_item,
    is_mineru_carrier,
    load_draft,
    run_agent_md_stage1,
    serialize_draft,
)
from app.services.agent_md_types import MappingItem, Seg
from app.services.batch_mode_validation import is_file_type_allowed_for_job_type
from app.services.file_store_db import FileStoreDB
from app.services.job_models import JobItemStatus
from app.services.job_store import JobStore, JobType
from app.services.task_queue import SimpleTaskQueue, TaskItem

# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


def _store(tmp_path: Path) -> JobStore:
    return JobStore(str(tmp_path / "jobs.sqlite3"))


@pytest.fixture(autouse=True)
def _sandbox_dirs(tmp_path, monkeypatch):
    """FileParser 路径白名单与导出 OUTPUT_DIR 校验都钉在 settings 目录上，
    测试夹具文件在 tmp_path，须把两个目录指过去（否则「路径不在允许的目录中」）。"""
    from app.core.config import settings

    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "OUTPUT_DIR", str(tmp_path))


def _fake_file_store(tmp_path: Path, monkeypatch) -> FileStoreDB:
    fs = FileStoreDB(str(tmp_path / "file_store.sqlite3"))
    monkeypatch.setattr(fms, "file_store", fs)
    return fs


class FakeNER:
    """按块返回预置实体（start/end 在块内文本上，run_ner 负责平移）。"""

    def __init__(self, spans_by_call: list[list[tuple[str, str, int, int]]]) -> None:
        self.spans_by_call = list(spans_by_call)
        self.calls: list[str] = []

    async def extract(self, text, entity_types):
        self.calls.append(text)
        spans = self.spans_by_call.pop(0) if self.spans_by_call else []
        return [
            Entity(id=f"e{i}", text=text[s:e], type=t, start=s, end=e, page=1)
            for i, (t, _txt, s, e) in enumerate(spans)
        ]


def _register_file(fs: FileStoreDB, file_id: str, path: Path, *, file_type: str = "txt",
                   owner: str = "u1", **extra) -> dict:
    info = {
        "file_id": file_id,
        "filename": path.name,
        "original_filename": path.name,
        "file_path": str(path),
        "file_type": file_type,
        "owner_id": owner,
    }
    info.update(extra)
    fs.set(file_id, info)
    return info


def _make_agent_job(store: JobStore, *, items: int = 1, config: dict | None = None):
    jid = jms.create_job(
        store=store, job_type_str="agent_batch", title="供料批",
        config={"entity_type_ids": ["PERSON", "PHONE"], **(config or {})},
        skip_item_review=False, priority=0,
    )["id"]
    item_ids = [store.add_item(jid, f"file-{i}") for i in range(items)]
    return jid, item_ids


def _queue_with_store(store: JobStore) -> SimpleTaskQueue:
    q = SimpleTaskQueue(concurrency=1)
    q._get_store = lambda: store
    return q


def _run_item(queue: SimpleTaskQueue, jid: str, item_id: str, file_id: str) -> None:
    asyncio.run(queue._run_recognition(
        TaskItem(job_id=jid, item_id=item_id, file_id=file_id, task_type="recognition"),
    ))


def _draft_for(items: list[tuple[str, str]]) -> dict:
    """(text, type) 列表 → 合法 agent_md_draft（segs/entities/mapping 自洽）。

    实体偏移必须按 render_outputs 的全局坐标系累计（段间 '\\n\\n' 恒宽 2），
    与真实管线 run_ner 的 base_offset 平移一致——偏移错会让 span 落错段。
    """
    segs = [Seg(text=t, page_idx=0, source="text") for t, _ in items]
    entities = []
    cursor = 0
    for i, (t, ty) in enumerate(items):
        entities.append(
            Entity(id=f"e{i}", text=t, type=ty, start=cursor, end=cursor + len(t), page=1)
        )
        cursor += len(t) + 2  # _SEP = "\n\n"
    mapping = [
        MappingItem(id=f"m{i}", original_text=t, entity_type=ty, replacement=f"[占位{i}]")
        for i, (t, ty) in enumerate(items)
    ]
    return serialize_draft(segs, entities, mapping, carrier="text")


# ---------------------------------------------------------------------------
# 1) 校验：格式白名单 + 化名门控三入口
# ---------------------------------------------------------------------------


def test_agent_batch_allows_all_file_types(tmp_path):
    for ft in ("pdf", "pdf_scanned", "image", "docx", "doc", "txt", "md", "html", "rtf"):
        assert is_file_type_allowed_for_job_type(JobType.AGENT_BATCH, ft), ft
    assert not is_file_type_allowed_for_job_type("no_such_type", "pdf")


def test_agent_batch_pseudonym_gate_three_entrances(tmp_path):
    """供料批与普通批同受化名门控（JMS 门控不区分 job_type，钉住该语义）。"""
    store = _store(tmp_path)
    with pytest.raises(ValueError, match="暂未上线"):
        jms.create_job(store=store, job_type_str="agent_batch", title="t",
                       config={"replacement_mode": "pseudonym"}, skip_item_review=False, priority=0)
    jid = jms.create_job(store=store, job_type_str="agent_batch", title="t",
                         config={"replacement_mode": "placeholder"}, skip_item_review=False, priority=0)["id"]
    with pytest.raises(ValueError, match="暂未上线"):
        jms.update_draft(store, jid, {"config": {"replacement_mode": "pseudonym"}})
    with pytest.raises(ValueError, match="暂未上线"):
        jms.update_draft(store, jid, {"replacement_mode": "pseudonym"})
    # 提交口兜底：绕过 create 直写库的存量 pseudonym 草稿
    jid2 = store.create_job(job_type=JobType.AGENT_BATCH, title="legacy",
                            config={"replacement_mode": "pseudonym"}, owner_id="u1")
    store.add_item(jid2, "f1")
    with pytest.raises(ValueError, match="暂未上线"):
        jms.submit_job(store, jid2)


# ---------------------------------------------------------------------------
# 2) 迁移：旧 CHECK 库 → agent_batch 可写 + 幂等 + 存量保留
# ---------------------------------------------------------------------------


def test_jobstore_migrates_old_check_and_preserves_rows(tmp_path):
    db = tmp_path / "legacy.sqlite3"
    old_check = "('text_batch','image_batch','smart_batch','structured_batch','single')"
    conn = sqlite3.connect(db)
    conn.execute(f"""
        CREATE TABLE jobs (
            id TEXT PRIMARY KEY,
            job_type TEXT NOT NULL CHECK(job_type IN {old_check}),
            title TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL,
            skip_item_review INTEGER NOT NULL DEFAULT 0,
            config_json TEXT NOT NULL DEFAULT '{{}}',
            priority INTEGER NOT NULL DEFAULT 0,
            owner_id TEXT NOT NULL DEFAULT 'local_user',
            error_message TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    conn.execute(
        "INSERT INTO jobs (id, job_type, status, created_at, updated_at) "
        "VALUES ('legacy-1', 'text_batch', 'draft', '', '')"
    )
    conn.commit()
    conn.close()

    store = JobStore(str(db))  # __init__ 触发探针 → 重建
    # 存量行保留 + 新类型可写（两轮启动幂等）
    assert store.get_job("legacy-1")["job_type"] == "text_batch"
    jid = store.create_job(job_type=JobType.AGENT_BATCH, title="t", config={}, owner_id="u1")
    assert store.get_job(jid)["job_type"] == "agent_batch"
    store2 = JobStore(str(db))
    assert store2.get_job(jid)["job_type"] == "agent_batch"


# ---------------------------------------------------------------------------
# 3) 工人管线：分流 / 草稿落盘 / 失败隔离 / 幂等短路
# ---------------------------------------------------------------------------


def test_worker_txt_item_reaches_awaiting_review_with_draft(tmp_path, monkeypatch):
    store = _store(tmp_path)
    fs = _fake_file_store(tmp_path, monkeypatch)
    jid, item_ids = _make_agent_job(store, items=1)
    doc = tmp_path / "a.txt"
    doc.write_text("张三的电话是13800000001，住址在北京市。", encoding="utf-8")
    _register_file(fs, "file-0", doc)

    monkeypatch.setattr(
        "app.services.hybrid_ner_service.HybridNERService",
        lambda: FakeNER([[("PERSON", "", 0, 2), ("PHONE", "", 6, 17)]]),
    )
    _run_item(_queue_with_store(store), jid, item_ids[0], "file-0")

    item = store.get_item(item_ids[0])
    assert item["status"] == "awaiting_review"
    info = fs.get("file-0")
    draft = load_draft(info)
    assert draft and draft["carrier"] == "text"
    texts = {m["original_text"] for m in draft["mapping"]}
    assert "张三" in texts and "13800000001" in texts


def test_worker_mineru_carrier_uses_adapter_not_fileparser(tmp_path, monkeypatch):
    store = _store(tmp_path)
    fs = _fake_file_store(tmp_path, monkeypatch)
    jid, item_ids = _make_agent_job(store, items=1)
    pdf = tmp_path / "b.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    _register_file(fs, "file-0", pdf, file_type="pdf_scanned", is_scanned=True)

    calls = {"mineru": 0}

    async def fake_mineru_segments(file_info, *, ocr=None, on_progress=None):
        calls["mineru"] += 1
        if on_progress:
            on_progress(1, 3)
        return [Seg(text="扫描页文本李四", page_idx=0, source="text")]

    monkeypatch.setattr("app.services.agent_md_batch.build_mineru_segments", fake_mineru_segments)
    monkeypatch.setattr(
        "app.services.hybrid_ner_service.HybridNERService",
        lambda: FakeNER([[("PERSON", "", 5, 7)]]),
    )
    _run_item(_queue_with_store(store), jid, item_ids[0], "file-0")

    assert calls["mineru"] == 1
    assert store.get_item(item_ids[0])["status"] == "awaiting_review"
    assert load_draft(fs.get("file-0"))["carrier"] == "mineru"


def test_worker_failure_isolates_items(tmp_path, monkeypatch):
    store = _store(tmp_path)
    fs = _fake_file_store(tmp_path, monkeypatch)
    jid, item_ids = _make_agent_job(store, items=2)
    good = tmp_path / "good.txt"
    good.write_text("王五的电话是13900000002。", encoding="utf-8")
    _register_file(fs, "file-0", good)
    bad = tmp_path / "missing.pdf"  # 不创建实体文件：MinerU 件 stage1 读字节即抛 → 该件 FAILED
    _register_file(fs, "file-1", bad, file_type="pdf")

    monkeypatch.setattr(
        "app.services.hybrid_ner_service.HybridNERService",
        lambda: FakeNER([[("PERSON", "", 0, 2)]]),
    )
    q = _queue_with_store(store)
    for i, fid in zip(item_ids, ("file-0", "file-1")):
        _run_item(q, jid, i, fid)

    assert store.get_item(item_ids[0])["status"] == "awaiting_review"
    failed = store.get_item(item_ids[1])
    assert failed["status"] == "failed"
    assert "file" in (failed.get("error_message") or "").lower()


def test_worker_short_circuits_when_draft_exists(tmp_path, monkeypatch):
    """watchdog 重入队/重试：已有合法草稿则不重跑解析（MinerU 调用计数不增）。"""
    store = _store(tmp_path)
    fs = _fake_file_store(tmp_path, monkeypatch)
    jid, item_ids = _make_agent_job(store, items=1)
    doc = tmp_path / "c.txt"
    doc.write_text("赵六，13811112222。", encoding="utf-8")
    _register_file(fs, "file-0", doc, **{AGENT_MD_DRAFT_KEY: _draft_for([("赵六", "PERSON")])})

    mineru_calls = {"n": 0}

    async def fail_if_called(*a, **k):
        mineru_calls["n"] += 1
        raise AssertionError("stage1 must be short-circuited when draft exists")

    monkeypatch.setattr("app.services.agent_md_batch.build_mineru_segments", fail_if_called)
    ner_calls = {"n": 0}

    class CountingNER(FakeNER):
        async def extract(self, text, entity_types):
            ner_calls["n"] += 1
            return await super().extract(text, entity_types)

    monkeypatch.setattr(
        "app.services.hybrid_ner_service.HybridNERService", lambda: CountingNER([[]]),
    )
    _run_item(_queue_with_store(store), jid, item_ids[0], "file-0")
    assert mineru_calls["n"] == 0
    assert ner_calls["n"] == 0  # NER 同样短路（整段 Stage1 复用）
    assert store.get_item(item_ids[0])["status"] == "awaiting_review"


# ---------------------------------------------------------------------------
# 4) 确认：产物内容正确性 / 一键确认 / reject 重识别 / 幂等 / 负样本
# ---------------------------------------------------------------------------


def _seed_completed_stage(fs: FileStoreDB, fid: str, path: Path, items) -> dict:
    info = _register_file(fs, fid, path)
    info[AGENT_MD_DRAFT_KEY] = _draft_for(items)
    fs.set(fid, info)
    return info


def test_commit_review_applies_three_kinds_of_decisions(tmp_path, monkeypatch):
    """内容级断言：替换→占位符、保留→原文、自定义→自定义值；mapping/retained 与决策一致。"""
    store = _store(tmp_path)
    fs = _fake_file_store(tmp_path, monkeypatch)
    jid, item_ids = _make_agent_job(store, items=1)
    doc = tmp_path / "d.txt"
    doc.write_text("占位", encoding="utf-8")
    _seed_completed_stage(fs, "file-0", doc, [("张三", "PERSON"), ("李四", "PERSON"), ("13800000001", "PHONE")])

    decisions = [
        {"id": "m0", "action": "custom", "replacement": "【化名甲】"},
        {"id": "m1", "action": "exclude"},
        # m2 缺省 = 默认占位符替换
    ]
    out = asyncio.run(jms.commit_review(
        store=store, job_id=jid, item_id=item_ids[0],
        entities=[], bounding_boxes=[],
        payload={"entities": [], "bounding_boxes": [], "agent_md_decisions": decisions},
        reviewer="u1",
    ))
    assert out["status"] == "completed"

    info = fs.get("file-0")
    md = Path(info["output_path"]).read_text(encoding="utf-8")
    assert "【化名甲】" in md
    assert "李四" in md                      # 保留原文
    assert "[占位2]" in md                    # m2 缺省 → 草稿默认占位符
    assert "13800000001" not in md and "张三" not in md

    mapping_json = json.loads(Path(info["vl_md_meta"]["mapping_path"]).read_text(encoding="utf-8"))
    by_text = {m["original_text"]: m for m in mapping_json["items"]}
    assert by_text["张三"]["replacement"] == "【化名甲】" and by_text["张三"]["excluded"] is False
    assert by_text["李四"]["excluded"] is True
    retained = json.loads(Path(info["vl_md_meta"]["retained_path"]).read_text(encoding="utf-8"))
    retained_texts = [r["text"] for r in retained["retained_fields"]]
    assert "李四" in retained_texts and "张三" not in retained_texts
    # 单文件同构契约：mapping JSON 键集一致（还原 round-trip 兼容）
    assert set(mapping_json.keys()) == {"items"}
    assert set(retained.keys()) == {"retained_fields"}


def test_commit_review_without_decisions_defaults_full_replace(tmp_path, monkeypatch):
    store = _store(tmp_path)
    fs = _fake_file_store(tmp_path, monkeypatch)
    jid, item_ids = _make_agent_job(store, items=1)
    doc = tmp_path / "e.txt"
    doc.write_text("占位", encoding="utf-8")
    _seed_completed_stage(fs, "file-0", doc, [("孙七", "PERSON")])
    asyncio.run(jms.commit_review(
        store=store, job_id=jid, item_id=item_ids[0],
        entities=[], bounding_boxes=[], payload={"entities": [], "bounding_boxes": []},
    ))
    md = Path(fs.get("file-0")["output_path"]).read_text(encoding="utf-8")
    assert "孙七" not in md and "[占位0]" in md  # 无草稿决策 = 按草稿默认占位符全替换


def test_commit_review_missing_draft_raises_and_keeps_awaiting(tmp_path):
    store = _store(tmp_path)
    fs = FileStoreDB(str(tmp_path / "fs.sqlite3"))
    jid, item_ids = _make_agent_job(store, items=1)
    doc = tmp_path / "f.txt"
    doc.write_text("x", encoding="utf-8")
    _register_file(fs, "file-0", doc)  # 无草稿
    with pytest.raises(Exception):
        asyncio.run(jms.commit_review(
            store=store, job_id=jid, item_id=item_ids[0],
            entities=[], bounding_boxes=[], payload={},
        ))
    assert store.get_item(item_ids[0])["status"] == "awaiting_review"


def test_commit_review_idempotent_after_completed(tmp_path, monkeypatch):
    store = _store(tmp_path)
    fs = _fake_file_store(tmp_path, monkeypatch)
    jid, item_ids = _make_agent_job(store, items=1)
    doc = tmp_path / "g.txt"
    doc.write_text("x", encoding="utf-8")
    _seed_completed_stage(fs, "file-0", doc, [("周八", "PERSON")])
    kw = dict(store=store, job_id=jid, item_id=item_ids[0], entities=[],
              bounding_boxes=[], payload={})
    asyncio.run(jms.commit_review(**kw))
    out2 = asyncio.run(jms.commit_review(**kw))
    assert out2["status"] == "completed"


def test_commit_all_renders_every_awaiting_item(tmp_path, monkeypatch):
    store = _store(tmp_path)
    fs = _fake_file_store(tmp_path, monkeypatch)
    jid, item_ids = _make_agent_job(store, items=2)
    for i, fid in enumerate(("file-0", "file-1")):
        doc = tmp_path / f"h{i}.txt"
        doc.write_text("x", encoding="utf-8")
        _seed_completed_stage(fs, fid, doc, [(f"同姓{i}", "PERSON")])

    enqueued: list[tuple] = []
    monkeypatch.setattr(jms, "enqueue_task", lambda *a, **k: enqueued.append(a))
    # 状态推到 awaiting_review（模拟识别完成）
    for iid in item_ids:
        store.update_item_status(iid, JobItemStatus.AWAITING_REVIEW)

    result = asyncio.run(jms.commit_all_reviews(store, jid, reviewer="u1"))
    assert result["confirmed"] == 2 and result["failed"] == []
    assert enqueued == []  # 供料一键确认绝不入队 redaction
    for iid in item_ids:
        assert store.get_item(iid)["status"] == "completed"
    # 重复调用幂等（双击不双产）
    result2 = asyncio.run(jms.commit_all_reviews(store, jid, reviewer="u1"))
    assert result2["total_awaiting"] == 0 and result2["confirmed"] == 0


def test_reject_review_clears_draft_and_reenqueues(tmp_path, monkeypatch):
    store = _store(tmp_path)
    fs = _fake_file_store(tmp_path, monkeypatch)
    jid, item_ids = _make_agent_job(store, items=1)
    doc = tmp_path / "i.txt"
    doc.write_text("x", encoding="utf-8")
    _seed_completed_stage(fs, "file-0", doc, [("吴九", "PERSON")])
    store.update_item_status(item_ids[0], JobItemStatus.AWAITING_REVIEW)

    enqueued: list[tuple] = []
    monkeypatch.setattr(jms, "enqueue_task", lambda *a, **k: enqueued.append(a))
    jms.reject_review(store, jid, item_ids[0], reviewer="u1")
    assert load_draft(fs.get("file-0")) is None  # 草稿已清 → 重跑 stage1
    assert enqueued and enqueued[0][0] == "recognition"
    assert store.get_item(item_ids[0])["status"] in ("pending", "queued")  # 重识别队列态


def test_zero_entities_still_produces_artifacts(tmp_path, monkeypatch):
    store = _store(tmp_path)
    fs = _fake_file_store(tmp_path, monkeypatch)
    jid, item_ids = _make_agent_job(store, items=1)
    doc = tmp_path / "j.txt"  # 空文件：0 字节
    doc.write_text("", encoding="utf-8")
    _register_file(fs, "file-0", doc)
    monkeypatch.setattr(
        "app.services.hybrid_ner_service.HybridNERService", lambda: FakeNER([]),
    )
    _run_item(_queue_with_store(store), jid, item_ids[0], "file-0")
    assert store.get_item(item_ids[0])["status"] == "awaiting_review"

    asyncio.run(jms.commit_review(
        store=store, job_id=jid, item_id=item_ids[0],
        entities=[], bounding_boxes=[], payload={},
    ))
    info = fs.get("file-0")
    assert Path(info["output_path"]).exists()
    assert json.loads(Path(info["vl_md_meta"]["mapping_path"]).read_text(encoding="utf-8"))["items"] == []


def test_same_stem_artifacts_do_not_collide(tmp_path):
    fs = FileStoreDB(str(tmp_path / "fs.sqlite3"))
    a = tmp_path / "同名.txt"
    b = tmp_path / "sub" / "同名.txt"
    a.parent.mkdir(exist_ok=True)
    b.parent.mkdir(parents=True, exist_ok=True)
    a.write_text("x", encoding="utf-8")
    b.write_text("x", encoding="utf-8")
    for fid, p in (("fa", a), ("fb", b)):
        info = _register_file(fs, fid, p)
        info[AGENT_MD_DRAFT_KEY] = _draft_for([("钱十", "PERSON")])
        fs.set(fid, info)
        patch = commit_agent_md_item(fs.get(fid), [])  # 单元调用：补丁由调用方落库（同 JMS）
        cur = fs.get(fid)
        cur.update(patch)
        fs.set(fid, cur)
    names = {Path(fs.get(f)["output_path"]).name for f in ("fa", "fb")}
    assert len(names) == 2  # ts 分钟级 + uuid 短缀，重名不互撞


def test_duplicate_filenames_in_zip_get_unique_arcnames(tmp_path, monkeypatch):
    from app.services.file_management_service import collect_batch_zip_entries
    from app.models.file_schemas import BatchDownloadRequest

    fs = _fake_file_store(tmp_path, monkeypatch)
    for i, fid in enumerate(("fa", "fb")):
        p = tmp_path / f"s{i}.md"
        p.write_text("md", encoding="utf-8")
        mp = tmp_path / f"s{i}_映射表.json"
        mp.write_text("{}", encoding="utf-8")
        rp = tmp_path / f"s{i}_保留清单.json"
        rp.write_text("{}", encoding="utf-8")
        info = _register_file(fs, fid, p, original_filename=p.name,
                              output_path=str(p),
                              vl_md_meta={"mapping_path": str(mp), "retained_path": str(rp)})
        fs.set(fid, info)
    req = BatchDownloadRequest(file_ids=["fa", "fb"], redacted=True)
    pairs, manifest = collect_batch_zip_entries(req, owner_id="u1")
    arcnames = [a for _p, a, _s in pairs]
    assert len(pairs) == 6  # 每件 md+映射表+保留清单
    assert len(set(arcnames)) == 6  # 归档名两两不撞
    assert manifest["included_count"] == 6
    assert sum(1 for x in manifest["included"] if x.get("agent_artifact") == "mapping_path") == 2


def test_export_without_vl_md_meta_unchanged(tmp_path, monkeypatch):
    from app.services.file_management_service import collect_batch_zip_entries
    from app.models.file_schemas import BatchDownloadRequest

    fs = _fake_file_store(tmp_path, monkeypatch)
    p = tmp_path / "normal.pdf"
    p.write_text("pdf", encoding="utf-8")
    _register_file(fs, "file-0", p, output_path=str(p))
    req = BatchDownloadRequest(file_ids=["file-0"], redacted=True)
    pairs, manifest = collect_batch_zip_entries(req, owner_id="u1")
    assert len(pairs) == 1 and manifest["included_count"] == 1


# ---------------------------------------------------------------------------
# 5) 杂项：分流判据 / 文本分段
# ---------------------------------------------------------------------------


def test_carrier_routing_and_text_segments():
    assert is_mineru_carrier({"file_type": "pdf"})
    assert is_mineru_carrier({"file_type": "pdf_scanned"})
    assert is_mineru_carrier({"file_type": "image"})
    assert not is_mineru_carrier({"file_type": "docx"})
    assert not is_mineru_carrier({"file_type": "txt"})
    segs = build_text_segments("第一页\n\n第二页", ["第一页", "第二页"])
    assert [s.page_idx for s in segs] == [0, 1]
    single = build_text_segments("整文", [])
    assert len(single) == 1 and single[0].source == "text"
    assert build_text_segments("", []) == []


# ---------------------------------------------------------------------------
# 6) 映射表读取端点（get_agent_md_mapping）
# ---------------------------------------------------------------------------


def test_get_agent_md_mapping_merges_saved_decisions(tmp_path, monkeypatch):
    store = _store(tmp_path)
    fs = _fake_file_store(tmp_path, monkeypatch)
    jid, item_ids = _make_agent_job(store, items=1)
    doc = tmp_path / "k.txt"
    doc.write_text("x", encoding="utf-8")
    _seed_completed_stage(fs, "file-0", doc, [("张三", "PERSON"), ("13800000001", "PHONE")])

    out = jms.get_agent_md_mapping(store, jid, item_ids[0])
    assert [m["id"] for m in out["items"]] == ["m0", "m1"]
    assert all(m["excluded"] is False for m in out["items"])

    store.save_item_review_draft(item_ids[0], {
        "entities": [], "bounding_boxes": [],
        "agent_md_decisions": [{"id": "m1", "action": "exclude"}],
    })
    out2 = jms.get_agent_md_mapping(store, jid, item_ids[0])
    by_id = {m["id"]: m for m in out2["items"]}
    assert by_id["m1"]["excluded"] is True and by_id["m0"]["excluded"] is False


def test_get_agent_md_mapping_rejects_non_agent_job(tmp_path):
    store = _store(tmp_path)
    jid = jms.create_job(store=store, job_type_str="text_batch", title="t",
                         config={"entity_type_ids": ["PERSON"]}, skip_item_review=False, priority=0)["id"]
    item_id = store.add_item(jid, "file-0")
    from app.models.errors import NotFoundError
    with pytest.raises(NotFoundError):
        jms.get_agent_md_mapping(store, jid, item_id)


def test_get_agent_md_mapping_conflict_when_not_ready(tmp_path, monkeypatch):
    store = _store(tmp_path)
    fs = _fake_file_store(tmp_path, monkeypatch)
    jid, item_ids = _make_agent_job(store, items=1)
    doc = tmp_path / "l.txt"
    doc.write_text("x", encoding="utf-8")
    _register_file(fs, "file-0", doc)  # 无草稿 = 识别未完成
    from app.models.errors import ConflictError
    with pytest.raises(ConflictError):
        jms.get_agent_md_mapping(store, jid, item_ids[0])
