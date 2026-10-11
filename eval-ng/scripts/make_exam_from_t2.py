"""T2 桶 jsonl → eval-ng 考卷 JSON（类型派生规则与旧 benchmark_t2 完全一致）。

旧线：types = sorted(e["entities"])，gt = e["entities"]；
新考卷逐条保留同派生，保证背靠背对齐（back_to_back.sh 头部清单第 1 条）。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", required=True, type=Path)
    ap.add_argument("--bucket", required=True)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    items = []
    for jsonl in sorted(args.data_dir.rglob("*.jsonl")):
        for line in jsonl.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            e = json.loads(line)
            if e.get("bucket") != args.bucket:
                continue
            types = sorted(e["entities"])
            items.append({
                "id": e["id"],
                "form": "entity_table",
                "text": e["text"],
                "types": types,
                "gt": e["entities"],
            })
            if args.limit and len(items) >= args.limit:
                break
        if args.limit and len(items) >= args.limit:
            break
    if not items:
        raise SystemExit(f"桶 {args.bucket!r} 在 {args.data_dir} 下无条目")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps({"exam_id": args.bucket, "items": items}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    print(f"考卷 {args.bucket}: {len(items)} 题 → {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
