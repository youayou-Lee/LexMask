"""Issue#75 agent_md 类型契约与配置项。"""
from app.core.config import settings
from app.services.agent_md_types import AgentMdTask, MappingItem, Seg, TaskState


def test_settings_defaults():
    assert settings.MINERU_API_BASE_URL == "http://127.0.0.1:16581"
    assert settings.MINERU_TASK_TIMEOUT == 7200.0
    assert settings.AGENT_MD_CHUNK_CHARS == 6000


def test_task_initial_state():
    t = AgentMdTask(task_id="t1", state=TaskState.UPLOADED)
    assert t.state == TaskState.UPLOADED
    assert t.mapping == [] and t.segs == [] and t.warnings == []
    assert t.output_file_id is None


def test_seg_sources():
    assert Seg(text="x", page_idx=0, source="text").source == "text"
    assert Seg(text="[公章]", page_idx=0, source="sentinel").text == "[公章]"


def test_mapping_item_defaults():
    m = MappingItem(id="e1", original_text="王小明", entity_type="PERSON", replacement="[人名_1]")
    assert m.excluded is False
