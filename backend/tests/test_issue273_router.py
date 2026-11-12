"""Issue#273 Task 7：router 编排（decide/finalize + 决策即落账）测试。

打桩口径沿用 test_mask_mode_gating.py 的 file_store 桩（``_StubStore`` +
monkeypatch ``orch._get_file_store``/``orch._get_file_store_lock``）；合成 PDF
夹具复制 test_issue273_signals.py 的 ``_make_pdf``（中文字符必须用内置 CJK 字体
``china-s`` 写入，helv 无 CJK 字形）。路由态（state registry + 内容指纹 memo）
每例前后复位，防止跨用例串态。
"""

from __future__ import annotations

import asyncio
import os

import fitz
import pytest

from app.core.config import settings
from app.services import redaction_orchestrator as orch
from app.services.page_routing import router as router_mod
from app.services.page_routing import signals as page_signals
from app.services.page_routing import state as rt_state


class _StubStore(dict):
    def set(self, key, value):
        self[key] = value


def _make_pdf(tmp_path, pages: list[list[str]], *, name: str = "synthetic.pdf") -> str:
    path = str(tmp_path / name)
    doc = fitz.open()
    for words in pages:
        page = doc.new_page()
        y = 72
        for w in words:
            page.insert_text((72, y), w, fontsize=12, fontname="china-s")
            y += 20
    doc.save(path)
    return path


# 196 字符（≥120×1.5 灰区带上限）：非灰区、全指标健康 ⇒ default_verify。
_LINES_CLEAR = [
    "中华人民共和国最高人民法院刑事判决书",
    "本院依法组成合议庭公开开庭审理本案",
    "检察机关指派检察员出庭支持公诉意见",
    "被告人及其辩护人均到庭参加诉讼活",
    "经审理查明被告人犯罪事实清楚证据",
    "上述事实有书证物证证人证言予以证",
    "本院认为被告人行为已构成犯罪后果",
    "依照法律规定判决如下驳回上诉维持",
    "如不服本判决可在法定期限内提出上",
    "审判长审判员书记员依法署名于文末",
    "二〇二四年三月十五日判决作出日期",
    "本件与原本核对无异专用章加盖完毕",
]
# 132 字符（∈[120, 180) 灰区带）⇒ needs_l2。
_LINES_GRAY = _LINES_CLEAR[:8]


@pytest.fixture()
def routing_env(monkeypatch):
    """file_store 打桩 + 路由全局态复位 + 抽样关闭（默认页走 default_verify）。"""
    store = _StubStore()
    lock = asyncio.Lock()  # 共享锁实例：与生产「单一 _file_store_lock」同构
    monkeypatch.setattr(orch, "_get_file_store", lambda: store)
    monkeypatch.setattr(orch, "_get_file_store_lock", lambda: lock)
    monkeypatch.setattr(settings, "ROUTING_SAMPLE_PAGES", 0)
    rt_state.reset_registry()
    router_mod.reset_caches()
    yield store
    rt_state.reset_registry()
    router_mod.reset_caches()


# --------------------------------------------------------------------- decide


@pytest.mark.asyncio
async def test_decide_non_pdf_routes_ocr_not_pdf(routing_env, tmp_path):
    store = routing_env
    store["f-1"] = {"file_type": "docx", "file_path": str(tmp_path / "x.docx")}
    decision = await router_mod.decide("f-1", store["f-1"], 1)
    assert decision.action == "ocr"
    assert decision.reason == "not_pdf"
    assert decision.needs_verify is False
    # 决策即落账
    entry = store["f-1"]["page_routing"]["1"]
    assert entry["text_source"] == "ocr"
    assert entry["text_metrics"] == {"action": "ocr", "reason": "not_pdf"}
    assert entry["decision"]["page"] == 1
    assert entry["decided_at"]


