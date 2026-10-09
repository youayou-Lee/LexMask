"""管线状态机测试：全 fake 依赖，无网络无真实模型。

Stage1 会真实读取 file_path 字节（生产契约），故 fixture 用 fitz 造 1 页最小 PDF，
而非 brief 草稿里的 /tmp/a.pdf（不存在，read_bytes 必炸）。
fake Mineru 返回 **v2 嵌套真形态**（Task 0 §6c：外层按页 list-of-lists，文本在
block["content"]["paragraph_content"][i]["content"]，无 page_idx）——终审修复后
Stage1 走 normalize_content_list 归一化，此处覆盖 C1 接线端到端。
"""
import fitz
import pytest

from app.services.agent_md_pipeline_service import AgentMdPipelineService
from app.services.agent_md_types import TaskState
from app.services.mineru_parse_client import MineruResult


def _v2_nested_one_para():
    """真实 sidecar v2 结构复刻（内容合成）：单页单段落块。"""
    return [
        [
            {
                "type": "paragraph",
                "content": {
                    "paragraph_content": [
                        {"type": "text", "content": "张三借李四人民币一万元"}
                    ]
                },
                "bbox": [112, 75, 566, 94],
            }
        ]
    ]


class _FakeMineru:
    async def submit(self, file_bytes, filename):
        return "job-1"

    async def wait_result(self, task_id, on_progress=None):
        if on_progress:
            on_progress(1, 1)
        return MineruResult(md="# 合成", content_list_v2=_v2_nested_one_para(), images={})


class _FakeOcr:
    def extract_text_boxes(self, image_bytes):
        return []


class _FakeNer:
    async def extract(self, text, entity_types):
        from app.models.entity_schemas import Entity

        ents = []
        for name in ("张三", "李四"):
            idx = text.find(name)
            if idx >= 0:
                ents.append(Entity(id=name, text=name, type="PERSON", start=idx, end=idx + 2))
        return ents


@pytest.fixture
def sample_pdf(tmp_path):
    """1 页最小真实 PDF：Stage1 真实读文件字节 + fitz 数页数。"""
    p = tmp_path / "a.pdf"
    doc = fitz.open()
    doc.new_page()
    doc.save(str(p))
    doc.close()
    return str(p)


@pytest.fixture
def service(tmp_path):
    return AgentMdPipelineService(
        mineru_client=_FakeMineru(), ocr=_FakeOcr(), ner=_FakeNer(),
        entity_types=[], output_dir=str(tmp_path),
    )


def test_stage1_reaches_mapping_ready(service, sample_pdf):
    task = service.create_task_nowait(sample_pdf, "a.pdf")  # 同步驱动版
    assert task.state == TaskState.MAPPING_READY
    assert [m.replacement for m in task.mapping] == ["[张某]", "[李某]"]
    assert task.pages_total == 1


def test_stage2_confirm_writes_artifacts(service, sample_pdf, tmp_path):
    task = service.create_task_nowait(sample_pdf, "a.pdf")
    done = service.confirm_nowait(task.task_id, [{"id": "e1", "action": "exclude"}])
    assert done.state == TaskState.COMPLETED
    md_files = list(tmp_path.glob("a_脱敏MD_*.md"))
    assert len(md_files) == 1
    assert "张三借[李某]" in md_files[0].read_text()
    assert list(tmp_path.glob("a_映射表_*.json"))
    assert list(tmp_path.glob("a_保留清单_*.json"))


def test_stage2_confirm_friendly_filenames(service, sample_pdf, tmp_path):
    """导出文件名 = 原文件名前缀 + 类型 + 时间 + short4（非随机 UUID 名）。"""
    import re as _re

    task = service.create_task_nowait(sample_pdf, "我的 案卷:借条/ v2.pdf")
    done = service.confirm_nowait(task.task_id, [])
    assert done.state == TaskState.COMPLETED
    md_files = list(tmp_path.glob("*.md"))
    assert len(md_files) == 1
    name = md_files[0].name
    assert name.startswith("我的 案卷_借条_ v2_脱敏MD_")
    assert _re.fullmatch(
        r"我的 案卷_借条_ v2_脱敏MD_\d{8}_\d{4}_[0-9a-f]{4}\.md", name), name
    # file_store 登记的 filename 与落盘名一致（下载走 Content-Disposition）
    from app.services import file_management_service as fms

    try:
        assert fms.file_store[done.output_file_id]["filename"] == name
        assert fms.file_store[done.output_file_id]["output_path"] == str(md_files[0])
    finally:
        fms.file_store.pop(done.output_file_id, None)


def test_stage2_confirm_registers_vl_md_meta_key(service, sample_pdf):
    """C3：file_store 登记键必须是 ``vl_md_meta``——main.py orphan 清理
    （_known_file_store_paths）只认 output_path + vl_md_meta，键名错则
    mapping/retained 两份产物在孤儿清理窗口后被删。"""
    import os

    from app.services import file_management_service as fms

    task = service.create_task_nowait(sample_pdf, "a.pdf")
    done = service.confirm_nowait(task.task_id, [])
    try:
        info = fms.file_store[done.output_file_id]
        assert "agent_md_meta" not in info  # 旧键名已废除
        meta = info["vl_md_meta"]
        assert set(meta) == {"mapping_path", "retained_path"}
        assert os.path.exists(meta["mapping_path"])
        assert os.path.exists(meta["retained_path"])
    finally:
        fms.file_store.pop(done.output_file_id, None)  # 不污染全局 store


def test_unknown_task_returns_none(service):
    assert service.get_task("nope") is None


