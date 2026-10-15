"""eval-ng gates 单测：数字闸 / 零泄漏闸新旧同判（AC3） / 鲁棒性 / 基线三闸门透传。"""

import importlib.util
import sys
from pathlib import Path

import evalng_bootstrap as boot

REPO = boot.REPO

digital = boot.load("engine/gates/digital_veto.py")
leak = boot.load("engine/gates/leak_free.py")
baseline = boot.load("engine/gates/baseline_gate.py")
robust = boot.load("engine/gates/robustness.py")
protocol = boot.load("engine/protocol.py")


def _load_old_leak_check():
    path = REPO / "eval" / "scripts" / "leak_check.py"
    spec = importlib.util.spec_from_file_location("evalng_old_leak_check", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["evalng_old_leak_check"] = mod
    spec.loader.exec_module(mod)
    return mod


def _ans(form, **kw):
    return protocol.Answer(form=form, **kw)


def _item(form, payload=None, gt=None):
    return protocol.ExamItem(id="p1", exam_id="ex", form=form, payload=payload or {}, gt=gt)


# ---------- digital_veto ----------

def test_digital_veto_skip_non_entity_form():
    r = digital.gate(_ans(protocol.AnswerForm.PERF_ONLY), _item(protocol.AnswerForm.PERF_ONLY), {})
    assert r.passed is True and "skipped" in r.details


def test_digital_veto_fail_on_wrong_id():
    gt = {"身份证号": ["110101199001010011"]}
    pred = {"身份证号": ["110101199001010012"]}
    r = digital.gate(_ans(protocol.AnswerForm.ENTITY_TABLE, entities=pred),
                     _item(protocol.AnswerForm.ENTITY_TABLE, gt=gt), {})
    assert r.passed is False and r.details["failures"]


def test_digital_veto_pass_on_exact():
    gt = {"身份证号": ["110101199001010011"]}
    r = digital.gate(_ans(protocol.AnswerForm.ENTITY_TABLE, entities=dict(gt)),
                     _item(protocol.AnswerForm.ENTITY_TABLE, gt=gt), {})
    assert r.passed is True


# ---------- leak_free：与旧 leak_check 同判 ----------

def _make_case(tmp_path, artifact_text, include_original=True):
    mapping = tmp_path / "mapping.csv"
    mapping.write_text(
        "原文,类型,化名\n李四,姓名,某人1\n110101199001010011,身份证号,某某身份证号\n",
        encoding="utf-8",
    )
    artifact = tmp_path / "out.txt"
    artifact.write_text(artifact_text, encoding="utf-8")
    return mapping, artifact


def test_leak_free_same_verdict_as_old_check(tmp_path):
    old = _load_old_leak_check()
    clean_text = "某人1持某某身份证号办理业务。"
    dirty_text = "李四持110101199001010011办理业务。"
    for text, expect_pass in ((clean_text, True), (dirty_text, False)):
        mapping, artifact = _make_case(tmp_path, text)
        item = _item(protocol.AnswerForm.ARTIFACT_FILE,
                     payload={"path": str(artifact),
                              "sidecars": {"mapping_csv": str(mapping)}})
        ans = _ans(protocol.AnswerForm.ARTIFACT_FILE, artifact_path=str(artifact))
        new_r = leak.gate(ans, item, {})
        old_r = old.run_check(Path(artifact), old.load_mapping(Path(mapping)))
        assert new_r.passed == expect_pass, text
        assert old_r["clean"] == expect_pass, text  # 新旧同判（AC3 本地部分）


def test_leak_free_normalized_match(tmp_path):
    # 强归一化：加空格/标点的残留也要抓到（与旧 _norm 同语义）
    _, artifact = _make_case(tmp_path, "李  四 持 1101-0101-9900…无")
    mapping, artifact = _make_case(tmp_path, "李　四（持）110101199001010011。")
    item = _item(protocol.AnswerForm.ARTIFACT_FILE,
                 payload={"path": str(artifact), "sidecars": {"mapping_csv": str(mapping)}})
    r = leak.gate(_ans(protocol.AnswerForm.ARTIFACT_FILE, artifact_path=str(artifact)), item, {})
    assert r.passed is False and r.details["findings"]


def test_leak_free_missing_mapping_fails(tmp_path):
    artifact = tmp_path / "out.txt"
    artifact.write_text("clean", encoding="utf-8")
    item = _item(protocol.AnswerForm.ARTIFACT_FILE, payload={"path": str(artifact)})
    r = leak.gate(_ans(protocol.AnswerForm.ARTIFACT_FILE, artifact_path=str(artifact)), item, {})
    assert r.passed is False and "reason" in r.details


# ---------- robustness ----------

def test_robustness_expect_reject_pass():
    item = _item(protocol.AnswerForm.ROBUSTNESS, payload={"path": "enc.pdf", "expect_reject": True})
    r = robust.gate(_ans(protocol.AnswerForm.ROBUSTNESS, rejected=True), item, {})
    assert r.passed is True


def test_robustness_expect_reject_but_accepted_fails():
    item = _item(protocol.AnswerForm.ROBUSTNESS, payload={"path": "enc.pdf", "expect_reject": True})
    r = robust.gate(_ans(protocol.AnswerForm.ROBUSTNESS, rejected=False), item, {})
    assert r.passed is False


def test_robustness_wrong_form_fails():
    item = _item(protocol.AnswerForm.ROBUSTNESS, payload={"path": "enc.pdf"})
    r = robust.gate(_ans(protocol.AnswerForm.ENTITY_TABLE, entities={}), item, {})
    assert r.passed is False


# ---------- baseline_gate ----------

def _metrics(f1, p, r):
    return {"overall": {"f1": f1, "precision": p, "recall": r},
            "digital": {}}


def test_baseline_gate_pass_and_env_mismatch():
    cand = _metrics(0.7, 0.8, 0.70)
    base = _metrics(0.7, 0.75, 0.66)
    env = {"candidate_metrics": cand, "baseline_metrics": base, "env_label": "scnet"}
    base["env_label"] = "other-box"
    r = baseline.gate(_ans(protocol.AnswerForm.ENTITY_TABLE, entities={}),
                      _item(protocol.AnswerForm.ENTITY_TABLE), env)
    # R 0.65 ≥ 0.66-1pp、P 0.8 ≥ 0.75 → 闸门过，但环境不一致要标记
    assert r.passed is True and r.details.get("env_mismatch")


def test_baseline_gate_recall_drop_fails():
    cand = _metrics(0.7, 0.8, 0.60)
    base = _metrics(0.7, 0.75, 0.66)
    env = {"candidate_metrics": cand, "baseline_metrics": base}
    r = baseline.gate(_ans(protocol.AnswerForm.ENTITY_TABLE, entities={}),
                      _item(protocol.AnswerForm.ENTITY_TABLE), env)
    assert r.passed is False


def test_baseline_gate_missing_inputs_fails():
    r = baseline.gate(_ans(protocol.AnswerForm.ENTITY_TABLE, entities={}),
                      _item(protocol.AnswerForm.ENTITY_TABLE), {})
    assert r.passed is False and "reason" in r.details