@pytest.mark.asyncio
async def test_decide_text_pdf_defaults_to_verify_and_writes_ledger(routing_env, tmp_path):
    store = routing_env
    path = _make_pdf(tmp_path, [_LINES_CLEAR])
    store["f-1"] = {"file_type": "pdf", "file_path": path}
    decision = await router_mod.decide("f-1", store["f-1"], 1)
    assert decision.action == "text_layer_verify"
    assert decision.reason == "default_verify"
    assert decision.needs_verify is True
    assert decision.confidence == 1.0
    entry = store["f-1"]["page_routing"]["1"]
    assert entry["text_source"] == "text_layer"
    assert entry["text_metrics"] == {"action": "text_layer_verify", "reason": "default_verify"}
    assert entry["decision"]["signals"]["chars"] == decision.signals["chars"]
    assert decision.routing_version == int(settings.ROUTING_VERSION)


@pytest.mark.asyncio
async def test_decide_gray_zone_collects_l2_and_fails_closed_without_ink(routing_env, monkeypatch, tmp_path):
    store = routing_env
    calls: list[tuple[str, int, int]] = []

    def fake_l2(path, page, dpi):
        calls.append((path, page, dpi))
        return (None, None)  # L2 采集失败 ⇒ fail-closed 退 ocr

    monkeypatch.setattr(page_signals, "collect_l2", fake_l2)
    path = _make_pdf(tmp_path, [_LINES_GRAY])
    store["f-1"] = {"file_type": "pdf", "file_path": path}
    decision = await router_mod.decide("f-1", store["f-1"], 1)
    assert calls and calls[0] == (path, 1, int(settings.ROUTING_L2_RENDER_DPI))
    assert decision.action == "ocr"
    assert decision.reason == "ink_probe_error"
    assert decision.confidence == 0.0


@pytest.mark.asyncio
async def test_decide_gray_zone_healthy_ink_passes_to_verify(routing_env, monkeypatch, tmp_path):
    store = routing_env
    real_l2 = page_signals.collect_l2

    def spy_l2(path, page, dpi):
        return real_l2(path, page, dpi)

    monkeypatch.setattr(page_signals, "collect_l2", spy_l2)
    path = _make_pdf(tmp_path, [_LINES_GRAY])
    store["f-1"] = {"file_type": "pdf", "file_path": path}
    decision = await router_mod.decide("f-1", store["f-1"], 1)
    assert decision.action == "text_layer_verify"
    assert decision.signals["ink_alignment"] is not None
    assert decision.signals["ink_alignment"] >= float(settings.TEXT_LAYER_MIN_INK_ALIGNMENT)


@pytest.mark.asyncio
async def test_decide_sampled_page_routes_both(routing_env, monkeypatch, tmp_path):
    store = routing_env
    monkeypatch.setattr(settings, "ROUTING_SAMPLE_PAGES", 2)
    path = _make_pdf(tmp_path, [_LINES_CLEAR, _LINES_CLEAR])
    store["f-1"] = {"file_type": "pdf", "file_path": path}
    decision = await router_mod.decide("f-1", store["f-1"], 1)  # 首页必在抽样集
    assert decision.action == "both"
    assert decision.reason == "sampled_both"
    assert decision.needs_verify is True
    entry = store["f-1"]["page_routing"]["1"]
    assert entry["text_source"] == "both"


@pytest.mark.asyncio
async def test_decide_corrupt_pdf_fails_closed(routing_env, tmp_path):
    store = routing_env
    bad = tmp_path / "corrupt.pdf"
    bad.write_bytes(b"%PDF-1.4\nthis is not a pdf at all")
    store["f-1"] = {"file_type": "pdf", "file_path": str(bad)}
    decision = await router_mod.decide("f-1", store["f-1"], 1)
    assert decision.action == "ocr"
    assert decision.reason == "signal_probe_error"
    assert store["f-1"]["page_routing"]["1"]["text_source"] == "ocr"


@pytest.mark.asyncio
async def test_decide_ledger_write_failure_does_not_break_decision(routing_env, monkeypatch):
    class _BoomStore:
        def get(self, file_id):
            return {"file_type": "docx"}

        def set(self, file_id, data):
            raise RuntimeError("disk full")

    monkeypatch.setattr(orch, "_get_file_store", lambda: _BoomStore())
    decision = await router_mod.decide("f-1", {"file_type": "docx"}, 1)
    assert decision.action == "ocr" and decision.reason == "not_pdf"


