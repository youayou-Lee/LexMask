"""speedkit.driver：`single` / `batch` 两条生产链路的测量驱动。

- **single**：`SpeedApi`（继承 `eval-ng.engine.e2e.E2eApi`）逐页 `POST /api/v1/redaction/{id}/vision`
  ，**显式带 body**（`selected_ocr_has_types` / `selected_visual_feature_types`）——不传即
  `has_request=False` 回落默认类型集，与锚点装置不可比（D1④/M7）。harness 用 `time.perf_counter()`
  **在客户端侧**包住每次请求，与后端逐页 `duration_ms` 分列成两条序列（D3①b）。
  页并发 k = **客户端并发请求数**（与 #106 前端探针同构）——另一证 = 驱动器实测在飞峰值（D2①）。
- **batch**：`POST /api/v1/jobs`（smart_batch）→ 上传（带 `job_id`/`upload_source=batch`）→
  `POST /api/v1/jobs/{id}/submit` → 轮询 `GET /api/v1/jobs/{id}` 至终态。逐页仪表**只**从该响应
  的 `items[*].performance.recognition.pages[*]` 取（`duration_ms` + `duration_breakdown_ms`
  + `page_concurrency`）——不复用 `benchmark_throughput.py` 的 `endswith` 后缀归并（M6）。
- **两个墙钟并列**（D4②）：`file_wall_s` 用 **wall-clock（epoch）** 测（提交→末页完成），
  `driver_self_wall_s` 用 `perf_counter` 测；两者不同源，比值 ≤5% 才算自足。
- **零 spawn（业务动作）**：`cold` 要求的「逐计时轮重启后端」由**编排薄脚本**在轮间执行
  （`--round-restart-cmd`），证据来自 `--round-evidence`（`rounds.json`）；驱动只执行编排给的
  重启命令并**读**证据做机器校验（D2②），绝不自己起停服务、绝不改工作树。
- **加密卷语义（D6③）**：声明 `expect_reject` 的条目**仍必须发起尝试**，失败后记
  `unsupported:true` + `reason`，且**该条不含任何数值型耗时字段**；未失败 ⇒ FAIL。
- **质量旁证全部从响应取**（D8①）：`box_count`/`entity_count`/`has_text_ner_finish_reason`/
  `has_text_ner_truncation_retries`/`has_text_ner_rebatch_batches`——**不读任何日志文件**。
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import mimetypes
import os
import statistics
import threading
import time
from pathlib import Path

from ..e2e import E2eApi, percentile
from . import collect

# 锚点可比的钉死载荷（与验收方案 §2 同源；默认即此表，绝不空 body）
ANCHOR_OCR_TYPES: tuple[str, ...] = (
    "CASE_NUMBER", "BIRTH_DATE", "ADDRESS", "PHONE", "WORK_UNIT", "COMPANY_NAME", "PASSPORT",
    "INSTITUTION_NAME", "GOVERNMENT_AGENCY", "NATIVE_PLACE", "BANK_NAME", "AGE", "ID_CARD",
    "PERSON", "BANK_CARD", "EMAIL", "LEGAL_CASE_ID", "LICENSE_PLATE", "DATE",
)
ANCHOR_VISUAL_TYPES: tuple[str, ...] = ("official_seal", "signature")
PAYLOAD_SOURCE_DEFAULT = "default"
PAYLOAD_SOURCE_FILE = "file"

POLL_INTERVAL_SEC = 5.0
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "awaiting_review", "review_approved"})
FAILED_STATUSES = frozenset({"failed", "cancelled"})

MAX_DISCRETION_RATIO = 0.20     # D2③：跨计时轮离散上界
MIN_TIMING_ROUNDS = 3
MAX_TIMING_ROUNDS = 4           # 总轮数上限 5（含 1 个 warmup 轮）
WARMUP_ROUNDS = 1

# 质量旁证字段（D8①）：全部从响应取，键名与后端仪表一致
SIDECAR_KEYS: tuple[str, ...] = (
    "has_text_ner_finish_reason", "has_text_ner_truncation_retries",
    "has_text_ner_rebatch_batches", "has_text_cache_status",
)
# D4② 双墙钟一致性上界
WALL_AGREEMENT_TOLERANCE = 0.05


def payload_sha256(payload: dict) -> str:
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def effective_payload(payload_file: str | None) -> tuple[dict, str, str]:
    """返回 (payload, source, sha256)。无 `--payload-file` ⇒ 钉死默认集（不空 body）。"""
    if payload_file:
        data = json.loads(Path(payload_file).read_text(encoding="utf-8"))
        ocr = list(data.get("selected_ocr_has_types") or [])
        visual = list(data.get("selected_visual_feature_types") or [])
        if not ocr or not visual:
            raise ValueError(
                f"--payload-file {payload_file} 的两类型集不得为空（D1④）：ocr={len(ocr)} visual={len(visual)}"
            )
        payload = {"selected_ocr_has_types": ocr, "selected_visual_feature_types": visual}
        return payload, PAYLOAD_SOURCE_FILE, payload_sha256(payload)
    payload = {
        "selected_ocr_has_types": list(ANCHOR_OCR_TYPES),
        "selected_visual_feature_types": list(ANCHOR_VISUAL_TYPES),
    }
    return payload, PAYLOAD_SOURCE_DEFAULT, payload_sha256(payload)


def pdftotext_nonblank_chars(path: str) -> int | None:
    """前提机检（D1④ 唯一定义）：`pdftotext "$f" - | tr -d '[:space:]' | wc -c`。

    失败（无 pdftotext / 非 PDF / 报错）返回 None——**不得**用 0 冒充「已机检」。
    """
    import subprocess

    try:
        out = subprocess.run(["pdftotext", path, "-"], capture_output=True, timeout=60, check=False)
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        return None
    if out.returncode != 0:
        return None
    text = out.stdout.decode("utf-8", errors="ignore")
    return len("".join(text.split()))


# ── API 客户端（复用 E2eApi；只在此处扩展 body 与 jobs 面） ──────────────────
class SpeedApi(E2eApi):
    """`E2eApi` 的速度测量扩展：vision 带 body + jobs 面（batch 线）。"""

    def upload(self, path: Path, job_id: str | None = None) -> str:
        name = Path(path).name
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        data = {"job_id": job_id, "upload_source": "batch"} if job_id else None
        with open(path, "rb") as handle:
            r = self.client.post("/api/v1/files/upload", files={"file": (name, handle, mime)}, data=data)
        r.raise_for_status()
        body = r.json()
        return body.get("file_id") or body.get("id")

    def vision(self, file_id: str, page: int, force: bool = True, payload: dict | None = None) -> dict:
        r = self.client.post(
            f"/api/v1/redaction/{file_id}/vision",
            params={"page": page, "force": str(force).lower(), "include_result_image": "false"},
            json=payload,
        )
        if r.status_code != 200:
            raise VisionRejected(r.status_code, r.text[:400])
        return r.json()

    def parse_meta(self, file_id: str) -> dict:
        r = self.client.get(f"/api/v1/files/{file_id}/parse")
        if r.status_code != 200:
            raise RuntimeError(f"parse {file_id} -> HTTP {r.status_code}: {r.text[:200]}")
        return r.json()

    def health_version(self) -> str | None:
        r = self.client.get("/health")
        if r.status_code != 200:
            return None
        body = r.json() if hasattr(r, "json") else {}
        return str(body.get("version")) if body.get("version") else None

    def default_type_ids(self) -> list[str]:
        r = self.client.get("/api/v1/custom-types", params={"enabled_only": True})
        if r.status_code != 200:
            return []
        types = (r.json() or {}).get("custom_types") or []
        ids = [t.get("id") for t in types if t.get("default_enabled")]
        return [i for i in ids if i]

    def create_job(self, *, title: str, type_ids: list[str], payload: dict) -> str:
        body = {
            "job_type": "smart_batch",
            "title": title,
            "config": {
                "entity_type_ids": type_ids,
                "ocr_has_types": list(payload["selected_ocr_has_types"]),
                "visual_feature_types": list(payload["selected_visual_feature_types"]),
                "replacement_mode": "mask",
            },
            "skip_item_review": True,
        }
        r = self.client.post("/api/v1/jobs", json=body)
        if r.status_code not in (200, 201):
            raise RuntimeError(f"create job -> HTTP {r.status_code}: {r.text[:300]}")
        return (r.json() or {}).get("id")

    def submit_job(self, job_id: str) -> None:
        r = self.client.post(f"/api/v1/jobs/{job_id}/submit")
        if r.status_code not in (200, 202):
            raise RuntimeError(f"submit job {job_id} -> HTTP {r.status_code}: {r.text[:200]}")

    def job_detail(self, job_id: str) -> dict:
        r = self.client.get(f"/api/v1/jobs/{job_id}")
        if r.status_code != 200:
            raise RuntimeError(f"job detail {job_id} -> HTTP {r.status_code}: {r.text[:200]}")
        return r.json()


class VisionRejected(RuntimeError):
    """vision 端点明确拒绝（4xx/5xx）——加密卷等 `expect_reject` 场景据此判。"""

    def __init__(self, status_code: int, body: str):
        super().__init__(f"vision -> HTTP {status_code}: {body}")
        self.status_code = status_code
        self.body = body


# ── 墙钟对（D4②：两个不同源的时钟并列） ────────────────────────────────────
def _wall_pair(epoch_start: float, mono_start: float) -> tuple[float, float, float, float]:
    return epoch_start, mono_start, 0.0, 0.0


def _close_wall(epoch_start: float, mono_start: float) -> tuple[float, float]:
    """返回 (file_wall_s=epoch 跨度, driver_self_wall_s=monotonic 跨度)。"""
    return round(time.time() - epoch_start, 6), round(time.perf_counter() - mono_start, 6)


# ── single 线 ──────────────────────────────────────────────────────────────
class _InFlight:
    """在飞计数：D2①「驱动自报 k + 实测反推 k」的实测侧证据。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.current = 0
        self.peak = 0

    def enter(self) -> None:
        with self._lock:
            self.current += 1
            self.peak = max(self.peak, self.current)

    def leave(self) -> None:
        with self._lock:
            self.current -= 1


