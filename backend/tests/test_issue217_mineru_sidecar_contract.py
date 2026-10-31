"""Issue#217 PR③：MinerU sidecar 契约单测。

sidecar 按 ``backend/app/services/mineru_parse_client.py`` 已钉死的契约实现
（该客户端是契约权威）：POST /tasks(multipart files)→202 {task_id}；
GET /tasks/{id}→status ∈ pending|processing|completed|failed；
GET /tasks/{id}/result→application/zip；GET /health→200。

MinerU CLI 用 fake 脚本替身（产出最小合法产物集），全流程不过真模型——
契约形状与状态机是本文件的判据，真解析由本地/云端端到端验收（A5）覆盖。
"""
from __future__ import annotations

import io
import json
import os
import stat
import zipfile
from pathlib import Path

import pytest
import httpx


@pytest.fixture()
def client(tmp_path: Path, monkeypatch):
    """sidecar 客户端 + fake mineru CLI（读取输入、写出 content_list/md）。"""
    workdir = tmp_path / "sidecar-work"
    fake_bin = tmp_path / "fake-mineru"
    fake_bin.write_text(
        "#!/bin/sh\n"
        "# fake mineru 4.x: parse --json --tier T -o <md-file> PATH\n"
        'out=""\n'
        'prev=""\n'
        "for a in \"$@\"; do\n"
        '  if [ "$prev" = "-o" ]; then out="$a"; fi\n'
        '  prev="$a"\n'
        "done\n"
        'printf "# fake\\n" > "$out"\n'
        'printf \'[{"type":"text","text":"fake"}]\' > "$out.content_list.json"\n'
        "",
        encoding="utf-8",
    )
    os.chmod(fake_bin, os.stat(fake_bin).st_mode | stat.S_IEXEC)

    monkeypatch.setenv("MINERU_SIDECAR_WORKDIR", str(workdir))
    monkeypatch.setenv("MINERU_SIDECAR_BIN", str(fake_bin))
    monkeypatch.setenv("MINERU_SIDECAR_PORT", "16581")

    import importlib

    import scripts.mineru_sidecar as sidecar

    importlib.reload(sidecar)
    # 真 uvicorn 后台线程（生产同形态）：create_task 的解析协程随 loop 存活，
    # TestClient 的 portal 语义会冻结后台任务——弃用。
    import threading

    import uvicorn

    config = uvicorn.Config(sidecar.app, host="127.0.0.1", port=18237, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    import time as _t

    for _ in range(50):
        try:
            import httpx

            if httpx.get("http://127.0.0.1:18237/health", timeout=2).status_code == 200:
                break
        except Exception:
            _t.sleep(0.2)
    client = _RealClient("http://127.0.0.1:18237")
    yield client
    server.should_exit = True
    thread.join(timeout=5)


def test_health_is_200_online(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "online"


def test_full_task_lifecycle_contract(client):
    """202 → processing/pending → completed → zip 内含产物。"""
    r = client.post(
        "/tasks",
        files={"files": ("sample.pdf", b"%PDF-fake", "application/pdf")},
    )
    assert r.status_code == 202, r.text
    task_id = r.json()["task_id"]
    assert task_id

    # 轮询直至 completed（fake CLI 秒级）
    status = None
    for _ in range(50):
        s = client.get(f"/tasks/{task_id}")
        assert s.status_code == 200
        status = s.json()
        if status["status"] == "completed":
            break
        assert status["status"] in ("pending", "processing"), status
    assert status and status["status"] == "completed", status

    zr = client.get(f"/tasks/{task_id}/result")
    assert zr.status_code == 200
    assert zr.headers["content-type"].startswith("application/zip")
    with zipfile.ZipFile(io.BytesIO(zr.content)) as zf:
        names = set(zf.namelist())
    assert any(n.endswith(".content_list.json") for n in names), names
    assert any(n.endswith(".md") for n in names), names


def test_unknown_task_404_and_incomplete_409(client):
    assert client.get("/tasks/nonexistent").status_code == 404
    assert client.get("/tasks/nonexistent/result").status_code == 404
    # 未完成任务取结果 → 409（不是静默空 zip）。fake CLI 秒级完成，真任务抢
    # 不在「processing 窗口」⇒ 状态机用内存条目直接钉（实现语义=读 _tasks）。
    from scripts import mineru_sidecar as sidecar

    sidecar._tasks["fake-task"] = {"status": "processing", "progress": 10, "error": None}
    assert client.get("/tasks/fake-task").json()["status"] == "processing"
    assert client.get("/tasks/fake-task/result").status_code == 409


def test_failed_task_carries_error(client, tmp_path: Path, monkeypatch):
    """CLI 非零退出 ⇒ failed + error 可读（轮询侧能拿到原因）。"""
    bad_bin = tmp_path / "bad-mineru"
    bad_bin.write_text("#!/bin/sh\necho 'model exploded' >&2\nexit 3\n", encoding="utf-8")
    os.chmod(bad_bin, os.stat(bad_bin).st_mode | stat.S_IEXEC)
    # 直接 patch 模块全局（_run_mineru_sync 运行时读取，无需 reload 重启服务）
    from scripts import mineru_sidecar as sidecar

    monkeypatch.setattr(sidecar, "MINERU_BIN", str(bad_bin))
    r = client.post("/tasks", files={"files": ("a.pdf", b"%PDF", "application/pdf")})
    task_id = r.json()["task_id"]
    status = None
    for _ in range(50):
        status = client.get(f"/tasks/{task_id}").json()
        if status["status"] == "failed":
            break
    assert status and status["status"] == "failed"
    assert "exit=3" in (status.get("error") or ""), status


class _RealClient:
    """httpx 包装：client.post/get（files/json 传参），契约测试专用。"""

    def __init__(self, base: str) -> None:
        self._base = base

    def post(self, path: str, **kw):
        return httpx.post(self._base + path, timeout=30, **kw)

    def get(self, path: str, **kw):
        return httpx.get(self._base + path, timeout=30, **kw)
