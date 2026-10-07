"""GT pagepack 模式校验与 JSONL 定稿写出（Issue#56 M1 / Task 6）。

GT 管线的质量闸门：Task 7 组装逐页 pagepack，本模块定义其契约——**先校验后
写出**，任何不合格页拒绝定稿（fail-fast，宁缺毋滥）。零网络、纯本地。

常量单一事实源（Task 4/5 自本模块取用）：

- ``PAGE_TYPES``        有效页型（``body``/``table``/``seal_handwriting``/
                        ``edge``；T4 评审 Minor#3 收编——此前以 compare 模块
                        常量为占位事实源，现 compare 仅 re-export）；
- ``ARBITRATION_RULES`` Task 5 仲裁分支编号 R1-R7；
- ``VERIFY_STATES``     实体 verify 状态机：``consistent``（R1 两云一致）/
                        ``arbitrated``（R2-R7 自动裁决采信）/
                        ``user-confirmed``（工作台人工确认）/
                        ``user-corrected``（工作台人工改判）；
- ``PRESET_TYPE_NAMES`` 实体类型名全集，**动态**读 ``eval/scripts/
                        common_api.py`` 的 ``TYPE_ID_TO_NAME`` 值集（其读
                        backend/config/preset_entity_types.json 单一事实源，
                        不硬编码类型名列表，preset 改名自动跟随）。

pack 契约（Task 7 按此组装）::

    {"page_id": str 非空,
     "page_type": ∈ PAGE_TYPES,
     "source": {"file_sha256", "page", "carrier", "segment"},
     "transcript_gt": {"text", "normalized_text", "fidelity"},
     "entities":    [Entity（Task 3 形状）+ "verify" ∈ VERIFY_STATES, ...],
     "adjudications": [Task 5 仲裁条目（rule ∈ R1..R7）, ...]}

``validate_pagepack`` 校验项（返回错误串列表，空 = 合法）：

- page_id 非空（纯空白视同空）；page_type ∈ PAGE_TYPES；
- source 含 file_sha256 / page / carrier / segment；
- transcript_gt 含 text / normalized_text（span 界的度量基准）；
- entities 每条：type ∈ PRESET_TYPE_NAMES；
  ``0 <= span_original[0] <= span_original[1] <= len(transcript_gt.text)``；
  span_normalized 同理以归一化面为界（``len(normalized_text)``）；
  verify ∈ VERIFY_STATES；
- adjudications 每条 rule ∈ ARBITRATION_RULES。

重叠策略：同类型且 span_original 完全相同 → error（重复实体）；同类型部分
重叠 → 允许，以 ``"warning:"`` 前缀条目记入同一返回列表（前缀即可区分级别，
不单独成通道）。跨类型重叠不在契约内（Task 3 merge 的 conflict 标注本就
允许同 span 异型共存）。

``write_gt_jsonl``：一页一行 JSONL，每行注入 ``"gt_version": version``（写入
副本，入参 pack 不被修改）；每个 adjudication 条目同时注入 ``"at"`` = 定稿日
（写入当日 ISO 日期，spec §4——pack 定稿前不落任何日期）；任一 pack 校验失败
即 raise ``ValueError``（错误清单带 ``packs[i]`` 定位），文件不落盘。中文按
``ensure_ascii=False`` 原文写出（GT 底稿可读性优先）。

依赖注记：本模块经 common_api 间接依赖 httpx 与 backend/config/
preset_entity_types.json（import 时读取）——精简环境运行需先安装。
"""
from __future__ import annotations

import importlib.util
import json
from datetime import date
from pathlib import Path

# ---- 常量（单一事实源） ---------------------------------------------------------

# 有效页型（GT 管线唯一事实源；compare re-export，arbitrate 经 compare 取用）
PAGE_TYPES = {"body", "table", "seal_handwriting", "edge"}

# Task 5 仲裁分支编号（adjudications 的 rule 值域）
ARBITRATION_RULES = {f"R{i}" for i in range(1, 8)}

# 实体 verify 状态机（consistent/arbitrated 由管线写入；user-* 由工作台写入）
VERIFY_STATES = {"consistent", "arbitrated", "user-confirmed", "user-corrected"}