def sidecar_from_response(resp: dict) -> dict:
    """质量旁证 + 计数（全部从响应取；D8①）。"""
    boxes = resp.get("bounding_boxes")
    box_count = len(boxes) if isinstance(boxes, list) else None
    entity_count = None
    if isinstance(boxes, list):
        entity_count = sum(1 for b in boxes if isinstance(b, dict) and str(b.get("text") or "").strip())
    stage = {}
    for _name, block in (resp.get("pipeline_status") or {}).items():
        if isinstance(block, dict) and isinstance(block.get("stage_duration_ms"), dict):
            for key, value in block["stage_duration_ms"].items():
                if key in SIDECAR_KEYS and not collect.is_number(value):
                    stage.setdefault(key, value)
    return {"box_count": box_count, "entity_count": entity_count, **stage}


def measure_single_round(
    api: SpeedApi, corpus_items: list[dict], *, payload: dict, page_concurrency: int,
    cleanup: bool, upload_workers: int = 1,
) -> dict:
    """跑一轮 single：逐卷上传 → 逐页请求（客户端并发 k）→ 逐卷删除。"""
    inflight = _InFlight()
    records: list[dict] = []
    lock = threading.Lock()
    items_view: list[dict] = []
    rejected: list[dict] = []
    sidecars: dict[str, dict] = {}
    epoch_start, mono_start, _, _ = _wall_pair(time.time(), time.perf_counter())

    for item in corpus_items:
        item_id = str(item["id"])
        path = Path(item["path"])
        pages = int(item["pages"])
        file_id = api.upload(path)
        meta: dict = {}
        try:
            meta = api.parse_meta(file_id)
        except Exception:  # noqa: BLE001 —— 前提机检尽力而为，失败不阻断计时
            meta = {}
        page_records: list[dict] = []
        unsupported = False
        reason = None
        if item.get("expect_reject"):
            try:
                api.vision(file_id, 1, payload=payload)
                rejected.append({"id": item_id, "unsupported": False,
                                 "reason": "声明 expect_reject 的条目却成功了（D6③ 判 FAIL）"})
            except VisionRejected as exc:
                rejected.append({"id": item_id, "unsupported": True, "reason": exc.body[:200],
                                 "http_status": exc.status_code})
                unsupported = True
                reason = f"HTTP {exc.status_code}"
            except Exception as exc:  # noqa: BLE001
                rejected.append({"id": item_id, "unsupported": True, "reason": repr(exc)[:200]})
                unsupported = True
                reason = repr(exc)[:120]
            if cleanup:
                api.delete_file(file_id)
            items_view.append({
                "id": item_id, "pages": pages, "file_id": file_id, "unsupported": unsupported,
                "reason": reason, "md5": item.get("md5"), "parse": _parse_view(meta),
                "box_count": None, "entity_count": None,
            })
            continue

        item_totals = {"box_count": 0, "entity_count": 0}

        def one(page: int, *, _fid=file_id, _item_id=item_id, _records=page_records,
                _totals=item_totals) -> None:
            inflight.enter()
            try:
                t0_mono = time.perf_counter()
                resp = api.vision(_fid, page, payload=payload)
                t1_mono = time.perf_counter()
            finally:
                inflight.leave()
            signal = collect.page_signal(resp, wall_s=t1_mono - t0_mono,
                                         harness_span=(t0_mono, t1_mono), page_no=page)
            signal["item_id"] = _item_id
            side = sidecar_from_response(resp)
            with lock:
                _records.append(signal)
                for key in ("box_count", "entity_count"):
                    if collect.is_number(side.get(key)):
                        _totals[key] += int(side[key])
                for key, value in side.items():
                    if value is not None:
                        sidecars.setdefault(key, value)
                        if collect.is_number(value):
                            sidecars[key] = max(sidecars.get(key, 0), value) if key.endswith("_count") \
                                else sidecars.get(key, 0) + value if key.endswith(("_retries", "_batches")) \
                                else sidecars[key]

        if page_concurrency <= 1:
            for page in range(1, pages + 1):
                one(page)
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=page_concurrency) as pool:
                list(pool.map(one, range(1, pages + 1)))
        if cleanup:
            api.delete_file(file_id)
        page_records.sort(key=lambda r: (r.get("page") or 0))
        with lock:
            records.extend(page_records)
        items_view.append({
            "id": item_id, "pages": pages, "file_id": file_id, "unsupported": unsupported,
            "reason": reason, "md5": item.get("md5"), "parse": _parse_view(meta),
            "box_count": item_totals["box_count"] or None,
            "entity_count": item_totals["entity_count"] or None,
        })

    file_wall_s, driver_self_wall_s = _close_wall(epoch_start, mono_start)
    agg = collect.sum_pages(records)
    agg["observed_page_concurrency"] = inflight.peak
    return {
        "pages": records,
        "agg": agg,
        "file_wall_s": file_wall_s,
        "driver_self_wall_s": driver_self_wall_s,
        "items": items_view,
        "rejected_items": rejected,
        "sidecar": _finalize_sidecar(sidecars),
        "harness_start_ts": None,
        "harness_end_ts": None,
    }


