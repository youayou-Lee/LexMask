"""T0 探测原始输出 → manifest.private.json 构建与校验工具。

T0 agent 的探测原始输出（perf-work/issue92-t0/）兼容两种输入形态，本工具
从其构建 T3 manifest（schema 见 spec.validate_manifest）：

1. LA eval 风格 JSON（locate_anything_eval.py 产物，manifest.json 或逐页
   <page>.json）：{"source", "page_index", "width", "height",
   "tasks": {task: {"boxes": [...]}}} —— 由 tasks 内 seal/signature 任务的
   命中数推 has_seal / has_signature；
2. 通用逐页探测 JSON：{"page_id"/"volume"/"page", "la_boxes",
   "mineru_seal_blocks", ...}（字段缺省视为 0）。

桶标签（provisional，最终人工定档在 T1）：默认规则——
  has_seal 且 has_signature  → ["seal_hard", "sig_hard"]（压字页先归难桶）
  仅 has_seal                → ["seal_clean"]
  仅 has_signature           → ["sig_hard"]（签名默认进难桶，宁可多收）
可用 --bucket-seal-hard / --bucket-sig-hard 覆盖打标集合（页号逗号列表）。

不负责跑全量（T0 agent 并行产出数据）；提供 --validate 只校验现有 manifest。

用法：
  python3 eval/benchmarks/t3/build_sample.py \
    --probe-dir /path/to/perf-work/issue92-t0 \
    --out testdata/benchmarks/t3/manifest.private.json
  python3 eval/benchmarks/t3/build_sample.py --validate <manifest.json>
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from eval.benchmarks.t3 import spec

_PAGE_IN_NAME = re.compile(r"#p(?P<page>\d+)$")


def _page_id_from_probe_name(stem: str, volume_hint: str = "") -> tuple[str, int]:
    """文件名 '卷名#p3' → page_id='卷名#p3', page=3；无页号则 page=1。"""
    m = _PAGE_IN_NAME.search(stem)
    if m:
        return stem, int(m.group("page"))
    return f"{stem}#p1", 1


def _probe_entry_from_la_record(record: dict, probe_stem: str = "") -> dict | None:
    """LA eval 产物 → 通用探测条目；非页记录返回 None。

    page_id 优先取探测文件名 stem（'卷名#pN'），缺失时退回 source stem。"""
    if "tasks" not in record:
        return None
    la_boxes = 0
    has_seal = False
    has_signature = False
    for task, payload in (record.get("tasks") or {}).items():
        count = len((payload or {}).get("boxes") or [])
        if task == "seal":
            has_seal = count > 0
            la_boxes += count
        elif task == "signature":
            has_signature = count > 0
            la_boxes += count
    source = str(record.get("source") or "")
    stem = probe_stem or Path(source).stem if source else Path(str(record.get("json_path") or "")).stem
    page_id, page = _page_id_from_probe_name(stem)
    page = int(record.get("page_index") or page)
    return {
        "page_id": page_id,
        "volume": source,
        "page": page,
        "buckets": [],
        "has_seal": has_seal,
        "has_signature": has_signature,
        "probe": {"la_boxes": la_boxes, "mineru_seal_blocks": int(record.get("mineru_seal_blocks") or 0)},
    }


def _probe_entry_generic(record: dict) -> dict:
    """通用探测条目（含 page_id/volume/page 直接给全的形态）。"""
    page_id = str(record.get("page_id") or "")
    if not page_id:
        page_id, page = _page_id_from_probe_name(str(record.get("stem") or ""))
    else:
        m = _PAGE_IN_NAME.search(page_id)
        page = int(m.group("page")) if m else int(record.get("page") or 1)
    probe = record.get("probe") or {}
    return {
        "page_id": page_id,
        "volume": str(record.get("volume") or ""),
        "page": int(record.get("page") or page or 1),
        "buckets": [],
        "has_seal": bool(record.get("has_seal", False)),
        "has_signature": bool(record.get("has_signature", False)),
        "probe": {
            "la_boxes": int(probe.get("la_boxes") or record.get("la_boxes") or 0),
            "mineru_seal_blocks": int(probe.get("mineru_seal_blocks") or record.get("mineru_seal_blocks") or 0),
        },
    }


def load_probe_records(probe_dir: str | Path) -> list[dict]:
    """扫描探测目录的 *.json，兼容两种形态，归一为 manifest 条目。"""
    entries: list[dict] = []
    seen: set[str] = set()
    for path in sorted(Path(probe_dir).rglob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue  # 非法/半成品探测文件跳过
        records: list[dict]
        if isinstance(payload, list):
            records = [r for r in payload if isinstance(r, dict)]
        elif isinstance(payload, dict):
            records = [payload]
        else:
            continue
        for record in records:
            entry = _probe_entry_from_la_record(record, path.stem) or (
                _probe_entry_generic(record) if ("page_id" in record or "stem" in record) else None
            )
            if entry is None:
                continue
            if entry["page_id"] in seen:
                continue
            seen.add(entry["page_id"])
            entries.append(entry)
    return entries


def assign_buckets(entries: list[dict]) -> list[dict]:
    """provisional 桶标签（T1 人工定档前口径，见模块 docstring）。"""
    for entry in entries:
        buckets: list[str] = []
        if entry["has_seal"] and entry["has_signature"]:
            buckets = ["seal_hard", "sig_hard"]
        elif entry["has_seal"]:
            buckets = ["seal_clean"]
        elif entry["has_signature"]:
            buckets = ["sig_hard"]
        entry["buckets"] = buckets
    return entries


def build_manifest(probe_dir: str | Path) -> list[dict]:
    return assign_buckets(load_probe_records(probe_dir))


def bucket_distribution(manifest: list[dict]) -> dict[str, int]:
    dist: dict[str, int] = {}
    for entry in manifest:
        for bucket in entry.get("buckets") or []:
            dist[bucket] = dist.get(bucket, 0) + 1
        if not entry.get("buckets"):
            dist["(unbucketed)"] = dist.get("(unbucketed)", 0) + 1
    return dist


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe-dir", help="T0 探测原始输出目录（perf-work/issue92-t0/）")
    parser.add_argument("--out", help="manifest 输出路径（testdata/benchmarks/t3/manifest.private.json）")
    parser.add_argument("--validate", dest="validate_path", help="只校验给定 manifest 后退出")
    args = parser.parse_args(argv)

    if args.validate_path:
        manifest = json.loads(Path(args.validate_path).read_text(encoding="utf-8"))
        errors = spec.validate_manifest(manifest)
        if errors:
            print("FAIL:\n  " + "\n  ".join(errors), file=sys.stderr)
            return 1
        print(f"OK: {len(manifest)} 页, 桶分布 {bucket_distribution(manifest)}")
        return 0

    if not args.probe_dir or not args.out:
        parser.error("需要 --probe-dir 与 --out（或 --validate）")
    manifest = build_manifest(args.probe_dir)
    errors = spec.validate_manifest(manifest)
    if errors:
        print("构建结果未过 schema 校验:\n  " + "\n  ".join(errors), file=sys.stderr)
        return 1
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[ok] {len(manifest)} 页 → {out}；桶分布 {bucket_distribution(manifest)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
