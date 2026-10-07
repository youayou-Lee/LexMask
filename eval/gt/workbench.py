"""GT 标注工作台数据层（Issue#56 M3 / Task 1+2）。

纯本地、零网络、只读写工作目录（``--work``），不碰任何 git 仓（铁律 1：
GT 原文不进仓）。工作目录布局::

    {work}/pages/{page_id}/pack.json   M1 Task 7 组装的逐页 pagepack
    {work}/journal.jsonl               追加式操作日志（只追加不删除）
    {work}/sample_seed.json            抽样 seed + 选中清单（同 seed 同抽样）

三键裁决（对/错/漏）写回状态机只用 ``gt_schema.VERIFY_STATES``：

- 对 → 该实体 ``verify="user-confirmed"``；
- 错 → ``correct`` 覆盖 text/type/span_* 且 ``verify="user-corrected"``；
- 漏 → ``correct`` 为新实体（``origin="user"``、``verify="user-confirmed"``）
  追加进 entities。

对应页上**与被裁决实体关联的**仲裁条目改 ``user:{对|错|漏}``——关联 = 该
disputed 条目的 candidates（a/b/...）里存在候选其 ``span_original`` 与实体相同
（退而求其次 ``text`` 相同）；无关联 disputed 条目时不碰任何仲裁（典型如漏：
被漏实体本无条目），其余 disputed 条目保持 disputed 等待各自裁决。

撤销（控制器裁定 1）：**快照还原，不算逆**——每条 journal 行内嵌被触 pack 的
``entities``+``adjudications`` 操作前深拷贝快照，``undo_last`` 直接从快照恢复
并追加一条 ``op="undo"`` 行（快照 = 撤销前状态，可连续撤销）。

分层抽样（控制器裁定 3）：层键 = ``(page_type, entity_type, verify)``，只在
``verify=="consistent"`` 的实体上抽；比例 ``ratio``（默认 0.1），每个非空层至
少抽 1；``random.Random(seed)`` + 层按排序序遍历 → 确定性；seed 与选中清单落
盘 ``{work}/sample_seed.json``。

所有写回（裁决/抽检改判/撤销）写前必过 ``gt_schema.validate_pagepack``，非法
即 raise ``ValueError``，任何文件不落盘。
"""
from __future__ import annotations

import copy
import json
import math
import random
from datetime import datetime, timezone
from pathlib import Path

from gt.gt_schema import validate_pagepack

# 三键 verdict 值域（UI/HTTP 层透传）
VERDICTS = ("对", "错", "漏")

_JOURNAL = "journal.jsonl"
_SAMPLE = "sample_seed.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _pack_path(work: Path, page_id: str) -> Path:
    return Path(work) / "pages" / page_id / "pack.json"


def _read_pack(work: Path, page_id: str) -> dict:
    path = _pack_path(work, page_id)
    if not path.is_file():
        raise ValueError(f"页 {page_id!r} 不存在（缺 {path}）")
    return json.loads(path.read_text(encoding="utf-8"))


