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


# ---- 终审修复波（2026-10-07）：真实引擎形状（结构抄 arbitrate.py/pagepack.py，
#      合成占位值）——R6 gap 条目/0 实体页、R3 被否读数不在 entities ---------------

def _pack_gap():  # R6 整页升级真实形状：0 实体 + gap 条目（candidates=detail 字符串）
    return {
        "page_id": "pg", "page_type": "body",
        "source": {"file_sha256": "3" * 64, "page": 3,
                   "carrier": "scanned", "segment": "first"},
        "transcript_gt": {"text": "甲乙丙丁", "normalized_text": "甲乙丙丁",
                          "fidelity": "machine"},
        "entities": [],
        "adjudications": [
            {"models": ["v6", "vl"], "rule": "R6", "verdict": "disputed",
             "candidates": {"detail": ["单侧多出：v6 面读数「戊」、VL 面无"]},
             "gap": "单方多字/集合不合，整页升级（R6）"},
            {"models": ["v6", "vl"], "rule": "R6", "verdict": "disputed",
             "candidates": {"a": [{"text": "戊", "type": "姓名",
                                   "span_original": [3, 4]}],
                            "b": [], "md": []}}]}


def _pack_r3():  # R3 采 VL 面真实形状：采信读数入库，被否 a 读数留痕、不在 entities
    return {
        "page_id": "pr", "page_type": "table",
        "source": {"file_sha256": "4" * 64, "page": 4,
                   "carrier": "text_pdf", "segment": "first"},
        "transcript_gt": {"text": "甲乙丙丁", "normalized_text": "甲乙丙丁",
                          "fidelity": "machine"},
        "entities": [
            {"text": "甲乙", "type": "姓名",
             "span_original": [0, 2], "span_normalized": [0, 2],
             "origin": "regex", "verify": "consistent", "note": None},
            {"text": "丙", "type": "姓名",
             "span_original": [2, 3], "span_normalized": [2, 3],
             "origin": "regex", "verify": "arbitrated", "arbitration": "R3",
             "note": None}],
        "adjudications": [
            {"models": ["v6", "vl"], "rule": "R1", "verdict": "consistent"},
            {"models": ["v6", "vl", "vl-md"], "rule": "R3", "verdict": "auto:b"},
            {"models": ["v6", "vl", "vl-md"], "rule": "R3", "verdict": "disputed",
             "candidates": {"a": [{"text": "戊", "type": "姓名",
                                   "span_original": [3, 4]}],
                            "b": [], "md": []}}]}


@pytest.fixture()
def work_real(tmp_path):
    for pid, pack in (("pg", _pack_gap()), ("pr", _pack_r3())):
        d = tmp_path / "pages" / pid
        d.mkdir(parents=True)
        (d / "pack.json").write_text(json.dumps(pack, ensure_ascii=False),
                                     encoding="utf-8")
    return tmp_path


def test_resolve_ack_closes_gap_dispute_without_touching_entities(work_real):
    out = workbench.resolve_dispute(work_real, "pg", None, "ack", None, None,
                                    adjudication_index=0)
    assert out["verdict"] == "ack"
    assert out["verify"] is None  # ack 不碰实体 → 无 verify 语义
    assert out["adjudication_index"] == 0
    pack = _read_pack(work_real, "pg")
    assert pack["entities"] == []  # 一根毫毛都没动
    assert pack["adjudications"][0]["verdict"] == "user:ack"
    assert pack["adjudications"][1]["verdict"] == "disputed"  # 其余条目不受牵连


def test_resolve_ack_requires_explicit_adjudication_index(work_real):
    before = copy.deepcopy(_read_pack(work_real, "pg"))
    with pytest.raises(ValueError):
        workbench.resolve_dispute(work_real, "pg", None, "ack", None, None)
    assert _read_pack(work_real, "pg") == before
    assert not (work_real / "journal.jsonl").exists()


def test_resolve_ack_on_rejected_r3_reading(work_real):
    out = workbench.resolve_dispute(work_real, "pr", None, "ack", None, None,
                                    adjudication_index=2)
    pack = _read_pack(work_real, "pr")
    assert pack["adjudications"][2]["verdict"] == "user:ack"
    assert len(pack["entities"]) == 2  # 无新实体
    assert [e["verify"] for e in pack["entities"]] == ["consistent", "arbitrated"]


