"""旧 run_eval manifest（levels 含 e2e）→ eval-ng e2e 考卷 JSON。

同一 manifest 单一事实源（AC2 前提）；GT json 就地展开为 gt_pages（新考卷形态）。
用法：python3 eval-ng/scripts/make_exam_from_manifest.py --repo-root <evalng-worktree> \
        --suite synthetic --out <out>/exam_e2e.json [--only id1,id2]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo-root", required=True, type=Path)
    ap.add_argument("--suite", default="synthetic")
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--only", default=None, help="逗号分隔条目 id")
    args = ap.parse_args()

    ds_dir = args.repo_root / "eval" / "datasets"
    files: list[dict] = []
    for mpath in (ds_dir / "manifest.json", ds_dir / "manifest.private.json"):
        if mpath.exists():
            files += [f for f in json.loads(mpath.read_text(encoding="utf-8"))["files"]
                      if "e2e" in f["levels"]]
    if args.suite != "all":
        files = [f for f in files if f["source"] == args.suite]
    if args.only:
        only = [x.strip() for x in args.only.split(",")]
        missing = set(only) - {f["id"] for f in files}
        if missing:
            raise SystemExit(f"--only 引用不存在的条目: {sorted(missing)}")
        files = [f for f in files if f["id"] in only]
    if not files:
        raise SystemExit(f"manifest 无 suite={args.suite} 的 e2e 条目")

    items = []
    for f in files:
        path = Path(f["path"])
        if not path.is_absolute():
            path = ds_dir / path
        gt_pages = None
        if f.get("gt"):
            gtp = ds_dir / f["gt"]
            gt_pages = json.loads(gtp.read_text(encoding="utf-8"))["pages"]
        items.append({
            "id": f["id"], "path": str(path), "carrier": f["carrier"],
            "doc_type": f.get("doc_type"), "density": f.get("density"),
            "pages": f.get("pages", 1), "gt_pages": gt_pages or [],
        })
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({"exam_id": f"e2e-{args.suite}", "items": items},
                                   ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"e2e 考卷 {args.suite}: {len(items)} 文件 → {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
