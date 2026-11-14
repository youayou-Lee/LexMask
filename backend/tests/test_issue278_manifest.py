"""Issue#278 · manifest 机读契约逐键断言 + route_counts_of 聚合口径。

字段表出处：design-issue278.md §2.1 manifest dict（r2）。逐键断言的杀伤面：
* 任何 manifest 键被删（V4）→ ``test_manifest_field_table`` 红；
* route_counts_of 口径错（docline 接管/text_source 三值/缺记录 unknown）→ 聚合断言红。
"""

from __future__ import annotations

import asyncio
import json
import re
import zipfile
from pathlib import Path

import fitz
import pytest

from app.core.config import settings
from app.services import page_status_service
from app.services.export_service import ExportTaskManager
from app.services.job_store import JobItemStatus, JobStore, JobType
from app.services.partial_export_service import config_from_job_config, submit_item_export


def _make_pdf(path: Path, pages: int = 6) -> str:
    doc = fitz.open()
    for i in range(pages):
        page = doc.new_page(width=595, height=842)
        page.insert_text((72, 100), f"MARKER-P{i + 1}", fontsize=14)
        page.draw_rect(fitz.Rect(100, 200, 300, 260), color=(1, 0, 0), fill=(1, 0, 0))
    doc.save(str(path))
    doc.close()
    return str(path)


def _boxes(page_count: int) -> dict[int, list[dict]]:
    return {
        p: [
            {
                "id": f"b{p}",
                "x": 0.17,
                "y": 0.24,
                "width": 0.34,
                "height": 0.072,
                "type": "PERSON",
                "page": p,
                "source": "manual",
            }
        ]
        for p in range(1, page_count + 1)
    }


def _seed(
    store: JobStore,
    *,
    page_count: int = 6,
    failed: dict[int, str] | None = None,
    manual: dict[str, dict] | None = None,
    text_source: dict[int, str] | None = None,
    docline_pages: set[int] | None = None,
) -> tuple[str, str]:
    failed = failed or {}
    job_id = store.create_job(
        job_type=JobType.TEXT_BATCH, title="t", config={"watermark_delivery": "keep"}, owner_id="admin"
    )
    item_id = store.add_item(job_id, "file-1")
    store.update_item_status(item_id, JobItemStatus.AWAITING_REVIEW)
    pages: dict[str, dict] = {}
    for p in range(1, page_count + 1):
        record: dict = {"status": "completed", "duration_ms": 100}
        if p in (docline_pages or set()):
            record["pipeline_status"] = {"text_layer_doc": {"ran": True}}
        if text_source and p in text_source:
            record["text_source"] = text_source[p]
        if p in failed:
            record = {"status": "failed", "error": failed[p]}
        pages[str(p)] = record
    recognition: dict = {"page_count": page_count, "pages": pages, "failed_pages": dict(failed)}
    if manual is not None:
        recognition["manual_verified_pages"] = manual
    store.update_item_performance(item_id, {"recognition": recognition})
    return job_id, item_id


# ---------------------------------------------------------------------------
# route_counts_of（§2.4 聚合口径）
# ---------------------------------------------------------------------------

def _perf_row(pages: dict[str, dict]) -> dict:
    return {"performance": {"recognition": {"pages": pages}}}


def test_route_counts_docline_takeover_wins():
    item = _perf_row(
        {
            "1": {"status": "completed", "pipeline_status": {"text_layer_doc": {"ran": True}}},
            "2": {"status": "completed", "text_source": "ocr"},
        }
    )
    counts = page_status_service.route_counts_of(item, [1, 2])
    assert counts == {"text_layer": 1, "ocr": 1, "accelerator": 0, "both": 0, "unknown": 0}


def test_route_counts_text_source_three_values():
    item = _perf_row(
        {
            "1": {"status": "completed", "text_source": "text_layer"},
            "3": {"status": "completed", "text_source": "ocr"},
            "4": {"status": "completed", "text_source": "both"},
        }
    )
    counts = page_status_service.route_counts_of(item, [1, 3, 4])
    assert counts == {"text_layer": 1, "ocr": 1, "accelerator": 0, "both": 1, "unknown": 0}


def test_route_counts_missing_record_is_unknown_and_accelerator_reserved():
    """缺页记录（pending/旧任务/file 模式）→ unknown；accelerator 是 #274/#275 预留
    字段、现无生产者，即便页账里出现该标记也计入（聚合口径支持，值恒 0 的键不省）。"""
    item = _perf_row({"2": {"status": "pending"}})
    counts = page_status_service.route_counts_of(item, [1, 2])
    assert counts == {"text_layer": 0, "ocr": 0, "accelerator": 0, "both": 0, "unknown": 2}

    with_acc = _perf_row({"1": {"status": "completed", "accelerator": True}})
    counts2 = page_status_service.route_counts_of(with_acc, [1])
    assert counts2["accelerator"] == 1


