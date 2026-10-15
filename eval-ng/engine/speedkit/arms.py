"""speedkit.arms：臂定义 schema + 声明状态捕获 + patch 臂的可证伪前置。

臂定义 JSON（示例见 `eval-ng/arms/*.json`；**真实臂清单在私有仓**）：

```json
{
  "name": "base",
  "kind": "env",
  "env": {"HAS_NER_GLOBAL_MAX_INFLIGHT": "8"},
  "patches": [{"file": "frontend/src/constants/timing.ts", "from": "= 3;", "to": "= 1;"}],
  "expect": [{"file": "frontend/src/constants/timing.ts", "contains": "= 1;"}],
  "probe": "python",
  "notes": "页并发 3"
}
```

规则（对齐验收方案 v2 D2/D8/G2）：

- `kind ∈ {env, patch}`；**未知键即报错**（schema 严格，不静默吞）。
- `env` 臂只允许改进程环境（由**编排**在重启后端时生效；引擎不 spawn 任何进程）。
- `patch` 臂：引擎**只读**声明并断言 (a) 跑前工作树干净、(b) 声明的替换/`expect` 确实已生效
  （静默未生效即 FAIL）、(c) 臂跑完工作树必须复原（脏 ⇒ FAIL，不许静默继续）。
- `probe` 指明对照仪器：`python` = 本模块；`vitest` = 归档前端探针（`#106` D3）。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

ARM_KEYS = frozenset({"name", "kind", "env", "patches", "expect", "probe", "notes"})
ARM_KINDS = frozenset({"env", "patch"})
PATCH_KEYS = frozenset({"file", "from", "to"})
EXPECT_KEYS = frozenset({"file", "contains"})
PROBE_KINDS = frozenset({"python", "vitest", None})
REPO_ROOT = Path(__file__).resolve().parents[3]


def load_arm(path: str) -> dict:
    """读并严格校验臂定义；任何越界键或字段缺失都抛 `ValueError`（CLI 转非零退出）。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"臂定义 {path} 必须是 JSON 对象")
    unknown = set(data) - ARM_KEYS
    if unknown:
        raise ValueError(f"臂定义 {path} 含未知键：{', '.join(sorted(unknown))}")
    name = str(data.get("name") or "").strip()
    if not name:
        raise ValueError(f"臂定义 {path} 缺 name")
    kind = data.get("kind")
    if kind not in ARM_KINDS:
        raise ValueError(f"臂定义 {path} 的 kind={kind!r} 非法，应为 {sorted(ARM_KINDS)}")
    env = data.get("env") or {}
    if not isinstance(env, dict):
        raise ValueError(f"臂定义 {path} 的 env 必须是对象")
    for key, value in env.items():
        if not isinstance(value, (str, int, float, bool)):
            raise ValueError(f"臂定义 {path} 的 env.{key} 必须是标量")
    patches = data.get("patches") or []
    if not isinstance(patches, list):
        raise ValueError(f"臂定义 {path} 的 patches 必须是数组")
    for patch in patches:
        if not isinstance(patch, dict) or not PATCH_KEYS <= set(patch):
            raise ValueError(f"臂定义 {path} 的 patch 缺 {sorted(PATCH_KEYS)}：{patch!r}")
    expect = data.get("expect") or []
    if not isinstance(expect, list):
        raise ValueError(f"臂定义 {path} 的 expect 必须是数组")
    for item in expect:
        if not isinstance(item, dict) or not EXPECT_KEYS <= set(item):
            raise ValueError(f"臂定义 {path} 的 expect 缺 {sorted(EXPECT_KEYS)}：{item!r}")
    probe = data.get("probe")
    if probe not in PROBE_KINDS:
        raise ValueError(f"臂定义 {path} 的 probe={probe!r} 非法，应为 python / vitest / null")
    if kind == "patch" and not (patches or expect):
        raise ValueError(f"臂定义 {path} 是 patch 臂却既无 patches 也无 expect（无法判定是否生效）")
    return {
        "name": name,
        "kind": kind,
        "env": {str(k): str(v) for k, v in env.items()},
        "patches": patches,
        "expect": expect,
        "probe": probe,
        "notes": str(data.get("notes") or ""),
        "source": str(path),
    }


