"""Issue#279 任务级复盘 summary（A2.5）。纯函数：从 JSONL 目录复算，不读 DB。

页状态权威仍是 DB 账本；本模块是可观测面聚合，一致性由门④ A2.5 三源 diff=0 判据兜。

归并口径（A2.5 注入判据钉死，brief 测试即判官）：
* 归宿面（pages_by_source / failed_pages）：页身份 = (item_id, page)——页号只在
  单卷内唯一（spec §2.1，跨卷会撞号）；同一 (item, page) 多次 page_done 按
  最后一条计 source（重试翻绿归 retry 桶）；终态为 page_failed 的页计入
  failed_pages 且不进任何 source 桶，翻绿（其后 page_done）即除名——
  「重试翻绿后 summary 失败页清零」由重算天然成立；
* 计时面（page_count / avg_page_ms / slowest_page）：按页号首见终态事件的 ms
  计——同页号的跨卷重跑与重试不重复计入耗时。
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_PAGE_EVENTS = {"page_done", "page_failed"}

def compute_summary(job_dir: Path) -> dict[str, Any]:
    pages_by_source = {"docline": 0, "page_pipeline": 0, "retry": 0}
    latest_page: dict[tuple[str, int], tuple[str, int, str]] = {}
    # (item_id, page) -> (source, ms, error_type)：同一 (item, page) 按最后一条终态计
    first_page_ms: dict[int, int] = {}   # page -> 首见终态 ms（计时面，重试/跨卷重跑不重复计）
    ner_calls = ner_ms = ocr_ms = retry_count = 0
    for path in sorted(job_dir.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            event = rec.get("event")
            if event == "ner_done":
                ner_calls += int(rec.get("calls") or 0)
                ner_ms += int(rec.get("ms") or 0)
            elif event == "ocr_done":
                ocr_ms += int(rec.get("ms") or 0)
            elif event == "retry_done":
                retry_count += 1
            elif event in _PAGE_EVENTS:
                page = rec.get("page")
                if page is None:
                    continue
                page_no = int(page)
                ms = int(rec.get("ms") or 0)
                item_key = (str(rec.get("item_id") or path.stem), page_no)
                if event == "page_failed":
                    latest_page[item_key] = ("failed", ms, str(rec.get("error_type") or "unknown"))
                else:
                    latest_page[item_key] = (str(rec.get("source") or "page_pipeline"), ms, "")
                if page_no not in first_page_ms:
                    first_page_ms[page_no] = ms
    failed_pages: dict[str, str] = {}
    for (_item, page), (source, _ms, error) in sorted(latest_page.items()):
        if source == "failed":
            failed_pages[str(page)] = error   # 终态仍失败的页；翻绿页不进桶也不列失败
        else:
            bucket = source if source in pages_by_source else "page_pipeline"
            pages_by_source[bucket] += 1
    page_ms_list = [first_page_ms[page] for page in sorted(first_page_ms)]
    avg = round(sum(page_ms_list) / len(page_ms_list)) if page_ms_list else 0
    slowest = {"page": None, "ms": 0}
    for page, ms in sorted(first_page_ms.items()):
        if ms > slowest["ms"]:
            slowest = {"page": page, "ms": ms}
    return {
        "page_count": len(first_page_ms),
        "pages_by_source": pages_by_source,
        "failed_pages": dict(sorted(failed_pages.items(), key=lambda kv: int(kv[0]))),
        "avg_page_ms": avg,
        "slowest_page": slowest,
        "ner_calls_total": ner_calls,
        "ner_ms_total": ner_ms,
        "ocr_ms_total": ocr_ms,
        "retry_count": retry_count,
        "saved_by_accelerator": 0,   # 预留（#275）
        "upgraded_pages": 0,         # 预留（#273）
        "arbitrated_pages": 0,       # 预留（#273）
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }

def write_summary(job_id: str) -> dict[str, Any] | None:
    """任务终态调一次：复算并落 summary.json（fail-open，失败仅告警）。"""
    from app.services.page_event_log import _warn_once, job_log_dir
    try:
        job_dir = job_log_dir(job_id)
        if not job_dir.exists():
            return None
        summary = compute_summary(job_dir)
        target = job_dir / "summary.json"
        # 尾随换行与复算脚本的 print 口径对齐——stdout 与 summary.json 逐字节一致（A2.5/AC6）
        target.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return summary
    except Exception as exc:  # noqa: BLE001 - fail-open
        _warn_once("summary_write", f"page_event_summary: write failed ({exc})")
        return None