def _finalize_sidecar(sidecars: dict) -> dict:
    out: dict = {}
    for key, value in sidecars.items():
        out[key] = value
    return out


def _parse_view(meta: dict) -> dict:
    return {
        "file_type": meta.get("file_type"),
        "is_scanned": meta.get("is_scanned"),
        "total_pages": meta.get("total_pages") or meta.get("page_count"),
    }


# ── batch 线 ───────────────────────────────────────────────────────────────
def wait_job(api: SpeedApi, job_id: str, *, timeout_sec: float, progress=None) -> dict:
    started = time.perf_counter()
    deadline = started + timeout_sec
    detail = api.job_detail(job_id)
    while True:
        items = detail.get("items") or []
        done = [i for i in items if str(i.get("status")).lower() in TERMINAL_STATUSES]
        if progress:
            progress(len(done), len(items), detail.get("status"))
        if items and len(done) == len(items):
            break
        if time.perf_counter() > deadline:
            break
        time.sleep(POLL_INTERVAL_SEC)
        detail = api.job_detail(job_id)
    return detail


def measure_batch_round(
    api: SpeedApi, corpus_items: list[dict], *, payload: dict, type_ids: list[str], label: str,
    timeout_sec: float, upload_workers: int = 4, cleanup: bool = False, progress=None,
) -> dict:
    """跑一轮 batch：建作业 → 上传 → 提交 → 轮询 → 取逐页仪表。"""
    files = [str(it["path"]) for it in corpus_items]
    job_id = api.create_job(title=f"speedkit-{label}", type_ids=type_ids, payload=payload)
    upload_started = time.perf_counter()
    sizes: dict[str, int] = {}
    for path in files:
        try:
            sizes[path] = os.path.getsize(path)
        except OSError:
            sizes[path] = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, upload_workers)) as pool:
        list(pool.map(lambda p: api.upload(p, job_id), files))
    upload_sec = time.perf_counter() - upload_started

    epoch_start, mono_start, _, _ = _wall_pair(time.time(), time.perf_counter())
    api.submit_job(job_id)
    detail = wait_job(api, job_id, timeout_sec=timeout_sec, progress=progress)
    file_wall_s, driver_self_wall_s = _close_wall(epoch_start, mono_start)

    items = detail.get("items") or []
    statuses: dict[str, int] = {}
    pages: list[dict] = []
    sidecar: dict = {}
    for item in items:
        status = str(item.get("status") or "")
        statuses[status] = statuses.get(status, 0) + 1
        perf = item.get("performance") or {}
        recognition = perf.get("recognition") or {}
        raw_pages = recognition.get("pages")
        entries = list(raw_pages.values()) if isinstance(raw_pages, dict) else list(raw_pages or [])
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            signal = collect.batch_page_signal(entry)
            pages.append(signal)
        for key in SIDECAR_KEYS:
            value = recognition.get(key) if isinstance(recognition, dict) else None
            if value is not None:
                sidecar[key] = value
        for key in ("box_count", "entity_count"):
            value = recognition.get(key) if isinstance(recognition, dict) else None
            if collect.is_number(value):
                sidecar[key] = sidecar.get(key, 0) + float(value)
    pages.sort(key=lambda r: (r.get("page") or 0))
    agg = collect.sum_pages(pages)
    queue_waits = [float(i["queue_wait_ms"]) for i in items if collect.is_number(i.get("queue_wait_ms"))]
    return {
        "pages": pages,
        "agg": agg,
        "file_wall_s": file_wall_s,
        "driver_self_wall_s": driver_self_wall_s,
        "items": [{"id": it.get("id"), "status": it.get("status")} for it in items],
        "rejected_items": [],
        "sidecar": sidecar,
        "batch": {
            "job_id": job_id,
            "job_status": detail.get("status"),
            "item_statuses": statuses,
            "items_total": len(items),
            "items_terminal": sum(v for k, v in statuses.items() if k.lower() in TERMINAL_STATUSES),
            "failed_items": sum(v for k, v in statuses.items() if k.lower() in FAILED_STATUSES),
            "upload_sec": round(upload_sec, 3),
            "max_upload_bytes": max(sizes.values()) if sizes else 0,
            "recognition_page_concurrency": _dig_number(detail, ("performance", "recognition", "page_concurrency")),
            "recognition_page_concurrency_configured": _dig_number(
                detail, ("performance", "recognition", "page_concurrency_configured")
            ),
            "job_queue_wait_ms": (queue_waits[0] if queue_waits else _dig_number(detail, ("queue_wait_ms",))),
        },
    }


