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
    "", "-", "unknown", "n/a", "na", "none", "null", "todo", "tbd", "test", "placeholder", "占位", "待填",
})

VERDICT_ORDER = ("FAIL", "BLOCKED", "UNVERIFIED", "PASS")

# 数值字段白名单（D5①）：**按完整键路径**逐段钉死，**不做任何后缀匹配**。
# 旧实现按叶子键名后缀放行（`_value`/`_total`/`_ms`…）等于给「改个名字就能溜过」开后门，
# 冷评审第一轮 M12 用 `summary.agg_value` 实证；故改为显式路径表：新增/改名任何测量字段
# 都必须同步改本表，否则 D5.numeric_whitelist 判 FAIL（宁可判告白、不许静默通过）。
# `[i]` 归一为 `[]` 后匹配。放行的「区域」只有两类：判定/引用表（判据载体）与
# 后端回吐的键映射（其叶子键名由后端定义，如 `ocr_has.prepare`）。
_NUMERIC_ALLOW_RES = tuple(re.compile(pattern) for pattern in (
    # ── 判定表 / 判据引用表（value 是判据载体，不是指标） ──────────────────
    r"^checks\[\d+\]\.value$",
    r"^criteria_refs\..*\.value$",
    # ── 运行参数（声明值，非测量值） ──────────────────────────────────────
    r"^mode\.(rounds|warmup_rounds|timing_rounds|page_concurrency|timeout|upload_workers)$",
    r"^mode\.(job_timeout_min|payload_ocr_types|payload_visual_types)$",
    # ── 臂定义（声明值：期望值/patch 数量/探针字段） ──────────────────────
    r"^arm\.(expect|patches|probe)\..*$",
    r"^arm\.expect_p50_s$",
    # ── 机读轮次证据（pid / 时间戳 / 就绪等待与静置实测） ──────────────────
    r"^cold_evidence\[\d+\]\.(round|pid|exit_code|timestamp|start_time)$",
    r"^cold_evidence\[\d+\]\.(ready_wait_s|ready_wait_sec|settle_sec)$",
    r"^readiness\.(ready_wait_sec|settle_sec)$",
    # ── 考卷视图（卷页数 / 文本层字符数，现场机检） ────────────────────────
    r"^exam\.corpus\[\d+\]\.(pages|text_chars)$",
    # ── 缓存两级（D8） ───────────────────────────────────────────────────
    r"^cache\.hits$",
    r"^cache\.per_round\[\d+\]\.(round|hits)$",
    r"^cache(\.per_round\[\d+\])?\.histogram\.[^.]+$",
    # ── summary：测量值逐个点名 ──────────────────────────────────────────
    r"^summary\.(pages_total|pages_measured_timing|pages_measured_all_rounds|steady_pages)$",
    r"^summary\.(files_total|files_timed)$",
    r"^summary\.(sum_backend_pages_ms|sum_harness_pages_ms|sum_rings_ms|unclassified_ms)$",
    r"^summary\.(residual_ratio|closure_ratio|harness_ratio|cache_hits|page_ms_p50)$",
    r"^summary\.(pages_per_min|files_per_min|parallel_efficiency|page_concurrency_peak)$",
    r"^summary\.(file_wall_s|driver_self_wall_s)\.[a-z0-9_]+$",
    r"^summary\.wall_agreement\.[a-z0-9_]+$",
    r"^summary\.wall_agreement\.pairs\[\d+\]\.(file_wall_s|driver_self_wall_s)$",
    r"^summary\.(rings_by_key_ms|cache_histogram)\..+$",
    # ── 逐轮聚合（与 summary 同口径） ────────────────────────────────────
    r"^rounds\[\d+\]\.(round|file_wall_s|driver_self_wall_s|pages_measured)$",
    r"^rounds\[\d+\]\.agg\.(pages_measured|pages_with_backend_total)$",
    r"^rounds\[\d+\]\.agg\.(sum_backend_pages_ms|sum_harness_pages_ms|sum_rings_ms|unclassified_ms)$",
    r"^rounds\[\d+\]\.agg\.(residual_ratio|harness_ratio|cache_hits|page_concurrency_peak)$",
    r"^rounds\[\d+\]\.agg\.(observed_page_concurrency|page_concurrency_values)(\[\d+\])?$",
    r"^rounds\[\d+\]\.agg\.(rings_by_key_ms|cache_histogram|pipelines_seen)\..+$",
    r"^rounds\[\d+\]\.cache\.(round|hits)$",
    r"^rounds\[\d+\]\.cache\.histogram\..+$",
    r"^rounds\[\d+\]\.sidecar\.(box_count|entity_count)$",
    r"^rounds\[\d+\]\.sidecar\.(has_text_ner_truncation_retries|has_text_ner_rebatch_batches)$",
    # ── 逐卷视图（文件级页数/解析元数据/旁证计数） ────────────────────────
    r"^rounds\[\d+\]\.items\[\d+\]\.(pages|box_count|entity_count)$",
    r"^rounds\[\d+\]\.items\[\d+\]\.parse\.total_pages$",
    r"^rounds\[\d+\]\.rejected_items\[\d+\]\.http_status$",
    # ── 逐页信号（两线同构） ─────────────────────────────────────────────
    r"^rounds\[\d+\]\.pages\[\d+\]\.(page|wall_s|root_total_ms|request_total_ms|cache_hits)$",
    r"^rounds\[\d+\]\.pages\[\d+\]\.(harness_start|harness_end)$",
    r"^rounds\[\d+\]\.pages\[\d+\]\.(page_concurrency|page_concurrency_configured)$",
    r"^rounds\[\d+\]\.pages\[\d+\]\.(box_count|entity_count)$",
    r"^rounds\[\d+\]\.pages\[\d+\]\.(ring_leaves_ms|pipeline_totals_ms|duration_breakdown_ms)\..+$",
    r"^rounds\[\d+\]\.pages\[\d+\]\.cache_status\..+$",
    # ── batch 作业元数据（D2④/D6④ 证据；键集合在 driver 侧钉死） ──────────
    r"^rounds\[\d+\]\.batch\.(items_total|items_terminal|failed_items|upload_sec|max_upload_bytes)$",
    r"^rounds\[\d+\]\.batch\.(job_queue_wait_ms|job_poll_count|job_upload_sec)$",
    r"^rounds\[\d+\]\.batch\.(job_items_total|job_items_done|job_http_status|upload_files_per_min)$",
    r"^rounds\[\d+\]\.batch\.(recognition_page_concurrency|recognition_page_concurrency_configured)$",
    r"^rounds\[\d+\]\.batch\.(recognition_page_duration_sum_ms|recognition_wall_ms)$",
    r"^rounds\[\d+\]\.batch\.(recognition_parallelism_ratio|page_ms_p50|pages_per_min)$",
    r"^rounds\[\d+\]\.batch\.item_statuses\.[^.]+$",
    # ── P4 观测（D1 锚点绝对带） ─────────────────────────────────────────
    # ④云门实测：这两条是**我自己的新代码**被自己的白名单抓出来的（闸门有效）。
    # 只放行两条墙钟秒数键；不许写 `observations\..*` 通配，否则给「改名绕过」开后门。
    r"^observations\.anchor_band\.(declared_p50_s|measured_p50_s)$",
))
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


