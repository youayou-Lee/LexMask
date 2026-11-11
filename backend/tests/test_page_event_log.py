"""Issue#279 页级事件写入器单测。文件句柄与目录全部落 tmp_path，不碰真实 DATA_DIR。"""
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app.core.config import settings
from app.services import page_event_log as pel


@pytest.fixture()
def log_env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "PAGE_EVENT_LOG_ENABLED", True)
    monkeypatch.setattr(settings, "PAGE_EVENT_LOG_DIR", str(tmp_path / "logs"))
    pel.reset_for_tests()  # 清 fd 缓存/计数器
    yield tmp_path / "logs"
    pel.reset_for_tests()


def _read_lines(log_dir, job_id, item_id):
    path = log_dir / job_id / f"{item_id}.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_whitelist_rejects_unknown_event_and_field(log_env):
    with pel.event_context("job1", "item1"):
        assert pel.log_event("page_done", page=1, ms=12) is True
        assert pel.log_event("not_an_event", ms=1) is False
        assert pel.log_event("page_done", page=1, evil_free_text="正文") is False
    lines = _read_lines(log_env, "job1", "item1")
    assert len(lines) == 1 and lines[0]["event"] == "page_done"
    s = pel.stats()
    assert s["dropped_unknown_event"] == 1 and s["dropped_unknown_field"] == 1


def test_failopen_readonly_dir_never_raises(log_env):
    d = log_env / "jobro"
    d.mkdir(parents=True)
    (d / "itemro.jsonl").write_text("", encoding="utf-8")
    os.chmod(d, 0o500)  # 只读目录
    # 目录只读挡不住「打开既有可写文件」（POSIX 目录写权限只管 create/unlink），
    # 既有文件本身也须只读，open(O_WRONLY) 才会 EACCES（模拟日志文件被 root 化的实况）。
    os.chmod(d / "itemro.jsonl", 0o400)
    try:
        with pel.event_context("jobro", "itemro"):
            assert pel.log_event("page_done", page=1, ms=1) is False
    finally:
        os.chmod(d, 0o700)
    s = pel.stats()
    assert s["write_errors"] == 1 and s["written"] == 0


def test_failopen_unserializable_value_never_raises(log_env):
    with pel.event_context("jobu", "itemu"):
        assert pel.log_event("page_done", page=1, ms=object()) is False
        assert pel.log_event("page_done", page=1, ms=1) is True  # 恢复后续写
    assert len(_read_lines(log_env, "jobu", "itemu")) == 1
    assert pel.stats()["dropped_unserializable"] == 1


def test_concurrent_appends_no_interleave(log_env):
    N, T = 200, 8
    def worker(t):
        with pel.event_context("jobc", "itemc"):
            for i in range(N):
                pel.log_event("page_done", page=t * N + i, ms=i)
    with ThreadPoolExecutor(T) as ex:
        list(ex.map(worker, range(T)))
    path = log_env / "jobc" / "itemc.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == N * T
    assert all(isinstance(json.loads(l), dict) for l in lines)  # 每行合法 JSON=无交插


