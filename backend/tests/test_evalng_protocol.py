"""eval-ng protocol 层单测（CI，无真端点）。"""

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _load(name, relpath):
    spec = importlib.util.spec_from_file_location(
        name, REPO / relpath, submodule_search_locations=[str((REPO / relpath).parent)]
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


protocol = _load(
    "evalng_engine_protocol",
    "eval-ng/engine/protocol.py",
)


def test_answer_form_enum_values():
    assert {f.value for f in protocol.AnswerForm} == {
        "entity_table",
        "transcript",
        "artifact_file",
        "robustness",
        "perf_only",
    }


def test_dataclass_defaults():
    item = protocol.ExamItem(id="p1", exam_id="ex1", form=protocol.AnswerForm.ENTITY_TABLE)
    assert item.payload == {} and item.gt is None
    ans = protocol.Answer(form=protocol.AnswerForm.ENTITY_TABLE)
    assert ans.warnings == [] and ans.entities is None and ans.latency_sec is None


def test_env_report_missing_fields():
    env = protocol.EnvReport(hardware="K500SM", stack_versions={"vllm": "0.9.2"}, topology="scnet-main")
    assert env.missing_fields() == []
    assert protocol.EnvReport().missing_fields() == ["hardware", "stack_versions", "topology"]
    assert protocol.EnvReport(hardware="x").missing_fields() == ["stack_versions", "topology"]


def _make_valid_participant():
    class Fake:
        name = "fake"
        answer_form = protocol.AnswerForm.ENTITY_TABLE
        supports = None

        async def probe(self, item):
            return protocol.Answer(form=self.answer_form, entities={}, latency_sec=0.1)

    return Fake()


def test_validate_participant_ok():
    protocol.validate_participant(_make_valid_participant())


def test_validate_participant_missing_name():
    class Bad:
        answer_form = protocol.AnswerForm.ENTITY_TABLE
        supports = None

        async def probe(self, item):
            return protocol.Answer(form=self.answer_form)

    try:
        protocol.validate_participant(Bad())
    except protocol.ParticipantProtocolError as e:
        assert "name" in str(e)
    else:
        raise AssertionError("expected ParticipantProtocolError")


def test_validate_participant_probe_not_callable():
    class Bad:
        name = "x"
        answer_form = protocol.AnswerForm.ENTITY_TABLE
        supports = None
        probe = None

    try:
        protocol.validate_participant(Bad())
    except protocol.ParticipantProtocolError:
        pass
    else:
        raise AssertionError("expected ParticipantProtocolError")
