"""选卷矩阵脚本：案卷树 → 候选页池 → 分层随机选样 → 私有清单（Issue#56 M2 / Task 9）。

GT 工作的入口脚本（spec §2）：在真实案卷树上按页型配额选出待标注页集，
产出**选样清单 + 配额对账表 + 案名映射 sidecar** 三件套。零网络、零新依赖
（pypdf 延迟 import、可选；缺失时全链路退化 hints-only 并在清单显性标注）。

配额（spec §2 逐字，键集 = ``gt_schema.PAGE_TYPES`` 唯一事实源）::

    QUOTAS = {"body": 50, "table": 25, "seal_handwriting": 15, "edge": 10}

页型初判 ``classify_page``（启发式，初判一律由用户过目纠正——设计 §2）：

- 优先级：**表格文本信号 > 空白页 > 文件名提示 > body**；
- 表格信号：页文本含「目录/表格/流水」关键词，或列密度高（≥4 有效行且
  ≥40% 行呈两列以上排版/含制表线字符）→ ``table``；
- 空/极少字（strip 后 < 10 字符）且页面无图像信号 → ``edge``（数字空白页）；
- 文件/路径名含 授权|询问|讯问|笔录|照片|回执 → ``seal_handwriting`` 候选
  （文本层在场与否均适用；``--hints`` 可追加正则取并）；
- **扫描页无文本层一律初判 body**：pypdf 提取文本为空但页面带图像 XObject
  时按扫描页处理（宁判 body 不误占 edge 格）→ body（文件名提示命中时先归
  seal_handwriting，提示优先级高于 body 兜底）；
- pypdf 缺失 / 开卷失败 / 页越界 → 该页退化为纯文件名提示判定
  （提示命中 → seal_handwriting，否则 body）。

候选页池（``build_pool``）：递归收集案卷根下 ``*.pdf`` / ``*.PDF``（目录
同名不收；不可读文件跳过并告警），每个 PDF 一个不透明卷编号 ``case_ref``
（``case-001`` 起按路径序编号）。**大卷切段**（spec §2 首段+中段）：页数
≥ 10 取第 0 页（``segment="first"``）与第 n//2 页（``segment="mid"``）；
< 10 页整卷全取（不做切分，segment 一律记 ``first``）；开不了卷按 1 页
（第 0 页）计。池条目形状 ``{"case_ref","file","page","page_type","segment"}``。

选样 ``select``：按页型分层随机（``random.Random(seed)`` 逐格不放回抽样，
格序与遍历序确定），每格取 ``min(配额, 池内数)``——**不足时全取**，缺口由
``reconcile`` 出对账表（逐格 quota/available/selected/shortfall）。选出条目
注入 ``"reason": "quota:<页型>"``（入选理由 = 命中的配额格）；同 seed 两次
选样逐条一致（清单可复现），输入池不被原地改写。

隐私边界（铁律 1 的脚本侧落法）：

- **清单不写案名**：``file`` 字段是文件 sha256（非路径），``case_ref`` 是
  不透明顺序编号——清单可以拿出私有体系过目签字；
- 真实路径只进 **sidecar 映射**（``<清单名>.mapping.json``，与清单同目录
  旁挂），case_ref ↔ {path, sha256, pages_total, classifier}；
- 两文件均经 ``--out`` 落在仓库外私有目录（``ensure_outside_repo`` 守卫强制：
  ``--out`` 解析后落在仓根内直接拒绝），本脚本不读不写仓内任何路径。

清单形状：``{kind, version, classifier, seed, quotas, reconciliation,
mapping_file, selection}``；``classifier`` = ``"pypdf"``（pypdf 在场）或
``"hints-only"``（pypdf 缺失；单文件开卷失败在 sidecar 逐卷标注）。输出
不含时间戳——同输入同 seed 两次落盘逐字节一致，便于 diff 留痕。

CLI（``python eval/gt/select_pages.py``）::

    --src 案卷根 --out 私有清单.json --seed N [--hints 追加提示正则]

错误处理：``--src`` 不是目录 / ``--hints`` 正则非法 / 写盘失败 → stderr
报错、退出码 1、不落盘；``--out`` 落在仓根内 → argparse error（退出码 2，
sidecar 载真实案名不得入仓）；其余 argparse 参数错误按惯例直接 SystemExit。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import sys
from pathlib import Path

if __package__ in (None, ""):  # 直接脚本执行（python eval/gt/select_pages.py）：补 eval/ 进 sys.path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gt.gt_schema import PAGE_TYPES  # noqa: E402

# ---- 常量（spec §2 逐字；键集 = gt_schema.PAGE_TYPES 唯一事实源） ----------------

QUOTAS = {"body": 50, "table": 25, "seal_handwriting": 15, "edge": 10}

# seal_handwriting 文件名/路径提示（brief 逐字；--hints 追加正则与此取并）
_HINT_RE = re.compile(r"授权|询问|讯问|笔录|照片|回执")

_EDGE_TEXT_MIN = 10  # 页文本 strip 后少于此字符数视为空/极少字（→ edge 候选）
_TABLE_KEYWORD_RE = re.compile(r"目录|表格|流水")
_TABLE_COLUMN_RE = re.compile(r"\S(?: {2,}|\t+)\S")  # 一行内 ≥2 空格/制表符分列（两列以上）
_TABLE_BOX_RE = re.compile(r"[─│┌┐└┘├┤┬┴┼═║╔╗╚╝╠╣╦╩╬]")  # 制表线字符
_TABLE_DENSITY_MIN_LINES = 4  # 列密度判定的最少有效行数
_TABLE_DENSITY_RATIO = 0.4    # 呈列式排版的行占比阈值

_BIG_VOLUME_PAGES = 10        # 页数 ≥ 此值按大卷切段：首/中两页（spec §2）

_POOL_KEYS = ("case_ref", "file", "page", "page_type", "segment")
_MANIFEST_KIND = "gt-selection-manifest"
_MAPPING_KIND = "gt-selection-case-mapping"
_MAPPING_SUFFIX = ".mapping.json"


# ---- pypdf（可选依赖，延迟加载） --------------------------------------------------

def _load_pypdf():
    """延迟 import pypdf：缺失返回 None（全链路 hints-only，清单标注）。"""
    try:
        import pypdf
    except ImportError:
        return None
    return pypdf


def _page_has_image(page) -> bool:
    """页资源里是否存在图像 XObject（扫描页信号）。

    读不出资源/子类型一律按有图处理——宁判扫描页 body，不误占 edge 格。
    """
    try:
        xobj = (page.get("/Resources") or {}).get("/XObject")
        if not xobj:
            return False
        for value in xobj.values():
            subtype = value.get_object().get("/Subtype")
            if subtype is None or subtype == "/Image":
                return True
        return False
    except Exception:
        return True


def _probe_page(path: str, page_no: int) -> tuple[str | None, bool | None]:
    """pypdf 抽取该页文本与图像信号；任何失败返回 (None, None)（该页 hints-only）。

    文本抽取失败与开卷失败同判：无文本层证据时不硬造文本判定。
    """
    pypdf = _load_pypdf()
    if pypdf is None:
        return None, None
    try:
        page = pypdf.PdfReader(path).pages[page_no]
        text = page.extract_text() or ""
    except Exception:
        return None, None
    return text, _page_has_image(page)


# ---- 页型初判 ---------------------------------------------------------------------

def _table_signal(text: str) -> bool:
    """表格信号：关键词命中，或行数足够且列式排版行占比达阈值。"""
    if _TABLE_KEYWORD_RE.search(text):
        return True
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) < _TABLE_DENSITY_MIN_LINES:
        return False
    columned = sum(1 for ln in lines
                   if _TABLE_COLUMN_RE.search(ln) or _TABLE_BOX_RE.search(ln))
    return columned / len(lines) >= _TABLE_DENSITY_RATIO


def classify_page(pdf_path: str, page_no: int, text_hint: str = "",
                  hint_re: re.Pattern | None = None) -> str:
    """单页页型初判（启发式；初判由用户过目纠正，设计 §2）。

    - ``text_hint`` 非空：直接作为该页文本（调用方已抽取的合法入口，跳过
      pypdf；此时图像信号不可知，按无图处理）；空则 pypdf 现抽；
    - 优先级：表格文本信号 > 空白页 > 文件名提示 > body（详见模块 docstring；
      扫描页 = 文本空但带图像 XObject → body，提示命中则先归 seal_handwriting）；
    - pypdf 缺失/开卷失败/页越界 → 纯文件名提示判定（命中 → seal_handwriting，
      否则 body）。
    """
    hint = hint_re if hint_re is not None else _HINT_RE
    hint_hit = hint.search(str(pdf_path)) is not None

    if text_hint:
        text: str | None = text_hint
        has_image: bool | None = False
    else:
        text, has_image = _probe_page(str(pdf_path), page_no)

    if text is None:  # 无文本层证据（pypdf 缺失/开卷失败/页越界）→ 纯 hints 判定
        return "seal_handwriting" if hint_hit else "body"
    if _table_signal(text):
        return "table"
    if len(text.strip()) < _EDGE_TEXT_MIN and not has_image:
        return "edge"  # 数字空白页（无图像信号；扫描空白页按 body 初判，见下）
    if hint_hit:
        return "seal_handwriting"
    return "body"  # 含扫描页（有图像无文本层）——一律初判 body，用户过目纠正


# ---- 候选页池 ---------------------------------------------------------------------

def build_hint_re(extra: str | None = None) -> re.Pattern:
    """内置提示正则（可 --hints 追加取并）；非法正则 raise ValueError。"""
    pattern = _HINT_RE.pattern + (f"|{extra}" if extra else "")
    try:
        return re.compile(pattern)
    except re.error as e:
        raise ValueError(f"提示正则非法（{pattern!r}）: {e}") from e


def _sha256_hex(path: Path) -> str:
    """文件字节 sha256（流式；不可读的 OSError 由调用方捕获跳过）。"""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _count_pages(path: Path, pypdf) -> tuple[int | None, str]:
    """开卷数页：(页数, "pypdf")；开不了卷（或无 pypdf）→ (None, "hints-only")。"""
    if pypdf is None:
        return None, "hints-only"
    try:
        return len(pypdf.PdfReader(str(path)).pages), "pypdf"
    except Exception:
        return None, "hints-only"


def _candidate_pages(n_pages: int | None) -> list[tuple[int, str]]:
    """大卷切段规则（spec §2）：≥10 页取首/中两页；<10 页全取；开不了卷按 1 页。"""
    if n_pages is None:
        return [(0, "first")]
    if n_pages == 0:
        return []
    if n_pages >= _BIG_VOLUME_PAGES:
        return [(0, "first"), (n_pages // 2, "mid")]
    return [(i, "first") for i in range(n_pages)]


def build_pool(src: Path, hint_re: re.Pattern | None = None) -> tuple[list[dict], dict]:
    """遍历案卷根造候选页池，返回 ``(池条目列表, case_ref → 卷信息映射)``。

    - 递归收 ``*.pdf`` / ``*.PDF``（大小写不敏感文件系统下两 glob 可能重合，
      以集合去重；目录同名不收；按路径序排序——case_ref 编号确定）；
    - 不可读文件告警跳过；每个 PDF 一个 ``case-NNN`` 不透明卷编号；
    - 池条目 ``file`` 字段 = 文件 sha256（清单不写案名的隐私边界，见模块
      docstring）；真实路径只进映射表（sidecar 落私有目录）。
    """
    src = Path(src)
    if not src.is_dir():
        raise ValueError(f"案卷根不存在或不是目录: {src}")
    files = sorted({p for pattern in ("*.pdf", "*.PDF") for p in src.rglob(pattern)
                    if p.is_file()},
                   key=lambda p: p.as_posix())

    pypdf = _load_pypdf()
    pool: list[dict] = []
    mapping: dict[str, dict] = {}
    for idx, path in enumerate(files, start=1):
        case_ref = f"case-{idx:03d}"
        try:
            sha = _sha256_hex(path)
        except OSError as e:
            print(f"[select_pages] 警告: 文件不可读，跳过 {path}: {e}", file=sys.stderr)
            continue
        n_pages, classifier = _count_pages(path, pypdf)
        mapping[case_ref] = {"path": str(path.resolve()), "sha256": sha,
                             "pages_total": n_pages, "classifier": classifier}
        for page_no, segment in _candidate_pages(n_pages):
            pool.append({"case_ref": case_ref, "file": sha, "page": page_no,
                         "page_type": classify_page(str(path), page_no, hint_re=hint_re),
                         "segment": segment})
    return pool, mapping


# ---- 分层随机选样与对账 ------------------------------------------------------------

def _check_quotas(quotas: dict) -> None:
    """配额守卫：键集恰为 PAGE_TYPES，值须为非负整数（bool 不算整数配额）。"""
    if not isinstance(quotas, dict) or set(quotas) != set(PAGE_TYPES):
        got = sorted(quotas) if isinstance(quotas, dict) else type(quotas).__name__
        raise ValueError(f"quotas 键必须恰为 PAGE_TYPES {sorted(PAGE_TYPES)}，实得 {got}")
    for cell, n in quotas.items():
        if isinstance(n, bool) or not isinstance(n, int) or n < 0:
            raise ValueError(f"quotas[{cell!r}] 须为非负整数，实得 {n!r}")


def select(src_manifest: list[dict], quotas: dict, seed: int) -> list[dict]:
    """按页型分层随机选样（seed 可复现；格内不足全取，缺口走 reconcile 对账）。

    - 输入池条目须含 ``{"case_ref","file","page","page_type","segment"}``，
      ``page_type`` ∈ gt_schema.PAGE_TYPES（不合格即 ValueError，宁缺毋滥）；
    - 逐格（格序 = sorted(PAGE_TYPES)，确定）以 ``random.Random(seed)`` 不放回
      抽 ``min(配额, 格内数)`` 条，选出副本注入 ``"reason": "quota:<页型>"``
      （入选理由 = 命中的配额格）；输入池不被原地改写。
    """
    _check_quotas(quotas)
    pool_by_type: dict[str, list[dict]] = {cell: [] for cell in sorted(PAGE_TYPES)}
    for i, entry in enumerate(src_manifest):
        if not isinstance(entry, dict):
            raise ValueError(f"候选池第 {i} 条非对象")
        missing = [k for k in _POOL_KEYS if k not in entry]
        if missing:
            raise ValueError(f"候选池第 {i} 条缺字段 {missing}")
        if entry["page_type"] not in PAGE_TYPES:
            raise ValueError(f"候选池第 {i} 条 page_type {entry['page_type']!r} "
                             f"不在 PAGE_TYPES {sorted(PAGE_TYPES)}")
        pool_by_type[entry["page_type"]].append(entry)

    rng = random.Random(seed)
    selected: list[dict] = []
    for cell in sorted(PAGE_TYPES):
        candidates = pool_by_type[cell]
        for entry in rng.sample(candidates, min(quotas[cell], len(candidates))):
            projected = {k: entry[k] for k in _POOL_KEYS}
            selected.append({**projected, "reason": f"quota:{cell}"})
    return selected


def reconcile(src_manifest: list[dict], quotas: dict, selection: list[dict]) -> dict:
    """配额对账表：逐格 {quota, available, selected, shortfall}（格序确定，可 diff）。"""
    _check_quotas(quotas)
    available: dict[str, int] = {}
    for entry in src_manifest:
        ptype = entry.get("page_type") if isinstance(entry, dict) else None
        if ptype in PAGE_TYPES:
            available[ptype] = available.get(ptype, 0) + 1
    filled: dict[str, int] = {}
    for entry in selection:
        ptype = entry.get("page_type")
        if ptype in PAGE_TYPES:
            filled[ptype] = filled.get(ptype, 0) + 1
    return {cell: {"quota": quotas[cell],
                   "available": available.get(cell, 0),
                   "selected": filled.get(cell, 0),
                   "shortfall": max(0, quotas[cell] - filled.get(cell, 0))}
            for cell in sorted(PAGE_TYPES)}


# ---- 落盘 -------------------------------------------------------------------------

def ensure_outside_repo(out_path: Path, repo_root: Path | None = None) -> Path:
    """输出路径防入仓守卫：``--out`` 解析后落在仓根（含子目录）内即 ValueError。

    sidecar 案名映射载真实案名/路径（铁律 1），清单与其必须落仓外私有目录；
    仓根 = 本文件上溯两级（worktree 根）。``repo_root`` 供测试注入假仓根
    （tmp_path 在真仓外，直接测真仓根亦可）。返回解析后的绝对路径。
    """
    root = Path(__file__).resolve().parents[2] if repo_root is None else Path(repo_root)
    resolved = Path(out_path).resolve()
    if resolved.is_relative_to(root):
        raise ValueError(f"--out 不得落在仓库内（sidecar 载真实案名，铁律 1）: "
                         f"{resolved} ⊂ 仓根 {root}")
    return resolved


def write_outputs(out_path: Path, *, selection: list[dict], reconciliation: dict,
                  mapping: dict, seed: int, classifier: str) -> None:
    """写私有清单与 sidecar 案名映射（同目录旁挂；父目录不存在则创建）。"""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    sidecar = out_path.with_name(out_path.stem + _MAPPING_SUFFIX)
    manifest = {
        "kind": _MANIFEST_KIND,
        "version": 1,
        "classifier": classifier,
        "seed": seed,
        "quotas": dict(QUOTAS),
        "reconciliation": reconciliation,
        "mapping_file": sidecar.name,  # 同目录文件名（目录整体搬移不断链）
        "selection": selection,
    }
    out_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
    sidecar_doc = {"kind": _MAPPING_KIND, "version": 1, "cases": mapping}
    sidecar.write_text(json.dumps(sidecar_doc, ensure_ascii=False, indent=2) + "\n",
                       encoding="utf-8")


# ---- CLI（薄壳：装配逻辑全在上文函数） --------------------------------------------

def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="select_pages",
        description="GT 选卷矩阵：案卷树 → 页型配额分层随机选样 → 私有清单（Issue#56 M2）")
    parser.add_argument("--src", required=True, help="案卷根目录（递归收 *.pdf/*.PDF）")
    parser.add_argument("--out", required=True,
                        help="私有清单 JSON 输出路径（须在仓库外；旁挂 .mapping.json 案名映射）")
    parser.add_argument("--seed", required=True, type=int,
                        help="选样随机种子（落盘清单；同 seed 可复现）")
    parser.add_argument("--hints", default=None,
                        help="追加 seal_handwriting 文件名/路径提示正则（与内置提示取并）")
    args = parser.parse_args(argv)
    try:
        ensure_outside_repo(Path(args.out))
    except ValueError as e:
        parser.error(str(e))  # 防入仓：argparse error 惯例退出码 2，不落盘
    return args


def main(argv: list[str] | None = None) -> int:
    """CLI 入口：0 成功；1 业务错误（--src 不存在/正则非法/写盘失败，不落盘）。"""
    args = _parse_args(argv)
    try:
        hint_re = build_hint_re(args.hints)
        pool, mapping = build_pool(Path(args.src), hint_re)
        selection = select(pool, QUOTAS, args.seed)
        recon = reconcile(pool, QUOTAS, selection)
        classifier = "pypdf" if _load_pypdf() is not None else "hints-only"
        write_outputs(Path(args.out), selection=selection, reconciliation=recon,
                      mapping=mapping, seed=args.seed, classifier=classifier)
        shortfall = sum(cell["shortfall"] for cell in recon.values())
        print(f"[select_pages] 候选 {len(pool)} 页 → 选出 {len(selection)} 页"
              f"（配额缺口合计 {shortfall} 页，见对账表）；classifier={classifier}；"
              f"清单 {args.out}", file=sys.stderr)
        return 0
    except Exception as e:  # CLI 边界：错误进 stderr、非零退出、不落盘
        print(f"[select_pages] 错误: {type(e).__name__}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
