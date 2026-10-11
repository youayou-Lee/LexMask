"""eval-ng 唯一 CLI 入口：python eval-ng/cli.py run <考卷> --participant <名> [--ci]。

exit code：0 全过；1 闸门 FAIL；2 用法/环境标注错误。
（目录名 eval-ng 带连字符，无法 python -m eval_ng；脚本直跑时自举注册包名 evalng。）
"""

from __future__ import annotations

if __package__ in (None, ""):  # 脚本直跑：注册包上下文使相对 import 生效
    import sys
    import types
    from pathlib import Path as _Path

    _pkg_dir = _Path(__file__).resolve().parent
    _pkg = types.ModuleType("evalng")
    _pkg.__path__ = [str(_pkg_dir)]
    sys.modules["evalng"] = _pkg
    __package__ = "evalng"

import argparse
import asyncio
import json
import sys
from pathlib import Path

from .engine.protocol import AnswerForm, EnvReport, ExamItem
from .engine.report import save_report
from .engine.runner import MissingEnvAnnotation, MetricGroup, run_exam


def load_exam_json(path: Path, exam_id: str | None = None) -> list[ExamItem]:
    """考卷 JSON：{"exam_id": ..., "items": [{id, form, text?, types?, gt?, path?, sidecars?,
    pages?, carrier?, expect_reject?}]}"""
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    exam_id = exam_id or doc.get("exam_id", Path(path).stem)
    items = []
    for raw in doc["items"]:
        form = AnswerForm(raw["form"])
        payload: dict = {}
        if form is AnswerForm.ENTITY_TABLE:
            payload = {"text": raw.get("text", ""), "types": raw.get("types", []),
                       "mock_answer": raw.get("mock_answer")}
        elif form is AnswerForm.ROBUSTNESS:
            payload = {"path": raw.get("path", ""), "expect_reject": raw.get("expect_reject", True)}
        elif form is AnswerForm.ARTIFACT_FILE:
            payload = {"path": raw.get("path", ""), "carrier": raw.get("carrier"),
                       "pages": raw.get("pages"), "sidecars": raw.get("sidecars", {})}
        elif form is AnswerForm.PERF_ONLY:
            payload = {"path": raw.get("path", ""), "text": raw.get("text", "")}
        items.append(ExamItem(id=raw["id"], exam_id=exam_id, form=form,
                              payload=payload, gt=raw.get("gt")))
    return items


def build_participant(name: str, args) -> object:
    if name == "mock":
        from .tests.mock_participant import MockParticipant

        return MockParticipant()
    if name in ("has", "glm-gen4"):
        from .participants.has import HasParticipant
        from .participants.glm_gen4 import GlmGen4Participant

        cls = HasParticipant if name == "has" else GlmGen4Participant
        return cls(args.ner_base, model=args.model, max_tokens=args.max_tokens,
                   timeout=args.timeout)
    if name == "backend-pipeline":
        from .participants.backend_pipeline import BackendPipelineParticipant

        return BackendPipelineParticipant(args.api_base, args.api_user, args.api_pass,
                                          timeout=args.timeout)
    raise SystemExit(f"未知参赛者 {name!r}（可选 mock/has/glm-gen4/backend-pipeline）")


def _parse_args(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="eval_ng", description="eval-ng 统一评测引擎")
    sub = ap.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="执行一次评测（四元组）")
    run.add_argument("exam", help="考卷 JSON 路径（或 manifest v2 路径）")
    run.add_argument("--exam-id", default=None)
    run.add_argument("--participant", required=True,
                     help="mock | has | glm-gen4 | backend-pipeline")
    run.add_argument("--runs", type=int, default=3)
    run.add_argument("--ci", action="store_true",
                     help="CI 档：只跑绝对红线（数字否决/零泄漏），跳过基线闸门")
    run.add_argument("--out", default="eval-ng/reports")
    run.add_argument("--env-label", default=None)
    run.add_argument("--target-label", default=None)
    run.add_argument("--hardware", default=None, help="环境自报：硬件")
    run.add_argument("--topology", default=None, help="环境自报：拓扑")
    run.add_argument("--stack-version", action="append", default=[],
                     metavar="NAME=VER", help="环境自报：组件版本，可多次")
    run.add_argument("--ner-base", default="http://127.0.0.1:8080/v1")
    run.add_argument("--model", default=None)
    run.add_argument("--api-base", default="http://127.0.0.1:8000")
    run.add_argument("--api-user", default="eval_user")
    run.add_argument("--api-pass", default="EvalUser!2026")
    run.add_argument("--timeout", type=float, default=600.0)
    run.add_argument("--max-tokens", type=int, default=4000)
    run.add_argument("--baseline", default=None, help="基线 metrics JSON（非 --ci 才有意义）")
    args = ap.parse_args(argv)
    return args


async def _run(args: argparse.Namespace) -> int:
    if not args.env_label or not args.target_label:
        print("缺 --env-label / --target-label（沿用旧 run_eval 强制惯例）", file=sys.stderr)
        return 2
    stack = {"env": args.env_label}
    for kv in args.stack_version:
        k, _, v = kv.partition("=")
        stack[k] = v
    env = EnvReport(hardware=args.hardware or "unspecified",
                    stack_versions=stack,
                    topology=args.topology or "unspecified")

    exam = load_exam_json(Path(args.exam), args.exam_id)
    participant = build_participant(args.participant, args)

    gate_names = ["digital_veto"]
    if any(i.form is AnswerForm.ROBUSTNESS for i in exam):
        gate_names.append("robustness")
    if any(i.form is AnswerForm.ARTIFACT_FILE for i in exam):
        gate_names.append("leak_free")
    if args.baseline and not args.ci:
        gate_names.append("baseline_gate")

    gate_env: dict = {}
    if args.baseline:
        base_doc = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
        gate_env["baseline_metrics"] = base_doc

    group = MetricGroup(name="default", gates=gate_names, runs=args.runs)
    try:
        result = await run_exam(exam, participant, group, env,
                                ci_mode=args.ci, gate_env=gate_env,
                                exam_id=args.exam_id or exam[0].exam_id)
    except MissingEnvAnnotation as exc:
        print(str(exc), file=sys.stderr)
        return 2

    md_path, json_path = save_report(result, Path(args.out))
    print(f"[eval-ng] 报告：{md_path}\n[eval-ng] 报告：{json_path}")
    print(f"[eval-ng] 闸门总判定：{'PASS' if result['gate_pass'] else 'FAIL'}")
    return 0 if result["gate_pass"] else 1


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    if args.command == "run":
        return asyncio.run(_run(args))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
