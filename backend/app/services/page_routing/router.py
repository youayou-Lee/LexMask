"""Issue#273 Task 7：页级路由编排——``decide`` / ``finalize`` + 决策即落账。

编排序（spec §2.2/§2.4）：

* ``decide(file_id, snapshot, page)``：非 PDF 快照 ⇒ 直接构造 ``is_pdf=False``
  信号走 judge（**判定只走 judge 单出口**）；PDF ⇒ 线程池采集 L1 → 取/建卷态 →
  灰区且缺墨迹时线程池采集 L2 并合并（``dataclasses.replace``）→
  ``judge_page`` 出决策 → **决策即落账**（file_store ``info["page_routing"][str(page)]``）；
* ``finalize(file_id, page, decision, outcome)``：消费仲裁结果（Task 9 产出），
  合成 ``pipeline_status["page_routing"]`` 内容并**改写**账本终值；升档/整卷升级
  计数回写（``record_pass``/``record_switch``/``record_volume_escalation``）
  **只在事件循环内**——:mod:`.state` 无锁设计的前提就是单事件循环，绝不放
  ``to_thread``（线程里只做内容指纹这类纯 IO）。

状态键（挂账要求：换文件同 file_id 时旧态不得复用）::

    f"{file_id}|{path}:{size}:{mtime_ns}:{sha256[:16]}"

内容指纹 = ``size:mtime_ns`` 指纹（复用 ``text_layer_doc_source._file_fingerprint``
的 size:mtime 口径，精度升到纳秒）+ sha256 全文件字节前 16 位，双保险：同秒同
尺寸替换也会因 sha 不同而换新态。content_sha 同文件只算一次（memo key =
``(realpath, st_dev, st_ino, mtime_ns, size)``——**inode 进 key**：os.replace /
rsync -a / cp -p 换入的新文件即使字节数相同、mtime_ns 被还原，inode 也必变 ⇒
memo 必失效重算），并随态挂在 ``VolumeRoutingState._content_sha`` 供 key 重建/
排查复用。

页数来源：``collect_l1`` 不返回 page_count，本模块在内容指纹计算时顺带
``fitz.open`` 取一次总页数（同一 memo 条目），供 ``get_state`` 做确定性抽样。

IO 纪律：fitz/hash/统计全走 ``asyncio.to_thread``；file_store 读改写拿
orchestrator 同款锁（``_get_file_store_lock``，**复用同一入口、不新造存储**）；
落账任何失败只记 warning，绝不影响决策返回。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import threading
from collections import OrderedDict
from dataclasses import replace
from datetime import UTC, datetime

from app.core.config import settings
from app.core.routing_thresholds import get_routing_thresholds, thresholds_fingerprint
from app.services.page_routing import judge as judge_mod
from app.services.page_routing import signals as page_signals
from app.services.page_routing import state as state_mod
from app.services.page_routing.state import VolumeRoutingState
from app.services.page_routing.types import PageDecision, PageSignals, RoutingThresholds

logger = logging.getLogger(__name__)

# ----------------------------------------------------------------- 内容指纹 memo

_FP_MEMO_MAX = 64
_FP_MEMO: OrderedDict[tuple[str, int, int, int, int], tuple[str, int, int, int]] = OrderedDict()
_FP_LOCK = threading.Lock()


def _content_fingerprint(path: str) -> tuple[str, int, int, int] | None:
    """``(sha256_hex, page_count, size, mtime_ns)``；任何失败 ⇒ ``None``。

    memo key = ``(realpath, st_dev, st_ino, mtime_ns, size)``。**inode 必须进
    key**（评审实证）：两份内容不同、字节数相同的 PDF 经 ``os.replace`` 换入并
    ``os.utime`` 还原 mtime_ns（等价 cp -p / rsync -a）后，仅靠 mtime_ns+size
    会让 memo 命中返回旧 sha ⇒ 状态 key 不变 ⇒ 旧卷信任态（upgraded）迁移到
    新内容；换入文件必是新 inode（dev+ino 变化）⇒ memo 必失效重算。signals
    侧 memo 存在同病但后果仅陈旧信号，本任务只堵 router 侧（signals 挂账）。
    ``page_count`` 在同一次 ``fitz.open`` 里顺带取（signals memo 不暴露页数）。
    """
    try:
        st = os.stat(path)
        key = (os.path.realpath(path), st.st_dev, st.st_ino, st.st_mtime_ns, st.st_size)
        with _FP_LOCK:
            hit = _FP_MEMO.get(key)
        if hit is not None:
            return hit
        digest = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                digest.update(chunk)
        page_count = 0
        import fitz  # 惰性：保持本模块顶层无 fitz 依赖（页数仅此处需要）

        with fitz.open(path) as doc:
            page_count = int(doc.page_count)
        value = (digest.hexdigest(), page_count, int(st.st_size), int(st.st_mtime_ns))
        with _FP_LOCK:
            _FP_MEMO[key] = value
            _FP_MEMO.move_to_end(key)
            while len(_FP_MEMO) > _FP_MEMO_MAX:
                _FP_MEMO.popitem(last=False)
        return value
    except Exception:
        return None


def reset_caches() -> None:
    """清空内容指纹 memo（路由态清理由 ``state.reset_registry`` 负责）。"""
    with _FP_LOCK:
        _FP_MEMO.clear()


# --------------------------------------------------------------------- 状态键


def _state_key(file_id: str, path: str, size: int, mtime_ns: int, sha: str) -> str:
    return f"{file_id}|{path}:{size}:{mtime_ns}:{sha[:16]}"


def _state_from_fingerprint(file_id: str, path: str, fp: tuple[str, int, int, int] | None) -> VolumeRoutingState | None:
    """由指纹取/建卷态（**须在事件循环内调用**，get_state 会改注册表）。"""
    if fp is None:
        return None
    sha, page_count, size, mtime_ns = fp
    key = _state_key(file_id, path, size, mtime_ns, sha)
    state = state_mod.get_state(key, total_pages=page_count, content_sha=sha)
    state._content_sha = sha  # 挂账要求：内容指纹随态可查、供 key 重建复用
    return state


def _state_for(file_id: str, path: str) -> VolumeRoutingState | None:
    """同步便捷入口（测试/finalize 复用）：按当前文件内容重建 key 并取态。

    memo 命中时零 IO；memo 失效（LRU 淘汰）时重算一次——确定性不变。
    """
    return _state_from_fingerprint(file_id, path, _content_fingerprint(path))


# ------------------------------------------------------------------ file_store


def _file_store():
    # 复用 orchestrator 的同一入口（不要新造存储）；惰性 import 防循环依赖，
    # 且保证测试对 orch._get_file_store 的 monkeypatch 生效。
    from app.services.redaction_orchestrator import _get_file_store

    return _get_file_store()


def _file_store_lock():
    from app.services.redaction_orchestrator import _get_file_store_lock

    return _get_file_store_lock()


async def _update_page_entry(file_id: str, page: int, updates: dict) -> dict | None:
    """锁内读改写 ``info["page_routing"][str(page)]``；记录不存在 ⇒ ``None``。"""
    async with _file_store_lock():
        store = _file_store()
        info = store.get(file_id)
        if info is None:
            logger.warning("issue273 page_routing: file record missing, skip ledger file=%s", file_id)
            return None
        page_routing = dict(info.get("page_routing") or {})
        entry = dict(page_routing.get(str(page)) or {})
        entry.update(updates)
        page_routing[str(page)] = entry
        info["page_routing"] = page_routing
        store.set(file_id, info)
        return entry


async def _read_info(file_id: str) -> dict | None:
    async with _file_store_lock():
        return _file_store().get(file_id)


# ---------------------------------------------------------------------- decide


def prelim_text_source(action: str) -> str:
    """决策动作 → 预记 text_source：``text_layer*``→text_layer；ocr→ocr；both→both。"""
    if action == "ocr":
        return "ocr"
    if action == "both":
        return "both"
    return "text_layer"  # text_layer / text_layer_verify（未知动作的保守缺省）


def _snapshot_is_pdf(snapshot: dict) -> bool:
    file_type = str(snapshot.get("file_type") or "").lower()
    if "pdf" in file_type:
        return True
    return str(snapshot.get("file_path") or "").lower().endswith(".pdf")


async def decide(file_id: str, snapshot: dict, page: int) -> PageDecision:
    """页级路由决策入口：判定只走 judge 单出口，决策即落账。"""
    snapshot = snapshot or {}
    path = str(snapshot.get("file_path") or "")
    thresholds = get_routing_thresholds()
    version = int(settings.ROUTING_VERSION)

    if not _snapshot_is_pdf(snapshot):
        # 非 PDF（file_type 不含 pdf 且 .pdf 后缀判否）⇒ 直接 judge 出 ocr/not_pdf。
        return await _judge_and_record(file_id, page, PageSignals(page=page, is_pdf=False), thresholds, version)
    if not path:
        # 快照缺 file_path ⇒ 无法采集，fail-closed。
        return await _judge_and_record(file_id, page, PageSignals(page=page, probe_error=True), thresholds, version)

    sig = await asyncio.to_thread(page_signals.collect_l1, path, page)

    state: VolumeRoutingState | None = None
    if sig.is_pdf and not sig.probe_error:
        fp = await asyncio.to_thread(_content_fingerprint, path)
        state = _state_from_fingerprint(file_id, path, fp)

    if judge_mod.needs_l2(sig, thresholds) and sig.ink_alignment is None:
        ink, coverage = await asyncio.to_thread(
            page_signals.collect_l2, path, page, int(settings.ROUTING_L2_RENDER_DPI)
        )
        sig = replace(sig, ink_alignment=ink, image_coverage=coverage)

    return await _judge_and_record(
        file_id,
        page,
        sig,
        thresholds,
        version,
        upgraded=state.upgraded if state is not None else False,
        sampled=state_mod.is_sample_page(state, page) if state is not None else False,
        escalated=state.escalated_to_both if state is not None else False,
    )


async def _judge_and_record(
    file_id: str,
    page: int,
    signals: PageSignals,
    thresholds: RoutingThresholds,
    version: int,
    *,
    upgraded: bool = False,
    sampled: bool = False,
    escalated: bool = False,
) -> PageDecision:
    decision = judge_mod.judge_page(
        signals,
        thresholds,
        routing_version=version,
        upgraded=upgraded,
        sampled=sampled,
        escalated=escalated,
        # A7（终审 I-2）：阈值指纹随决策落账（file_store decision dict +
        # pipeline_status 同源），重放可校验决策所用阈值口径；阈值翻 env 而
        # 不 bump ROUTING_VERSION 时指纹仍变。
        thresholds_fp=thresholds_fingerprint(thresholds),
    )
    entry = {
        "decision": decision.as_dict(),
        "text_source": prelim_text_source(decision.action),
        "text_metrics": {"action": decision.action, "reason": decision.reason},
        "decided_at": datetime.now(UTC).isoformat(),
    }
    try:
        await _update_page_entry(file_id, page, entry)
    except Exception as exc:  # 落账失败不得影响决策返回
        logger.warning("issue273 page_routing ledger write failed file=%s page=%s: %s", file_id, page, exc)
    return decision


# -------------------------------------------------------------------- finalize


async def finalize(file_id: str, page: int, decision: PageDecision, outcome: dict | None) -> dict:
    """消费仲裁结果：合成 ``pipeline_status["page_routing"]`` 内容并改写账本终值。

    调用方（Task 9）契约——``verify_incomplete`` 页不得携带
    ``escalate_volume=True``：对账未实际发生的页不构成整卷升级的依据。router
    不做自卫校验（两字段行为正交：verify_incomplete 本就不计数，见 spec §2.3），
    靠此契约 + 账本审计兜底。
    """
    prelim = prelim_text_source(decision.action)
    decision_dict = decision.as_dict()

    if not outcome:  # 理论不发生（防御）：维持 prelim，不动状态
        result = {
            "decision": decision_dict,
            "outcome": None,
            "text_source": prelim,
            "switched_from": None,
            "switch_reason": None,
            "verify_incomplete": False,
            "stage_ms": None,
        }
        try:
            await _update_page_entry(
                file_id,
                page,
                {
                    "decision": decision_dict,
                    "text_source": prelim,
                    "text_metrics": {"action": decision.action, "reason": decision.reason},
                },
            )
        except Exception as exc:
            logger.warning(
                "issue273 page_routing final ledger write failed file=%s page=%s: %s",
                file_id,
                page,
                exc,
            )
        return result

    outcome = dict(outcome)
    winner = str(outcome.get("winner") or "")
    final_source = "ocr" if winner == "ocr" else prelim

    # —— 升档/升级回写：只在事件循环内（state 无锁设计前提 = 单事件循环）——
    state: VolumeRoutingState | None = None
    try:
        info = await _read_info(file_id)
        path = str((info or {}).get("file_path") or "")
        if path:
            fp = await asyncio.to_thread(_content_fingerprint, path)  # 纯 IO 放线程
            state = _state_from_fingerprint(file_id, path, fp)  # 注册表操作回循环
    except Exception as exc:
        logger.warning("issue273 finalize state lookup failed file=%s page=%s: %s", file_id, page, exc)

    if state is not None:
        arbitration = str(outcome.get("arbitration") or "")
        if arbitration == "pass":
            state_mod.record_pass(state, int(settings.ROUTING_VERIFY_UPGRADE_PASS))
        elif arbitration == "switch":
            state_mod.record_switch(state)
        if outcome.get("escalate_volume"):
            state_mod.record_volume_escalation(state)

    result = {
        "decision": decision_dict,
        "outcome": outcome,
        "text_source": final_source,
        "switched_from": outcome.get("switched_from"),
        "switch_reason": outcome.get("switch_reason"),
        "verify_incomplete": bool(outcome.get("verify_incomplete", False)),
        "stage_ms": outcome.get("stage_ms"),
    }
    try:
        await _update_page_entry(
            file_id,
            page,
            {
                "decision": decision_dict,
                "text_source": final_source,
                "switched_from": result["switched_from"],
                "switch_reason": result["switch_reason"],
                "verify_incomplete": result["verify_incomplete"],
                "stage_ms": result["stage_ms"],
            },
        )
    except Exception as exc:
        logger.warning("issue273 page_routing final ledger write failed file=%s page=%s: %s", file_id, page, exc)
    return result
