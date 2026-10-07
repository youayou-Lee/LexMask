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

def _pack_p1():  # body 页：2 个 consistent 实体 + 2 条 disputed 仲裁（各挂一条）
    return {
        "page_id": "p1", "page_type": "body",
        "source": {"file_sha256": "0" * 64, "page": 0,
                   "carrier": "text_pdf", "segment": "first"},
        "transcript_gt": {"text": "张三号码110122198110227771",
                          "normalized_text": "张三号码110122198110227771",
                          "fidelity": "machine"},
        "entities": [
            {"text": "张三", "type": "姓名",
             "span_original": [0, 2], "span_normalized": [0, 2],
             "origin": "regex", "verify": "consistent", "note": None},
            {"text": "110122198110227771", "type": "身份证号",
             "span_original": [4, 22], "span_normalized": [4, 22],
             "origin": "regex", "verify": "consistent", "note": None}],
        "adjudications": [
            {"models": ["a", "b"], "rule": "R6", "verdict": "disputed",
             "candidates": {"a": [{"text": "张三", "type": "姓名",
                                   "span_original": [0, 2]}],
                            "b": []}},
            {"models": ["a", "b"], "rule": "R5", "verdict": "disputed",
             "candidates": {"a": [{"text": "110122198110227771", "type": "身份证号",
                                   "span_original": [4, 22]}],
                            "b": [{"text": "110122198110227771", "type": "手机号",
                                   "span_original": [4, 22]}]}}]}


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
    assert stats["consistent"] == 5  # p1 两条 + p2 三条
    assert stats["arbitrated"] == 0


def test_disputes_listing(work):
    ws = workbench.load_workspace(work)
    ds = ws.disputes()
    assert list(ds) == ["p1"]
    assert len(ds["p1"]) == 2
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
    # 漏：无关联 disputed 条目，任何仲裁都不被触碰（评审 Important#1）
    assert all(a["verdict"] == "disputed" for a in pack["adjudications"])


def test_resolve_closes_only_linked_disputed(work):
    # 页上有两条 disputed（各挂一实体）：只裁 A（实体0），B 必须保持 disputed
    workbench.resolve_dispute(work, "p1", 0, "对", None, None)
    adjs = _read_pack(work, "p1")["adjudications"]
    assert adjs[0]["verdict"] == "user:对"  # span 匹配实体 0 → A 关单
    assert adjs[1]["verdict"] == "disputed"  # B 未被人工裁决，不得静默关单
    ds = workbench.load_workspace(work).disputes()
    assert "p1" in ds and len(ds["p1"]) == 1  # disputes() 仍列出 B
    assert ds["p1"][0]["rule"] == "R5"
    # 再裁 B（实体1，text 兜底路径同样只关自己那条）
    workbench.resolve_dispute(work, "p1", 1, "错",
                              {"text": "110122198110227771", "type": "身份证号",
                               "span_original": [4, 22], "span_normalized": [4, 22]},
                              None)
    adjs = _read_pack(work, "p1")["adjudications"]
    assert [a["verdict"] for a in adjs] == ["user:对", "user:错"]
    assert workbench.load_workspace(work).disputes() == {}


def test_resolve_rejects_invalid_and_writes_nothing(work):
    before = copy.deepcopy(_read_pack(work, "p1"))
    journal = work / "journal.jsonl"
    bad = {"text": "越界", "type": "姓名",
           "span_original": [0, 999], "span_normalized": [0, 2]}
    with pytest.raises(ValueError):
        workbench.resolve_dispute(work, "p1", 0, "错", bad, None)
    assert _read_pack(work, "p1") == before  # pack 未被改动
    assert not journal.exists()  # 无 journal 落盘


def test_resolve_correct_requires_full_fields(work):
    # 键=错：correct 四字段（text/type/span_original/span_normalized）全必填（评审 Minor#3）
    before = copy.deepcopy(_read_pack(work, "p1"))
    partial = {"text": "李四", "type": "姓名"}  # 缺两个 span 字段
    with pytest.raises(ValueError):
        workbench.resolve_dispute(work, "p1", 0, "错", partial, None)
    assert _read_pack(work, "p1") == before
    assert not (work / "journal.jsonl").exists()


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


