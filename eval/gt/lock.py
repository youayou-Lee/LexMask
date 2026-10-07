"""跑批单实例守卫：同一工作目录同时只允许一个跑批进程（2026-10-07 实战缺陷）。

两个并发 verify_engine 进程在同一 work 目录上竞态（pack 双写、隧道过载）
的回归防线：``{work}/.lock`` 记录持锁 pid——锁存在**且 pid 仍存活**则拒绝；
pid 已死（上次崩溃残留）视为陈旧锁，清掉后继续。锁文件在进程退出时由
调用方的 try/finally（或 :func:`release_work_lock`）删除。
"""
from __future__ import annotations

import errno
import os
from pathlib import Path


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError as e:
        return e.errno == errno.EPERM  # EPERM = 进程存在但无权发信号；ESRCH = 不存在
    return True


def acquire_work_lock(work_dir: Path) -> None:
    """在 work_dir 上获取单实例锁；已有存活持锁者时抛 RuntimeError。"""
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    lock_path = work_dir / ".lock"
    if lock_path.exists():
        try:
            holder = int(lock_path.read_text(encoding="utf-8").strip())
        except ValueError:
            holder = None
        if holder is not None and _pid_alive(holder):
            raise RuntimeError(
                f"工作目录已被另一个跑批进程占用（pid={holder}，锁 {lock_path}）。"
                "并发跑批会在同一 work 上竞态（pack 双写、通道过载）——"
                "请先等该进程结束，或确认其已死后删除锁文件重跑。")
        # 陈旧锁（持者已死或内容损坏）：清掉后继续
        lock_path.unlink(missing_ok=True)
    lock_path.write_text(str(os.getpid()), encoding="utf-8")


def release_work_lock(work_dir: Path) -> None:
    """删除锁文件（仅当锁记录的是本进程 pid，避免误删他人锁）。"""
    lock_path = Path(work_dir) / ".lock"
    try:
        if lock_path.read_text(encoding="utf-8").strip() == str(os.getpid()):
            lock_path.unlink(missing_ok=True)
    except (OSError, ValueError):
        pass
