"""eval-ng runner + report 单测（FakeParticipant，零端点）。"""

import json

import evalng_bootstrap as boot

runner = boot.load("engine/runner.py")
report = boot.load("engine/report.py")
protocol = boot.load("engine/protocol.py")

EnvReport = protocol.EnvReport
AnswerForm = protocol.AnswerForm


class FakeEntityParticipant:
    name = "fake-ner"
    answer_form = AnswerForm.ENTITY_TABLE
    supports = None

    def __init__(self, wrong_digital=False):
        self.wrong_digital = wrong_digital

    async def probe(self, item):
        entities = dict(item.gt or {})
        if self.wrong_digital and "身份证号" in entities:
            entities["身份证号"] = ["9".join("000")]
        return protocol.Answer(form=self.answer_form, entities=entities, latency_sec=0.5)


class FakeRobustnessParticipant:
    name = "fake-rob"
    answer_form = AnswerForm.ROBUSTNESS
    supports = None

    async def probe(self, item):
        return protocol.Answer(form=self.answer_form, rejected=True, latency_sec=0.1)


def _exam():
    return [
        protocol.ExamItem(id="p1", exam_id="ex", form=AnswerForm.ENTITY_TABLE,
                          payload={"text": "x", "types": ["姓名"]},
                          gt={"姓名": ["张三"], "身份证号": ["110101199001010011"]}),
    ]


def _env():
    return EnvReport(hardware="K500SM", stack_versions={"vllm": "0.9.2"}, topology="scnet-main")


def test_missing_env_annotation_raises():
    try:
        runner.run_exam_sync(_exam(), FakeEntityParticipant(), runner.MetricGroup(name="g", runs=1),
                             EnvReport(hardware="x"))
    except runner.MissingEnvAnnotation as e:
        assert "stack_versions" in str(e)
    else:
        raise AssertionError("expected MissingEnvAnnotation")


def test_runs_and_per_run_length():
    result = runner.run_exam_sync(_exam(), FakeEntityParticipant(),
                                  runner.MetricGroup(name="g", runs=3), _env())
    assert result["runs"] == 3
    assert len(result["per_run_metrics"]) == 3
    assert result["gate_pass"] is True
    assert result["aggregated"]["f1"]["mean"] == 1.0


def test_gate_fail_marks_fail():
    result = runner.run_exam_sync(_exam(), FakeEntityParticipant(wrong_digital=True),
                                  runner.MetricGroup(name="g", runs=1), _env())
    assert result["gate_pass"] is False
    names = [gr.name for gr in result["gates"]]
    assert "digital_veto" in names


def test_ci_mode_skips_baseline_gate():
    group = runner.MetricGroup(name="g", runs=1, gates=["baseline_gate", "digital_veto"])
    result = runner.run_exam_sync(_exam(), FakeEntityParticipant(), group, _env(), ci_mode=True)
    names = [gr.name for gr in result["gates"]]
    assert "baseline_gate" not in names and "digital_veto" in names


def test_robustness_gate_in_runner():
    exam = [protocol.ExamItem(id="r1", exam_id="ex", form=AnswerForm.ROBUSTNESS,
                              payload={"path": "enc.pdf", "expect_reject": True})]
    group = runner.MetricGroup(name="g", runs=1, gates=["robustness"])
    result = runner.run_exam_sync(exam, FakeRobustnessParticipant(), group, _env())
    assert result["gate_pass"] is True


def test_render_md_and_json_roundtrip(tmp_path):
    result = runner.run_exam_sync(_exam(), FakeEntityParticipant(),
                                  runner.MetricGroup(name="g", runs=2), _env())
    md = report.render_report(result, "md")
    assert "runs" in md and "K500SM" in md and "scnet-main" in md and "vllm" in md
    payload = json.loads(report.render_report(result, "json"))
    assert payload["runs"] == 2 and payload["env"]["hardware"] == "K500SM"
    md_path, json_path = report.save_report(result, tmp_path / "out")
    assert md_path.exists() and json_path.exists()


def test_report_rejects_missing_env():
    result = runner.run_exam_sync(_exam(), FakeEntityParticipant(),
                                  runner.MetricGroup(name="g", runs=1), _env())
    result["env"] = EnvReport()  # 模拟下游篡改
    try:
        report.render_report(result, "md")
    except report.MissingEnvAnnotation:
        pass
    else:
        raise AssertionError("expected MissingEnvAnnotation")
