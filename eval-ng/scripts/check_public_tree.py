#!/usr/bin/env python3
"""check_public_tree.py —— 公开树红线扫描器（验收方案 v2 G4）。

与 `check_report_redaction.py` 同族，但**扫的是公开仓本身**（含新增文档与 CHANGELOG），
用途是给「敏感物不出私有体系」（AGENTS.md 铁律①）一个**有失败入口**的机检：

1. **公网 IP**：回环 / RFC1918 私网 / RFC5737 文档段放行（测试夹具与示例常用），
   其余一律命中——真实实例地址另经 `--denylist` 点名；
2. **绝对路径**：POSIX 根用户/家目录/挂载点等前缀、Windows 盘符路径（`--allow-prefix` 放行指定前缀）；
3. **凭据形态**（`--self-test` 逐类断言的 10 类）：键名含凭据词的普通赋值 / 带类型注解赋值（含
   PEP 604 联合、泛型、前向引用）/ 引号键（JSON·YAML·dict，键名允许 `-`）/ `--flag` 后的引号
   字面量或空格裸值 / 无引号字母数字混合值 / `Bearer <≥24 字符>`（**大小写不敏感**，Issue#142）/ 厂商前缀密钥（`sk-`·`ghp_`·
   `AKIA…`）/ URL 内联凭据 / HTTP Basic。带键名的分支要求值 ≥8 字符（含汉字 ≥6）且**像真口令**（含数字或非
   字母数字符号，且非占位词/模板/类型名/ID 形态/JSON 片段）；含汉字的值另要求**混排证据**
   （至少一个 ASCII 字母/数字或口令符号），纯中文文案仍不判定（Issue#160 现象①）；
   `os.environ[…]`、
   `self._password` 这类引用与代码取值不算。**覆盖与不覆盖的完整边界**见文件内 `_COVERAGE_NOTE`；
4. **`--denylist` 词表**（一行一词，`re:` 前缀走正则）——真实案卷名、实例域名、口令进这里。

行内放过：`public-tree-scan: ignore-line(<规则名>, …)` **只放过点名的规则**，同行其它规则照常报出；
裸标记（无规则列表）不放过任何规则（扫描器自己的阳性对照夹具、以及确非凭据
的既有行需要它；每处都有就地说明）。

计数口径：Issue#160 之前含汉字的值**整类不参与**凭据判定，因此该 Issue 前后的命中数
**不可直接比较**（同一版本内的 A/B 才可比；历史数字见各自 Issue 记忆，不要跨版本对账）。

自带阳性对照：`--self-test` 用含上述 10 类痕迹的样本断言「必须全部命中」，再用干净样本断言
「必须零命中」——**没有阳性对照的扫描器等于恒真判据**。

用法::

    python eval-ng/scripts/check_public_tree.py                      # 默认扫 --base 以来改动过的文件
    python eval-ng/scripts/check_public_tree.py --paths docs/ README.md
    python eval-ng/scripts/check_public_tree.py --denylist <私有词表> --allow-prefix <公开工具链前缀>
    python eval-ng/scripts/check_public_tree.py --self-test

exit：0=干净；1=命中（或阳性对照失败）；2=用法错误。

`--base` 默认判「改动文件的**整份内容**」；加 `--added-lines` 只判「本次改动**新增的行**」
（Issue#187）：基线里既有的命中不再连坐，新增行里的红线照旧判红。无法定位新增行的文件
（纯模式变更、重命名、二进制）退化为整文件判定并在 stderr 注明 —— 宁可多扫，不可漏扫。
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
    ("bearer", re.compile(r"(?i)\bBearer\s+[A-Za-z0-9\-._~+/]{24,}")),
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
# 命令行 flag 后的**裸值**（无引号）：`--api-pass Ab12Cd34`（⑨冷评审 R9-3）。  #
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
#: 中文标点：值里出现它说明这段是**正文/说明**（`"委托人陈文清，身份证号…。"` / `"2048，密集页预算"`），
#: 不是口令。ASCII 分隔符由 `_DELIMITER_RE` 管，这里补中文一侧（Issue#160 实测的整树假阳性来源）。
_CJK_PUNCT_RE = re.compile(r"[，。、；：？！（）〈〉《》「」『』【】〔〕…—～·]")
#: 中文日期形态（`2023年6月15日`）：数字 + 年月日是**日期**不是口令（`key="2023年6月15日"` 实测假阳性）。
_CJK_DATE_RE = re.compile(r"^\d{4}\s*年(?:\s*\d{1,2}\s*月)?(?:\s*\d{1,2}\s*日)?$")

_MIN_LITERAL_LEN = 8
#: 中文取值口令的长度底线（Issue#160 现象①）：一个汉字的信息量远高于一个 ASCII 字符，
#: 若仍按 8 计会漏掉 `password="测试口令123"`（7 字符）这类真实形态；6 是「纯中文长文案」  # public-tree-scan: ignore-line(credential_literal)
#: 与「中文短口令」的分界，且与下面的「混排证据」要求共同生效（两者缺一不可）。
_MIN_LITERAL_LEN_CJK = 6


def _looks_like_type_name(value: str) -> bool:
    """`Ed25519PrivateKey` 这类 CamelCase 类型名不是口令（短口令形如 `Zz9Kk2Lm` 不在此列）。"""
    return bool(_CAMEL_TYPE_RE.match(value)) and len(value) >= 15


def _cjk_mixed_evidence(value: str) -> bool:
    """中文取值口令的**混排证据**（Issue#160 现象①）。

    键名已被 `is_credential_name()` 判为凭据名（上游门），这里只回答「值本身是否带非中文成分」。
    纯中文文案（`"中文标题"` / `"本院认为合同有效"`）不得当口令——否则公开文档里的中文正文会整类误报。
    """
    return bool(re.search(r"[0-9A-Za-z]", value)) or bool(_SECRET_SYMBOL_RE.search(value))


def _is_secret_shaped(value: str) -> bool:
    if not value or _DELIMITER_RE.search(value):
        return False
    if _CJK_RE.search(value):
        # Issue#160 现象①：旧实现把「含汉字」整类当文案排除（Issue 正文示例形态静默漏网）。
        # 现改为「键名门 + 混排证据」双条件，并显式排除中文正文/日期两类假阳性。
        if _CJK_PUNCT_RE.search(value) or _CJK_DATE_RE.match(value):
            return False
        return _cjk_mixed_evidence(value)
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
        floor = _MIN_LITERAL_LEN_CJK if _CJK_RE.search(body) else _MIN_LITERAL_LEN
        if len(body) < floor or body.startswith("-----"):
            return False
        if TEMPLATE_VALUE_RE.search(body) or _is_placeholder_value(body):
            return False
        return _is_secret_shaped(body)
    if any(ch in raw for ch in "(.["):
        return False  # 调用/属性/下标：值是代码引用，不是字面量
    body = raw.rstrip(",")
    # 源码形态的字面量常带转义引号（`\"2023年6月15日\"`）⇒ **排除判定**用剥掉转义/引号噪声的视图，
    # 否则中文日期/标点排除会被前导反斜杠绕过（自检夹具正落在这个形态上）；
    # 形状判定仍用原值（转义引号会命中 _DELIMITER_RE，语义不放宽）。
    probe = body.strip("\\\"'")
    if TEMPLATE_VALUE_RE.search(probe) or _is_placeholder_value(probe):
        return False
    if _CJK_RE.search(probe):
        # 中文裸值（`password=测试口令123`）：长度按 CJK 底线，形状按混排证据。
        if _CJK_PUNCT_RE.search(probe) or _CJK_DATE_RE.match(probe):
            return False
        return len(probe) >= _MIN_LITERAL_LEN_CJK and _cjk_mixed_evidence(probe)
    if len(body) < _MIN_LITERAL_LEN:
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
#: Issue#160 AC3：放过标记必须**点名规则**——``。
#: 旧实现 `IGNORE_MARKER in line` 是整行跳过＝一票放行（同行其它规则被一起放过）；
#: 裸标记（无括号）现在不放过任何规则：写错方向的失败是「扫描变红」，而不是「静默放过」。
_IGNORE_LINE_RE = re.compile(re.escape(IGNORE_MARKER) + r"\s*\((?P<rules>[A-Za-z0-9_,\s]*)\)")
SKIP_SUFFIXES = frozenset({
    ".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".zip", ".gz", ".whl", ".bin",
    ".so", ".dylib", ".dll", ".pt", ".pth", ".onnx", ".safetensors", ".docx", ".xlsx", ".pptx",
    ".woff", ".woff2", ".ttf", ".otf", ".mp4", ".mov", ".db", ".sqlite", ".lock",
})
MAX_BYTES = 2 * 1024 * 1024

#: 门①.5 r2 I-3：旧契约只钉「`MAX_BYTES` 不得缩到 64 KiB 以下」，缩到恰在 64 KiB 仍全绿，
#: 而真树实测会丢 6 处真实命中（1006→997 文件 / 332→326 处）。改为**超限夹具**驱动：
#: `_extension_coverage_check()` 会造一个超过此下限的文件并要求它被扫到。
#: 硬化（作者自查）：夹具体积用**与下限无关的固定字面量** `_OVERSIZED_FIXTURE_BYTES`，
#: 且下限本身另有字面量地板 `MIN_MAX_BYTES_FLOOR`；否则「把下限调小」会让夹具跟着变小，
#: 检查与夹具同源 ⇒ 自指空转。
MIN_MAX_BYTES = 96 * 1024
MIN_MAX_BYTES_FLOOR = 96 * 1024
_OVERSIZED_FIXTURE_BYTES = 128 * 1024

#: 门①.5 r2 I-2：必须被扫描的**文本/源码后缀**（反向白名单）。
#: 旧契约只断言「21 个已知源码后缀不得进 `SKIP_SUFFIXES`」——白名单式 ⇒ 加一个名单外的
#: 文本后缀（`.ps1` / `.example` / `.jsonl`）三层全绿，真树却丢 4–5 处命中。
#: 这里改成**真文件驱动**：每个后缀造一个含凭据的文件走 `main()` 全链，少扫一个就判红。
#: 另有数量地板 `MIN_REQUIRED_TEXT_SUFFIXES`：删条目同样会让检查与夹具一起变弱。
REQUIRED_TEXT_SUFFIXES = (
    ".py", ".pyi", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".json", ".jsonl", ".yml", ".yaml",
    ".md", ".sh", ".toml", ".cfg", ".ini", ".env", ".txt", ".sql", ".go", ".java", ".rb",
    ".ps1", ".html", ".css", ".example",
)
MIN_REQUIRED_TEXT_SUFFIXES = 26

#: 门①.5 r2 I-5：全部**已登记**的规则 id。它让 AC2 的汇总断言真正可失败——
#: 规则改名/新规则涌现而未登记时判红，而不是恒真的「逐规则计数之和 == 命中总数」。
KNOWN_RULE_IDS = frozenset({
    "public_ip", "abs_path", "win_path", "bearer", "api_key", "url_credential", "basic_auth",
    "credential_literal", "credential_flag", "credential_bare", "denylist",
})
_ADDED_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
_FINDING_LINE_RE = re.compile(r"^.+?:\d+: \[[a-z_]+\] ")

#: 门①.5 r2 I-7：负面控制也必须有下限——两份干净样本清空后自检照样 rc=0 并照打
#: 「干净样本零命中」，缺口③ 的唯一正面证据可被无声删除。长度 + CJK 双向比对封住它。
MIN_CLEAN_CHARS = 200
MIN_CLEAN_CN_CHARS = 100

#: 阳性对照**逐例**清单（例名, 代码片段）。Issue#159 AC3：逐例命中是硬要求——
#: 只查「10 个类别是否都出现」会放过**整类放松**：往 `META_TAIL_WORDS` 里加 `token`
#: 只让个别片段逃逸，其余片段仍覆盖同一类别，类别级断言照旧通过。
#: 每行末尾的 `public-tree-scan: ignore-line(<规则名>)` 是自指豁免：本文件自身也会被扫描。
#: Issue#160 AC3：豁免必须逐个点名规则，同行其它规则照常报出（旧实现整行跳过＝一票放行）。
SELF_TEST_BAD_CASES = (
    ("url-and-path", "instance 8.8.8.8 path /srv/evidence/arm.json win C:\\Users\\dev\\secret.txt"),  # public-tree-scan: ignore-line(abs_path, public_ip, win_path)
    ("bearer-and-literal", "auth Bearer abcdefghijklmnopqrstuvwxyz012345 password=\"Passw0rd!2026\" deny SCNET-INSTANCE-1"),  # public-tree-scan: ignore-line(bearer)
    # Issue#142：Bearer 大小写不敏感——小写与混写必须同样落网（值均为合成串，非真实令牌）
    ("bearer-lowercase", "auth bearer abcdefghijklmnopqrstuvwxyz012345"),  # public-tree-scan: ignore-line(bearer)
    ("bearer-mixedcase", "auth BeArEr ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"),  # public-tree-scan: ignore-line(bearer)
    ("flag-cli", "cli --api-pass \"Synthetic-Pass!123\""),  # public-tree-scan: ignore-line(credential_flag)
    ("flag-argparse", "parser.add_argument(\"--api-pass\", default=\"Synthetic-Pass!123\")"),  # public-tree-scan: ignore-line(credential_flag)
    ("bare-assignment", "cfg password=Hunter2Hunter2"),  #
    ("suffix-secret", "cfg api_secret=Zz9Kk2Lm"),  #
    ("snake-db-password", "cfg db_password=\"Hunter2Hunter2\""),  #
    ("upper-secret", "cfg API_SECRET = \"Zz9Kk2Lm\""),  #
    ("camel-secret", "cfg clientSecret: \"Ab3xY9Qq2\""),  #
    ("long-token", "cfg access_token = \"ghp_abcdefghijklmnopqrstuvwxyz0123456\""),  # public-tree-scan: ignore-line(api_key)
    ("annotated-secret", "cfg client_secret: str = \"Ab12Cd34\""),  #
    ("annotated-key-optional", 'api_key: str | None = "Ab12Cd34"'),  # public-tree-scan: ignore-line(credential_literal)
    ("url-inline", "db postgresql://eval_user:Synthetic-Pass!123@127.0.0.1:5432/app"),  # public-tree-scan: ignore-line(url_credential)
    ("basic-auth", "auth Basic U3ludGhldGljLVBhc3MhMTIz"),  # public-tree-scan: ignore-line(basic_auth)
    # 与键名无关的厂商前缀密钥（`api_key` 规则）：门①.5 r1 指出 required 里缺它 ⇒ 该规则曾被静默改假
    ("vendor-prefixed-key", 'cfg value = "sk-abcdefghijklmnopqrstuvwx"'),  # public-tree-scan: ignore-line(api_key)
    # 只靠**键名**才成立的凭据例（值不带厂商前缀）：元数据尾巴词放松时它必须逃逸 ⇒ 自检判红
    ("token-name-only", 'cfg access_token = "Ab12Cd34"'),  # public-tree-scan: ignore-line(credential_literal)
    # 只靠 `is_credential_name` 的 compact 兜底才成立（词元切分后无凭据词）：冷审 N8 曾删掉该兜底而全绿
    ("compact-fallback-name", 'cfg dbpassword = "Ab12Cd34"'),  # public-tree-scan: ignore-line(credential_literal)
    ("key-name-only", 'cfg api_key = "Ab12Cd34"'),  # public-tree-scan: ignore-line(credential_literal)
    # 门①.5 r2 I-1：只靠**符号**（不含数字）成形的密钥。此前 18 例正样本值全含数字，
    # 于是把 `_SECRET_SYMBOL_RE` 里的 `!` 删掉仍然全绿，而仅含符号的口令会漏（见下方用例）。
    ("symbol-shaped-secret", 'cfg password = "Abcdefg!"'),  # public-tree-scan: ignore-line(credential_literal)
    # Issue#160 现象①：含汉字的值曾被 `_is_secret_shaped` 整类排除 ⇒ 中文口令静默漏网。
    # 两例覆盖主 Issue 点名的两个形态：带数字的纯中文值、中英数混排值。
    ("cjk-password-with-digits", 'cfg password="测试口令123"'),  # public-tree-scan: ignore-line(credential_literal)
    ("cjk-mixed-secret", 'cfg secret = "超机密密码9o0i"'),  # public-tree-scan: ignore-line(credential_literal)
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
    # Issue#142：<24 字符的 bearer 值属设计边界（阈值有意取 24），不得命中
    "auth bearer ShortToken12345 "
)

# Issue#159 缺口③：干净样本补**中文内容 + 非 ASCII 路径**（自检此前只有 ASCII 路径/内容）。
# 注意：缺口③的另一半（「应命中却曾被放过」的中文口令语料）属 Issue#160，不在此处假装覆盖。
SELF_TEST_CLEAN_CN = (
    "本院于2022年1月25日立案受理，承办人张三，联系电话 13800000000，"
    "账号 202020202020202020，地址 广西南宁市青秀区示例路 1 号。"
    "配置示例：api_key=os.environ[\"X\"] password=<你的密码> "
    # Issue#160 AC2 反例：凭据**键名**下的纯中文文案不得当口令（缺混排证据）；
    # 长到 CJK 长度底线（6 字）也不能只靠长度成立。
    "secret = \"中文标题\" password = \"甲乙丙丁戊己\" "  # public-tree-scan: ignore-line(credential_bare)
    # 门①.5 r1 M-3 / r2 R2-1：中文标点正文 / 中文日期形态也必须零命中——
    # **键名必须是凭据名**，否则会被键名门提前挡掉、压根测不到中文标点/日期排除。
    "secret = \"本次共3人，已逐一核对。\" pwd = \"2023年6月15日\" "
)


def _is_public_ip(ip: str) -> bool:
    if any(ip.startswith(prefix) for prefix in LOOPBACK_PREFIXES):
        return False
    return not PRIVATE_IP_RE.match(ip)


_COVERAGE_NOTE = """凭据面覆盖边界（⑦⑧⑨冷评审反复追问的口径，明写以免「静默盲区」）。
覆盖：普通赋值 `n = "v"`；带类型注解赋值 `n: Ann = "v"`（含 PEP 604 `|`、泛型、顶层前向引用）；
引号键（JSON/YAML/dict，键名可含 `-`）；`--flag` 后的引号字面量**与空格裸值**；无引号字母数字
混合值；Bearer（**大小写不敏感**：`(?i)\bBearer\s+[A-Za-z0-9\-._~+/]{24,}`；报告侧 `eval-ng/engine/speedkit/report.py` 的同族规则也带 `(?i)`，阈值 12 —— 差异为**有意**：公开树门取防误报、报告脱敏取防漏报，两侧大小写口径一致）/ 厂商前缀密钥 / URL 内联凭据 / HTTP Basic。
不覆盖（设计边界，非静默盲区）：① 编码/拼接/运行时构造的口令（base64 常量、`"a"+"b"`、f-string
插值——后者按模板豁免）。**已知安全侧多报**：含数字且无 `=` 填充的 base64 常量会被判命中——**多报不是漏报**，处置方式是就地说明或 `ignore-line`；② 跨行材料（PEM 私钥正文逐行 `-`，形状判定视为分隔符）；③ 仅在密钥库/
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
        # 空格分隔的裸值形态（`--api-pass Ab12Cd34`，⑨冷评审 R9-3）：仅看同一行，且与其它分支同形状判据。  #
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


