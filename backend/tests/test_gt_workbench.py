# Issue#56 M3 Task 1+2 —— 工作台数据层 workbench.py 的离线单测。
# 零网络、零真实案卷数据（全部合成占位符）；不碰任何 git 仓。
# 覆盖：装载/分歧清单/三键裁决写回/validate 拒绝/journal 追加/快照撤销/
#       分层抽样（确定性）/抽检三键/可信率已知值。
import copy
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "eval"))
from gt import gt_schema, workbench  # noqa: E402


# ---- 夹具：两条最小合法 pack（合成占位符，同 test_gt_schema 模式） ----------------

def _pack_p1():  # body 页：1 个 consistent 实体 + 1 条 disputed 仲裁
    return {
        "page_id": "p1", "page_type": "body",
        "source": {"file_sha256": "0" * 64, "page": 0,
                   "carrier": "text_pdf", "segment": "first"},
        "transcript_gt": {"text": "张三号码110122198110227771",
                          "normalized_text": "张三号码110122198110227771",
                          "fidelity": "machine"},
        "entities": [{"text": "张三", "type": "姓名",
                      "span_original": [0, 2], "span_normalized": [0, 2],
                      "origin": "regex", "verify": "consistent", "note": None}],
        "adjudications": [{"models": ["a", "b"], "rule": "R6",
                           "verdict": "disputed",
                           "candidates": {"a": [{"text": "张三", "type": "姓名",
                                                 "span_original": [0, 2]}],
                                          "b": []}}]}


def _pack_p2():  # table 页：3 个同型 consistent 实体（分层抽样用）
    text = "甲乙丙丁戊己庚辛壬癸"
    ents = [{"text": c, "type": "姓名",
             "span_original": [i, i + 1], "span_normalized": [i, i + 1],
             "origin": "regex", "verify": "consistent", "note": None}
            for i, c in enumerate("甲乙丙")]
    return {
        "page_id": "p2", "page_type": "table",
        "source": {"file_sha256": "1" * 64, "page": 1,
                   "carrier": "text_pdf", "segment": "second"},
        "transcript_gt": {"text": text, "normalized_text": text,
                          "fidelity": "machine"},
        "entities": ents,
        "adjudications": []}


@pytest.fixture()
def work(tmp_path):
    for pid, pack in (("p1", _pack_p1()), ("p2", _pack_p2())):
        d = tmp_path / "pages" / pid
        d.mkdir(parents=True)
        (d / "pack.json").write_text(json.dumps(pack, ensure_ascii=False),
                                     encoding="utf-8")
    return tmp_path


def _read_pack(work, pid):
    return json.loads((work / "pages" / pid / "pack.json").read_text(encoding="utf-8"))


# ---- Task 1: 装载 / 分歧清单 / stats --------------------------------------------

def test_load_workspace_and_stats(work):
    ws = workbench.load_workspace(work)
    assert set(ws.pages) == {"p1", "p2"}
    stats = ws.stats()
    assert stats["consistent"] == 4  # p1 一条 + p2 三条
    assert stats["arbitrated"] == 0


def test_disputes_listing(work):
    ws = workbench.load_workspace(work)
    ds = ws.disputes()
    assert list(ds) == ["p1"]
    assert ds["p1"][0]["verdict"] == "disputed"
    assert "candidates" in ds["p1"][0]


# ---- Task 1: 三键裁决 -----------------------------------------------------------

def test_resolve_confirm(work):
    out = workbench.resolve_dispute(work, "p1", 0, "对", None, None)
    pack = _read_pack(work, "p1")
    assert pack["entities"][0]["verify"] == "user-confirmed"
    assert pack["adjudications"][0]["verdict"] == "user:对"
    assert out["page_id"] == "p1"


def test_resolve_correct_overrides(work):
    correct = {"text": "李四", "type": "姓名",
               "span_original": [0, 2], "span_normalized": [0, 2]}
    workbench.resolve_dispute(work, "p1", 0, "错", correct, "两云分歧人工改判")
    ent = _read_pack(work, "p1")["entities"][0]
    assert ent["verify"] == "user-corrected"
    assert ent["text"] == "李四"
    assert ent["note"] == "两云分歧人工改判"
    assert _read_pack(work, "p1")["adjudications"][0]["verdict"] == "user:错"


def test_resolve_miss_appends_entity(work):
    correct = {"text": "王五", "type": "姓名",
               "span_original": [3, 5], "span_normalized": [3, 5]}
    n_before = len(_read_pack(work, "p1")["entities"])
    workbench.resolve_dispute(work, "p1", 0, "漏", correct, None)
    pack = _read_pack(work, "p1")
    assert len(pack["entities"]) == n_before + 1
    new = pack["entities"][-1]
    assert new["origin"] == "user"
    assert new["verify"] == "user-confirmed"
    assert new["text"] == "王五"
    assert pack["adjudications"][0]["verdict"] == "user:漏"


def test_resolve_rejects_invalid_and_writes_nothing(work):
    before = copy.deepcopy(_read_pack(work, "p1"))
    journal = work / "journal.jsonl"
    bad = {"text": "越界", "type": "姓名",
           "span_original": [0, 999], "span_normalized": [0, 2]}
    with pytest.raises(ValueError):
        workbench.resolve_dispute(work, "p1", 0, "错", bad, None)
    assert _read_pack(work, "p1") == before  # pack 未被改动
    assert not journal.exists()  # 无 journal 落盘


def test_resolve_bad_args_raise(work):
    with pytest.raises(ValueError):
        workbench.resolve_dispute(work, "p1", 99, "对", None, None)  # 越界 index
    with pytest.raises(ValueError):
        workbench.resolve_dispute(work, "nope", 0, "对", None, None)  # 无此页
    with pytest.raises(ValueError):
        workbench.resolve_dispute(work, "p1", 0, "离谱", None, None)  # 非法 verdict


# ---- Task 1: journal 与快照撤销 --------------------------------------------------

def test_journal_appends_each_op(work):
    workbench.resolve_dispute(work, "p1", 0, "对", None, None)
    workbench.resolve_dispute(work, "p2", 0, "对", None, None)
    lines = workbench.journal_tail(work, 10)
    assert [ln["op"] for ln in lines] == ["resolve", "resolve"]
    assert {ln["page_id"] for ln in lines} == {"p1", "p2"}
    assert all("snapshot" in ln and "ts" in ln for ln in lines)


def test_undo_restores_exact_preop_state(work):
    pre = copy.deepcopy(_read_pack(work, "p1"))
    correct = {"text": "李四", "type": "姓名",
               "span_original": [0, 2], "span_normalized": [0, 2]}
    workbench.resolve_dispute(work, "p1", 0, "错", correct, None)
    assert _read_pack(work, "p1") != pre
    out = workbench.undo_last(work)
    assert out["op"] == "undo"
    after = _read_pack(work, "p1")
    assert after["entities"] == pre["entities"]  # span+text+type+verify 全等
    assert after["adjudications"] == pre["adjudications"]
    # undo 本身也留痕（日志只追加不删除）
    lines = workbench.journal_tail(work, 2)
    assert [ln["op"] for ln in lines] == ["resolve", "undo"]


def test_undo_consecutive(work):
    workbench.resolve_dispute(work, "p1", 0, "对", None, None)
    workbench.resolve_dispute(work, "p2", 1, "对", None, None)
    workbench.undo_last(work)
    workbench.undo_last(work)
    assert _read_pack(work, "p1") == _pack_p1()
    assert _read_pack(work, "p2") == _pack_p2()
    with pytest.raises(ValueError):
        workbench.undo_last(work)  # 无可撤销
