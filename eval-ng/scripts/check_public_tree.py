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

from collections import Counter
import argparse
import re
import subprocess
import sys
from pathlib import Path


class GitError(RuntimeError):
    """取改动清单时 git 失败（base 不可解析 / 非 git 仓 / 浅克隆缺对象…）。

    必须与「真的没有改动」区分：把失败吞成空列表会让门**绿着放行**一个根本没扫的改动面（Issue#154 冷审 I-1）。
    """

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

#: 阳性对照**逐例**清单（例名, 代码片段）。Issue#159 AC3：逐例命中是硬要求——
#: 只查「10 个类别是否都出现」会放过**整类放松**：往 `META_TAIL_WORDS` 里加 `token`
#: 只让个别片段逃逸，其余片段仍覆盖同一类别，类别级断言照旧通过。
#: 每行末尾的 `public-tree-scan: ignore-line` 是自指豁免：本文件自身也会被扫描。
SELF_TEST_BAD_CASES = (
    ("url-and-path", "instance 8.8.8.8 path /srv/evidence/arm.json win C:\\Users\\dev\\secret.txt"),  # public-tree-scan: ignore-line
    ("bearer-and-literal", "auth Bearer abcdefghijklmnopqrstuvwxyz012345 password=\"Passw0rd!2026\" deny SCNET-INSTANCE-1"),  # public-tree-scan: ignore-line
    ("flag-cli", "cli --api-pass \"Synthetic-Pass!123\""),  # public-tree-scan: ignore-line
    ("flag-argparse", "parser.add_argument(\"--api-pass\", default=\"Synthetic-Pass!123\")"),  # public-tree-scan: ignore-line
    ("bare-assignment", "cfg password=Hunter2Hunter2"),  # public-tree-scan: ignore-line
    ("suffix-secret", "cfg api_secret=Zz9Kk2Lm"),  # public-tree-scan: ignore-line
    ("snake-db-password", "cfg db_password=\"Hunter2Hunter2\""),  # public-tree-scan: ignore-line
    ("upper-secret", "cfg API_SECRET = \"Zz9Kk2Lm\""),  # public-tree-scan: ignore-line
    ("camel-secret", "cfg clientSecret: \"Ab3xY9Qq2\""),  # public-tree-scan: ignore-line
    ("long-token", "cfg access_token = \"ghp_abcdefghijklmnopqrstuvwxyz0123456\""),  # public-tree-scan: ignore-line
    ("annotated-secret", "cfg client_secret: str = \"Ab12Cd34\""),  # public-tree-scan: ignore-line
    ("annotated-key-optional", 'api_key: str | None = "Ab12Cd34"'),  # public-tree-scan: ignore-line
    ("url-inline", "db postgresql://eval_user:Synthetic-Pass!123@127.0.0.1:5432/app"),  # public-tree-scan: ignore-line
    ("basic-auth", "auth Basic U3ludGhldGljLVBhc3MhMTIz"),  # public-tree-scan: ignore-line
    # 只靠**键名**才成立的凭据例（值不带厂商前缀）：元数据尾巴词放松时它必须逃逸 ⇒ 自检判红
    ("token-name-only", 'cfg access_token = "Ab12Cd34"'),  # public-tree-scan: ignore-line
    ("key-name-only", 'cfg api_key = "Ab12Cd34"'),  # public-tree-scan: ignore-line
)
SELF_TEST_BAD = " ".join(snippet for _label, snippet in SELF_TEST_BAD_CASES)
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
    """跑 git 并返回路径项；带 `-z` 时按 NUL 切分（原始字节口径，不经 git 的 shell 转义）。

    历史坑（Issue#154）：不带 `-z` 时 git 会把非 ASCII 路径**转义**成 `"docs/\345..."` 形态，
    下游 `Path(...).is_file()` 判假 ⇒ 整份中文名文件被**静默漏扫**（只会少报、不会报错）。

    **失败必须抛出 `GitError`**：把 `CalledProcessError` 吞成 `[]` 会让「base 写错 / 浅克隆缺对象」
    与「真的没有改动」不可区分，门于是绿着放行（Issue#154 冷审 I-1）。
    """
    cmd = ["git", "-C", str(repo_root), *args]
    try:
        out = subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="surrogateescape",
        ).stdout
    except FileNotFoundError as exc:  # 环境里没有 git
        raise GitError(f"找不到 git 可执行文件：{exc}") from exc
    except OSError as exc:
        raise GitError(f"调用 git 失败：{exc}") from exc
    except subprocess.CalledProcessError as exc:
        detail = [line for line in (exc.stderr or "").splitlines() if line.strip()]
        raise GitError(
            f"git {' '.join(args)} 失败（rc={exc.returncode}）：{detail[-1] if detail else '无 stderr'}"
        ) from exc
    # 只丢**空项**（`-z` 的尾随空串）。**不要**用 `strip()` 当谓词：名字全是空白的文件会被静默丢掉（冷审 M-1）。
    return [item for item in out.split("\0" if "-z" in args else "\n") if item]