def _split_lines(text: str) -> list[str]:
    """**唯一**断行真源：只按 `\n` 断（与 `git diff` 的行号空间一致）。

    Issue#187 冷审 Critical-1：`str.splitlines()` 还会按 `\v \f \x1c \x1d \x1e \x85 U+2028 U+2029`
    断行，而 `git diff` 只按 `\n` 计数 ⇒ 若两者混用，`--added-lines` 掩码后的行号会与 git 的
    新增行号错位，**新增的红线字面量可被静默漏判**（实测：文件含 `\f` 时新口径 rc=0、旧口径 rc=1）。
    因此掩码与报行号必须共用本函数。
    """
    return text.split("\n")


def _read_text_file(path: Path) -> str:
    """**唯一**读文本真源：`newline=""` 关闭 universal-newline 转换。

    默认 `read_text()` 会把孤立 `\r` 折成 `\n`，与 git 的行号空间不一致（同一个 Critical-1）。
    """
    with path.open("r", encoding="utf-8", newline="") as handle:
        return handle.read()


def scan_source(name: str, text: str, *, denylist: list[str], allow_prefixes: list[str]) -> list[str]:
    """逐行扫描并带行号；放过标记**按规则名**生效（Issue#160 AC3）。"""
    lines: list[str] = []
    for lineno, line in enumerate(_split_lines(text), 1):
        marker = _IGNORE_LINE_RE.search(line)
        allowed = (
            {item.strip() for item in marker.group("rules").split(",") if item.strip()} if marker else set()
        )
        for rule, value in scan(line, denylist=denylist, allow_prefixes=allow_prefixes):
            if rule in allowed:
                continue
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


