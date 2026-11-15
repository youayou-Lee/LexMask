#!/usr/bin/env python3
"""Issue#279 A3.1：JSONL 全量 PII 探针扫描（零命中 = exit 0）。

正控自检（--self-test）：已知 PII 夹具必须命中——自检不过则扫描结论作废、exit 1
（沿 #159 G4 scanner --self-test 先例：零命中断言必须先证探测器活着）。
探针正则表与 backend/app/services/page_event_log.py 同源（PII_PROBE_RES）。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.page_event_log import probe_hits  # noqa: E402

_SELF_TEST_FIXTURES: tuple[tuple[str, str], ...] = (
    ("id_card", "11010519491231002X"),
    ("mobile", "13812345678"),
    ("bank_card", "6222020200112345678"),
    ("long_cjk", "这是一段超过二十个汉字的连续案卷正文内容用于探针正控自检"),
)


def _schema_self_test() -> bool:
    """Issue#276：page_done 白名单双向钉——visual_source 落盘 / visual_types 拒写。

    白名单铁律（#279）：未声明字段 = 拒写**整事件** + dropped_unknown_field 计数
    （不是静默丢字段）；走真实写入口 log_event（临时目录落盘），不用内部校验函数。
    """
    import tempfile

    from app.services import page_event_log

    ok = True
    page_event_log.reset_for_tests()
    with tempfile.TemporaryDirectory() as tmp:
        page_event_log.set_log_root(tmp)
        page_event_log.set_event_context("self-test-job", "self-test-item", "self-test-file")
        page_event_log.set_current_page(1)
        if not page_event_log.log_event("page_done", visual_source="la"):
            print(
                "self-test MISS: page_done+visual_source rejected "
                "(EVENT_SCHEMAS whitelist not extended?)",
                file=sys.stderr,
            )
            ok = False
        before = page_event_log.stats()["dropped_unknown_field"]
        if page_event_log.log_event("page_done", visual_types=["official_seal"]):
            print(
                "self-test FALSE-POSITIVE: page_done+visual_types accepted "
                "(list field must not enter JSONL)",
                file=sys.stderr,
            )
            ok = False
        elif page_event_log.stats()["dropped_unknown_field"] != before + 1:
            print("self-test MISS: visual_types rejection not counted", file=sys.stderr)
            ok = False
    page_event_log.reset_for_tests()
    return ok


def self_test() -> bool:
    ok = True
    for expected, fixture in _SELF_TEST_FIXTURES:
        hits = probe_hits(fixture)
        if expected not in hits:
            print(f"self-test MISS: {expected} fixture not detected (hits={hits})", file=sys.stderr)
            ok = False
    # 负控：干净短文本不命中
    if probe_hits("本页完成 用时12ms"):
        print("self-test FALSE-POSITIVE: clean text flagged", file=sys.stderr)
        ok = False
    if not _schema_self_test():
        ok = False
    if ok:
        print("self-test passed: all fixtures detected, clean text clean, page_done schema双向钉通过")
    return ok


def scan_dir(job_dir: Path) -> list[tuple[str, int, str]]:
    findings: list[tuple[str, int, str]] = []
    for path in sorted(job_dir.glob("*.jsonl")):
        with path.open(encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, 1):
                hits = probe_hits(line)
                for hit in hits:
                    findings.append((str(path), lineno, hit))
    return findings


def main() -> int:
    args = [a for a in sys.argv[1:] if a != "--self-test"]
    if "--self-test" in sys.argv[1:]:
        if not self_test():
            return 1
        if not args:
            return 0
    if len(args) != 1:
        print("usage: audit_page_event_log.py [--self-test] <DATA_DIR/logs/<job_id>>", file=sys.stderr)
        return 2
    job_dir = Path(args[0])
    if not job_dir.is_dir():
        print(f"not a directory: {job_dir}", file=sys.stderr)
        return 2
    findings = scan_dir(job_dir)
    if findings:
        for path, lineno, hit in findings:
            print(f"HIT {hit} {path}:{lineno}")
        print(f"total hits: {len(findings)}")
        return 1
    print("clean: 0 PII hits across all JSONL files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