# ------------------------------------------------------------------- finalize


def _pass_outcome(**overrides) -> dict:
    outcome = {
        "winner": "text_layer",
        "switched_from": None,
        "switch_reason": None,
        "evidence_valid": True,
        "verify_incomplete": False,
        "metrics": {"chars": {"text_layer": 196, "ocr": 180}},
        "stage_ms": 12.5,
        "arbitration": "pass",
        "escalate_volume": False,
    }
    outcome.update(overrides)
    return outcome


@pytest.mark.asyncio
async def test_finalize_switch_to_ocr_rewrites_ledger_and_resets_passes(routing_env, tmp_path):
    store = routing_env
    path = _make_pdf(tmp_path, [_LINES_CLEAR])
    store["f-1"] = {"file_type": "pdf", "file_path": path}
    decision = await router_mod.decide("f-1", store["f-1"], 1)
    assert decision.action == "text_layer_verify"

    outcome = _pass_outcome(
        winner="ocr",
        switched_from="text_layer",
        switch_reason="ocr_evidence_higher_chars",
        arbitration="switch",
    )
    result = await router_mod.finalize("f-1", 1, decision, outcome)
    assert result["text_source"] == "ocr"
    assert result["switched_from"] == "text_layer"
    assert result["switch_reason"] == "ocr_evidence_higher_chars"
    assert result["verify_incomplete"] is False
    assert result["stage_ms"] == 12.5
    assert result["outcome"]["winner"] == "ocr"
    assert result["decision"]["page"] == 1
    # state：record_switch 清零、未升档
    state = router_mod._state_for("f-1", path)
    assert state is not None
    assert state.consecutive_passes == 0
    assert state.upgraded is False
    # 账本终值改写
    entry = store["f-1"]["page_routing"]["1"]
    assert entry["text_source"] == "ocr"
    assert entry["switched_from"] == "text_layer"
    assert entry["switch_reason"] == "ocr_evidence_higher_chars"
    assert entry["verify_incomplete"] is False
    assert entry["stage_ms"] == 12.5
    assert entry["decided_at"]  # decide 阶段时间戳保留


@pytest.mark.asyncio
async def test_finalize_three_passes_upgrades_then_volume_escalates(routing_env, monkeypatch, tmp_path):
    store = routing_env
    monkeypatch.setattr(settings, "ROUTING_VERIFY_UPGRADE_PASS", 3)
    path = _make_pdf(tmp_path, [_LINES_CLEAR])
    store["f-1"] = {"file_type": "pdf", "file_path": path}
    decision = await router_mod.decide("f-1", store["f-1"], 1)

    for _ in range(3):
        result = await router_mod.finalize("f-1", 1, decision, _pass_outcome())
        assert result["text_source"] == "text_layer"  # winner=text_layer ⇒ 维持 prelim
    state = router_mod._state_for("f-1", path)
    assert state.consecutive_passes == 3
    assert state.upgraded is True

    outcome_esc = _pass_outcome(escalate_volume=True)
    await router_mod.finalize("f-1", 1, decision, outcome_esc)
    assert state.escalated_to_both is True

    # 终审 I-1 裁定：整卷升级（spec §2.4）= 无条件语义，组合态（upgraded+escalated）
    # 下升档直信不得遮蔽整卷升级 ⇒ 判定序 escalated 先于 upgraded，页走 both 哨兵。
    upgraded_decision = await router_mod.decide("f-1", store["f-1"], 1)
    assert upgraded_decision.action == "both"
    assert upgraded_decision.reason == "volume_escalated_both"


