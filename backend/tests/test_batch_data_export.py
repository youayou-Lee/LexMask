"""批量结果明细导出（P0-数据）：流式行生成器 + CSV 分卷 + summary_only。

10 万 item 的 job 只有一个全量 JSON 报告可导，后端内存组装+浏览器全量拉取
双双卡死；技术评测需要"每条结果一行"的可分析明细。此处钉住：
  - iter_report_file_rows / iter_entity_rows 逐行 yield（不攒全量）
  - write_csv_parts 按行数分卷、utf-8-sig
  - build_export_report(include_files=False) 聚合与全量版一致
"""

import csv
import json

import pytest

import app.services.job_management_service as jms
from app.services import export_service
from app.services.job_store import JobStore, JobType


def _fake_info(fid: str, entities: int = 2, boxes: int = 1) -> dict:
    return {
        "original_filename": f"{fid}.pdf",
        "file_type": "pdf",
        "file_size": 1234,
        "page_count": 3,
        "output_path": f"/outputs/{fid}.pdf",
        "entities": [
            {"text": f"张三{k}", "type": "PERSON", "page": 1, "start": k, "end": k + 3,
             "source": "has", "confidence": 0.95}
            for k in range(entities)
        ],
        "bounding_boxes": [
            {"type": "official_seal", "text": "公章", "page": 1, "x": 0.1, "y": 0.2,
             "width": 0.05, "height": 0.06, "source": "visual_features", "selected": True}
            for _ in range(boxes)
        ],
    }


@pytest.fixture()
def job_env(tmp_path, monkeypatch):
    store = JobStore(str(tmp_path / "jobs.sqlite3"))
    job_id = store.create_job(job_type=JobType.SMART_BATCH, owner_id="tenant_a")
    file_ids = [f"file_{i:04d}" for i in range(250)]
    for fid in file_ids:
        store.add_item(job_id, fid)
    infos = {fid: _fake_info(fid) for fid in file_ids}
    monkeypatch.setattr(jms, "_safe_file_info", lambda fid: (infos.get(fid), None))
    return store, job_id, file_ids


def test_iter_report_file_rows_matches_full_report(job_env):
    store, job_id, file_ids = job_env
    rows = list(jms.iter_report_file_rows(store, job_id))
    assert len(rows) == 250
    assert rows[0]["file_id"] == "file_0000"
    assert rows[0]["filename"] == "file_0000.pdf"
    assert rows[0]["entity_count"] == 3  # 2 entities + 1 selected box

    report = jms.build_export_report(store, job_id)
    assert len(report["files"]) == 250
    by_id = {f["file_id"]: f for f in report["files"]}
    for row in rows[:10]:
        assert row["entity_count"] == by_id[row["file_id"]]["entity_count"]
        assert row["status"] == by_id[row["file_id"]]["status"]


def test_export_report_surfaces_recognition_failed_pages(job_env):
    """Issue#199 冷审 C1b：识别失败页必须在导出报告可见（error_message 活不过打码完成）。"""
    store, job_id, file_ids = job_env
    item_id = next(
        it["id"] for it in store.list_items(job_id) if it["file_id"] == file_ids[0]
    )
    store.update_item_performance(
        item_id, {"recognition": {"failed_pages": {"2": "timeout", "3": "ValueError: bad"}}}
    )

    report = jms.build_export_report(store, job_id)
    entry = next(f for f in report["files"] if f["file_id"] == file_ids[0])
    assert entry["recognition_failed_pages"] == [2, 3]
    assert "failed recognition" in str(entry["error"]), entry["error"]
    assert "2" in str(entry["error"]) and "3" in str(entry["error"])

    # 干净条目：字段为空列表、error 不含警示
    clean = next(f for f in report["files"] if f["file_id"] == file_ids[1])
    assert clean["recognition_failed_pages"] == []