def test_two_processes_append_safety(log_env):
    """A1.3 多 worker 形态：两个真进程各写 50 条，逐行完整（O_APPEND 单次 write）。"""
    import subprocess, sys
    backend_root = str(Path(__file__).resolve().parents[1])
    env = {**os.environ, "PYTHONPATH": backend_root}
    script = (
        "from app.services import page_event_log as pel;"
        "pel.reset_for_tests();"
        f"pel.set_log_root({str(log_env)!r});"
        "pel.set_event_context('jobp','itemp');"
        "[pel.log_event('page_done', page=i, ms=i) for i in range(50)]"
    )
    subprocess.run([sys.executable, "-c", script], check=True, env=env)
    subprocess.run([sys.executable, "-c", script], check=True, env=env)
    lines = (log_env / "jobp" / "itemp.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 100
    assert all(isinstance(json.loads(l), dict) for l in lines)


def test_thresholds_snapshot_immune_to_config_change(log_env, monkeypatch):
    with pel.event_context("jobsnap", "itemsnap"):
        pel.log_event("route_decided", gate="verdict_unusable", source="docline_gate",
                      signals={"chars": 10}, verdict="fallback:verdict_unusable",
                      thresholds_snapshot=pel.route_thresholds_snapshot(
                          ("TEXT_LAYER_MIN_CHARS_PER_PAGE", "TEXT_LAYER_MAX_GARBAGE_RATIO")),
                      config_hash=pel.route_config_hash())
        snap1 = _read_lines(log_env, "jobsnap", "itemsnap")[0]["thresholds_snapshot"]
    monkeypatch.setattr(settings, "TEXT_LAYER_MIN_CHARS_PER_PAGE", 999)
    with pel.event_context("jobsnap", "itemsnap"):
        pel.log_event("route_decided", gate="verdict_unusable", source="docline_gate",
                      signals={"chars": 10}, verdict="fallback:verdict_unusable",
                      thresholds_snapshot=pel.route_thresholds_snapshot(
                          ("TEXT_LAYER_MIN_CHARS_PER_PAGE", "TEXT_LAYER_MAX_GARBAGE_RATIO")),
                      config_hash=pel.route_config_hash())
    lines = _read_lines(log_env, "jobsnap", "itemsnap")
    assert lines[0]["thresholds_snapshot"]["TEXT_LAYER_MIN_CHARS_PER_PAGE"] == snap1["TEXT_LAYER_MIN_CHARS_PER_PAGE"]
    assert lines[0]["thresholds_snapshot"]["TEXT_LAYER_MIN_CHARS_PER_PAGE"] == 120
    assert lines[1]["thresholds_snapshot"]["TEXT_LAYER_MIN_CHARS_PER_PAGE"] == 999
    assert lines[0]["config_hash"] != lines[1]["config_hash"]


def test_config_hash_ignores_unrelated_settings(log_env, monkeypatch):
    h1 = pel.route_config_hash()
    monkeypatch.setattr(settings, "TRASH_RETENTION_DAYS", 3)  # 非路由子集
    assert pel.route_config_hash() == h1


def test_sanitizer_redacts_pii(log_env):
    assert "13812345678" not in pel.sanitize_text("电话 13812345678 请查收")
    assert pel.probe_hits("身份证 11010519491231002X") != []
    assert pel.probe_hits("这是一段超过二十个汉字的连续正文内容绝对不许出现在日志里") != []
    assert pel.probe_hits("本页完成 用时12ms") == []  # 干净短文本零误报


def test_traceback_frames_contain_no_message_text():
    """A3.3 钉子：帧只含 代码位置，异常消息（可能嵌正文/PII）不得出现在任何帧里。"""
    try:
        raise ValueError("正文片段 13812345678")
    except ValueError as exc:
        frames = pel.traceback_frames(exc)
    assert frames and all("13812345678" not in f and "正文片段" not in f for f in frames)
    assert any("test_page_event_log" in f for f in frames)  # 形如 文件:行号 in 函数


def test_traceback_frames_unraised_exception_returns_empty():
    assert pel.traceback_frames(ValueError("无 trace")) == []


def test_no_context_drops_with_counter(log_env):
    assert pel.log_event("page_done", page=1, ms=1) is False
    assert pel.stats()["dropped_no_context"] == 1


def test_writer_contract_all_twelve_events(log_env):
    """A1.7：12 种事件名逐一过 schema 落盘（含 4 种预留——writer 契约可验）。"""
    events_fields = {
        "route_decided": dict(source="docline_gate", gate="no_text_layer", signals={}, verdict="fallback:no_text_layer"),
        "text_layer_extracted": dict(chars=1, blocks=1, pages=1, ms=1),
        "chunk_failed": dict(stage="docline", chunk_index=0, error_type="TypeError", traceback_frames=[], message_sanitized="x"),
        "ner_done": dict(ms=1, regions=1),
        "ocr_done": dict(ms=1, regions=1, source="ocr"),
        "page_done": dict(ms=1, stage_ms={}, regions=1),
        "page_failed": dict(stage="ner", error_type="E", message_sanitized="m", traceback_frames=[], attempts=1, ms=1),
        "retry_started": dict(attempt=2, trigger="manual"),
        "retry_done": dict(attempt=2, outcome="success", ms=1),
        "arbitration": dict(winner="ocr", loser="text_layer", metric="m", values={}),
        "stage_changed": dict(to="stage"),
        "accelerator_block_done": dict(block_index=0, ms=1),
        "accelerator_block_failed": dict(block_index=0, error_type="E"),
    }
    with pel.event_context("jobschema", "itemschema"):
        for name, fields in events_fields.items():
            assert pel.log_event(name, page=1, **fields) is True, name
    assert len(_read_lines(log_env, "jobschema", "itemschema")) == len(events_fields)


def test_bad_page_value_never_raises(log_env):
    """评审 r1 Finding 1：page 非法值绝不向识别链上抛——拒写整事件+计数（与白名单同哲学）。"""
    with pel.event_context("jobbad", "itembad"):
        assert pel.log_event("page_done", page="abc", ms=1) is False
        assert pel.log_event("page_done", page=True, ms=1) is False  # bool 显式排除
    assert pel.stats()["dropped_unserializable"] == 2
    assert _read_lines(log_env, "jobbad", "itembad") == []
    pel.set_current_page("x")  # 不得上抛；非法置 None
    with pel.event_context("jobbad", "itembad"):
        assert pel.log_event("page_done", ms=1) is True  # 上下文 page 已置 None → 正常落盘
    lines = _read_lines(log_env, "jobbad", "itembad")
    assert len(lines) == 1 and lines[0]["page"] is None


def test_safe_component_blocks_dot_traversal(log_env):
    """评审 r1 Finding 2：'.'/'..' 不逃逸日志根；delete_job_logs('..') 不删根外任何内容。"""
    assert pel._safe_component("..") == "_"
    assert pel._safe_component(".") == "_"
    assert pel.job_log_dir("..") == log_env / "_"
    log_env.mkdir(parents=True)  # fixture 的日志根是惰性创建的，哨兵文件须自备目录
    sentinel = log_env / "keep.txt"
    sentinel.write_text("x", encoding="utf-8")
    pel.delete_job_logs("..")  # 修前会 rmtree 逃逸到日志根之外；修后不动任何根外内容
    assert log_env.exists() and sentinel.exists()
