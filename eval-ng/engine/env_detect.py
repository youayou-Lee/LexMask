"""环境硬件自动探测（用户反馈：机器自己知道自己插的什么卡，不该让人手填）。

探测顺序：NVIDIA（nvidia-smi）→ DCU（hy_smi）。探测失败返回空串，
由调用方决定：人工传了 --hardware 以人工为准；两者皆无 → 拒绝评测（AC4）。
"""

from __future__ import annotations

import subprocess


def _glob(pattern: str) -> list[str]:
    import glob as _g

    return _g.glob(pattern)


def _run(cmd: list[str], timeout: float = 10.0) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return ""
    return r.stdout if r.returncode == 0 else ""


def detect_hardware(run=None) -> str:
    """返回硬件名（如 'NVIDIA GeForce RTX 5090' / 'K500SM_AI'）；探测不到返回 ''。

    run：可注入的命令执行器（单测用），签名 (cmd_list) -> stdout_str。
    """
    run = run or _run
    out = run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"])
    if out.strip():
        return out.strip().splitlines()[0].strip()
    # DCU（本项目唯一部署平台，默认走 hy-smi-wrapper——输出干净且 Card Series 即具体型号；
    # hy_smi.py 为兜底）。优先级：型号/产品名 > 系列名。
    for cmd in ([["hy-smi-wrapper", "--showproductname"]]
                + [["python3", p, "--showproductname"]
                   for p in sorted(_glob("/opt/dtk-*/bin/hy_smi.py"))]):
        out = run(cmd)
        lines = out.splitlines()
        for want in ("card model", "product name", "卡型号", "card series", "series"):
            for line in lines:
                if ":" in line and want in line.lower():
                    return line.rsplit(":", 1)[1].strip()
        if out.strip():
            first = out.strip().splitlines()[0].strip()
            if first:
                return first[:80]
    return ""
