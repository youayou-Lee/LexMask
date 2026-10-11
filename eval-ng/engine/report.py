"""统一报告版式（md/json）：成绩单 + 环境标注 + 阶段分解 + runs 数。

环境标注为硬校验（AC4）：渲染前二次断言，缺项抛 MissingEnvAnnotation。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .runner import MissingEnvAnnotation


def _assert_env(result: dict) -> dict:
    env = result.get("env")
    missing = env.missing_fields() if env is not None else ["env(整体缺失)"]
    if missing:
        raise MissingEnvAnnotation(f"报告缺环境标注 {missing}（AC4）")
    return {
        "hardware": env.hardware,
        "stack_versions": env.stack_versions,
        "topology": env.topology,
        **({"extra": env.extra} if env.extra else {}),
    }


def _gate_rows(result: dict) -> list[tuple[str, str, str]]:
    rows = []
    for gr in result.get("gates", []):
        rows.append((gr.name, "PASS" if gr.passed else "FAIL", json.dumps(gr.details, ensure_ascii=False)))
    if not rows:
        rows.append(("(无闸门)", "—", "—"))
    return rows


def render_report(result: dict, fmt: str) -> str:
    env = _assert_env(result)
    agg = result.get("aggregated", {})
    overall = agg.get("last_run_full", {}).get("overall", {})
    if fmt == "json":
        payload = {
            "participant": result["participant"],
            "exam_id": result["exam_id"],
            "runs": result["runs"],
            "concurrency": result.get("concurrency", 1),
            "gate_pass": result["gate_pass"],
            "aggregated": {k: v for k, v in agg.items() if k != "last_run_full"},
            "overall_last_run": overall,
            "perf": result.get("perf", {}),
            "gates": [{"name": gr.name, "passed": gr.passed, "details": gr.details}
                      for gr in result.get("gates", [])],
            "errors": result.get("errors") or [],
            "env": env,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)
    if fmt == "md":
        lines = [
            f"# eval-ng 评测报告：{result['exam_id']} × {result['participant']}",
            "",
            f"- runs：**{result['runs']}**（报告层必输出）",
            f"- 闸门总判定：**{'PASS' if result['gate_pass'] else 'FAIL'}**",
            "",
            "## 环境标注（缺任一项本报告不可能生成）",
            f"- 硬件：{env['hardware']}",
            f"- 版本：{json.dumps(env['stack_versions'], ensure_ascii=False)}",
            f"- 拓扑：{env['topology']}",
            "",
            "## 成绩（N 轮聚合）",
        ]
        if agg.get("f1"):
            lines.append(
                f"- F1 mean/std：{agg['f1']['mean']:.4f} / {agg['f1']['std']:.4f}"
                f"（P {agg['precision']['mean']:.4f}，R {agg['recall']['mean']:.4f}）"
            )
        perf = result.get("perf", {})
        ws = perf.get("wall_s", {})
        conc = result.get("concurrency", 1)
        if ws:
            speed_note = (f"（并发 {conc} 路测得，含排队等待，不与串行数据对比）"
                          if conc > 1 else "")
            lines.append(
                f"- 速度：mean {ws['mean']:.3f}s / p95 {ws['p95']:.3f}s，"
                f"吞吐 {perf['throughput_pages_per_min']:.1f} 页/分{speed_note}"
            )
        lines += ["", "## 闸门", "| 闸门 | 判定 | 详情 |", "|---|---|---|"]
        for name, verdict, details in _gate_rows(result):
            lines.append(f"| {name} | {verdict} | {details} |")
        errors = result.get("errors") or []
        if errors:
            lines += ["", f"## 错题清单（前 {min(len(errors), 30)} 条 / 共 {len(errors)}）",
                      "| 题目 | 类型 | 类别 | 期望 | 实际 |", "|---|---|---|---|---|"]
            for row in errors[:30]:
                lines.append(f"| {row['page_id']} | {row['type']} | {row['kind']} "
                             f"| {row['expect']} | {row['actual']} |")
        return "\n".join(lines) + "\n"
    raise ValueError(f"未知报告格式 {fmt!r}（支持 md/json）")


def save_report(result: dict, out_dir: Path) -> tuple[Path, Path]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = f"{ts}-{result['exam_id']}-{result['participant']}"
    md_path = out_dir / f"{base}.md"
    json_path = out_dir / f"{base}.json"
    md_path.write_text(render_report(result, "md"), encoding="utf-8")
    json_path.write_text(render_report(result, "json"), encoding="utf-8")
    return md_path, json_path