@pytest.mark.asyncio
async def test_finalize_upgraded_without_escalation_direct_trusts(routing_env, monkeypatch, tmp_path):
    """纯升档（无整卷升级）：未抽样页直接信任文本层（upgraded_after_3_pass）。

    终审 I-1 换序后的对照钉：只有 escalated（而非 upgraded）在判定序里前置，
    单独的 upgraded 直信路径不变。
    """
    store = routing_env
    monkeypatch.setattr(settings, "ROUTING_VERIFY_UPGRADE_PASS", 3)
    path = _make_pdf(tmp_path, [_LINES_CLEAR])
    store["f-1"] = {"file_type": "pdf", "file_path": path}
    decision = await router_mod.decide("f-1", store["f-1"], 1)

    for _ in range(3):
        await router_mod.finalize("f-1", 1, decision, _pass_outcome())
    state = router_mod._state_for("f-1", path)
    assert state.upgraded is True
    assert state.escalated_to_both is False

    upgraded_decision = await router_mod.decide("f-1", store["f-1"], 1)
    assert upgraded_decision.action == "text_layer"
    assert upgraded_decision.reason == "upgraded_after_3_pass"


@pytest.mark.asyncio
async def test_finalize_none_outcome_keeps_prelim(routing_env, tmp_path):
    store = routing_env
    path = _make_pdf(tmp_path, [_LINES_CLEAR])
    store["f-1"] = {"file_type": "pdf", "file_path": path}
    decision = await router_mod.decide("f-1", store["f-1"], 1)
    result = await router_mod.finalize("f-1", 1, decision, None)
    assert result["outcome"] is None
    assert result["text_source"] == "text_layer"
    assert result["switched_from"] is None
    state = router_mod._state_for("f-1", path)
    assert state.consecutive_passes == 0  # 无 outcome ⇒ 不动状态
    assert store["f-1"]["page_routing"]["1"]["text_source"] == "text_layer"


@pytest.mark.asyncio
async def test_finalize_without_state_still_returns_and_writes(routing_env, tmp_path):
    """file 记录缺失（无 file_path 可重建 key）时：防御返回，不抛，且账本终值已写。"""
    store = routing_env
    store["f-1"] = {"file_type": "pdf"}  # 无 file_path
    decision = await router_mod.decide("f-1", store["f-1"], 1)  # 快照缺路径 ⇒ ocr/signal_probe_error
    result = await router_mod.finalize("f-1", 1, decision, _pass_outcome())
    assert result["text_source"] == "ocr"
    # 账本写入断言（与测试名主张对齐）
    entry = store["f-1"]["page_routing"]["1"]
    assert entry["text_source"] == "ocr"
    assert entry["decision"]["reason"] == "signal_probe_error"
    assert entry["switched_from"] is None
    assert entry["switch_reason"] is None
    assert entry["verify_incomplete"] is False
    assert entry["decided_at"]


# --------------------------------------------------------- 状态键 / 内容指纹


@pytest.mark.asyncio
async def test_state_key_changes_when_file_replaced_under_same_file_id(routing_env, tmp_path):
    """挂账要求：换文件同 file_id 时旧态不得复用（内容指纹进 key）。"""
    store = routing_env
    path = _make_pdf(tmp_path, [_LINES_CLEAR])
    store["f-1"] = {"file_type": "pdf", "file_path": path}
    await router_mod.decide("f-1", store["f-1"], 1)
    await router_mod.finalize("f-1", 1, await router_mod.decide("f-1", store["f-1"], 1), _pass_outcome())
    old_state = router_mod._state_for("f-1", path)
    assert old_state.consecutive_passes == 1

    # 同路径换内容：新 PDF 覆盖 + 显式前移 mtime_ns（防同 ns 撞车）
    new_doc = fitz.open()
    page = new_doc.new_page()
    page.insert_text((72, 72), "另一份案卷的正文内容完全不同", fontsize=12, fontname="china-s")
    tmp_new = str(tmp_path / "synthetic.pdf.new")
    new_doc.save(tmp_new)
    new_doc.close()
    os.replace(tmp_new, path)
    st = os.stat(path)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))

    new_state = router_mod._state_for("f-1", path)
    assert new_state is not old_state
    assert new_state.consecutive_passes == 0
    assert getattr(new_state, "_content_sha", "") != getattr(old_state, "_content_sha", "")


