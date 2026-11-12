"""A3.1：探针脚本正控自检——已知 PII 夹具必须命中；自检不过=扫描报告作废。"""
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "audit_page_event_log.py"


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True)


def test_self_test_passes_and_detects_fixtures():
    r = _run("--self-test")
    assert r.returncode == 0, r.stderr
    assert "self-test" in r.stdout.lower()


def test_scan_clean_dir_exits_zero(tmp_path):
    (tmp_path / "j1").mkdir()
    (tmp_path / "j1" / "i1.jsonl").write_text(
        '{"ts":"t","job_id":"j1","item_id":"i1","page":1,"event":"page_done","ms":1}\n',
        encoding="utf-8",
    )
    assert _run(str(tmp_path / "j1")).returncode == 0


def test_scan_hit_exits_one(tmp_path):
    d = tmp_path / "j1"
    d.mkdir()
    (d / "i1.jsonl").write_text(
        '{"ts":"t","job_id":"j1","item_id":"i1","page":1,"event":"x","note":"联系 13812345678"}\n',
        encoding="utf-8",
    )
    r = _run(str(d))
    assert r.returncode == 1 and "mobile" in r.stdout
