"""Issue#273 Task 6：误判样本 registry loader。

registry.json 与种子样本 PDF 全部登记在**私有 testdata 仓**
（``testdata/canaries/routing-misjudgment-registry/``），主仓（公开）不含任何案卷内容。
目录解析顺序（spec §B2a）：

1. env ``ROUTING_REGISTRY_DIR`` —— 指向**含 registry.json 的目录**；
2. 从 backend 目录向上最多 4 层找 ``testdata/canaries/routing-misjudgment-registry``；
3. 都找不到 ⇒ :func:`load_registry` 返回 ``[]``（CI 无 testdata 必须绿，不报错）。

entry 级容错：``file`` 键缺失或样本文件不存在 ⇒ ``file_path=None``
（消费方按 ``entry.file_path is None`` 跳过），不抛、不剔除——
占位 entry（如混排卷「待钉」）保留在清单里但没有任何可跑页。
registry 文件缺失/损坏/JSON 非法一律返回 ``[]``：registry 缺席 = 回落默认路由，
这是安全方向（默认 verify/ocr 侧），fail-open 到「无登记」等价于未启用登记。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

_ENV_VAR = "ROUTING_REGISTRY_DIR"
_REGISTRY_FILE = "registry.json"
_REGISTRY_SUBPATH = Path("testdata") / "canaries" / "routing-misjudgment-registry"
_UPWARD_LEVELS = 4  # backend → 仓根 → .worktrees → 主仓根 → 容器根（testdata 在容器根下）


@dataclass(frozen=True)
class RegistryEntry:
    file_name: str          # registry 内样本文件名（sha256 前 16 位 .pdf 或别名）
    file_path: str | None   # 解析后的绝对路径；testdata 缺失 ⇒ None
    pages: tuple[dict, ...]  # {"page", "expected_action", "expected_entities", "consequence", "correct_source", "note"}


def _registry_dir() -> Path | None:
    """定位含 registry.json 的目录；找不到返回 None。env 优先，失效则顺序回落。

    env 值**直接指向**含 registry.json 的目录；向上搜索的候选则是「父目录」，
    registry 固定落在其 ``testdata/canaries/routing-misjudgment-registry/`` 子路径下。
    """
    # env 段：直接目录。
    env_dir = os.environ.get(_ENV_VAR)
    if env_dir and (Path(env_dir) / _REGISTRY_FILE).is_file():
        return Path(env_dir)
    # 向上段：backend 自身 + 最多 4 层父目录，逐个试 <候选>/testdata/canaries/…/registry.json。
    # registry.py 位于 backend/app/services/page_routing/ 下，parents[3] = backend 目录。
    backend_dir = Path(__file__).resolve().parents[3]
    cur = backend_dir
    for _ in range(_UPWARD_LEVELS + 1):  # +1 把 backend 自身也纳入（其下可能直接挂 testdata）
        if ((cur / _REGISTRY_SUBPATH) / _REGISTRY_FILE).is_file():
            return cur / _REGISTRY_SUBPATH
        cur = cur.parent
    return None


def load_registry() -> list[RegistryEntry]:
    """读 registry.json 并解析 entries。任何缺失/损坏路径都折算成 []，绝不抛。"""
    reg_dir = _registry_dir()
    if reg_dir is None:
        return []
    try:
        raw = json.loads((reg_dir / _REGISTRY_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):  # 读不到 / JSON 非法（JSONDecodeError ⊂ ValueError）
        return []
    if not isinstance(raw, dict):
        return []
    entries: list[RegistryEntry] = []
    for item in raw.get("entries") or []:
        if not isinstance(item, dict):
            continue
        file_name = str(item.get("file") or "")
        pages = tuple(p for p in (item.get("pages") or []) if isinstance(p, dict))
        file_path: str | None = None
        if file_name:
            candidate = reg_dir / file_name
            if candidate.is_file():
                file_path = str(candidate.resolve())
        entries.append(RegistryEntry(file_name=file_name, file_path=file_path, pages=pages))
    return entries


def expected_action_allows(entry_page: dict, action: str) -> bool:
    """允许集（竖线分隔）判定：``expected_action: "ocr|both"`` ⇒ {"ocr","both"}。

    entry_page 非法 / expected_action 缺失 / action 为空 ⇒ False（从严）。
    """
    if not isinstance(entry_page, dict) or not action:
        return False
    allowed_raw = str(entry_page.get("expected_action") or "")
    allowed = {part.strip() for part in allowed_raw.split("|") if part.strip()}
    return action in allowed
