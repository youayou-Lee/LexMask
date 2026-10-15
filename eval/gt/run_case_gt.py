"""单案 GT/评测集产线一键编排（Issue#105 G3，方案 §11.4 CLI）。

把产线的**确定性阶段**串成一条命令；Agent 阶段（逐页识别 / 软层分诊）是
接缝——本 CLI 准备任务书与清单、消费其产物，派发由 ZCode 会话执行
（子代理调用无法脱离会话进程，这是刻意设计而非缺陷）。

阶段与产物::

    render   PDF → pages/*.png + manifest.json（断点清单）
    compare  逐页结果零幻觉门（实体=转写逐字子串）+ 异源 OCR 文档级比对
    reduce   L0.5 归一回一（复用 reduce.semantic_equal，模型不参与）
    gate     软层裁定验证（四条件闸门，消费 adjudications/*.json）
    export   考卷导出（--types 类型勾选，eval-ng/t2 桶族 jsonl）

铁律：
- **零幻觉门不过 → 后续阶段全部拒绝**（宁可停，不可静默跑歪）；
- 所有阶段幂等，可 ``--from-step`` 断点续跑；
- 流水域判定用**内容签名**（交易表/对手方账户/信用卡还款），不是打印页脚
  ——xbg 补漏教训（2026-10-15）。

用法::

    python eval/gt/run_case_gt.py --case-dir <案卷> \\
        --results-dir <self-ner-eval/results> --out <self-ner-eval> \\
        --from-step compare --to-step export --types 姓名 金额
"""
from __future__ import annotations

import argparse
import collections
import glob
import json
import re
import sys
from pathlib import Path

from gt.export_exam import build_evalng_exam, build_exam, exam_summary, \
    write_evalng_exam, write_exam_jsonl
from gt.quality_metrics import build_quality_report

STAGES = ("render", "compare", "reduce", "gate", "export")

#: 流水域内容签名（≥2 处命中即判流水页；xbg 70 页反推的同款判据）
_FLOW_SIG = re.compile(
    r"信用卡还款|财付通|银盛支付|EP01000|R227000|B4S10000|交易时间|"
    r"对手方账户|\.htm|微信支付交易明细|农业银行\(7")


def is_flow_page(text: str) -> bool:
    return len(_FLOW_SIG.findall(text or "")) >= 2


def plan_stages(from_step: str, to_step: str, violations: int = 0) -> list[str]:
    """阶段切片 + 零幻觉熔断：违规 >0 时强制只重跑 compare 复检（宁可停）。"""
    if from_step not in STAGES or to_step not in STAGES:
        raise ValueError(f"未知阶段：{from_step!r} / {to_step!r}（合法：{STAGES}）")
    if violations > 0:
        return ["compare"]
    i, j = STAGES.index(from_step), STAGES.index(to_step)
    return list(STAGES[i:j + 1])


def _norm(s: str) -> str:
    return "".join((s or "").split())


# ---- 各阶段实现 ---------------------------------------------------------------

def _page_parts(f: str) -> tuple[str, int]:
    """``.../results/<stem>/pNNN.json`` → ``(stem, 0起页码)``（页号用正则，位数不限）。"""
    parts = Path(f).parts
    m = re.fullmatch(r"p(\d+)\.json", parts[-1])
    if not m:
        raise ValueError(f"结果文件名不符合契约 pNNN.json：{f}")
    return parts[-2], int(m.group(1)) - 1


def render_stage(case_dir: Path, out_dir: Path) -> dict:
    """PDF → pages/<stem>/pNNN.png（150dpi，已存在跳过）+ manifest.json。"""
    import subprocess
    pages_dir = out_dir / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)
    manifest = []
    for pdf in sorted(glob.glob(str(Path(case_dir) / "*.pdf"))):
        stem = Path(pdf).stem
        n = int(subprocess.check_output(["pdfinfo", pdf]).decode()
                .split("Pages:")[1].split()[0])
        for i in range(1, n + 1):
            png = pages_dir / stem / f"p{i:03d}.png"
            png.parent.mkdir(parents=True, exist_ok=True)
            if not png.exists():
                subprocess.run(["pdftoppm", "-f", str(i), "-l", str(i), "-r", "150",
                                "-png", "-singlefile", pdf, str(png)[:-4]],
                               check=True)
            manifest.append({"pdf": Path(pdf).name, "stem": stem, "page_no": i - 1,
                             "png": str(png),
                             "result": str(results_dir_for(out_dir) / stem / f"p{i:03d}.json")})
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"pdfs": len(manifest) and len({m['pdf'] for m in manifest}),
            "pages": len(manifest), "manifest": str(out_dir / "manifest.json")}


