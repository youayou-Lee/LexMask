#!/usr/bin/env python3
"""check_public_tree.py —— 公开树红线扫描器（验收方案 v2 G4）。

与 `check_report_redaction.py` 同族，但**扫的是公开仓本身**（含新增文档与 CHANGELOG），
用途是给「敏感物不出私有体系」（AGENTS.md 铁律①）一个**有失败入口**的机检：

1. **公网 IP**：回环 / RFC1918 私网 / RFC5737 文档段放行（测试夹具与示例常用），
   其余一律命中——真实实例地址另经 `--denylist` 点名；
2. **绝对路径**：POSIX 根用户/家目录/挂载点等前缀、Windows 盘符路径（`--allow-prefix` 放行指定前缀）；
3. **凭据形态**：`Bearer <token>`、`sk-…`/`ghp_…`/`AKIA…`，以及 `password|api_key|secret|token`
   后跟**像真口令的**字面量（≥10 字符且同时含数字与非字母数字；`<你的密码>`/`self._password`
   /`os.environ[…]` 这类占位或引用不算）；
4. **`--denylist` 词表**（一行一词，`re:` 前缀走正则）——真实案卷名、实例域名、口令进这里。

行内放过：含 `public-tree-scan: ignore-line` 的行跳过（扫描器自己的阳性对照夹具需要它）。

自带阳性对照：`--self-test` 用含上述五类痕迹的样本断言「必须全部命中」，再用干净样本断言
「必须零命中」——**没有阳性对照的扫描器等于恒真判据**。

用法::

    python eval-ng/scripts/check_public_tree.py                      # 默认扫 --base 以来改动过的文件
    python eval-ng/scripts/check_public_tree.py --paths docs/ README.md
    python eval-ng/scripts/check_public_tree.py --denylist <私有词表> --allow-prefix <公开工具链前缀>
    python eval-ng/scripts/check_public_tree.py --self-test

exit：0=干净；1=命中（或阳性对照失败）；2=用法错误。
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

IP_RE = re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b")
ABS_PATH_RE = re.compile(
    r"(?<![\w.\-/])(/(?:root|home|mnt|data|opt|srv|var|Users|tmp|workspace|media)/[^\s\"'`,)]*)"
)
WIN_PATH_RE = re.compile(r"(?<![\w])[A-Za-z]:\\[^\s\"'`,)]*")
# 凭据类键名：`--api-pass` / `api_pass=` / `password=` / `secret:` …（R1：必须覆盖 flag 形态）
CREDENTIAL_NAME_RE = r"(?:api[_-]?(?:pass(?:word)?|key)|passw(?:or)?d|secret|token)s?"
# 模板/占位值不算命中：`<你的密码>`、`${ENV}`、`changeme`、`your-password` …
PLACEHOLDER_VALUE_RE = re.compile(
    r"(?i)(?:[<>{}]|\$\{?\w+"
    r"|^(?:\W*(?:your|my|the)?[-_ ]*(?:password|passwd|secret|token|key))\W*$"
    r"|^(?:test|fake|sample|dummy|placeholder|example|changeme|redacted)[-_ ]?\w*$"
    r"|^(?:os\.)?environ\b|[\w.]+\(|[\w.]+\["
    r"|^[A-Z][A-Z0-9_]{2,}$)"
)
SECRET_RES = (
    ("bearer", re.compile(r"\bBearer\s+[A-Za-z0-9\-._~+/]{24,}")),
    # 必须带厂商分隔符，否则 `skeletonButtonSize` / `skippedNoCandidates` 这类标识符会误报
    ("api_key", re.compile(r"\b(?:sk|rk|ghp|gho|ghs|github_pat)-[A-Za-z0-9_-]{16,}\b|AKIA[0-9A-Z]{16}")),
    # 键名 + 字面量（`password="…"`、`api_pass: "…"`）
    ("credential_literal", re.compile(rf"(?i)\b{CREDENTIAL_NAME_RE}\b\s*[:=]\s*[\"'](?P<val>[^\"']{{8,}})[\"']")),
    # flag 形态：本仓 CLI 的「凭据开关 + 引号字面量」与 argparse 的 default= 写法
    ("credential_flag", re.compile(rf"(?i)--{CREDENTIAL_NAME_RE}\b[^)\n]{{0,40}}?[\"'](?P<val>[^\"'=\s,]{{8,}})[\"']")),
)
CREDENTIAL_RULES = frozenset({"credential_literal", "credential_flag"})
LOOPBACK_PREFIXES = ("127.", "0.0.0.0", "::1")
# 私网（RFC1918）与文档段（RFC5737）+ 保留段：示例与夹具的合法取值，不是实例地址
PRIVATE_IP_RE = re.compile(
    r"^(?:10\.|192\.168\.|172\.(?:1[6-9]|2\d|3[01])\.|192\.0\.2\.|198\.51\.100\.|203\.0\.113\.)"
)
IGNORE_MARKER = "public-tree-scan: ignore-line"
SKIP_SUFFIXES = frozenset({
    ".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".zip", ".gz", ".whl", ".bin",
    ".so", ".dylib", ".dll", ".pt", ".pth", ".onnx", ".safetensors", ".docx", ".xlsx", ".pptx",
    ".woff", ".woff2", ".ttf", ".otf", ".mp4", ".mov", ".db", ".sqlite", ".lock",
})
MAX_BYTES = 2 * 1024 * 1024

SELF_TEST_BAD = (
    "instance 8.8.8.8 path /srv/evidence/arm.json win C:\\Users\\dev\\secret.txt "  # public-tree-scan: ignore-line
    "auth Bearer abcdefghijklmnopqrstuvwxyz012345 password=\"Passw0rd!2026\" deny SCNET-INSTANCE-1 "  # public-tree-scan: ignore-line
    # R1 形态：flag 与 add_argument(default=) 两种写法都必须落网（值用合成串，不是真实口令）
    "cli --api-pass \"Synthetic-Pass!123\" "  # public-tree-scan: ignore-line
    "parser.add_argument(\"--api-pass\", default=\"Synthetic-Pass!123\")"  # public-tree-scan: ignore-line
)
SELF_TEST_CLEAN = (
    "api_base http://127.0.0.1:8000 fixture 203.0.113.9 host 10.1.2.3 "
    "password=<你的密码> api_key=os.environ['X'] path testdata/canary-issue106-scan-10p.pdf"
)


def _is_public_ip(ip: str) -> bool:
    if any(ip.startswith(prefix) for prefix in LOOPBACK_PREFIXES):
        return False
    return not PRIVATE_IP_RE.match(ip)


def load_denylist(path: str | None) -> list[str]:
    if not path:
        return []
    return [line.strip() for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def scan(text: str, *, denylist: list[str], allow_prefixes: list[str]) -> list[tuple[str, str]]:
    """返回 [(规则, 命中片段), ...]。"""
    findings: list[tuple[str, str]] = []
    for match in IP_RE.finditer(text):
        if _is_public_ip(match.group(0)):
            findings.append(("public_ip", match.group(0)))
    for rule, regex in (("abs_path", ABS_PATH_RE), ("win_path", WIN_PATH_RE)):
        for match in regex.finditer(text):
            value = match.group(0)
            if any(value.startswith(prefix) for prefix in allow_prefixes):
                continue
            findings.append((rule, value))
    for rule, regex in SECRET_RES:
        for match in regex.finditer(text):
            if rule in CREDENTIAL_RULES and PLACEHOLDER_VALUE_RE.search(match.group("val")):
                continue  # 模板/占位值（`<你的密码>`、`${ENV}` …）不是凭据
            findings.append((rule, match.group(0)))
    for token in denylist:
        if token.startswith("re:"):
            for match in re.finditer(token[3:], text):
                findings.append(("denylist_re", match.group(0)))
        elif token and token in text:
            findings.append(("denylist", token))
    return findings


def scan_source(name: str, text: str, *, denylist: list[str], allow_prefixes: list[str]) -> list[str]:
    """逐行扫描并带行号；含放过标记的行跳过。"""
    lines: list[str] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if IGNORE_MARKER in line:
            continue
        for rule, value in scan(line, denylist=denylist, allow_prefixes=allow_prefixes):
            lines.append(f"{name}:{lineno}: [{rule}] {value}")
    return lines


def _git_lines(repo_root: Path, args: list[str]) -> list[str]:
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_root), *args], check=True, capture_output=True, text=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return []
    return [line for line in out.split("\0" if "-z" in args else "\n") if line.strip()]


def default_paths(repo_root: Path, base: str | None) -> list[Path]:
    """默认扫「本次改动新增/修改的文件」——G4 的判决面是本次交付物，不是整棵历史树。

    `--base` 给定时取三者并集：`<base>...HEAD` 的已提交改动、**工作区未提交改动**（含未跟踪文件）。
    这样门禁在提交前就能跑——否则扫描器会在最需要它的时刻漏掉改动。
    给空串则退化为整树 `git ls-files`（只适合人工巡检，历史文件有既有噪声）。
    """
    if not base:
        return [repo_root / rel for rel in _git_lines(repo_root, ["ls-files", "-z"])]
    rels = _git_lines(repo_root, ["diff", "--name-only", "--diff-filter=AM", f"{base}...HEAD"])
    rels += _git_lines(repo_root, ["diff", "--name-only", "--diff-filter=AM", "HEAD"])
    rels += _git_lines(repo_root, ["ls-files", "--others", "--exclude-standard"])
    return [repo_root / rel for rel in dict.fromkeys(rels)]


def _iter_targets(repo_root: Path, paths: list[str], base: str | None) -> list[Path]:
    if paths:
        candidates: list[Path] = []
        for raw in paths:
            target = Path(raw)
            if not target.is_absolute():
                target = repo_root / raw
            if target.is_dir():
                candidates.extend(sorted(p for p in target.rglob("*") if p.is_file()))
            else:
                candidates.append(target)
        return candidates
    return default_paths(repo_root, base)


def self_test(denylist: list[str] | None = None) -> int:
    """阳性对照：坏样本必须四类全中，干净样本必须零命中。"""
    words = list(denylist or [])
    bad = scan_source("self-test-bad", SELF_TEST_BAD, denylist=words + ["SCNET-INSTANCE-1"],
                      allow_prefixes=[])
    rules = {line.split("[", 1)[1].split("]", 1)[0] for line in bad}
    required = {"public_ip", "abs_path", "win_path", "bearer", "credential_literal", "credential_flag", "denylist"}
    missing = required - rules
    clean = scan_source("self-test-clean", SELF_TEST_CLEAN, denylist=words, allow_prefixes=[])
    problems: list[str] = []
    if missing:
        problems.append(f"阳性对照未命中：{sorted(missing)}")
    if clean:
        problems.append(f"干净样本被误报：{clean}")
    if problems:
        for line in problems:
            print(f"[self-test] {line}", file=sys.stderr)
        return 1
    print(f"[self-test] 阳性对照 {len(required)} 类全中，干净样本零命中")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="公开树红线扫描器（G4）")
    parser.add_argument("--repo-root", default=".", help="公开仓根（默认当前目录）")
    parser.add_argument("--paths", nargs="*", default=None, help="只扫这些路径（默认扫 --base 以来改动过的文件）")
    parser.add_argument(
        "--base", default="origin/preview",
        help="改动基线（默认 origin/preview，即扫本次 PR 新增/修改的文件）；给空串则退化为整树 git ls-files",
    )
    parser.add_argument("--denylist", default=None, help="词表文件（一行一词；re: 前缀走正则）")
    parser.add_argument("--allow-prefix", action="append", default=[], help="放行的路径前缀（可重复）")
    parser.add_argument("--self-test", action="store_true", help="只跑阳性对照")
    args = parser.parse_args(argv)

    denylist = load_denylist(args.denylist)
    if args.self_test:
        return self_test(denylist)

    repo_root = Path(args.repo_root).resolve()
    if not repo_root.is_dir():
        print(f"仓根不存在：{repo_root}", file=sys.stderr)
        return 2
    targets = _iter_targets(repo_root, list(args.paths or []), args.base or None)
    if not targets:
        print("没有可扫的文件（git 不可用且未给 --paths）", file=sys.stderr)
        return 2

    findings: list[str] = []
    scanned = 0
    for path in targets:
        if path.suffix.lower() in SKIP_SUFFIXES or not path.is_file():
            continue
        try:
            if path.stat().st_size > MAX_BYTES:
                continue
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        scanned += 1
        try:
            rel = path.relative_to(repo_root)
        except ValueError:
            rel = path
        findings.extend(scan_source(str(rel), text, denylist=denylist, allow_prefixes=args.allow_prefix))

    if findings:
        for line in findings:
            print(line)
        print(f"[public-tree] {scanned} 文件，命中 {len(findings)} 处（exit 1）", file=sys.stderr)
        return 1
    print(f"[public-tree] {scanned} 文件，零命中")
    return 0


if __name__ == "__main__":
    sys.exit(main())