_ABS_PATH_START_RE = re.compile(r"^(?:/|~[\\/]?|\\\\|[A-Za-z]:[\\/])")
_PATH_SEP_RE = re.compile(r"[\\/]")
# 口令面（D5④ 机检）：Bearer 令牌、`password=…` 形态、以及键名命中凭据词的环境变量
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/\-]{12,}")
_CRED_LITERAL_RE = re.compile(r"(?i)\b(pass(?:word|wd)?|secret|token|api[_-]?key)\b\s*[:=]\s*[\"']?([^\s\"',;]{6,})")
_SECRET_KEY_RE = re.compile(r"(?i)(pass(?:word|wd)?|secret|token|api[_-]?key|credential|auth)")
_SECRET_FLAG_RE = re.compile(
    r"(?i)^--?(?:[a-z0-9_]*[-_])?(pass(?:word|wd)?|secret|token|api[_-]?key|credential|auth)(?:=(.*))?$"
)


def redact_path(text) -> str:
    """报告内路径脱敏：`<任何绝对前缀>/exam_d1.json` → `<abs>/…/exam_d1.json`。

    ④云门实测：合成卷也带实例绝对路径，报告拿不到脱敏扫描器零命中（D5④/G4 是硬门）。这里只压
    前缀、保留 basename 供人核对；同一文件的机检身份由 `md5`/`manifest_sha256` 独立承载。
    ②冷评审 I13：`~`（家目录）与 UNC（`\\\\server\\share\\x`）同属绝对路径，一并压。
    **普通相对路径故意不压**——`reproduce.command` 要能照着重放，压了就不是可复现命令了；
    相对路径不携带主机前缀，D5④ 扫描器只把绝对路径形态列为红线。
    """
    value = str(text)
    if not _ABS_PATH_START_RE.match(value):
        return value
    base = _PATH_SEP_RE.split(value.rstrip("\\/"))[-1]
    return f"<abs>/…/{base}" if base else "<abs>"


