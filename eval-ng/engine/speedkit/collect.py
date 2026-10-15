"""speedkit.collect：single/batch 两线响应 → 统一信号抽取 + 聚合 + 闭合校验。

对齐验收方案 v2 §4.1（D3）的硬约束：

- **两线共用同一压平函数**（D3⑥）；`single` 线的输入是 `VisionResult.duration_ms`（根
  `total`）+ `pipeline_status[*].stage_duration_ms`；`batch` 线是 `GET /jobs/{id}` 的
  逐页 `duration_ms` + `duration_breakdown_ms`（键已带 `<pipeline>.` 前缀）。
- **闭合只在「每管线 stage `.total`」上做**（同源同粒度：`Σ_backend_pages` = 逐页根
  `total`，`Σ_rings` = 逐页各管线 `total` 之和）。叶子（`ocr_has.prepare/ocr/has_ner/
  match/draw`、`has_text_*`、`visual_features.custom_*`）**只做归因，不得加进闭合和**
  ——它们与 `has_ner`/`total` 嵌套，相加即双计。
- **键路径按 `<pipeline>.<key>` 拼接**；禁止 `endswith(suffix)` 后缀归并（M6）。
- `unclassified_ms` **只由残差反推**（`Σ_backend_pages - Σ_rings`），双侧校验（D3②）：
  负残差即 FAIL，上界 10%。
- 跨时钟两序列**不得逐元素全等**（全等 ⇒ harness 抄后端），比值 ∈ [1.00, 1.25]（D3①b）。
"""

from __future__ import annotations

# ── 判据常量（验收方案把数写死在此，报告与验收脚本都从这里取） ──────────────
CLOSURE_TOLERANCE = 0.02          # D3①a：|Σ_backend − Σ_rings| ÷ Σ_backend
HARNESS_RATIO_MIN = 1.00          # D3①b：客户端不可能比服务端快
HARNESS_RATIO_MAX = 1.25          # D3①b：上界 = HTTP/排队开销
MAX_RESIDUAL_RATIO = 0.10         # D3②：0 ≤ unclassified ÷ Σ_backend
MIN_TIMING_ROUNDS = 3             # D2③：≥3 计时轮才报中位
RESIDUAL_SOURCE = "derived_from_residual"

HIT_STATUS = "hit_before_slot"    # 唯一计入「命中」的状态（D2②）
SKIPPED_PREFIX = "skipped_"
# 未走到 HaS 的状态：其 has_text_*_ms 恒 0 是正确的，**不得据此判 FAIL**（D3⑤/S8）
NON_HA_S_STATUSES = frozenset({
    "not_started", "skipped_empty_ocr", "skipped_no_client", "skipped_recent_unavailable",
    "skipped_no_eligible_text", "skipped_too_short", "skipped_visual_only_types", "failed",
})

# `single` 线报告必含的环叶子（D3⑤）
SINGLE_REQUIRED_RING_LEAVES = (
    "ocr_has.prepare", "ocr_has.ocr", "ocr_has.has_ner", "ocr_has.match", "ocr_has.draw",
)

# 环叶子只收**耗时**键（④云门实测订正）：后端的 `stage_duration_ms` 是混合账本——
# 除耗时外还塞了计数与字符数（`has_text_input_chars`/`ocr_cache_misses`/`has_text_type_count` …）。
# 把计数当耗时求和 ⇒ Σ_rings 远大于 Σ_root（10 页实测 44.6s/页 vs 24.5s/页），闭合 54% 恒 FAIL。
# 故：声明的阶段名 ∪ `*_ms` 后缀 = 耗时；`total` 恒归 `pipeline_totals_ms`，不进叶子（防双算）。
DURATION_STAGE_NAMES = frozenset({"prepare", "ocr", "has_ner", "match", "draw", "model"})


def is_duration_key(key: str) -> bool:
    """耗时叶子判据：声明阶段名或以 `_ms` 结尾；`total` 另有归属（管线环），不入叶子。"""
    name = key.rsplit(".", 1)[-1]
    if name == "total":
        return False
    return name in DURATION_STAGE_NAMES or name.endswith("_ms")


