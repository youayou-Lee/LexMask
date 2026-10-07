"""管线状态机测试：全 fake 依赖，无网络无真实模型。

Stage1 会真实读取 file_path 字节（生产契约），故 fixture 用 fitz 造 1 页最小 PDF，
而非 brief 草稿里的 /tmp/a.pdf（不存在，read_bytes 必炸）。
"""
import fitz
import pytest

from app.services.agent_md_pipeline_service import AgentMdPipelineService
from app.services.agent_md_types import TaskState
from app.services.mineru_parse_client import MineruResult


class _FakeMineru:
    async def submit(self, file_bytes, filename):
        return "job-1"

    async def wait_result(self, task_id, on_progress=None):
        if on_progress:
            on_progress(1, 1)
        return MineruResult(
            md="# 合成",
            content_list_v2=[
                {"type": "text", "text": "张三借李四人民币一万元", "page_idx": 0},
            ],
            images={},
        )


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
    assert [m.replacement for m in task.mapping] == ["[人名_1]", "[人名_2]"]
    assert task.pages_total == 1


def test_stage2_confirm_writes_artifacts(service, sample_pdf, tmp_path):
    task = service.create_task_nowait(sample_pdf, "a.pdf")
    done = service.confirm_nowait(task.task_id, [{"id": "e1", "action": "exclude"}])
    assert done.state == TaskState.COMPLETED
    md_path = tmp_path / f"{done.output_file_id}.md"
    assert md_path.exists() and "张三借[人名_2]" in md_path.read_text()
    assert (tmp_path / f"{done.output_file_id}.mapping.json").exists()
    assert (tmp_path / f"{done.output_file_id}.retained_fields.json").exists()


def test_unknown_task_returns_none(service):
    assert service.get_task("nope") is None