class _GatedMineru:
    """wait_result 挂起直到放行：复现 Stage1 进行中 confirm 的竞争窗口。"""

    def __init__(self):
        self.gate = None  # scenario 内注入 asyncio.Event
        self.wait_started = None

    async def submit(self, file_bytes, filename):
        return "job-1"

    async def wait_result(self, task_id, on_progress=None):
        self.wait_started.set()
        await self.gate.wait()
        if on_progress:
            on_progress(1, 1)
        return MineruResult(md="# 合成", content_list_v2=_v2_nested_one_para(), images={})


def test_confirm_during_stage1_failed_not_resurrected(tmp_path, sample_pdf):
    """评审 rider：Stage1 挂起期间 confirm 把任务判 FAILED，Stage1 完成后不得复活为 MAPPING_READY。"""
    import asyncio

    async def scenario():
        mineru = _GatedMineru()
        mineru.gate = asyncio.Event()
        mineru.wait_started = asyncio.Event()
        svc = AgentMdPipelineService(
            mineru_client=mineru, ocr=_FakeOcr(), ner=_FakeNer(),
            entity_types=[], output_dir=str(tmp_path),
        )
        task = await svc.create_task(sample_pdf, "a.pdf")
        await asyncio.wait_for(mineru.wait_started.wait(), 2)  # Stage1 已挂起在解析等待点
        judged = await svc.confirm(task.task_id, [])  # 非 MAPPING_READY → 判死
        assert judged.state == TaskState.FAILED
        mineru.gate.set()  # 放行 Stage1 完成
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        await asyncio.gather(*pending)
        final = svc.get_task(task.task_id)
        assert final.state == TaskState.FAILED  # 不复活
        assert final.message == "task not ready"

    asyncio.run(scenario())


class _SubmitGatedMineru:
    """submit 挂起直到放行：复现 submit await 窗口内 confirm 判死的复活窗口（终审 I1）。"""

    def __init__(self):
        self.gate = None
        self.submit_started = None

    async def submit(self, file_bytes, filename):
        self.submit_started.set()
        await self.gate.wait()
        return "job-1"

    async def wait_result(self, task_id, on_progress=None):
        if on_progress:
            on_progress(1, 1)
        return MineruResult(md="# 合成", content_list_v2=_v2_nested_one_para(), images={})


def test_confirm_in_submit_window_failed_not_resurrected(tmp_path, sample_pdf):
    """终审 I1：submit await 窗口内 confirm 判死，submit 返回后
    ``task.state = PARSING`` 不得无条件覆写——复活会让迟到 confirm 绕过就绪校验。"""
    import asyncio

    async def scenario():
        mineru = _SubmitGatedMineru()
        mineru.gate = asyncio.Event()
        mineru.submit_started = asyncio.Event()
        svc = AgentMdPipelineService(
            mineru_client=mineru, ocr=_FakeOcr(), ner=_FakeNer(),
            entity_types=[], output_dir=str(tmp_path),
        )
        task = await svc.create_task(sample_pdf, "a.pdf")
        await asyncio.wait_for(mineru.submit_started.wait(), 2)  # Stage1 挂起在 submit 窗口
        judged = await svc.confirm(task.task_id, [])  # 判死
        assert judged.state == TaskState.FAILED
        mineru.gate.set()  # 放行 submit——修复前此处状态被覆写回 PARSING 而复活
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        await asyncio.gather(*pending)
        final = svc.get_task(task.task_id)
        assert final.state == TaskState.FAILED  # 不复活
        assert final.message == "task not ready"

    asyncio.run(scenario())


class _TypeRecordingNer:
    """记录 run_ner 实际收到的 entity_types（C2 零实体静默回归用）。"""

    def __init__(self):
        self.seen = None

    async def extract(self, text, entity_types):
        self.seen = entity_types
        return []


def test_default_entity_types_resolved_for_fresh_owner(tmp_path, sample_pdf):
    """终审 C2：entity_types 缺省空 → Stage1 须按 #66 同款口径解析 owner 缺省启用类型；
    否则 run_ner 恒零实体、「零脱敏 MD」静默出稿。"""
    ner = _TypeRecordingNer()
    svc = AgentMdPipelineService(
        mineru_client=_FakeMineru(), ocr=_FakeOcr(), ner=ner,
        entity_types=[], output_dir=str(tmp_path),
    )
    task = svc.create_task_nowait(sample_pdf, "a.pdf", owner_id="fresh-owner-c2")
    assert task.state == TaskState.MAPPING_READY
    assert ner.seen, "缺省必须解析出非空实体类型清单"
    ids = {t.id for t in ner.seen}
    # 与 #66 缺省九类一致（test_vl_md_pipeline_service.test_default_types_include_all_nine）
    assert ids >= {"PERSON", "ORG", "ADDRESS", "CASE_NUMBER", "BIRTH_DATE",
                   "ID_CARD", "PHONE", "BANK_CARD", "LICENSE_PLATE"}


def test_explicit_entity_types_override_still_honored(tmp_path, sample_pdf):
    """终审 C2 不回归：构造期显式传入的 entity_types 原样透传，不触发解析。"""
    ner = _TypeRecordingNer()
    svc = AgentMdPipelineService(
        mineru_client=_FakeMineru(), ocr=_FakeOcr(), ner=ner,
        entity_types=["SENTINEL_TYPE"], output_dir=str(tmp_path),
    )
    task = svc.create_task_nowait(sample_pdf, "a.pdf")
    assert task.state == TaskState.MAPPING_READY
    assert ner.seen == ["SENTINEL_TYPE"]
