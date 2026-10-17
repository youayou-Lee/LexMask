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
import hashlib
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from .engine.protocol import AnswerForm, EnvReport, ExamItem
from .engine.report import save_report
from .engine.runner import MetricGroup, MissingEnvAnnotation, run_exam


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
        from .participants.glm_gen4 import GlmGen4Participant
        from .participants.has import HasParticipant

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
    run.add_argument("exam", help="考卷 JSON 路径（manifest v2 接线为后续迭代）")
    run.add_argument("--exam-id", default=None)
    run.add_argument("--participant", required=True,
                     help="mock | has | glm-gen4 | backend-pipeline")
    run.add_argument("--runs", type=int, default=3)
    run.add_argument("--concurrency", type=int, default=1,
                     help="逐题并发路数（质量口径不变；>1 时速度字段自动标注不与串行对比）")
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
        # 口令经环境变量传入：公开仓不得出现真实口令字面量（G4）
        p.add_argument("--api-pass", default=os.environ.get("EVAL_API_PASS"))
        p.add_argument("--timeout", type=float, default=600.0)
    e2e.add_argument("exam", help="e2e 考卷 JSON（make_exam_from_manifest.py 产物）")
    e2e.add_argument("--warmup-pages", type=int, default=1)
    e2e.add_argument("--baseline", default=None)
    speed = sub.add_parser(
        "speed",
        help="速度测量（单臂单次报告；臂/重启编排在引擎外的薄脚本里）",
        description="四态判定：PASS / FAIL / BLOCKED / UNVERIFIED；exit 0=PASS 1=FAIL 3=BLOCKED|UNVERIFIED 2=用法/前置错",
    )
    speed.add_argument("exam", help="速度考卷 JSON（corpus[]；或 eval-ng items[] 的 artifact_file/robustness）")
    speed.add_argument("--driver", required=True, choices=["single", "batch"],
                       help="single=逐页 vision（锚点装置同构）；batch=smart_batch 作业链路")
    speed.add_argument("--arm", required=True, help="臂定义 JSON（私有仓 arms/*.json；schema 见 speedkit.arms）")
    speed.add_argument("--out", required=True, help="报告前缀：写 <out>.json 与 <out>.md")
    speed.add_argument("--page-concurrency", type=int, required=True, help="页并发 k（显式给，无默认）")
    speed.add_argument("--cache-policy", required=True, choices=["cold", "warm"])
    speed.add_argument("--suite", required=True, choices=["synthetic", "real"])
    speed.add_argument("--round-evidence", required=True,
                       help="编排写出的 rounds.json（含 pid/start_time/时间戳/就绪等待/生效开关集）")
    speed.add_argument("--payload-file", default=None,
                       help="显式载荷 JSON；缺省=锚点同款钉死集（OCR19+印章/签名）")
    speed.add_argument("--cleanup", action="store_true", help="跑完删除上传的卷（真卷必须带）")
    speed.add_argument("--rounds", type=int, default=4, help="总轮数（含 warmup 轮），上限 5")
    speed.add_argument("--warmup-rounds", type=int, default=1)
    speed.add_argument("--round-restart-cmd", default=None,
                       help="轮间重启命令（编排给的薄脚本；引擎只执行它并读证据，不自己起停服务）")
    speed.add_argument("--api-base", default="http://127.0.0.1:8000")
    speed.add_argument("--api-user", default="eval_user")
    speed.add_argument("--api-pass", default=os.environ.get("EVAL_API_PASS"))
    speed.add_argument("--timeout", type=float, default=600.0)
    speed.add_argument("--job-timeout-min", type=float, default=120.0)
    speed.add_argument("--upload-workers", type=int, default=4)
    speed.add_argument("--code-rev", default=None, help="编排传入的代码版本（引擎会与现场 git HEAD 双向核对）")
    speed.add_argument("--install", default=None, help="安装标识（默认记分支+rev）")
    speed.add_argument("--baseline", default=None,
                       help="[悬空] 同实例 benchmark_throughput.py 报告，用于 D7 parity 对照")
    speed.add_argument("--runs", type=int, default=1, help=argparse.SUPPRESS)
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

    raw_doc = json.loads(Path(args.exam).read_text(encoding="utf-8"))
    exam = load_exam_json(Path(args.exam), args.exam_id)
    # 按桶（exam_id）分组：多桶考卷逐桶出分（用户意见①：一次跑多桶）
    groups: dict[str, list[ExamItem]] = {}
    for it in exam:
        groups.setdefault(it.exam_id, []).append(it)

    if not groups:
        print("考卷无任何条目", file=sys.stderr)
        return 2
    bucket_meta = raw_doc.get("buckets") or {}

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
    first = next(iter(results.values()))
    env_line = f"- 硬件：{first['env'].hardware}｜拓扑：{first['env'].topology}"
    conc = first.get("concurrency", 1)
    if conc > 1:
        env_line += f"｜并发 {conc} 路（速度含排队等待，不与串行对比）"
    rows = ["# eval-ng 多桶成绩单", "", env_line, "",
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
            "concurrency": res.get("concurrency", 1),
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
        gate_env["env_label"] = args.env_label

    group = MetricGroup(name="default", gates=gate_names, runs=args.runs)
    try:
        result = await run_exam(exam, participant, group, env,
                                ci_mode=args.ci, gate_env=gate_env,
                                exam_id=exam_id,
                                concurrency=getattr(args, "concurrency", 1))
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
    if args.command == "speed":
        return _run_speed(args)
    return 2


class _Usage(Exception):
    """用法/前置错误 ⇒ exit 2（装置不可得记 BLOCKED，不与用法错误混判）。"""


# 判据 ↔ 报告字段路径（D3⑦：引用表逐条回取，路径不存在即 FAIL）
SPEED_CRITERIA_PATHS: dict[str, str] = {
    "D3.closure_same_clock": "summary.closure_ratio",
    "D3.residual_two_sided": "summary.residual_ratio",
    "D3.cross_clock_not_copied": "summary.harness_ratio",
    "D4.file_wall_p50": "summary.file_wall_s.p50",
    "D4.file_wall_mean": "summary.file_wall_s.mean",
    "D4.wall_agreement": "summary.wall_agreement.max_abs_ratio_diff",
    "D4.pages_per_min": "summary.pages_per_min",
    "D4.parallel_efficiency": "summary.parallel_efficiency",
    "D5.readiness_ready": "readiness.ready_wait_sec",
    "D5.readiness_settle": "readiness.settle_sec",
    "D8.cache_hits": "summary.cache_hits",
}


def _speed_exit(verdict: str) -> int:
    return {"PASS": 0, "FAIL": 1}.get(verdict, 3)


def _load_speed_corpus(args) -> tuple[list[dict], dict]:
    """读速度考卷 → (逐卷条目, 入报告的考卷视图)。页数必须明示；md5/文本层现场机检。"""
    from .engine.speedkit import driver as sk_driver
    from .engine.speedkit import report as sk_report

    path = Path(args.exam)
    if not path.is_file():
        raise _Usage(f"考卷不存在：{path}")
    doc = json.loads(path.read_text(encoding="utf-8"))
    suite = str(doc.get("suite") or "synthetic")
    if args.suite == "real" and suite != "real":
        raise _Usage(f"--suite real 但考卷 {path} 未声明 \"suite\":\"real\"（防合成卷冒充真卷）")
    if args.suite == "synthetic" and suite == "real":
        raise _Usage("考卷声明 real，须用 --suite real 调用（防真卷被当合成卷对外报数）")

    raw_items = doc.get("corpus") or []
    if not raw_items:
        raw_items = [raw for raw in (doc.get("items") or [])
                     if raw.get("path") and str(raw.get("form") or "") in ("artifact_file", "robustness", "")]
    if not raw_items:
        raise _Usage(f"考卷 {path} 无可用条目（corpus[] 或 items[form=artifact_file|robustness]）")

    items: list[dict] = []
    for raw in raw_items:
        file_path = str(raw.get("path") or "")
        if not file_path or not Path(file_path).is_file():
            raise _Usage(f"考卷条目 {raw.get('id')!r} 的 path 不存在：{file_path!r}")
        pages = raw.get("pages")
        if not isinstance(pages, int) or pages <= 0:
            raise _Usage(f"考卷条目 {raw.get('id')!r} 必须显式给正整数 pages（轮次合法性依赖它）")
        md5 = str(raw.get("md5") or sk_report.md5_file(file_path))
        expect_md5 = str(raw.get("expect_md5") or "")
        if expect_md5 and md5 != expect_md5:
            raise _Usage(f"考卷条目 {raw.get('id')!r} 的 md5 {md5} ≠ 声明 expect_md5 {expect_md5}"
                         "（卷内容已漂移，锚点不可比）")
        carrier = str(raw.get("carrier") or "")
        carrier_view = str(raw.get("carrier_view") or "")
        if carrier_view not in ("", "backend", "local"):
            raise _Usage(f"考卷条目 {raw.get('id')!r} 的 carrier_view 只能是 backend/local 或留空，"
                         f"收到 {carrier_view!r}（该字段决定 D1.corpus_premise 拿哪个机检视图对账）")
        text_chars = None
        carrier_source = "declared" if carrier else ""
        if Path(file_path).suffix.lower() == ".pdf":
            text_chars = sk_driver.pdftotext_nonblank_chars(file_path)
            if not carrier:
                carrier = "scanned_pdf" if not text_chars else "text_pdf"
                carrier_source = "inferred_local"
        items.append({
            "id": str(raw.get("id") or Path(file_path).stem),
            "path": str(file_path),
            "pages": pages,
            "md5": md5,
            "carrier": carrier or "unknown",
            "carrier_view": carrier_view,
            "carrier_source": carrier_source,
            "text_chars": text_chars,
            "expect_reject": bool(raw.get("expect_reject")),
            "no_gt": True,
        })
    # 报告视图：绝对路径一律脱敏（D5④/G4 是硬门；real 卷另加 digest，D9②）
    view_items = []
    for entry in items:
        view = dict(entry)
        if suite == "real":
            digest = hashlib.sha256(str(entry["path"]).encode("utf-8")).hexdigest()[:12]
            view["path"] = f"<redacted:{digest}>"
        else:
            view["path"] = sk_report.redact_path(entry["path"])
        view_items.append(view)
    exam_view = {
        "exam_id": str(doc.get("exam_id") or path.stem),
        "suite": suite,
        "path": (f"<redacted:{hashlib.sha256(str(path).encode()).hexdigest()[:12]}>"
                 if suite == "real" else sk_report.redact_path(str(path))),
        "manifest_sha256": sk_report.sha256_file(str(path)),
        "corpus": view_items,
    }
    return items, exam_view


def _run_speed(args: argparse.Namespace) -> int:
    """`speed` 子命令：单臂单次报告。臂/重启/授权在引擎外的编排脚本里，引擎零业务 spawn。"""
    import os
    import shlex
    import subprocess

    from .engine.speedkit import arms as _arms  # noqa: F401 —— 保持模块级常量可读性
    from .engine.speedkit import arms as sk_arms
    from .engine.speedkit import collect as sk_collect
    from .engine.speedkit import driver as sk_driver
    from .engine.speedkit import report as sk_report

    started_at = sk_report.utcnow()

    try:
        if args.page_concurrency < 1:
            raise _Usage(f"--page-concurrency 必须 ≥1，实为 {args.page_concurrency}")
        if args.rounds < 2 or args.rounds > sk_driver.MAX_TIMING_ROUNDS + sk_driver.WARMUP_ROUNDS:
            raise _Usage(f"--rounds 必须在 2..{sk_driver.MAX_TIMING_ROUNDS + sk_driver.WARMUP_ROUNDS}"
                         f"（含 warmup 轮），实为 {args.rounds}")
        if args.warmup_rounds < 0 or args.warmup_rounds >= args.rounds:
            raise _Usage(f"--warmup-rounds={args.warmup_rounds} 必须落在 0..{args.rounds - 1}")
        if args.upload_workers < 1:
            raise _Usage("--upload-workers 必须 ≥1")
        out_dir = Path(args.out).resolve().parent
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise _Usage(f"--out 目录不可建：{out_dir}（{exc}）") from exc
        if not os.access(out_dir, os.W_OK):
            raise _Usage(f"--out 目录不可写：{out_dir}")
        try:
            payload, payload_source, payload_sha = sk_driver.effective_payload(args.payload_file)
            arm = sk_arms.load_arm(args.arm)
            evidence = sk_driver.load_round_evidence(args.round_evidence)
            corpus, exam_view = _load_speed_corpus(args)
            baseline_doc = _load_baseline(args.baseline)  # 测量前 fail-fast（别跑完才发现基线不可读）
        except OSError as exc:
            # 文件不存在/不可读是**用法前置错误**（D6②：明确报错非 0），不得冒泡成 traceback，
            # 也不得混进「装置不可得 ⇒ BLOCKED」把作者的用法错误洗成被测系统问题。
            raise _Usage(f"前置文件不可读：{exc}") from exc
        total_pages = sum(it["pages"] for it in corpus)
        if args.page_concurrency > total_pages:
            raise _Usage(f"--page-concurrency={args.page_concurrency} > 清单总页数 {total_pages}")
        timing_items = [it for it in corpus if not it["expect_reject"]]
        expected_pages = sum(it["pages"] for it in timing_items)
    except _Usage as exc:
        print(f"[speed] 用法错误：{exc}", file=sys.stderr)
        return 2
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"[speed] 前置错误：{exc}", file=sys.stderr)
        return 2

    ignore_paths = (str(Path(args.out).resolve()),)
    checks: list[dict] = []
    abort_reason: str | None = None
    try:
        checks.extend(sk_arms.arm_checks(arm, phase="pre", ignore_paths=ignore_paths))
    except ValueError as exc:
        print(f"[speed] 前置错误：{exc}", file=sys.stderr)
        return 2
    if any(c.get("status") in ("FAIL", "BLOCKED") for c in checks):
        abort_reason = "臂前置断言未过（patch 臂跑前工作树必须干净）；未进入计时"
        print(f"[speed] {abort_reason}", file=sys.stderr)

    rounds: list[dict] = []
    type_ids: list[str] = []
    if not abort_reason:
        try:
            api = sk_driver.SpeedApi(args.api_base, args.api_user, args.api_pass, timeout=args.timeout)
        except Exception as exc:  # noqa: BLE001
            # 登录/建连即失败 = 装置不可得 ⇒ BLOCKED（不是被测对象的性能结论，不得判 FAIL）
            api = None
            checks.append({"id": "D1.measurement_reachable", "status": "BLOCKED",
                           "detail": f"登录/建连失败（装置不可得 ⇒ BLOCKED，不判 FAIL）："
                                     f"{type(exc).__name__}: {str(exc)[:200]}", "value": None})
            abort_reason = f"装置不可得（登录失败）：{type(exc).__name__}: {str(exc)[:160]}"
            print(f"[speed] {abort_reason}", file=sys.stderr)
        try:
            if args.driver == "batch" and not abort_reason:
                try:
                    type_ids = api.default_type_ids()
                except Exception as exc:  # noqa: BLE001
                    type_ids = []
                    checks.append({"id": "D6.batch_type_ids", "status": "BLOCKED",
                                   "detail": f"取默认类型集失败：{exc!r}", "value": 0})
                if not type_ids:
                    abort_reason = "batch 线取不到默认实体类型集（entity_type_ids 不能为空）"
                    checks.append({"id": "D6.batch_type_ids", "status": "BLOCKED",
                                   "detail": abort_reason, "value": len(type_ids)})
            for round_no in range(1, args.rounds + 1):
                if abort_reason:
                    break
                if round_no > 1 and args.round_restart_cmd:
                    proc = subprocess.run(shlex.split(args.round_restart_cmd), capture_output=True,
                                          text=True, timeout=600, check=False)
                    if proc.returncode != 0:
                        checks.append({"id": "D2.restart_cmd", "status": "FAIL",
                                       "detail": f"轮间重启命令返回 {proc.returncode}："
                                                 f"{(proc.stderr or proc.stdout)[:200]}", "value": proc.returncode})
                        abort_reason = "轮间重启命令失败（冷跑协议不成立）"
                        break
                round_started = sk_report.utcnow()
                try:
                    if args.driver == "single":
                        record = sk_driver.measure_single_round(
                            api, corpus, payload=payload, page_concurrency=args.page_concurrency,
                            cleanup=args.cleanup, upload_workers=args.upload_workers,
                        )
                    else:
                        record = sk_driver.measure_batch_round(
                            api, corpus, payload=payload, type_ids=type_ids, label=arm["name"],
                            timeout_sec=args.job_timeout_min * 60, upload_workers=args.upload_workers,
                            cleanup=args.cleanup,
                        )
                except Exception as exc:  # noqa: BLE001
                    # 装置不可得（后端未起 / 认证失败 / 网络不通）⇒ BLOCKED；被测对象主动拒收
                    # （413 请求体过大 / 504 网关超时）⇒ FAIL（D6④/§7），不得把 FAIL 洗成 BLOCKED。
                    status, detail = sk_driver.measurement_failure_kind(exc)
                    checks.append({"id": "D1.measurement_reachable", "status": status,
                                   "detail": f"{args.driver} 线第 {round_no} 轮测量中断（{detail}）",
                                   "value": getattr(exc, "status_code", None)})
                    if status == "BLOCKED":
                        abort_reason = f"测量不可得（{type(exc).__name__}）：{str(exc)[:160]}"
                        print(f"[speed] {abort_reason}", file=sys.stderr)
                    else:
                        print(f"[speed] 第 {round_no} 轮测量判 FAIL：{detail}", file=sys.stderr)
                    break
                round_finished = sk_report.utcnow()
                warmup = round_no <= args.warmup_rounds
                valid, invalid_reason = sk_driver.round_valid(
                    record, driver=args.driver, expected_pages=expected_pages,
                )
                record.update({
                    "round": round_no, "warmup": warmup, "invalid": bool(not valid),
                    "invalid_reason": None if valid else invalid_reason,
                    "harness_start_ts": round_started, "harness_end_ts": round_finished,
                    "driver_self_wall_s": record.get("driver_self_wall_s"),
                })
                page_agg = record.get("agg") or {}
                record["cache"] = {"histogram": page_agg.get("cache_histogram"),
                                   "hits": page_agg.get("cache_hits")}
                rounds.append(record)
                if not valid and not warmup:
                    checks.append({"id": "D1.round_validity", "status": "FAIL",
                                   "detail": f"计时轮 {round_no} 不合法（已从聚合中排除）：{invalid_reason}",
                                   "value": round_no})
            if not abort_reason and rounds:
                # runner 在**离开整个轮循环之后**才写下一轮重启证据 ⇒ 循环内读到的是上一轮（实测把
                # D2.cold_evidence 谎报成「计时轮 2 缺重启证据」）。退出循环后快照一次并重读。
                evidence = _refresh_round_evidence(args.round_evidence)
        finally:
            try:
                api.close()
            except Exception:  # noqa: BLE001
                pass

    timing = [r for r in rounds if not r.get("warmup") and not r.get("invalid")]
    agg = sk_collect.sum_pages([p for r in timing for p in (r.get("pages") or [])])
    overview_agg = sk_collect.sum_pages([p for r in rounds for p in (r.get("pages") or [])])
    walls = [float(r.get("file_wall_s") or 0.0) for r in timing]
    self_walls = [float(r.get("driver_self_wall_s") or 0.0) for r in timing]
    wall_stats = sk_driver.wall_stats(walls)
    wall_agreement = sk_driver.wall_agreement(
        [(r.get("file_wall_s"), r.get("driver_self_wall_s")) for r in timing]
    )
    pages_in_timing = sum(len(r.get("pages") or []) for r in timing)
    total_wall = sum(walls)
    files = len(timing) * len(timing_items)
    summary = {
        "pages_total": total_pages,
        "pages_measured_timing": pages_in_timing,
        "pages_measured_all_rounds": overview_agg.get("pages_measured"),
        "files_total": len(corpus),
        "files_timed": files,
        "steady_pages": pages_in_timing,
        "sum_backend_pages_ms": agg.get("sum_backend_pages_ms"),
        "sum_harness_pages_ms": agg.get("sum_harness_pages_ms"),
        "sum_rings_ms": agg.get("sum_rings_ms"),
        "unclassified_ms": agg.get("unclassified_ms"),
        "unclassified_source": agg.get("unclassified_source"),
        "residual_ratio": agg.get("residual_ratio"),
        "closure_ratio": (round(abs(agg.get("unclassified_ms") or 0.0) / agg["sum_backend_pages_ms"], 6)
                          if (agg.get("sum_backend_pages_ms") or 0) > 0 else None),
        "harness_ratio": agg.get("harness_ratio"),
        "rings_by_key_ms": agg.get("rings_by_key_ms"),
        "cache_histogram": agg.get("cache_histogram"),
        "cache_hits": agg.get("cache_hits"),
        "page_concurrency_peak": agg.get("page_concurrency_peak"),
        "file_wall_s": wall_stats,
        "driver_self_wall_s": sk_driver.wall_stats(self_walls),
        "wall_agreement": wall_agreement,
        "page_ms_p50": sk_driver.wall_stats(
            [float(p["root_total_ms"]) for r in timing for p in (r.get("pages") or [])
             if sk_collect.is_number(p.get("root_total_ms"))]
        ).get("p50"),
        "pages_per_min": (round(pages_in_timing * 60 / total_wall, 3) if total_wall > 0 else None),
        "files_per_min": (round(files * 60 / total_wall, 3) if total_wall > 0 else None),
        "parallel_efficiency": (
            round(sum(float(p.get("wall_s") or 0.0) for r in timing for p in (r.get("pages") or []))
                  / total_wall, 6) if total_wall > 0 else None
        ),
    }

    checks.extend(sk_driver.env_matches_arm(evidence, arm))
    checks.extend(sk_driver.cold_evidence_checks(
        evidence, rounds, cache_policy=args.cache_policy, driver=args.driver,
        api_base=args.api_base, restart_cmd=args.round_restart_cmd,
        trust_env=(getattr(api.client, "trust_env", None) if api is not None else None),
    ))
    checks.extend(sk_driver.concurrency_dual_evidence(rounds, args.page_concurrency, args.driver))
    checks.extend(sk_driver.expect_reject_checks(rounds, corpus))
    checks.extend(_speed_corpus_premise_checks(corpus, rounds))
    harness_seq, backend_seq = sk_collect.recorded_pair_sequences(
        [[p for p in (r.get("pages") or [])] for r in timing]
    )
    checks.extend(sk_collect.closure_checks(
        agg, harness_seq=harness_seq, backend_seq=backend_seq,
    ))
    checks.extend(sk_collect.key_placement_checks(agg, args.driver))
    checks.extend(_speed_driver_key_checks(args.driver, summary, rounds,
                                           corpus_max_bytes=_corpus_max_bytes(corpus)))
    checks.append({
        "id": "D4.wall_agreement",
        "status": "PASS" if wall_agreement.get("within_tolerance") else "FAIL",
        "detail": f"file_wall_s 与驱动自测墙钟逐轮最大相对差 "
                  f"{wall_agreement.get('max_abs_ratio_diff')}（阈值 ≤{sk_driver.WALL_AGREEMENT_TOLERANCE}）",
        "value": wall_agreement.get("max_abs_ratio_diff"),
    })
    _needs, timing_status, why = sk_driver.timing_rounds_needed(wall_stats)
    checks.append({"id": "D2.timing_rounds", "status": timing_status,
                   "detail": why, "value": wall_stats.get("n")})
    checks.extend(sk_driver.quality_sidecar_check(_merge_sidecars(rounds), rounds))
    if abort_reason:
        checks.append({"id": "D1.aborted", "status": "BLOCKED", "detail": abort_reason, "value": None})
        if not rounds:
            checks.append({"id": "D1.round_validity", "status": "BLOCKED",
                           "detail": "未产生任何计时轮（空报告不得冒充成功）", "value": 0})

    readiness = {
        "ready_wait_sec": evidence.get("ready_wait_sec"),
        "settle_sec": evidence.get("settle_sec"),
    }
    fingerprints = sk_report.build_fingerprints(api_base=args.api_base)
    footnote = {
        "instance": ("local" if sk_report.is_loopback_host(sk_report.api_base_host(args.api_base))
                     else str(fingerprints.get("hostname"))),
        "hardware": fingerprints.get("hardware_raw"),
        "image_dtk": fingerprints.get("dtk_version_raw"),
        "code_rev": args.code_rev or sk_report.runtime_code_rev(),
        "switches": _switches_line(evidence),
        "manifest_hash": exam_view.get("manifest_sha256"),
        "window": f"{started_at} → {sk_report.utcnow()}",
        "readiness": (f"就绪等待 {readiness['ready_wait_sec']}s / 静置 {readiness['settle_sec']}s"
                      if sk_collect.is_number(readiness["ready_wait_sec"])
                      and sk_collect.is_number(readiness["settle_sec"]) else None),
    }
    mode = {
        "driver": args.driver,
        "page_concurrency": args.page_concurrency,
        "cache_policy": args.cache_policy,
        "rounds": args.rounds,
        "warmup_rounds": args.warmup_rounds,
        "timing_rounds": len(timing),
        "payload_source": payload_source,
        "payload_sha256": payload_sha,
        "payload_ocr_types": len(payload["selected_ocr_has_types"]),
        "payload_visual_types": len(payload["selected_visual_feature_types"]),
        "api_base": args.api_base,
        "proxy_bypass": "trust_env=False",
        "suite": exam_view.get("suite"),
        "install": args.install or _install_label(),
        "upload_workers": args.upload_workers,
        "job_timeout_min": args.job_timeout_min,
        # I6：两条线的 file_wall_s 覆盖范围不同，口径随报告落档（跨线比较前必须先读这一行）
        "file_wall_s_semantics": sk_driver.FILE_WALL_SEMANTICS.get(args.driver),
    }
    cache = {
        "policy": args.cache_policy,
        "histogram": agg.get("cache_histogram"),
        "hits": agg.get("cache_hits"),
        "hit_definition": sk_collect.HIT_STATUS,
        "inflight_status_separate": True,
        "per_round": [{"round": r.get("round"), "histogram": (r.get("agg") or {}).get("cache_histogram")}
                      for r in timing],
    }
    reproduce = {
        "command": sk_report.redact_command(
            ["python", "eval-ng/cli.py", "speed", str(args.exam), "--driver", args.driver,
             "--arm", args.arm, "--out", args.out,
             "--page-concurrency", str(args.page_concurrency),
             "--cache-policy", args.cache_policy, "--suite", args.suite,
             "--round-evidence", args.round_evidence]
            + (["--payload-file", args.payload_file] if args.payload_file else [])
            + (["--cleanup"] if args.cleanup else [])),
        "env": sk_report.redact_env(evidence.get("env") or {}),
        "requires": ["同实例", "同窗口", "同语料（md5 见 exam.corpus）", "冷跑时逐轮重启后端"],
    }
    checks.extend(_speed_self_sufficiency_checks(
        mode=mode, arm=sk_arms.arm_report_block(arm), summary=summary, evidence=evidence,
    ))
    # `--baseline` 对照判据（D1① 两臂比值 / D7 parity）依赖 mode 与 summary，故在装配后追加
    checks.extend(_speed_baseline_checks(baseline_doc, mode=mode, summary=summary))
    report = sk_report.build_report(
        exam=exam_view, mode=mode, arm=sk_arms.arm_report_block(arm), rounds=rounds,
        cold_evidence=list(evidence.get("rounds") or []), summary=summary, cache=cache,
        footnote=footnote, fingerprints=fingerprints, criteria_spec=SPEED_CRITERIA_PATHS,
        reproduce=reproduce, checks=checks, not_measured=_not_measured(args),
        started_at=started_at, finished_at=sk_report.utcnow(), has_gt=False, readiness=readiness,
        observations=_speed_anchor_observation(arm, summary),
    )
    try:
        checks.extend(sk_arms.arm_checks(arm, phase="post", ignore_paths=ignore_paths))
        report["checks"] = checks
    except ValueError as exc:
        report["checks"] = checks + [{"id": "D8.arm_post", "status": "FAIL",
                                      "detail": f"跑后臂断言不可执行：{exc}", "value": None}]
    report = sk_report.finalize(report, api_base=args.api_base, live_code_rev=args.code_rev)
    json_path, md_path = sk_report.write_outputs(report, args.out)
    md_errors = sk_report.md_consistency_errors(report, Path(md_path).read_text(encoding="utf-8"))
    if md_errors:
        report["checks"] = list(report["checks"]) + [
            {"id": "D4.md_consistency", "status": "FAIL", "detail": err, "value": None} for err in md_errors
        ]
        report["verdict"] = sk_report.verdict_from_checks(report["checks"])
        report["checks"] = list(report["checks"])
        json_path, md_path = sk_report.write_outputs(report, args.out)
    print(f"[speed] 判定 {report['verdict']}｜报告：{json_path}｜{md_path}")
    return _speed_exit(report["verdict"])


