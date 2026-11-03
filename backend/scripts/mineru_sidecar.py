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
``*_content_list*.json`` / ``*.md`` 全量打包装 zip。任务表在内存 + meta.json
落盘（同容器内进程重启后 completed 任务仍可取 zip；WORKDIR 在 /tmp，不跨容器
重建——容器重建后任务历史清零，属 dev 形态预期）。

部署：容器内 16581 端口（与既有实例侧部署对齐）；配置一律环境变量
（``MINERU_SIDECAR_*``），不内嵌任何地址口令。
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse

APP_PORT = int(os.environ.get("MINERU_SIDECAR_PORT", "16581"))
WORK_DIR = Path(os.environ.get("MINERU_SIDECAR_WORKDIR", os.path.join(os.sep, "tmp", "mineru-sidecar")))
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


def _mineru_wait_sec() -> int:
    """--wait 上限（Issue#230 缺陷①）：CLI 缺省 60s，真卷 standard 10 页就要
    3.5 分钟 ⇒ 不传必超时。缺省跟随 MINERU_SIDECAR_TIMEOUT 留 120s 余量（子进程
    timeout 比服务端等待上限大，保证先收到 CLI 的结构化超时错误而非被 kill）。"""
    if os.environ.get("MINERU_SIDECAR_WAIT"):
        return max(60, int(float(os.environ["MINERU_SIDECAR_WAIT"])))
    return max(60, int(MINERU_TIMEOUT_SEC) - 120)


def _merge_docvortex_docs(docs: list[dict]) -> tuple[dict, list[str]]:
    """多个 docvortex.middle 窗口 doc 合并为单份（Issue#230 缺陷③）。

    MinerU 4.x 对 >64 页文档按窗口拆成多份 json（实测 149 页 → ``1-10`` +
    ``11-149``）。盲拼会把导出 md 全文翻倍，所以合并规则收紧：

    - 同 ``page_idx`` 且逐字节同内容 ⇒ 去重（doclib 历史缓存页段常态），计数告警；
    - 同 ``page_idx`` 内容不同 / ``schema_version`` 不一致 ⇒ raise（任务显式失败，
      不许猜）；
    - 合并键 = 页 canonical JSON，合并后按 page_idx 排序；metadata 取首份。
    """
    if not docs:
        raise ValueError("docvortex merge: 无文档可合并")
    if len(docs) == 1:
        return docs[0], []
    base = docs[0]
    merged_version = base.get("schema_version")
    pages_by_idx: dict[int, dict] = {}
    warns: list[str] = []
    deduped = 0
    for doc in docs:
        if doc.get("schema_version") != merged_version:
            raise ValueError(
                "docvortex merge: schema_version 不一致 "
                f"({merged_version} vs {doc.get('schema_version')})，拒绝盲拼"
            )
        for page in doc.get("pages") or []:
            idx = page.get("page_idx")
            key = int(idx) if isinstance(idx, (int, float)) else -1
            canon = json.dumps(page, sort_keys=True, ensure_ascii=False)
            if key in pages_by_idx:
                if json.dumps(pages_by_idx[key], sort_keys=True, ensure_ascii=False) == canon:
                    deduped += 1
                    continue
                raise ValueError(
                    f"docvortex merge: page_idx {idx} 在多个窗口内容不同，拒绝盲拼"
                )
            pages_by_idx[key] = page
    if deduped:
        warns.append(f"docvortex merge: 去重 {deduped} 个重复页")
    merged = dict(base)
    merged["pages"] = [pages_by_idx[k] for k in sorted(pages_by_idx)]
    return merged, warns