def scrub_secrets(text) -> str:
    """值内凭据痕迹：`Bearer <token>` 与 `password=…` 形态一律替换（②冷评审 I13 口令面）。"""
    out = _BEARER_RE.sub("Bearer <redacted>", str(text))
    return _CRED_LITERAL_RE.sub(lambda m: f"{m.group(1)}=<redacted>", out)


def redact_env(mapping) -> dict:
    """环境变量回放：键名命中凭据词 ⇒ 整值 `<redacted>`；其余值再过 `scrub_secrets`。"""
    redacted: dict = {}
    for key, value in (mapping or {}).items():
        redacted[key] = "<redacted>" if _SECRET_KEY_RE.search(str(key)) else scrub_secrets(value)
    return redacted


def redact_command(argv) -> list:
    """命令回放：路径 token 压前缀、值内凭据抹掉、凭据类开关的**下一个** token 整体替换。"""
    out = []
    mask_next = False
    for token in argv or []:
        if not isinstance(token, str):
            out.append(token)
            continue
        if mask_next:
            out.append("<redacted>")
            mask_next = False
            continue
        flag = _SECRET_FLAG_RE.match(token)
        if flag:
            if flag.group(2) is None:  # `--api-pass <值>`：值在下一个 token
                out.append(redact_path(scrub_secrets(token)))
                mask_next = True
            else:  # `--api-pass=<值>`：值在本 token 内
                out.append(token[: flag.start(2)] + "<redacted>")
            continue
        out.append(redact_path(scrub_secrets(token)))
    return out


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
# ②冷评审 I17：`hy-smi` 第一行是横幅（`=== System Management Interface ===`），形式真值但零取证力
_BANNER_LINE_RE = re.compile(r"(?i)^[\s=\-*#+]+$|system management interface|^\s*(usage|command)\b")


_COLUMN_TOKENS = frozenset({
    "temp", "avgpwr", "perf", "pwrcap", "vram%", "hcu%", "mode", "gpu%", "mem%", "ecc",
    "fan", "power", "usage", "did", "ssid", "gfx", "ras", "sdma", "umc", "vbios", "bus", "memory",
})
_DEVICE_ROW_RE = re.compile(r"^\d+\s+\S")


def _looks_like_table_header(line: str) -> bool:
    """一眼表头：≥4 个 token 命中已知列名（`hy-smi` 的表头行不承载硬件型号，别当证据）。"""
    tokens = [tok.strip("|").lower() for tok in re.split(r"\s+", line) if tok.strip("|")]
    if len(tokens) < 4:
        return False
    return sum(1 for tok in tokens if tok in _COLUMN_TOKENS) >= 4


def _first_meaningful_line(text: str) -> str:
    """跳过横幅/分隔行与表头行，取第一条承载信息的原始输出行。"""
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped or _BANNER_LINE_RE.search(stripped) or _looks_like_table_header(stripped):
            continue
        return stripped[:200]
    return ""


def _hardware_capture(text: str) -> str:
    """设备清单类输出：优先「表头｜设备行」两段式原始捕获，否则退第一条可用行。

    ④云门实测：DCU 上 `hy-smi` 的首条可用行是**表头**（`HCU Temp AvgPwr …`），作为「硬件」证据
    价值为零；`hy-smi --showhw` 才给出设备身份（DID/SSID/VBIOS/BUS）。这里取设备行并与表头合并，
    全部来自现场命令原始 stdout（禁手填、禁拼造型号）。
    """
    lines = [ln.strip() for ln in (text or "").splitlines()]
    meaningful = [ln for ln in lines if ln and not _BANNER_LINE_RE.search(ln)]
    for idx, line in enumerate(meaningful):
        if _DEVICE_ROW_RE.match(line):
            header = meaningful[idx - 1] if idx > 0 and _looks_like_table_header(meaningful[idx - 1]) else ""
            row = re.sub(r"\s{2,}", " ", line)
            return (f"{re.sub(chr(92) + 's{2,}', ' ', header)}｜{row}" if header else row)[:200]
    return _first_meaningful_line(text)