def results_dir_for(out_dir: Path) -> Path:
    return out_dir / "results"


def compare_stage(results_dir: Path, vl_md_template: str | None = None) -> dict:
    """零幻觉门（实体=本页转写逐字子串）。返回统计并写 compare_report.jsonl。"""
    rows, stats = [], collections.Counter()
    for f in sorted(glob.glob(str(results_dir / "*" / "*.json"))):
        stem, page = _page_parts(f)
        try:
            d = json.load(open(f, encoding="utf-8"))
        except Exception as e:
            rows.append({"page": f"{stem}#{page}", "error": str(e)})
            stats["bad_json"] += 1
            continue
        text = d.get("text", "")
        bad = [e for e in d.get("entities", []) if e.get("text") not in text]
        stats["entities"] += len(d.get("entities", []))
        stats["violations"] += len(bad)
        rows.append({"page": f"{stem}#{page}", "entities": len(d.get("entities", [])),
                     "invalid": bad})
    report = {"entities": stats["entities"], "violations": stats["violations"],
              "zero_hallucination": stats["violations"] == 0}
    out = results_dir.parent / "compare_report.jsonl"
    with open(out, "w", encoding="utf-8") as fo:
        for r in rows:
            fo.write(json.dumps(r, ensure_ascii=False) + "\n")
    return report


def reduce_and_gate_stages(results_dir: Path, out_dir: Path) -> dict:
    """reduce/gate 的批处理入口：消费/产出 caseData 侧既有脚本同款文件。

    （这两阶段的逐条判定逻辑在 gt.reduce / 闸门脚本中，已是纯函数并有
    单测；此处负责按页批读、写回 human_review/adjudications 产物路径。）
    """
    review = out_dir / "human_review.jsonl"
    n = sum(1 for _ in open(review, encoding="utf-8")) if review.exists() else 0
    return {"human_review_entries": n, "executed": False,
            "note": "本阶段未执行逐条判定——逐条判定由 gt.reduce/gate 纯函数承担，"
                    "批编排按产物文件接续（会话侧波次产出 human_review/adjudications 后此处只校验计数）"}


def export_stage(results_dir: Path, out_path: Path, types: set[str] | None,
                 version: str, keep_empty: bool = False,
                 flow_scope_file: Path | None = None,
                 evalng_out: Path | None = None) -> tuple[list[dict], dict]:
    """读 results/*.json → 剔流水域 → 类型勾选 → 写考卷 jsonl。

    流水域判定 = 内容签名（is_flow_page）∪ 显式清单（flow_scope_file，
    phase1-scope.json 的 flow_pages——内容判据难免漏网，人工圈定优先）。
    ``evalng_out`` 给出时同步写一份 eval-ng 考卷 JSON（exam_id 取考卷文件
    名去后缀），一条命令产出两种消费形态。
    """
    explicit: set[tuple[str, int]] = set()
    if flow_scope_file and Path(flow_scope_file).exists():
        scope = json.load(open(flow_scope_file, encoding="utf-8"))
        for rel in scope.get("phase1_excluded", []):
            explicit.add(_page_parts(str(results_dir.parent / rel)))
    pages, excluded = [], set()
    for f in sorted(glob.glob(str(results_dir / "*" / "*.json"))):
        stem, page_no = _page_parts(f)
        d = json.load(open(f, encoding="utf-8"))
        if is_flow_page(d.get("text", "")) or (stem, page_no) in explicit:
            excluded.add((stem, page_no))
            continue
        pages.append(d)
    rows = build_exam(pages, types=types, exclude_pages=excluded,
                      keep_empty=keep_empty)
    write_exam_jsonl(rows, out_path, version=version)
    if evalng_out is not None:
        if not rows:
            print("[warn] 勾选/剔域后 0 页，evalng 考卷将为空（eval-ng 会拒绝执行）",
                  file=sys.stderr)
        write_evalng_exam(
            build_evalng_exam(rows, exam_id=Path(out_path).stem),
            evalng_out)
    return rows, exam_summary(rows)