def _run_mineru_sync(task_id: str, inputs: list[Path]) -> None:
    """同步执行 mineru CLI（在线程池跑——asyncio 子进程在 uvicorn 非主线程
    的事件循环里收不到退出回调，线程形态任何部署都稳）。"""
    task = _tasks[task_id]
    out_dir = _task_dir(task_id) / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    task["status"] = "processing"
    task["progress"] = 5
    _write_task_meta(task_id)
    try:
        t0 = time.monotonic()
        # MinerU 4.x CLI：`mineru parse --json --tier <tier> -o <md> PATH`（逐文件）。
        # 3.x 的 `mineru -p` 在 4.x 已移除（实测 "No such option '-p'"）；4.x 走
        # 「本地 parse-server」架构（--tier standard 需 server managed 模式）。
        # stdout(--json) 含 parse.sha256/tier，据此从 doclib 收取结构化产物。
        exit_code = 0
        output_tail = b""
        parse_results: list[dict] = []
        for input_path in inputs:
            proc = subprocess.run(
                [
                    MINERU_BIN,
                    "parse",
                    "--json",
                    "--tier",
                    os.environ.get("MINERU_SIDECAR_TIER", "flash"),
                    # Issue#230：--wait 不传 CLI 缺省 60s（真卷必超时）；--pages 不传
                    # CLI 默认只解析前 10 页（静默截断）。均可配，缺省就安全。
                    "--wait",
                    str(_mineru_wait_sec()),
                    "--pages",
                    os.environ.get("MINERU_SIDECAR_PAGES", "all"),
                    "-o",
                    str(out_dir / (input_path.stem + ".md")),
                    str(input_path),
                ],
                capture_output=True,
                timeout=MINERU_TIMEOUT_SEC,
            )
            if proc.returncode != 0:
                exit_code = proc.returncode
                output_tail = proc.stdout or proc.stderr or b""
                break
            try:
                parse_results.append(json.loads(proc.stdout.decode(errors="replace")))
            except Exception:
                parse_results.append({})
        # 收取 doclib 结构化产物（4.x schema；backend 侧 3.x content_list 适配另案）。
        # Issue#230 缺陷③：大文档按窗口拆多份 json，直接拷会撞后端
        # mineru4_multiple_docs fail-closed（整单作废）⇒ 同 sha 窗口按 page_idx
        # 合并成单份；合并后 pages 数必须等于 metadata.page_count，否则任务显式
        # 失败（残卷不许静默出厂——缺陷②的静默截断就是事故本体）。
        # 门⑥ I2：MINERU_SIDECAR_PAGES 传部分 range（如 1-5）是开发调试旋钮，
        # 截断校验放宽为显式 warning——不静默、也不误杀故意的部分解析。
        home = Path(os.environ.get("MINERU_SIDECAR_HOME", str(Path.home() / ".mineru")))
        partial_pages = os.environ.get("MINERU_SIDECAR_PAGES", "all") != "all"
        # strict=False 是刻意的：CLI 失败项在 break 前不 append，len(parse_results)
        # <= len(inputs) 是本控制流的不变式（前缀对齐，错位结构上不可能）；
        # strict=True 反而炸掉失败路径（契约测试 test_failed_task_carries_error 实测）。
        for pr, input_path in zip(parse_results, inputs, strict=False):
            sha = (pr.get("parse") or {}).get("sha256") or ""
            tier = (pr.get("parse") or {}).get("tier") or os.environ.get("MINERU_SIDECAR_TIER", "flash")
            if not sha:
                continue
            lib = home / "doclib" / "parsed" / sha[:2] / sha / tier
            raw_docs = []
            for f in sorted(lib.glob("*.json")):
                try:
                    raw_docs.append(json.loads(f.read_text(encoding="utf-8")))
                except Exception as exc:  # noqa: BLE001 —— 坏窗口不许静默丢
                    raise RuntimeError(f"mineru doclib json unreadable: {f.name}: {exc}")
            if not raw_docs:
                continue
            doc, merge_warns = _merge_docvortex_docs(raw_docs)
            if merge_warns:
                task.setdefault("warnings", []).extend(merge_warns)
            pages = doc.get("pages") or []
            page_count = (doc.get("metadata") or {}).get("page_count")
            if isinstance(page_count, int) and page_count != len(pages):
                if partial_pages:
                    task.setdefault("warnings", []).append(
                        f"partial pages requested ({os.environ['MINERU_SIDECAR_PAGES']}):"
                        f" pages={len(pages)} metadata.page_count={page_count}（部分解析，非全卷）"
                    )
                else:
                    raise RuntimeError(
                        f"mineru truncated: pages={len(pages)} metadata.page_count={page_count}"
                        f"（输入 {input_path.name}；残卷不许出厂，请检查 --pages/窗口收取）"
                    )
            elif not isinstance(page_count, int):
                # 门⑥ M3：page_count 缺失 = 页数口径不可证（fail-open 缺口），
                # 必须留痕让下游/值守可见，不许静默跳过
                task.setdefault("warnings", []).append(
                    f"mineru metadata.page_count missing: pages={len(pages)}（截断校验不可用）"
                )
            dest = out_dir / f"mineru4_{tier}_{input_path.stem}.json"
            dest.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        # 进度按时间粗估（CLI 无结构化进度输出）；完成时置 100。
        task["progress"] = min(
            90, 5 + int((time.monotonic() - t0) / max(MINERU_TIMEOUT_SEC, 1) * 85)
        )
        if exit_code != 0:
            tail = output_tail[-500:].decode(errors="replace")
            raise RuntimeError(f"mineru exit={exit_code}: {tail}")
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
        "warnings": [],  # Issue#230：去重/截断等非致命异常显式可见
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
        "warnings": task.get("warnings") or [],
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