def test_update_item_performance_is_deep_merge_across_page_patches(job_env):
    """门⑥ 第三段 must-fix：失败页补记依赖「深合并、不覆盖同 item 其它页」。

    `task_queue_pipelines` 的失败分支会为**同一 item 的另一页**再写一笔
    `recognition.pages.<p>`。若 `update_item_performance` 退化成覆盖写，页 2 的
    completed 记录会被页 1 的失败补记整体冲掉（交付面丢页）。这里钉住合并语义
    （`job_store.update_item_performance` → `_deep_merge_dict`）。

    变异验证：把 `_record_item_performance` 那段删掉 ⇒
    `test_issue199_page_failure_isolation.py::test_u11…` 变红；把 job_store 的
    `_deep_merge_dict(...)` 换成 `patch` ⇒ 本用例变红。
    """
    store, job_id, file_ids = job_env
    item_id = next(
        it["id"] for it in store.list_items(job_id) if it["file_id"] == file_ids[0]
    )
    store.update_item_performance(
        item_id,
        {"recognition": {"pages": {"2": {"status": "completed", "duration_ms": 111}}}},
    )
    store.update_item_performance(
        item_id,
        {
            "recognition": {
                "pages": {"1": {"status": "failed", "error": "ValueError: page boom"}},
                "total_ms": 4242,
            }
        },
    )

    row = store.get_item(item_id)
    perf = json.loads(row["performance_json"])
    pages = perf["recognition"]["pages"]
    assert pages["2"] == {"status": "completed", "duration_ms": 111}, pages
    assert pages["1"]["status"] == "failed", pages
    assert perf["recognition"]["total_ms"] == 4242, perf


def test_summary_only_report_keeps_aggregates_drops_files(job_env):
    store, job_id, _ = job_env
    full = jms.build_export_report(store, job_id)
    slim = jms.build_export_report(store, job_id, include_files=False)
    assert slim["files"] == []
    assert slim["summary"] == full["summary"]
    assert slim["redacted_zip"] == full["redacted_zip"]


def test_iter_entity_rows_expands_entities_and_regions(job_env):
    store, job_id, _ = job_env
    rows = list(jms.iter_entity_rows(store, job_id, selected_file_ids=["file_0001"]))
    kinds = [r["record_kind"] for r in rows]
    assert kinds.count("entity") == 2
    assert kinds.count("region") == 1
    entity = next(r for r in rows if r["record_kind"] == "entity")
    assert entity["type"] == "PERSON" and entity["filename"] == "file_0001.pdf"
    region = next(r for r in rows if r["record_kind"] == "region")
    assert region["type"] == "official_seal" and region["page"] == 1


def test_write_csv_parts_splits_and_is_excel_friendly(tmp_path):
    headers = ["a", "b"]
    rows = ({"a": i, "b": f"值{i}"} for i in range(250))
    parts = export_service.write_csv_parts(
        rows, str(tmp_path), "files", headers, rows_per_part=100
    )
    assert [p["file_count"] for p in parts] == [100, 100, 50]
    assert parts[0]["name"] == "files-part001.csv"
    raw = (tmp_path / parts[0]["name"]).read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")  # utf-8-sig BOM，Excel 直开
    with open(tmp_path / parts[2]["name"], encoding="utf-8-sig") as f:
        got = list(csv.DictReader(f))
    assert len(got) == 50 and got[0]["a"] == "200"


def test_write_csv_parts_single_part_has_no_suffix_pressure(tmp_path):
    rows = ({"a": i} for i in range(5))
    parts = export_service.write_csv_parts(rows, str(tmp_path), "entities", ["a"], rows_per_part=100)
    assert len(parts) == 1
    assert parts[0]["name"] == "entities-part001.csv"


# ---------------------------------------------------------------- Issue#199 交付裁定钉（门⑥ 二段 F4）

