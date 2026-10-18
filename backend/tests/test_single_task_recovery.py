"""Issue#194 AC7：启动恢复/修复例程对 SINGLE 的行为钉死。

- 重派映射：main.py 启动段把「item 状态 → 应派 task_type」抽成纯函数
  ``redispatch_task_type_for``（single_task_service.py），SINGLE 与四种
  batch 的映射在此钉死（batch 行为逐字不变）。
- 修复例程：``repair_completed_without_output`` 对 SINGLE（非
  structured_batch）与 batch 同语义——COMPLETED 无 output 回 awaiting_review，
  有 output 不动。
"""
from pathlib import Path

from app.services import file_management_service
from app.services.job_models import JobItemStatus, JobStatus, JobType
from app.services.job_store import JobStore


def test_single_item_statuses_map_to_task_types():
    from app.services.single_task_service import redispatch_task_type_for
    job = {"id": "j", "job_type": "single"}
    for st in ("pending", "processing", "queued", "parsing", "ner", "vision"):
        assert redispatch_task_type_for(job, {"status": st}) == "recognition"
    for st in ("review_approved", "redacting"):
        assert redispatch_task_type_for(job, {"status": st}) == "redaction"
    assert redispatch_task_type_for(job, {"status": "awaiting_review"}) is None
    assert redispatch_task_type_for(job, {"status": "completed"}) is None
    # batch 行为不变
    assert redispatch_task_type_for({"job_type": "structured_batch"}, {"status": "pending"}) == "structured"
    assert redispatch_task_type_for({"job_type": "text_batch"}, {"status": "pending"}) == "recognition"


def test_redispatch_mapping_covers_remaining_batch_types():
    """既有循环对四种 batch 的口径：仅 structured_batch 派 structured，其余 recognition。"""
    from app.services.single_task_service import redispatch_task_type_for
    assert redispatch_task_type_for({"job_type": "image_batch"}, {"status": "review_approved"}) == "redaction"
    assert redispatch_task_type_for({"job_type": "smart_batch"}, {"status": "vision"}) == "recognition"
    assert redispatch_task_type_for({"job_type": "structured_batch"}, {"status": "ner"}) == "structured"
    # 缺 job_type 的旧行按非 structured 处理（与内联 .get 判定一致）
    assert redispatch_task_type_for({}, {"status": "pending"}) == "recognition"


def _complete_single_job(store: JobStore, file_id: str) -> tuple[str, str]:
    """建一个 SINGLE job 并把 item/job 一路推到 COMPLETED（公共状态机迁移）。"""
    job_id = store.create_job(job_type=JobType.SINGLE, title=file_id, owner_id="u1")
    item_id = store.add_item(job_id, file_id)
    store.submit_job(job_id)
    store.update_job_status(job_id, JobStatus.PROCESSING)
    store.update_item_status(item_id, JobItemStatus.PROCESSING)
    store.update_item_status(item_id, JobItemStatus.COMPLETED)
    store.update_job_status(job_id, JobStatus.COMPLETED)
    return job_id, item_id


def test_repair_resets_completed_single_item_without_output(tmp_path, monkeypatch):
    """COMPLETED 无 output 的 SINGLE item → 修复回 awaiting_review；有 output 的对照不动。"""
    store = JobStore(str(tmp_path / "jobs.sqlite3"))
    job_a, item_a = _complete_single_job(store, "file-a")  # 无 output_path → 应修复
    job_b, item_b = _complete_single_job(store, "file-b")  # 有 output_path → 不动

    class _FakeFileStore:
        def get(self, fid):
            if fid == "file-b":
                return {"file_path": "/data/b.pdf", "output_path": "/data/b_out.pdf"}
            return {"file_path": "/data/a.pdf"}

    monkeypatch.setattr(file_management_service, "get_file_store", lambda: _FakeFileStore())

    repaired = store.repair_completed_without_output()

    assert repaired == 1
    item_a_row = store.get_item(item_a)
    assert item_a_row["status"] == JobItemStatus.AWAITING_REVIEW.value
    assert not item_a_row["error_message"]
    assert store.get_job(job_a)["status"] == JobStatus.AWAITING_REVIEW.value
    # 对照组：有 output_path 的 item/job 原样不动
    assert store.get_item(item_b)["status"] == JobItemStatus.COMPLETED.value
    assert store.get_job(job_b)["status"] == JobStatus.COMPLETED.value


def test_main_startup_wiring_uses_redispatch_pure_function():
    # 接线判据（#159 同构教训）：上面的映射测试只钉纯函数本身，钉不住 main.py
    # 启动段是否真的调用它——将来若改回内联状态集合而纯函数留存，测试仍绿。
    # 这里静态断言 main.py 源码含纯函数调用、且旧内联集合不复存在。
    import app.main

    src = Path(app.main.__file__).read_text(encoding="utf-8")
    assert "redispatch_task_type_for(" in src  # 调用形：注释里只剩名字不算接线
    assert "_recognition_statuses" not in src  # 旧内联集合，抽出时已删
