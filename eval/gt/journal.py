"""软层审计日志（Issue#56 P1，契约见方案 §6.4）。

软层（Agent A/B）的每一次调用都落一条 journal：输入 hash、原始响应、闸门裁定、
耗时、token。它是三件事的唯一凭据——

- **归因**：一条争议为什么被这么裁，回放 journal 就能重演；
- **成本**：token 与耗时从哪里来；
- **AC4 对账**：走过了软层的争议，journal 里必须找得到对应条目，
  ``reconcile`` 把"漏记"变成可见的 missing 清单。

写入策略：**每条立即追加落盘**——审计日志的写入时刻就是它防篡改能力最强的
时刻，攒批落盘等于给"进程崩了丢审计"留门。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

SCHEMA_VERSION = "gt-journal-v1"

#: §6.4 契约字段（校验用；skip_reason 允许 None 但键必须在）
FIELDS = ("ts", "stage", "agent_id", "context_id", "model_id", "prompt_version",
          "input_hash", "raw_response", "gate_verdict", "skip_reason",
          "elapsed_s", "tokens")

#: 闸门裁定的合法取值（§6.4：pass | fail | unmodeled）
GATE_VERDICTS = ("pass", "fail", "unmodeled")


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class JournalEntry(dict):
    """审计条目——就是 dict，加一个字段完整性校验入口。"""

    @classmethod
    def build(cls, **kw) -> "JournalEntry":
        missing = [f for f in FIELDS if f not in kw]
        if missing:
            raise ValueError(f"journal 条目缺契约字段：{missing}")
        verdict = kw["gate_verdict"]
        if verdict not in GATE_VERDICTS:
            raise ValueError(
                f"gate_verdict {verdict!r} 非法，须 ∈ {GATE_VERDICTS}")
        entry = {"schema_version": SCHEMA_VERSION, **kw}
        # 可选业务字段：争议关联与扩展位（不进 FIELDS 契约，向后兼容）
        entry.setdefault("dispute_id", None)
        entry.setdefault("extra", None)
        return cls(entry)


class Journal:
    """JSONL 追加式审计日志。``Journal(path)`` 打开即续写，不覆盖既有条目。"""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else None
        self.entries: list[JournalEntry] = []
        if self.path and self.path.exists():
            self.entries.extend(self.load(self.path).entries)

    def record(self, **kw) -> JournalEntry:
        """写一条审计：缺契约字段 / 非法裁定值当场报错，不静默。"""
        entry = JournalEntry.build(ts=_now(), **kw)
        self.entries.append(entry)
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
        return entry

    # -- AC4 对账 -------------------------------------------------------------

    def reconcile(self, dispute_ids: list[str], stage: str) -> dict:
        """对账：走过软层（stage）的每条争议都必须有 journal 条目。

        返回 ``{"missing": [无记录的 dispute_id...], "journaled": 有记录数}``。
        missing 非空 = 审计链有洞，放行前必须先补齐。
        """
        seen = {e["dispute_id"] for e in self.entries
                if e.get("stage") == stage and e.get("dispute_id")}
        missing = [d for d in dispute_ids if d not in seen]
        return {"missing": missing, "journaled": len(dispute_ids) - len(missing)}

    # -- 持久化 ----------------------------------------------------------------

    @classmethod
    def load(cls, path: str | Path) -> "Journal":
        entries = []
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    entries.append(JournalEntry(json.loads(line)))
        j = cls(None)
        j.entries = entries
        return j
