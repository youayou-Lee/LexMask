#!/usr/bin/env python3
"""check_report_redaction.py —— speed 报告脱敏扫描器（验收方案 v2 D5④）。

扫三类痕迹，命中即退 1：

1. **非回环 IP**（`127.*` / `0.0.0.0` 放行——报告里的本地回环不是泄漏）；
2. **绝对路径**（POSIX 根用户/家目录/挂载点等前缀、Windows 盘符路径；`--allow-prefix` 可放行指定前缀）；
3. **`--denylist` 词表**（一行一词；`re:<正则>` 前缀走正则）。

自带阳性对照：`--self-test` 会用含 IP/绝对路径/denylist 词的样本断言「必须命中」，再用干净样本断言
「必须不命中」——**没有阳性对照的扫描器等于恒真判据**（D5④）。

用法::

    python eval-ng/scripts/check_report_redaction.py <report.json|report.md> [...] \
        [--allow-prefix /path/to/private-repo] [--denylist denylist.txt] [--self-test]
    echo ... | python eval-ng/scripts/check_report_redaction.py - --denylist words.txt

exit：0=干净；1=命中（或有阳性对照失败）；2=用法错误。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

IP_RE = re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b")
ABS_PATH_RE = re.compile(
    r"(?<![\w.\-/])(/(?:root|home|mnt|data|opt|srv|var|Users|tmp|workspace|media)/[^\s\"'`,)]*)"
)
WIN_PATH_RE = re.compile(r"(?<![\w])[A-Za-z]:\\[^\s\"'`,)]*")
LOOPBACK_PREFIXES = ("127.", "0.0.0.0", "::1")

SELF_TEST_BAD = (
    "instance 10.11.12.13 path /root/ab/d3-after-1.json win C:\\Users\\dev\\secret.txt "  # public-tree-scan: ignore-line
    "case 苏A-某某公司-2021"  # public-tree-scan: ignore-line
)
SELF_TEST_CLEAN = "instance local path testdata/canary-issue106-scan-10p.pdf ip 127.0.0.1 ok"


def _is_loopback(ip: str) -> bool:
    return any(ip.startswith(prefix) for prefix in LOOPBACK_PREFIXES)


def load_denylist(path: str | None) -> list[str]:
    if not path:
        return []
    return [line.strip() for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def scan(text: str, *, denylist: list[str], allow_prefixes: list[str]) -> list[tuple[str, str]]:
    """返回 [(规则, 命中片段), ...]。"""
    findings: list[tuple[str, str]] = []
    for match in IP_RE.finditer(text):
        if not _is_loopback(match.group(0)):
            findings.append(("ip", match.group(0)))
    for rule, regex in (("abs_path", ABS_PATH_RE), ("win_path", WIN_PATH_RE)):
        for match in regex.finditer(text):
            value = match.group(0)
            if any(value.startswith(prefix) for prefix in allow_prefixes):
                continue
            findings.append((rule, value))
    for token in denylist:
        if token.startswith("re:"):
            pattern = token[3:]
            for match in re.finditer(pattern, text):
                findings.append(("denylist_re", match.group(0)))
        elif token and token in text:
            findings.append(("denylist", token))
    return findings


def scan_source(name: str, text: str, *, denylist: list[str], allow_prefixes: list[str]) -> list[str]:
    lines: list[str] = []
    findings = scan(text, denylist=denylist, allow_prefixes=allow_prefixes)
    for rule, value in findings:
        lines.append(f"{name}: [{rule}] {value}")
    return lines


def self_test() -> list[str]:
    """阳性对照：坏样本必须命中三类；干净样本必须零命中。"""
    errors: list[str] = []
    bad = scan(SELF_TEST_BAD, denylist=["苏A-某某公司-2021"], allow_prefixes=[])
    rules = {rule for rule, _ in bad}
    for expected in ("ip", "abs_path", "win_path", "denylist"):
        if expected not in rules:
            errors.append(f"阳性对照失败：坏样本未命中 {expected}（实得 {sorted(rules)}）")
    clean = scan(SELF_TEST_CLEAN, denylist=[], allow_prefixes=[])
    if clean:
        errors.append(f"阳性对照失败：干净样本误报 {clean}")
    return errors


def _parse_args(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="check_report_redaction", description="speed 报告脱敏扫描器")
    ap.add_argument("paths", nargs="*", help="报告文件（.json/.md）；`-` 读 stdin")
    ap.add_argument("--denylist", default=None, help="禁词表（一行一词；re: 前缀走正则）")
    ap.add_argument("--allow-prefix", action="append", default=[], help="放行的绝对路径前缀（可重复）")
    ap.add_argument("--self-test", action="store_true", help="先跑阳性对照，再扫文件")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    denylist = load_denylist(args.denylist)

    exit_code = 0
    if args.self_test:
        errors = self_test()
        if errors:
            for err in errors:
                print(f"[self-test] {err}", file=sys.stderr)
            exit_code = 1
        else:
            print("[self-test] 阳性对照通过（坏样本命中 4 类；干净样本零命中）")

    if not args.paths:
        if not args.self_test:
            print("用法错误：至少给一个报告路径，或用 --self-test", file=sys.stderr)
            return 2
        return exit_code

    hits: list[str] = []
    for name in args.paths:
        if name == "-":
            hits += scan_source("<stdin>", sys.stdin.read(), denylist=denylist,
                                allow_prefixes=args.allow_prefix)
            continue
        path = Path(name)
        if not path.is_file():
            print(f"用法错误：{name} 不是文件", file=sys.stderr)
            return 2
        text = path.read_text(encoding="utf-8", errors="replace")
        hits += scan_source(str(path), text, denylist=denylist, allow_prefixes=args.allow_prefix)

    if hits:
        for line in hits:
            print(line)
        print(f"[redaction-scan] 命中 {len(hits)} 处痕迹（exit 1）", file=sys.stderr)
        return 1
    print(f"[redaction-scan] clean（扫描 {len(args.paths)} 个文件）")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
