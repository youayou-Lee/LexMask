"""争议条目与队列状态机（Issue#56 P1，契约见方案 §6.1/§6.2）。

两条通道跑完、仲裁 R1–R7 后剩下的分歧进入本模块的队列，之后每一步流转
（归一回一关闭 / 软层 Agent / 闸门 / 人审）都是**状态机上的一次显式迁移**：
非法迁移一律拒收报错，绝不静默——审计链断裂是所有"跑歪了没人知道"的起点。

状态机（§6.1）::

    pending
      ├─▶ filtered_out           （预过滤：非考点 / 手写门）
      ├─▶ normalized_closed      （L0.5 归一回一，不调模型）
      └─▶ agent_running
            ├─▶ agent_done
            │     ├─▶ gated_pass     （闸门放行，自动定案 ✅）
            │     ├─▶ gated_fail ─▶ human_pending
            │     └─▶ unmodeled      （逃逸口 §9）
            └─▶ agent_error ─▶ 重试 agent_running / 放弃 human_pending
    human_pending ─▶ human_decided（review_ui）

**幂等要求**（AC2）：同一 dispute 重跑，``normalized_closed`` / ``gated_pass``
结论逐位一致——本模块只做确定性状态迁移，不做任何判断，判断都在
``reduce``（纯函数）与 ``gate``（纯函数）里。

条目契约（§6.2）固定字段：``dispute_id / page_id / page_type / rule / kind /
entity_type / domain / candidates / page_image_ref / status / revision``，
外加 ``schema_version`` 与迁移历史 ``history``（from/to/ts）。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

SCHEMA_VERSION = "gt-dispute-v1"

# ---- 状态常量（唯一事实源） --------------------------------------------------
STATUS_PENDING = "pending"
STATUS_FILTERED_OUT = "filtered_out"
STATUS_NORMALIZED_CLOSED = "normalized_closed"
STATUS_AGENT_RUNNING = "agent_running"
STATUS_AGENT_DONE = "agent_done"
STATUS_AGENT_ERROR = "agent_error"
STATUS_GATED_PASS = "gated_pass"
STATUS_GATED_FAIL = "gated_fail"
STATUS_UNMODELED = "unmodeled"
STATUS_HUMAN_PENDING = "human_pending"
STATUS_HUMAN_DECIDED = "human_decided"

#: 终态：再迁出即非法（gated_pass 自动定案、human_decided 人已裁、其余出口已定）
TERMINAL_STATUSES = frozenset({
    STATUS_FILTERED_OUT, STATUS_NORMALIZED_CLOSED, STATUS_GATED_PASS,
    STATUS_UNMODELED, STATUS_HUMAN_DECIDED,
})

#: 合法迁移表（§6.1 的代码化；不在表内 = 拒收）
TRANSITIONS: dict[str, frozenset[str]] = {
    STATUS_PENDING: frozenset({
        STATUS_FILTERED_OUT, STATUS_NORMALIZED_CLOSED, STATUS_AGENT_RUNNING}),
    STATUS_AGENT_RUNNING: frozenset({STATUS_AGENT_DONE, STATUS_AGENT_ERROR}),
    STATUS_AGENT_DONE: frozenset({
        STATUS_GATED_PASS, STATUS_GATED_FAIL, STATUS_UNMODELED}),
    STATUS_AGENT_ERROR: frozenset({STATUS_AGENT_RUNNING, STATUS_HUMAN_PENDING}),
    STATUS_GATED_FAIL: frozenset({STATUS_HUMAN_PENDING}),
    STATUS_HUMAN_PENDING: frozenset({STATUS_HUMAN_DECIDED}),
    # 终态不可迁出，仍显式登记便于报错信息可读
    STATUS_FILTERED_OUT: frozenset(),
    STATUS_NORMALIZED_CLOSED: frozenset(),
    STATUS_GATED_PASS: frozenset(),
    STATUS_UNMODELED: frozenset(),
    STATUS_HUMAN_DECIDED: frozenset(),
}


class InvalidTransition(ValueError):
    """非法状态迁移——拒收并报错，不静默。"""


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def make_dispute_id(page_id: str, rule: str, entity_type: str, key: str) -> str:
    """§6.2：``dispute_id = sha1(page_id|rule|type|key)``。稳定、可复算。"""
    raw = "|".join((page_id, rule, entity_type, key))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def new_dispute(page_id: str, rule: str, entity_type: str, key: str,
                kind: str = "entity", domain: str | None = None,
                candidates: dict | None = None,
                page_image_ref: dict | None = None,
                page_type: str = "body") -> dict:
    """造一条 pending 争议（§6.2 契约形状，纯 dict 便于 JSONL 与幂等断言）。"""
    return {
        "schema_version": SCHEMA_VERSION,
        "dispute_id": make_dispute_id(page_id, rule, entity_type, key),
        "page_id": page_id,
        "page_type": page_type,
        "rule": rule,
        "kind": kind,
        "entity_type": entity_type,
        "domain": domain,
        "candidates": candidates if candidates is not None else {"a": [], "b": [], "md": []},
        "page_image_ref": page_image_ref or {},
        "status": STATUS_PENDING,
        "revision": 1,
        "history": [],
    }


class DisputeQueue:
    """争议队列：内存 dict + JSONL 落盘。迁移合法性在此把关。"""

    def __init__(self, disputes: list[dict] | None = None):
        self._items: dict[str, dict] = {}
        for d in disputes or []:
            self.add(d)

    # -- 增查 ---------------------------------------------------------------

    def add(self, dispute: dict) -> None:
        did = dispute.get("dispute_id")
        if not did:
            raise ValueError("dispute 缺 dispute_id")
        if did in self._items:
            raise ValueError(f"dispute_id 重复：{did}")
        self._items[did] = dispute

    def get(self, dispute_id: str) -> dict:
        return self._items[dispute_id]

    def all(self) -> list[dict]:
        return list(self._items.values())

    def pending_items(self) -> list[dict]:
        return [d for d in self._items.values() if d["status"] == STATUS_PENDING]

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for d in self._items.values():
            out[d["status"]] = out.get(d["status"], 0) + 1
        return dict(sorted(out.items()))

    def terminal(self, dispute_id: str) -> bool:
        """终态判定（gated_pass 自动定案 / human_decided 人已裁 / 其余出口已定）。"""
        return self.get(dispute_id)["status"] in TERMINAL_STATUSES

    # -- 迁移 ---------------------------------------------------------------

    def transition(self, dispute_id: str, to_status: str) -> dict:
        d = self._items[dispute_id]
        frm = d["status"]
        legal = TRANSITIONS.get(frm)
        if legal is None:
            raise InvalidTransition(f"未知状态 {frm!r}（dispute {dispute_id}）")
        if to_status not in legal:
            raise InvalidTransition(
                f"非法迁移 {frm!r} → {to_status!r}（dispute {dispute_id}）；"
                f"合法出路：{sorted(legal)}")
        d["status"] = to_status
        d["revision"] = d.get("revision", 1) + 1
        d.setdefault("history", []).append({"from": frm, "to": to_status, "ts": _now()})
        return d

    # -- 持久化（JSONL） ------------------------------------------------------

    def save(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            for d in self._items.values():
                f.write(json.dumps(d, ensure_ascii=False, sort_keys=True) + "\n")

    @classmethod
    def load(cls, path: str | Path) -> "DisputeQueue":
        items = []
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    items.append(json.loads(line))
        return cls(items)
