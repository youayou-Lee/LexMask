"""eval-ng E2E 层：镜像旧 run_eval.py e2e 口径（squash 域 P/R + 原串域 digital + warmup +
doc_level 特判 + 加密拒绝 + gt=null 速度条目 + 失败隔离 + 逐文件桶求和聚合）。

AC2（E2E 同题同分）的实现侧；打分函数仍走 nerq 单一源（AC5）。
旧 eval/scripts/run_eval.py 冻结只读，本模块不做 import（防耦合），靠 parity 单测锁定同判。
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from .scorers._nerq_source import load_backend_nerq

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PRESET_PATH = _REPO_ROOT / "backend" / "config" / "preset_entity_types.json"

_WS_RE = re.compile(r"\s+")


def _load_preset() -> dict[str, str]:
    preset = json.loads(_PRESET_PATH.read_text(encoding="utf-8"))
    return {tid: spec["name"] for tid, spec in preset.items()}


TYPE_ID_TO_NAME: dict[str, str] = _load_preset()


def squash(text: str) -> str:
    """去全部空白（与旧 common_api.squash 同语义，parity 单测锁定）。"""
    return _WS_RE.sub("", str(text or ""))


def extract_page_entities(vision_resp: dict) -> dict[str, list[str]]:
    """同旧 common_api.extract_page_entities：type ID 归一中文，text 原样 strip。"""
    entities: dict[str, list[str]] = {}
    for box in vision_resp.get("bounding_boxes") or []:
        text = str(box.get("text") or "").strip()
        if not text:
            continue
        type_id = str(box.get("type") or "")
        entities.setdefault(TYPE_ID_TO_NAME.get(type_id, type_id), []).append(text)
    return entities


class E2eApi:
    """backend 生产 API 最小封装（同步 httpx，trust_env=False 同旧 EvalApi）。"""

    DOC_LEVEL_CARRIERS = {"docx", "txt"}

    def __init__(self, base_url: str, username: str, password: str, timeout: float = 600.0):
        import httpx

        self.client = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout, trust_env=False)
        r = self.client.post("/api/v1/auth/login", json={"username": username, "password": password})
        if r.status_code != 200:
            r = self.client.post("/api/v1/auth/register",
                                 json={"username": username, "password": password})
            r.raise_for_status()
            r = self.client.post("/api/v1/auth/login",
                                 json={"username": username, "password": password})
        r.raise_for_status()
        self.client.headers["Authorization"] = f"Bearer {r.json()['access_token']}"

    def upload(self, path: Path) -> str:
        with open(path, "rb") as f:
            r = self.client.post("/api/v1/files/upload",
                                 files={"file": (Path(path).name, f, "application/octet-stream")})
        r.raise_for_status()
        return r.json()["file_id"]

    def vision(self, file_id: str, page: int, force: bool = True) -> dict:
        r = self.client.post(f"/api/v1/redaction/{file_id}/vision",
                             params={"page": page, "force": str(force).lower(),
                                     "include_result_image": "false"})
        if r.status_code != 200:
            raise RuntimeError(f"vision {file_id} p{page} -> HTTP {r.status_code}: {r.text[:300]}")
        return r.json()

    def parse_and_hybrid_ner(self, file_id: str) -> tuple[dict[str, list[str]], float]:
        started = time.perf_counter()
        r = self.client.get(f"/api/v1/files/{file_id}/parse")
        if r.status_code != 200:
            raise RuntimeError(f"parse {file_id} -> HTTP {r.status_code}: {r.text[:200]}")
        r = self.client.post(f"/api/v1/files/{file_id}/ner/hybrid", json={})
        if r.status_code != 200:
            raise RuntimeError(f"ner/hybrid {file_id} -> HTTP {r.status_code}: {r.text[:200]}")
        body = r.json()
        if body.get("recognition_failed"):
            raise RuntimeError(f"ner/hybrid {file_id} recognition_failed: {str(body.get('error'))[:200]}")
        entities: dict[str, list[str]] = {}
        for e in body.get("entities") or []:
            text = str(e.get("text") or "").strip()
            if not text:
                continue
            type_id = str(e.get("type") or "")
            entities.setdefault(TYPE_ID_TO_NAME.get(type_id, type_id), []).append(text)
        return entities, round(time.perf_counter() - started, 3)

    def delete_file(self, file_id: str) -> bool:
        r = self.client.delete(f"/api/v1/files/{file_id}")
        return r.status_code in (200, 204)

    def close(self) -> None:
        self.client.close()


def iter_pages_with_timing(api: E2eApi, file_id: str, total_pages: int,
                           warmup_pages: int = 0) -> list[dict]:
    results = []
    for page in range(1, total_pages + 1):
        t0 = time.perf_counter()
        resp = api.vision(file_id, page)
        wall = round(time.perf_counter() - t0, 3)
        results.append({
            "page": page,
            "warmup": page <= warmup_pages,
            "wall_s": wall,
            "duration_ms": resp.get("duration_ms") or {},
            "pipeline_status": resp.get("pipeline_status") or {},
            "entities": extract_page_entities(resp),
        })
    return results


def percentile(values: list, p: float) -> float:
    """与旧 run_eval.percentile 同语义（P 分位，线性插值）。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    import math

    k = (len(ordered) - 1) * p / 100
    lower = math.floor(k)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (k - lower)


