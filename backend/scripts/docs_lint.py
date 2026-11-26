#!/usr/bin/env python3
"""文档知识库 lint（Issue#367/#368）。

用法（在 backend 目录下）：

    python -m scripts.docs_lint
    python -m scripts.docs_lint --root ../some-docs

检查：frontmatter、slug、事实键唯一、断链、摘要页规则、跨文档重复句子、索引可达性。
以 `ERROR:` 开头的问题返回非 0（CI 判红），`WARN:` 仅提示。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app.services import docs_service


def main() -> int:
    parser = argparse.ArgumentParser(description="文档知识库 lint")
    parser.add_argument("--root", default=None, help="文档根目录（默认取 settings.DOCS_ROOT）")
    args = parser.parse_args()

    problems = docs_service.lint(Path(args.root) if args.root else None)
    errors = [problem for problem in problems if problem.startswith("ERROR:")]
    for problem in problems:
        print(problem)
    print(
        f"docs lint: {len(errors)} error(s), {len(problems) - len(errors)} warning(s)"
    )
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
