"""T3 GT 标注核对页生成器：页图 + 预标框叠加 + 浏览器内增删改 + 导出 gt.jsonl。

生成单文件静态 HTML（无依赖、无服务），打开后：
- 每页一节：页图（页像素 1:1 坐标）+ 预标注框（绿=seal、橙=signature）；
- 点击图内拖拽画新框（选 kind 后拖动）；点框选中可删除 / 改 kind；
- 「导出 gt.jsonl」按钮按统一契约（spec.py）拼 JSON 并触发下载。
预标来源：跑批明细或已有 gt.jsonl（--prefill 任一契约 jsonl，建议只喂
单臂做核对：--prefill-arm la 只叠 LA 框当草稿）。

导出脚本（可选，浏览器下载文件 → 落盘）：
  python3 gt_review.py export --downloaded gt.jsonl.download --out gt.jsonl
  （做 schema 校验后原子落盘；不合法退出码 1）

用法：
  python3 gt_review.py build --pages-dir testdata/benchmarks/t3/pages \
    --prefill <details.jsonl|gt.jsonl> [--prefill-arm la] --out gt_review.html
"""

from __future__ import annotations

import argparse
import html
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from eval.benchmarks.t3 import spec

_KIND_COLORS = {"seal": "#16a34a", "signature": "#ea580c"}