def _refresh_round_evidence(path: str) -> list[dict]:
    """轮循环结束后重读轮次证据（runner 只在整轮之后落盘；见 D2② 冷跑协议）。

    快照副本可避免「读到别人正在覆盖的半截文件」，也让退出循环后的读取与循环内语义一致。
    """
    import shutil
    import tempfile

    from .engine.speedkit import driver as sk_driver

    try:
        with tempfile.NamedTemporaryFile(prefix="round-evidence-", suffix=".json", delete=False) as handle:
            snapshot = handle.name
        shutil.copyfile(path, snapshot)
    except OSError:
        return sk_driver.load_round_evidence(path)
    try:
        return sk_driver.load_round_evidence(snapshot)
    finally:
        try:
            Path(snapshot).unlink()
        except OSError:
            pass


def _merge_sidecars(rounds: list[dict]) -> dict:
    merged: dict = {}
    for record in rounds:
        for key, value in (record.get("sidecar") or {}).items():
            if value is None:
                continue
            if key in merged and merged[key] == value:
                continue
            merged[key] = value
    return merged


def _switches_line(evidence: dict) -> str | None:
    env = (evidence or {}).get("env") or {}
    if not env:
        return None
    return ", ".join(f"{k}={env[k]}" for k in sorted(env))


def _install_label() -> str:
    import subprocess

    from .engine import speedkit as _sk  # noqa: F401

    try:
        branch = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True,
                                text=True, timeout=10, check=False).stdout.strip()
        rev = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True,
                             text=True, timeout=10, check=False).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return f"{branch}@{rev}" if branch and rev else (rev or "unknown")