def changed_files(repo_root: Path, base: str) -> list[str]:
    """`--base` 以来**改动过的文件**（已提交改动 + 工作区未提交 + 未跟踪），`-z` 机器口径。

    `-z` 是硬要求：本仓公开文档大量使用中文文件名（`docs/开发门禁-*.md`、
    `docs/协同开发规范-*.md` 等），转义形态会让「零命中」变成「没看那几个文件」（Issue#154）。

    `--diff-filter=AMRT`：**R（重命名）/T（类型变更）也要进扫描集**——旧实现用 `AM` 会把
    「git mv 后又改了内容」的文件整体排除，只要同批还有别的改动就静默漏扫（冷审 I-2）。
    `-z --name-only` 对重命名只输出**目标路径**，NUL 切分口径无需额外处理。
    """
    rels = _git_lines(repo_root, ["diff", "-z", "--name-only", "--diff-filter=AMRT", f"{base}...HEAD"])
    rels += _git_lines(repo_root, ["diff", "-z", "--name-only", "--diff-filter=AMRT", "HEAD"])
    rels += _git_lines(repo_root, ["ls-files", "-z", "--others", "--exclude-standard"])
    return list(dict.fromkeys(rels))


def default_paths(repo_root: Path, base: str | None) -> list[Path]:
    """默认扫「本次改动新增/修改的文件」——G4 的判决面是本次交付物，不是整棵历史树。

    `--base` 给定时取三者并集：`<base>...HEAD` 的已提交改动、**工作区未提交改动**（含未跟踪文件）。
    这样门禁在提交前就能跑——否则扫描器会在最需要它的时刻漏掉改动。
    给空串则退化为整树 `git ls-files`（只适合人工巡检，历史文件有既有噪声）。
    """
    if not base:
        return [repo_root / rel for rel in _git_lines(repo_root, ["ls-files", "-z"])]
    return [repo_root / rel for rel in changed_files(repo_root, base)]


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
    """阳性对照：**逐例**必须命中（类别清单见 `required`），干净样本必须零命中。

    Issue#159 AC3：原先只断言「10 个类别是否都出现」。整类放松（例如往
    `META_TAIL_WORDS` 里加 `token`）只让个别片段逃逸，其余片段仍覆盖同一类别，
    类别级断言照旧通过 ⇒ 放松判据**测不出来**。逐例断言后，任何让某一例逃逸的
    规则改动都会让自检判红（本文件的变异证据见 Issue#159 方案 §5）。
    """
    words = list(denylist or [])
    bad_denylist = words + ["SCNET-INSTANCE-1"]
    required = {
        "public_ip", "abs_path", "win_path", "bearer", "url_credential", "basic_auth",
        "credential_literal", "credential_flag", "credential_bare", "denylist",
    }
    problems: list[str] = []
    rules: set[str] = set()
    per_case: list[str] = []
    for label, snippet in SELF_TEST_BAD_CASES:
        hits = scan_source(f"self-test-bad:{label}", snippet, denylist=bad_denylist, allow_prefixes=[])
        if not hits:
            problems.append(f"阳性对照未命中：{label}")
        rules |= {line.split("[", 1)[1].split("]", 1)[0] for line in hits}
        per_case.append(f"{label}={len(hits)}")
    missing = required - rules
    if missing:
        problems.append(f"阳性对照类别未覆盖：{sorted(missing)}")
    clean = scan_source("self-test-clean", SELF_TEST_CLEAN, denylist=words, allow_prefixes=[])
    if clean:
        problems.append(f"干净样本被误报：{clean}")
    if problems:
        for line in problems:
            print(f"[self-test] {line}", file=sys.stderr)
        return 1
    print(
        f"[self-test] 阳性对照 {len(SELF_TEST_BAD_CASES)} 例逐例命中、"
        f"{len(required)} 类全覆盖（{'、'.join(per_case)}），干净样本零命中"
    )
    return 0