def collect_prefill(prefill_path: str, arm: str | None) -> dict[str, list[dict]]:
    """契约 jsonl → page_id → 预标框列表（可选按臂过滤）。"""
    by_page: dict[str, list[dict]] = {}
    for line in Path(prefill_path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("status") == "failed":
            continue
        if arm and row.get("arm") != arm:
            continue
        errors = spec.validate_record(row)
        if errors:
            continue  # 预标容错：坏行跳过，不阻塞生成
        by_page.setdefault(row["page_id"], []).append(
            {"kind": row["kind"], "box": row["box"], "from_arm": row.get("arm", "gt")}
        )
    return by_page


def page_images(pages_dir: str) -> list[Path]:
    return sorted(
        p for p in Path(pages_dir).iterdir()
        if p.suffix.lower() in (".png", ".jpg", ".jpeg") and p.is_file()
    )


_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<title>T3 GT 标注核对</title>
<style>
  body {{ font-family: sans-serif; margin: 16px; }}
  .page {{ margin-bottom: 40px; }}
  .wrap {{ position: relative; display: inline-block; border: 1px solid #999; }}
  img {{ display: block; }}
  svg.overlay {{ position: absolute; left: 0; top: 0; pointer-events: none; }}
  rect.box {{ fill: none; stroke-width: 2; }}
  rect.sel {{ stroke-dasharray: 4 2; stroke-width: 3; }}
  .controls {{ margin: 6px 0; }}
  .kind-picker button {{ margin-right: 6px; }}
  button {{ cursor: pointer; }}
  .export {{ position: fixed; top: 10px; right: 10px; padding: 8px 14px; }}
</style></head><body>
<button class="export" onclick="exportGt()">导出 gt.jsonl</button>
<h1>T3 GT 标注核对（页像素坐标，左上原点）</h1>
<div id="pages"></div>
<script>
const KIND_COLORS = {seal: "#16a34a", signature: "#ea580c"};
let currentKind = "seal";
const state = __PAGES_DATA__;  // [{page_id, src, w, h, boxes:[{kind, box:{x,y,w,h}, from_arm}]}]

function render() {
  const root = document.getElementById("pages");
  root.innerHTML = "";
  state.forEach((page, pi) => {
    const section = document.createElement("div");
    section.className = "page";
    section.innerHTML = `<h2>${page.page_id}</h2>
      <div class="controls">新框 kind：
        <span class="kind-picker">
          <button onclick="currentKind='seal'">seal</button>
          <button onclick="currentKind='signature'">signature</button>
        </span>
        （在图上拖拽画框；点击框选中后可删除）
      </div>
      <div class="wrap">
        <img src="${page.src}" width="${page.w}" height="${page.h}" data-pi="${pi}">
        <svg class="overlay" width="${page.w}" height="${page.h}"></svg>
      </div>`;
    root.appendChild(section);
    drawBoxes(pi);
    wireDrag(section.querySelector("img"), pi);
  });
}

function drawBoxes(pi) {
  const svg = document.querySelectorAll("svg.overlay")[pi];
  svg.innerHTML = "";
  state[pi].boxes.forEach((b, bi) => {
    const rect = document.createElementNS("http://www.w3.org/2000/svg", "rect");
    rect.setAttribute("x", b.box.x); rect.setAttribute("y", b.box.y);
    rect.setAttribute("width", b.box.w); rect.setAttribute("height", b.box.h);
    rect.setAttribute("class", "box" + (b._sel ? " sel" : ""));
    rect.setAttribute("stroke", KIND_COLORS[b.kind] || "blue");
    rect.style.pointerEvents = "all";
    rect.onclick = (ev) => {
      ev.stopPropagation();
      state[pi].boxes.forEach(x => x._sel = false);
      b._sel = true; render();
    };
    svg.appendChild(rect);
    if (b.from_arm) {
      const label = document.createElementNS("http://www.w3.org/2000/svg", "text");
      label.setAttribute("x", b.box.x + 2); label.setAttribute("y", Math.max(12, b.box.y - 4));
      label.setAttribute("font-size", "12");
      label.textContent = `${b.kind}@${b.from_arm}`;
      svg.appendChild(label);
    }
  });
  const sel = state[pi].boxes.find(b => b._sel);
  if (sel) {
    const del = document.createElementNS("http://www.w3.org/2000/svg", "text");
    del.setAttribute("x", sel.box.x); del.setAttribute("y", sel.box.y + sel.box.h + 14);
    del.setAttribute("font-size", "13"); del.setAttribute("fill", "red");
    del.textContent = "[选中] 点击此处删除 / 按 k 切 kind";
    del.style.pointerEvents = "all";
    del.onclick = () => { state[pi].boxes = state[pi].boxes.filter(b => b !== sel); render(); };
    svg.appendChild(del);
  }
}

function wireDrag(img, pi) {
  let start = null;
  img.parentElement.addEventListener("mousedown", (ev) => {
    const r = img.getBoundingClientRect();
    start = {x: ev.clientX - r.left, y: ev.clientY - r.top, r};
  });
  img.parentElement.addEventListener("mouseup", (ev) => {
    if (!start) return;
    const x = Math.min(start.x, ev.clientX - start.r.left);
    const y = Math.min(start.y, ev.clientY - start.r.top);
    const w = Math.abs(ev.clientX - start.r.left - start.x);
    const h = Math.abs(ev.clientY - start.r.top - start.y);
    start = null;
    if (w < 3 || h < 3) return;
    state[pi].boxes.forEach(b => b._sel = false);
    state[pi].boxes.push({kind: currentKind, box: {x: Math.round(x), y: Math.round(y), w: Math.round(w), h: Math.round(h)}, from_arm: "human"});
    render();
  });
}

document.addEventListener("keydown", (ev) => {
  if (ev.key !== "k") return;
  state.forEach(page => page.boxes.forEach(b => {
    if (b._sel) { b.kind = b.kind === "seal" ? "signature" : "seal"; }
  }));
  render();
});

function exportGt() {
  const rows = [];
  state.forEach(page => page.boxes.forEach(b => {
    rows.push({page_id: page.page_id, arm: "gt", kind: b.kind,
      box: {x: b.box.x, y: b.box.y, w: b.box.w, h: b.box.h},
      confidence: 1.0, raw: b.from_arm === "human" ? "" : `draft:${b.from_arm}`});
  }));
  const blob = new Blob([rows.map(r => JSON.stringify(r)).join("\\n") + "\\n"], {type: "application/json"});
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "gt.jsonl";
  a.click();
}

render();
</script></body></html>
"""


def build_html(pages_dir: str, prefill: dict[str, list[dict]]) -> str:
    data = []
    for path in page_images(pages_dir):
        from PIL import Image

        with Image.open(path) as im:
            w, h = im.size
        stem = path.stem  # 约定页图文件名 = page_id
        boxes = prefill.get(stem, [])
        data.append(
            {"page_id": stem, "src": path.name, "w": w, "h": h, "boxes": boxes}
        )
    return _HTML_TEMPLATE.replace("__PAGES_DATA__", json.dumps(data, ensure_ascii=False))


def cmd_build(args: argparse.Namespace) -> int:
    prefill = collect_prefill(args.prefill, args.prefill_arm) if args.prefill else {}
    out = Path(args.out)
    out.write_text(build_html(args.pages_dir, prefill), encoding="utf-8")
    print(f"[ok] {out}（浏览器打开，核对后点「导出 gt.jsonl」）")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    rows = [
        json.loads(line)
        for line in Path(args.downloaded).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    errors: list[str] = []
    for i, row in enumerate(rows):
        for err in spec.validate_record(row):
            errors.append(f"行 {i}: {err}")
    if errors:
        print("gt.jsonl 校验失败:\n  " + "\n  ".join(errors), file=sys.stderr)
        return 1
    Path(args.out).write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8"
    )
    print(f"[ok] {len(rows)} 条 GT → {args.out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_build = sub.add_parser("build", help="生成标注核对 HTML")
    p_build.add_argument("--pages-dir", required=True)
    p_build.add_argument("--prefill", default=None, help="预标来源 jsonl（details.jsonl 或已有 gt.jsonl）")
    p_build.add_argument("--prefill-arm", default=None, help="只叠某臂的预标（如 la）")
    p_build.add_argument("--out", required=True)
    p_build.set_defaults(func=cmd_build)

    p_export = sub.add_parser("export", help="浏览器导出文件校验并落盘")
    p_export.add_argument("--downloaded", required=True)
    p_export.add_argument("--out", required=True)
    p_export.set_defaults(func=cmd_export)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