def _git_text(repo_root: Path, args: list[str]) -> str:
    """跑 git 并返回**原始 stdout**（diff 文本口径；`_git_lines` 会丢空行，不能用于解析 diff）。"""
    cmd = ["git", "-C", str(repo_root), *args]
    try:
        return subprocess.run(
            cmd, check=True, capture_output=True, text=True,
            encoding="utf-8", errors="surrogateescape",
        ).stdout
    except FileNotFoundError as exc:
        raise GitError(f"找不到 git 可执行文件：{exc}") from exc
    except OSError as exc:
        raise GitError(f"调用 git 失败：{exc}") from exc
    except subprocess.CalledProcessError as exc:
        detail = [line for line in (exc.stderr or "").splitlines() if line.strip()]
        raise GitError(
            f"git {' '.join(args)} 失败（rc={exc.returncode}）：{detail[-1] if detail else '无 stderr'}"
        ) from exc


def added_lines(repo_root: Path, base: str, rel: str) -> set[int] | None:
    """`<base>` 以来该文件在**工作区坐标系**下的新增行号（含未提交改动）。

    只跑一次 `git diff <base> -- rel`：该命令把 `<base>` 与**工作区文本**对比，与调用方实际读取并扫描的
    文本**同一坐标系**。冷审 I-1：早先实现把 `base...HEAD`（HEAD 坐标系）与 `HEAD`（工作区坐标系）的
    行号**取并集**再套到工作区文本上 —— 工作区相对 HEAD 少了若干行时（未提交删行）行号会漂移，
    新增的凭据可被静默漏判（冷审已给最小反例）。

    返回 `None` = **无法定位新增行**（改动清单里有该文件但 diff 无任何 hunk：纯模式变更、重命名、
    二进制等，或只有删除没有新增）⇒ 调用方应按**整文件**判定并在输出里注明
    （Issue#187 边界要求：宁可多扫，不可漏扫）。
    """
    added: set[int] = set()
    hunks = 0
    out = _git_text(repo_root, ["diff", "-U0", "--no-color", "--no-ext-diff", base, "--", rel])
    for line in out.splitlines():
        if not line.startswith("@@"):
            continue
        hunks += 1
        match = _ADDED_HUNK_RE.match(line)
        if match is None:
            continue
        start = int(match.group(1))
        count = int(match.group(2)) if match.group(2) is not None else 1
        if count > 0:
            added.update(range(start, start + count))
    if hunks == 0:
        return None
    if not added:
        # 有 hunk 却没有任何新侧行号（只删不增 / 解析退化）⇒ 不做局部判定，退化为整文件（宁可多扫）。
        return None
    return added