def _corpus_max_bytes(corpus: list[dict]) -> int:
    """语料最大件字节数（D6④ 判据的比较基准；取不到按 0，即不比较）。"""
    sizes = []
    for item in corpus:
        try:
            sizes.append(Path(str(item.get("path"))).stat().st_size)
        except OSError:
            continue
    return max(sizes) if sizes else 0


def _batch_upload_checks(rounds: list[dict], corpus_max_bytes: int) -> list[dict]:
    """D6④：batch 上传体量的机读证据——经反向代理的大件上传不得被截断/限流。

    判据只认「后端实际收到的最大请求体量」（`batch.max_upload_bytes`，来自 job 条目回吐）：
    缺键/非正数 ⇒ FAIL；收到体量 < 语料最大件 ⇒ FAIL（被代理截断或上限过小）。
    被测对象主动拒收（413/504）的证据不在此处，而在 `D1.measurement_reachable`（FAIL + HTTP 码）。
    """
    timed = [r for r in rounds if not r.get("warmup") and not r.get("invalid")]
    if not timed:
        return []
    sizes: list[float | None] = []
    for record in timed:
        value = (record.get("batch") or {}).get("max_upload_bytes")
        sizes.append(float(value) if isinstance(value, (int, float)) and value > 0 else None)
    if any(size is None for size in sizes):
        return [{"id": "D6.batch_upload_bytes", "status": "FAIL",
                 "detail": f"batch 计时轮缺 max_upload_bytes（D6④ 要求记录经代理的实际上传体量）：{sizes}",
                 "value": None}]
    largest = max(sizes)
    if corpus_max_bytes and largest < corpus_max_bytes:
        return [{"id": "D6.batch_upload_bytes", "status": "FAIL",
                 "detail": (f"经代理收到的最大上传体量 {largest:.0f} B < 语料最大件 {corpus_max_bytes} B"
                            "（被代理截断或上限过小）"),
                 "value": largest}]
    return [{"id": "D6.batch_upload_bytes", "status": "PASS",
             "detail": (f"经代理最大上传体量 {largest:.0f} B ≥ 语料最大件 {corpus_max_bytes} B"
                        "（HTTP 状态另见 D1.measurement_reachable / D1.round_validity）"),
             "value": largest}]