@pytest.fixture()
def deliverable_job_env(tmp_path, monkeypatch):
    """一条**完全可交付**的 job：成品真实存在 + `skip_item_review`（评审前置自动满足）。

    默认 `job_env` 的 output_path 不在 OUTPUT_DIR 内、评审也未确认 ⇒ ready_for_delivery
    恒为 False，交付裁定的翻转不可观测；F4 判据必须在可交付基线上测。
    """
    from app.services import job_metadata

    store = JobStore(str(tmp_path / "jobs.sqlite3"))
    job_id = store.create_job(
        job_type=JobType.SMART_BATCH, owner_id="tenant_a", skip_item_review=True
    )
    out_dir = tmp_path / "outputs"
    out_dir.mkdir()
    file_ids = [f"file_{i:04d}" for i in range(3)]
    infos: dict[str, dict] = {}
    for fid in file_ids:
        store.add_item(job_id, fid)
        target = out_dir / f"{fid}.pdf"
        target.write_bytes(b"%PDF-1.4 redacted")
        infos[fid] = {**_fake_info(fid), "output_path": str(target)}
    monkeypatch.setattr(job_metadata.settings, "OUTPUT_DIR", str(out_dir))
    monkeypatch.setattr(jms, "_safe_file_info", lambda fid: (infos.get(fid), None))
    return store, job_id, file_ids


def test_recognition_failed_pages_block_delivery(deliverable_job_env):
    """门⑥ 二段 F4：页级识别失败必须进交付裁定，不能只写 error 文案。

    失败页「未被扫过敏感数据」（#199 冷审 C1b 语义）；旧实现把失败页写进 `error`
    展示串，而 ready_for_delivery/action_required/blocking_reasons 三处裁定完全看不见它。
    """
    store, job_id, file_ids = deliverable_job_env
    before = jms.build_export_report(store, job_id)
    assert before["summary"]["delivery_status"] == "ready_for_delivery"
    entry_before = next(f for f in before["files"] if f["file_id"] == file_ids[0])
    assert entry_before["ready_for_delivery"] is True
    assert entry_before["blocking_reasons"] == []
    assert entry_before["recognition_failed_pages"] == []

    item_id = next(it["id"] for it in store.list_items(job_id) if it["file_id"] == file_ids[0])
    store.update_item_performance(item_id, {"recognition": {"failed_pages": {"133": "timeout"}}})

    report = jms.build_export_report(store, job_id)
    entry = next(f for f in report["files"] if f["file_id"] == file_ids[0])
    assert entry["recognition_failed_pages"] == [133]
    assert entry["ready_for_delivery"] is False
    assert entry["action_required"] is True and entry["blocking"] is True
    assert entry["delivery_status"] == "action_required"
    assert entry["blocking_reasons"] == ["recognition_failed_pages"]
    assert "failed recognition" in str(entry["error"])  # 展示面文案不回归
    assert report["summary"]["delivery_status"] == "action_required"
    assert report["summary"]["action_required"] is True
    assert report["summary"]["action_required_files"] == 1
    # 干净条目零连坐
    clean = next(f for f in report["files"] if f["file_id"] == file_ids[1])
    assert clean["ready_for_delivery"] is True and clean["blocking_reasons"] == []


def test_iter_report_file_rows_agrees_with_report_on_failed_pages(deliverable_job_env):
    """裁定单一真源：CSV 明细行与 JSON 报告逐文件一致（旧实现两处各复制一份条件）。"""
    store, job_id, file_ids = deliverable_job_env
    item_id = next(it["id"] for it in store.list_items(job_id) if it["file_id"] == file_ids[0])
    store.update_item_performance(item_id, {"recognition": {"failed_pages": {"7": "timeout"}}})

    rows = {r["file_id"]: r for r in jms.iter_report_file_rows(store, job_id)}
    files = {f["file_id"]: f for f in jms.build_export_report(store, job_id)["files"]}
    assert rows[file_ids[0]]["ready_for_delivery"] is False
    assert "recognition_failed_pages" in rows[file_ids[0]]["blocking_reasons"]
    for fid in file_ids:
        assert rows[fid]["ready_for_delivery"] == files[fid]["ready_for_delivery"], fid


def test_delivery_adjudication_single_source_helper():
    """裁定条件只有一份实现：helper 与阻塞原因清单同源（防两处复制漂移）。"""
    item = {"status": jms.JobItemStatus.REVIEW_APPROVED.value}
    assert jms._is_ready_for_delivery(item, True, True, None, []) is True
    assert jms._is_ready_for_delivery(item, True, True, None, [133]) is False
    assert jms._delivery_blocking_reasons(item, True, True, None, [133]) == [
        "recognition_failed_pages"
    ]
    assert jms._delivery_blocking_reasons(item, True, True, None, []) == []