# ---------------- 口径核心（与 run_eval 逐函数同构） ----------------

def _squash_entities(entities: dict[str, list[str]]) -> dict[str, list[str]]:
    return {etype: [squash(v) for v in values] for etype, values in entities.items()}


def e2e_digital(gt_raw: dict[str, list[str]], pred_raw: dict[str, list[str]]) -> dict:
    nerq = load_backend_nerq()
    summary = {}
    for etype in nerq.DIGITAL_GATE_TYPES + nerq.REFERENCE_STRICT_TYPES:
        if etype not in gt_raw:
            continue
        graded = nerq.grade_digital(gt_raw.get(etype, []), pred_raw.get(etype, []))
        total = len(graded["exact"]) + len(graded["near_miss"]) + len(graded["miss"])
        summary[etype] = {"total_gt": total,
                          "exact_rate": len(graded["exact"]) / total if total else 1.0,
                          "exact": len(graded["exact"]), "near_miss": len(graded["near_miss"]),
                          "miss": len(graded["miss"]),
                          "near_miss_detail": graded["near_miss"][:20],
                          "miss_detail": graded["miss"][:20]}
    return summary


def loose_span_metrics(records: list[dict]) -> dict:
    gt_sq: dict[str, set[str]] = {}
    pred_sq: dict[str, set[str]] = {}
    for rec in records:
        for etype, values in rec["gt"].items():
            gt_sq.setdefault(etype, set()).update(values)
        for etype, values in rec["pred"].items():
            pred_sq.setdefault(etype, set()).update(values)
    all_gt_values = {v for vs in gt_sq.values() for v in vs}
    wrong_type = 0
    confusion: dict[str, int] = {}
    for ptype, values in pred_sq.items():
        for value in values:
            if value in all_gt_values and value not in gt_sq.get(ptype, set()):
                wrong_type += 1
                gtype = next(t for t, vs in gt_sq.items() if value in vs)
                confusion[f"{gtype}→{ptype}"] = confusion.get(f"{gtype}→{ptype}", 0) + 1
    return {"wrong_type": wrong_type,
            "type_confusion_top": dict(sorted(confusion.items(), key=lambda kv: -kv[1])[:10])}


def e2e_core_metrics(records: list[dict], gt_raw: dict[str, list[str]],
                     pred_raw: dict[str, list[str]]) -> dict:
    nerq = load_backend_nerq()
    metrics = nerq.compute_metrics(records)
    metrics["digital"] = e2e_digital({k: sorted(set(v)) for k, v in gt_raw.items()},
                                     {k: sorted(set(v)) for k, v in pred_raw.items()})
    gate_ok, gate_failures = nerq.digital_gate_pass(metrics)
    metrics["digital_gate"] = {"pass": gate_ok, "failures": gate_failures}
    metrics["loose"] = loose_span_metrics(records)
    return metrics


def merge_file_digital(per_file: list[dict]) -> dict:
    merged: dict[str, dict] = {}
    for fm in per_file:
        for etype, s in fm["digital"].items():
            bucket = merged.setdefault(
                etype, {"total_gt": 0, "exact": 0, "near_miss": 0, "miss": 0,
                        "near_miss_detail": [], "miss_detail": []})
            for key in ("total_gt", "exact", "near_miss", "miss"):
                bucket[key] += s[key]
            bucket["near_miss_detail"] += s["near_miss_detail"]
            bucket["miss_detail"] += s["miss_detail"]
    for s in merged.values():
        s["exact_rate"] = s["exact"] / s["total_gt"] if s["total_gt"] else 1.0
        s["near_miss_detail"] = list(dict.fromkeys(s["near_miss_detail"]))[:20]
        s["miss_detail"] = list(dict.fromkeys(s["miss_detail"]))[:20]
    return merged