ANCHOR_TOLERANCE = 0.20
SELF_SUFFICIENCY_ITEMS = (
    ("mode", "driver"), ("mode", "page_concurrency"), ("mode", "cache_policy"),
    ("mode", "warmup_rounds"), ("mode", "timing_rounds"), ("mode", "payload_sha256"),
    ("mode", "api_base"),
)


ANCHOR_RATIO_MIN = 1.8
BASELINE_TOLERANCE = 0.20


def _speed_anchor_observation(arm: dict, summary: dict) -> dict:
    """P4：锚点绝对带只做**观测**（`out_of_band` 标记 + 记录当前值 + 声明值）。

    验收方案 §7 明写：198.13s/86.65s 历史带**退为观测**、不充当任何判据、无 re-anchor 逃生舱；
    因此这里绝不进 `checks`，也不影响四态判定。
    """
    declared = arm.get("expect_p50_s")
    measured = (summary.get("file_wall_s") or {}).get("p50")
    if declared is None or not isinstance(measured, (int, float)):
        return {}
    deviation = abs(float(measured) - float(declared)) / float(declared)
    return {"anchor_band": {
        "declared_p50_s": float(declared),
        "measured_p50_s": float(measured),
        "deviation": f"{deviation:.1%}",
        "out_of_band": bool(deviation > BASELINE_TOLERANCE),
        "note": "历史绝对带仅作 P4 观测，不改变任何 D 组判定（无 re-anchor 逃生舱）",
    }}


