"""T2 选型 benchmark runner：引擎申报制、桶级明细、无闸门。数据目录指向云/本地私有桶。

引擎协议：name: str、supports: set[str] | None（None=全支持）、
async predict(text: str, types: list[str]) -> dict[str, list[str]]。
指标只复用 eval_ner_quality.compute_metrics，不另立口径；N/A 桶不记零分、不设闸门不 exit 1。
"""
import argparse
import asyncio
import importlib.util
import json
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import spec  # noqa: E402  BUCKETS 等规格在此维护（--buckets 桶名校验）

_spec = importlib.util.spec_from_file_location(
    "eval_ner_quality", _REPO / "backend" / "scripts" / "eval" / "eval_ner_quality.py")
nerq = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(nerq)


class HttpEngine:
    """远程 NER 引擎：包装 eval_ner_quality.call_ner（has / llm 两种命名均走同一协议）。"""

    supports = None  # 全支持

    def __init__(self, name: str, base: str, model: str | None = None,
                 max_tokens: int = 4000, timeout: float = 120.0):
        self.name = name
        self.base = base
        self.model = model
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.client = None
        self.warnings: list[str] = []

    async def predict(self, text: str, types: list[str]) -> dict[str, list[str]]:
        import httpx
        if self.client is None:
            self.client = httpx.AsyncClient()
        try:
            parsed, _latency, warns = await nerq.call_ner(
                self.client, self.base, self.model, text, types,
                max_tokens=self.max_tokens, timeout=self.timeout)
            self.warnings.extend(warns)
            return parsed
        except Exception as exc:  # 单条失败不中断整桶：记告警，按空预测计
            self.warnings.append(f"{self.name} predict error: {exc}")
            return {}


def load_entries(data_dir: Path, buckets: list[str] | None) -> list[dict]:
    entries = []
    for f in sorted(data_dir.rglob("*.jsonl")):
        for line in f.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            e = json.loads(line)
            if buckets is None or e["bucket"] in buckets:
                entries.append(e)
    return entries


