"""Issue#194: JobType.SINGLE 落库与校验。"""
import sqlite3

import pytest

from app.services.batch_mode_validation import is_file_type_allowed_for_job_type
from app.services.job_models import JobType
from app.services.job_store import JobStore


def test_job_type_single_member():
    assert JobType.SINGLE.value == "single"


def test_create_and_list_single_job(tmp_path):
    store = JobStore(str(tmp_path / "jobs.sqlite3"))
    job_id = store.create_job(job_type=JobType.SINGLE, title="a.pdf", owner_id="u1")
    row = store.get_job(job_id)
    assert row["job_type"] == "single"
    jobs, total = store.list_jobs(job_type=JobType.SINGLE, owner_id="u1")
    assert total == 1 and jobs[0]["id"] == job_id


def test_legacy_db_with_old_check_gets_rebuilt(tmp_path):
    """旧 CHECK 表（无 single）必须在 _init_db 迁移后能写 single。"""
    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE jobs (id TEXT PRIMARY KEY, job_type TEXT NOT NULL "
        "CHECK(job_type IN ('text_batch','image_batch','smart_batch','structured_batch')), "
        "title TEXT NOT NULL DEFAULT '', status TEXT NOT NULL, skip_item_review INTEGER NOT NULL DEFAULT 0, "
        "config_json TEXT NOT NULL DEFAULT '{}', priority INTEGER NOT NULL DEFAULT 0, "
        "owner_id TEXT NOT NULL DEFAULT 'local_user', error_message TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
    )
    conn.commit()
    conn.close()
    store = JobStore(str(db))
    job_id = store.create_job(job_type=JobType.SINGLE, owner_id="u1")
    assert store.get_job(job_id)["job_type"] == "single"


@pytest.mark.parametrize("ft", ["pdf", "pdf_scanned", "image", "txt", "docx"])
def test_single_allows_all_file_types(ft):
    assert is_file_type_allowed_for_job_type("single", ft) is True