def _load_baseline(path: str | None) -> dict | None:
    """测量**之前**就校验 `--baseline` 可读（否则跑完一轮才发现前置错，白烧几分钟机器）。"""
    if not path:
        return None
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise _Usage(f"--baseline 报告不可读：{exc}") from exc
    if not isinstance(doc, dict):
        raise _Usage("--baseline 必须是 JSON 对象")
    return doc


def _speed_baseline_checks(doc: dict | None, *, mode: dict, summary: dict) -> list[dict]:
    """`--baseline` 的两类真判据（②冷评审 I16：AC1 比值与 D7 parity 都要有机器落点）。

    - 基线是**本模块的另一份** speed 报告（含 mode+summary）⇒ **D1①** 两臂 k 中位墙钟比值 ≥1.8×；
    - 基线是 `benchmark_throughput.py` 的产物（顶层 pages_per_min + page_ms_p50）⇒ **D7** 口径差 ≤20%。
    """
    if doc is None:
        return [{"id": "D7.parity", "status": "UNVERIFIED", "value": None,
                 "detail": ("未给 --baseline：本模块另一臂报告 ⇒ D1① 比值；"
                            "benchmark_throughput.py 产物 ⇒ D7 parity（两者都由本条出口判）")}]
    mine_p50 = (summary.get("file_wall_s") or {}).get("p50")
    if isinstance(doc.get("mode"), dict) and isinstance(doc.get("summary"), dict):
        other_mode, other_summary = doc["mode"], doc["summary"]
        k_mine, k_other = mode.get("page_concurrency"), other_mode.get("page_concurrency")
        other_p50 = (other_summary.get("file_wall_s") or {}).get("p50")
        if (isinstance(k_mine, int) and isinstance(k_other, int) and k_mine != k_other
                and isinstance(mine_p50, (int, float)) and isinstance(other_p50, (int, float))
                and mine_p50 and other_p50):
            slow, fast = (mine_p50, other_p50) if k_mine < k_other else (other_p50, mine_p50)
            ratio = float(slow) / float(fast)
            return [{"id": "D1.anchor_ratio",
                     "status": "PASS" if ratio >= ANCHOR_RATIO_MIN else "FAIL",
                     "detail": (f"k={min(k_mine, k_other)} 中位墙钟 {float(slow):.3f}s ÷ "
                                f"k={max(k_mine, k_other)} {float(fast):.3f}s = {ratio:.3f}×"
                                f"（阈值 ≥{ANCHOR_RATIO_MIN}×）"),
                     "value": ratio},
                    {"id": "D7.parity", "status": "UNVERIFIED", "value": None,
                     "detail": "基线是本模块另一臂报告（用于 D1① 比值）；D7 parity 需交 benchmark 产物"}]
        return [{"id": "D1.anchor_ratio", "status": "UNVERIFIED",
                 "detail": (f"两臂 k 相同或数据缺失（本 k={k_mine}／基线 k={k_other}，"
                            f"p50 本 {mine_p50}／基线 {other_p50}）⇒ 比值无判别力"),
                 "value": None},
                {"id": "D7.parity", "status": "UNVERIFIED", "value": None,
                 "detail": "基线是本模块另一臂报告；D7 parity 需交 benchmark 产物"}]

    bench_ppm, bench_p50 = doc.get("pages_per_min"), doc.get("page_ms_p50")
    mine_ppm, mine_p50_ms = summary.get("pages_per_min"), summary.get("page_ms_p50")
    diffs: list[tuple[str, float]] = []
    for label, mine, bench in (("pages_per_min", mine_ppm, bench_ppm),
                               ("page_ms_p50", mine_p50_ms, bench_p50)):
        if isinstance(mine, (int, float)) and isinstance(bench, (int, float)) and bench:
            diffs.append((label, abs(float(mine) / float(bench) - 1.0)))
    if not diffs:
        return [{"id": "D7.parity", "status": "UNVERIFIED",
                 "detail": (f"--baseline 既非本模块报告也非 benchmark 产物（缺 pages_per_min/page_ms_p50）："
                            f"键 {sorted(doc)[:8]}"),
                 "value": None}]
    worst = max(diffs, key=lambda item: item[1])
    return [{"id": "D7.parity", "status": "PASS" if worst[1] <= BASELINE_TOLERANCE else "FAIL",
             "detail": ("对照 benchmark_throughput.py："
                        + "；".join(f"{label} 相对差 {value:.1%}" for label, value in diffs)
                        + f"（阈值 ≤{BASELINE_TOLERANCE:.0%}）"),
             "value": worst[1]}]