def single_page_pipeline_durations(resp: dict) -> dict[str, float]:
    """逐管线**块级** `duration_ms`（= 页根 total 的同粒度分量；D3①a 闭合用）。

    `stage_duration_ms.total` 只是**部分**阶段的合计（实测 ocr_has：块 1853ms vs stage total 849ms），
    拿它当环和会让闭合差 18%~54% 恒 FAIL；块级 `pipeline_status[*].duration_ms` 才与页根同粒度。
    """
    durations: dict[str, float] = {}
    status = resp.get("pipeline_status") or {}
    if not isinstance(status, dict):
        return durations
    for name, block in status.items():
        if not isinstance(block, dict):
            continue
        value = block.get("duration_ms")
        if is_number(value):
            durations[str(name)] = float(value)
    return durations

# batch 线专有键：`single` 报告出现任一即判 FAIL（D2④ 显式键集合比对）
BATCH_ONLY_KEYS = frozenset({
    "job_id", "job_status", "job_queue_wait_ms", "job_items_total", "job_items_done",
    "job_upload_sec", "job_poll_count", "recognition_page_concurrency",
    "recognition_page_concurrency_configured", "recognition_page_duration_sum_ms",
    "recognition_parallelism_ratio", "recognition_wall_ms", "max_upload_bytes",
})

# `single` 线报告必含的 batch 键（D2④：batch 必须含 job_queue_wait_ms）
BATCH_REQUIRED_KEYS = ("job_id", "job_queue_wait_ms")


def is_number(value) -> bool:
    """bool 不是数值（True 会被 isinstance 判成 int，必须排除）。"""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def numeric_leaves(obj, prefix: str = "") -> dict[str, float]:
    """递归取出全部数值叶子 → {键路径: 值}（供白名单校验与清点用）。"""
    out: dict[str, float] = {}
    if isinstance(obj, dict):
        for key, value in obj.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if is_number(value):
                out[path] = float(value)
            else:
                out.update(numeric_leaves(value, path))
    elif isinstance(obj, (list, tuple)):
        for idx, value in enumerate(obj):
            path = f"{prefix}[{idx}]"
            if is_number(value):
                out[path] = float(value)
            else:
                out.update(numeric_leaves(value, path))
    return out


# ── single 线：VisionResult → 页信号 ────────────────────────────────────────
def single_page_pipelines(resp: dict) -> dict[str, dict]:
    """`pipeline_status[*].stage_duration_ms` 原样取出（不合并、不截断键名）。"""
    pipelines: dict[str, dict] = {}
    status = resp.get("pipeline_status") or {}
    if not isinstance(status, dict):
        return pipelines
    for name, block in status.items():
        block = block or {}
        stage = block.get("stage_duration_ms") if isinstance(block, dict) else None
        if isinstance(stage, dict):
            pipelines[str(name)] = stage
    return pipelines


def page_signal(resp: dict, *, wall_s: float, harness_span: tuple[float, float] | None = None,
                page_no: int | None = None) -> dict:
    """把一页响应压成统一页信号（两线共用；batch 线走 `batch_page_signal` 适配后同样结构）。"""
    root = resp.get("duration_ms") if isinstance(resp.get("duration_ms"), dict) else {}
    pipelines = single_page_pipelines(resp)
    totals = single_page_pipeline_durations(resp)
    leaves: dict[str, float] = {}
    for name, stage in pipelines.items():
        for key, value in stage.items():
            if is_number(value) and is_duration_key(key):
                leaves[f"{name}.{key}"] = float(value)
        if name not in totals and is_number(stage.get("total")):
            totals[name] = float(stage["total"])  # 罕见兜底：仅当块级 duration_ms 缺失
    histogram: dict[str, int] = {}
    for _name, stage in pipelines.items():
        status = stage.get("has_text_cache_status")
        if isinstance(status, str) and status:
            histogram[status] = histogram.get(status, 0) + 1
    root_total = root.get("total")
    signal = {
        "page": page_no if page_no is not None else resp.get("page"),
        "wall_s": round(float(wall_s), 6),
        "root_total_ms": float(root_total) if is_number(root_total) else None,
        "pipeline_totals_ms": totals,
        "ring_leaves_ms": leaves,
        "ring_keys": sorted(leaves),
        "cache_status": dict(histogram),
        "cache_hits": histogram.get(HIT_STATUS, 0),
        "skipped_has": all(k.startswith(SKIPPED_PREFIX) for k in histogram) if histogram else False,
        "page_concurrency": None,
        "request_total_ms": float(root["request_total_ms"]) if is_number(root.get("request_total_ms")) else None,
    }
    if harness_span is not None:
        signal["harness_start"] = round(float(harness_span[0]), 6)
        signal["harness_end"] = round(float(harness_span[1]), 6)
    return signal


