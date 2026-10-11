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
from datetime import datetime
from pathlib import Path

from .engine.protocol import AnswerForm, EnvReport, ExamItem
from .engine.report import save_report
from .engine.runner import MissingEnvAnnotation, MetricGroup, run_exam


def load_exam_json(path: Path, exam_id: str | None = None) -> list[ExamItem]:
    """考卷 JSON：{"exam_id": ..., "items": [{id, form, text?, types?, gt?, path?, sidecars?,
    pages?, carrier?, expect_reject?}]}"""
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    doc_exam_id = exam_id or doc.get("exam_id", Path(path).stem)
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
        items.append(ExamItem(id=raw["id"], exam_id=raw.get("exam_id") or doc_exam_id,
                              form=form, payload=payload, gt=raw.get("gt")))
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
    run.add_argument("--ner-base", default="http://127.0.0.1:8080/v1")
    run.add_argument("--model", default=None)
    run.add_argument("--max-tokens", type=int, default=4000)
    run.add_argument("--baseline", default=None, help="基线 metrics JSON（非 --ci 才有意义）")
    e2e = sub.add_parser("run-e2e", help="端到端层评测（镜像旧 run_eval --level e2e 口径）")
    for p in (run, e2e):
        p.add_argument("--out", default="eval-ng/reports")
        p.add_argument("--env-label", default=None)
        p.add_argument("--target-label", default=None)
        p.add_argument("--hardware", default=None, help="环境自报：硬件")
        p.add_argument("--topology", default=None, help="环境自报：拓扑")
        p.add_argument("--stack-version", action="append", default=[],
                       metavar="NAME=VER", help="环境自报：组件版本，可多次")
        p.add_argument("--api-base", default="http://127.0.0.1:8000")
        p.add_argument("--api-user", default="eval_user")
        p.add_argument("--api-pass", default="EvalUser!2026")
        p.add_argument("--timeout", type=float, default=600.0)
    e2e.add_argument("exam", help="e2e 考卷 JSON（make_exam_from_manifest.py 产物）")
    e2e.add_argument("--warmup-pages", type=int, default=1)
    e2e.add_argument("--baseline", default=None)
    args = ap.parse_args(argv)
    return args


async def _run(args: argparse.Namespace) -> int:
    from .engine.env_detect import detect_hardware

    if not args.env_label or not args.target_label:
        print("缺 --env-label / --target-label（沿用旧 run_eval 强制惯例）", file=sys.stderr)
        return 2
    stack = {"env": args.env_label}
    for kv in args.stack_version:
        k, _, v = kv.partition("=")
        stack[k] = v
    # 硬件自动探测（用户反馈）：人工传了 --hardware 以人工为准，否则机器自读
    hardware = args.hardware or detect_hardware()
    if not hardware:
        print("硬件探测失败且未传 --hardware（AC4 环境标注硬校验）："
              "请手动指定，如 --hardware K500SM", file=sys.stderr)
        return 2
    if not args.hardware:
        print(f"[eval-ng] 硬件自动探测：{hardware}")
    env = EnvReport(hardware=hardware,
                    stack_versions=stack,
                    topology=args.topology or "unspecified")

    exam = load_exam_json(Path(args.exam), args.exam_id)
    # 按桶（exam_id）分组：多桶考卷逐桶出分（用户意见①：一次跑多桶）
    groups: dict[str, list[ExamItem]] = {}
    for it in exam:
        groups.setdefault(it.exam_id, []).append(it)

    bucket_meta = json.loads(Path(args.exam).read_text(encoding="utf-8")).get("buckets") or {}

    results = {}
    for gexam_id, gitems in groups.items():
        result = await _run_single(args, gitems, gexam_id, env)
        if result is None:
            return 2
        results[gexam_id] = result
    if len(results) == 1:
        res = next(iter(results.values()))
        print(f"[eval-ng] 闸门总判定：{'PASS' if res['gate_pass'] else 'FAIL'}")
        return 0 if res["gate_pass"] else 1
    _write_multi_report(results, bucket_meta, Path(args.out))
    all_pass = all(r["gate_pass"] for r in results.values())
    print(f"[eval-ng] 多桶总判定：{'PASS' if all_pass else 'FAIL'}（逐桶见汇总报告）")
    return 0 if all_pass else 1