def _dig_number(doc: dict, path: tuple[str, ...]):
    cur = doc
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return float(cur) if collect.is_number(cur) else None


# ── 轮次合法性（D1⑤）与统计（D2③） ─────────────────────────────────────────
def round_valid(record: dict, *, driver: str, expected_pages: int | None) -> tuple[bool, str | None]:
    pages = record.get("pages") or []
    if not pages:
        unsupported = [r for r in (record.get("rejected_items") or []) if r.get("unsupported")]
        if unsupported and driver == "single":
            return True, None
        return False, "该轮零页（空报告不得冒充成功）"
    if expected_pages and len(pages) != expected_pages:
        return False, f"页数 {len(pages)} ≠ 清单 {expected_pages}"
    if driver == "batch":
        batch = record.get("batch") or {}
        if batch.get("failed_items"):
            return False, f"batch 有失败 item（{batch.get('failed_items')} 条）"
        if batch.get("items_total") and batch.get("items_terminal") != batch.get("items_total"):
            return False, "轮询超时，item 未全部到终态"
    return True, None


def wall_stats(values: list[float]) -> dict:
    values = [float(v) for v in values]
    if not values:
        return {"n": 0, "mean": None, "p50": None, "p95": None, "min": None, "max": None,
                "discretion_ratio": None}
    ordered = sorted(values)
    mean = statistics.fmean(ordered)
    spread = (ordered[-1] - ordered[0]) / mean if mean else 0.0
    return {
        "n": len(ordered),
        "mean": round(mean, 3),
        "p50": round(percentile(ordered, 50), 3),
        "p95": round(percentile(ordered, 95), 3),
        "min": round(ordered[0], 3),
        "max": round(ordered[-1], 3),
        "discretion_ratio": round(spread, 6),
    }


