# Issue#56 P1 —— 争议条目与队列状态机 dispute.py 的离线单测。
# 零网络、零真实案卷数据（全部合成 page_id）。
#
# 状态机依据方案 §6.1：pending 三出路（filtered_out / normalized_closed /
# agent_running），agent_done 后走闸门三分（gated_pass / gated_fail→human_pending /
# unmodeled），agent_error 可重试或放弃转 human_pending，human_pending→human_decided。
# 非法迁移必须**拒收并报错**（静默吞掉错误迁移 = 审计链断裂）。
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "eval"))
from gt import dispute as gt_dispute  # noqa: E402


def _mk(**kw):
    base = dict(page_id="sha256:abc:12", rule="R2", entity_type="姓名", key="张三")
    base.update(kw)
    return gt_dispute.new_dispute(**base)


# ---- dispute_id ------------------------------------------------------------

def test_dispute_id_stable_and_sensitive():
    a = gt_dispute.make_dispute_id("p1", "R2", "姓名", "张三")
    b = gt_dispute.make_dispute_id("p1", "R2", "姓名", "张三")
    c = gt_dispute.make_dispute_id("p1", "R2", "姓名", "张四")
    assert a == b and a != c
    assert len(a) == 40  # sha1 hex


# ---- 合法迁移（§6.1 全路径） ------------------------------------------------

def test_legal_paths_full_lifecycle():
    d = _mk()
    q = gt_dispute.DisputeQueue([d])
    q.transition(d["dispute_id"], gt_dispute.STATUS_AGENT_RUNNING)
    q.transition(d["dispute_id"], gt_dispute.STATUS_AGENT_DONE)
    q.transition(d["dispute_id"], gt_dispute.STATUS_GATED_FAIL)
    q.transition(d["dispute_id"], gt_dispute.STATUS_HUMAN_PENDING)
    q.transition(d["dispute_id"], gt_dispute.STATUS_HUMAN_DECIDED)
    assert q.get(d["dispute_id"])["status"] == gt_dispute.STATUS_HUMAN_DECIDED


def test_legal_pending_short_circuits():
    for target in (gt_dispute.STATUS_FILTERED_OUT, gt_dispute.STATUS_NORMALIZED_CLOSED,
                   gt_dispute.STATUS_AGENT_RUNNING):
        d = _mk()
        q = gt_dispute.DisputeQueue([d])
        q.transition(d["dispute_id"], target)
        assert q.get(d["dispute_id"])["status"] == target


def test_legal_agent_error_retry_then_give_up():
    d = _mk()
    q = gt_dispute.DisputeQueue([d])
    q.transition(d["dispute_id"], gt_dispute.STATUS_AGENT_RUNNING)
    q.transition(d["dispute_id"], gt_dispute.STATUS_AGENT_ERROR)
    q.transition(d["dispute_id"], gt_dispute.STATUS_AGENT_RUNNING)  # 重试
    q.transition(d["dispute_id"], gt_dispute.STATUS_AGENT_ERROR)
    q.transition(d["dispute_id"], gt_dispute.STATUS_HUMAN_PENDING)  # 放弃
    assert q.get(d["dispute_id"])["status"] == gt_dispute.STATUS_HUMAN_PENDING


def test_legal_agent_done_gated_pass_and_unmodeled():
    for target in (gt_dispute.STATUS_GATED_PASS, gt_dispute.STATUS_UNMODELED):
        d = _mk()
        q = gt_dispute.DisputeQueue([d])
        q.transition(d["dispute_id"], gt_dispute.STATUS_AGENT_RUNNING)
        q.transition(d["dispute_id"], gt_dispute.STATUS_AGENT_DONE)
        q.transition(d["dispute_id"], target)
        assert q.get(d["dispute_id"])["status"] == target


# ---- 非法迁移（全部拒收） ---------------------------------------------------

@pytest.mark.parametrize("frm,to", [
    # pending 不能跳到软层之后的任何状态
    ("pending", "gated_pass"),
    ("pending", "human_pending"),
    ("pending", "human_decided"),
    ("pending", "agent_done"),
    # agent_done 必须先过闸门三分，不能回头
    ("agent_done", "agent_running"),
    ("agent_done", "normalized_closed"),
    # 终态一律不可再迁
    ("filtered_out", "pending"),
    ("normalized_closed", "agent_running"),
    ("gated_pass", "human_pending"),
    ("unmodeled", "pending"),
    ("human_decided", "human_pending"),
])
def test_illegal_transitions_rejected(frm, to):
    d = _mk()
    d["status"] = frm
    q = gt_dispute.DisputeQueue([d])
    with pytest.raises(gt_dispute.InvalidTransition):
        q.transition(d["dispute_id"], to)
    # 拒收后状态不变
    assert q.get(d["dispute_id"])["status"] == frm


def test_unknown_status_rejected():
    d = _mk()
    q = gt_dispute.DisputeQueue([d])
    with pytest.raises(gt_dispute.InvalidTransition):
        q.transition(d["dispute_id"], "somewhere_else")


# ---- revision 与历史 --------------------------------------------------------

def test_transition_bumps_revision_and_records_history():
    d = _mk()
    q = gt_dispute.DisputeQueue([d])
    rid = d["dispute_id"]
    rev0 = q.get(rid)["revision"]
    q.transition(rid, gt_dispute.STATUS_FILTERED_OUT)
    after = q.get(rid)
    assert after["revision"] == rev0 + 1
    assert after["history"][-1]["from"] == "pending"
    assert after["history"][-1]["to"] == gt_dispute.STATUS_FILTERED_OUT


# ---- JSONL 持久化 -----------------------------------------------------------

def test_jsonl_roundtrip(tmp_path):
    d1 = _mk()
    d2 = _mk(page_id="sha256:def:3", rule="R6", entity_type="日期", key="2023年6月15日")
    q = gt_dispute.DisputeQueue([d1, d2])
    q.transition(d1["dispute_id"], gt_dispute.STATUS_NORMALIZED_CLOSED)
    path = tmp_path / "disputes.jsonl"
    q.save(path)

    q2 = gt_dispute.DisputeQueue.load(path)
    assert q2.get(d1["dispute_id"])["status"] == gt_dispute.STATUS_NORMALIZED_CLOSED
    assert q2.get(d2["dispute_id"])["entity_type"] == "日期"
    assert q2.get(d1["dispute_id"])["history"] == q.get(d1["dispute_id"])["history"]
    assert q2.counts() == q.counts()


def test_queue_views_and_counts():
    d1 = _mk()
    d2 = _mk(page_id="sha256:def:3")
    d3 = _mk(page_id="sha256:789:5")
    q = gt_dispute.DisputeQueue([d1, d2, d3])
    q.transition(d1["dispute_id"], gt_dispute.STATUS_NORMALIZED_CLOSED)
    q.transition(d2["dispute_id"], gt_dispute.STATUS_FILTERED_OUT)
    assert q.pending_items() == [d3]
    assert q.counts() == {"pending": 1, "normalized_closed": 1, "filtered_out": 1}
    assert q.terminal(d1["dispute_id"]) is True
    assert q.terminal(d3["dispute_id"]) is False


def test_duplicate_id_rejected():
    d = _mk()
    with pytest.raises(ValueError):
        gt_dispute.DisputeQueue([d, dict(d)])