def _ensure_utf8_output() -> None:
    """把 stdout/stderr 切到 UTF-8（打印中文路径不得在 C locale / 管道下抛 UnicodeEncodeError）。

    Issue#154 冷审 C2：本仓公开文档大量中文名，命中行会把路径打出来；`LC_ALL=C` 时
    默认的 ASCII 编码器会直接崩，虽然「崩=红」但如果红在无关问题上就没法定位。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:  # 被测试替换过的流（如 capsys 捕获对象）
            continue
        try:
            reconfigure(encoding="utf-8", errors="backslashreplace")
        except (OSError, ValueError):
            continue


def _filesystem_encoding_note() -> str | None:
    """文件系统编码不是 UTF-8 时返回一句说明；正常 UTF-8 环境返回 None。

    实测（Issue#154 冷审 C2 / M-5）：`LC_ALL=C LANG=C PYTHONCOERCECLOCALE=0 PYTHONUTF8=0` 下
    Python 的文件系统编码退化成 ASCII，`Path.is_file()` 对中文名返回 False（`os.stat` 的
    `UnicodeEncodeError` 被 `is_file()` 吞成 False）⇒ 会被误报成「路径解析可疑」。这不是静默漏扫
    （自检② 会红），但文案会把人带偏，所以显式点名原因并给出补救办法。
    """
    encoding = (sys.getfilesystemencoding() or "").lower().replace("-", "")
    if encoding in ("utf8", "utf8mb4"):
        return None
    return (
        f"当前进程的文件系统编码是 {sys.getfilesystemencoding()}（非 UTF-8）：非 ASCII 文件名无法 "
        "stat/读取，会被误判为「不是普通文件」。请用 `PYTHONUTF8=1`（或 UTF-8 locale）重跑。"
    )


def unexplained_candidates(n_targets: int, scanned: int, skipped: dict[str, list[str]]) -> int:
    """未解释的候选余数（Issue#154 自检③ / AC2 的字面项）。

    「候选数」应恒等于「已扫描 + 已解释跳过（后缀/超上限/非 UTF-8）」；余数非零即有人新增了
    跳过路径却没说明。抽成纯函数是为了让它自己也有单测（否则只能靠端到端路径触发）。
    `not_a_file` **不算**已解释：那是路径解析异常，调用方在此之前已 return 2。
    返回值不夹到 0：调用方按 `!= 0` 判红，正负都说明两侧账不平。
    """
    explained = (
        len(skipped.get("skip_suffix", []))
        + len(skipped.get("too_large", []))
        + len(skipped.get("unreadable", []))
    )
    return n_targets - (scanned + explained)


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
    _ensure_utf8_output()  # C locale / 管道下打印中文路径不得崩（Issue#154 冷审 C2）
    # 非 UTF-8 文件系统编码下**只在真有非 ASCII 候选时**才警告（冷审第三轮 M4）：全 ASCII 的树
    # 不存在 stat/读取风险，无条件打印只会让「干净」看起来像有问题。`not_a_file` 分支另给提示。
    fs_note = _filesystem_encoding_note()

    denylist = load_denylist(args.denylist)
    if args.self_test:
        return self_test(denylist)

    repo_root = Path(args.repo_root).resolve()
    if not repo_root.is_dir():
        print(f"仓根不存在：{repo_root}", file=sys.stderr)
        return 2
    try:
        targets = _iter_targets(repo_root, list(args.paths or []), args.base or None)
    except GitError as exc:
        print(f"[public-tree] 取改动清单失败：{exc}", file=sys.stderr)
        print(
            "[public-tree] 宁可红，不可静默漏（Issue#154）：请检查 --base 是否存在、是否在 git 仓内"
            "（浅克隆请先 fetch 该 base）。",
            file=sys.stderr,
        )
        return 2
    if not targets:
        print(
            "[public-tree] 本次范围内没有候选文件（--base 范围内无新增/修改，且无未跟踪文件）；"
            "若确要扫别的文件请显式给 --paths。注意：这与「取清单失败」是两回事。",
            file=sys.stderr,
        )
        return 2

    def _rel(path: Path) -> str:
        try:
            return str(path.relative_to(repo_root))
        except ValueError:
            return str(path)

    if fs_note and any(not _rel(path).isascii() for path in targets):
        print(f"[public-tree] 警告：{fs_note}", file=sys.stderr)

    # Issue#154 自检①：**内部不变量**——`default_paths` 与 `changed_files` 的口径必须一致。
    # 能抓：`default_paths`（Python 侧后处理）未来偏离 `changed_files`（正对照实测 `rc=2`）。
    # 抓不到：**`changed_files` 内部**给某条 git 命令加过滤/去重/截断——`default_paths` 就是
    #   `[repo_root / rel for rel in changed_files(...)]`，两侧同时变小、恒等 ⇒ 静默绿。
    #   含 `--diff-filter` 回退（`AM` ↔ `AMRT`）这类 I-2 回归形状：那类**只能靠用例钉**
    #   （`TestNonAsciiPaths::test_renamed_file_is_still_scanned`），不是靠这条自检。
    #   （冷审第三轮 I1：原文把这份功劳记在自检① 头上，方向恰好相反，已更正。）
    if not args.paths and args.base:
        expected = len(changed_files(repo_root, args.base))
        if expected != len(targets):
            print(
                f"[public-tree] 内部不变量被破坏：改动清单条目数 {expected} 与候选集合数 {len(targets)} 不一致"
                "（default_paths 与 changed_files 口径漂移）",
                file=sys.stderr,
            )
            return 2

    findings: list[str] = []
    scanned = 0
    # 跳过必须**逐类说明**：intentional 两类放行但打印，其余（非文件/不可读）视为门被绕过的信号。
    skipped: dict[str, list[str]] = {"skip_suffix": [], "not_a_file": [], "too_large": [], "unreadable": []}
    for path in targets:
        if path.suffix.lower() in SKIP_SUFFIXES:
            skipped["skip_suffix"].append(_rel(path))
            continue
        if not path.is_file():
            skipped["not_a_file"].append(_rel(path))
            continue
        try:
            size = path.stat().st_size
        except OSError:
            skipped["unreadable"].append(_rel(path))
            continue
        if size > MAX_BYTES:
            skipped["too_large"].append(_rel(path))
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            skipped["unreadable"].append(_rel(path))
            continue
        scanned += 1
        findings.extend(scan_source(_rel(path), text, denylist=denylist, allow_prefixes=args.allow_prefix))

    # Issue#154 自检②：改动清单里的文件**凭空消失**（路径解析/转义回归的典型症状）⇒ 报错退出。
    # 其余跳过类别（后缀/超上限/非 UTF-8）都是「解释了为什么没扫」，打印出来但不判红。
    if skipped["not_a_file"]:
        print("[public-tree] 以下改动文件不是普通文件（路径解析可疑 — 宁可红）：", file=sys.stderr)
        for item in skipped["not_a_file"]:
            print(f"  - {item}", file=sys.stderr)
        if fs_note:
            print(f"[public-tree] 提示：{fs_note}", file=sys.stderr)
        print(
            f"[public-tree] 扫描 {scanned} 文件；路径解析异常 {len(skipped['not_a_file'])} 个（exit 2）",
            file=sys.stderr,
        )
        return 2
    for items, reason in (
        (skipped["skip_suffix"], "后缀不在扫描范围"),
        (skipped["too_large"], f"超过 {MAX_BYTES // (1024 * 1024)}MiB 上限"),
        (skipped["unreadable"], "非 UTF-8 文本（读取失败）"),
    ):
        if items:
            shown = "、".join(items[:5]) + ("…" if len(items) > 5 else "")
            print(f"[public-tree] 有意跳过 {len(items)} 个（{reason}）：{shown}", file=sys.stderr)

    # Issue#154 自检③（AC2 的字面项）：候选数 vs「已扫描 + 已解释跳过」的**未解释余数**必须为零。
    # 今天余数恒等于 not_a_file（上一分支已 return 2）；这条的价值是**结构性对账**：将来任何人新增
    # 一个跳过类别而忘了说明/打印，余数会在这里变红，而不是静默配平（冷审 I-3）。
    explained_skips = len(skipped["skip_suffix"]) + len(skipped["too_large"]) + len(skipped["unreadable"])
    unexplained = unexplained_candidates(len(targets), scanned, skipped)
    if unexplained != 0:
        # 正负分开写（冷审第三轮 M7）：负值意味着两侧重复计数，「未解释的候选文件 -2 个」会误导人。
        if unexplained > 0:
            head = f"未解释的候选文件 {unexplained} 个"
        else:
            head = f"已扫描 + 已解释跳过 比候选数多 {-unexplained} 个（两侧有重复计数）"
        print(
            f"[public-tree] {head}（候选 {len(targets)}、已扫描 {scanned}、"
            f"已解释跳过 {explained_skips}）— 宁可红，不可静默漏",
            file=sys.stderr,
        )
        return 2

    counts = Counter(line.split("[", 1)[1].split("]", 1)[0] for line in findings)
    dist = "、".join(f"{rule}={n}" for rule, n in sorted(counts.items())) or "无"
    if findings:
        for line in findings:
            print(line)
        print(
            f"[public-tree] {scanned} 文件，命中 {len(findings)} 处（按规则：{dist}）（exit 1）",
            file=sys.stderr,
        )
        return 1
    tail = f"，跳过 {sum(len(v) for v in skipped.values())} 个（有意）" if any(skipped.values()) else ""
    print(f"[public-tree] {scanned} 文件，零命中（按规则：{dist}）{tail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