# ---------------- 逐文件执行（镜像 run_e2e_file） ----------------

def run_e2e_file(api: E2eApi, spec: dict, warmup_pages: int = 1) -> dict:
    nerq = load_backend_nerq()
    file_path = Path(spec["path"])
    gt_pages = spec.get("gt_pages") or [{"page": i, "entities": {}}
                                        for i in range(max(spec.get("pages", 1), 1))]
    perf_only = not spec.get("gt_pages")

    if spec["carrier"] == "encrypted_pdf":
        started = time.perf_counter()
        import httpx as _httpx

        try:
            file_id = api.upload(file_path)
        except _httpx.HTTPStatusError as exc:  # 只认明确 4xx/5xx 拒绝（评审 I3：
            # 网络超时/连接错误不得误判为"正确拒绝"——宽度与旧 run_eval 对齐）
            detail = exc.response
            return {"file": {"id": spec["id"], "carrier": spec["carrier"],
                             "doc_type": spec.get("doc_type"), "density": spec.get("density"),
                             "gt_entities": 0},
                    "robustness": {"outcome": "rejected",
                                   "detail": f"HTTP {detail.status_code}（{detail.text[:80]}）",
                                   "wall_s": round(time.perf_counter() - started, 3)},
                    "perf": None}
        try:
            pages = iter_pages_with_timing(api, file_id, max(len(gt_pages), 1))
        finally:
            api.delete_file(file_id)
        return {"file": {"id": spec["id"], "carrier": spec["carrier"],
                         "doc_type": spec.get("doc_type"), "density": spec.get("density"),
                         "gt_entities": 0},
                "robustness": {"outcome": "accepted_should_reject",
                               "detail": "加密卷被正常受理（未拒绝）",
                               "wall_s": round(time.perf_counter() - started, 3)},
                "perf": {"pages_total": len(pages), "warmup_pages": 0, "steady_pages": len(pages),
                         "wall_s": {"total": round(sum(p["wall_s"] for p in pages), 3)},
                         "throughput_pages_per_min": None, "duration_ms": {},
                         "pages_detail": pages}}

    file_id = api.upload(file_path)
    doc_level = spec["carrier"] in api.DOC_LEVEL_CARRIERS
    try:
        if doc_level:
            pred_entities, wall = api.parse_and_hybrid_ner(file_id)
            pages = [{"page": 1, "warmup": False, "wall_s": wall, "duration_ms": {},
                      "pipeline_status": {}, "entities": pred_entities}]
        else:
            pages = iter_pages_with_timing(api, file_id, len(gt_pages),
                                           warmup_pages=warmup_pages)
    finally:
        api.delete_file(file_id)

    steady = [p for p in pages if not p["warmup"]]
    walls = [p["wall_s"] for p in steady]
    empty_pages = sum(1 for p in steady if not any(p["entities"].values()))

    if perf_only:
        return {"file": {"id": spec["id"], "carrier": spec["carrier"],
                         "doc_type": spec.get("doc_type"), "density": spec.get("density"),
                         "gt_entities": 0},
                "perf": {"pages_total": len(pages), "warmup_pages": warmup_pages,
                         "steady_pages": len(steady),
                         "wall_s": {"total": round(sum(p["wall_s"] for p in pages), 3),
                                    "mean": round(sum(walls) / len(walls), 3) if walls else 0,
                                    "p50": round(percentile(walls, 50), 3),
                                    "p95": round(percentile(walls, 95), 3)},
                         "throughput_pages_per_min": round(len(steady) * 60 / sum(walls), 3) if walls else None,
                         "duration_ms": {}, "pages_detail": pages},
                "robustness": {"outcome": "ok",
                               "detail": f"{len(steady)} steady 页，空框页 {empty_pages}",
                               "wall_s": round(sum(p["wall_s"] for p in pages), 3),
                               "empty_pages": empty_pages}}

    records, gt_raw_all, pred_raw_all = [], {}, {}
    for i, gt_page in enumerate(gt_pages):
        pred_page = pages[i]["entities"] if i < len(pages) else {}
        for etype, values in gt_page["entities"].items():
            gt_raw_all.setdefault(etype, []).extend(values)
        for etype, values in pred_page.items():
            pred_raw_all.setdefault(etype, []).extend(values)
        if doc_level:
            continue
        records.append({"page_id": i, "gt": _squash_entities(gt_page["entities"]),
                        "pred": _squash_entities(pred_page),
                        "latency_sec": pages[i]["wall_s"]})
    if doc_level:
        records.append({"page_id": 0, "gt": _squash_entities(gt_raw_all),
                        "pred": _squash_entities(pred_raw_all),
                        "latency_sec": sum(p["wall_s"] for p in pages)})

    metrics = e2e_core_metrics(records, gt_raw_all, pred_raw_all)
    metrics["records"] = records
    metrics["_gt_raw"] = gt_raw_all
    metrics["_pred_raw"] = pred_raw_all

    duration_agg: dict[str, dict] = {}
    for key in sorted({k for p in steady for k in p["duration_ms"]}):
        vals = [p["duration_ms"][key] for p in steady
                if isinstance(p["duration_ms"].get(key), (int, float))]
        if vals:
            duration_agg[key] = {"mean": round(sum(vals) / len(vals), 1),
                                 "p95": round(percentile(vals, 95), 1)}
    metrics["perf"] = {
        "pages_total": len(pages), "warmup_pages": warmup_pages,
        "steady_pages": len(steady),
        "wall_s": {"total": round(sum(p["wall_s"] for p in pages), 3),
                   "mean": round(sum(walls) / len(walls), 3) if walls else 0,
                   "p50": round(percentile(walls, 50), 3), "p95": round(percentile(walls, 95), 3)},
        "throughput_pages_per_min": round(len(steady) * 60 / sum(walls), 3) if walls else None,
        "duration_ms": duration_agg,
        "pages_detail": pages,
    }
    metrics["file"] = {"id": spec["id"], "carrier": spec["carrier"],
                       "doc_type": spec.get("doc_type"), "density": spec.get("density"),
                       "gt_entities": sum(len(vs) for p in gt_pages
                                          for vs in p["entities"].values())}
    return metrics