def batch_page_signal(page: dict) -> dict:
    """batch 线逐页条目 → 统一页信号。

    batch 的 `duration_breakdown_ms` 键已是 `<pipeline>.<key>` 全名（无需也不许再拼接或
    后缀归并）；管线环 = `<prefix>.total`。`cache_status` 是该页的直方图字典。
    """
    page = page or {}
    breakdown = page.get("duration_breakdown_ms") if isinstance(page.get("duration_breakdown_ms"), dict) else {}
    leaves: dict[str, float] = {}
    totals: dict[str, float] = {}
    for key, value in breakdown.items():
        if not is_number(value):
            continue
        name = str(key)
        if name.rsplit(".", 1)[-1] == "total" and "." in name:
            totals[name.rsplit(".", 1)[0]] = float(value)  # 管线环（与 single 块级 duration_ms 同粒度）
        elif is_duration_key(name):
            leaves[name] = float(value)  # 耗时叶子；计数/字符数/缓存标记不入环
    histogram_raw = page.get("cache_status") if isinstance(page.get("cache_status"), dict) else {}
    histogram: dict[str, int] = {}
    for key, value in histogram_raw.items():
        status = str(key).replace("has_text_cache_status", "").strip("_.") or str(key)
        if is_number(value):
            histogram[status] = int(value)
        elif isinstance(value, str):
            histogram[value] = histogram.get(value, 0) + 1
    duration = page.get("duration_ms")
    return {
        "page": page.get("page"),
        "wall_s": None,
        "root_total_ms": float(duration) if is_number(duration) else None,
        "pipeline_totals_ms": totals,
        "ring_leaves_ms": leaves,
        "ring_keys": sorted(leaves),
        "cache_status": histogram,
        "cache_hits": histogram.get(HIT_STATUS, 0),
        "skipped_has": all(k.startswith(SKIPPED_PREFIX) for k in histogram) if histogram else False,
        "page_concurrency": page.get("page_concurrency"),
        "page_concurrency_configured": page.get("page_concurrency_configured"),
        "page_concurrency_reason": page.get("page_concurrency_reason"),
    }


# ── 聚合 ───────────────────────────────────────────────────────────────────
def sum_pages(pages: list[dict]) -> dict:
    """逐页信号求和：两序列、环和、残差、键归因、缓存直方图。"""
    backend = [p["root_total_ms"] for p in pages if is_number(p.get("root_total_ms"))]
    harness = [float(p["wall_s"]) * 1000.0 for p in pages if is_number(p.get("wall_s"))]
    ring_total = 0.0
    rings_by_key: dict[str, float] = {}
    concurrency_peaks: list[int] = []
    histogram: dict[str, int] = {}
    pipelines_seen: set[str] = set()
    hits = 0
    for page in pages:
        totals = page.get("pipeline_totals_ms") or {}
        pipelines_seen.update(str(k) for k in totals)
        ring_total += sum(float(v) for v in totals.values())
        for key, value in (page.get("ring_leaves_ms") or {}).items():
            rings_by_key[key] = rings_by_key.get(key, 0.0) + float(value)
        for status, count in (page.get("cache_status") or {}).items():
            histogram[status] = histogram.get(status, 0) + int(count)
        hits += int(page.get("cache_hits") or 0)
        if is_number(page.get("page_concurrency")):
            concurrency_peaks.append(int(page["page_concurrency"]))
    sum_backend = sum(backend)
    sum_harness = sum(harness)
    unclassified = sum_backend - ring_total
    return {
        "pages_measured": len(pages),
        "pages_with_backend_total": len(backend),
        "sum_backend_pages_ms": round(sum_backend, 3),
        "sum_harness_pages_ms": round(sum_harness, 3),
        "sum_rings_ms": round(ring_total, 3),
        "unclassified_ms": round(unclassified, 3),
        "unclassified_source": RESIDUAL_SOURCE,
        "residual_ratio": (round(unclassified / sum_backend, 6) if sum_backend > 0 else None),
        "harness_ratio": (round(sum_harness / sum_backend, 6) if sum_backend > 0 else None),
        "rings_by_key_ms": {k: round(v, 3) for k, v in sorted(rings_by_key.items())},
        "pipelines_seen": sorted(pipelines_seen),
        "cache_histogram": dict(sorted(histogram.items())),
        "cache_hits": hits,
        "page_concurrency_peak": (max(concurrency_peaks) if concurrency_peaks else None),
        "page_concurrency_values": sorted(set(concurrency_peaks)),
    }