def _write_multi_report(results: dict, bucket_meta: dict, out_dir: Path) -> Path:
    """多桶汇总：桶说明（内容/全量抽样）+ 逐桶成绩表（用户意见②）。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = out_dir / f"{ts}-t2-multi-summary.json"
    rows = ["# eval-ng 多桶成绩单", "",
            "| 桶 | 内容 | 全量/抽样 | 条数 | F1(mean) | P | R | 数字闸 |",
            "|---|---|---|---|---|---|---|---|"]
    for gid, res in results.items():
        meta = bucket_meta.get(gid, {})
        agg = res.get("aggregated", {})
        f1 = agg.get("f1", {}).get("mean")
        o = agg.get("last_run_full", {})
        dv = next((g for g in res.get("gates", []) if g.name == "digital_veto"), None)
        rows.append(
            f"| {gid} | {meta.get('content', '—')} | {meta.get('sampling', '—')} "
            f"| {meta.get('count', len(res.get('answers', [])))} "
            f"| {f1 if f1 is None else f'{f1:.4f}'} "
            f"| {o.get('overall', {}).get('precision', 0):.4f} "
            f"| {o.get('overall', {}).get('recall', 0):.4f} "
            f"| {'—' if dv is None else ('PASS' if dv.passed else 'FAIL')} |"
        )
    summary = {
        "buckets": {gid: {
            "meta": bucket_meta.get(gid, {}),
            "runs": res["runs"],
            "aggregated": {k: v for k, v in res["aggregated"].items()
                           if k != "last_run_full"},
            "overall_last_run": res["aggregated"].get("last_run_full", {}).get("overall"),
            "gates": [{"name": g.name, "passed": g.passed, "details": g.details}
                      for g in res.get("gates", [])],
            "gate_pass": res["gate_pass"],
            "perf": res.get("perf"),
        } for gid, res in results.items()},
        "env": {"hardware": results and next(iter(results.values()))["env"].hardware,
                "topology": results and next(iter(results.values()))["env"].topology,
                "stack_versions": results and next(iter(results.values()))["env"].stack_versions},
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    md_path = path.with_suffix(".md")
    md_path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    print(f"[eval-ng] 多桶汇总：{md_path}\n[eval-ng] 多桶汇总：{path}")
    return md_path


async def _run_single(args: argparse.Namespace, exam: list[ExamItem], exam_id: str,
                      env: EnvReport) -> dict | None:
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
                                exam_id=exam_id)
    except MissingEnvAnnotation as exc:
        print(str(exc), file=sys.stderr)
        return None

    md_path, json_path = save_report(result, Path(args.out))
    print(f"[eval-ng] {exam_id} 报告：{md_path}")
    return result


async def _run_e2e(args: argparse.Namespace) -> int:
    from .engine import e2e as e2e_mod

    from .engine.env_detect import detect_hardware

    if not args.env_label or not args.target_label:
        print("缺 --env-label / --target-label（沿用旧 run_eval 强制惯例）", file=sys.stderr)
        return 2
    stack = {"env": args.env_label}
    for kv in args.stack_version:
        k, _, v = kv.partition("=")
        stack[k] = v
    hardware = args.hardware or detect_hardware()
    if not hardware:
        print("硬件探测失败且未传 --hardware（AC4）：请手动指定", file=sys.stderr)
        return 2
    env = {"hardware": hardware, "stack_versions": stack,
           "topology": args.topology or "unspecified"}

    doc = json.loads(Path(args.exam).read_text(encoding="utf-8"))
    api = e2e_mod.E2eApi(args.api_base, args.api_user, args.api_pass, timeout=args.timeout)
    try:
        result = e2e_mod.run_e2e_level(api, doc["items"], warmup_pages=args.warmup_pages)
    finally:
        api.close()
    result = e2e_mod.strip_heavy(result)
    result["env"] = env
    result["config"] = {"api_base": args.api_base, "warmup_pages": args.warmup_pages,
                        "exam": str(args.exam)}
    result["gate_pass"] = bool(result["overall"].get("digital_gate", {}).get("pass")) \
        if result["overall"] else not result["failed"]
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    json_path = out / f"{ts}-{result.get('exam_id', 'e2e')}-e2e.json"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[eval-ng] e2e 报告：{json_path}")
    overall = result.get("overall") or {}
    if overall:
        m = overall.get("overall", {})
        print(f"[eval-ng] e2e 总体 P={m.get('precision'):.3f} R={m.get('recall'):.3f} "
              f"F1={m.get('f1'):.3f} 数字闸={'PASS' if result['gate_pass'] else 'FAIL'}")
    print(f"[eval-ng] rejected={result['rejected']} failed={[f['id'] for f in result['failed']]}")
    return 0 if result["gate_pass"] else 1


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    if args.command == "run":
        return asyncio.run(_run(args))
    if args.command == "run-e2e":
        return asyncio.run(_run_e2e(args))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