# ---- Task 2: 分层抽样 / 抽检三键 / 可信率 ----------------------------------------

def test_make_sample_deterministic_and_persisted(work):
    s1 = workbench.make_sample(work, ratio=0.1, seed=42)
    s2 = workbench.make_sample(work, ratio=0.1, seed=42)
    assert s1["selected"] == s2["selected"]  # 同 seed 同抽样
    assert s1["seed"] == 42
    assert len(s1["selected"]) >= 1  # 每个非空层至少 1 条
    saved = json.loads((work / "sample_seed.json").read_text(encoding="utf-8"))
    assert saved["seed"] == 42
    assert saved["selected"] == s1["selected"]
    # 只抽 consistent 实体
    for pid, idx in s1["selected"]:
        assert _read_pack(work, pid)["entities"][idx]["verify"] == "consistent"


def test_make_sample_ratio_ge_one_per_stratum(work):
    # 三层：body×姓名、body×身份证号、table×姓名，各至少 1 条
    s = workbench.make_sample(work, ratio=0.1, seed=7)
    keys = set()
    for pid, idx in s["selected"]:
        pack = _read_pack(work, pid)
        ent = pack["entities"][idx]
        keys.add((pack["page_type"], ent["type"], ent["verify"]))
    assert keys == {("body", "姓名", "consistent"),
                    ("body", "身份证号", "consistent"),
                    ("table", "姓名", "consistent")}


def test_apply_sample_verdict_and_trust_rate(work):
    s = workbench.make_sample(work, ratio=0.1, seed=42)
    pid, idx = s["selected"][0]
    workbench.apply_sample_verdict(work, pid, idx, ok=True, correct=None)
    ent = _read_pack(work, pid)["entities"][idx]
    assert ent["verify"] == "user-confirmed"

    tr = workbench.trust_rate(work)
    assert tr["checked"] == len(s["selected"])
    assert tr["confirmed"] == 1
    assert tr["corrected"] == 0
    assert tr["rate"] == pytest.approx(1 / tr["checked"])

    # 抽检错 → corrected + 可信率重算
    pid2, idx2 = s["selected"][-1]
    if (pid2, idx2) == (pid, idx):
        pytest.skip("样本只有一条，无法构造第二键")
    correct = {"text": "改", "type": "姓名",
               "span_original": _read_pack(work, pid2)["entities"][idx2]["span_original"],
               "span_normalized": _read_pack(work, pid2)["entities"][idx2]["span_normalized"]}
    workbench.apply_sample_verdict(work, pid2, idx2, ok=False, correct=correct)
    ent2 = _read_pack(work, pid2)["entities"][idx2]
    assert ent2["verify"] == "user-corrected"
    tr2 = workbench.trust_rate(work)
    assert tr2["checked"] == tr["checked"]
    assert tr2["confirmed"] == 1
    assert tr2["corrected"] == 1
    assert tr2["rate"] == pytest.approx(1 / tr2["checked"])  # 修正后重算（评审 Minor#5）


def test_apply_sample_verdict_rejects_unsampled(work):
    saved = workbench.make_sample(work, ratio=0.1, seed=42)
    selected = {tuple(x) for x in saved["selected"]}
    unsampled = ("p2", 2) if ("p2", 2) not in selected else ("p2", 99)
    with pytest.raises(ValueError):
        workbench.apply_sample_verdict(work, *unsampled, ok=True, correct=None)


def test_apply_sample_verdict_wrong_requires_correct(work):
    s = workbench.make_sample(work, ratio=0.1, seed=42)
    pid, idx = s["selected"][0]
    with pytest.raises(ValueError):
        workbench.apply_sample_verdict(work, pid, idx, ok=False, correct=None)
