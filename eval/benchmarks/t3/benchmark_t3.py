"""T3 四臂跑批：manifest × 页图 → 四臂逐页调用 → metrics.json + 明细 jsonl。

用法（冒烟）：
  PYTHONPATH=$PWD python3 eval/benchmarks/t3/benchmark_t3.py \
    --manifest testdata/benchmarks/t3/manifest.private.json \
    --pages-dir testdata/benchmarks/t3/pages \
    --arms la,sigdet --limit 3 --out-dir eval/reports/t3-<ts>

输出：
  metrics.json — 每臂 {pages, ok_pages, failed_pages, total_elapsed_s,
  avg_page_elapsed_s, boxes, failed_ratio, arm_invalid}；
  details.jsonl — 统一契约行（见 spec.py docstring）+ status/elapsed_ms/error。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from eval.benchmarks.t3 import spec
from eval.benchmarks.t3.adapters import ARMS, run_arm

PAGE_IMAGE_EXTS = (".png", ".jpg", ".jpeg")


def find_page_image(pages_dir: str | Path, page_id: str) -> Path | None:
    """manifest page_id → 页图路径（<pages_dir>/<page_id>.<ext>）。"""
    base = Path(pages_dir) / page_id
    for ext in PAGE_IMAGE_EXTS:
        candidate = base.with_suffix(ext)
        if candidate.is_file():
            return candidate
    return None


def load_manifest(path: str | Path) -> list[dict]:
    manifest = json.loads(Path(path).read_text(encoding="utf-8"))
    errors = spec.validate_manifest(manifest)
    if errors:
        raise SystemExit("manifest 校验失败:\n  " + "\n  ".join(errors))
    return manifest


def run_benchmark(
    manifest: list[dict],
    pages_dir: str | Path,
    arms: list[str],
    limit: int | None = None,
) -> tuple[dict, list[dict]]:
    """跑批核心（与 CLI 解耦，供测试注入）。返回 (metrics, details)。"""
    pages = manifest[:limit] if limit else manifest
    details: list[dict] = []
    for arm in arms:
        if arm not in ARMS:
            raise SystemExit(f"未知臂 {arm!r}（允许 {ARMS}）")
        for page in pages:
            page_id = page["page_id"]
            image = find_page_image(pages_dir, page_id)
            record = {
                "page_id": page_id,
                "arm": arm,
                "kind": "",
                "box": {"x": 0, "y": 0, "w": 0, "h": 0},
                "confidence": 1.0,
                "raw": "",
                "status": "failed",
                "elapsed_ms": 0,
                "error": "",
            }
            if image is None:
                record["error"] = f"页图缺失: {page_id}"
                details.append(record)
                continue
            start = time.perf_counter()
            try:
                preds = run_arm(arm, str(image))
            except Exception as exc:  # noqa: BLE001 —— 单页失败不中断整臂，页记 failed
                record["error"] = f"{type(exc).__name__}: {exc}"
                record["elapsed_ms"] = round((time.perf_counter() - start) * 1000, 1)
                details.append(record)
                print(f"[failed] {arm} {page_id}: {record['error'][:160]}", file=sys.stderr, flush=True)
                continue
            elapsed_ms = round((time.perf_counter() - start) * 1000, 1)
            for i, pred in enumerate(preds):
                details.append(
                    {
                        "page_id": page_id,
                        "arm": arm,
                        "kind": pred["kind"],
                        "box": pred["box"],
                        "confidence": pred["confidence"],
                        "raw": pred["raw"],
                        "status": "ok",
                        "elapsed_ms": elapsed_ms if i == 0 else 0,
                        "error": "",
                    }
                )
            if not preds:
                record["status"] = "ok"
                details.append(record)

    metrics: dict[str, dict] = {}
    for arm in arms:
        rows = [d for d in details if d["arm"] == arm]
        ok_rows = [d for d in rows if d["status"] == "ok"]
        # 页粒度耗时 = 该页首条 ok 记录的 elapsed_ms
        page_times = [d["elapsed_ms"] for d in ok_rows if d["elapsed_ms"] > 0]
        pages_seen = {d["page_id"] for d in rows}
        failed_pages = {d["page_id"] for d in rows if d["status"] == "failed"}
        total_s = sum(page_times) / 1000
        metrics[arm] = {
            "pages": len(pages_seen),
            "ok_pages": len(pages_seen - failed_pages),
            "failed_pages": len(failed_pages),
            "failed_ratio": round(len(failed_pages) / len(pages_seen), 4) if pages_seen else None,
            "arm_invalid": spec.is_arm_invalid(len(failed_pages), len(pages_seen)),
            "total_elapsed_s": round(total_s, 3),
            "avg_page_elapsed_s": round(total_s / len(page_times), 3) if page_times else None,
            "boxes": len([d for d in ok_rows if d["kind"]]),
            "by_kind": {
                kind: len([d for d in ok_rows if d["kind"] == kind]) for kind in spec.KINDS
            },
        }
    return metrics, details


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--pages-dir", required=True)
    parser.add_argument("--arms", default=",".join(ARMS), help=f"逗号分隔，默认全四臂 {ARMS}")
    parser.add_argument("--limit", type=int, default=None, help="冒烟：只跑前 N 页")
    parser.add_argument("--out-dir", default=None, help="默认 eval/reports/t3-<UTC时间戳>")
    args = parser.parse_args(argv)

    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    manifest = load_manifest(args.manifest)

    out_dir = Path(args.out_dir) if args.out_dir else (
        Path(__file__).resolve().parents[2] / "reports" / f"t3-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    started = datetime.now(timezone.utc).isoformat()
    metrics, details = run_benchmark(manifest, args.pages_dir, arms, args.limit)
    metrics_path = out_dir / "metrics.json"
    details_path = out_dir / "details.jsonl"
    metrics_path.write_text(
        json.dumps(
            {"generated_at": started, "manifest": str(args.manifest), "pages_dir": str(args.pages_dir), "arms": arms, "metrics": metrics},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    with details_path.open("w", encoding="utf-8") as fh:
        for row in details:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    print(f"[ok] metrics → {metrics_path}")
    print(f"[ok] details → {details_path} ({len(details)} 行)")
    for arm, m in metrics.items():
        print(
            f"  {arm}: pages={m['pages']} failed={m['failed_pages']}"
            f" invalid={m['arm_invalid']} boxes={m['boxes']} avg={m['avg_page_elapsed_s']}s"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