def wall_agreement(walls: list[tuple[float, float]]) -> dict:
    """D4②(ii)：`file_wall_s`（epoch）与驱动自测墙钟（perf_counter）的逐轮比对。"""
    pairs = [(float(a), float(b)) for a, b in walls if collect.is_number(a) and collect.is_number(b)]
    if not pairs:
        return {"n": 0, "max_abs_ratio_diff": None, "within_tolerance": False, "pairs": []}
    diffs = [abs(a - b) / a if a else 0.0 for a, b in pairs]
    return {
        "n": len(pairs),
        "max_abs_ratio_diff": round(max(diffs), 6),
        "within_tolerance": max(diffs) <= WALL_AGREEMENT_TOLERANCE,
        "pairs": [{"file_wall_s": round(a, 6), "driver_self_wall_s": round(b, 6)} for a, b in pairs],
    }


def timing_rounds_needed(stats: dict) -> tuple[bool, str]:
    """D2③：n<3 或离散 >20% ⇒ 需补轮；补满上限仍 >20% ⇒ UNVERIFIED。"""
    n = stats.get("n") or 0
    ratio = stats.get("discretion_ratio")
    if n < MIN_TIMING_ROUNDS:
        return True, f"计时轮 {n} < {MIN_TIMING_ROUNDS}"
    if ratio is not None and ratio > MAX_DISCRETION_RATIO:
        if n < MAX_TIMING_ROUNDS:
            return True, f"跨轮离散 {ratio:.1%} > {MAX_DISCRETION_RATIO:.0%}，需补到总 {MAX_TIMING_ROUNDS + WARMUP_ROUNDS} 轮"
        return False, (f"补满 {MAX_TIMING_ROUNDS} 个计时轮后离散仍 {ratio:.1%} > {MAX_DISCRETION_RATIO:.0%}"
                       " ⇒ UNVERIFIED（既非 PASS 也非 FAIL）")
    return False, f"计时轮 n={n}，离散 {ratio:.1%} ≤ {MAX_DISCRETION_RATIO:.0%}"