def _speed_self_sufficiency_checks(*, mode: dict, arm: dict, summary: dict,
                                   evidence: dict) -> list[dict]:
    """D4②/D4③：报告自足性——重放与复核所需字段必须齐备，缺项点名 FAIL。

    冷跑证据只在 `cache_policy=cold` 时要求（warm 轮本就不重启后端）。
    """
    missing: list[str] = []
    for section, key in SELF_SUFFICIENCY_ITEMS:
        if (mode or {}).get(key) in (None, ""):
            missing.append(f"{section}.{key}")
    for key in ("name", "kind"):
        if (arm or {}).get(key) in (None, ""):
            missing.append(f"arm.{key}")
    for key in ("file_wall_s", "pages_per_min", "page_ms_p50", "pages_measured_timing"):
        if (summary or {}).get(key) in (None, ""):
            missing.append(f"summary.{key}")
    if str((mode or {}).get("cache_policy")) == "cold" and not (evidence or {}).get("rounds"):
        missing.append("cold_evidence")
    return [{"id": "D4.self_sufficiency", "status": "PASS" if not missing else "FAIL",
             "detail": ("报告自足：重放/复核所需字段齐备" if not missing
                        else f"报告自足性缺项：{', '.join(missing)}"),
             "value": len(missing)}]