def run_engines(entries: list[dict], engines: dict) -> dict:
    by_bucket: dict[str, list[dict]] = defaultdict(list)
    for e in entries:
        by_bucket[e["bucket"]].append(e)
    result = {"engines": list(engines), "buckets": {}}

    # 整桶（所有引擎）装进一个 async 驱动函数，只 asyncio.run 一次，
    # 避免事件循环反复创建（也保证引擎内共享 AsyncClient 复用同一连接池）。
    async def drive():
        for bucket, items in sorted(by_bucket.items()):
            for ename, engine in engines.items():
                supports = getattr(engine, "supports", None)
                if supports is not None and bucket not in supports:
                    result["buckets"].setdefault(bucket, {})[ename] = "N/A"
                    continue
                records = []
                for e in items:
                    types = sorted(e["entities"])
                    started = time.perf_counter()
                    pred = await engine.predict(e["text"], types)
                    records.append({"page_id": e["id"], "gt": e["entities"], "pred": pred,
                                    "latency_sec": time.perf_counter() - started})
                m = nerq.compute_metrics(records)
                lats = sorted(r["latency_sec"] for r in records)
                m["latency_sec"]["p50"] = lats[len(lats) // 2] if lats else 0.0
                m["pred_entities_total"] = sum(
                    len(v) for r in records for v in r["pred"].values())
                m.pop("records", None)
                m["warnings"] = list(getattr(engine, "warnings", []))
                if hasattr(engine, "warnings"):
                    engine.warnings = []
                result["buckets"].setdefault(bucket, {})[ename] = m

    asyncio.run(drive())
    return result


def dedup_engine_names(engine_list: list) -> dict:
    """重名引擎自动加后缀：llm、llm-2、llm-3…（--engine llm=a --engine llm=b 不再静默覆盖）。"""
    out: dict = {}
    counts: dict[str, int] = {}
    for e in engine_list:
        name = getattr(e, "name", f"engine-{len(out) + 1}")
        counts[name] = counts.get(name, 0) + 1
        final = name if counts[name] == 1 else f"{name}-{counts[name]}"
        e.name = final
        out[final] = e
    return out


def render_report(result: dict) -> str:
    lines = ["# T2 Benchmark 桶级对比", "",
             f"- 引擎：{', '.join(result.get('engines', []))}",
             f"- 生成时间：{result.get('generated_at', '')}",
             f"- 数据目录：{result.get('data_dir', '')}", "",
             "| 桶 | 引擎 | P | R | F1 | 数字exact | 延迟mean/p50(s) |",
             "|---|---|---|---|---|---|---|"]
    for ename, meta in (result.get("engine_meta") or {}).items():
        if meta:
            bits = [f"{k}={meta[k]}" for k in ("model_name", "device", "load_sec") if k in meta]
            lines.insert(4, f"- {ename}：{' / '.join(bits)}")
    for bucket, per_engine in result["buckets"].items():
        for ename, m in per_engine.items():
            if m == "N/A":
                lines.append(f"| {bucket} | {ename} | N/A | N/A | N/A | N/A | N/A |")
            else:
                dg = m.get("digital", {})
                if dg:
                    exact = min(s["exact_rate"] for s in dg.values())
                    exact_cell = f"{exact:.4f}"
                else:
                    exact_cell = "N/A"  # 桶内无数字实体，不显示误导性的 1.0
                lat = m.get("latency_sec", {})
                lat_cell = f"{lat.get('mean', 0):.3f}/{lat.get('p50', 0):.3f}"
                lines.append(f"| {bucket} | {ename} | {m['overall']['precision']:.4f} "
                             f"| {m['overall']['recall']:.4f} | {m['overall']['f1']:.4f} "
                             f"| {exact_cell} | {lat_cell} |")
    # 数字三级分级明细：仅含数字实体桶的引擎行（数据来自 json 的 digital 字段）
    detail_lines = []
    for bucket, per_engine in result["buckets"].items():
        for ename, m in per_engine.items():
            if m == "N/A":
                continue
            for etype, s in sorted(m.get("digital", {}).items()):
                detail_lines.append(
                    f"- {bucket} / {ename} / {etype}：exact={s['exact']} "
                    f"near_miss={s['near_miss']} miss={s['miss']} "
                    f"exact_rate={s['exact_rate']:.4f}")
    if detail_lines:
        lines += ["", "## 数字分级明细（exact / near_miss / miss）"] + detail_lines
    return "\n".join(lines) + "\n"


def parse_engine_arg(value: str):
    """--engine has=<base> | llm=<base>[,model=<m>] | gliner=<hf模型> | uie=<模型名>。"""
    kind, _, rest = value.partition("=")
    kind = kind.strip()
    parts = rest.split(",")
    base = parts[0].strip()
    model = None
    for extra in parts[1:]:
        k, _, v = extra.partition("=")
        if k.strip() == "model":
            model = v.strip()
    if kind == "has":
        return HttpEngine("has", base)
    if kind == "llm":
        return HttpEngine("llm", base, model=model)
    if kind in ("gliner", "uie"):  # 本地抽取式引擎（Issue#90 POC）：参数=模型 checkpoint 名
        import local_engines
        cls = local_engines.GlinerEngine if kind == "gliner" else local_engines.UieEngine
        return local_engines.LazyEngine(cls, base)
    raise argparse.ArgumentTypeError(
        f"invalid --engine '{value}': expected has=<base> or llm=<base>[,model=<m>] "
        f"or gliner=<hf模型> / uie=<模型名>")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="T2 benchmark：引擎申报制、桶级明细、无闸门")
    ap.add_argument("--data-dir", required=True, type=Path, help="桶 jsonl 目录（rglob *.jsonl）")
    ap.add_argument("--engine", required=True, action="append", type=parse_engine_arg,
                    help="引擎申明，可多次：has=<ner_base> 或 llm=<base>[,model=<m>] "
                         "或 gliner=<hf模型> / uie=<模型名>（本地，Issue#90 POC）")
    ap.add_argument("--buckets", default=None, help="逗号分隔桶名过滤（可选，默认全部）")
    ap.add_argument("--out", type=Path, default=Path("eval/reports"), help="报告输出目录")
    args = ap.parse_args(argv)

    buckets = [b.strip() for b in args.buckets.split(",") if b.strip()] if args.buckets else None
    if buckets is not None:
        unknown = [b for b in buckets if b not in spec.BUCKETS]
        if unknown:
            ap.error(f"unknown --buckets: {', '.join(unknown)}; "
                     f"valid: {', '.join(sorted(spec.BUCKETS))}")  # exit 2，仅参数错误，非闸门
    # 本地引擎物化放在 --buckets 校验之后：参数错零加载成本（LazyEngine 仍为代理时跳过）
    from local_engines import materialize
    engines = dedup_engine_names([materialize(e) for e in args.engine])
    print(f"engines: {', '.join(engines)}")  # 启动日志：含去重后的引擎清单
    entries = load_entries(args.data_dir, buckets)
    result = run_engines(entries, engines)
    result["generated_at"] = datetime.now().isoformat(timespec="seconds")
    result["data_dir"] = str(args.data_dir)
    # 引擎元信息：checkpoint / 推理设备 / 加载耗时（延迟口径可比性的前提，Issue#90 A6）
    result["engine_meta"] = {
        ename: {k: getattr(eng, k) for k in ("model_name", "device", "load_sec")
                if hasattr(eng, k)}
        for ename, eng in engines.items()}

    args.out.mkdir(parents=True, exist_ok=True)
    stem = datetime.now().strftime("%Y%m%d-%H%M%S") + "-t2-benchmark"
    (args.out / f"{stem}.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    (args.out / f"{stem}.md").write_text(render_report(result), encoding="utf-8")
    print(f"wrote {args.out / stem}.json / .md")
    return 0  # 无闸门：结果只报告，不因指标低 exit 1


if __name__ == "__main__":
    sys.exit(main())