def hardware_raw(fallback: str = "") -> str:
    """现场硬件原始输出（`nvidia-smi` → `hy-smi` → `/proc/driver`；禁手填）。

    ④云门实测：SCNet 实例的非交互 PATH 里**没有** `hy-smi`（实为 `/opt/hyhal/bin/hy-smi`）⇒ 按绝对
    路径探测；再退 `/proc/cpuinfo` 的 `model name`（实例上给出 `Hygon C86 7185 32-core Processor`）。
    """
    for cmd in (["nvidia-smi", "-L"], ["hy-smi", "--showhw"], ["/opt/hyhal/bin/hy-smi", "--showhw"],
                ["hy-smi"], ["/opt/hyhal/bin/hy-smi"], ["hy-smi", "-L"], ["/opt/hyhal/bin/hy-smi", "-L"],
                ["npu-smi", "info", "-m"]):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=20, check=False)
        except (OSError, subprocess.SubprocessError):
            continue
        if out.returncode == 0 and out.stdout.strip():
            line = _hardware_capture(out.stdout)
            if line:
                return line
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
    """DTK/镜像版本原始输出：`DTK_VERSION` env → 版本文件 → `hy-smi --version`。

    ④云门实测：SCNet 实例上**不是** `/opt/dtk/version`，而是 `/opt/dtk/.dtk_version`（57 B）⇒ 补探测；
    `hy-smi` 同样要按绝对路径试。
    """
    env = os.environ.get("DTK_VERSION")
    if env and env.strip():
        return env.strip()
    for path in ("/opt/dtk/version", "/opt/dtk/.version", "/opt/dtk/.dtk_version",
                 "/usr/local/dtk/version", "/etc/dtk-version"):
        try:
            text = Path(path).read_text(encoding="utf-8", errors="ignore").strip()
        except OSError:
            continue
        if text:
            return text.splitlines()[0][:200]
    for cmd in (["hy-smi", "--version"], ["hy-smi", "-v"],
                ["/opt/hyhal/bin/hy-smi", "--version"], ["/opt/hyhal/bin/hy-smi", "-v"]):
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
        # ②冷评审 C5：脚注第 2/3 项必须与指纹**原始输出**逐字交叉，否则手填型号/DTK 版本可逃逸
        image = str(footnote.get("image_dtk") or "").strip()
        image_raw = str(fingerprints.get("dtk_version_raw") or "").strip()
        if image and image_raw and image != image_raw:
            errors.append(f"脚注 image_dtk {image!r} 与指纹 dtk_version_raw {image_raw!r} 不一致（D5②）")
        hardware = str(footnote.get("hardware") or "").strip()
        hardware_raw_value = str(fingerprints.get("hardware_raw") or "").strip()
        if hardware and hardware_raw_value and hardware != hardware_raw_value:
            errors.append(f"脚注 hardware {hardware!r} 与指纹 hardware_raw {hardware_raw_value!r} 不一致（D5②）")
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
    """D5①：数值叶子的**完整键路径**必须命中白名单（不做后缀匹配，防「改名绕过」）。"""
    return any(pattern.match(path) for pattern in _NUMERIC_ALLOW_RES)


def numeric_whitelist_errors(report: dict) -> list[str]:
    errors: list[str] = []
    for path, value in collect.numeric_leaves(report).items():
        if not numeric_leaf_allowed(path):
            errors.append(f"白名单外的数值字段 {path}={value}（D5①）")
    return errors


def quality_key_errors(report: dict) -> list[str]:
    errors: list[str] = []
    for path in _key_paths(report):
        if path == "quality_eval":
            # D5① 的**必填声明位**（值必须恒为 "not_run"），不是质量指标本身
            if report.get("quality_eval") != "not_run":
                errors.append(f"quality_eval 必须为 'not_run'，实为 {report.get('quality_eval')!r}（D5①）")
            continue
        if QUALITY_SUBTREE_RE.search(path):
            errors.append(f"报告出现质量/指标子树 {path}（D5①）")
            continue
        leaf = _leaf_name(path)
        if QUALITY_KEY_RE.search(leaf):
            errors.append(f"报告出现质量类字段 {path}（D5①）")
    return errors