def recorded_pair_sequences(page_records: list[list[dict]]) -> tuple[list[float], list[float]]:
    """跨轮展平出两条**分列**序列（harness / backend），供非同源断言使用。"""
    backend: list[float] = []
    harness: list[float] = []
    for pages in page_records:
        for page in pages:
            if is_number(page.get("root_total_ms")):
                backend.append(float(page["root_total_ms"]))
            if is_number(page.get("wall_s")):
                harness.append(float(page["wall_s"]) * 1000.0)
    return harness, backend


def closure_checks(agg: dict, *, harness_seq=None, backend_seq=None) -> list[dict]:
    """D3①a/①b/② 三条机器判定。返回 [{id, status, detail, value}]。"""
    checks: list[dict] = []
    total = agg.get("sum_backend_pages_ms") or 0.0
    residual = agg.get("unclassified_ms")

    if total <= 0:
        checks.append(_check("D3.closure_same_clock", "FAIL",
                             "无后端逐页 total（Σ_backend_pages_ms ≤ 0）", total))
    else:
        ratio = abs(residual) / total
        checks.append(_check(
            "D3.closure_same_clock",
            "PASS" if ratio <= CLOSURE_TOLERANCE else "FAIL",
            f"|Σ_backend − Σ_rings| ÷ Σ_backend = {ratio:.6f}（阈值 ≤{CLOSURE_TOLERANCE}）",
            round(ratio, 6),
        ))

    if residual is None:
        checks.append(_check("D3.residual_two_sided", "FAIL", "无残差可算", None))
    elif residual < 0:
        checks.append(_check("D3.residual_two_sided", "FAIL",
                             f"负残差 {residual}ms（环和 > 页总耗时，必属键归并错误）", residual))
    elif total <= 0 or residual > MAX_RESIDUAL_RATIO * total:
        checks.append(_check("D3.residual_two_sided", "FAIL",
                             f"残差 {residual}ms 超过 {MAX_RESIDUAL_RATIO:.0%} × {total}ms",
                             round((residual / total) if total else 0.0, 6)))
    else:
        checks.append(_check("D3.residual_two_sided", "PASS",
                             f"0 ≤ {residual}ms ≤ {MAX_RESIDUAL_RATIO:.0%} × {total}ms",
                             round(residual / total, 6)))

    harness = harness_seq or []
    backend = backend_seq or []
    ratio = agg.get("harness_ratio")
    if not harness or not backend:
        checks.append(_check("D3.cross_clock_not_copied", "UNVERIFIED",
                             "缺 harness 或 backend 逐页序列，非同源断言无法成立", None))
    elif len(harness) == len(backend) and all(abs(a - b) < 1e-9 for a, b in zip(harness, backend, strict=True)):
        checks.append(_check("D3.cross_clock_not_copied", "FAIL",
                             "两序列逐元素全等 ⇒ harness 抄了后端数字", None))
    elif ratio is None or ratio < HARNESS_RATIO_MIN or ratio > HARNESS_RATIO_MAX:
        checks.append(_check("D3.cross_clock_not_copied", "FAIL",
                             f"Σ_harness ÷ Σ_backend = {ratio} 越出 [{HARNESS_RATIO_MIN}, {HARNESS_RATIO_MAX}]",
                             ratio))
    else:
        checks.append(_check("D3.cross_clock_not_copied", "PASS",
                             f"Σ_harness ÷ Σ_backend = {ratio} ∈ [{HARNESS_RATIO_MIN}, {HARNESS_RATIO_MAX}]"
                             "，且两序列非逐元素全等", ratio))
    return checks


