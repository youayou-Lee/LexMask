"""把 eval-ng/ 注册为命名包 evalng_pkg 供 backend/tests 各测试文件共享。

eval-ng 是独立目录（非 backend 包内），测试用 importlib 按包结构加载，
使模块内相对 import（from ..engine.protocol import ...）正常工作。
"""

import importlib.util
import sys
import types
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
EVALNG_DIR = REPO / "eval-ng"

_SUBPKGS = ("engine", "engine/scorers", "engine/gates", "datasets", "participants", "tests")


def _register_pkg():
    if "evalng_pkg" in sys.modules:
        return sys.modules["evalng_pkg"]
    pkg = types.ModuleType("evalng_pkg")
    pkg.__path__ = [str(EVALNG_DIR)]
    sys.modules["evalng_pkg"] = pkg
    for sub in _SUBPKGS:
        name = "evalng_pkg." + sub.replace("/", ".")
        mod = types.ModuleType(name)
        mod.__path__ = [str(EVALNG_DIR / sub)]
        sys.modules[name] = mod
        parent = pkg
        for part in sub.split("/")[:-1]:
            parent = getattr(parent, part)
        setattr(parent, sub.split("/")[-1], mod)
    return pkg


def load(relpath: str):
    """加载 eval-ng 下相对路径模块（包上下文内，相对 import 可用）。"""
    pkg = _register_pkg()
    mod_name = "evalng_pkg." + relpath.replace("/", ".").removesuffix(".py")
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, EVALNG_DIR / relpath)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    # 挂到父包属性，保证 from ..xxx import 语义完整
    parent = pkg
    parts = mod_name.split(".")[1:-1]
    for part in parts:
        parent = getattr(parent, part)
    setattr(parent, mod_name.split(".")[-1], mod)
    return mod