# ── 冷跑证据（D2②）与并发双证（D2①） ──────────────────────────────────────
def load_round_evidence(path: str | None) -> dict:
    if not path:
        return {}
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("rounds"), list):
        raise ValueError(f"--round-evidence {path} 结构非法：需 {{\"rounds\": [...]}}")
    return data


def _entry_for(evidence: dict, round_no: int) -> dict | None:
    for entry in evidence.get("rounds") or []:
        try:
            if int(entry.get("round")) == round_no:
                return entry
        except (TypeError, ValueError):
            continue
    return None


def cold_evidence_checks(evidence: dict, rounds: list[dict], *, cache_policy: str,
                         driver: str, api_base: str, restart_cmd: str | None) -> list[dict]:
    """逐计时轮的机器证据：pid + /proc start_time + 时间戳；相邻计时轮必须换 pid。"""
    checks: list[dict] = []
    timing = [r for r in rounds if not r.get("warmup") and not r.get("invalid")]

    if cache_policy != "cold":
        checks.append({"id": "D2.cache_policy", "status": "UNVERIFIED",
                       "detail": f"cache_policy={cache_policy}（warm 轮，命中率不作冷跑判据）", "value": None})
    else:
        if not timing:
            checks.append({"id": "D2.cold_evidence", "status": "BLOCKED",
                           "detail": "无有效计时轮，冷跑证据无从判定", "value": 0})
        else:
            for entry in timing:
                got = _entry_for(evidence, int(entry.get("round") or 0)) or {}
                missing = [k for k in ("pid", "start_time", "timestamp") if not got.get(k)]
                if missing:
                    checks.append({"id": "D2.cold_evidence", "status": "FAIL",
                                   "detail": f"计时轮 {entry.get('round')} 缺重启证据字段："
                                             f"{', '.join(missing)}（--round-evidence 由编排逐轮重启后写出）",
                                   "value": None})
                    break
            else:
                pids = [(_entry_for(evidence, int(r.get("round") or 0)) or {}).get("pid") for r in timing]
                if len(set(pids)) != len(pids):
                    checks.append({"id": "D2.cold_evidence", "status": "FAIL",
                                   "detail": f"计时轮 pid 出现重复（{pids}）⇒ 未逐轮重启，冷跑不成立",
                                   "value": pids})
                else:
                    checks.append({"id": "D2.cold_evidence", "status": "PASS",
                                   "detail": f"{len(timing)} 个计时轮各带 pid/start_time/时间戳，pid 互不相同：{pids}",
                                   "value": len(timing)})
        if not restart_cmd:
            checks.append({"id": "D2.restart_cmd", "status": "UNVERIFIED",
                           "detail": "未给 --round-restart-cmd：轮间重启由编排在调用外部完成（须有 --round-evidence 佐证）",
                           "value": None})
    env = evidence.get("env") or {}
    checks.append({"id": "D2.env_snapshot", "status": "PASS" if env else "UNVERIFIED",
                   "detail": ("生效开关集已由编排记录：" + ", ".join(sorted(env))) if env
                             else "round-evidence 未附 env（生效开关集不可核）", "value": len(env)})
    ready = evidence.get("ready_wait_sec")
    settle = evidence.get("settle_sec")
    checks.append({"id": "D5.readiness_probe",
                   "status": "PASS" if (collect.is_number(ready) and collect.is_number(settle)) else "FAIL",
                   "detail": (f"就绪等待 {ready}s + 静置 {settle}s（脚注第 8 项）"
                              if collect.is_number(ready) and collect.is_number(settle)
                              else "round-evidence 缺 ready_wait_sec / settle_sec（脚注第 8 项必须实测）"),
                   "value": ready})
    checks.append({"id": "D2.api_base", "status": "PASS",
                   "detail": f"api_base={api_base}；driver={driver}", "value": None})
    return checks