def mask_non_added_lines(text: str, added: set[int]) -> str:
    """把**非新增行**替换成空行（保留行数）⇒ `scan_source` 报出的行号仍是文件真实行号。"""
    return "\n".join(line if index in added else "" for index, line in enumerate(_split_lines(text), 1))


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


def _scan_scope_contract() -> list[str]:
    """门①.5 r1 必修①：**跳过面与范围**是自指盲区。

    自检此前只直调 `scan_source`，结构上永远看不到 `SKIP_SUFFIXES` / `IGNORE_MARKER` /
    `MAX_BYTES` 被放宽（冷审实测：往 `SKIP_SUFFIXES` 加 `.py` 后，CI 门、自检、仓内回归
    三方全绿，而含凭据的 `.py` 照样进仓）。这里把三者钉成机械契约：
    源码后缀不得进跳过集、忽略标记不得改宽、大小上限不得缩小。
    """
    problems: list[str] = []
    source_suffixes = {
        ".py", ".pyi", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".json", ".yml", ".yaml",
        ".md", ".sh", ".toml", ".cfg", ".ini", ".env", ".txt", ".sql", ".go", ".java", ".rb",
    }
    leaked = sorted(source_suffixes & SKIP_SUFFIXES)
    if leaked:
        problems.append(f"SKIP_SUFFIXES 含源码后缀（扫描面被放宽）：{leaked}")
    if IGNORE_MARKER != "public-tree-scan: ignore-line":
        problems.append(f"忽略标记被改宽：{IGNORE_MARKER!r}")
    if MAX_BYTES < MIN_MAX_BYTES:
        problems.append(f"单文件大小上限被缩小到 {MAX_BYTES}（下限 {MIN_MAX_BYTES}）")
    # 自查硬化：下限与后缀清单本身也要有地板，否则「把下限/清单调小」会让检查与其夹具同源变弱。
    if MIN_MAX_BYTES < MIN_MAX_BYTES_FLOOR:
        problems.append(f"大小下限被调小：{MIN_MAX_BYTES} < 地板 {MIN_MAX_BYTES_FLOOR}")
    if len(REQUIRED_TEXT_SUFFIXES) < MIN_REQUIRED_TEXT_SUFFIXES:
        problems.append(
            f"必扫后缀清单被削短：{len(REQUIRED_TEXT_SUFFIXES)} < 地板 {MIN_REQUIRED_TEXT_SUFFIXES}"
        )
    if _OVERSIZED_FIXTURE_BYTES <= MIN_MAX_BYTES:
        problems.append(
            f"超限夹具({_OVERSIZED_FIXTURE_BYTES}) 不大于大小下限({MIN_MAX_BYTES})：检查会失去判别力"
        )
    return problems