def _write_pack(work: Path, page_id: str, pack: dict) -> None:
    """写前强校验：非法即 raise ValueError，任何文件不落盘（全局约束）。"""
    errors = validate_pagepack(pack)
    if errors:
        raise ValueError(f"pagepack 校验失败（未写回 {page_id!r}）：\n"
                         + "\n".join(errors))
    _pack_path(work, page_id).write_text(
        json.dumps(pack, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def _append_journal(work: Path, line: dict) -> None:
    with (Path(work) / _JOURNAL).open("a", encoding="utf-8") as f:
        f.write(json.dumps(line, ensure_ascii=False) + "\n")


def _read_journal(work: Path) -> list[dict]:
    path = Path(work) / _JOURNAL
    if not path.is_file():
        return []
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _snapshot(pack: dict) -> dict:
    """被触 pack 的可还原快照（控制器裁定 1：快照优先于逆逻辑）。"""
    return {"entities": copy.deepcopy(pack.get("entities", [])),
            "adjudications": copy.deepcopy(pack.get("adjudications", []))}


# ---- Task 1: 装载 / 分歧清单 / stats --------------------------------------------

class Workspace:
    """工作目录的只读视图（装载后供 UI/HTTP 层遍历；写走模块级函数）。"""

    def __init__(self, work: Path, pages: dict[str, dict]):
        self.work = Path(work)
        self.pages = pages  # page_id -> pack（dict，UI 只读）

    def disputes(self) -> dict[str, list[dict]]:
        """未裁决清单：adjudications 中 verdict=="disputed"，按页分组。"""
        out: dict[str, list[dict]] = {}
        for pid, pack in self.pages.items():
            hits = [{"adjudication_index": i, **adj}
                    for i, adj in enumerate(pack.get("adjudications", []))
                    if isinstance(adj, dict) and adj.get("verdict") == "disputed"]
            if hits:
                out[pid] = hits
        return out

    def stats(self) -> dict[str, int]:
        """各 verify 状态实体计数（含 0 项，键集 = VERIFY_STATES）。"""
        from gt.gt_schema import VERIFY_STATES
        counts = {v: 0 for v in VERIFY_STATES}
        for pack in self.pages.values():
            for ent in pack.get("entities", []):
                v = ent.get("verify")
                if v in counts:
                    counts[v] += 1
        return counts


def load_workspace(work: Path) -> Workspace:
    """扫 ``{work}/pages/*/pack.json`` 装载为可操作结构。"""
    work = Path(work)
    pages: dict[str, dict] = {}
    pages_dir = work / "pages"
    if pages_dir.is_dir():
        for pack_path in sorted(pages_dir.glob("*/pack.json")):
            pack = json.loads(pack_path.read_text(encoding="utf-8"))
            pages[pack["page_id"]] = pack
    return Workspace(work, pages)


def _linked_disputed_index(pack: dict, ent: dict) -> int | None:
    """找与实体关联的 disputed 仲裁条目下标（candidates 候选 span 优先、text 次之）。

    无匹配（实体没有挂 disputed 条目，典型如漏）返回 None——不碰任何仲裁。
    """
    for i, adj in enumerate(pack.get("adjudications", [])):
        if not (isinstance(adj, dict) and adj.get("verdict") == "disputed"):
            continue
        cands = adj.get("candidates") or {}
        entries = [e for c in cands.values() if isinstance(c, list)
                   for e in c if isinstance(e, dict)]
        for key in ("span_original", "text"):  # span 精确匹配优先，text 兜底
            if any(e.get(key) == ent.get(key) for e in entries):
                return i
    return None


def resolve_dispute(work: Path, page_id: str, entity_index: int,
                    verdict: str, correct: dict | None,
                    note: str | None) -> dict:
    """三键裁决写回（对/错/漏），返回操作摘要。

    - 对：实体 verify→user-confirmed；
    - 错：correct 全量覆盖 text/type/span_original/span_normalized（缺一即拒），
      verify→user-corrected；
    - 漏：correct 为新实体（origin="user"、verify="user-confirmed"）追加。
    只关单与被裁决实体关联的那条 disputed 仲裁（candidates 候选 span 优先、
    text 兜底匹配）为 ``user:{verdict}``；无关联 disputed（典型如漏）不碰任何
    仲裁，其余 disputed 条目保持原状等待各自裁决。写前过 validate_pagepack，
    非法 raise ValueError 不落盘；成功后追加 journal 行（含操作前快照）。
    """
    if verdict not in VERDICTS:
        raise ValueError(f"verdict {verdict!r} 非法（须为 {'/'.join(VERDICTS)}）")
    work = Path(work)
    pack = _read_pack(work, page_id)
    entities = pack.get("entities", [])
    if not isinstance(entity_index, int) or not 0 <= entity_index < len(entities):
        raise ValueError(f"entity_index {entity_index!r} 越界"
                         f"（页 {page_id!r} 共 {len(entities)} 条实体）")
    if verdict in ("错", "漏") and not isinstance(correct, dict):
        raise ValueError(f"verdict={verdict!r} 需要 correct 实体字段（text/type/span）")
    if verdict == "错":
        missing = [k for k in ("text", "type", "span_original", "span_normalized")
                   if k not in correct]
        if missing:
            raise ValueError(f"verdict=错 的 correct 缺字段：{missing}"
                             "（text/type/span_original/span_normalized 全必填）")

    snapshot = _snapshot(pack)
    ent = entities[entity_index]
    if verdict == "对":
        ent["verify"] = "user-confirmed"
    elif verdict == "错":
        for key in ("text", "type", "span_original", "span_normalized"):
            ent[key] = copy.deepcopy(correct[key])
        ent["verify"] = "user-corrected"
    else:  # 漏
        new_ent = copy.deepcopy(correct)
        new_ent["origin"] = "user"
        new_ent["verify"] = "user-confirmed"
        new_ent.setdefault("note", note)
        entities.append(new_ent)
    if note is not None and verdict != "漏":
        ent["note"] = note
    # 定向关单（评审 Important#1）：只关与被裁决实体关联的那条 disputed
    adj_idx = None if verdict == "漏" else _linked_disputed_index(pack, ent)
    if adj_idx is not None:
        pack["adjudications"][adj_idx]["verdict"] = f"user:{verdict}"

    _write_pack(work, page_id, pack)  # 校验失败在此 raise，不落任何盘
    line = {"ts": _now(), "op": "resolve", "page_id": page_id,
            "entity_index": entity_index, "verdict": verdict,
            "note": note, "snapshot": snapshot}
    _append_journal(work, line)
    return {"page_id": page_id, "entity_index": entity_index,
            "verdict": verdict, "verify": ent["verify"]}


def undo_last(work: Path) -> dict:
    """撤销最近一次未撤销的操作：从其内嵌快照还原 pack，追加 ``op="undo"`` 行。

    快照还原而非逆运算（控制器裁定 1）；可连续撤销——``undo`` 行本身不可撤，
    每次回溯到 journal 中最近一条**尚未被撤销**的操作行取其快照（日志只追加
    不删除，被撤销操作由 ``undo_of`` 行号标记）。无操作可撤时 raise ValueError。
    """
    work = Path(work)
    lines = _read_journal(work)
    undone = {ln["undo_of"] for ln in lines
              if ln.get("op") == "undo" and isinstance(ln.get("undo_of"), int)}
    pos = next((i for i in range(len(lines) - 1, -1, -1)
                if lines[i].get("op") != "undo" and i not in undone), None)
    if pos is None:
        raise ValueError("无可撤销操作（journal 为空或已全部撤销）")
    last = lines[pos]
    snapshot = last.get("snapshot")
    if not snapshot:
        raise ValueError(f"journal 行 {pos}（op={last.get('op')!r}）无可还原快照")
    page_id = last["page_id"]
    pack = _read_pack(work, page_id)
    pack["entities"] = copy.deepcopy(snapshot["entities"])
    pack["adjudications"] = copy.deepcopy(snapshot["adjudications"])
    _write_pack(work, page_id, pack)
    line = {"ts": _now(), "op": "undo", "page_id": page_id,
            "undo_of": pos, "undo_of_op": last.get("op")}
    _append_journal(work, line)
    return line


def journal_tail(work: Path, n: int = 20) -> list[dict]:
    """journal 末 n 行（时间正序）。"""
    return _read_journal(Path(work))[-n:]


# ---- Task 2: 分层抽样 / 抽检三键 / 可信率 ----------------------------------------

def make_sample(work: Path, ratio: float = 0.1, seed: int | None = None) -> dict:
    """从 verify=="consistent" 实体按 (页型, 类型, verify) 分层抽 ratio。

    每个非空层至少抽 1（层内条数 × ratio 向下取整，<1 则取 1）；层按排序序
    遍历、``random.Random(seed)`` 层内抽样 → 同 seed 同抽样（确定性）。seed 与
    选中清单落盘 ``{work}/sample_seed.json`` 并随返回值给出。selected 元素 =
    ``[page_id, entity_index]``。
    """
    if not 0 < ratio <= 1:
        raise ValueError(f"ratio {ratio!r} 非法（须 0 < ratio <= 1）")
    if seed is None:
        seed = random.SystemRandom().randrange(2 ** 32)
    work = Path(work)
    strata: dict[tuple, list[list]] = {}
    for pid, pack in sorted(load_workspace(work).pages.items()):
        for idx, ent in enumerate(pack.get("entities", [])):
            if ent.get("verify") != "consistent":
                continue
            key = (pack["page_type"], ent.get("type"), ent["verify"])
            strata.setdefault(key, []).append([pid, idx])
    rng = random.Random(seed)
    selected: list[list] = []
    for key in sorted(strata):
        pool = sorted(strata[key])  # 层内确定性顺序
        k = max(1, math.floor(len(pool) * ratio))
        selected.extend(rng.sample(pool, k))
    result = {"seed": seed, "ratio": ratio,
              "strata": {"×".join(map(str, k)): len(v) for k, v in sorted(strata.items())},
              "selected": selected}
    (work / _SAMPLE).write_text(json.dumps(result, ensure_ascii=False, indent=1) + "\n",
                                encoding="utf-8")
    return result


def _load_sample(work: Path) -> dict:
    path = Path(work) / _SAMPLE
    if not path.is_file():
        raise ValueError("尚无抽样（先 make_sample）")
    return json.loads(path.read_text(encoding="utf-8"))


def apply_sample_verdict(work: Path, page_id: str, entity_index: int,
                         ok: bool, correct: dict | None) -> dict:
    """抽检三键：对（ok=True）→ user-confirmed；错 → corrected（同 Task 1 键）。

    仅对已抽样实体可用（不在 sample_seed.json 选中清单即拒绝）；抽检修正后
    返回值携带重算后的 ``trust``。
    """
    sample = _load_sample(work)
    if [page_id, entity_index] not in sample["selected"]:
        raise ValueError(f"实体 ({page_id!r}, {entity_index}) 不在抽样清单中")
    if not ok and not isinstance(correct, dict):
        raise ValueError("ok=False（抽检判错）需要 correct 修正字段")
    verdict = "对" if ok else "错"
    out = resolve_dispute(work, page_id, entity_index, verdict, correct, None)
    out["op"] = "sample-verdict"
    out["ok"] = ok
    out["trust"] = trust_rate(work)  # 抽检修正后重算可信率
    return out


def trust_rate(work: Path) -> dict:
    """一致集抽检可信率 = 抽检中保持原判（user-confirmed）的比例。

    以 ``{work}/sample_seed.json`` 选中清单为口径逐条读当前 verify 状态计数；
    未抽检（checked=0）时 rate=0.0。返回 ``{checked, confirmed, corrected, rate}``。
    """
    sample = _load_sample(work)
    checked = confirmed = corrected = 0
    for pid, idx in sample["selected"]:
        entities = _read_pack(Path(work), pid).get("entities", [])
        if not isinstance(idx, int) or not 0 <= idx < len(entities):
            continue  # 抽样后实体被增删漂移：不计入（漏/撤回场景）
        verify = entities[idx].get("verify")
        checked += 1
        if verify == "user-confirmed":
            confirmed += 1
        elif verify == "user-corrected":
            corrected += 1
    rate = confirmed / checked if checked else 0.0
    return {"checked": checked, "confirmed": confirmed,
            "corrected": corrected, "rate": rate}
