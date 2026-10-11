"""eval-ng manifest v2 加载器。

公开骨架（仓内，相对引用+期望内容 hash）+ 实例本地私有 override 合并；
合并后按 content hash 校验考卷字节级一致（冷评审 I4）。
类型映射在此收敛（设计 §4：TYPE_MAPS 数据侧唯一一份）。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ..engine.protocol import AnswerForm, ExamItem

SUPPORTED_FORMS = {f.value for f in AnswerForm}


class ManifestMismatch(Exception):
    """公开骨架与私有 override 解析出的考卷 hash 不一致。"""


class ManifestError(ValueError):
    """manifest 结构非法。"""


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_type_maps() -> dict[str, dict[str, str]]:
    """类型映射（自旧 eval/benchmarks/t2/spec.py TYPE_MAPS 收敛，值冻结不变）。

    旧文件冻结只读；新代码只 import 本函数。
    """
    return {
        "cluener": {
            "name": "姓名",
            "address": "地址",
            "organization": "机构名称",
            "company": "机构名称",
            "government": "机构名称",
            "position": "职位",
            "scene": "地址",
        },
        "leven": {},
        "resume": {
            "NAME": "姓名",
            "LOCATION": "地址",
            "ORGANIZATION": "机构名称",
            "RACE": "民族",
        },
    }


def _validate_entry(entry: dict, origin: str) -> None:
    for key in ("id", "exam_id", "form"):
        if key not in entry:
            raise ManifestError(f"{origin}: 条目缺字段 {key!r}: {entry}")
    if "path" not in entry and "resolved_path" not in entry:
        raise ManifestError(f"{origin}: 条目缺 path/resolved_path: {entry}")
    if entry["form"] not in SUPPORTED_FORMS:
        raise ManifestError(f"{origin}: 未知 form {entry['form']!r}（条目 {entry['id']}）")


def load_manifest_v2(
    public_path: Path, private_path: Path | None = None, *, base_dir: Path | None = None
) -> list[ExamItem]:
    """加载 manifest v2：公开骨架 + 私有 override（按 id 覆盖/补充真实路径）。

    合并语义：
    - public 条目 path 为相对引用（相对 base_dir，默认 manifest 所在目录）；
    - private 条目（同 id）提供 path/gt 的实例本地真实路径，并覆盖 content_sha256/gt_sha256；
    - 若两侧均给出 content_sha256 且文件可达，则按文件实际 hash 校验一致，
      不一致 raise ManifestMismatch（"两边解析出的考卷字节级相同"）。
    """
    base_dir = base_dir or Path(public_path).parent
    public_doc = json.loads(Path(public_path).read_text(encoding="utf-8"))
    entries: dict[str, dict] = {}
    for entry in public_doc.get("files", []):
        _validate_entry(entry, f"public:{public_path}")
        entries[entry["id"]] = dict(entry)

    if private_path is not None and Path(private_path).exists():
        private_doc = json.loads(Path(private_path).read_text(encoding="utf-8"))
        for entry in private_doc.get("files", []):
            _validate_entry(entry, f"private:{private_path}")
            eid = entry["id"]
            if eid in entries:
                merged = {**entries[eid], **{k: v for k, v in entry.items() if v is not None}}
                entries[eid] = merged
            else:
                entries[eid] = dict(entry)

    items: list[ExamItem] = []
    for eid, entry in entries.items():
        form = AnswerForm(entry["form"])
        gt = entry.get("gt")

        payload_path = entry.get("resolved_path") or entry["path"]
        resolved = Path(payload_path)
        if not resolved.is_absolute():
            resolved = base_dir / resolved

        expected_sha = entry.get("content_sha256")
        if expected_sha and resolved.exists():
            actual = _sha256_file(resolved)
            if actual != expected_sha:
                raise ManifestMismatch(
                    f"考卷 {eid} 内容 hash 不符：manifest 声明 {expected_sha[:12]}…，"
                    f"实际 {actual[:12]}…（{resolved}）"
                )

        payload: dict = {"path": str(resolved)}
        if form is AnswerForm.ENTITY_TABLE:
            # 考卷文本由数据文件提供（jsonl/json 单条 entry），此处仅记录路径；
            # types 由 runner 从 gt 派生（gt=None 时由 runner 填全类型清单）。
            payload["source_path"] = str(resolved)
        if entry.get("sidecars"):
            payload["sidecars"] = entry["sidecars"]
        if form is AnswerForm.ROBUSTNESS:
            payload["expect_reject"] = bool(entry.get("expect_reject", True))

        items.append(
            ExamItem(id=eid, exam_id=entry["exam_id"], form=form, payload=payload, gt=gt)
        )
    return items