def _load_preset_type_names() -> frozenset[str]:
    """从 common_api.TYPE_ID_TO_NAME 动态取 preset 类型名集（单一事实源）。

    按文件路径加载（test_gt_entities 同款，不污染 sys.path、不依赖 scripts
    为包）；common_api 自行定位仓内 preset_entity_types.json。
    """
    path = Path(__file__).resolve().parents[1] / "scripts" / "common_api.py"
    spec = importlib.util.spec_from_file_location("gt_schema_preset_source", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return frozenset(module.TYPE_ID_TO_NAME.values())


# 实体类型名全集（preset 中文名；测试锁定其与 common_api.TYPE_ID_TO_NAME 一致）
PRESET_TYPE_NAMES = _load_preset_type_names()

# source 必备键（pack 契约）
_SOURCE_KEYS = ("file_sha256", "page", "carrier", "segment")


def _span_error(index: int, face: str, span: object, bound: int) -> str | None:
    """单面 span 校验：须为 [start, end] 整数对且 0 <= start <= end <= bound。"""
    if (not isinstance(span, (list, tuple)) or len(span) != 2
            or not all(isinstance(x, int) and not isinstance(x, bool) for x in span)):
        return f"entities[{index}].{face} {span!r} 缺失或非法（需 [start, end] 整数对）"
    start, end = span
    if not (0 <= start <= end <= bound):
        return f"entities[{index}].{face} {span!r} 越界（要求 0 <= start <= end <= {bound}）"
    return None


def validate_pagepack(pack: dict) -> list[str]:
    """校验一条 pagepack，返回错误串列表（空 = 合法；warning 以 ``warning:`` 前缀同列）。"""
    if not isinstance(pack, dict):
        return ["pack 非对象（dict）"]
    errors: list[str] = []

    page_id = pack.get("page_id")
    if not isinstance(page_id, str) or not page_id.strip():
        errors.append("page_id 缺失或为空")

    page_type = pack.get("page_type")
    if page_type not in PAGE_TYPES:
        errors.append(f"page_type {page_type!r} 不在 PAGE_TYPES {sorted(PAGE_TYPES)}")

    source = pack.get("source")
    if not isinstance(source, dict):
        errors.append("source 缺失或非对象")
    else:
        errors.extend(f"source 缺 {key!r}" for key in _SOURCE_KEYS if key not in source)

    transcript = pack.get("transcript_gt")
    if not isinstance(transcript, dict):
        errors.append("transcript_gt 缺失或非对象")
        transcript = {}
    text = transcript.get("text")
    norm_text = transcript.get("normalized_text")
    if not isinstance(text, str):
        errors.append("transcript_gt.text 缺失或非字符串")
        text = ""
    if not isinstance(norm_text, str):
        errors.append("transcript_gt.normalized_text 缺失或非字符串")
        norm_text = ""

    entities = pack.get("entities")
    if not isinstance(entities, list):
        errors.append("entities 缺失或非列表")
        entities = []
    for i, ent in enumerate(entities):
        if not isinstance(ent, dict):
            errors.append(f"entities[{i}] 非对象")
            continue
        if ent.get("type") not in PRESET_TYPE_NAMES:
            errors.append(f"entities[{i}].type {ent.get('type')!r} 不在 preset 类型名集")
        for face, bound in (("span_original", len(text)),
                            ("span_normalized", len(norm_text))):
            problem = _span_error(i, face, ent.get(face), bound)
            if problem:
                errors.append(problem)
        if ent.get("verify") not in VERIFY_STATES:
            errors.append(f"entities[{i}].verify {ent.get('verify')!r} 不在 VERIFY_STATES")

    # 重叠策略（span_original 半开区间）：同类型完全同 span = error；部分重叠 = warning
    spans = [(i, e) for i, e in enumerate(entities)
             if isinstance(e, dict)
             and isinstance(e.get("span_original"), (list, tuple))
             and len(e["span_original"]) == 2
             and all(isinstance(x, int) for x in e["span_original"])]
    for pos, (i, ei) in enumerate(spans):
        for j, ej in spans[pos + 1:]:
            if ei.get("type") != ej.get("type"):
                continue
            (a0, a1), (b0, b1) = ei["span_original"], ej["span_original"]
            if a0 == b0 and a1 == b1:
                errors.append(f"entities[{i}] 与 entities[{j}] 同类型 {ei.get('type')!r} "
                              f"且 span_original 完全相同 {[a0, a1]!r}（重复实体）")
            elif a0 < b1 and b0 < a1:
                errors.append(f"warning: entities[{i}] 与 entities[{j}] 同类型 "
                              f"{ei.get('type')!r} span_original 部分重叠 "
                              f"{[a0, a1]!r}/{[b0, b1]!r}（允许，留痕）")

    adjudications = pack.get("adjudications")
    if not isinstance(adjudications, list):
        errors.append("adjudications 缺失或非列表")
        adjudications = []
    for i, adj in enumerate(adjudications):
        rule = adj.get("rule") if isinstance(adj, dict) else None
        if rule not in ARBITRATION_RULES:
            errors.append(f"adjudications[{i}].rule {rule!r} 不在 ARBITRATION_RULES")

    return errors


def write_gt_jsonl(packs: list[dict], out_path: Path, version: str) -> None:
    """逐页校验并写 GT JSONL（一页一行，每行注入 ``"gt_version": version``）。

    先校验后写出：任一 pack 不合法即 raise ``ValueError``（错误清单带
    ``packs[i]`` 定位），文件不落盘。入参 pack 不被修改（``gt_version`` 与
    adjudications 的 ``"at"`` 定稿日均注入在写出副本上；``at`` = 写出当日
    ISO 日期，spec §4——``validate_pagepack`` 不要求该字段，pack 定稿前
    不落任何日期）。
    """
    problems: list[str] = []
    for idx, pack in enumerate(packs):
        problems.extend(f"packs[{idx}]: {err}" for err in validate_pagepack(pack))
    if problems:
        raise ValueError("GT pagepack 校验失败（未写出文件）：\n" + "\n".join(problems))
    at = date.today().isoformat()  # 定稿日：一次定稿一个日期（整批一致）
    with Path(out_path).open("w", encoding="utf-8") as f:
        for pack in packs:
            adjs = [{**adj, "at": at} for adj in pack["adjudications"]]
            f.write(json.dumps({**pack, "gt_version": version,
                                "adjudications": adjs},
                               ensure_ascii=False) + "\n")