def env_matches_arm(evidence: dict, arm: dict) -> list[dict]:
    """臂声明的 env 必须与实际生效集一致（D2① 自报侧证据）。"""
    checks: list[dict] = []
    declared = {str(k): str(v) for k, v in (arm.get("env") or {}).items()}
    actual = {str(k): str(v) for k, v in ((evidence or {}).get("env") or {}).items()}
    if not declared:
        return checks
    missing = [k for k in declared if k not in actual]
    mismatched = [k for k, v in declared.items() if k in actual and str(actual[k]) != v]
    if missing:
        checks.append({"id": "D2.arm_env", "status": "FAIL",
                       "detail": f"臂声明 env 未在生效集里出现：{', '.join(missing)}", "value": None})
    elif mismatched:
        checks.append({"id": "D2.arm_env", "status": "FAIL",
                       "detail": f"臂声明 env 与生效值不符：{', '.join(mismatched)}", "value": None})
    else:
        checks.append({"id": "D2.arm_env", "status": "PASS",
                       "detail": f"臂声明的 {len(declared)} 个 env 与生效集一致", "value": len(declared)})
    return checks


def concurrency_dual_evidence(rounds: list[dict], declared_k: int, driver: str) -> list[dict]:
    """D2①：驱动自报 k 与实测反推并置（single=在飞峰值；batch=后端回吐页并发）。"""
    observed: list[int] = []
    for entry in rounds:
        if entry.get("warmup") or entry.get("invalid"):
            continue
        agg = entry.get("agg") or {}
        value = agg.get("observed_page_concurrency") if driver == "single" else agg.get("page_concurrency_peak")
        if collect.is_number(value):
            observed.append(int(value))
    if not observed:
        return [{"id": "D2.k_dual_evidence", "status": "UNVERIFIED",
                 "detail": "无计时轮可反推页并发", "value": None}]
    peak = max(observed)
    ok = peak == declared_k
    label = "实测在飞峰值" if driver == "single" else "后端回吐页并发峰值"
    return [{"id": "D2.k_dual_evidence", "status": "PASS" if ok else "FAIL",
             "detail": f"声明 k={declared_k}；{label}={peak}" + ("" if ok else " ⇒ 两证不一致"),
             "value": peak}]