def arm_report_block(arm: dict) -> dict:
    """入报告的臂快照（不含判定，判定见 `arm_checks`）。"""
    return {
        "name": arm.get("name"),
        "kind": arm.get("kind"),
        "source": arm.get("source"),
        "env": dict(arm.get("env") or {}),
        "patches": list(arm.get("patches") or []),
        "expect": list(arm.get("expect") or []),
        "probe": arm.get("probe"),
        "notes": arm.get("notes"),
        "checkout_root": str(REPO_ROOT),
    }


def git_porcelain(root: Path | str = REPO_ROOT, *, ignore_paths: tuple[str, ...] = ()) -> list[str]:
    """`git status --porcelain` 的行（`ignore_paths` 用于排除引擎自己写出的报告产物）。"""
    try:
        out = subprocess.run(
            ["git", "status", "--porcelain"], cwd=str(root), capture_output=True, text=True,
            timeout=20, check=False,
        )
    except (FileNotFoundError, subprocess.SubprocessError, OSError) as exc:
        raise ValueError(f"git status 不可用（{root}）：{exc!r}") from exc
    if out.returncode != 0:
        raise ValueError(f"git status 失败（{root}）：{out.stderr.strip()[:200]}")
    lines = [line for line in out.stdout.splitlines() if line.strip()]
    for ignored in ignore_paths:
        lines = [line for line in lines if ignored not in line]
    return lines


def arm_checks(arm: dict, *, phase: str, ignore_paths: tuple[str, ...] = ()) -> list[dict]:
    """臂的四条机器判定：树状态（两次）+ patch 生效 + probe 声明。"""
    checks: list[dict] = []
    kind = arm.get("kind")
    dirty = git_porcelain(REPO_ROOT, ignore_paths=ignore_paths)
    if kind == "patch":
        if phase == "pre" and dirty:
            checks.append({"id": "D8.patch_arm_pretree", "status": "BLOCKED",
                           "detail": f"patch 臂跑前工作树非空：{dirty[:5]}（不得在脏树上跑臂）", "value": len(dirty)})
            return checks
        if phase == "post" and dirty:
            checks.append({"id": "D8.patch_arm_revert", "status": "FAIL",
                           "detail": f"patch 臂跑完工作树未复原（还原失败必须报错）：{dirty[:5]}", "value": len(dirty)})
            return checks
        checks.append({"id": f"D8.patch_arm_{'pretree' if phase == 'pre' else 'revert'}",
                       "status": "PASS",
                       "detail": "工作树干净（patch 臂前置/还原断言）", "value": len(dirty)})
    else:
        checks.append({"id": "D8.env_arm_no_file_write", "status": "PASS",
                       "detail": "env 臂只声明进程环境；引擎不触碰工作树", "value": None})
    if phase == "pre":
        checks.extend(patch_effect_checks(arm))
    return checks


def patch_effect_checks(arm: dict) -> list[dict]:
    """声明已生效断言：`expect` 与 `patches[].to` 必须真在文件里（静默未生效即 FAIL）。"""
    checks: list[dict] = []
    targets: list[tuple[str, str]] = []
    for item in arm.get("patches") or []:
        targets.append((str(item["file"]), str(item["to"])))
    for item in arm.get("expect") or []:
        targets.append((str(item["file"]), str(item["contains"])))
    for rel, needle in targets:
        path = REPO_ROOT / rel
        if not path.exists():
            checks.append({"id": "D8.patch_effect", "status": "FAIL",
                           "detail": f"臂声明的文件不存在：{rel}", "value": None})
            continue
        content = path.read_text(encoding="utf-8", errors="ignore")
        if needle not in content:
            checks.append({"id": "D8.patch_effect", "status": "FAIL",
                           "detail": f"{rel} 未含声明的标记 {needle!r} ⇒ 补丁静默未生效", "value": None})
        else:
            checks.append({"id": "D8.patch_effect", "status": "PASS",
                           "detail": f"{rel} 含声明标记 {needle!r}", "value": None})
    if not targets:
        checks.append({"id": "D8.patch_effect", "status": "UNVERIFIED",
                       "detail": "臂未声明 patches/expect，无可核的生效证据（env 臂正常）", "value": None})
    return checks


def patch_state_digest(arm: dict) -> dict:
    """跑前/跑后内容摘要（供编排查还原；不参与判定）。"""
    digest: dict[str, str] = {}
    for item in (arm.get("patches") or []) + (arm.get("expect") or []):
        rel = str(item.get("file"))
        path = REPO_ROOT / rel
        if rel not in digest and path.exists():
            data = path.read_bytes()
            import hashlib

            digest[rel] = hashlib.sha256(data).hexdigest()
    return digest
