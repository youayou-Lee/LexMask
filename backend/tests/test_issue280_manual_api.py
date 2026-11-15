# Copyright 2026 LexMask Contributors

"""Issue#280 · B1–B4 / C3 / D1：manual-verify 端点全链（真 JobStore + 真 file_store 替身）。

钉住：
* B1 verify → 交付闸放行 → 部分导出 manifest 落 ``manual_verified_pages``；
* B2 撤销 → 回 failed、重新阻断、二次撤销 409；
* B3 负向准入矩阵（含 r2 F-I4 ``manual_boxes_conflict`` 与 r3 例外正判据）；
* B4 框落账（file_store + 审阅草稿，``source=manual``）；
* C3 manual 页不可重试（``page_not_failed``）；
* D1 审计（顶层 ``action``/``user`` + detail page/box_count + ledger 快照）。
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import fitz
import pytest

import app.services.task_queue_pipelines as tqp
from app.api import files as files_api
from app.api.jobs import manual_verify_page, retry_item_pages, revoke_manual_verify_page
from app.core.config import settings
from app.core.errors import AppError
from app.models.job_schemas import ManualVerifyBoxIn, ManualVerifyRequest
from app.services.export_service import ExportTaskManager
from app.services.file_management_service import _file_store_lock
from app.services.file_store_db import FileStoreDB
from app.services.job_store import JobItemStatus, JobStore, JobType
from app.services.partial_export_service import config_from_job_config, submit_item_export

_FILE_ID = "file-1"
_ORIGINAL_REASON = "视觉识别 page timeout after 300s: page budget exhausted"


def _make_pdf(path: Path, pages: int) -> Path:
    doc = fitz.open()
    for index in range(pages):
        page = doc.new_page(width=595, height=842)
        page.insert_text((72, 100), f"MARKER-P{index + 1}", fontsize=18)
    doc.save(path)
    doc.close()
    return path


def _boxes_payload() -> list[ManualVerifyBoxIn]:
    return [
        ManualVerifyBoxIn(x=0.1, y=0.2, width=0.3, height=0.05, type="PERSON", text="张三"),
        ManualVerifyBoxIn(x=0.5, y=0.2, width=0.3, height=0.05, type="official_seal"),
    ]


def _box_dicts() -> list[dict[str, Any]]:
    return [
        {
            "id": f"manual-{i}",
            "page": 5,
            "x": 0.1 + i * 0.4,
            "y": 0.2,
            "width": 0.3,
            "height": 0.05,
            "type": "PERSON",
            "text": None,
            "selected": True,
            "confidence": None,
            "source": "manual",
            "evidence_source": "manual",
        }
        for i in range(2)
    ]


class _Env:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, file_type: str = "pdf_scanned"):
        self.tmp_path = tmp_path
        self.monkeypatch = monkeypatch
        self.file_type = file_type
        out_root = tmp_path / "out"
        out_root.mkdir(exist_ok=True)
        monkeypatch.setattr(settings, "OUTPUT_DIR", str(out_root))
        monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path))
        # has_output 要求 output_path 是 OUTPUT_DIR 下真实存在的文件（_redacted_output_state）。
        self.output_path = out_root / "redacted.pdf"
        self.output_path.write_bytes(b"%PDF-1.4 fake")
        self.store = JobStore(str(tmp_path / "jobs.sqlite3"))
        self.jid = self.store.create_job(job_type=JobType.TEXT_BATCH, title="t", owner_id="apitester")
        self.iid = self.store.add_item(self.jid, _FILE_ID)
        self.store.update_item_status(self.iid, JobItemStatus.AWAITING_REVIEW)
        pages = {str(p): {"status": "completed", "duration_ms": 120} for p in range(1, 11) if p != 5}
        pages["5"] = {"status": "failed", "error": _ORIGINAL_REASON}
        self.store.update_item_performance(
            self.iid,
            {
                "recognition": {
                    "page_count": 10,
                    "pages": pages,
                    "failed_pages": {"5": _ORIGINAL_REASON},
                }
            },
        )
        src = _make_pdf(tmp_path / "src.pdf", 10)
        self.file_store = FileStoreDB(str(tmp_path / "file_store.sqlite3"))
        self.file_store.set(
            _FILE_ID,
            {
                "file_id": _FILE_ID,
                "file_type": file_type,
                "page_count": 10,
                "original_filename": "测试卷.pdf",
                "file_path": str(src),
                "output_path": str(self.output_path),
                "bounding_boxes": {
                    str(p): [
                        {"id": f"auto{p}", "page": p, "x": 0.1, "y": 0.1, "width": 0.2,
                         "height": 0.05, "type": "PERSON", "source": "ocr_has", "selected": True}
                    ]
                    for p in (1, 2, 3, 4, 6, 7, 8, 9, 10)
                },
            },
        )
        monkeypatch.setattr("app.services.file_management_service.file_store", self.file_store)
        monkeypatch.setattr(
            "app.services.file_management_service._file_store_lock", _file_store_lock
        )

    def item_row(self) -> dict[str, Any]:
        return self.store.get_item(self.iid) or {}

    def recognition(self) -> dict[str, Any]:
        return (self.store.get_item_performance(self.iid) or {}).get("recognition") or {}

    def file_info(self) -> dict[str, Any]:
        return self.file_store.get(_FILE_ID) or {}

    def boxes_on_page5(self) -> list[dict[str, Any]]:
        # 与 task_queue_pipelines._boxes_by_page 同口径归一（r2 F-M4）
        return tqp._boxes_by_page(self.file_info().get("bounding_boxes")).get(5, [])

    def verify(self, *, page: int = 5, boxes: list[ManualVerifyBoxIn] | None = None):
        return manual_verify_page(
            self.jid,
            self.iid,
            page,
            ManualVerifyRequest(boxes=boxes if boxes is not None else _boxes_payload()),
            store=self.store,
            owner_id="apitester",
        )

    def revoke(self, *, page: int = 5):
        return revoke_manual_verify_page(self.jid, self.iid, page, store=self.store, owner_id="apitester")


# ---------------------------------------------------------------------------
# B1：verify → 放行 → manifest
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_then_export_report_and_partial_manifest(tmp_path, monkeypatch):
    """注意：ExportTaskManager 的 worker 挂在**当前事件循环**上（_ensure_worker 用
    asyncio.get_running_loop()），submit 与轮询必须在同一个 asyncio.run/pytest-asyncio
    循环内完成，否则 worker 任务随循环关闭而饿死（task 永远 running）。"""
    env = _Env(tmp_path, monkeypatch)

    # 事前：交付闸 409（失败页未处理）
    request = files_api.BatchDownloadRequest(job_id=env.jid, file_ids=[_FILE_ID], redacted=True)
    with pytest.raises(Exception) as info:
        files_api._assert_batch_delivery_ready(request, env.store, "apitester")
    assert getattr(info.value, "status_code", None) == 409

    result = await env.verify()
    assert result["page"] == 5
    assert result["state"] == "manual_verified"
    assert result["box_count"] == 2
    assert result["operator"] == "apitester"
    assert result["item_status"] == "awaiting_review"
    assert result["verified_at"]

    recognition = env.recognition()
    assert "5" not in (recognition.get("failed_pages") or {})
    assert recognition["manual_verified_pages"]["5"]["box_count"] == 2
    assert recognition["manual_verified_pages"]["5"]["original_reason"] == _ORIGINAL_REASON

    # 交付闸放行：verify 之后用户确认审阅（awaiting_review → completed）
    env.store.update_item_status(env.iid, JobItemStatus.COMPLETED)
    import app.services.job_management_service as jms

    report = jms.build_export_report(env.store, env.jid, selected_file_ids=[_FILE_ID])
    assert report["summary"]["ready_for_delivery"] is True
    files_api._assert_batch_delivery_ready(request, env.store, "apitester")  # 不再 409

    # 选页导出 pages=[5]（#278 统一链）→ manifest 落 manual_verified_pages
    # （同一循环内 submit+轮询）；include_summary=True 以便端到端验证人工页披露句。
    task = await submit_item_export(
        owner_id="apitester",
        store=env.store,
        job_row=env.store.get_job(env.jid) or {},
        item_row=env.item_row(),
        file_info=env.file_info(),
        config=config_from_job_config({"watermark_delivery": "keep"}),
        kind="partial",
        pages=[5],
        include_summary=True,
        manager=ExportTaskManager(),
    )
    for _ in range(600):
        if task.status in {"completed", "failed"}:
            break
        await asyncio.sleep(0.05)
    assert task.status == "completed", task.error
    manifest = json.loads((Path(task.out_dir) / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["manual_verified_pages_count"] == 1
    entry = manifest["manual_verified_pages"][0]
    assert entry["page"] == 5 and entry["box_count"] == 2
    assert entry["operator"] == "apitester" and entry["verified_at"]
    # 说明.txt 逐页文案（#278 起说明信息在包内 说明.txt，不再有 manifest.cover）
    import zipfile

    with zipfile.ZipFile(Path(task.out_dir) / "part001.zip") as zf:
        summary_text = zf.read("说明.txt").decode("utf-8")
    assert "第 5 页为人工补框结果" in summary_text and "未经自动识别校验" in summary_text
    # meta 同字段（任务卡/记录面同拍快照）
    assert task.public().get("manual_verified_pages_count") == 1


# ---------------------------------------------------------------------------
# B2：撤销 → 重新 409
# ---------------------------------------------------------------------------


def test_revoke_returns_to_failed_and_blocks_delivery(tmp_path, monkeypatch):
    env = _Env(tmp_path, monkeypatch)
    asyncio.run(env.verify())
    request = files_api.BatchDownloadRequest(job_id=env.jid, file_ids=[_FILE_ID], redacted=True)

    result = asyncio.run(env.revoke())
    assert result["page"] == 5
    assert result["state"] == "failed"
    assert result["operator"] == "apitester"

    recognition = env.recognition()
    assert recognition["failed_pages"].get("5") == _ORIGINAL_REASON
    assert "5" not in (recognition.get("manual_verified_pages") or {})
    # file_store 该页 manual 框删除（页回红叉）
    assert all(box.get("source") != "manual" for box in env.boxes_on_page5())

    import app.services.job_management_service as jms

    report = jms.build_export_report(env.store, env.jid, selected_file_ids=[_FILE_ID])
    assert report["summary"]["ready_for_delivery"] is False
    assert 5 in report["files"][0]["recognition_failed_pages"]
    with pytest.raises(Exception) as info:
        files_api._assert_batch_delivery_ready(request, env.store, "apitester")
    assert getattr(info.value, "status_code", None) == 409

    # 二次撤销（仍在审阅态）→ 409 page_not_manual_verified
    with pytest.raises(AppError) as exc:
        asyncio.run(env.revoke())
    assert exc.value.status_code == 409
    assert exc.value.error_code == "page_not_manual_verified"

    # 确认审阅后，让「重新 409」的唯一阻断理由就是失败页回清单（红线不退让的可辨断言）。
    env.store.update_item_status(env.iid, JobItemStatus.COMPLETED)


# ---------------------------------------------------------------------------
# B3：负向准入矩阵
# ---------------------------------------------------------------------------


def _expect_app_error(coro, *, status: int, code: str) -> None:
    with pytest.raises(AppError) as exc:
        asyncio.run(coro)
    assert exc.value.status_code == status, exc.value
    assert exc.value.error_code == code, exc.value


def test_verify_rejections(tmp_path, monkeypatch):
    env = _Env(tmp_path, monkeypatch)

    # 空 boxes → 400 manual_boxes_required（Ruling：框必填）
    _expect_app_error(env.verify(boxes=[]), status=400, code="manual_boxes_required")
    # 成功页 → 409 page_not_failed
    _expect_app_error(env.verify(page=3), status=409, code="page_not_failed")
    # 越界 → 400 page_out_of_range
    _expect_app_error(env.verify(page=0), status=400, code="page_out_of_range")
    _expect_app_error(env.verify(page=11), status=400, code="page_out_of_range")
    # txt 文件 → 400 manual_verify_unsupported_type
    env2 = _Env(tmp_path, monkeypatch, file_type="txt")
    _expect_app_error(env2.verify(), status=400, code="manual_verify_unsupported_type")

    # processing 件 → 409 item_busy
    env3 = _Env(tmp_path, monkeypatch)
    env3.store.update_item_status(env3.iid, JobItemStatus.PROCESSING)
    _expect_app_error(env3.verify(), status=409, code="item_busy")

    # 坐标越界 → 400 manual_boxes_invalid
    bad = [ManualVerifyBoxIn(x=1.5, y=0.2, width=0.3, height=0.05, type="PERSON")]
    _expect_app_error(env.verify(boxes=bad), status=400, code="manual_boxes_invalid")
    empty_type = [ManualVerifyBoxIn(x=0.1, y=0.2, width=0.3, height=0.05, type=" ")]
    _expect_app_error(env.verify(boxes=empty_type), status=400, code="manual_boxes_invalid")


def test_verify_conflicts_with_out_of_ledger_manual_boxes(tmp_path, monkeypatch):
    """r2 F-I4：目标页存在页不在 ledger 的 manual 框（审阅期手拉）⇒ 409 不静默吞。"""
    env = _Env(tmp_path, monkeypatch)
    info = env.file_info()
    info.setdefault("bounding_boxes", {})["5"] = [
        {"id": "user-box", "page": 5, "x": 0.1, "y": 0.1, "width": 0.2, "height": 0.05,
         "type": "PERSON", "source": "manual", "selected": True}
    ]
    env.file_store.set(_FILE_ID, info)
    _expect_app_error(env.verify(), status=409, code="manual_boxes_conflict")


def test_verify_replaces_auto_boxes_on_failed_page(tmp_path, monkeypatch):
    """例外 (a)：自动框（残留半成品）不触发 409，整页替换正是本操作本意。"""
    env = _Env(tmp_path, monkeypatch)
    result = asyncio.run(env.verify())
    assert result["state"] == "manual_verified"
    boxes = env.boxes_on_page5()
    assert len(boxes) == 2
    assert all(box["source"] == "manual" for box in boxes)


def test_verify_already_manual_verified_page_rejected(tmp_path, monkeypatch):
    """r3 B3 补：页已在 ledger 且不在 failed_pages（非再失败态）⇒ 409。"""
    env = _Env(tmp_path, monkeypatch)
    asyncio.run(env.verify())
    _expect_app_error(env.verify(), status=409, code="page_already_manual_verified")


def test_verify_refailed_page_in_ledger_passes_and_replaces(tmp_path, monkeypatch):
    """r3 B3 例外 (b)：重跑再失败页（failed_pages ∧ ledger）⇒ 202，新框集整页替换旧 manual 集。"""
    env = _Env(tmp_path, monkeypatch)
    asyncio.run(env.verify())
    # 模拟整卷重跑再失败：_write_failed_pages 整表替换写回页 5
    env.store.update_item_performance(
        env.iid,
        {"recognition": {"failed_pages": {"5": "rerun boom"}}},
        replace_paths=(("recognition", "failed_pages"),),
    )
    info = env.file_info()
    info.setdefault("bounding_boxes", {})[5] = _box_dicts()  # 旧 manual 集（页在 ledger，int 键=写回点形态）
    env.file_store.set(_FILE_ID, info)

    result = asyncio.run(env.verify())
    assert result["state"] == "manual_verified"
    boxes = env.boxes_on_page5()
    assert len(boxes) == 2
    assert all(box["id"] != "manual-0" or box["source"] == "manual" for box in boxes)
    # 失败清单再次移出
    assert "5" not in (env.recognition().get("failed_pages") or {})


# ---------------------------------------------------------------------------
# B4：框落账
# ---------------------------------------------------------------------------


def test_boxes_land_in_file_store_with_manual_source(tmp_path, monkeypatch):
    env = _Env(tmp_path, monkeypatch)
    # 预置审阅草稿 → verify 后草稿同页同集（草稿是交付期整份覆盖 file_store 的真源）
    draft = {
        "bounding_boxes": [
            {"id": "auto1", "page": 1, "x": 0.1, "y": 0.1, "width": 0.2, "height": 0.05,
             "type": "PERSON", "source": "ocr_has", "selected": True}
        ],
        "entities": [],
    }
    env.store.save_item_review_draft(env.iid, draft)
    asyncio.run(env.verify())

    boxes = env.boxes_on_page5()
    assert len(boxes) == 2
    for box in boxes:
        assert box["source"] == "manual"
        assert box["selected"] is True
        assert box["confidence"] is None
        assert box["evidence_source"] == "manual"
        assert box["page"] == 5

    draft_after = env.store.get_item_review_draft(env.iid)
    draft_p5 = [
        box for box in draft_after["bounding_boxes"]
        if int((box or {}).get("page") or 0) == 5
    ]
    assert len(draft_p5) == 2
    assert all(box["source"] == "manual" for box in draft_p5)
    # 页 1 的既有草稿框不受影响
    assert any(box["id"] == "auto1" for box in draft_after["bounding_boxes"])


# ---------------------------------------------------------------------------
# C3：manual 页不可重试
# ---------------------------------------------------------------------------


def test_retry_rejects_manual_verified_page(tmp_path, monkeypatch):
    env = _Env(tmp_path, monkeypatch)
    asyncio.run(env.verify())

    # 设计稿 §5-C3 写「409 page_not_failed」，但既有 retry 端点（issue257 契约钉
    # test_bad_pages_return_400）把 page_not_failed 映射为 400；§2.10 明确
    # page_retry_service 准入不改 ⇒ 按既有契约钉 400（报告偏差清单注记）。
    with pytest.raises(AppError) as exc:
        asyncio.run(
            retry_item_pages(env.jid, env.iid, [5], store=env.store, owner_id="apitester")
        )
    assert exc.value.status_code == 400
    assert exc.value.error_code == "page_not_failed"


# ---------------------------------------------------------------------------
# D1：审计
# ---------------------------------------------------------------------------


@pytest.fixture()
def audit_file(tmp_path, monkeypatch):
    from app.core import audit as audit_mod

    path = tmp_path / "audit.log"
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(message)s"))
    monkeypatch.setattr(audit_mod._audit_logger, "handlers", [handler])
    yield path
    handler.close()


def _audit_entries(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_audit_trail_records_operator_time_boxcount(tmp_path, monkeypatch, audit_file):
    env = _Env(tmp_path, monkeypatch)
    result = asyncio.run(env.verify())
    asyncio.run(env.revoke())

    entries = _audit_entries(audit_file)
    actions = [entry["action"] for entry in entries]
    assert actions == ["manual_verify_page", "manual_revoke_page"]
    for entry in entries:
        assert entry["resource_type"] == "job"
        assert entry["resource_id"] == env.jid
        # operator 落顶层 user（r2 F-I1）
        assert entry["user"] == "apitester"
    verify_entry = entries[0]
    assert verify_entry["detail"]["page"] == 5
    assert verify_entry["detail"]["box_count"] == 2
    assert verify_entry["detail"]["item_id"] == env.iid

    # ledger 快照：verified_at ISO8601、operator 与登录用户一致
    ledger = env.recognition().get("manual_verified_pages") or {}
    assert ledger == {}  # 已撤销
    # 重验一次读快照（第二次 verify 的时间戳独立生成）
    second = asyncio.run(env.verify())
    ledger = env.recognition().get("manual_verified_pages") or {}
    assert ledger["5"]["operator"] == "apitester"
    assert second["verified_at"] == ledger["5"]["verified_at"]
    assert result["verified_at"] != second["verified_at"]
    assert "T" in ledger["5"]["verified_at"] and ledger["5"]["verified_at"].endswith("+00:00")