def run_e2e_level(api: E2eApi, specs: list[dict], warmup_pages: int = 1) -> dict:
    """镜像 run_e2e_level：失败隔离 + 总体聚合（页级 records + 逐文件 digital 求和）。"""
    import sys

    per_file, failed = [], []
    for spec in specs:
        started = time.perf_counter()
        try:
            metrics = run_e2e_file(api, spec, warmup_pages=warmup_pages)
        except Exception as exc:  # noqa: BLE001 — 隔离记录后继续
            failed.append({"id": spec["id"], "error": f"{type(exc).__name__}: {exc}"})
            print(f"  {spec['id']}: ❌ {type(exc).__name__}: {exc}", file=sys.stderr)
            continue
        metrics["file"]["eval_wall_s"] = round(time.perf_counter() - started, 3)
        per_file.append(metrics)
        if "overall" in metrics:
            m = metrics["overall"]
            print(f"  {spec['id']}: P={m['precision']:.3f} R={m['recall']:.3f} "
                  f"F1={m['f1']:.3f} gate={'✅' if metrics['digital_gate']['pass'] else '❌'} "
                  f"wall={metrics['perf']['wall_s']['total']}s")
        else:
            r = metrics.get("robustness") or {}
            print(f"  {spec['id']}: [{r.get('outcome', '?')}] {r.get('detail', '')[:60]}")

    if not per_file:
        raise RuntimeError("全部文件失败，无结果可报告")

    quality_files = [fm for fm in per_file if "overall" in fm]
    overall: dict = {}
    if quality_files:
        all_records = []
        for fm in quality_files:
            for rec in fm["records"]:
                all_records.append({"page_id": f"{fm['file']['id']}#{rec['page_id']}",
                                    "gt": rec["gt"], "pred": rec["pred"],
                                    "latency_sec": rec["latency_sec"]})
        overall = e2e_core_metrics(all_records, {}, {})
        overall["digital"] = merge_file_digital(quality_files)
        gate_ok, gate_failures = load_backend_nerq().digital_gate_pass(overall)
        overall["digital_gate"] = {"pass": gate_ok, "failures": gate_failures}
    return {"per_file": per_file, "failed": failed, "overall": overall,
            "rejected": [fm["file"]["id"] for fm in per_file
                         if fm.get("robustness", {}).get("outcome") == "rejected"]}


def strip_heavy(result: dict) -> dict:
    """落盘前剔除逐页大对象（同旧 run_eval 默认裁剪）。"""
    for fm in result.get("per_file", []):
        fm.pop("records", None)
        fm.pop("_gt_raw", None)
        fm.pop("_pred_raw", None)
        if isinstance(fm.get("perf"), dict):
            fm["perf"].pop("pages_detail", None)
    return result