# ── 判定 ────────────────────────────────────────────────────────────────────
# 「无数据可判」类判据（E1）：装置不可得 ⇒ 0 个计时轮，这些判据**没有被测对象可判**。
# 此时若保持 FAIL，会把「装置连不上」误报成「被测系统不达标」（且 exit 1 而非 3）；
# 故降为 BLOCKED。一旦存在合法计时轮，一律照原样保留 FAIL（判别力不得被降级稀释）。
NO_DATA_CHECK_IDS = frozenset({
    "D2.batch_key_set", "D3.closure_same_clock", "D3.residual_two_sided",
    "D4.wall_agreement", "D4.self_sufficiency", "D5.readiness_probe",
})


def downgrade_no_data_checks(checks: list[dict], *, measured: bool) -> list[dict]:
    """无计时轮时把「无数据可判」类 FAIL 降为 BLOCKED（明细里点名原因，不改判据名）。"""
    if measured:
        return list(checks)
    out: list[dict] = []
    for check in checks:
        if str(check.get("status")) == "FAIL" and check.get("id") in NO_DATA_CHECK_IDS:
            out.append({**check, "status": "BLOCKED",
                        "detail": f"{check.get('detail')}｜装置不可得：无计时轮，该项无数据可判（E1）"})
        else:
            out.append(check)
    return out


def dedupe_checks(checks: list[dict]) -> list[dict]:
    """同一 `id` 只保留**最严**的一条（④云门实测：臂检查 pre/post 两相天然重名）。

    报告是机器判据的载体：同一 id 出现两条时，外部消费者按 id 取值会拿到歧义答案
    （k=3 实测 `D8.env_arm_no_file_write` 同时 PASS 两次）。这里按四态取最严、保留首次
    出现的位置，并在 detail 末尾注明条数——**不新增数值字段**（白名单只放行具名路径）。
    """
    if not checks:
        return []
    order: list[str] = []
    worst: dict[str, dict] = {}
    counts: dict[str, int] = {}
    for check in checks:
        cid = check.get("id")
        counts[cid] = counts.get(cid, 0) + 1
        if cid not in worst:
            order.append(cid)
            worst[cid] = check
            continue
        if VERDICT_ORDER.index(str(check.get("status"))) < VERDICT_ORDER.index(str(worst[cid].get("status"))):
            worst[cid] = check
    out: list[dict] = []
    for cid in order:
        check = dict(worst[cid])
        if counts[cid] > 1:
            check["detail"] = f"{check.get('detail') or ''}（同 id {counts[cid]} 条，取最严）"
        out.append(check)
    return out


def verdict_from_checks(checks: list[dict]) -> str:
    """四态归并：`FAIL` > `BLOCKED` > `UNVERIFIED` > `PASS`。

    空集合判 **FAIL**（③ 第一轮冷评审 I9）：`[]` 返回 `PASS` 会让「压根没跑 / 报告被截断 /
    checks 被清空」伪装成通过——这是恒真判据的一种形态，不放行。
    """
    if not checks:
        return "FAIL"
    statuses = {str(c.get("status")) for c in checks}
    for verdict in VERDICT_ORDER:
        if verdict in statuses:
            return verdict
    return "FAIL"  # 有检查项但状态全是未知字串：同样不得读作通过


def build_report(
    *, exam: dict, mode: dict, arm: dict, rounds: list[dict], cold_evidence: list[dict],
    summary: dict, cache: dict, footnote: dict, fingerprints: dict, criteria_spec: dict[str, str],
    reproduce: dict, checks: list[dict], not_measured: list[str],
    started_at: str, finished_at: str, has_gt: bool = False, readiness: dict | None = None,
    observations: dict | None = None,
) -> dict:
    round_views = []
    for entry in rounds:
        view = {
            "round": entry.get("round"),
            "warmup": bool(entry.get("warmup")),
            "invalid": bool(entry.get("invalid")),
            "invalid_reason": entry.get("invalid_reason"),
            "harness_start_ts": entry.get("harness_start_ts"),
            "harness_end_ts": entry.get("harness_end_ts"),
            "file_wall_s": entry.get("file_wall_s"),
            "driver_self_wall_s": entry.get("driver_self_wall_s"),
            "pages_measured": len(entry.get("pages") or []),
            "agg": entry.get("agg") or {},
            "cache": entry.get("cache") or {},
            "sidecar": entry.get("sidecar") or {},
            "pages": entry.get("pages") or [],
        }
        # batch 线证据（job_queue_wait_ms / max_upload_bytes / 识别面并发）必须落进报告：
        # D2④ 与 D6④ 要求它可核验；single 线不得出现该键（留 None 会与 D2④ 键集合判据混淆）。
        if entry.get("batch"):
            view["batch"] = entry["batch"]
        round_views.append(view)
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
        # P4 一类**观测**（不进 checks、不参与四态判定）：如锚点绝对带出带标记
        "observations": dict(observations or {}),
        "not_measured": list(not_measured or []),
        "reproduce": reproduce,
        "verdict": "UNVERIFIED",
    }
    report["criteria_refs"] = collect.criteria_refs(report, criteria_spec)
    return report


