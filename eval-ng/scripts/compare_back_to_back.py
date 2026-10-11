"""背靠背汇总：旧 benchmark_t2 JSON × 新 eval-ng JSON → P/R/F1 对照 md。

N=3 聚合（均值）后逐桶对照；不一致项醒目标出（AC1：逐桶一致才有资格接替）。
"""

from __future__ import annotations

import argparse
import glob
import json
import statistics
from pathlib import Path


def _load(paths: list[str]) -> list[dict]:
    docs = []
    for pat in paths:
        for p in sorted(glob.glob(pat)):
            docs.append(json.loads(Path(p).read_text(encoding="utf-8")))
    if not docs:
        raise SystemExit("未匹配到任何结果 JSON")
    return docs


def _new_engine_name(doc: dict) -> str:
    return doc.get("participant", "?")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--old", nargs="+", required=True)
    ap.add_argument("--new", nargs="+", required=True)
    ap.add_argument("--exam", required=True)
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args()

    old_docs = _load(args.old)
    new_docs = _load(args.new)

    # 旧：doc["buckets"][exam][engine] = metrics；多 run 取均值
    old_runs = []
    for doc in old_docs:
        m = doc.get("buckets", {}).get(args.exam, {})
        for engine, metrics in m.items():
            if isinstance(metrics, dict):
                old_runs.append((engine, metrics))
    # 新：doc["aggregated"]["last_run_full"]["overall"]（单 run 报告，多文件聚合）
    new_runs = []
    for doc in new_docs:
        m = doc.get("aggregated", {}).get("last_run_full", {})
        if m.get("overall"):
            new_runs.append((_new_engine_name(doc), m))

    def agg(runs, key):
        vals = [m["overall"][key] for _, m in runs]
        return statistics.fmean(vals) if vals else float("nan")

    lines = [
        f"# 背靠背对照：考卷 {args.exam}（N={len(old_runs)} 旧 / {len(new_runs)} 新）",
        "",
        "| 线 | P | R | F1 |",
        "|---|---|---|---|",
        f"| 旧 benchmark_t2 | {agg(old_runs, 'precision'):.4f} | {agg(old_runs, 'recall'):.4f} | {agg(old_runs, 'f1'):.4f} |",
        f"| 新 eval-ng | {agg(new_runs, 'precision'):.4f} | {agg(new_runs, 'recall'):.4f} | {agg(new_runs, 'f1'):.4f} |",
        "",
    ]
    verdict = "一致" if old_runs and new_runs and abs(agg(old_runs, "f1") - agg(new_runs, "f1")) < 1e-9 else "不一致"
    lines.append(f"判定：**{verdict}**（F1 均值差 < 1e-9 视为逐桶一致）")
    args.out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
