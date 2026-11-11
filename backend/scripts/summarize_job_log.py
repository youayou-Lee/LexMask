#!/usr/bin/env python3
"""A2.5/AC6 一条命令复算 summary：python scripts/summarize_job_log.py <logs>/<job_id>。

stdout 输出与 summary.json 逐字节一致（same json.dumps 口径）——三源 diff=0 的复算端。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # backend/

from app.services.page_event_summary import compute_summary  # noqa: E402


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: summarize_job_log.py <DATA_DIR/logs/<job_id>>", file=sys.stderr)
        return 2
    job_dir = Path(sys.argv[1])
    if not job_dir.is_dir():
        print(f"not a directory: {job_dir}", file=sys.stderr)
        return 2
    print(json.dumps(compute_summary(job_dir), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
