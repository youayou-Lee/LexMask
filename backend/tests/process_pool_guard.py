"""进程池可用性守卫（Issue#109，抽出供多个测试文件复用）。

`safe_compile` / `safe_finditer` / `safe_finditer_many` 都靠子进程池落实 ReDoS
预算。部分开发容器/CI 沙箱禁用 POSIX 信号量，`ProcessPoolExecutor(...)` 在
**构造期**就抛 `PermissionError: [Errno 13] Permission denied`
（`multiprocessing/synchronize.py` 的 `SemLock.__init__`）——这种环境下真实池
用例是「无法执行」而非「断言失败」，应显式跳过而不是留一堆假红灯；CI（完整
权限的 Linux）会真执行它们。
"""

import multiprocessing
from concurrent.futures import ProcessPoolExecutor

import pytest


def process_pool_unusable_reason() -> str | None:
    """本环境建不出进程池时返回原因，可用则返回 `None`。

    只把 `OSError`（含 `PermissionError`）当作「环境不可用」；其他异常应当炸
    出来，而不是被宽 `except` 静默吞掉、把用例伪装成「跳过」。
    """
    try:
        with ProcessPoolExecutor(
            max_workers=1, mp_context=multiprocessing.get_context("spawn")
        ):
            pass
    except OSError as exc:  # 含 PermissionError（SemLock 受限）
        return (
            f"本环境无法创建进程池（{type(exc).__name__}: {exc}）"
            "，真实池用例跳过；见 Issue#109"
        )
    return None


POOL_SKIP_REASON = process_pool_unusable_reason()

requires_process_pool = pytest.mark.skipif(
    POOL_SKIP_REASON is not None,
    reason=POOL_SKIP_REASON or "",
)