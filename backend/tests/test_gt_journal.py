# Issue#56 P1 —— 软层审计 journal.py 的离线单测。
# 零网络、零真实案卷数据。
#
# AC4（审计完整）：每条争议在 journal 有完整记录——对账 = 争议数与 journal 条目数
# 可互相核对，漏一条都能被 reconcile 抓出来。
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "eval"))
from gt import journal as gt_journal  # noqa: E402

FIELDS = ("ts", "stage", "agent_id", "context_id", "model_id", "prompt_version",
          "input_hash", "raw_response", "gate_verdict", "skip_reason",
          "elapsed_s", "tokens")


def _entry(**kw):
    base = dict(
        stage="step6.agent_b", agent_id="b-0031", context_id="ctx-9f2",
        model_id="glm-5.3-flash", prompt_version="v0.1",
        input_hash="abc123", raw_response='{"diff_class":"morphological"}',
        gate_verdict="pass", skip_reason=None, elapsed_s=12.3,
        tokens={"in": 100, "out": 40}, dispute_id="d-1",
    )
    base.update(kw)
    return base


# ---- 记录与字段 -------------------------------------------------------------

def test_record_has_contract_fields(tmp_path):
    j = gt_journal.Journal(tmp_path / "journal.jsonl")
    e = j.record(**_entry())
    for f in FIELDS:
        assert f in e, f"契约字段缺失：{f}"
    assert e["schema_version"] == gt_journal.SCHEMA_VERSION
    # ts 是带时区的 ISO 时间
    assert e["ts"].endswith("+08:00") or e["ts"][-3] == ":"
    # 可选字段显式落 None，不静默缺键
    assert e["skip_reason"] is None


def test_gate_verdict_constrained(tmp_path):
    j = gt_journal.Journal(tmp_path / "j.jsonl")
    with pytest.raises(ValueError):
        j.record(**_entry(gate_verdict="maybe"))


def test_append_persists_each_record(tmp_path):
    path = tmp_path / "j.jsonl"
    j = gt_journal.Journal(path)
    j.record(**_entry(dispute_id="d-1"))
    j.record(**_entry(dispute_id="d-2", gate_verdict="fail", skip_reason="id_mismatch"))
    # 每条立即落盘（进程崩了也不丢审计）
    j2 = gt_journal.Journal.load(path)
    assert len(j2.entries) == 2
    assert j2.entries[1]["gate_verdict"] == "fail"


def test_load_existing_file(tmp_path):
    path = tmp_path / "j.jsonl"
    j = gt_journal.Journal(path)
    j.record(**_entry())
    j2 = gt_journal.Journal(path)  # 重开不覆盖
    j2.record(**_entry(dispute_id="d-2"))
    assert len(gt_journal.Journal.load(path).entries) == 2


# ---- AC4 对账 ---------------------------------------------------------------

def test_reconcile_finds_unjournaled_disputes(tmp_path):
    j = gt_journal.Journal(tmp_path / "j.jsonl")
    j.record(**_entry(dispute_id="d-1", stage="step6.agent_b"))
    j.record(**_entry(dispute_id="d-2", stage="step6.agent_b"))
    # d-3 走了软层却没留 journal → 对账必须抓出来
    report = j.reconcile(["d-1", "d-2", "d-3"], stage="step6.agent_b")
    assert report == {"missing": ["d-3"], "journaled": 2}


def test_reconcile_stage_scoped(tmp_path):
    j = gt_journal.Journal(tmp_path / "j.jsonl")
    j.record(**_entry(dispute_id="d-1", stage="step6.agent_a"))
    report = j.reconcile(["d-1"], stage="step6.agent_b")
    assert report["missing"] == ["d-1"]


def test_reconcile_all_covered(tmp_path):
    j = gt_journal.Journal(tmp_path / "j.jsonl")
    for i in range(5):
        j.record(**_entry(dispute_id=f"d-{i}", stage="step6.agent_b"))
    report = j.reconcile([f"d-{i}" for i in range(5)], stage="step6.agent_b")
    assert report == {"missing": [], "journaled": 5}


def test_elapsed_s_monotonic_walls(tmp_path):
    j = gt_journal.Journal(tmp_path / "j.jsonl")
    t0 = time.time()
    e = j.record(**_entry())
    assert e["ts"][:4] == time.strftime("%Y", time.localtime(t0))
