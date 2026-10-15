"""T2 桶 jsonl → eval-ng 考卷 JSON（类型派生规则与旧 benchmark_t2 完全一致）。

支持多桶：--bucket "hardcase,cluener-person"（逗号分隔），每题携带所属桶（exam_id），
CLI 按桶分组出分。桶说明（内容/全量抽样）见 t2_meta.BUCKET_META，随考卷写入报告。

data-dir 解析顺序：--data-dir > 配置文件 eval-ng/datasets/buckets.local.json 的 data_dir
（本机配一次即可，不入库）> 报错提示如何配置。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from datasets.t2_meta import BUCKET_META  # noqa: E402

CONFIG_PATH = Path(__file__).resolve().parent.parent / "datasets" / "buckets.local.json"


def resolve_data_dir(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    if CONFIG_PATH.exists():
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        if cfg.get("data_dir"):
            return Path(cfg["data_dir"])
    raise SystemExit(
        f"未指定 --data-dir 且无本机配置。请在 {CONFIG_PATH} 写入：\n"
        '  {"data_dir": "/root/private_data/benchmarks/t2/buckets"}\n'
        "（本机配一次即可；该文件不入库）"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=None,
                    help="桶 jsonl 根目录；缺省读 buckets.local.json 配置")
    ap.add_argument("--bucket", required=True,
                    help="桶名，逗号分隔可多个，如 hardcase,cluener-person")
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--limit", type=int, default=None,
                    help="每桶最多取多少条（调试用，默认全取）")
    args = ap.parse_args()

    data_dir = resolve_data_dir(args.data_dir)
    buckets = [b.strip() for b in args.bucket.split(",") if b.strip()]
    seen_ids: set[str] = set()
    items, bucket_stats = [], {}

    for bucket in buckets:
        entries = []
        for jsonl in sorted(data_dir.rglob("*.jsonl")):
            for line in jsonl.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                e = json.loads(line)
                if e.get("bucket") != bucket or e["id"] in seen_ids:
                    continue
                seen_ids.add(e["id"])
                entries.append(e)
                if args.limit and len(entries) >= args.limit:
                    break
            if args.limit and len(entries) >= args.limit:
                break
        if not entries:
            print(f"[warn] 桶 {bucket!r} 在 {data_dir} 下无条目，跳过", file=sys.stderr)
            continue
        for e in entries:
            items.append({
                "id": e["id"], "exam_id": bucket,  # exam_id=桶名 → CLI 按桶分组出分
                "form": "entity_table",
                "text": e["text"],
                "types": sorted(e["entities"]),
                "gt": e["entities"],
            })
        meta = BUCKET_META.get(bucket, {"content": "（未登记说明）", "sampling": "—"})
        bucket_stats[bucket] = {**meta, "count": len(entries)}

    if not items:
        raise SystemExit("没有任何桶产出条目")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps({"exam_id": "t2-multi" if len(bucket_stats) > 1 else next(iter(bucket_stats)),
                    "buckets": bucket_stats, "items": items},
                   ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    print(f"考卷：{len(bucket_stats)} 桶 {len(items)} 题 → {args.out}")
    for b, meta in bucket_stats.items():
        print(f"  {b}：{meta['count']} 题（{meta['sampling']}）｜{meta['content']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
