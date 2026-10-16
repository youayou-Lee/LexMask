#!/usr/bin/env python3
"""check_public_tree.py —— 公开树红线扫描器（验收方案 v2 G4）。

与 `check_report_redaction.py` 同族，但**扫的是公开仓本身**（含新增文档与 CHANGELOG），
用途是给「敏感物不出私有体系」（AGENTS.md 铁律①）一个**有失败入口**的机检：

1. **公网 IP**：回环 / RFC1918 私网 / RFC5737 文档段放行（测试夹具与示例常用），
   其余一律命中——真实实例地址另经 `--denylist` 点名；
2. **绝对路径**：POSIX 根用户/家目录/挂载点等前缀、Windows 盘符路径（`--allow-prefix` 放行指定前缀）；
3. **凭据形态**（`--self-test` 逐类断言的 10 类）：键名含凭据词的普通赋值 / 带类型注解赋值（含
   PEP 604 联合、泛型、前向引用）/ 引号键（JSON·YAML·dict，键名允许 `-`）/ `--flag` 后的引号
   字面量或空格裸值 / 无引号字母数字混合值 / `Bearer <≥24 字符>` / 厂商前缀密钥（`sk-`·`ghp_`·
   `AKIA…`）/ URL 内联凭据 / HTTP Basic。带键名的分支要求值 ≥8 字符且**像真口令**（含数字或非
   字母数字符号，且非占位词/模板/类型名/ID 形态/JSON 片段/CJK 文案）；`os.environ[…]`、
   `self._password` 这类引用与代码取值不算。**覆盖与不覆盖的完整边界**见文件内 `_COVERAGE_NOTE`；
4. **`--denylist` 词表**（一行一词，`re:` 前缀走正则）——真实案卷名、实例域名、口令进这里。

行内放过：含 `public-tree-scan: ignore-line` 的行跳过（扫描器自己的阳性对照夹具、以及确非凭据
的既有行需要它；每处都有就地说明）。

自带阳性对照：`--self-test` 用含上述 10 类痕迹的样本断言「必须全部命中」，再用干净样本断言
「必须零命中」——**没有阳性对照的扫描器等于恒真判据**。

用法::

    python eval-ng/scripts/check_public_tree.py                      # 默认扫 --base 以来改动过的文件
    python eval-ng/scripts/check_public_tree.py --paths docs/ README.md
    python eval-ng/scripts/check_public_tree.py --denylist <私有词表> --allow-prefix <公开工具链前缀>
    python eval-ng/scripts/check_public_tree.py --self-test

exit：0=干净；1=命中（或阳性对照失败）；2=用法错误。
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

# 前面不得是数字或点：DICOM/OID 这类点分数字长串（`1.2.840.10008.5.1.4.1.1.2`）里的四个八位组
# 曾被当成公网 IP（⑨冷评审期间的实测假阳性），所以把「点/数字紧跟在前」的候选整类排除。
IP_RE = re.compile(r"(?<![\d.])(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b")
ABS_PATH_RE = re.compile(
    r"(?<![\w.\-/])(/(?:root|home|mnt|data|opt|srv|var|Users|tmp|workspace|media)/[^\s\"'`,)]*)"
)
WIN_PATH_RE = re.compile(r"(?<![\w])[A-Za-z]:\\[^\s\"'`,)]*")
# ── 凭据类：键名按 `_` / `-` / camelCase 切成词元后查词表 ──
# R6 教训：旧式 `\b(?:secret|token|password)\b` 对 `api_secret` / `db_password` 这类「凭据词作后缀」的
# 复合键**没有边界**（`_` 属 `\w`），整类漏网；而 `(?i)` 又不能与 camelCase 边界断言共存。
# 因此键名判定改为 Python 词元判定（前缀、后缀、camelCase 通吃），值只看「字面量形状」，豁免只看语境。
CREDENTIAL_WORDS = frozenset({
    "password", "passwd", "pwd", "pass", "passphrase", "secret", "token", "key",
    "apikey", "credential", "auth", "bearer",
})
# 元数据尾巴：`token_count=12` / `token_type: "refresh_token"` / `secret_name` / `api_key_id` 里的值不是凭据本身
META_TAIL_WORDS = frozenset({
    "count", "counts", "type", "types", "kind", "name", "names", "len", "length", "size", "prefix",
    "suffix", "field", "fields", "header", "headers", "url", "uri", "endpoint", "method", "format",
    "algorithm", "algo", "ttl", "expiry", "expires", "version", "bucket", "id", "ids", "ref", "index",
    "idx", "status", "state", "mode", "path", "dir", "file", "location", "store", "cache", "table",
    "schema", "policy", "rule", "rules", "flag", "enabled", "bytes", "ms", "sec", "seconds", "limit",
    "hex", "public", "pubkey", "source", "target", "cipher", "encoding", "digest",
})
# 协议/枚举关键字值（如 fetch 的 credentials 取 same-origin、token_type 取 bearer）不是凭据
VALUE_KEYWORDS = frozenset({
    "same-origin", "same-site", "include", "omit", "no-cors", "cors", "bearer", "basic", "digest",
    "none", "null", "true", "false", "default", "inherit", "unset", "optional", "required",
})
# 豁免只认「语境」与「模板/占位形态」，**绝不按值的普通形状豁免**
# （第五轮教训：`(?i)` 作用到 `^[A-Z][A-Z0-9_]{2,}$` 后等价于「任何纯字母数字值」，真口令漏网）
ENV_LOOKUP_RE = re.compile(r"(?i)(?:os\.)?(?:environ|getenv)\b|environ\s*\[")
TEMPLATE_VALUE_RE = re.compile(r"[<>{}]|\$\{?\w+")
PLACEHOLDER_WORDS = frozenset({
    "changeme", "dummy", "placeholder", "example", "redacted", "your-password", "your_password",
    "test-token", "test_token", "test-password", "fake-token", "sample-token",
})
PLACEHOLDER_SUBSTRINGS = (
    "changeme", "change-me", "change_me", "placeholder", "example", "dummy", "redacted",
    "your-", "your_", "xxxx", "****", "todo", "test-token", "test_token", "test-password",
    "fake-", "sample-", "not-a-real", "not-a-credential", "notreal",
    "unit-test", "-test-", "test-", "deterministic-", "example-",
)
CODE_VALUE_PREFIXES = (
    "settings.", "os.", "self.", "cls.", "config.", "request.", "args.", "env.", "params.", "kwargs.",
)
# 值本身就是令牌形态（与键名无关）：`Bearer <长串>`、厂商前缀密钥、URL 内联凭据、HTTP Basic
TOKEN_RES = (
    ("bearer", re.compile(r"\bBearer\s+[A-Za-z0-9\-._~+/]{24,}")),
    # 必须带厂商分隔符，否则 `skeletonButtonSize` / `skippedNoCandidates` 这类标识符会误报。
    # 分隔符两种都要认：GitHub 系真实形态用**下划线**（`ghp_…`/`gho_…`/`ghs_…`/`github_pat_…`，
    # ⑩冷评审 R10-1 实测：硬写 `-` 时这些在无凭据键名的语境整类静默通过）。
    ("api_key", re.compile(
        r"\b(?:sk|rk)-[A-Za-z0-9_-]{16,}\b"
        r"|\b(?:ghp|gho|ghs|github_pat)[-_][A-Za-z0-9_-]{16,}\b"
        r"|AKIA[0-9A-Z]{16}"
    )),
    # 连接串内联凭据（`协议://用户:口令@主机` 形态；口令 ≥8 字符，避免占位/变量形态）
    ("url_credential", re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.\-]*://[^\s:/@]{1,40}:(?P<urlsecret>[^\s/@]{8,})@")),
    # HTTP Basic 的 base64 凭据（≥16 字符，且必须长到不像普通词）
    ("basic_auth", re.compile(r"\bBasic\s+(?P<basicauth>[A-Za-z0-9+/]{16,}={0,2})")),
)
_WORD_SPLIT_RE = re.compile(r"[^A-Za-z0-9]+")
_CAMEL_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z0-9]+")
_VALUE_GROUP = r'(?P<val>"[^"\n]{1,200}"|\'[^\'\n]{1,200}\'|[^\s,;)]{1,120})'
CREDENTIAL_ASSIGN_RE = re.compile(
    r"(?P<name>[A-Za-z_][A-Za-z0-9_]{0,40})\s*[:=]\s*" + _VALUE_GROUP
)
# 引号键（JSON / YAML / dict 字面量）：键名被引号包起、后跟冒号或等号与一个口令字面量。
# 普通赋值的 name 组不含引号，键被引号包起时针冒号/等号之前先撞上 `"` ⇒ 整类漏网（⑧期预前加固实测）。
# 键名允许 `-`（HTTP 头/配置文件惯用：`"api-key"`、`"x-api-key"`、`"auth-token"`，⑨冷评审 R9-1）。
QUOTED_KEY_ASSIGN_RE = re.compile(
    r"(?P<q>[\"'])(?P<name>[A-Za-z_][A-Za-z0-9_\-]{0,40})(?P=q)\s*[:=]\s*" + _VALUE_GROUP
)
CREDENTIAL_FLAG_RE = re.compile(r"(?P<name>--[A-Za-z][A-Za-z0-9_-]{1,40})")
# 命令行 flag 后的**裸值**（无引号）：`--api-pass Ab12Cd34`（⑨冷评审 R9-3）。
_BARE_FLAG_VALUE_RE = re.compile(r"\s*(?P<val>[^\s,;)\"']{8,60})")
# 带类型注解的赋值：`NAME: <注解> = "<真口令>"`（Python 惯用写法）。
# `CREDENTIAL_ASSIGN_RE` 的 `[:=]` 会把注解的冒号吃掉、把 `str` 当值，`finditer` 随即越过真值 ⇒ 整类漏网；
# 注解段必须锚到标识符开头（允许先出现引号：顶层前向引用 `"Cfg"` / `"pkg.Cfg"`，⑨冷评审 R9-2）、
# 不含 `=` 与换行（防跨语句/跨赋值误配），但要覆盖 PEP 604 联合 `str | None`、泛型 `Final[str | None]` /
# `Literal["a"]` / `Optional["Cfg"]` 等引号与括号形态（⑧冷评审 R8-1）。
ANNOTATED_ASSIGN_RE = re.compile(
    r'(?P<name>[A-Za-z_][A-Za-z0-9_]{0,40})\s*:\s*(?P<ann>["\']?[A-Za-z_][\w\.\[\], |"\'()]{0,80}?)\s*=\s*' + _VALUE_GROUP
)
# 引号字面量用**零宽前瞻**配对（`finditer` 会吃掉共享引号：`"--flag", default="VALUE"` 里
# 第 2、3 个引号分属两段，普通匹配会把 `", default="` 整段吞掉导致真值漏网）
_QUOTED_WINDOW_RE = re.compile(r"(?=(?P<q>[\"'])(?P<val>.{8,}?)(?P=q))")
CREDENTIAL_RULES = frozenset({"credential_literal", "credential_flag", "credential_bare"})


def name_tokens(name: str) -> list[str]:
    """`db_password` / `api-secret` / `apiSecret` → 词元列表（大小写与分隔符通吃）。"""
    out: list[str] = []
    for part in _WORD_SPLIT_RE.split(name.lstrip("-")):
        out.extend(_CAMEL_RE.findall(part))
    return [token.lower() for token in out if token]


def is_credential_name(name: str) -> bool:
    tokens = name_tokens(name)
    if not tokens:
        return False
    tail = tokens[-1]
    if tail in META_TAIL_WORDS or tail.rstrip("s") in META_TAIL_WORDS:
        return False
    if any(token in CREDENTIAL_WORDS or token.rstrip("s") in CREDENTIAL_WORDS for token in tokens):
        return True
    compact = re.sub(r"[^a-z0-9]", "", name.lower())
    return any(word in compact for word in ("password", "passwd", "passphrase", "apikey", "secretkey"))


_CJK_RE = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")
# CamelCase 类型名（如 Ed25519PrivateKey）：≥2 个「驼峰」且够长；`Zz9Kk2Lm` 这类口令会被排除在外
_CAMEL_TYPE_RE = re.compile(r"^[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+)+$")
# 口令里常见的符号（**不含** `_` `-` `.` `/`，否则 access_token / my-secret-name / a/b 会被误判）
_SECRET_SYMBOL_RE = re.compile(r"[!@#$%^&*()+={}\[\]|;:'\"<>?,~`]")
_DELIMITER_RE = re.compile(r"[=,\s{}\[\]:;\"]")
_ID_SHAPE_RE = re.compile(r"^(?:[A-Z]{2,}|[A-Za-z]{2,})[-_]\d{1,6}$")


def _looks_like_type_name(value: str) -> bool:
    """`Ed25519PrivateKey` 这类 CamelCase 类型名不是口令（短口令形如 `Zz9Kk2Lm` 不在此列）。"""
    return bool(_CAMEL_TYPE_RE.match(value)) and len(value) >= 15


def _is_secret_shaped(value: str) -> bool:
    if not value or _CJK_RE.search(value) or _DELIMITER_RE.search(value):
        return False
    if _looks_like_type_name(value) or _ID_SHAPE_RE.match(value):
        return False
    return bool(re.search(r"\d", value)) or bool(_SECRET_SYMBOL_RE.search(value))


def _is_placeholder_value(value: str) -> bool:
    low = value.strip().strip("\"'").lower()
    return low in PLACEHOLDER_WORDS or low in VALUE_KEYWORDS or any(sub in low for sub in PLACEHOLDER_SUBSTRINGS)


def _literal_hit(val: str) -> bool:
    """值是不是「像凭据的字面量」——只判形状，语义交给豁免。"""
    raw = val.strip()
    if not raw:
        return False
    if raw[0] in "\"'":
        body = raw[1:].rstrip("\"'")
        if len(body) < 8 or body.startswith("-----"):
            return False
        if TEMPLATE_VALUE_RE.search(body) or _is_placeholder_value(body):
            return False
        return _is_secret_shaped(body)
    if any(ch in raw for ch in "(.["):
        return False  # 调用/属性/下标：值是代码引用，不是字面量
    body = raw.rstrip(",")
    if len(body) < 8 or TEMPLATE_VALUE_RE.search(body) or _is_placeholder_value(body):
        return False
    if not (bool(re.search(r"[A-Za-z]", body)) and bool(re.search(r"\d", body))):
        return False
    return _is_secret_shaped(body)


LOOPBACK_PREFIXES = ("127.", "0.0.0.0", "::1")
# 私网（RFC1918）与文档段（RFC5737）+ 保留段：示例与夹具的合法取值，不是实例地址
PRIVATE_IP_RE = re.compile(
    r"^(?:10\.|192\.168\.|172\.(?:1[6-9]|2\d|3[01])\.|192\.0\.2\.|198\.51\.100\.|203\.0\.113\.)"
)
IGNORE_MARKER = "public-tree-scan: ignore-line"
SKIP_SUFFIXES = frozenset({
    ".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".zip", ".gz", ".whl", ".bin",
    ".so", ".dylib", ".dll", ".pt", ".pth", ".onnx", ".safetensors", ".docx", ".xlsx", ".pptx",
    ".woff", ".woff2", ".ttf", ".otf", ".mp4", ".mov", ".db", ".sqlite", ".lock",
})
MAX_BYTES = 2 * 1024 * 1024

SELF_TEST_BAD = (
    "instance 8.8.8.8 path /srv/evidence/arm.json win C:\\Users\\dev\\secret.txt "  # public-tree-scan: ignore-line
    "auth Bearer abcdefghijklmnopqrstuvwxyz012345 password=\"Passw0rd!2026\" deny SCNET-INSTANCE-1 "  # public-tree-scan: ignore-line
    # R1 形态：flag 与 add_argument(default=) 两种写法都必须落网（值用合成串，不是真实口令）
    "cli --api-pass \"Synthetic-Pass!123\" "  # public-tree-scan: ignore-line
    "parser.add_argument(\"--api-pass\", default=\"Synthetic-Pass!123\")"  # public-tree-scan: ignore-line
    "cfg password=Hunter2Hunter2 "  # public-tree-scan: ignore-line
    # R6 形态：凭据词作**后缀**的复合键与 camelCase 键（旧 `\b` 正则整类漏网）
    "cfg api_secret=Zz9Kk2Lm "  # public-tree-scan: ignore-line
    "cfg db_password=\"Hunter2Hunter2\" "  # public-tree-scan: ignore-line
    "cfg API_SECRET = \"Zz9Kk2Lm\" "  # public-tree-scan: ignore-line
    "cfg clientSecret: \"Ab3xY9Qq2\" "  # public-tree-scan: ignore-line
    "cfg access_token = \"ghp_abcdefghijklmnopqrstuvwxyz0123456\" "  # public-tree-scan: ignore-line
    # R7-1 形态：带类型注解的赋值（注解冒号会吃掉值位置，`str` 被当值 ⇒ 整类漏网）
    "cfg client_secret: str = \"Ab12Cd34\" "  # public-tree-scan: ignore-line
    'api_key: str | None = "Ab12Cd34"'  # public-tree-scan: ignore-line,
    # 与键名无关的令牌形态：URL 内联凭据与 HTTP Basic（值均为合成串）
    "db postgresql://eval_user:Synthetic-Pass!123@127.0.0.1:5432/app "  # public-tree-scan: ignore-line
    "auth Basic U3ludGhldGljLVBhc3MhMTIz"  # public-tree-scan: ignore-line

)
SELF_TEST_CLEAN = (
    "api_base http://127.0.0.1:8000 fixture 203.0.113.9 host 10.1.2.3 "
    "password=<你的密码> api_key=os.environ['X'] path testdata/canary-issue106-scan-10p.pdf "
    # R6 反例：元数据尾巴键、协议关键字值、代码引用/调用都不得命中
    "token_count=12 token_type: \"refresh_token\" secret_name = \"my-secret-name\" "
    "credentials: \"same-origin\" api_key_id = \"key-1234567\" "
    "password=body.password secret = json.load token = getCsrfToken monkey = \"abc12345\" "
    # R10-1 假阳性守卫：厂商前缀正则的字母标识符邻居（`skeleton…`/`skippedNoCandidates`）不得命中
    "skeletonButtonSize skippedNoCandidates "
    "--api-pass\", default=os.environ.get(\"EVAL_API_PASS\")"
)


def _is_public_ip(ip: str) -> bool:
    if any(ip.startswith(prefix) for prefix in LOOPBACK_PREFIXES):
        return False
    return not PRIVATE_IP_RE.match(ip)


_COVERAGE_NOTE = """凭据面覆盖边界（⑦⑧⑨冷评审反复追问的口径，明写以免「静默盲区」）。
覆盖：普通赋值 `n = "v"`；带类型注解赋值 `n: Ann = "v"`（含 PEP 604 `|`、泛型、顶层前向引用）；
引号键（JSON/YAML/dict，键名可含 `-`）；`--flag` 后的引号字面量**与空格裸值**；无引号字母数字
混合值；Bearer / 厂商前缀密钥 / URL 内联凭据 / HTTP Basic。
不覆盖（设计边界，非静默盲区）：① 编码/拼接/运行时构造的口令（base64 常量、`"a"+"b"`、f-string
插值——后者按模板豁免）；② 跨行材料（PEM 私钥正文逐行 `-`，形状判定视为分隔符）；③ 仅在密钥库/
环境/凭据文件中出现、代码内无字面量的凭据；④ 长度 <8 或纯占位词的值。
值形状判定要求「含数字或非字母数字符号」且非分隔符/JSON 片段/CJK 文案/类型名/ID 形态。"""
def load_denylist(path: str | None) -> list[str]:
    if not path:
        return []
    return [line.strip() for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def _window_quoted_values(window: str) -> list[str]:
    """取出窗口里的引号字面量（不消费共享引号）。

    `default="VALUE"` 的第 2 个引号同时是上一段（`"--flag"`）的收尾，普通 `finditer` 会把
    `", default="` 整段吃掉，紧随其后的真值（`Qq11Ww22`）再无引号可配对而漏网（R6 实测）。
    实现用**零宽前瞻**（`_QUOTED_WINDOW_RE`）配对，匹配不消费字符，共享引号不再吃值。
    """
    parts = _QUOTED_WINDOW_RE.findall(window)
    return [value for _, value in parts if len(value) >= 8]


def credential_findings(text: str) -> list[tuple[str, str]]:
    """口令面唯一真源：`check_report_redaction.py` 与公开树门共用本函数，避免两处口径漂移。

    规则名沿用既有三态：`credential_literal`（键名 + 引号字面量）、`credential_flag`
    （`--flag` 后跟引号字面量）、`credential_bare`（键名 + 无引号字母数字混合值）；
    另有 `bearer` / `api_key` / `url_credential` / `basic_auth` 四条与键名无关的令牌形态。
    """
    findings: list[tuple[str, str]] = []
    for rule, regex in TOKEN_RES:
        for match in regex.finditer(text):
            findings.append((rule, match.group(0)))
    # 普通赋值（`name = val` / `name: val`）与带类型注解的赋值（`name: Type = val`）取并：
    # 后者若单靠前一个正则，注解冒号会把值位置吃掉 ⇒ 整类漏网（⑦冷评审 R7-1）。
    assign_regexes = (CREDENTIAL_ASSIGN_RE, ANNOTATED_ASSIGN_RE, QUOTED_KEY_ASSIGN_RE)
    for regex in assign_regexes:
        for match in regex.finditer(text):
            if not is_credential_name(match.group("name")):
                continue
            window = match.group(0)
            if ENV_LOOKUP_RE.search(window):
                continue  # 语境：读环境变量，不是内置口令
            val = match.group("val")
            if not _literal_hit(val):
                continue
            rule = "credential_literal" if val[:1] in "\"'" else "credential_bare"
            findings.append((rule, window))
    for flag in CREDENTIAL_FLAG_RE.finditer(text):
        if not is_credential_name(flag.group("name")):
            continue
        window = text[flag.end(): flag.end() + 120]
        if ENV_LOOKUP_RE.search(window):
            continue
        same_line = window.split("\n", 1)[0]
        matched = False
        for flag_val in _window_quoted_values(same_line):
            if _is_placeholder_value(flag_val) or TEMPLATE_VALUE_RE.search(flag_val):
                continue
            if not _is_secret_shaped(flag_val):
                continue
            findings.append(("credential_flag", flag.group(0) + " " + flag_val))
            matched = True
            break
        if matched:
            continue
        # 空格分隔的裸值形态（`--api-pass Ab12Cd34`，⑨冷评审 R9-3）：仅看同一行，且与其它分支同形状判据。
        bare = _BARE_FLAG_VALUE_RE.match(same_line)
        if bare is None:
            continue
        bare_val = bare.group("val")
        if _is_placeholder_value(bare_val) or TEMPLATE_VALUE_RE.search(bare_val):
            continue
        if not _is_secret_shaped(bare_val):
            continue
        findings.append(("credential_flag", flag.group(0) + " " + bare_val))
    return findings


def scan(text: str, *, denylist: list[str], allow_prefixes: list[str]) -> list[tuple[str, str]]:
    """返回 [(规则, 命中片段), ...]。"""
    findings: list[tuple[str, str]] = []
    for match in IP_RE.finditer(text):
        if _is_public_ip(match.group(0)):
            findings.append(("public_ip", match.group(0)))
    for rule, regex in (("abs_path", ABS_PATH_RE), ("win_path", WIN_PATH_RE)):
        for match in regex.finditer(text):
            value = match.group(0)
            if any(value.startswith(prefix) for prefix in allow_prefixes):
                continue
            findings.append((rule, value))
    findings.extend(credential_findings(text))
    for token in denylist:
        if token.startswith("re:"):
            for match in re.finditer(token[3:], text):
                findings.append(("denylist_re", match.group(0)))
        elif token and token in text:
            findings.append(("denylist", token))
    return findings


def scan_source(name: str, text: str, *, denylist: list[str], allow_prefixes: list[str]) -> list[str]:
    """逐行扫描并带行号；含放过标记的行跳过。"""
    lines: list[str] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if IGNORE_MARKER in line:
            continue
        for rule, value in scan(line, denylist=denylist, allow_prefixes=allow_prefixes):
            lines.append(f"{name}:{lineno}: [{rule}] {value}")
    return lines


def _git_lines(repo_root: Path, args: list[str]) -> list[str]:
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_root), *args], check=True, capture_output=True, text=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return []
    return [line for line in out.split("\0" if "-z" in args else "\n") if line.strip()]


def default_paths(repo_root: Path, base: str | None) -> list[Path]:
    """默认扫「本次改动新增/修改的文件」——G4 的判决面是本次交付物，不是整棵历史树。

    `--base` 给定时取三者并集：`<base>...HEAD` 的已提交改动、**工作区未提交改动**（含未跟踪文件）。
    这样门禁在提交前就能跑——否则扫描器会在最需要它的时刻漏掉改动。
    给空串则退化为整树 `git ls-files`（只适合人工巡检，历史文件有既有噪声）。
    """
    if not base:
        return [repo_root / rel for rel in _git_lines(repo_root, ["ls-files", "-z"])]
    rels = _git_lines(repo_root, ["diff", "--name-only", "--diff-filter=AM", f"{base}...HEAD"])
    rels += _git_lines(repo_root, ["diff", "--name-only", "--diff-filter=AM", "HEAD"])
    rels += _git_lines(repo_root, ["ls-files", "--others", "--exclude-standard"])
    return [repo_root / rel for rel in dict.fromkeys(rels)]


def _iter_targets(repo_root: Path, paths: list[str], base: str | None) -> list[Path]:
    if paths:
        candidates: list[Path] = []
        for raw in paths:
            target = Path(raw)
            if not target.is_absolute():
                target = repo_root / raw
            if target.is_dir():
                candidates.extend(sorted(p for p in target.rglob("*") if p.is_file()))
            else:
                candidates.append(target)
        return candidates
    return default_paths(repo_root, base)


def self_test(denylist: list[str] | None = None) -> int:
    """阳性对照：坏样本必须**逐类**全中（类别清单见 `required`），干净样本必须零命中。"""
    words = list(denylist or [])
    bad = scan_source("self-test-bad", SELF_TEST_BAD, denylist=words + ["SCNET-INSTANCE-1"],
                      allow_prefixes=[])
    rules = {line.split("[", 1)[1].split("]", 1)[0] for line in bad}
    required = {
        "public_ip", "abs_path", "win_path", "bearer", "url_credential", "basic_auth",
        "credential_literal", "credential_flag", "credential_bare", "denylist",
    }
    missing = required - rules
    clean = scan_source("self-test-clean", SELF_TEST_CLEAN, denylist=words, allow_prefixes=[])
    problems: list[str] = []
    if missing:
        problems.append(f"阳性对照未命中：{sorted(missing)}")
    if clean:
        problems.append(f"干净样本被误报：{clean}")
    if problems:
        for line in problems:
            print(f"[self-test] {line}", file=sys.stderr)
        return 1
    print(f"[self-test] 阳性对照 {len(required)} 类全中，干净样本零命中")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="公开树红线扫描器（G4）")
    parser.add_argument("--repo-root", default=".", help="公开仓根（默认当前目录）")
    parser.add_argument("--paths", nargs="*", default=None, help="只扫这些路径（默认扫 --base 以来改动过的文件）")
    parser.add_argument(
        "--base", default="origin/preview",
        help="改动基线（默认 origin/preview，即扫本次 PR 新增/修改的文件）；给空串则退化为整树 git ls-files",
    )
    parser.add_argument("--denylist", default=None, help="词表文件（一行一词；re: 前缀走正则）")
    parser.add_argument("--allow-prefix", action="append", default=[], help="放行的路径前缀（可重复）")
    parser.add_argument("--self-test", action="store_true", help="只跑阳性对照")
    args = parser.parse_args(argv)

    denylist = load_denylist(args.denylist)
    if args.self_test:
        return self_test(denylist)

    repo_root = Path(args.repo_root).resolve()
    if not repo_root.is_dir():
        print(f"仓根不存在：{repo_root}", file=sys.stderr)
        return 2
    targets = _iter_targets(repo_root, list(args.paths or []), args.base or None)
    if not targets:
        print("没有可扫的文件（git 不可用且未给 --paths）", file=sys.stderr)
        return 2

    findings: list[str] = []
    scanned = 0
    for path in targets:
        if path.suffix.lower() in SKIP_SUFFIXES or not path.is_file():
            continue
        try:
            if path.stat().st_size > MAX_BYTES:
                continue
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        scanned += 1
        try:
            rel = path.relative_to(repo_root)
        except ValueError:
            rel = path
        findings.extend(scan_source(str(rel), text, denylist=denylist, allow_prefixes=args.allow_prefix))

    if findings:
        for line in findings:
            print(line)
        print(f"[public-tree] {scanned} 文件，命中 {len(findings)} 处（exit 1）", file=sys.stderr)
        return 1
    print(f"[public-tree] {scanned} 文件，零命中")
    return 0


if __name__ == "__main__":
    sys.exit(main())