def _end_to_end_scope_check() -> list[str]:
    """门①.5 r1 必修①：走 `main()` 的端到端自检（临时 git 仓 + **已提交**的 .py 凭据）。

    与 CI 的调用路径一致（`--base` 取改动清单 ⇒ 候选 ⇒ 跳过面 ⇒ 扫描 ⇒ 退出码）。
    作者原先的本地阳性对照用的是**未跟踪**文件，CI 上永远不出现那种路径。
    """
    import contextlib
    import io
    import os
    import subprocess
    import tempfile

    problems: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "probe", "GIT_AUTHOR_EMAIL": "probe@example.com",
            "GIT_COMMITTER_NAME": "probe", "GIT_COMMITTER_EMAIL": "probe@example.com",
        }

        def git(*args: str) -> None:
            subprocess.run(["git", "-C", tmp, *args], capture_output=True, text=True, env=env, check=True)

        git("init", "-q")
        (Path(tmp) / "keep.txt").write_text("clean\n", encoding="utf-8")
        git("add", "-A")
        git("commit", "-q", "-m", "base")
        # 夹具文本按片段拼接：本文件自身也在扫描范围内（本 PR 改了它），
        # 写成连续字面量 `api_pass = "<值>"` 会被自己的规则判红（实测 CI G4 FAILURE）。
        probe_text = "".join(
            ["api", "_pass", ' = "', "Synth", "etic-", "Pass!", "1", "2", "3", '"', "\n"]
        )
        (Path(tmp) / "leak_probe.py").write_text(probe_text, encoding="utf-8")
        git("add", "-A")
        git("commit", "-q", "-m", "probe")
        (Path(tmp) / "keep.txt").write_text("clean but changed\n", encoding="utf-8")
        git("add", "-A")
        git("commit", "-q", "-m", "benign")
        sink = io.StringIO()
        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            leaked = main(["--repo-root", tmp, "--base", "HEAD~2"])
            clean = main(["--repo-root", tmp, "--base", "HEAD~1"])
        if leaked != 1:
            problems.append(f"端到端自检：临时仓提交含凭据的 .py 后应 rc=1，实得 {leaked}")
        if clean != 0:
            problems.append(f"端到端自检：临时仓只改良性文件时应 rc=0，实得 {clean}（注意：无候选文件是 2）")
    return problems


