"""speedkit.report：报告装配 + 自足校验 + 四态判定。

对齐验收方案 v2 §4.1：

- **D4① 报告自足**：耗时序列、逐环耗时、原始计数与缓存策略/命中率、卷清单（md5+页数）、
  时点、窗口、参数快照、环境脚注、`mode`/`arm`、`criteria_refs`、`not_measured` 全部落在同一份 JSON。
- **D4③ 自足可复跑**：`reproduce.command` + `reproduce.env` 足够在另一台机重跑。
- **D5① 白名单**：任何数值字段必须落在白名单路径前缀内；质量类字段（precision/recall/f1/
  accuracy/score/quality/per_type…）**出现即 FAIL**（黑名单可换名绕过，故改白名单）。
- **D5③ 脚注不可硬编码**：`code_rev` 由引擎现场 `git rev-parse HEAD` 双向核对；`instance`
  必须与 `api_base` 的 host 一致，本地回环则只能记 `local`（云端数字不得出于本地回环）。
- **四态判定**：FAIL > BLOCKED > UNVERIFIED > PASS（装置/输入不可得记 BLOCKED，不得记 PASS）。
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import socket
import subprocess
from datetime import datetime
from pathlib import Path

from . import collect

SCHEMA_VERSION = "speedkit/1"

# 八项环境脚注（验收方案 v2 §4.1 D5②；顺序即方案顺序）
FOOTNOTE_KEYS = (
    "instance", "hardware", "image_dtk", "code_rev",
    "switches", "manifest_hash", "window", "readiness",
)
# 同会话环境指纹（D5②：脚注第 1/2/3 项必须与指纹原始输出交叉，禁手填）
FINGERPRINT_KEYS = ("hostname", "hardware_raw", "dtk_version_raw", "api_base_host")
FOOTNOTE_PLACEHOLDERS = frozenset({
    "", "-", "unknown", "n/a", "na", "none", "null", "todo", "tbd", "test", "placeholder", "占位",
})

VERDICT_ORDER = ("FAIL", "BLOCKED", "UNVERIFIED", "PASS")

# 数值字段白名单（D5①：**按键名**，非按区域）——任何数值叶子的键名必须匹配其一
NUMERIC_LEAF_EXACT = frozenset({
    "n", "mean", "median", "min", "max", "std", "var", "value", "index", "level", "code",
    "status", "pid", "exit_code", "timestamp", "ts", "epoch", "start_time", "round", "rounds",
    "attempts", "hits", "concurrency", "peak", "total", "subtotal", "size",
})
NUMERIC_LEAF_SUFFIX = (
    "_ms", "_s", "_sec", "_seconds", "_min", "_bytes", "_pages", "_page", "_count", "_counts",
    "_total", "_items", "_item", "_ratio", "_efficiency", "_hits", "_rounds", "_round",
    "_attempts", "_index", "_code", "_status", "_concurrency", "_peak", "_n", "_ts", "_epoch",
    "_timestamp", "_pid", "_size", "_level", "_per_min", "_per_sec", "_calls",
    "_diff", "_measured", "_workers", "_limit", "_chars", "_terminal", "_configured",
    "_done", "_failed", "_value", "_ms_total",
)
NUMERIC_LEAF_RE = re.compile(r"^p\d+$")
# 判定表 / 引用表内的载数值字段显式放行（它们是判据载体，不是指标）
NUMERIC_PATH_ALLOW_PREFIX = ("checks[", "criteria_refs[")
# 后端回吐的时序/计数映射：其叶子键名由后端定义（如 `ocr_has.prepare`），路径段命中即放行
NUMERIC_PATH_ALLOW_CONTAINS = (
    "rings_by_key", "ring_leaves_ms", "pipeline_totals_ms", "by_key_ms",
    "cache_histogram", "cache_status", "histogram", "item_statuses", "duration_ms",
)
# 质量类键名：出现（无论数值或字符串）即 FAIL
QUALITY_KEY_RE = re.compile(
    r"(^|[_.])(precision|recall|f1|f1_score|accuracy|quality|score|per_type|entity_quality|"
    r"leak|leakage|missed|false_positive|false_negative)([_.]|$)", re.I,
)
# 质量/指标子树（路径段命中即 FAIL）
QUALITY_SUBTREE_RE = re.compile(r"(^|\.)(metrics|quality|per_type|aggregated)(\.|\[|$)")


def utcnow() -> str:
    return datetime.now().astimezone().isoformat()


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def md5_file(path: str) -> str:
    digest = hashlib.md5()  # noqa: S324 —— 卷清单比对用，非安全用途
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def runtime_code_rev(root: str | None = None) -> str:
    """引擎现场读出的代码版本（用于与编排传入的 `--code-rev` 双向核对）。"""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, timeout=10,
            check=False,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return ""


def runtime_hostname() -> str:
    try:
        return socket.gethostname()
    except OSError:
        return platform.node() or ""


def api_base_host(api_base: str) -> str:
    """从 api_base 取出 host（去 scheme、port、userinfo）。"""
    host = str(api_base or "").split("://")[-1].split("/")[0]
    if "@" in host:
        host = host.split("@", 1)[1]
    if host.startswith("["):
        return host.split("]", 1)[0].lstrip("[")
    return host.split(":", 1)[0]


def is_loopback_host(host: str) -> bool:
    return host in ("127.0.0.1", "localhost", "::1", "0.0.0.0")


# ── 脚注 + 指纹 ─────────────────────────────────────────────────────────────
def hardware_raw(fallback: str = "") -> str:
    """现场硬件原始输出（`nvidia-smi` → `hy-smi` → `/proc/driver`；禁手填）。"""
    for cmd in (["nvidia-smi", "-L"], ["hy-smi", "-L"], ["hy-smi"]):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=20, check=False)
        except (OSError, subprocess.SubprocessError):
            continue
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip().splitlines()[0][:200]
    # 无 GPU 工具（本地 dev 容器）：退到 CPU/机器原始输出，仍是现场机检、非手填
    try:
        cpuinfo = Path("/proc/cpuinfo").read_text(encoding="utf-8", errors="ignore")
    except OSError:
        cpuinfo = ""
    for line in cpuinfo.splitlines():
        if line.lower().startswith("model name"):
            return line.strip()[:200]
    text = (platform.processor() or platform.machine() or "").strip()
    if text:
        return f"{text} (no gpu tool on this box)"[:200]
    return fallback


def dtk_version_raw() -> str:
    """DTK/镜像版本原始输出：`DTK_VERSION` env → `/opt/dtk/version` → `hy-smi --version`。"""
    env = os.environ.get("DTK_VERSION")
    if env and env.strip():
        return env.strip()
    for path in ("/opt/dtk/version", "/opt/dtk/.version", "/usr/local/dtk/version"):
        try:
            text = Path(path).read_text(encoding="utf-8", errors="ignore").strip()
        except OSError:
            continue
        if text:
            return text.splitlines()[0][:200]
    for cmd in (["hy-smi", "--version"], ["hy-smi", "-v"]):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=20, check=False)
        except (OSError, subprocess.SubprocessError):
            continue
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip().splitlines()[0][:200]
    return ""


def build_fingerprints(*, api_base: str, hardware_fallback: str = "") -> dict:
    """采集同会话环境指纹（+自身 sha256），供脚注交叉与报告留存。"""
    body = {
        "hostname": runtime_hostname(),
        "hardware_raw": hardware_raw(hardware_fallback),
        "dtk_version_raw": dtk_version_raw(),
        "api_base_host": api_base_host(api_base),
    }
    blob = json.dumps(body, ensure_ascii=False, sort_keys=True).encode("utf-8")
    body["sha256"] = hashlib.sha256(blob).hexdigest()
    return body


def validate_fingerprints(fingerprints: dict) -> list[str]:
    errors: list[str] = []
    if not isinstance(fingerprints, dict) or not fingerprints:
        return ["报告缺 fingerprints（D5②：脚注第 1/2/3 项须与同会话指纹交叉，禁手填）"]
    for key in FINGERPRINT_KEYS:
        value = fingerprints.get(key)
        if value is None or (isinstance(value, str) and value.strip().lower() in FOOTNOTE_PLACEHOLDERS):
            errors.append(f"指纹 {key} 缺失或为占位符（D5②）")
    if not fingerprints.get("dtk_version_raw"):
        errors.append("指纹 dtk_version_raw 为空 ⇒ 镜像/DTK 不可核验（D5③：数值 AC 判 FAIL）")
    claimed = str(fingerprints.get("sha256") or "")
    body = {k: fingerprints.get(k) for k in FINGERPRINT_KEYS}
    blob = json.dumps(body, ensure_ascii=False, sort_keys=True).encode("utf-8")
    if claimed and claimed != hashlib.sha256(blob).hexdigest():
        errors.append("指纹 sha256 与内容不符（D5②：指纹被改写）")
    return errors


def validate_footnote(
    footnote: dict, *, api_base: str, live_code_rev: str, fingerprints: dict | None = None,
    readiness: dict | None = None,
) -> list[str]:
    """八项齐备、无占位符、与指纹交叉、`code_rev`/`instance` 与现场一致。"""
    errors: list[str] = []
    if not isinstance(footnote, dict):
        return ["脚注缺失（D5③）"]
    for key in FOOTNOTE_KEYS:
        value = footnote.get(key)
        if value is None or (isinstance(value, str) and value.strip().lower() in FOOTNOTE_PLACEHOLDERS):
            errors.append(f"脚注 {key} 缺失或为占位符（D5③）")
    host = api_base_host(api_base)
    instance = str(footnote.get("instance") or "")
    if is_loopback_host(host):
        if instance != "local":
            errors.append(f"api_base 是回环（{host}），脚注 instance 必须记 'local'，实为 {instance!r}（D5③）")
    elif instance and instance != host and host not in instance:
        errors.append(f"脚注 instance {instance!r} 与 api_base host {host!r} 不一致（D5③ 防硬编码冒充云端）")
    recorded_rev = str(footnote.get("code_rev") or "")
    if live_code_rev and recorded_rev and recorded_rev != live_code_rev:
        errors.append(
            f"脚注 code_rev {recorded_rev[:12]} 与引擎现场 git HEAD {live_code_rev[:12]} 不一致（D5③）"
        )
    if fingerprints:
        if fingerprints.get("api_base_host") != host:
            errors.append("脚注 instance 与指纹 api_base_host 不一致（D5②）")
        if fingerprints.get("hostname") and instance not in (str(fingerprints.get("hostname")), host, "local"):
            errors.append(
                f"脚注 instance {instance!r} 与指纹 hostname {fingerprints.get('hostname')!r} 不一致（D5②）"
            )
    if readiness is not None:
        has_ready = collect.is_number((readiness or {}).get("ready_wait_sec"))
        has_settle = collect.is_number((readiness or {}).get("settle_sec"))
        if not (has_ready and has_settle):
            errors.append("就绪等待/静置实测缺失（D5③：脚注第 8 项须为实测值，禁 N/A）")
    return errors


# ── 数值白名单 / 质量字段 ───────────────────────────────────────────────────
def _key_paths(obj, prefix: str = "") -> list[str]:
    paths: list[str] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            paths.append(path)
            paths.extend(_key_paths(value, path))
    elif isinstance(obj, (list, tuple)):
        for idx, value in enumerate(obj):
            paths.extend(_key_paths(value, f"{prefix}[{idx}]"))
    return paths


def _section(path: str) -> str:
    return path.split(".", 1)[0].split("[", 1)[0]


def _leaf_name(path: str) -> str:
    return path.split(".")[-1].split("[")[0]


def numeric_leaf_allowed(path: str) -> bool:
    """D5①：数值叶子的键名必须命中白名单（计时/计数/比值/体积/轮次）或判定表显式放行。"""
    if path.startswith(NUMERIC_PATH_ALLOW_PREFIX):
        return True
    if any(marker in path for marker in NUMERIC_PATH_ALLOW_CONTAINS):
        return True
    leaf = _leaf_name(path)
    if leaf in NUMERIC_LEAF_EXACT:
        return True
    if NUMERIC_LEAF_RE.match(leaf):
        return True
    return any(leaf.endswith(suffix) for suffix in NUMERIC_LEAF_SUFFIX)


def numeric_whitelist_errors(report: dict) -> list[str]:
    errors: list[str] = []
    for path, value in collect.numeric_leaves(report).items():
        if not numeric_leaf_allowed(path):
            errors.append(f"白名单外的数值字段 {path}={value}（D5①）")
    return errors


def quality_key_errors(report: dict) -> list[str]:
    errors: list[str] = []
    for path in _key_paths(report):
        if QUALITY_SUBTREE_RE.search(path):
            errors.append(f"报告出现质量/指标子树 {path}（D5①）")
            continue
        leaf = _leaf_name(path)
        if QUALITY_KEY_RE.search(leaf):
            errors.append(f"报告出现质量类字段 {path}（D5①）")
    return errors


# ── 判定 ────────────────────────────────────────────────────────────────────
def verdict_from_checks(checks: list[dict]) -> str:
    statuses = {str(c.get("status")) for c in (checks or [])}
    for verdict in VERDICT_ORDER:
        if verdict in statuses:
            return verdict
    return "PASS"


def build_report(
    *, exam: dict, mode: dict, arm: dict, rounds: list[dict], cold_evidence: list[dict],
    summary: dict, cache: dict, footnote: dict, fingerprints: dict, criteria_spec: dict[str, str],
    reproduce: dict, checks: list[dict], not_measured: list[str],
    started_at: str, finished_at: str, has_gt: bool = False, readiness: dict | None = None,
) -> dict:
    round_views = []
    for entry in rounds:
        round_views.append({
            "round": entry.get("round"),
            "warmup": bool(entry.get("warmup")),
            "invalid": bool(entry.get("invalid")),
            "invalid_reason": entry.get("invalid_reason"),
            "harness_start_ts": entry.get("harness_start_ts"),
            "harness_end_ts": entry.get("harness_end_ts"),
            "file_wall_s": entry.get("file_wall_s"),
            "pages_measured": len(entry.get("pages") or []),
            "agg": entry.get("agg") or {},
            "cache": entry.get("cache") or {},
            "pages": entry.get("pages") or [],
        })
    report = {
        "schema_version": SCHEMA_VERSION,
        "module": {"name": "eval-ng speedkit", "entry": "eval-ng/cli.py speed"},
        "generated_at": utcnow(),
        "started_at": started_at,
        "finished_at": finished_at,
        "exam": exam,
        "no_gt": not has_gt,
        "quality_eval": "not_run",
        "mode": mode,
        "arm": arm,
        "rounds": round_views,
        "cold_evidence": list(cold_evidence or []),
        "readiness": dict(readiness or {}),
        "summary": summary,
        "cache": cache,
        "footnote": footnote,
        "fingerprints": fingerprints,
        "checks": list(checks or []),
        "not_measured": list(not_measured or []),
        "reproduce": reproduce,
        "verdict": "UNVERIFIED",
    }
    report["criteria_refs"] = collect.criteria_refs(report, criteria_spec)
    return report


def finalize(report: dict, *, api_base: str, live_code_rev: str | None = None) -> dict:
    """装配派生判据（自足/白名单/引用表）并落四态判定。"""
    live_rev = live_code_rev if live_code_rev is not None else runtime_code_rev()
    derived: list[dict] = []

    fp_errors = validate_fingerprints(report.get("fingerprints") or {})
    for err in fp_errors:
        status = "UNVERIFIED" if "dtk_version_raw 为空" in err else "FAIL"
        derived.append({"id": "D5.fingerprints", "status": status, "detail": err, "value": None})
    for err in validate_footnote(
        report.get("footnote") or {}, api_base=api_base, live_code_rev=live_rev,
        fingerprints=report.get("fingerprints") or None, readiness=report.get("readiness") or None,
    ):
        status = "UNVERIFIED" if "就绪等待/静置实测缺失" in err else "FAIL"
        derived.append({"id": "D5.footnote", "status": status, "detail": err, "value": None})
    for err in quality_key_errors(report):
        derived.append({"id": "D5.no_quality_fields", "status": "FAIL", "detail": err, "value": None})
    for err in numeric_whitelist_errors(report):
        derived.append({"id": "D5.numeric_whitelist", "status": "FAIL", "detail": err, "value": None})
    for err in collect.validate_criteria_refs(report):
        derived.append({"id": "D4.criteria_refs", "status": "FAIL", "detail": err, "value": None})

    checks = list(report.get("checks") or []) + derived
    report["checks"] = checks
    report["verdict"] = verdict_from_checks(checks)
    return report


def md_consistency_errors(report: dict, md_text: str) -> list[str]:
    """D4③：md 非空且关键字段与 JSON 一致（逐项点名比对）。"""
    errors: list[str] = []
    if not md_text or not md_text.strip():
        return ["md 产物为空（D4③）"]
    summary = report.get("summary") or {}
    mode = report.get("mode") or {}
    wall = summary.get("file_wall_s") or {}
    probes: list[tuple[str, object]] = [
        ("verdict", report.get("verdict")),
        ("driver", mode.get("driver")),
        ("page_concurrency", mode.get("page_concurrency")),
        ("cache_policy", mode.get("cache_policy")),
        ("timing_rounds", mode.get("timing_rounds")),
        ("file_wall_s.p50", wall.get("p50")),
        ("file_wall_s.mean", wall.get("mean")),
        ("pages_per_min", summary.get("pages_per_min")),
    ]
    for name, value in probes:
        if value is None:
            continue
        text = f"{value:.3f}" if isinstance(value, float) else str(value)
        if text not in md_text:
            errors.append(f"md 产物缺关键字段 {name}={text}（D4③：md 与 JSON 必须一致）")
    for key in FOOTNOTE_KEYS:
        value = (report.get("footnote") or {}).get(key)
        if isinstance(value, str) and value and value not in md_text:
            errors.append(f"md 产物缺脚注项 {key}（D4③）")
    for item in report.get("exam", {}).get("corpus") or []:
        md5 = item.get("md5")
        if md5 and md5 not in md_text:
            errors.append(f"md 产物缺卷 md5 {md5}（D4③：大客体量下 md 须自足）")
    for name, entry in (report.get("criteria_refs") or {}).items():
        if str((entry or {}).get("path")) not in md_text:
            errors.append(f"md 产物缺判据引用 {name} 的路径 {(entry or {}).get('path')}（D4③）")
    return errors


def check_counts(checks: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for entry in checks or []:
        status = str(entry.get("status"))
        counts[status] = counts.get(status, 0) + 1
    return counts


# ── Markdown 渲染（人读汇报；JSON 才是产物本体） ─────────────────────────────
def render_markdown(report: dict) -> str:
    summary = report.get("summary") or {}
    mode = report.get("mode") or {}
    footnote = report.get("footnote") or {}
    wall = summary.get("file_wall_s") or {}
    lines = [
        f"# speedkit 报告 — {report.get('exam', {}).get('exam_id', '?')}",
        "",
        f"- 判定：**{report.get('verdict')}**（PASS / FAIL / BLOCKED / UNVERIFIED）",
        f"- 驱动：`{mode.get('driver')}`　页并发 k=`{mode.get('page_concurrency')}`　"
        f"缓存策略：`{mode.get('cache_policy')}`（计时轮 {mode.get('timing_rounds')} 轮）",
        f"- 文件墙钟 p50：{_fmt(wall.get('p50'))} s／均值 {_fmt(wall.get('mean'))} s"
        f"（n={wall.get('n')}；舍弃轮 {mode.get('warmup_rounds')}）",
        f"- 页吞吐：{_fmt(summary.get('pages_per_min'))} 页/min；文件吞吐：{_fmt(summary.get('files_per_min'))} 文件/min",
        f"- Σ 后端逐页 {_fmt(summary.get('sum_backend_pages_ms'))} ms／Σ 环 {_fmt(summary.get('sum_rings_ms'))} ms",
        f"- 残差 unclassified={_fmt(summary.get('unclassified_ms'))} ms（{_fmt(summary.get('residual_ratio'))}）"
        f"／并行效率 {_fmt(summary.get('parallel_efficiency'))}",
        f"- 缓存命中 `{collect.HIT_STATUS}`={summary.get('cache_hits')}；in-flight 单列"
        f"（直方图见 JSON `cache.histogram`）",
        f"- 臂：`{(report.get('arm') or {}).get('name')}`（kind={(report.get('arm') or {}).get('kind')}）"
        f"／安装：`{mode.get('install')}`",
        "",
        "## 判据",
        "",
        "| 判据 | 状态 | 细节 |",
        "|---|---|---|",
    ]
    for entry in report.get("checks") or []:
        lines.append(f"| {entry.get('id')} | {entry.get('status')} | {entry.get('detail')} |")
    lines += [
        "",
        "## 环境脚注",
        "",
    ]
    for key in FOOTNOTE_KEYS:
        lines.append(f"- {key}：`{footnote.get(key)}`")
    fingerprints = report.get("fingerprints") or {}
    lines += ["", "## 环境指纹（现场采集）", ""]
    for key in FINGERPRINT_KEYS:
        lines.append(f"- {key}：`{fingerprints.get(key)}`")
    lines.append(f"- sha256：`{fingerprints.get('sha256')}`")
    readiness = report.get("readiness") or {}
    lines.append(f"- 就绪等待：`{readiness.get('ready_wait_sec')}` s／静置：`{readiness.get('settle_sec')}` s")
    lines += [
        "",
        "## 卷清单（md5 + 页数 + 形态）",
        "",
        "| 卷 id | 页数 | md5 | carrier | 文本层非空白字符 |",
        "|---|---|---|---|---|",
    ]
    for item in report.get("exam", {}).get("corpus") or []:
        lines.append(
            f"| {item.get('id')} | {item.get('pages')} | `{item.get('md5')}` | {item.get('carrier')} "
            f"| {item.get('text_chars')} |"
        )
    lines += [
        "",
        "## 判据引用表（判据 ↔ 报告字段路径 ↔ 实测值）",
        "",
        "| 判据 | 字段路径 | 实测值 |",
        "|---|---|---|",
    ]
    for name, entry in (report.get("criteria_refs") or {}).items():
        lines.append(f"| {name} | `{(entry or {}).get('path')}` | {(entry or {}).get('value')} |")
    lines += [
        "",
        "## 未测项",
        "",
    ]
    for item in report.get("not_measured") or []:
        lines.append(f"- {item}")
    lines += [
        "",
        "## 复跑",
        "",
        "```bash",
        " ".join(str(x) for x in (report.get("reproduce") or {}).get("command") or []),
        "```",
        "",
    ]
    return "\n".join(lines)


def _fmt(value) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"{float(value):.3f}"
    return "—" if value is None else str(value)


def write_outputs(report: dict, out_prefix: str) -> tuple[str, str]:
    """写 `<out>.json` + `<out>.md`，返回两个路径。"""
    json_path = out_prefix + ".json"
    md_path = out_prefix + ".md"
    with open(json_path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2, sort_keys=False)
        handle.write("\n")
    with open(md_path, "w", encoding="utf-8") as handle:
        handle.write(render_markdown(report))
    return json_path, md_path