def _pad_to_size(path: str, target: int) -> None:
    """EOF 后补尾随字节到目标大小（fitz 容忍 %%EOF 后的尾随注释，实测可解析）。

    PDF 字节数取决于所用字形子集，靠内容凑等长不可靠 ⇒ 显式补齐，保证
    「同尺寸」对抗条件真实成立。
    """
    with open(path, "rb") as fh:
        raw = fh.read()
    assert len(raw) <= target, "target too small"
    with open(path, "wb") as fh:
        fh.write(raw + b"\n%" + b"P" * (target - len(raw) - 2))


@pytest.mark.asyncio
async def test_fingerprint_memo_recomputes_on_same_mtime_replacement(routing_env, tmp_path):
    """评审 Important 1：同尺寸同 mtime_ns 替换（os.replace 新 inode + utime 还原，
    等价 cp -p / rsync -a）不得让 memo 命中旧 sha——inode 进 memo key，sha 必重算、
    状态 key 必变化，旧卷信任态不得迁移到新内容。"""
    store = routing_env
    path = _make_pdf(tmp_path, [_LINES_CLEAR])
    _pad_to_size(path, 8192)
    store["f-1"] = {"file_type": "pdf", "file_path": path}
    await router_mod.decide("f-1", store["f-1"], 1)
    old_fp = router_mod._content_fingerprint(path)
    old_state = router_mod._state_for("f-1", path)
    await router_mod.finalize("f-1", 1, await router_mod.decide("f-1", store["f-1"], 1), _pass_outcome())
    assert old_state.consecutive_passes == 1

    # 同 mtime_ns 同字节数换内容：os.replace 换入不同内容的 PDF，再还原 mtime_ns。
    old_stat = os.stat(path)
    new_doc = fitz.open()
    new_page = new_doc.new_page()
    new_page.insert_text((72, 72), "换入卷宗的正文与原卷完全不同", fontsize=12, fontname="china-s")
    tmp_new = str(tmp_path / "synthetic.pdf.new")
    new_doc.save(tmp_new)
    new_doc.close()
    _pad_to_size(tmp_new, 8192)  # 与原文件字节数相同（对抗条件）
    os.replace(tmp_new, path)
    os.utime(path, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
    st = os.stat(path)
    assert (st.st_dev, st.st_ino) != (old_stat.st_dev, old_stat.st_ino)  # 新 inode
    assert st.st_mtime_ns == old_stat.st_mtime_ns  # mtime_ns 被还原（对抗条件成立）
    assert st.st_size == old_stat.st_size  # 字节数相同（对抗条件成立）

    new_fp = router_mod._content_fingerprint(path)
    assert new_fp is not None
    assert new_fp[0] != old_fp[0]  # sha 重算，未命中旧 memo
    new_state = router_mod._state_for("f-1", path)
    assert new_state is not old_state  # 状态 key 变化，旧信任态不迁移
    assert new_state.consecutive_passes == 0
    assert getattr(new_state, "_content_sha", "") == new_fp[0]


@pytest.mark.asyncio
async def test_finalize_verify_incomplete_does_not_count_pass(routing_env, tmp_path):
    """spec §2.3 硬约束：verify_incomplete 页（对账未实际发生）不得计入升档连击。"""
    store = routing_env
    path = _make_pdf(tmp_path, [_LINES_CLEAR])
    store["f-1"] = {"file_type": "pdf", "file_path": path}
    decision = await router_mod.decide("f-1", store["f-1"], 1)
    outcome = _pass_outcome(winner="text_layer", verify_incomplete=True)
    outcome.pop("arbitration")  # 甚至缺 arbitration 键（评审指定形态）
    result = await router_mod.finalize("f-1", 1, decision, outcome)
    assert result["verify_incomplete"] is True
    assert result["text_source"] == "text_layer"
    state = router_mod._state_for("f-1", path)
    assert state.consecutive_passes == 0
    assert state.upgraded is False


def test_prelim_text_source_mapping():
    assert router_mod.prelim_text_source("text_layer") == "text_layer"
    assert router_mod.prelim_text_source("text_layer_verify") == "text_layer"
    assert router_mod.prelim_text_source("ocr") == "ocr"
    assert router_mod.prelim_text_source("both") == "both"
