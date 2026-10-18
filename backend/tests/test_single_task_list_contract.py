"""Issue#194：任务列表/详情对 SINGLE 的 HTTP 契约钉（前端依赖面）。

- GET /api/v1/jobs?job_type=single 只回 single job（text_batch 不串场）；
- GET /api/v1/jobs/{id}（默认 performance=true）把 item 的 performance_json
  透出为别名键 queue_wait_ms / recognition_duration_ms / recognition_pages，
  打码后再透出 redaction_duration_ms。

夹具的 performance patch 逐键对照真实工人写入（task_queue.py 起跑段、
task_queue_pipelines.py 的 parse_ms/ner_ms/pages/收尾与 redaction 落账），
防止夹具自造形状造成假绿。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import app
from app.services.job_models import JobItemStatus, JobType
from app.services.job_store import JobStore, _singleton_store

client = TestClient(app)


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "AUTH_ENABLED", False)  # require_auth → anonymous
    monkeypatch.setattr(settings, "JOB_DB_PATH", str(tmp_path / "jobs.sqlite3"))
    # _singleton_store 带 lru_cache：首个请求会把当时路径永久缓存，
    # 跨用例换库必须清缓存，请求态 get_job_store() 才会读到本例的 tmp 库。
    _singleton_store.cache_clear()
    yield
    _singleton_store.cache_clear()  # 不把指向已删 tmp 的单例漏给后续用例


def _make_job(store: JobStore, job_type: JobType) -> tuple[str, str | None]:
    """建 job（SINGLE 才带一条 item），返回 (job_id, item_id)。"""
    job_id = store.create_job(job_type=job_type, title=f"t-{job_type.value}", owner_id="anonymous")
    item_id = store.add_item(job_id, "f1") if job_type is JobType.SINGLE else None
    return job_id, item_id


def test_list_filter_single_returns_only_single_jobs():
    store = JobStore(settings.JOB_DB_PATH)
    single_id, _ = _make_job(store, JobType.SINGLE)
    batch_id, _ = _make_job(store, JobType.TEXT_BATCH)

    # 夹具自检：不过滤时两类都在，防「只建成一个」的假绿
    unfiltered = client.get("/api/v1/jobs")
    assert unfiltered.status_code == 200
    assert {j["id"] for j in unfiltered.json()["jobs"]} == {single_id, batch_id}

    resp = client.get("/api/v1/jobs", params={"job_type": "single"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert [j["id"] for j in body["jobs"]] == [single_id]
    assert all(j["job_type"] == "single" for j in body["jobs"])
    assert batch_id not in {j["id"] for j in body["jobs"]}


def test_detail_exposes_recognition_aliases():
    store = JobStore(settings.JOB_DB_PATH)
    job_id, item_id = _make_job(store, JobType.SINGLE)

    # —— 以下 patch 逐键对照真实工人写入 ——
    # 起跑：task_queue.py 起跑段（recognition.queue_wait_ms 与 queue.last_wait_ms 同值同写）
    store.update_item_performance(
        item_id,
        {
            "recognition": {"started_at": "2026-10-18T00:00:00+00:00", "queue_wait_ms": 12},
            "queue": {
                "last_task_type": "recognition",
                "last_started_at": "2026-10-18T00:00:00+00:00",
                "last_wait_ms": 12,
            },
        },
    )
    # 解析 / NER：task_queue_pipelines.py
    store.update_item_performance(item_id, {"recognition": {"parse_ms": 5}})
    store.update_item_performance(item_id, {"recognition": {"ner_ms": 7}})
    # 单页落账：pages 按字符串页号键控，页内含 page/duration_ms/status
    store.update_item_performance(
        item_id,
        {
            "recognition": {
                "pages": {
                    "1": {
                        "page": 1,
                        "finished_at": "2026-10-18T00:00:01+00:00",
                        "duration_ms": 99,
                        "status": "completed",
                        "active_pages_at_end": 0,
                    }
                }
            }
        },
    )
    # 识别整体收尾：recognition.duration_ms
    store.update_item_performance(
        item_id,
        {"recognition": {"finished_at": "2026-10-18T00:00:02+00:00", "duration_ms": 150}},
    )

    resp = client.get(f"/api/v1/jobs/{job_id}")  # 默认 performance=true
    assert resp.status_code == 200
    body = resp.json()
    assert body["job_type"] == "single"
    items = body["items"]
    assert len(items) == 1
    item = items[0]
    assert item["status"] == JobItemStatus.PENDING.value  # 恢复轮询依赖 items[].status 透出
    assert item["queue_wait_ms"] == 12
    assert item["recognition_duration_ms"] == 150
    pages = item["recognition_pages"]
    assert len(pages) == 1
    assert pages[0]["page"] == 1 and pages[0]["duration_ms"] == 99
    # 同一别名族的派生键一并钉（前端性能面板依赖）
    assert item["recognition_page_duration_sum_ms"] == 99
    assert item["recognition_parallelism_ratio"] == round(99 / 150, 2)
    # 打码未做：别名必须为 None（不能串出旧值或 0）
    assert item["redaction_duration_ms"] is None
    # 原始 performance 照常透出（轻量模式才剥）
    assert item["performance"]["recognition"]["parse_ms"] == 5


def test_detail_exposes_redaction_duration_alias():
    store = JobStore(settings.JOB_DB_PATH)
    job_id, item_id = _make_job(store, JobType.SINGLE)

    # 打码落账：task_queue_pipelines.py redaction 完成段的真实形状
    store.update_item_performance(
        item_id,
        {
            "redaction": {
                "finished_at": "2026-10-18T00:00:03+00:00",
                "duration_ms": 88,
                "entity_count": 3,
                "bounding_box_count": 0,
            }
        },
    )

    resp = client.get(f"/api/v1/jobs/{job_id}")
    assert resp.status_code == 200
    item = resp.json()["items"][0]
    assert item["redaction_duration_ms"] == 88
