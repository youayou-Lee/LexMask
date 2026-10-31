"""MinerU sidecar — 任务式 REST 包装器（Issue#217 PR③ 收编入仓）。

人话：**sidecar（边车）= 伴生小服务**。MinerU 的依赖（torch/vLLM 一整套）与
backend 的 paddle 冲突，本来就不能塞进同一进程——所以让 MinerU 在旁边的
进程/容器里跑，本脚本把「提交解析 → 查进度 → 取结果 zip」包成几个简单
HTTP 接口，backend 隔着网络调用即可。好处：依赖隔离、可独立启停（与
LA/OCR 同为可选服务）、位置可换（本地容器或云端隧道，接口不变）。

契约（与 ``backend/app/services/mineru_parse_client.py`` 逐条对齐，该客户端
是契约的权威定义）：

- ``GET  /health``                → 200（存活探测；backend 预检/轮询起手）
- ``POST /tasks``（multipart，字段名 ``files``，可多文件）
                                  → 202 ``{"task_id": ...}``
- ``GET  /tasks/{task_id}``       → 200 ``{"status": pending|processing|
                                    completed|failed, "progress": p,
                                    "error": str|None}``
- ``GET  /tasks/{task_id}/result``→ 200 ``application/zip``（内含解析产物）

MinerU 调用形态：``mineru -p <input> -o <outdir>``（4.x CLI），产物目录内
``*_content_list*.json`` / ``*.md`` 全量打包装 zip。任务表在内存 + 落盘
（重启后 completed 任务仍可取 zip）。

部署：容器内 16581 端口（与既有实例侧部署对齐）；配置一律环境变量
（``MINERU_SIDECAR_*``），不内嵌任何地址口令。
"""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import threading
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse

APP_PORT = int(os.environ.get("MINERU_SIDECAR_PORT", "16581"))
WORK_DIR = Path(os.environ.get("MINERU_SIDECAR_WORKDIR", "/tmp/mineru-sidecar"))
MINERU_BIN = os.environ.get("MINERU_SIDECAR_BIN", "mineru")
MINERU_TIMEOUT_SEC = float(os.environ.get("MINERU_SIDECAR_TIMEOUT", "7200"))

WORK_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="MinerU sidecar", version="1.0")

_tasks: dict[str, dict[str, Any]] = {}
# 后台解析协程的强引用集（asyncio.create_task 的结果若无人持有会被 GC——
# 表现为任务永远停在 pending。CPython 文档明确要求自行保存引用。）
_bg_tasks: set[asyncio.Task] = set()


def _task_dir(task_id: str) -> Path:
    return WORK_DIR / task_id


def _task_zip(task_id: str) -> Path:
    return WORK_DIR / f"{task_id}.zip"


def _write_task_meta(task_id: str) -> None:
    import json

    (_task_dir(task_id) / "meta.json").write_text(
        json.dumps(_tasks[task_id], ensure_ascii=False), encoding="utf-8"
    )


def _load_task_meta(task_id: str) -> None:
    """重启恢复：meta.json 里是 completed/failed 的任务状态，zip 仍在盘上。"""
    meta = _task_dir(task_id) / "meta.json"
    if meta.exists():
        import json

        try:
            _tasks[task_id] = json.loads(meta.read_text(encoding="utf-8"))
        except Exception:
            pass


def _run_mineru_sync(task_id: str, inputs: list[Path]) -> None:
    """同步执行 mineru CLI（在线程池跑——asyncio 子进程在 uvicorn 非主线程
    的事件循环里收不到退出回调，线程形态任何部署都稳）。"""
    task = _tasks[task_id]
    out_dir = _task_dir(task_id) / "out"
    task["status"] = "processing"
    task["progress"] = 5
    _write_task_meta(task_id)
    try:
        t0 = time.monotonic()
        proc = subprocess.run(
            [MINERU_BIN, "-p", *[str(p) for p in inputs], "-o", str(out_dir)],
            capture_output=True,
            timeout=MINERU_TIMEOUT_SEC,
        )
        # 进度按时间粗估（CLI 无结构化进度输出）；完成时置 100。
        task["progress"] = min(
            90, 5 + int((time.monotonic() - t0) / max(MINERU_TIMEOUT_SEC, 1) * 85)
        )
        if proc.returncode != 0:
            tail = (proc.stdout or proc.stderr or b"")[-500:].decode(errors="replace")
            raise RuntimeError(f"mineru exit={proc.returncode}: {tail}")
        files = sorted(p for p in out_dir.rglob("*") if p.is_file())
        if not files:
            raise RuntimeError("mineru produced no output files")
        with zipfile.ZipFile(_task_zip(task_id), "w", zipfile.ZIP_DEFLATED) as zf:
            for f in files:
                zf.write(f, f.relative_to(out_dir).as_posix())
        task["progress"] = 100
        task["status"] = "completed"
        _write_task_meta(task_id)
    except Exception as exc:  # noqa: BLE001 —— 任务级失败要落 meta 供轮询侧读取
        task["status"] = "failed"
        task["error"] = str(exc)[:500]
        _write_task_meta(task_id)


async def _run_mineru(task_id: str, inputs: list[Path]) -> None:
    await asyncio.to_thread(_run_mineru_sync, task_id, inputs)


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "online", "mineru_bin": MINERU_BIN}


@app.post("/tasks", status_code=202)
async def create_task(files: list[UploadFile] = File(...)) -> dict[str, Any]:
    if not files:
        raise HTTPException(status_code=422, detail="no files uploaded")
    task_id = uuid.uuid4().hex
    tdir = _task_dir(task_id)
    tdir.mkdir(parents=True, exist_ok=True)
    inputs: list[Path] = []
    for f in files:
        dest = tdir / "in" / Path(f.filename or "input").name
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("wb") as fh:
            shutil.copyfileobj(f.file, fh)
        inputs.append(dest)
    _tasks[task_id] = {
        "status": "pending",
        "progress": 0,
        "error": None,
        "files": [p.name for p in inputs],
    }
    _write_task_meta(task_id)
    t = asyncio.create_task(_run_mineru(task_id, inputs))
    _bg_tasks.add(t)
    t.add_done_callback(_bg_tasks.discard)
    return {"task_id": task_id}


@app.get("/tasks/{task_id}")
async def task_status(task_id: str) -> dict[str, Any]:
    task = _tasks.get(task_id)
    if task is None:
        _load_task_meta(task_id)
        task = _tasks.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    return {
        "task_id": task_id,
        "status": task["status"],
        "progress": task.get("progress"),
        "error": task.get("error"),
    }


@app.get("/tasks/{task_id}/result")
async def task_result(task_id: str) -> FileResponse:
    task = _tasks.get(task_id)
    if task is None:
        _load_task_meta(task_id)
        task = _tasks.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    if task["status"] != "completed":
        raise HTTPException(status_code=409, detail=f"task not completed: {task['status']}")
    zip_path = _task_zip(task_id)
    if not zip_path.exists():
        raise HTTPException(status_code=404, detail="result zip missing")
    return FileResponse(
        zip_path, media_type="application/zip", filename=f"{task_id}.zip"
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=APP_PORT)
