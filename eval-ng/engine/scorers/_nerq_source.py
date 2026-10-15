"""打分口径加载：唯一事实源 = backend/scripts/eval/eval_ner_quality.py（不复制）。

AC5（零漂移）：eval-ng 全仓不得出现打分函数本体定义；本模块是唯一 import 出口。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_NERQ_PATH = Path(__file__).resolve().parents[3] / "backend" / "scripts" / "eval" / "eval_ner_quality.py"

_cached = None


def load_backend_nerq():
    """importlib 加载 backend 打分口径源，进程内缓存。"""
    global _cached
    if _cached is not None:
        return _cached
    if not _NERQ_PATH.exists():
        raise FileNotFoundError(f"打分口径源不存在: {_NERQ_PATH}")
    spec = importlib.util.spec_from_file_location("evalng_backend_nerq", _NERQ_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["evalng_backend_nerq"] = mod
    spec.loader.exec_module(mod)
    _cached = mod
    return mod