def _extension_coverage_check() -> list[str]:
    """门①.5 r2 I-2 / I-3：后缀集与大小上限是**白名单式**契约，必须用真文件走 `main()` 全链。

    旧契约只断言「已知源码后缀不在 `SKIP_SUFFIXES` 里」「`MAX_BYTES` 不小于 64 KiB」，
    两条都只在**名单内**成立：往跳过集加一个名单外的文本后缀（`.ps1`/`.example`/`.jsonl`）
    三层全绿而真树丢 4–5 处命中；把上限缩到恰在 64 KiB 也全绿而真树丢 6 处（实测）。

    这里反向钉住「**哪些后缀必须被扫到**」：每个后缀造一个含凭据的文件 + 一个超过
    `MIN_MAX_BYTES` 的大文件，提交后走 `--base HEAD~1` 全链，要求每个文件名都出现在
    命中输出里。少扫一个、跳过集放宽一个、上限缩小到夹具以下，都会判红。
    """
    import contextlib
    import io
    import os
    import subprocess
    import tempfile

    payload = 'cfg password = "Hunter2Hunter2"\n'
    problems: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "probe", "GIT_AUTHOR_EMAIL": "probe@example.com",
            "GIT_COMMITTER_NAME": "probe", "GIT_COMMITTER_EMAIL": "probe@example.com",
        }

        def git(*args: str) -> None:
            subprocess.run(["git", "-C", tmp, *args], capture_output=True, text=True, env=env, check=True)

        git("init", "-q")
        (Path(tmp) / "keep.txt").write_text("clean\n", encoding="utf-8")
        git("add", "-A")
        git("commit", "-q", "-m", "base")
        expected: list[str] = []
        for suffix in REQUIRED_TEXT_SUFFIXES:
            name = f"probe{suffix}"
            (Path(tmp) / name).write_text(payload, encoding="utf-8")
            expected.append(name)
        # 超限夹具：体积来自**与下限无关的固定字面量**（大于 `MIN_MAX_BYTES`），凭据落在文件末尾
        # （截断/跳过都会漏掉它）。夹具不随下限缩放 ⇒「把下限调小」不会同时把检查变弱。
        oversized = "oversized_probe.txt"
        padding = "# padding line\n" * (_OVERSIZED_FIXTURE_BYTES // 14 + 64)
        (Path(tmp) / oversized).write_text(padding + payload, encoding="utf-8")
        expected.append(oversized)
        git("add", "-A")
        git("commit", "-q", "-m", "fixtures")

        sink = io.StringIO()
        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            rc = main(["--repo-root", tmp, "--base", "HEAD~1"])
        out = sink.getvalue()
        if rc != 1:
            problems.append(f"后缀覆盖自检：{len(expected)} 个含凭据夹具提交后应 rc=1，实得 {rc}")
        missing = [name for name in expected if f"{name}:" not in out]
        if missing:
            problems.append(
                f"后缀/体积覆盖：{len(missing)} 个夹具未被扫描（跳过集放宽或上限缩小）：{missing}"
            )
    return problems


def _ignore_line_scope_check() -> list[str]:
    """Issue#160 AC3：放过标记必须**按规则名**生效，且裸标记不得整行豁免。

    反向对照两条：(a) 只点名 `credential_literal` 时同行的 `public_ip` 必须照常报出；
    (b) 裸标记不放过任何规则。两条都在 `scan_source` 的真路径上跑（不是断言常量）。
    """
    problems: list[str] = []
    # 标记字面量按符号拼出：本函数所在行也会被扫描器扫到，写成字面量会自指命中。
    scoped = 'cfg password="Ab12Cd34" host 8.8.8.8  # ' + f"{IGNORE_MARKER}(credential_literal)"  # public-tree-scan: ignore-line(public_ip)
    rules = {
        line.split("[", 1)[1].split("]", 1)[0]
        for line in scan_source("self-test-ignore-scope", scoped, denylist=[], allow_prefixes=[])
    }
    if "credential_literal" in rules:
        problems.append("规则级放过标记未生效：被点名的 credential_literal 仍被报出")
    if "public_ip" not in rules:
        problems.append(f"规则级放过标记过宽：同行未被点名的规则被一起放过（实得 {sorted(rules)}）")
    bare = 'cfg password="Ab12Cd34" host 8.8.8.8  # ' + IGNORE_MARKER  # public-tree-scan: ignore-line(public_ip)
    bare_rules = {
        line.split("[", 1)[1].split("]", 1)[0]
        for line in scan_source("self-test-ignore-bare", bare, denylist=[], allow_prefixes=[])
    }
    if "credential_literal" not in bare_rules or "public_ip" not in bare_rules:
        problems.append(f"裸放过标记不应放过任何规则（实得 {sorted(bare_rules)}）")
    # 门①.5 r1 M-2：空括号 `ignore-line()` 的规则集为空 ⇒ **什么都不放过**（不是「放过整行」）。
    empty = 'cfg password="Ab12Cd34" host 8.8.8.8  # ' + IGNORE_MARKER + "()"  # public-tree-scan: ignore-line(public_ip)
    empty_rules = {
        line.split("[", 1)[1].split("]", 1)[0]
        for line in scan_source("self-test-ignore-empty", empty, denylist=[], allow_prefixes=[])
    }
    if "credential_literal" not in empty_rules or "public_ip" not in empty_rules:
        problems.append(f"空括号放过标记不应放过任何规则（实得 {sorted(empty_rules)}）")
    return problems


def self_test(denylist: list[str] | None = None) -> int:
    """阳性对照：**逐例**必须命中（类别清单见 `required`），干净样本必须零命中。

    Issue#159 AC3：原先只断言「10 个类别是否都出现」。整类放松（例如往
    `META_TAIL_WORDS` 里加 `token`）只让个别片段逃逸，其余片段仍覆盖同一类别，
    类别级断言照旧通过 ⇒ 放松判据**测不出来**。逐例断言 + 用例数下限 + 规则归属
    对账 + 跳过面机械契约 + 端到端 `main()` 正反对照之后，任一条放宽路径都会判红
    （证据见 Issue#159 方案 §5 的变异矩阵）。
    """
    words = list(denylist or [])
    bad_denylist = words + ["SCNET-INSTANCE-1"]
    # 与 `KNOWN_RULE_IDS` 同源：汇总断言（main）用同一份登记表，规则改名即红。
    required = set(KNOWN_RULE_IDS)
    # 用例数下限：删掉一条用例（冷审 N10）不能靠「剩下的仍然全中」蒙过去。
    min_cases = 21
    problems: list[str] = []
    coverage: dict[str, list[str]] = {}
    per_case: list[str] = []
    for label, snippet in SELF_TEST_BAD_CASES:
        hits = scan_source(f"self-test-bad:{label}", snippet, denylist=bad_denylist, allow_prefixes=[])
        if not hits:
            problems.append(f"阳性对照未命中：{label}")
        for line in hits:
            coverage.setdefault(line.split("[", 1)[1].split("]", 1)[0], []).append(label)
        per_case.append(f"{label}={len(hits)}")
    if len(SELF_TEST_BAD_CASES) < min_cases:
        problems.append(f"阳性对照用例数 {len(SELF_TEST_BAD_CASES)} 少于下限 {min_cases}")
    missing = required - set(coverage)
    if missing:
        detail = {r: coverage.get(r, []) for r in sorted(missing)}
        problems.append(f"阳性对照类别未覆盖：{sorted(missing)}（归属：{detail}）")
    clean = scan_source("self-test-clean", SELF_TEST_CLEAN, denylist=words, allow_prefixes=[])
    if clean:
        problems.append(f"干净样本被误报：{clean}")
    clean_cn = scan_source(
        "自检/中文-非ASCII 路径示例.md", SELF_TEST_CLEAN_CN, denylist=words, allow_prefixes=[]
    )
    if clean_cn:
        problems.append(f"中文/非 ASCII 干净样本被误报：{clean_cn}")
    # 门①.5 r2 I-7：负面控制的下限（长度 + CJK 双向），否则清空样本仍 rc=0。
    if len(SELF_TEST_CLEAN) < MIN_CLEAN_CHARS:
        problems.append(f"ASCII 干净样本被删短：{len(SELF_TEST_CLEAN)} < {MIN_CLEAN_CHARS}")
    if len(SELF_TEST_CLEAN_CN) < MIN_CLEAN_CN_CHARS:
        problems.append(f"中文干净样本被删短：{len(SELF_TEST_CLEAN_CN)} < {MIN_CLEAN_CN_CHARS}")
    if not _CJK_RE.search(SELF_TEST_CLEAN_CN):
        problems.append("中文干净样本不含 CJK：非 ASCII 路径覆盖被无声删除")
    if SELF_TEST_CLEAN == SELF_TEST_CLEAN_CN:
        problems.append("两份干净样本内容完全相同：非 ASCII 覆盖被合并掉")
    problems += _scan_scope_contract()
    problems += _end_to_end_scope_check()
    problems += _extension_coverage_check()
    problems += _ignore_line_scope_check()
    if problems:
        for line in problems:
            print(f"[self-test] {line}", file=sys.stderr)
        return 1
    print(
        f"[self-test] 阳性对照 {len(SELF_TEST_BAD_CASES)} 例逐例命中、{len(required)} 类全覆盖"
        f"（{'、'.join(per_case)}），干净样本零命中；跳过面契约、端到端 main() 正反对照、"
        f"后缀/体积真文件覆盖（{len(REQUIRED_TEXT_SUFFIXES) + 1} 个夹具）通过"
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
    parser.add_argument(
        "--added-lines", action="store_true",
        help="只扫本次改动**新增的行**（须配合 --base；无法定位新增行的文件退化为整文件判定并在输出注明）",
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
    if args.added_lines and not args.base:
        print(
            "[public-tree] --added-lines 需要 --base（要拿基线才能算新增行）；"
            "整树巡检请去掉 --added-lines。",
            file=sys.stderr,
        )
        return 2
    if args.added_lines and args.paths:
        print(
            "[public-tree] --added-lines 与 --paths 互斥（显式路径没有基线语义）；"
            "门禁请用 --base + --added-lines。",
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
    whole_file_fallback: list[str] = []
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
            text = _read_text_file(path)
        except (OSError, UnicodeDecodeError):
            skipped["unreadable"].append(_rel(path))
            continue
        scanned += 1
        rel = _rel(path)
        if args.added_lines:
            try:
                new_lines = added_lines(repo_root, args.base, rel)
            except GitError as exc:
                # 取新增行失败 ⇒ 宁可红：绝不静默退化成整文件或整树
                print(f"[public-tree] --added-lines 取新增行失败（{rel}）：{exc}", file=sys.stderr)
                return 2
            if new_lines is None:
                whole_file_fallback.append(rel)
            else:
                text = mask_non_added_lines(text, new_lines)
        findings.extend(scan_source(rel, text, denylist=denylist, allow_prefixes=args.allow_prefix))

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
    if args.added_lines:
        print(
            f"[public-tree] 只扫新增行（--added-lines，基线 {args.base}）：扫描 {scanned} 文件；"
            f"整文件判定 {len(whole_file_fallback)} 个",
            file=sys.stderr,
        )
        for rel in whole_file_fallback[:5]:
            print(f"  - 无法定位新增行（纯模式变更/重命名/整文件重写）⇒ 按整文件判定：{rel}", file=sys.stderr)

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
    # AC2 自洽断言（门①.5 r2 I-5）：原先的「逐规则计数之和 == 命中总数」是**恒真**的
    # （`counts` 就是从 `findings` 派生的），断言空转。改为两条真正可失败的不变式：
    # ① 每条命中行必须符合 `路径:行号: [规则] 值` 的严格形状；
    # ② 出现的规则 id 必须全部已登记（`KNOWN_RULE_IDS`）——规则改名/新增而未登记即红。
    malformed = [line for line in findings if not _FINDING_LINE_RE.match(line)]
    unknown = sorted(set(counts) - KNOWN_RULE_IDS)
    if malformed or unknown:
        print(
            f"[public-tree] 汇总自洽检查失败：形状异常 {len(malformed)} 行、未登记规则 id {unknown}"
            f"（已登记：{sorted(KNOWN_RULE_IDS)}）",
            file=sys.stderr,
        )
        return 2
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