def key_placement_checks(agg: dict, driver: str) -> list[dict]:
    """D3④/⑤：键前缀归位与非零性（只对 `single` 线）。"""
    checks: list[dict] = []
    leaves = agg.get("rings_by_key_ms") or {}
    if driver != "single":
        return checks
    missing = [k for k in SINGLE_REQUIRED_RING_LEAVES if k not in leaves]
    checks.append(_check("D3.ring_keys_present", "PASS" if not missing else "FAIL",
                         "缺环叶子：" + ", ".join(missing) if missing else
                         "ocr_has.prepare/ocr/has_ner/match/draw 全在", len(leaves)))
    # 文本层页（prepare/ocr 恒 0 正确）——只在扫描页上要求 OCR 路径非零，故此处只断言键存在，
    # 非零性由收卷人用 cache_status=="miss" 的页集合单独核对（见验收方案 D3⑤）。
    # D3④：键路径必须按 `<pipeline>.<leaf>` **压平**，两管线的同名叶子（如 `text_layer_doc.ocr`
    # vs `ocr_has.ocr`）各成独立键。判别力来自两处交叉核对：
    #   ① 任何环键都必须带 `.` 前缀（裸叶子 = 被后缀归并过的痕迹）；
    #   ② 环键的前缀集合必须与页级 `pipeline_status` 实际出现的管线集合**逐管线相等**
    #      （跨管线按同名叶子归并，会让两集合不等 —— 见单测 test_no_suffix_merge_is_falsifiable）。
    pipelines = sorted(str(p) for p in (agg.get("pipelines_seen") or []))
    ring_prefixes = sorted({k.split(".", 1)[0] for k in leaves if "." in k})
    flat_violations = sorted(k for k in leaves if "." not in k)
    unmapped = sorted(set(ring_prefixes) ^ set(pipelines))
    problems: list[str] = []
    if flat_violations:
        problems.append("无管线前缀（疑似后缀归并）：" + ", ".join(flat_violations))
    if unmapped:
        problems.append(f"环键管线 {ring_prefixes or '∅'} 与实际管线 {pipelines or '∅'} 不一致："
                        f"{', '.join(unmapped)}")
    checks.append(_check("D3.no_suffix_merge", "FAIL" if problems else "PASS",
                         "；".join(problems) if problems else
                         f"{len(ring_prefixes)} 条管线的同名叶子各成独立键，键路径均为 <pipeline>.<leaf>"
                         f"（管线：{', '.join(ring_prefixes)}）",
                         len(leaves)))
    return checks


def _check(cid: str, status: str, detail: str, value) -> dict:
    return {"id": cid, "status": status, "detail": detail, "value": value}


def criteria_refs(report: dict, spec: dict[str, str]) -> dict[str, dict]:
    """D3⑦：判据 ↔ json 键路径引用表。`spec` = {判据名: 点号路径}。

    路径不存在**不得抛错崩掉整次测量**（评审 I3）：逐条捕获并记 `error`，
    由 `validate_criteria_refs` 在同一次 finalize 里判 FAIL——「引用表取不到」必须是
    一条可读的判据失败，而不是 traceback。
    """
    refs: dict[str, dict] = {}
    for name, path in spec.items():
        try:
            value = resolve_path(report, path)
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            refs[name] = {"path": path, "value": None, "error": f"{type(exc).__name__}: {exc}"}
            continue
        refs[name] = {"path": path, "value": value}
    return refs


def resolve_path(doc, path: str):
    """按 `a.b.c` / `a[0]` 取值；不存在抛 KeyError（验收脚本据此判 FAIL，M15）。"""
    cur = doc
    for part in path.split("."):
        if "[" in part and part.endswith("]"):
            head, _, idx = part.partition("[")
            if head:
                cur = cur[head]
            cur = cur[int(idx[:-1])]
            continue
        cur = cur[part]
    return cur


def validate_criteria_refs(report: dict) -> list[str]:
    """逐条按引用路径回取，值不等或路径不存在 ⇒ 记错误（M15 的判定实现）。"""
    errors: list[str] = []
    refs = report.get("criteria_refs")
    if not isinstance(refs, dict) or not refs:
        return ["报告缺 criteria_refs（D3⑦）"]
    for name, ref in refs.items():
        if not isinstance(ref, dict) or "path" not in ref:
            errors.append(f"criteria_refs.{name} 结构非法")
            continue
        try:
            actual = resolve_path(report, ref["path"])
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            errors.append(f"criteria_refs.{name} 路径 {ref['path']!r} 不可取：{exc!r}")
            continue
        if actual != ref.get("value"):
            errors.append(f"criteria_refs.{name} 路径 {ref['path']!r} 值 {actual!r} ≠ 记录 {ref.get('value')!r}")
    return errors