def test_resolve_miss_on_zero_entity_page_closes_dispute(work_real):
    correct = {"text": "戊", "type": "姓名",
               "span_original": [3, 4], "span_normalized": [3, 4]}
    out = workbench.resolve_dispute(work_real, "pg", None, "漏", correct, None,
                                    adjudication_index=1)
    assert out["verify"] == "user-confirmed"  # M1：返回追加实体的 verify
    assert out["entity_index"] is None
    assert out["adjudication_index"] == 1
    pack = _read_pack(work_real, "pg")
    assert len(pack["entities"]) == 1
    assert pack["entities"][0]["origin"] == "user"
    assert pack["entities"][0]["verify"] == "user-confirmed"
    assert pack["adjudications"][1]["verdict"] == "user:漏"


def test_resolve_miss_without_index_keeps_disputes_disputed(work_real):
    # 旧客户端兼容路径：漏不带 adjudication_index → 只补录、不关单（旧行为）
    correct = {"text": "戊", "type": "姓名",
               "span_original": [3, 4], "span_normalized": [3, 4]}
    workbench.resolve_dispute(work_real, "pr", None, "漏", correct, None)
    pack = _read_pack(work_real, "pr")
    assert len(pack["entities"]) == 3
    assert [a["verdict"] for a in pack["adjudications"]] == \
        ["consistent", "auto:b", "disputed"]


def test_resolve_correct_creates_entity_when_none_exists(work_real):
    # 采纳被否读数：错 + entity_index=null → 新实体 user-corrected + 显式关单
    correct = {"text": "戊", "type": "姓名",
               "span_original": [3, 4], "span_normalized": [3, 4]}
    out = workbench.resolve_dispute(work_real, "pr", None, "错", correct, "采信 a 读数",
                                    adjudication_index=2)
    assert out["verify"] == "user-corrected"
    assert out["entity_index"] is None
    pack = _read_pack(work_real, "pr")
    assert len(pack["entities"]) == 3
    new = pack["entities"][-1]
    assert new["text"] == "戊" and new["origin"] == "user"
    assert new["verify"] == "user-corrected" and new["note"] == "采信 a 读数"
    assert pack["adjudications"][2]["verdict"] == "user:错"


def test_resolve_explicit_index_skips_candidate_matching(work_real):
    # 显式关单不做候选匹配：pr#2 的候选（戊）与实体0（甲乙）毫无交集，仍照关
    out = workbench.resolve_dispute(work_real, "pr", 0, "对", None, None,
                                    adjudication_index=2)
    assert out["adjudication_index"] == 2
    pack = _read_pack(work_real, "pr")
    assert pack["entities"][0]["verify"] == "user-confirmed"
    assert pack["adjudications"][2]["verdict"] == "user:对"


def test_resolve_explicit_index_rejects_bad_target(work_real):
    with pytest.raises(ValueError):  # 越界
        workbench.resolve_dispute(work_real, "pr", 0, "对", None, None,
                                  adjudication_index=9)
    with pytest.raises(ValueError):  # 非 disputed 条目不可重裁
        workbench.resolve_dispute(work_real, "pr", 0, "对", None, None,
                                  adjudication_index=0)
    assert not (work_real / "journal.jsonl").exists()


def test_resolve_ack_undo_restores_dispute(work_real):
    workbench.resolve_dispute(work_real, "pg", None, "ack", None, None,
                              adjudication_index=0)
    assert _read_pack(work_real, "pg")["adjudications"][0]["verdict"] == "user:ack"
    workbench.undo_last(work_real)
    pack = _read_pack(work_real, "pg")
    assert pack["adjudications"][0]["verdict"] == "disputed"
    assert pack["entities"] == []


def test_resolve_warns_on_span_transcript_mismatch(work_real):
    # I2：span 切出的转录文本 ≠ 提交原文（OCR 变体修正合法）→ 非致命 warnings
    correct = {"text": "戊", "type": "姓名",
               "span_original": [3, 4], "span_normalized": [3, 4]}  # 转录该处为「丁」
    out = workbench.resolve_dispute(work_real, "pg", None, "漏", correct, None,
                                    adjudication_index=1)
    assert any("不一致" in w for w in out.get("warnings", []))
    # 定位命中时无 warnings 键（错改 pr#0：文本未变、span 切出即原文）
    out2 = workbench.resolve_dispute(
        work_real, "pr", 0, "错",
        {"text": "甲乙", "type": "姓名",
         "span_original": [0, 2], "span_normalized": [0, 2]}, None)
    assert "warnings" not in out2


def test_resolve_miss_ignores_stale_entity_index(work_real):
    # 漏不使用 entity_index：旧客户端兜底值（0）在 0 实体页不再炸越界
    correct = {"text": "戊", "type": "姓名",
               "span_original": [3, 4], "span_normalized": [3, 4]}
    out = workbench.resolve_dispute(work_real, "pg", 0, "漏", correct, None)
    assert out["entity_index"] is None
    assert len(_read_pack(work_real, "pg")["entities"]) == 1