def test_route_counts_empty_pages_all_zero_but_keys_present():
    counts = page_status_service.route_counts_of(_perf_row({}), [])
    assert counts == {"text_layer": 0, "ocr": 0, "accelerator": 0, "both": 0, "unknown": 0}


# ---------------------------------------------------------------------------
# exportable_page_numbers / manual_ledger_of（#280 防御性消费）
# ---------------------------------------------------------------------------

def test_exportable_is_recognized_union_manual(tmp_path):
    store = JobStore(str(tmp_path / "j1.sqlite3"))
    _job_id, _item_id = _seed(
        store,
        page_count=6,
        manual={"7": {"verified_at": "2026-11-15T00:00:00+00:00", "operator": "r", "box_count": 2}},
    )
    item = store.get_item(_item_id)
    exportable = page_status_service.exportable_page_numbers(item, 6)
    assert exportable == {1, 2, 3, 4, 5, 6}  # 页 7 超出 page_count，不入集

    store2 = JobStore(str(tmp_path / "j2.sqlite3"))
    _job2, item2 = _seed(
        store2,
        page_count=10,
        failed={4: "boom"},
        manual={"7": {"verified_at": "v", "operator": "o", "box_count": 1}},
    )
    item_row = store2.get_item(item2)
    exportable2 = page_status_service.exportable_page_numbers(item_row, 10)
    assert exportable2 == {1, 2, 3, 5, 6, 7, 8, 9, 10}, "recognized ∪ manual，失败页永不入集"

    # V3 杀手面：manual 账本与失败清单同时记到的页（人工兜底没成功仍记失败）
    # 也不得进缺省集——#199 红线优先于人工兜底。
    store3 = JobStore(str(tmp_path / "j3.sqlite3"))
    _job3, item3 = _seed(
        store3,
        page_count=10,
        failed={4: "boom", 9: "boom-9"},
        manual={
            "7": {"verified_at": "v", "operator": "o", "box_count": 1},
            "9": {"verified_at": "v9", "operator": "o", "box_count": 2},
        },
    )
    exportable3 = page_status_service.exportable_page_numbers(store3.get_item(item3), 10)
    assert exportable3 == {1, 2, 3, 5, 6, 7, 8, 10}, "manual∧failed 页不入集"
    assert 4 not in exportable3 and 9 not in exportable3


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "not-a-dict",
        [],
        {"7": "not-a-dict-entry"},
        {"x": {"verified_at": "v"}},
        {"0": {"verified_at": "v"}},
        {"7": None},
    ],
)
def test_manual_ledger_dirty_shapes_yield_empty(raw):
    """#280 §2.0 账本脏形状：缺失/类型不对/条目非 dict ⇒ 空表，不崩导出。"""
    perf = {"recognition": {"page_count": 10, "pages": {}, "manual_verified_pages": raw}}
    assert page_status_service.manual_ledger_of({"performance": perf}) == []
    assert page_status_service.exportable_page_numbers({"performance": perf}, 10) == set()


def test_manual_ledger_projected_keys():
    perf = {
        "recognition": {
            "manual_verified_pages": {
                "7": {"verified_at": "2026-11-15T01:00:00+00:00", "operator": "alice", "box_count": 3, "original_reason": "x"},
                "3": {"verified_at": "2026-11-15T00:00:00+00:00", "operator": "bob", "box_count": 1},
            }
        }
    }
    ledger = page_status_service.manual_ledger_of({"performance": perf})
    assert [e["page"] for e in ledger] == [3, 7]
    assert ledger[0] == {"page": 3, "box_count": 1, "verified_at": "2026-11-15T00:00:00+00:00", "operator": "bob"}
    assert set(ledger[1]) == {"page", "box_count", "verified_at", "operator"}


# ---------------------------------------------------------------------------
# manifest 字段表（r2 §2.1）
# ---------------------------------------------------------------------------

@pytest.fixture()
def export_env(tmp_path, monkeypatch):
    out_root = tmp_path / "out"
    out_root.mkdir()
    monkeypatch.setattr(settings, "OUTPUT_DIR", str(out_root))
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path))
    return out_root, tmp_path


def _run_export(store, item_id, file_info, kind="partial", pages=None, include_summary=False):
    manager = ExportTaskManager()
    item_row = store.get_item(item_id)

    async def _flow():
        task = await submit_item_export(
            owner_id="admin",
            store=store,
            job_row=store.get_job(item_row["job_id"]),
            item_row=item_row,
            file_info=file_info,
            config=config_from_job_config({"watermark_delivery": "keep"}),
            kind=kind,
            pages=pages,
            include_summary=include_summary,
            manager=manager,
        )
        for _ in range(600):
            if task.status in {"completed", "failed"}:
                break
            await asyncio.sleep(0.02)
        return task

    task = asyncio.run(_flow())
    assert task.status == "completed", task.error
    return task


