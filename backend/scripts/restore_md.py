#!/usr/bin/env python
"""还原 CLI(Issue#70/#50 T7):脱敏文本 + 映射表 → 还原原文。

用法:
  python restore_md.py in.md --mapping mapping.json > out.md
  python restore_md.py in.md --mapping mapping.json --policy first --report report.json

与 API(/api/v1/vlmd/restore)走同一 restore_service,输出一致(测试保证)。
律师可脱离网页单独使用;报告 JSON 含 restored/ambiguous/unknown 三清单(安全信号)。
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.restore_service import normalize_mapping, restore  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="脱敏文本还原(占位符/化名 → 原文)")
    parser.add_argument("input", nargs="?", default=None, help="脱敏文本文件(.md/.txt);省略则读 stdin")
    parser.add_argument("--mapping", required=True, help="映射表 JSON(#66 产物)")
    parser.add_argument("--policy", choices=["safe", "first"], default="safe",
                        help="一对多策略:safe=保留占位符+候选(默认);first=取首条")
    parser.add_argument("--report", help="还原报告输出路径(JSON);缺省不写")
    args = parser.parse_args()

    text = (
        Path(args.input).read_text(encoding="utf-8")
        if args.input
        else sys.stdin.read()
    )
    raw = json.loads(Path(args.mapping).read_text(encoding="utf-8"))
    mapping = normalize_mapping(raw)
    result = restore(text, mapping, policy=args.policy)

    sys.stdout.write(result.restored_text)
    if args.report:
        report = {
            "restored_count": result.restored_count,
            "ambiguous": result.ambiguous,
            "unknown": result.unknown,
            "hits": result.hits,
            "parse_warnings": mapping.parse_warnings,
        }
        Path(args.report).write_text(
            json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8"
        )
    if result.unknown:
        print(
            f"\n[restore] 警告:{len(result.unknown)} 处未知占位符/化名(可能是幻觉或映射缺失):"
            f"{result.unknown[:5]}",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
