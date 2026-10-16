"""T3 成绩单：metrics.json + details.jsonl + gt.jsonl → markdown 报告。

读跑批产物与 GT（同统一契约，arm="gt"），出：
- 分桶 × 臂 × kind 的 召回/误报率/单页耗时 表；
- 每臂接入成本小节（依赖/部署面/单页延迟，从 metrics + adapters 常量摘录）；
- 按 spec 预注册判据的自动判读（候选召回 ≥ LA、难例桶塌方、误报上界、
  单页成本不高于 LA、臂无效；metrics 缺 la 基线时报「判读不可用」而非放行）。

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


def _targets(buckets: list[str] | None) -> list[str]:
    """页面所属桶 + 全局桶 "(all)"（去重）。

    无桶页（`buckets` 为空）只归 "(all)"；若不在这里去重，
    `list(buckets) + ["(all)"]` 会让无桶页把 (all) 计两次（绝对值与 FP/页 翻倍）。
    """
    out = list(buckets or [])
    if "(all)" not in out:
        out.append("(all)")
    return out


def aggregate(
    details: list[dict],
    gt_rows: list[dict],
    page_buckets: dict[str, list[str]],
) -> tuple[dict, dict]:
    """聚合 (arm, bucket, kind) → tp/fp/fn 与 (arm) → 单页耗时。

    bucket 维度：页面归属 manifest 桶（一页多桶则各桶都计）；无桶归 "(all)"。
    返回 (cells, arm_latency)：cells[(arm, bucket, kind)] = {"tp","fp","fn"}。
    单页耗时口径 = **ok 页**（该 (arm,page) 上无任何 failed 记录）的墙钟均值；
    失败页（含重试/超时的墙钟）一律不计入，其 GT 按全漏检（FN）计。
    """
    by_page_kind: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in gt_rows:
        by_page_kind[(row["page_id"], row["kind"])].append(row)

    # 页失败索引：单次分组（**任一记录 failed 即整页失败**，与跑批侧
    # metrics.failed_pages 口径一致），避免逐页重扫的 O(n²)
    status_by_arm_page: dict[tuple[str, str], list[str]] = defaultdict(list)
    for row in details:
        status_by_arm_page[(row["arm"], row["page_id"])].append(str(row.get("status") or ""))
    failed_pages: dict[tuple[str, str], bool] = {
        key: any(s == "failed" for s in statuses)
        for key, statuses in status_by_arm_page.items()
    }

    cells: dict[tuple[str, str, str], dict[str, int]] = defaultdict(lambda: {"tp": 0, "fp": 0, "fn": 0})
    pages_by_arm: dict[str, set[str]] = defaultdict(set)
    page_time: dict[tuple[str, str], float] = {}

    # 跑批明细按 (page, arm, kind) 分组做匹配
    pred_rows: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for row in details:
        arm, page_id = row["arm"], row["page_id"]
        pages_by_arm[arm].add(page_id)
        if failed_pages.get((arm, page_id)):
            # 失败页：不记耗时、不进匹配（GT 侧统一记 FN）
            continue
        if row.get("elapsed_ms", 0) > 0:
            key = (arm, page_id)
            page_time[key] = max(page_time.get(key, 0.0), row["elapsed_ms"])
        if not row.get("kind"):
            continue
        pred_rows[(page_id, arm, row["kind"])].append(row)

    arms = sorted(pages_by_arm)
    for arm in arms:
        for (page_id, kind), gt_list in by_page_kind.items():
            targets = _targets(page_buckets.get(page_id))
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
    # 无 GT 的 (page, kind) 上的预测全是 FP（GT 说没有，臂却框了）。
    # 必须在臂循环**之外**做：放在循环内会把同一行按臂数重复累加（每臂 ×N 臂），
    # 且各臂放大倍数不同 → 误报上界门可翻面。
    covered = set(by_page_kind)
    for (page_id, row_arm, kind), preds in pred_rows.items():
        if (page_id, kind) in covered:
            continue
        for bucket in _targets(page_buckets.get(page_id)):
            cells[(row_arm, bucket, kind)]["fp"] += len(preds)
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


def arm_notes(
    arm: str,
    cells: dict,
    metrics: dict,
    buckets: list[str],
    kinds: tuple[str, ...] = spec.KINDS,
) -> list[str]:
    """按 Issue#92 预注册判据给单臂判读（返回问题清单，空 = 全部通过）。

    判据（全部以 LA 为基线；LA 自身只给数据不判）：
    - **召回 ≥ LA**（首要判据，按该臂实际覆盖的 kind）；
    - **难例桶不塌方**（较 LA 同桶下降 > COLLAPSE_DROP_PP pp）；
    - **误报/页 ≤ 2×LA**（LA 为 0 时绝对上限 0.5/页）；
    - **单页成本不高于 LA**（首要判据之二）；
    - 失败页 > 10% 判臂无效。

    metrics 里没有 `la` 时**不打印通过**，改为明确报「基线缺失，判读不可用」——
    否则跑批时用 `benchmark_t3.py --arms <子集>` 或未跑基线的复算会静默全过。
    臂的 kind 覆盖面取 metrics.by_kind > 0（如 mineru_seal 只出 seal、sigdet 只出
    signature），避免把「本臂不覆盖该类型」误判成召回 0 失败；by_kind 缺失时退化为
    全部 kind（保守）。**边界**：若某臂在它本该覆盖的 kind 上真的 0 出框（多类型候选臂），
    该 kind 会被跳过 ⇒ 需人工复核（当前四臂均为单类型：la 双通道，其余单类型）。
    """
    stats = metrics.get(arm, {})
    notes: list[str] = []
    if spec.is_arm_invalid(stats.get("failed_pages", 0), stats.get("pages", 0)):
        notes.append(
            f"**臂无效**：失败页 {stats.get('failed_pages')}/{stats.get('pages')}"
            f" > {spec.ARM_FAIL_PAGE_RATIO:.0%}"
        )
    if arm == "la":
        return notes
    if "la" not in metrics:
        notes.append("**基线缺失**：metrics 无 `la` 臂，召回/误报/成本判据不可用")
        return notes
    if not any(key[0] == arm for key in cells):
        notes.append("**无明细记录**：details 中没有该臂的任何记录，判据不可用")
        return notes
    by_kind = stats.get("by_kind") or {}
    covered = [k for k in kinds if int(by_kind.get(k, 0)) > 0] or list(kinds)
    for kind in covered:
        all_cell = cells.get((arm, "(all)", kind), {"tp": 0, "fp": 0, "fn": 0})
        la_cell = cells.get(("la", "(all)", kind), {"tp": 0, "fp": 0, "fn": 0})
        # 首要判据 1：候选召回 ≥ LA
        a_gt = all_cell["tp"] + all_cell["fn"]
        la_gt = la_cell["tp"] + la_cell["fn"]
        if a_gt and la_gt:
            a_recall = 100 * all_cell["tp"] / a_gt
            la_recall = 100 * la_cell["tp"] / la_gt
            if a_recall < la_recall:
                notes.append(
                    f"**召回低于 LA** {kind}：{a_recall:.1f}% < {la_recall:.1f}%"
                    "（不满足「召回 ≥ LA」）"
                )
        # 难例桶塌方：候选臂 vs LA 同桶召回
        for bucket in buckets:
            if bucket == "(all)":
                continue
            cell = cells.get((arm, bucket, kind), {"tp": 0, "fp": 0, "fn": 0})
            la_bucket = cells.get(("la", bucket, kind), {"tp": 0, "fp": 0, "fn": 0})
            b_gt = cell["tp"] + cell["fn"]
            la_b_gt = la_bucket["tp"] + la_bucket["fn"]
            if not b_gt or not la_b_gt:
                continue  # 任一侧同桶无 GT 召回不可比
            b_recall = 100 * cell["tp"] / b_gt
            la_b_recall = 100 * la_bucket["tp"] / la_b_gt
            if spec.is_bucket_collapsed(b_recall, la_b_recall):
                notes.append(
                    f"**桶塌方** {kind}/{bucket}：召回 {b_recall:.1f}% 较 LA 同桶 "
                    f"{la_b_recall:.1f}% 下降 {la_b_recall - b_recall:.1f}pp"
                    f"（> {spec.COLLAPSE_DROP_PP:.0f}pp）"
                )
        # 误报上界：候选臂 FP/页 vs LA 基线 FP/页
        la_pages = metrics["la"].get("pages", 0)
        if spec.is_fp_excessive(
            all_cell["fp"], stats.get("pages", 0), la_cell["fp"], la_pages
        ):
            cap = spec.fp_cap_per_page(la_cell["fp"], la_pages)
            notes.append(
                f"**误报上界** {kind}：FP/页 "
                f"{spec.fp_per_page(all_cell['fp'], stats.get('pages', 0)):.2f}"
                f" > 上界 {cap:.2f}"
                f"（LA 基线 {spec.fp_per_page(la_cell['fp'], la_pages):.2f}）"
            )
    # 首要判据 2：单页成本不高于 LA
    a_cost = stats.get("avg_page_elapsed_s")
    la_cost = metrics["la"].get("avg_page_elapsed_s")
    if a_cost is not None and la_cost is not None and a_cost > la_cost:
        notes.append(
            f"**单页成本高于 LA**：{a_cost * 1000:.0f} ms > {la_cost * 1000:.0f} ms"
            "（不满足「成本不高于 LA」）"
        )
    return notes


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
            for arm in arms:
                cell = cells.get((arm, bucket, kind), {"tp": 0, "fp": 0, "fn": 0})
                tp, fp, fn = cell["tp"], cell["fp"], cell["fn"]
                recall = _pct(tp, tp + fn)
                fp_rate = _pct(fp, tp + fp)
                # (all) 行分母 = 该臂实际跑的页数（metrics.pages）；全局 FP/页 正是
                # 误报上界门与判读使用的量，必须渲染出来（不能显示 "-"）。
                den = (
                    m.get(arm, {}).get("pages", 0)
                    if bucket == "(all)"
                    else bucket_pages.get(bucket, 0)
                )
                row.append(f"{recall} / {fp_rate} / {spec.fp_per_page(fp, den):.2f} (TP={tp},FP={fp},FN={fn})")
            lines.append("| " + " | ".join(row) + " |")
        lines.append("")

    lines.append("## 单页耗时（ok 页平均，ms）")
    lines.append("")
    lines.append("| 臂 | 平均 ms |")
    lines.append("|---|---|")
    for arm, ms in sorted(arm_latency.items()):
        lines.append(f"| {arm} | {ms} |")
    lines.append("")
    lines.append(
        "> 口径：**该臂 ok 页全体**（该 (臂,页) 上无任何 failed 记录，含零出框页）平均；"
        "每页耗时为该页首条记录的墙钟（跑批侧逐页记账，见 `benchmark_t3.py`）；失败页不计入。"
    )
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

    # ---- 自动判读（预注册阈值，spec.py；判据实现见 arm_notes） ------------
    lines.append("## 自动判读（预注册阈值）")
    lines.append("")
    if "la" not in m:
        lines.append(
            "> ⚠️ **基线缺失**：metrics 中没有 `la` 臂，召回/误报/成本判据**不可用**，"
            "下面的逐臂结论不构成「可替代」判读。"
        )
        lines.append("")
    for arm in arms:
        notes = arm_notes(arm, cells, m, buckets, tuple(kinds))
        if arm == "la" and not notes:
            lines.append("- **la**：基线臂（召回/误报/成本作候选臂判据基线，自身不判）")
        elif notes:
            lines.append(f"- **{arm}**：" + "；".join(notes))
        else:
            lines.append(
                f"- **{arm}**：通过全部预注册判据"
                "（召回 ≥ LA、难例桶不塌方、误报/页 ≤ 2×LA、单页成本不高于 LA）"
            )
    lines.append("")
    lines.append(
        "> 阈值与判据：候选臂桶召回较 LA 同桶下降 >15pp 判塌方、候选臂误报/页 ≤ 2×LA "
        "误报/页（LA 基线为 0 时绝对上限 0.5/页）、候选臂召回 ≥ LA 同 kind、候选臂单页"
        "成本不高于 LA、失败页 > 10% 判臂无效（`eval/benchmarks/t3/spec.py` 预注册；"
        "「通过」= 同时满足，单条塌方/误报结论不等于可替代）。"
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
