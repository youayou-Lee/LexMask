"""T3 成绩单：metrics.json + details.jsonl + gt.jsonl → markdown 报告。

读跑批产物与 GT（同统一契约，arm="gt"），出：
- 分桶 × 臂 × kind 的 召回/误报率/单页耗时 表；
- 每臂接入成本小节（依赖/部署面/单页延迟，从 metrics + adapters 常量摘录）；
- 按 spec 预注册阈值的自动判读（塌方 / 误报上界 / 臂无效）。

用法：
  PYTHONPATH=$PWD python3 eval/benchmarks/t3/report_t3.py \
    --metrics <out>/metrics.json --details <out>/details.jsonl \
    --gt testdata/benchmarks/t3/gt.jsonl --out report.md
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from eval.benchmarks.t3 import spec

ARM_INTEGRATION_COST = {
    "la": "复用现役 LA 服务（vLLM/HF，GPU），/detect 一次双通道请求；部署面零新增。",
    "mineru_seal": "复用喂Agent MinerU sidecar（16581），一页一任务（提交+轮询+zip），延迟受 pipeline 引擎主导。",
    "paddlex_seal": "本地 paddlex seal_recognition 产线（PaddleX 3.7.2，T4 已实测页像素坐标），pipeline 实例按名缓存避免 35-40s 冷启动逐页重付。",
    "sigdet": "独立 conditional-detr 小模型常驻（28150），CPU/GPU 皆可；CPU 扫描页分块推理分钟级（实测单页均值见下），延迟是选型权重的一部分。",
}


def load_jsonl(path: str | Path) -> list[dict]:
    rows: list[dict] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def page_bucket_index(manifest_path: str | Path | None) -> dict[str, list[str]]:
    if not manifest_path:
        return {}
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    return {m["page_id"]: list(m.get("buckets") or []) for m in manifest}


def aggregate(
    details: list[dict],
    gt_rows: list[dict],
    page_buckets: dict[str, list[str]],
) -> tuple[dict, dict]:
    """聚合 (arm, bucket, kind) → tp/fp/fn 与 (arm) → 单页耗时。

    bucket 维度：页面归属 manifest 桶（一页多桶则各桶都计）；无桶归 "(all)"。
    返回 (cells, arm_latency)：cells[(arm, bucket, kind)] = {"tp","fp","fn"}。
    """
    by_page_kind: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in gt_rows:
        by_page_kind[(row["page_id"], row["kind"])].append(row)

    cells: dict[tuple[str, str, str], dict[str, int]] = defaultdict(lambda: {"tp": 0, "fp": 0, "fn": 0})
    pages_by_arm: dict[str, set[str]] = defaultdict(set)
    page_time: dict[tuple[str, str], float] = {}

    # 跑批明细按 (page, arm, kind) 分组做匹配
    pred_rows: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for row in details:
        pages_by_arm[row["arm"]].add(row["page_id"])
        if row.get("elapsed_ms", 0) > 0:
            key = (row["arm"], row["page_id"])
            page_time[key] = max(page_time.get(key, 0.0), row["elapsed_ms"])
        if row.get("status") == "failed" or not row.get("kind"):
            continue
        pred_rows[(row["page_id"], row["arm"], row["kind"])].append(row)

    arms = sorted(pages_by_arm)
    failed_pages: dict[tuple[str, str], bool] = {}
    for (arm, page_id) in {(r["arm"], r["page_id"]) for r in details}:
        rows = [r for r in details if r["arm"] == arm and r["page_id"] == page_id]
        failed_pages[(arm, page_id)] = bool(rows) and all(
            r.get("status") == "failed" for r in rows
        )
    for arm in arms:
        for (page_id, kind), gt_list in by_page_kind.items():
            buckets = page_buckets.get(page_id) or ["(all)"]
            targets = list(buckets) + ["(all)"]
            if failed_pages.get((arm, page_id)):
                # 整页失败 → 全 GT 记 FN（失败不豁免，也不重复计匹配）
                for bucket in targets:
                    cells[(arm, bucket, kind)]["fn"] += len(gt_list)
                continue
            preds = pred_rows.get((page_id, arm, kind), [])
            score = spec.score_arm_kind(preds, gt_list)
            for bucket in targets:
                cell = cells[(arm, bucket, kind)]
                for key in ("tp", "fp", "fn"):
                    cell[key] += score[key]
        # 无 GT 的 (page, kind) 上的预测全是 FP（GT 说没有，臂却框了）
        covered = set(by_page_kind)
        for (page_id, arm, kind), preds in pred_rows.items():
            if (page_id, kind) in covered:
                continue
            buckets = page_buckets.get(page_id) or ["(all)"]
            for bucket in list(buckets) + ["(all)"]:
                cells[(arm, bucket, kind)]["fp"] += len(preds)
    arm_latency = {
        arm: round(
            sum(t for (a, _), t in page_time.items() if a == arm)
            / max(1, len([1 for (a, _) in page_time if a == arm])),
            1,
        )
        for arm in arms
    }
    return dict(cells), arm_latency


def _pct(num: int, den: int) -> str:
    return f"{100 * num / den:.1f}%" if den else "-"


def render_markdown(
    cells: dict,
    arm_latency: dict,
    metrics: dict,
    page_buckets: dict[str, list[str]],
    manifest_path: str | None,
) -> str:
    lines: list[str] = []
    m = metrics.get("metrics", metrics)
    arms = sorted(m)
    lines.append("# T3 四臂视觉检测对比 成绩单")
    lines.append("")
    lines.append(f"- manifest: `{manifest_path or "-"}`（{len(page_buckets)} 页）")
    lines.append("")

    # ---- 主表：分桶 × 臂 × kind -------------------------------------------
    lines.append("## 分桶 × 臂 × kind")
    lines.append("")
    buckets = sorted({b for (_, b, _) in cells})
    kinds = list(spec.KINDS)
    # 分桶 FP 只展示（判读一律用全局 FP/页，见 spec.is_fp_excessive）
    bucket_pages: dict[str, int] = {}
    for page_id, blist in page_buckets.items():
        for b in blist:
            bucket_pages[b] = bucket_pages.get(b, 0) + 1
    for kind in kinds:
        lines.append(f"### kind = {kind}")
        lines.append("")
        lines.append("| 桶 | " + " | ".join(f"{arm} 召回 / 误报率 / FP/页" for arm in arms) + " |")
        lines.append("|---|" + "---|" * len(arms))
        for bucket in buckets:
            row = [bucket]
            b_pages = bucket_pages.get(bucket, 0)
            for arm in arms:
                cell = cells.get((arm, bucket, kind), {"tp": 0, "fp": 0, "fn": 0})
                tp, fp, fn = cell["tp"], cell["fp"], cell["fn"]
                recall = _pct(tp, tp + fn)
                fp_rate = _pct(fp, tp + fp)
                fp_pp = spec.fp_per_page(fp, b_pages) if bucket != "(all)" else None
                fp_txt = f"{fp_pp:.2f}" if fp_pp is not None else "-"
                row.append(f"{recall} / {fp_rate} / {fp_txt} (TP={tp},FP={fp},FN={fn})")
            lines.append("| " + " | ".join(row) + " |")
        lines.append("")

    lines.append("## 单页耗时（ok 页平均，ms）")
    lines.append("")
    lines.append("| 臂 | 平均 ms |")
    lines.append("|---|---|")
    for arm, ms in sorted(arm_latency.items()):
        lines.append(f"| {arm} | {ms} |")
    lines.append("")

    # ---- 接入成本 ---------------------------------------------------------
    lines.append("## 每臂接入成本")
    lines.append("")
    for arm, text in ARM_INTEGRATION_COST.items():
        if arm in arms:
            # 实测单页均值动态带出（metrics avg_page_elapsed_s），不写死形容词
            avg = m[arm].get("avg_page_elapsed_s")
            avg_txt = f"（实测单页均值 {avg * 1000:.0f} ms）" if avg is not None else ""
            lines.append(f"- **{arm}**：{text}{avg_txt}")
    lines.append("")

    # ---- 自动判读（预注册阈值，spec.py） -----------------------------------
    lines.append("## 自动判读（预注册阈值）")
    lines.append("")
    for arm in arms:
        stats = m[arm]
        notes: list[str] = []
        if spec.is_arm_invalid(stats.get("failed_pages", 0), stats.get("pages", 0)):
            notes.append(
                f"**臂无效**：失败页 {stats.get('failed_pages')}/{stats.get('pages')}"
                f" > {spec.ARM_FAIL_PAGE_RATIO:.0%}"
            )
        for kind in kinds:
            all_cell = cells.get((arm, "(all)", kind), {"tp": 0, "fp": 0, "fn": 0})
            # 桶塌方：候选臂 vs **LA 同桶召回**（LA 为基线；LA 自身不判，只给数据行）
            if arm != "la" and "la" in m:
                for bucket in buckets:
                    if bucket == "(all)":
                        continue
                    cell = cells.get((arm, bucket, kind), {"tp": 0, "fp": 0, "fn": 0})
                    b_gt = cell["tp"] + cell["fn"]
                    la_cell = cells.get(("la", bucket, kind), {"tp": 0, "fp": 0, "fn": 0})
                    la_gt = la_cell["tp"] + la_cell["fn"]
                    if not b_gt or not la_gt:
                        continue  # 任一侧同桶无 GT 召回不可比
                    b_recall = 100 * cell["tp"] / b_gt
                    la_recall = 100 * la_cell["tp"] / la_gt
                    if spec.is_bucket_collapsed(b_recall, la_recall):
                        notes.append(
                            f"**桶塌方** {kind}/{bucket}：召回 {b_recall:.1f}% 较 LA 同桶 "
                            f"{la_recall:.1f}% 下降 {la_recall - b_recall:.1f}pp"
                            f"（> {spec.COLLAPSE_DROP_PP:.0f}pp）"
                        )
            # 误报上界：候选臂 FP/页 vs LA 基线 FP/页（LA 自身不判）
            if arm != "la" and "la" in m:
                la_cell = cells.get(("la", "(all)", kind), {"tp": 0, "fp": 0, "fn": 0})
                if spec.is_fp_excessive(
                    all_cell["fp"], stats.get("pages", 0),
                    la_cell["fp"], m["la"].get("pages", 0),
                ):
                    cap = spec.fp_cap_per_page(la_cell["fp"], m["la"].get("pages", 0))
                    notes.append(
                        f"**误报上界** {kind}：FP/页 "
                        f"{spec.fp_per_page(all_cell['fp'], stats.get('pages', 0)):.2f}"
                        f" > 上界 {cap:.2f}"
                        f"（LA 基线 {spec.fp_per_page(la_cell['fp'], m['la'].get('pages', 0)):.2f}）"
                    )
        lines.append(f"- **{arm}**：" + ("；".join(notes) if notes else "通过全部预注册阈值"))
    lines.append("")
    lines.append(
        "> 阈值：候选臂桶召回较 LA 同桶下降 >15pp 判塌方（LA 为基线，自身不判）、"
        "候选臂误报/页 ≤ 2×LA 误报/页（LA 基线为 0 时绝对上限 0.5/页）、"
        "失败页 > 10% 判臂无效（eval/benchmarks/t3/spec.py 预注册）。"
    )
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metrics", required=True)
    parser.add_argument("--details", required=True)
    parser.add_argument("--gt", required=True)
    parser.add_argument("--manifest", default=None, help="分桶归属（与跑批同 manifest）")
    parser.add_argument("--out", default=None, help="默认与 metrics 同目录 report.md")
    args = parser.parse_args(argv)

    metrics = json.loads(Path(args.metrics).read_text(encoding="utf-8"))
    details = load_jsonl(args.details)
    gt_rows = load_jsonl(args.gt)
    page_buckets = page_bucket_index(args.manifest)

    cells, arm_latency = aggregate(details, gt_rows, page_buckets)
    md = render_markdown(cells, arm_latency, metrics, page_buckets, args.manifest)
    out = Path(args.out) if args.out else Path(args.metrics).parent / "report.md"
    out.write_text(md, encoding="utf-8")
    print(f"[ok] {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