def _speed_corpus_premise_checks(corpus: list[dict], rounds: list[dict]) -> list[dict]:
    """D1④（AC2）：卷的「声明形态」必须被机检视图证实，而不是靠文件名或口头说明。

    三个机检视图/来源：本地 `pdftotext` 非空白字符数（`text_chars`）、后端 `/parse` 的 `is_scanned`，
    以及条目上可选的 `carrier_view`（`backend` / `local`：显式指定「哪个视图是对账权威」）。

    判定（逐条，只对声明了 `scanned_pdf`/`text_pdf` 的条目）：

    1. `carrier` 是**操作者显式声明**的（`carrier_source == "declared"`），且没给 `carrier_view`：
       默认拿**后端路由视图**对账（它才是决定走扫描链还是文本链的权威）——不符 ⇒ FAIL；相符 ⇒ PASS，
       本地文本层计数降级为 detail 里的观测项（不再一票否决）。
    2. 显式给了 `carrier_view`：只拿该视图对账——不符 ⇒ FAIL；相符 ⇒ PASS（另一视图记为观测项）；
       该视图取不到（无 `pdftotext` / 无 `/parse` 旁证）⇒ UNVERIFIED，不猜 PASS。
    3. `carrier` 是**本地推定**的（`carrier_source == "inferred_local"`，清单没写 `carrier`）：
       没人声明过形态，故后端不一致**不判 FAIL**——判 **UNVERIFIED** 并点名两视图 + 给出已知形态提示
       （「扫描件 + 内嵌文本层」：本地文本层有内容、后端按整页底图判扫描；#115 全库重判显示真实案卷
       229/256 属此形态，两侧视图都没错）。要按后端路由判定，请在清单里显式写 `carrier` 与
       （可选）`carrier_view: "backend"`。
    4. 后端旁证取不到且是显式声明：退回本地视图对账（不符 ⇒ FAIL）；两视图都取不到 ⇒ UNVERIFIED。

    即：口径分歧不再被当成「声明错」判 FAIL（Issue#153：本地文本层有内容 ≠ 后端不该按底图路由），
    但真正的声明错误（显式声明与权威视图矛盾）仍然判红。
    """
    parse_view: dict[str, dict] = {}
    for rnd in rounds:
        for item in (rnd.get("items") or []):
            if item.get("id") is not None and isinstance(item.get("parse"), dict):
                parse_view[item["id"]] = item["parse"]

    mismatch: list[str] = []
    unknown: list[str] = []
    mixed: list[str] = []
    observations: list[str] = []
    confirmed: list[str] = []
    for entry in corpus:
        declared = str(entry.get("carrier") or "unknown")
        if declared not in ("scanned_pdf", "text_pdf"):
            continue
        want_scanned = declared == "scanned_pdf"
        item_id = str(entry.get("id"))
        chars = entry.get("text_chars")
        meta = parse_view.get(entry.get("id"))
        backend_scanned = None if meta is None else bool(meta.get("is_scanned"))
        local_scanned = None if chars is None else (chars == 0)
        view = str(entry.get("carrier_view") or "")

        if view:
            authoritative = local_scanned if view == "local" else backend_scanned
            other_value = (
                f"is_scanned={backend_scanned}" if view == "local"
                else f"pdftotext_nonblank_chars={chars}"
            ) if (backend_scanned if view == "local" else local_scanned) is not None else None
            if authoritative is None:
                unknown.append(f"{item_id}：声明 carrier_view={view}，但该视图取不到"
                               f"（{'无 pdftotext 计数' if view == 'local' else '无后端 /parse 旁证'}）")
            elif authoritative != want_scanned:
                mismatch.append(f"{item_id}：声明 {declared}，但权威视图 {view} 测得"
                                f"{'是扫描件' if authoritative else '是文本层'}")
            else:
                confirmed.append(item_id)
                if other_value is not None:
                    observations.append(f"{item_id}：{other_value}（view={view} 口径下的观测项，不判红）")
            continue

        if str(entry.get("carrier_source") or "declared") == "inferred_local":
            # 没人显式声明形态（carrier 由本地文本层推定）⇒ 后端不一致只报 UNVERIFIED，不判「声明错」。
            if backend_scanned is None:
                unknown.append(f"{item_id}：形态由本地推定（{declared}）且无后端 /parse 旁证")
            elif backend_scanned == want_scanned:
                confirmed.append(item_id)
            else:
                mixed.append(f"{item_id}：carrier 由本地文本层推定（{declared}，pdftotext 非空白={chars} 字），"
                             f"但后端 parse.is_scanned="
                             f"{backend_scanned}（判{'扫描' if backend_scanned else '文本'}）；"
                             "属「扫描件 + 内嵌文本层」已知形态（#115：真实案卷 229/256），"
                             "无人声明过形态故不判 FAIL——如要按后端路由判定，"
                             "请在清单里显式写 carrier 与（可选）carrier_view")
            continue

        # 显式声明：后端路由视图是权威，本地文本层计数降级为观测项。
        if backend_scanned is None:
            if local_scanned is None:
                unknown.append(f"{item_id}：本地无 pdftotext 非空白计数；无后端 /parse 旁证")
            elif local_scanned != want_scanned:
                mismatch.append(f"{item_id}：声明 {declared}，但本地测得"
                                f"{'是扫描件' if local_scanned else '是文本层'}"
                                f"（非空白 {chars} 字；后端旁证缺失）")
            else:
                unknown.append(f"{item_id}：本地确认 {declared}，但无后端 /parse 旁证")
            continue

        if backend_scanned != want_scanned:
            mismatch.append(f"{item_id}：声明 {declared}，但后端 parse.is_scanned={backend_scanned}"
                            f"（路由判{'扫描' if backend_scanned else '文本'}）"
                            + (f"，本地 pdftotext 非空白 {chars} 字"
                               if local_scanned is not None else ""))
            continue

        if local_scanned is None:
            unknown.append(f"{item_id}：后端确认 {declared}，但本地无 pdftotext 非空白计数")
            continue

        confirmed.append(item_id)
        if local_scanned != want_scanned:
            observations.append(f"{item_id}：pdftotext_nonblank_chars={chars}"
                                "（与后端路由不一致，按「扫描件 + 内嵌文本层」已知形态记为观测项，不判红）")

    if mismatch:
        return [{"id": "D1.corpus_premise", "status": "FAIL",
                 "detail": "声明形态与机检不符（测得不是它声明的形态）：" + "；".join(mismatch),
                 "value": len(mismatch)}]
    if unknown:
        return [{"id": "D1.corpus_premise", "status": "UNVERIFIED",
                 "detail": "前提未被机检全部证实：" + "；".join(unknown),
                 "value": len(unknown)}]
    if mixed:
        return [{"id": "D1.corpus_premise", "status": "UNVERIFIED",
                 "detail": "形态前提含已知「扫描件 + 内嵌文本层」形态，两视图不一致故不判 PASS/FAIL："
                           + "；".join(mixed),
                 "value": len(mixed)}]
    if not confirmed:
        return [{"id": "D1.corpus_premise", "status": "UNVERIFIED",
                 "detail": "清单没有声明 scanned_pdf/text_pdf 的条目，形态前提无从机检", "value": 0}]
    return [{"id": "D1.corpus_premise", "status": "PASS",
             "detail": f"{len(confirmed)} 件的声明形态与机检视图一致"
                       "（本地 pdftotext 非空白计数 + 后端 parse.is_scanned）"
                       + ("；观测项：" + "；".join(observations) if observations else ""),
             "value": len(confirmed)}]