def expect_reject_checks(rounds: list[dict], corpus_items: list[dict]) -> list[dict]:
    """D6③：声明 `expect_reject` 的条目必须被打到拒绝，且不带任何数值耗时。"""
    checks: list[dict] = []
    declared = [str(it["id"]) for it in corpus_items if it.get("expect_reject")]
    if not declared:
        return checks
    seen: dict[str, dict] = {}
    for entry in rounds:
        for rec in entry.get("rejected_items") or []:
            seen[str(rec.get("id"))] = rec
    for item_id in declared:
        rec = seen.get(item_id)
        if not rec:
            checks.append({"id": "D6.expect_reject", "status": "FAIL",
                           "detail": f"{item_id} 声明 expect_reject 却未见拒绝记录（静默吞掉）", "value": None})
        elif not rec.get("unsupported"):
            checks.append({"id": "D6.expect_reject", "status": "FAIL",
                           "detail": f"{item_id} 声明 expect_reject 却成功了：{rec.get('reason')}", "value": None})
        else:
            checks.append({"id": "D6.expect_reject", "status": "PASS",
                           "detail": f"{item_id} 已发起尝试并结构化拒绝（HTTP {rec.get('http_status')}）："
                                     f"{str(rec.get('reason'))[:80]}", "value": None})
    return checks


def quality_sidecar_check(sidecar: dict, rounds: list[dict]) -> list[dict]:
    """D8①：旁证字段全部来自响应（这里断言其存在与数值一致性由单测锁定）。"""
    keys = [k for k in sidecar if sidecar.get(k) is not None]
    return [{"id": "D8.sidecar_from_response",
             "status": "PASS" if keys else "UNVERIFIED",
             "detail": (f"旁证字段取自响应：{', '.join(sorted(keys))}") if keys
                       else "本轮未取到旁证字段（响应未回吐）", "value": len(keys)}]