# ---- CLI ----------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(
        description="单案 GT/评测集产线一键编排",
        epilog="注意：--report-only 的 agent_error 抽检由会话侧填入，"
               "CLI 报告里该项恒 available:false，不代表抽检通过。")
    ap.add_argument("--case-dir", required=True)
    ap.add_argument("--results-dir", default=None,
                    help="逐页结果目录（默认 <case-dir>/self-ner-eval/results）")
    ap.add_argument("--out", default=None,
                    help="产物目录（默认 <case-dir>/self-ner-eval）")
    ap.add_argument("--from-step", default="render", choices=STAGES)
    ap.add_argument("--to-step", default="export", choices=STAGES)
    ap.add_argument("--types", nargs="*", default=None,
                    help="考卷勾选类型（缺省=全类型）")
    ap.add_argument("--exam-version", default="v0")
    ap.add_argument("--flow-scope-file", default=None,
                    help="人工圈定的流水域清单（phase1-scope.json，与内容签名取并集）")
    ap.add_argument("--report-only", action="store_true",
                    help="只出质量三数报告（G5），不执行任何阶段")
    a = ap.parse_args()

    eval_dir = Path(a.out) if a.out else Path(a.case_dir) / "self-ner-eval"
    results_dir = Path(a.results_dir) if a.results_dir else eval_dir / "results"

    # 熔断检查：既有 compare 报告里若已有违规，拒绝往前走
    violations = 0
    report_path = eval_dir / "compare_report.jsonl"
    if report_path.exists():
        for line in open(report_path, encoding="utf-8"):
            r = json.loads(line)
            violations += len(r.get("invalid", []))
    if a.report_only:
        adjudications = []
        adj_path = eval_dir / "adjudications.jsonl"
        if adj_path.exists():
            adjudications = [json.loads(l) for l in open(adj_path, encoding="utf-8")]
        # 上游关停数 = 总实体 - 软层前残留数；pre_soft 缺失 → 如实缺数（评审：
        # 报"100% 消化"是最危险的编造）
        pre_soft = eval_dir / "human_review.pre_soft.jsonl"
        upstream_closed = None
        total_ents = sum(len(json.load(open(f, encoding="utf-8")).get("entities", []))
                         for f in glob.glob(str(results_dir / "*" / "*.json")))
        if pre_soft.exists():
            residual = sum(1 for _ in open(pre_soft, encoding="utf-8"))
            upstream_closed = max(total_ents - residual, 0)
        rep = build_quality_report(
            adjudications=adjudications,
            upstream_closed=upstream_closed,
            total_entities=total_ents,
            spot_check=[],  # 抽检结果由会话侧填入时走 quality_metrics.build_quality_report
            same_source=None,
        )
        print(json.dumps({"quality_report": rep}, ensure_ascii=False, indent=1))
        return 0

    stages = plan_stages(a.from_step, a.to_step, violations=violations)
    forced_recheck = violations > 0 and stages == ["compare"]
    for stage in stages:
        if stage == "render":
            rep = render_stage(Path(a.case_dir), eval_dir)
            print(json.dumps({"stage": stage, **rep}, ensure_ascii=False))
        elif stage == "compare":
            rep = compare_stage(results_dir)
            print(json.dumps({"stage": stage, **rep}, ensure_ascii=False))
            if not rep["zero_hallucination"]:
                print("零幻觉门未过，链路停止。", file=sys.stderr)
                return 2
            if forced_recheck and (a.from_step, a.to_step) != ("compare", "compare"):
                print("复检通过（旧报告违规已被修复）。请重跑原命令继续后续阶段。",
                      file=sys.stderr)
                return 0
        elif stage in ("reduce", "gate"):
            print(json.dumps({"stage": stage,
                              **reduce_and_gate_stages(results_dir, eval_dir)},
                             ensure_ascii=False))
        elif stage == "export":
            types = set(a.types) if a.types else None
            evalng_path = eval_dir / f'exam-{a.exam_version}-evalng.json'
            rows, summary = export_stage(
                results_dir, eval_dir / f"exam-{a.exam_version}.jsonl",
                types=types, version=a.exam_version,
                flow_scope_file=Path(a.flow_scope_file) if a.flow_scope_file else None,
                evalng_out=evalng_path)
            print(json.dumps({"stage": stage, **summary,
                              "exam": str(eval_dir / f'exam-{a.exam_version}.jsonl'),
                              "evalng_exam": str(evalng_path)},
                             ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