def has_timing_rounds(report: dict) -> bool:
    """报告里是否存在**可核验的计时轮**（非预热、非非法）。"""
    return any(
        not round_view.get("warmup") and not round_view.get("invalid")
        for round_view in (report.get("rounds") or [])
    )


def finalize(report: dict, *, api_base: str, live_code_rev: str | None = None) -> dict:
    """装配派生判据（自足/白名单/引用表）并落四态判定。"""
    live_rev = live_code_rev if live_code_rev is not None else runtime_code_rev()
    derived: list[dict] = []
    # 装置不可得/全部轮次非法 ⇒ 报告里没有被测对象，派生判据无处可查：判 UNVERIFIED 而非 FAIL
    # （验收方案 E1：装置不可得是 BLOCKED 语义；BLOCKED 在四态顺序里先于 UNVERIFIED，终态仍为 BLOCKED）。
    # 有计时轮时一律按 FAIL——「改个名字就能溜过」「缺脚注」「混入质量字段」都不许洗白。
    measured = has_timing_rounds(report)

    def _status(err: str, *, always_unverified: str | None = None) -> str:
        if not measured:
            return "UNVERIFIED"
        if always_unverified and always_unverified in err:
            return "UNVERIFIED"
        return "FAIL"

    fp_errors = validate_fingerprints(report.get("fingerprints") or {})
    for err in fp_errors:
        derived.append({"id": "D5.fingerprints",
                        "status": _status(err, always_unverified="dtk_version_raw 为空"),
                        "detail": err, "value": None})
    for err in validate_footnote(
        report.get("footnote") or {}, api_base=api_base, live_code_rev=live_rev,
        fingerprints=report.get("fingerprints") or None, readiness=report.get("readiness") or None,
    ):
        derived.append({"id": "D5.footnote",
                        "status": _status(err, always_unverified="就绪等待/静置实测缺失"),
                        "detail": err, "value": None})
    for err in quality_key_errors(report):
        derived.append({"id": "D5.no_quality_fields", "status": _status(err), "detail": err, "value": None})
    for err in numeric_whitelist_errors(report):
        derived.append({"id": "D5.numeric_whitelist", "status": _status(err), "detail": err, "value": None})
    for err in collect.validate_criteria_refs(report):
        derived.append({"id": "D4.criteria_refs", "status": _status(err), "detail": err, "value": None})

    checks = dedupe_checks(
        downgrade_no_data_checks(list(report.get("checks") or []), measured=measured) + derived
    )
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
    observations = report.get("observations") or {}
    wall = summary.get("file_wall_s") or {}
    lines = [
        f"# speedkit 报告 — {report.get('exam', {}).get('exam_id', '?')}",
        "",
        f"- 判定：**{report.get('verdict')}**（PASS / FAIL / BLOCKED / UNVERIFIED）",
        f"- 驱动：`{mode.get('driver')}`　页并发 k=`{mode.get('page_concurrency')}`　"
        f"缓存策略：`{mode.get('cache_policy')}`（计时轮 {mode.get('timing_rounds')} 轮）",
        f"- 文件墙钟 p50：{_fmt(wall.get('p50'))} s／均值 {_fmt(wall.get('mean'))} s"
        f"（n={wall.get('n')}；IQR {_fmt(wall.get('iqr'))} s；舍弃轮 {mode.get('warmup_rounds')}）",
        f"- 墙钟口径：{mode.get('file_wall_s_semantics') or '（未声明）'}",
    ]
    band = (observations or {}).get("anchor_band") or {}
    if band:
        lines.append(
            f"- 锚点带观测（P4，**不影响判定**）：声明 p50 {band.get('declared_p50_s')} s／"
            f"实测 {band.get('measured_p50_s')} s／偏差 {band.get('deviation')}"
            f"／out_of_band={'true' if band.get('out_of_band') else 'false'}"
        )
    lines +=[
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