def _speed_driver_key_checks(driver: str, summary: dict, rounds: list[dict], *,
                             corpus_max_bytes: int = 0) -> list[dict]:
    """D2④：single 报告不得含 batch 专有键；batch 必须含 jobs 面键；batch 另加 D6④ 上传体量证据。"""
    from .engine.speedkit import collect as sk_collect

    checks: list[dict] = []
    keys = set(summary) | {k for r in rounds for k in (r.get("batch") or {})}
    if driver == "single":
        stray = sorted(keys & sk_collect.BATCH_ONLY_KEYS)
        checks.append({"id": "D2.single_key_set", "status": "PASS" if not stray else "FAIL",
                       "detail": ("single 报告未含 batch 专有键" if not stray
                                  else f"single 报告出现 batch 专有键：{', '.join(stray)}"),
                       "value": len(stray)})
    else:
        batch = next((r.get("batch") for r in rounds if r.get("batch")), {}) or {}
        missing = [k for k in sk_collect.BATCH_REQUIRED_KEYS if batch.get(k) in (None, "")]
        checks.append({"id": "D2.batch_key_set", "status": "PASS" if not missing else "FAIL",
                       "detail": ("batch 报告含 job_id 与 job_queue_wait_ms" if not missing
                                  else f"batch 报告缺键：{', '.join(missing)}"),
                       "value": len(missing)})
        checks.extend(_batch_upload_checks(rounds, corpus_max_bytes))
    return checks


def _not_measured(args) -> list[str]:
    items = [
        "精度/质量分：本期不测（no_gt，gt 恒 None；质量字段出现即 FAIL）",
        "文本层文档级链：本期不测（属 #115/#127 面）",
        "侧车模型内部耗时（vLLM 服务端 CPU/GPU 时间）：后端不下发，不得凭空造键",
    ]
    if args.cache_policy == "warm":
        items.append("冷启动开销：warm 轮不测冷跑地板（cache_policy=warm）")
    # N5（②轮 Nit）：parity 由 `D7.parity` 这条**实测** check 承载（未给基线时 UNVERIFIED），
    # 这里只列「本期范围外」的三项，不再把「能力缺口」混进静态文案。
    return items


if __name__ == "__main__":
    raise SystemExit(main())