def test_manifest_field_table(export_env, tmp_path):
    out_root, tmp_path = export_env
    store = JobStore(str(tmp_path / "jobs.sqlite3"))
    job_id, item_id = _seed(
        store,
        page_count=6,
        failed={4: "视觉识别 page timeout after 300s"},
        manual={"5": {"verified_at": "2026-11-15T02:00:00+00:00", "operator": "alice", "box_count": 2}},
        docline_pages={1},
        text_source={2: "text_layer", 3: "ocr"},
    )
    src = Path(_make_pdf(tmp_path / "src.pdf", 6))
    info = {
        "file_id": "file-1",
        "file_type": "pdf_scanned",
        "page_count": 6,
        "original_filename": "测试卷.pdf",
        "file_path": str(src),
        "bounding_boxes": _boxes(6),
    }

    task = _run_export(store, item_id, info, kind="partial", pages=[1, 3, 5])
    manifest = json.loads((Path(task.out_dir) / "manifest.json").read_text(encoding="utf-8"))

    expected_keys = {
        "export_id", "kind", "file_id", "job_id", "item_id",
        "source_filename", "source_page_count",
        "pages", "page_count",
        "excluded_failed_pages",
        "manual_verified_pages", "manual_verified_pages_count",
        "route_summary",
        "recognized_pages_at_export",
        "verify_incomplete_pages",
        "include_summary",
        "files",
        "snapshot_at", "generated_at", "app_version",
        "volumes",
        "skipped",
    }
    assert expected_keys <= set(manifest), set(manifest) - expected_keys

    assert manifest["export_id"] == task.export_id
    assert manifest["kind"] == "partial"
    assert manifest["file_id"] == "file-1"
    assert manifest["job_id"] == job_id
    assert manifest["item_id"] == item_id
    assert manifest["source_filename"] == "测试卷.pdf"
    assert manifest["source_page_count"] == 6
    assert manifest["pages"] == [1, 3, 5]
    assert manifest["page_count"] == 3, "page_count 是包内页数（旧语义由 source_page_count 承担）"
    assert manifest["excluded_failed_pages"] == [
        {"page": 4, "reason_code": "视觉识别 page timeout after 300s"}
    ]
    assert manifest["manual_verified_pages"] == [
        {"page": 5, "box_count": 2, "verified_at": "2026-11-15T02:00:00+00:00", "operator": "alice"}
    ]
    assert manifest["manual_verified_pages_count"] == 1
    assert manifest["route_summary"] == {
        "text_layer": 1,  # 页 1 docline 接管 + 页 2 text_source
        "ocr": 1,
        "accelerator": 0,
        "both": 0,
        "unknown": 1,  # 页 5 无路由证据
    }
    assert manifest["recognized_pages_at_export"] == 5, "exportable 基数（失败页 4 不计）"
    assert manifest["verify_incomplete_pages"] == []
    assert manifest["include_summary"] is False
    assert manifest["app_version"] == settings.APP_VERSION
    assert manifest["snapshot_at"] and manifest["generated_at"]
    assert manifest["skipped"] == []

    files = manifest["files"]
    assert len(files) == 1
    assert files[0]["name"].endswith(".pdf")
    assert files[0]["bytes"] > 0
    assert re.fullmatch(r"[0-9a-f]{64}", files[0]["sha256"])

    # 包内一致性：manifest.json 也在 zip 里，files 与 zip entry 对得上
    with zipfile.ZipFile(Path(task.out_dir) / "part001.zip") as zf:
        names = zf.namelist()
        assert names[-1] == "manifest.json"
        inner = json.loads(zf.read("manifest.json"))
        assert inner["pages"] == [1, 3, 5]
        assert inner["files"] == files
        assert inner["volumes_planned"] >= 1, "包内 manifest 由 write_export_volumes 注入 volumes_planned"


def test_manifest_manual_empty_is_explicit_and_full_keys_present(export_env, tmp_path):
    out_root, tmp_path = export_env
    store = JobStore(str(tmp_path / "jobs.sqlite3"))
    job_id, item_id = _seed(store, page_count=4)
    src = Path(_make_pdf(tmp_path / "src.pdf", 4))
    product = out_root / "product.pdf"
    product.write_bytes(src.read_bytes())
    info = {
        "file_id": "file-1",
        "file_type": "pdf_scanned",
        "page_count": 4,
        "original_filename": "测试卷.pdf",
        "file_path": str(src),
        "bounding_boxes": _boxes(4),
        "output_path": str(product),
    }
    task = _run_export(store, item_id, info, kind="full")
    manifest = json.loads((Path(task.out_dir) / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["manual_verified_pages"] == [], "空集显式 [] 不省键（#280 §4.2）"
    assert manifest["manual_verified_pages_count"] == 0
    assert manifest["verify_incomplete_pages"] == []
    assert manifest["skipped"] == []
    assert manifest["include_summary"] is False
    assert set(manifest["route_summary"]) == {"text_layer", "ocr", "accelerator", "both", "unknown"}
