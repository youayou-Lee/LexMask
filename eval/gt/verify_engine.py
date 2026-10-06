"""合成集验证闸 A1/A2（Issue#56 M1 / Task 8，M1 出口闸门）。

对合成集（eval/datasets/manifest.json 的 e2e 条目）逐文件逐页跑 GT 预标流水线，
对照合成 GT 回答两个问题：

- **A1 分类型 P/R**：引擎实体（采信面 norm 域串）对合成 GT 的分类型精确率 /
  召回率。匹配规则 = **squash 域串相等 + 类型一致**（合成 GT 无 offset，串匹配
  是现成口径）：引擎实体 ``text`` 本就在归一化（squash）面，GT 串过
  ``gt.normalize.normalize_text`` 进入同域后按页按类型集合精确匹配。计数与
  P/R 公式**整段复用** ``backend/scripts/eval/eval_ner_quality.py``（Issue#23
  质量闸同源，sys.path 注入 import，不复制口径）：
  ``_match_records`` 产出其 ``compute_metrics`` 的 records 形状，分类型
  tp/fp/fn/precision/recall 全部来自 ``compute_metrics``（页内集合去重口径与其
  一致）；``per_type_pr`` 是单批（一文件或多文件 packs × 一份 GT）的公开包装。
- **A2 注入检验**：对 ≥10 页的 v6 转录各植入 ≥5 处（合计 ≥50）已知分歧，重跑
  compare+arbitrate（``_arbitrate_pair``：run_page 核心的离线复刻，md 恒缺席），
  检出率 = 病灶进入 disputed/auto_resolved 的比例。注入面 = v6 转录、对照面 =
  **同页干净 v6 转录**（而非 VL 面）：把引擎间差异从检验中隔离，任何读数分歧
  必然源自病灶，逐病灶归因才成立。

门槛（brief 逐字值，不因结果放宽）：
- A1：总体 P、R ≥ 0.95，且每个 GT 实体数 ≥5 的类型 P、R ≥ 0.95；
- A2：检出率 ≥ 0.95，且注毒页 ≥10、病灶合计 ≥50。

病灶五类轮转（``inject_disputes``，seed 确定性）：``swap`` 字符替换 / ``drift``
span 漂移（实体尾字符截断或复制延伸）/ ``drop`` 整实体删 / ``type_flip`` 类型
翻转（破坏正则结构位使原通道不再命中，如身份证号世纪位被换则翻为银行卡号形）/
``extra`` 多字（插入实体形额外文本——「空页多字」的有牙变体：纯水印字在长正文
页上不进任何实体读数，闸门无从谈起；空页/短页场景由 edge 域 single_side 兜住）。
无实体可打时退化为字符级操作（病灶仍可复核，检出与否如实进报告）。病灶坐标
同时记录原转录域（``orig_start/orig_end``）与病变转录域（``start/end``），
检测用病变域与采信面 span_original 相交、复核用原域重建。

检出判据（``_lesion_detected``，按病灶逐一判定）：
1. 页级 gap（R5/R6 整页升级）在场 → 该页全部病灶检出（整页显性化，人工必见）；
2. disputed 条目的 a 面（注入面）候选实体 span 与病灶区间相交 → 检出；
3. disputed 条目的 b 面候选文本 == 病灶 target_text（被删/被打散的原读数在对侧
   露面）→ 检出；auto_resolved 同理（R2 胜者实体按 source 归面判定）。

报告写 ``{work}/verify/A1A2-<日期>.md``（工作目录，不入仓），含合成集构成清单
（页型×类型×量）、A1 分类型表、A2 逐病灶表、**单行判定** ``A1=PASS/FAIL
A2=PASS/FAIL``（同时打印 stdout，exit 0/1 供门禁引用），以及给 A5 抽检设计的
**盲区清单**（两个已知仲裁盲区，见 REPORT_BLIND_SPOTS）。

云凭据红线：token 只经环境变量 CLOUD_VL_TOKEN（gt.unify.build_clients 负责，
本模块零接触凭据）；报告与日志零 token。离线约定：单测打桩 build_clients，
绝不实例化真实云客户端。
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
from datetime import date
from pathlib import Path

# gt.* 导入前置：python -m eval.gt.verify_engine / 直接脚本执行 / 测试导入三态通用
_EVAL_ROOT = str(Path(__file__).resolve().parents[1])
if _EVAL_ROOT not in sys.path:
    sys.path.insert(0, _EVAL_ROOT)

# P/R 口径单一事实源：Issue#23 的 eval_ner_quality（sys.path 注入，不复制口径）
_BACKEND_EVAL = str(Path(__file__).resolve().parents[2] / "backend" / "scripts" / "eval")
if _BACKEND_EVAL not in sys.path:
    sys.path.insert(0, _BACKEND_EVAL)
import eval_ner_quality as _nerq  # noqa: E402

from gt.arbitrate import arbitrate_page  # noqa: E402
from gt.compare import compare_transcripts  # noqa: E402
from gt.entities import REGEX_CHANNELS, extract_ner, extract_regex, merge_entities  # noqa: E402
from gt.normalize import FaceMap, normalize_text  # noqa: E402
from gt.pagepack import CachedTranscriptionClient, _pick_page, run_page  # noqa: E402
from gt.run_pipeline import build_ner, map_page_type, parse_clients  # noqa: E402

# ---- 门槛常量（brief 逐字值） ------------------------------------------------------

PR_THRESHOLD = 0.95        # A1：总体与分类型 P/R 门槛
PER_TYPE_MIN_GT = 5        # A1：分类型门槛的 GT 实体数下限（<5 豁免）
A2_MIN_PAGES = 10          # A2：注毒页数下限
A2_MIN_LESIONS = 50        # A2：病灶总数下限
A2_PER_PAGE = 5            # A2：每页植入病灶数
A2_THRESHOLD = 0.95        # A2：检出率门槛

# 五类病灶轮转（kind 名与 brief/测试脚手架一致）
_KINDS = ("swap", "drift", "drop", "type_flip", "extra")

_RX = {name: re.compile(pattern) for name, pattern in REGEX_CHANNELS.items()}
_INSERT_TEXT = "13800138000"   # extra 病灶的实体形多字（凭空多出的电话号）
_DIGITS = "0123456789"

# A2 页选取偏好说明：病灶五类中三类以实体为靶（drift/drop/type_flip），注毒页
# 优先选 v6 转录上有正则实体的页；实体真空页的字符级病灶在长正文页上不进读数、
# 本就不在 compare+arbitrate 的裁决范围（其显性化通道是 edge 域 single_side）。
A2_PREFER_ENTITY_PAGES = True


# ---- A1：分类型 P/R ---------------------------------------------------------------

def _match_records(engine_packs: list[dict], gt_json: dict) -> list[dict]:
    """引擎 packs × 合成 GT 页对齐 → ``eval_ner_quality.compute_metrics`` records。

    - 对齐键 = ``pack["source"]["page"]`` ↔ GT ``pages[].page``；GT 未列页的
      pack 不参评（如 docx 条目 GT 只列第 0 页——缺席页数由调用方对账披露）；
    - 匹配规则 = squash 域串相等 + 类型一致：GT 串过 ``normalize_text`` 进
      squash 域，与引擎实体 norm 面 text 按页按类型集合精确匹配（页内去重，
      与 eval_ner_quality 口径一致）。
    """
    gt_pages = {p.get("page"): (p.get("entities") or {})
                for p in (gt_json.get("pages") or [])}
    records: list[dict] = []
    for pack in engine_packs or []:
        page = (pack.get("source") or {}).get("page")
        if page not in gt_pages:
            continue
        gt_squashed = {t: [normalize_text(s) for s in vals]
                       for t, vals in gt_pages[page].items()}
        pred: dict[str, list[str]] = {}
        for ent in pack.get("entities") or []:
            pred.setdefault(ent["type"], []).append(ent["text"])
        records.append({"page_id": pack.get("page_id"), "gt": gt_squashed, "pred": pred})
    return records


def per_type_pr(engine_packs: list[dict], gt_json: dict) -> dict[str, dict]:
    """单批分类型 P/R（brief 接口）：``{类型: {"p","r","tp","fp","fn"}}``。

    tp/fp/fn 与 P/R 全部取自 ``eval_ner_quality.compute_metrics``（Issue#23
    同源口径，不复制公式）；批量门槛场景（verify main）在 records 层合并后
    一次 ``compute_metrics`` 得总体+分类型，与本函数同口径等价。
    """
    metrics = _nerq.compute_metrics(_match_records(engine_packs, gt_json))
    return {t: {"p": s["precision"], "r": s["recall"],
                "tp": s["tp"], "fp": s["fp"], "fn": s["fn"]}
            for t, s in metrics["per_type"].items()}


def a1_gate(metrics: dict) -> tuple[bool, list[str]]:
    """A1 门槛：总体 P、R ≥0.95 且 GT 实体数 ≥5 的类型 P、R ≥0.95（逐字值）。

    返回 ``(pass, 失败项描述列表)``；零参评页直接 FAIL（空跑不算过闸）。
    """
    failing: list[str] = []
    if not metrics.get("pages"):
        return False, ["参评页数为 0（无任何 pack 对上 GT）"]
    overall = metrics["overall"]
    if overall["precision"] < PR_THRESHOLD:
        failing.append(f"总体 P={overall['precision']:.4f} < {PR_THRESHOLD}")
    if overall["recall"] < PR_THRESHOLD:
        failing.append(f"总体 R={overall['recall']:.4f} < {PR_THRESHOLD}")
    for etype, s in metrics["per_type"].items():
        gt_n = s["tp"] + s["fn"]
        if gt_n < PER_TYPE_MIN_GT:
            continue  # 样本不足的类型豁免分类型门槛（仍计入总体）
        if s["precision"] < PR_THRESHOLD or s["recall"] < PR_THRESHOLD:
            failing.append(f"{etype}（GT {gt_n}）P={s['precision']:.4f} "
                           f"R={s['recall']:.4f}（tp {s['tp']} / fp {s['fp']} / fn {s['fn']}）")
    return (not failing, failing)


# ---- A2：病灶注入与检出 -----------------------------------------------------------

def _free(occupied: list[tuple[int, int]], start: int, end: int) -> bool:
    """候选区间与已占位区间不相交（端点相接允许）。"""
    return all(end <= s or start >= e for s, e in occupied)


def _sub_char(ch: str) -> str:
    """字符替换的替身：数字换邻近数字、其它换固定异字（确定性，无随机取字）。"""
    if ch in _DIGITS:
        return "1" if ch != "1" else "2"
    return "乙" if ch != "乙" else "丙"


def _find_pos(text: str, occupied: list[tuple[int, int]], rng: random.Random,
              pred=None, extra_insert: bool = False) -> int | None:
    """RNG 采样一个空闲位置（sub/delete 用 [p,p+1)、extra 插入用零宽 [p,p)）。"""
    hi = len(text) + 1 if extra_insert else len(text)
    if hi <= 0:
        return None
    for _ in range(64):
        p = rng.randrange(hi)
        if not _free(occupied, p, p + 1):
            continue
        if extra_insert:
            left_ok = p == 0 or not text[p - 1].isdigit()
            right_ok = p == len(text) or not text[p].isdigit()
            if left_ok and right_ok:
                return p
            continue
        if pred is None or pred(p):
            return p
    return None


def _pick_entity(ents: list[dict], occupied: list[tuple[int, int]],
                 rng: random.Random, region_fn, text_len: int) -> dict | None:
    """RNG 洗牌后取第一个 region_fn(e) 区间空闲的实体（区间须在原域界内）。"""
    order = list(ents)
    rng.shuffle(order)
    for ent in order:
        s, e = ent["span_original"]
        start, end = region_fn(s, e)
        if 0 <= start <= end <= text_len and _free(occupied, start, end):
            return ent
    return None


def _entity_flips(text: str, ent: dict, face: FaceMap) -> list[tuple[int, str, str, bool]]:
    """type_flip 的实体级候选：换一个数字位使原类型正则不再命中的全部方案。

    返回 ``[(原域位置 q, 替身数字, 翻转后 norm 域实体串, 是否翻入他通道), ...]``
    （真翻转型优先、位序次之，均确定性）；空列表 = 无结构位可破（如全数字
    银行卡号），调用方退化为实体内普通替换。raw↔norm 同位经 FaceMap 精确
    互映射（数字替身恒为 ASCII 数字，norm 位不变）。
    """
    s, e = ent["span_original"]
    n0, n1 = face.to_normalized(s, e)
    norm_seg = face.norm[n0:n1]  # = ent["text"]（抽取器契约，此处以 face.norm 为准）
    candidates = []
    for n_off, nch in enumerate(norm_seg):
        if nch not in _DIGITS:
            continue
        for d in _DIGITS:
            if d == nch:
                continue
            flipped = norm_seg[:n_off] + d + norm_seg[n_off + 1:]
            new_types = {name for name, rx in _RX.items() if rx.fullmatch(flipped)}
            if ent["type"] not in new_types:
                candidates.append((n_off, d, flipped, bool(new_types - {ent["type"]})))
                break  # 该位取第一个可行替身（确定性）
    true_flips = [c for c in candidates if c[3]]
    vanish = [c for c in candidates if not c[3]]
    ordered = []
    for n_off, d, flipped, true in true_flips + vanish:
        q = face.to_original(n0 + n_off, n0 + n_off + 1)[0]
        ordered.append((q, d, flipped, true))
    return ordered


def inject_disputes(transcript: str, n: int, seed: int) -> tuple[str, list[dict]]:
    """向转录植入 n 处已知分歧（五类轮转，seed 确定性），返回 (病变转录, 病灶清单)。

    病灶条目：``{"kind", "start", "end"}``（病变转录域坐标，与注入面实体
    span_original 直接可比）+ ``"orig_start"/"orig_end"``（原转录域，可复核重建）
    + ``"a_text"``（注入面病变读数串：替换/翻转/插入后的实体串或字符；纯删除为 ""）
    + ``"target_text"``（被打的原实体串；字符级病灶为 None）+ ``"detail"``。

    五类轮转（``_KINDS`` 顺序，第 i 处 = kinds[i % 5]）：swap 字符替换 /
    drift span 漂移（实体尾字符截断或复制延伸，rng 二选一）/ drop 整实体删 /
    type_flip 类型翻转（破正则结构位；无实体或无结构位时退化字符替换）/
    extra 多字（插入实体形文本）。无实体可打时全部退化为字符级操作。区间
    互不重叠（端点相接允许）；极端小文本放不下则少放（宁少勿假，不凑数）。
    """
    if n <= 0 or not transcript:
        return transcript, []
    rng = random.Random(seed)
    face = FaceMap.from_raw(transcript)
    ents = extract_regex(face.norm, face)
    occupied: list[tuple[int, int]] = []
    plans: list[dict] = []
    for i in range(n):
        kind = _KINDS[i % len(_KINDS)]
        plan = _plan_lesion(kind, transcript, face, ents, occupied, rng)
        if plan is None:
            continue
        # 占位：零宽插入按 [p, p+1) 记账，防与同点其它病灶歧义
        occupied.append((plan["orig_start"], max(plan["orig_end"], plan["orig_start"] + 1)))
        plans.append(plan)

    # 原域坐标从后往前逐个应用（区间互不相交，组合良定义）
    dis = transcript
    for plan in sorted(plans, key=lambda p: p["orig_start"], reverse=True):
        s, e = plan["orig_start"], plan["orig_end"]
        dis = dis[:s] + plan["repl"] + dis[e:]

    # 病变域坐标 = 原域坐标 + 其前方全部病灶的长度增量（不含自身——零宽插入的
    # 自身增量不得计入自身位移，否则坐标右移一个插入长度、检出区间相交失准）
    lesions: list[dict] = []
    for idx, plan in enumerate(plans):
        shift = sum(len(q["repl"]) - (q["orig_end"] - q["orig_start"])
                    for j, q in enumerate(plans)
                    if j != idx and q["orig_end"] <= plan["orig_start"])
        lesions.append({
            "kind": plan["kind"],
            "start": plan["orig_start"] + shift,
            "end": plan["orig_start"] + shift + len(plan["repl"]),
            "orig_start": plan["orig_start"], "orig_end": plan["orig_end"],
            "a_text": plan["a_text"], "target_text": plan["target_text"],
            "detail": plan["detail"],
        })
    return dis, lesions


def _plan_lesion(kind: str, text: str, face: FaceMap, ents: list[dict],
                 occupied: list[tuple[int, int]], rng: random.Random) -> dict | None:
    """单病灶规划：返回 {kind, orig_start, orig_end, repl, a_text, target_text, detail}。"""
    if kind == "extra":  # 空页多字（实体形）：零宽插入点
        p = _find_pos(text, occupied, rng,
                      pred=lambda i: (i == 0 or not text[i - 1].isdigit())
                      and (i == len(text) or not text[i].isdigit()),
                      extra_insert=True)
        if p is None:
            return None
        return {"kind": kind, "orig_start": p, "orig_end": p, "repl": _INSERT_TEXT,
                "a_text": _INSERT_TEXT, "target_text": None,
                "detail": f"extra：位置 {p} 插入实体形多字 {_INSERT_TEXT!r}"}

    if kind == "drop":
        ent = _pick_entity(ents, occupied, rng, lambda s, e: (s, e), len(text))
        if ent is not None:
            s, e = ent["span_original"]
            return {"kind": kind, "orig_start": s, "orig_end": e, "repl": "",
                    "a_text": "", "target_text": ent["text"],
                    "detail": f"drop：整实体删 {ent['type']}「{ent['text']}」@[{s},{e})"}
        p = _find_pos(text, occupied, rng)  # 无实体：字符级退化
        if p is None:
            return None
        return {"kind": kind, "orig_start": p, "orig_end": p + 1, "repl": "",
                "a_text": "", "target_text": None,
                "detail": f"drop（字符退化）：删位置 {p} 字符 {text[p]!r}"}

    if kind == "drift":
        mode = rng.choice(("truncate", "extend"))
        ent = _pick_entity(ents, occupied, rng, lambda s, e: (e - 1, e), len(text))
        if ent is not None:
            s, e = ent["span_original"]
            if mode == "truncate":
                return {"kind": kind, "orig_start": e - 1, "orig_end": e, "repl": "",
                        "a_text": ent["text"][:-1], "target_text": ent["text"],
                        "detail": f"drift/truncate：实体「{ent['text']}」尾字符截断 @[{e - 1},{e})"}
            raw_ch = text[e - 1]
            return {"kind": kind, "orig_start": e - 1, "orig_end": e, "repl": raw_ch * 2,
                    "a_text": ent["text"] + ent["text"][-1], "target_text": ent["text"],
                    "detail": f"drift/extend：实体「{ent['text']}」尾字符复制延伸 @[{e - 1},{e})"}
        p = _find_pos(text, occupied, rng)
        if p is None:
            return None
        if mode == "truncate":
            return {"kind": kind, "orig_start": p, "orig_end": p + 1, "repl": "",
                    "a_text": "", "target_text": None,
                    "detail": f"drift/truncate（字符退化）：删位置 {p}"}
        return {"kind": kind, "orig_start": p, "orig_end": p + 1, "repl": text[p] * 2,
                "a_text": text[p], "target_text": None,
                "detail": f"drift/extend（字符退化）：位置 {p} 字符复制"}

    if kind == "type_flip":
        # 逐实体（RNG 序）找可用翻转位：只占实际改动的那 1 个字符位
        order = list(ents)
        rng.shuffle(order)
        for ent in order:
            for q, d, flipped, _true in _entity_flips(text, ent, face):
                if _free(occupied, q, q + 1):
                    return {"kind": kind, "orig_start": q, "orig_end": q + 1, "repl": d,
                            "a_text": flipped, "target_text": ent["text"],
                            "detail": f"type_flip：{ent['type']}「{ent['text']}」@{q} 换 {d!r}"
                                      f" → 翻转读数「{flipped}」"}
            p = _find_pos(text, occupied, rng,
                          pred=lambda i: ent["span_original"][0] <= i < ent["span_original"][1])
            if p is not None:  # 无结构位：实体内普通替换（如实标注降级）
                return {"kind": kind, "orig_start": p, "orig_end": p + 1, "repl": _sub_char(text[p]),
                        "a_text": ent["text"].replace(text[p], _sub_char(text[p]), 1),
                        "target_text": ent["text"],
                        "detail": f"type_flip（降级替换）：{ent['type']}「{ent['text']}」@{p}"}
        p = _find_pos(text, occupied, rng)  # 无实体：字符级退化
        if p is None:
            return None
        return {"kind": kind, "orig_start": p, "orig_end": p + 1, "repl": _sub_char(text[p]),
                "a_text": _sub_char(text[p]), "target_text": None,
                "detail": f"type_flip（字符退化）：位置 {p} 替换为 {_sub_char(text[p])!r}"}

    # swap：字符替换（brief 口径「字符替换」；优先打在实体内的数字位上）
    order = list(ents)
    rng.shuffle(order)
    for ent in order:
        p = _find_pos(text, occupied, rng,
                      pred=lambda i: ent["span_original"][0] <= i < ent["span_original"][1]
                      and text[i].isdigit())
        if p is not None:
            d = _sub_char(text[p])
            return {"kind": kind, "orig_start": p, "orig_end": p + 1, "repl": d,
                    "a_text": ent["text"].replace(text[p], d, 1), "target_text": ent["text"],
                    "detail": f"swap：{ent['type']}「{ent['text']}」@{p} 换 {d!r}"}
    p = _find_pos(text, occupied, rng)
    if p is None:
        return None
    return {"kind": kind, "orig_start": p, "orig_end": p + 1, "repl": _sub_char(text[p]),
            "a_text": _sub_char(text[p]), "target_text": None,
            "detail": f"swap：位置 {p} 字符 {text[p]!r} 替换为 {_sub_char(text[p])!r}"}


def _arbitrate_pair(text_a_raw: str, text_b_raw: str, page_type: str, ner) -> dict:
    """run_page 核心的离线复刻（无 IO、无落盘）：归一化 → 比对 → 实体 → 仲裁。

    md 恒缺席（A2 检验的是 compare+arbitrate 自身对转录层分歧的显性化能力）。
    """
    face_a = FaceMap.from_raw(text_a_raw)
    face_b = FaceMap.from_raw(text_b_raw)
    cmp_result = compare_transcripts(face_a.norm, face_b.norm, page_type)
    ents_a = merge_entities(extract_regex(face_a.norm, face_a),
                            extract_ner(face_a.norm, ner, face_a))
    ents_b = merge_entities(extract_regex(face_b.norm, face_b),
                            extract_ner(face_b.norm, ner, face_b))
    return arbitrate_page(cmp_result, ents_a, ents_b, None, page_type)


def _overlap(inst: dict, lesion: dict) -> bool:
    """实体 span_original（病变转录域）与病灶区间相交。"""
    s0, s1 = inst["span_original"]
    return s0 < lesion["end"] and lesion["start"] < s1


def _lesion_detected(lesion: dict, arb: dict) -> tuple[bool, str]:
    """逐病灶检出判定（判据见模块 docstring），返回 (是否检出, 依据描述)。"""
    disputed = arb.get("disputed") or []
    if any("gap" in d for d in disputed):
        return True, "页级gap（整页升级，病灶随页显性）"
    for entry in disputed:
        rule = entry.get("rule", "?")
        for inst in (entry.get("candidates") or {}).get("a") or []:
            if _overlap(inst, lesion):
                return True, f"disputed/{rule}（a 面病变读数区间相交）"
        tgt = lesion.get("target_text")
        if tgt and any(inst.get("text") == tgt
                       for inst in (entry.get("candidates") or {}).get("b") or []):
            return True, f"disputed/{rule}（b 面原文读数涉案）"
    for entry in arb.get("auto_resolved") or []:
        rule, ent, src = entry.get("rule", "?"), entry.get("entity"), entry.get("source")
        if src == "a" and ent and _overlap(ent, lesion):
            return True, f"auto_resolved/{rule}（a 面病变读数被采信面改判）"
        tgt = lesion.get("target_text")
        if tgt and src == "b" and ent is not None and ent.get("text") == tgt:
            return True, f"auto_resolved/{rule}（b 面原文读数涉案）"
    return False, ""


def a2_gate(n_pages: int, n_lesions: int, n_detected: int) -> tuple[bool, list[str]]:
    """A2 门槛：检出率 ≥0.95，注毒页 ≥10、病灶合计 ≥50（逐字值）。"""
    reasons: list[str] = []
    if n_pages < A2_MIN_PAGES:
        reasons.append(f"注毒页 {n_pages} < {A2_MIN_PAGES}")
    if n_lesions < A2_MIN_LESIONS:
        reasons.append(f"病灶合计 {n_lesions} < {A2_MIN_LESIONS}")
    if n_lesions:
        rate = n_detected / n_lesions
        if rate < A2_THRESHOLD:
            reasons.append(f"检出率 {rate:.4f} < {A2_THRESHOLD}")
    return (not reasons and n_lesions > 0, reasons)


def verdict_line(a1_pass: bool, a2_pass: bool) -> str:
    """门禁引用的单行判定。"""
    return f"A1={'PASS' if a1_pass else 'FAIL'} A2={'PASS' if a2_pass else 'FAIL'}"


# ---- 报告盲区清单（A5 抽检设计必读；文案即裁定存档） --------------------------------

REPORT_BLIND_SPOTS = [
    "R2 正则代理窄误自动：R2 以「类型名 ∈ 正则通道键集」作通道归属证据，恰一名正则类型"
    "在场即自动采信。窄口在于：若 NER 通道把某读数误标成正则通道的类型名（如把机构名误标"
    "为「案号」），R2 会把这条 NER 误标读数当正则读数自动放行——假阳性进 GT。该盲区仅在"
    "「同读异型且正则类型名恰一名在场」的窄条件下触发，抽检（A5）应定向复核 arbitration=R2"
    " 且胜出类型 ∈ 正则通道、但 conflict 对侧为 NER 类型的实体。",
    "重复出现对集合比对不可见：compare 表格域与 per_type_pr 均按 (类型, 串) 集合比对，"
    "同串出现两次 vs 一次读数一致（页内去重口径）。同一实体串在页内确应出现两次而引擎只标"
    "一次（或反之）时，管线零争议、GT 静默少计/多计。抽检（A5）应对集合判 consistent 的"
    "表格页做同串出现次数人工核对。",
]


# ---- main：A1 + A2 + 报告 ----------------------------------------------------------

def _resolve_input(manifest_dir: Path, synthetic_dir: Path, rel: str) -> Path | None:
    """manifest 条目路径解析：先按 manifest 同目录，再按 --synthetic-dir。"""
    for cand in (manifest_dir / rel, synthetic_dir / rel, synthetic_dir / Path(rel).name):
        if cand.exists():
            return cand
    return None


def _run(args: argparse.Namespace) -> int:
    manifest_path = Path(args.manifest)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entries = [e for e in (manifest.get("files") or [])
               if "e2e" in (e.get("levels") or [])]  # 与 run_pipeline._run_batch 同款 levels 过滤
    base_dir = manifest_path.parent
    synthetic_dir = Path(args.synthetic_dir)
    work = Path(args.work)
    clients = parse_clients(args.clients)
    ner = build_ner(args.ner_base, args.ner)

    # ---- A1：逐文件逐页 run_page（转录缓存按文件复用，A2 零额外云调用） ----------
    packs_by_entry: dict[str, list[dict]] = {}
    wrapped_by_entry: dict[str, dict] = {}
    file_rows: list[dict] = []
    failed_pages: list[str] = []
    for entry in entries:
        entry_id = str(entry.get("id", entry.get("path", "?")))
        fpath = _resolve_input(base_dir, synthetic_dir, str(entry.get("path", "")))
        page_type = map_page_type(entry)
        n_pages = int(entry.get("pages") or 1)
        row = {"id": entry_id, "page_type": page_type, "pages": n_pages,
               "found": fpath is not None, "ok": 0, "failed": 0,
               "v6_pages": None, "vl_pages": None, "gt_pages": None}
        file_rows.append(row)
        if fpath is None:
            for page_no in range(n_pages):
                failed_pages.append(f"{entry_id}-p{page_no:03d}: 原件缺失")
                row["failed"] += 1
            continue
        wrapped = {key: CachedTranscriptionClient(c) for key, c in clients.items()}
        wrapped_by_entry[entry_id] = wrapped
        packs = []
        for page_no in range(n_pages):
            label = f"{entry_id}-p{page_no:03d}"
            try:
                packs.append(run_page(str(fpath), page_no, page_type, wrapped, ner, work,
                                      carrier=str(entry.get("carrier") or "scanned"),
                                      segment=str(args.segment)))
                row["ok"] += 1
            except Exception as e:  # 逐页失败隔离
                row["failed"] += 1
                failed_pages.append(f"{label}: {type(e).__name__}: {e}")
                print(f"[verify] 页失败 {label}: {type(e).__name__}: {e}", file=sys.stderr)
        packs_by_entry[entry_id] = packs
        row["v6_pages"] = len(_safe_transcribe(wrapped["a"], str(fpath)))
        row["vl_pages"] = len(_safe_transcribe(wrapped["b"], str(fpath)))
        gt_path = _resolve_input(base_dir, synthetic_dir, str(entry.get("gt") or ""))
        if gt_path is not None:
            try:
                row["gt_pages"] = len(json.loads(gt_path.read_text(encoding="utf-8")).get("pages") or [])
            except (ValueError, OSError):
                row["gt_pages"] = None

    # ---- A1 指标：records 层合并全部文件 → 一次 compute_metrics（同口径） --------
    all_records: list[dict] = []
    composition: dict[tuple[str, str], int] = {}
    for entry in entries:
        entry_id = str(entry.get("id", entry.get("path", "?")))
        gt_path = _resolve_input(base_dir, synthetic_dir, str(entry.get("gt") or ""))
        if gt_path is None:
            continue
        gt_json = json.loads(gt_path.read_text(encoding="utf-8"))
        page_type = map_page_type(entry)
        for p in gt_json.get("pages") or []:
            for etype, vals in (p.get("entities") or {}).items():
                key = (page_type, etype)
                composition[key] = composition.get(key, 0) + len(set(vals))
        all_records.extend(_match_records(packs_by_entry.get(entry_id) or [], gt_json))
    metrics = _nerq.compute_metrics(all_records)
    a1_pass, a1_failures = a1_gate(metrics)

    # ---- A2：选 ≥10 个有正则实体的 v6 页，各植 5 病灶 → 重跑 compare+arbitrate ----
    a2_rows: list[dict] = []
    page_seq = 0
    for entry in entries:
        entry_id = str(entry.get("id", entry.get("path", "?")))
        wrapped = wrapped_by_entry.get(entry_id)
        if wrapped is None:
            continue
        fpath = _resolve_input(base_dir, synthetic_dir, str(entry.get("path", "")))
        if fpath is None:
            continue
        page_type = map_page_type(entry)
        pages_a = _safe_transcribe(wrapped["a"], str(fpath))
        for page_no in range(int(entry.get("pages") or 1)):
            if page_no >= len(pages_a):
                break  # 转录页数不足（页数对账在报告披露），可切片页之外无从注毒
            v6_text = _pick_page(pages_a, page_no, "a(v6)").get("text_raw") or ""
            if not v6_text:
                continue
            face = FaceMap.from_raw(v6_text)
            if A2_PREFER_ENTITY_PAGES and not extract_regex(face.norm, face):
                continue  # 优先有实体的页（见 A2_PREFER_ENTITY_PAGES 说明）
            page_seq += 1
            dis, lesions = inject_disputes(v6_text, A2_PER_PAGE,
                                           seed=args.seed + page_seq)
            arb = _arbitrate_pair(dis, v6_text, page_type, ner)
            for lesion in lesions:
                detected, basis = _lesion_detected(lesion, arb)
                a2_rows.append({"page_id": f"{entry_id}-p{page_no:03d}", **lesion,
                                "detected": detected, "basis": basis})
    n_detected = sum(1 for r in a2_rows if r["detected"])
    a2_pass, a2_failures = a2_gate(len({r["page_id"] for r in a2_rows}),
                                   len(a2_rows), n_detected)

    # ---- 报告 -------------------------------------------------------------------
    line = verdict_line(a1_pass, a2_pass)
    if args.report:
        report_path = work / "verify" / f"A1A2-{date.today().isoformat()}.md"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(_render_report(
            args, entries, file_rows, failed_pages, composition, metrics,
            a1_pass, a1_failures, a2_rows, n_detected, a2_pass, a2_failures, line),
            encoding="utf-8")
        print(f"[verify] 报告 → {report_path}")
    print(line)  # 单行判定恒为 stdout 末行（门禁引用口径）
    return 0 if (a1_pass and a2_pass) else 1


def _safe_transcribe(client, file_path: str) -> list:
    """转录（缓存命中零开销）；失败返回空列表（页数对账记 None 场景）。"""
    try:
        return client.transcribe(file_path) or []
    except Exception:
        return []


def _render_report(args, entries, file_rows, failed_pages, composition, metrics,
                   a1_pass, a1_failures, a2_rows, n_detected, a2_pass, a2_failures,
                   line) -> str:
    """A1/A2 报告（工作目录，不入仓；零 token、零凭据）。"""
    today = date.today().isoformat()
    out: list[str] = [f"# 合成集验证闸 A1/A2 报告（{today}）", "",
                      f"- manifest：`{args.manifest}`（e2e 条目 {len(entries)} 个）",
                      f"- synthetic-dir：`{args.synthetic_dir}`",
                      f"- clients：`{args.clients}`；NER：`{args.ner_base or 'off'}`；seed：{args.seed}",
                      "- 云凭据：仅经环境变量 CLOUD_VL_TOKEN（本报告零凭据）", ""]

    out += ["## 合成集构成（页型 × 实体类型 × GT 量）", "",
            "GT 侧口径 = 页内去重后计数（与 P/R 集合口径一致）。", "",
            "| 页型 | 实体类型 | GT 实体数 |", "|---|---|---|"]
    for (page_type, etype), count in sorted(composition.items()):
        out.append(f"| {page_type} | {etype} | {count} |")
    out += ["", "| 文件 | 页型 | manifest 页 | GT 页 | v6 转录页 | VL 转录页 | pack 成功 | 页失败 |",
            "|---|---|---|---|---|---|---|---|"]
    for row in file_rows:
        out.append(f"| {row['id']} | {row['page_type']} | {row['pages']} | "
                   f"{row['gt_pages'] if row['gt_pages'] is not None else '—'} | "
                   f"{row['v6_pages'] if row['v6_pages'] is not None else '—'} | "
                   f"{row['vl_pages'] if row['vl_pages'] is not None else '—'} | "
                   f"{row['ok']} | {row['failed']} |")
    if failed_pages:
        out += ["", "失败/缺席页明细："] + [f"- {f}" for f in failed_pages]
    out.append("")

    # ---- A1 ----
    out += [f"## A1 分类型 P/R（门槛：总体与 GT≥{PER_TYPE_MIN_GT} 的类型 P、R ≥ {PR_THRESHOLD}）", "",
            "| 类型 | GT 实体数 | P | R | tp | fp | fn | 分类型门槛 | 判定 |",
            "|---|---|---|---|---|---|---|---|---|"]
    ov = metrics["overall"]
    out.append(f"| **总体** | {ov['tp'] + ov['fn']} | {ov['precision']:.4f} | {ov['recall']:.4f} "
               f"| {ov['tp']} | {ov['fp']} | {ov['fn']} | — | "
               f"{'✅' if ov['precision'] >= PR_THRESHOLD and ov['recall'] >= PR_THRESHOLD else '❌'} |")
    for etype, s in metrics["per_type"].items():
        gt_n = s["tp"] + s["fn"]
        gated = gt_n >= PER_TYPE_MIN_GT
        ok = (not gated) or (s["precision"] >= PR_THRESHOLD and s["recall"] >= PR_THRESHOLD)
        out.append(f"| {etype} | {gt_n} | {s['precision']:.4f} | {s['recall']:.4f} "
                   f"| {s['tp']} | {s['fp']} | {s['fn']} | "
                   f"{'判' if gated else '豁免（GT<5）'} | {'✅' if ok else '❌'} |")
    out += ["", f"A1 失败项：{'；'.join(a1_failures) if a1_failures else '（无）'}", ""]

    # ---- A2 ----
    n_pages_a2 = len({r["page_id"] for r in a2_rows})
    rate = n_detected / len(a2_rows) if a2_rows else 0.0
    out += [f"## A2 注入检验（门槛：检出率 ≥ {A2_THRESHOLD}，注毒页 ≥ {A2_MIN_PAGES}，"
            f"病灶 ≥ {A2_MIN_LESIONS}；注入面=v6，对照面=同页干净 v6）", "",
            f"注毒页 {n_pages_a2}，病灶 {len(a2_rows)}，检出 {n_detected}，检出率 {rate:.4f}", "",
            "| 页 | kind | 病变域坐标 | 原域坐标 | target_text | 检出 | 依据 |",
            "|---|---|---|---|---|---|---|"]
    for r in a2_rows:
        tgt = r["target_text"] or "—"
        out.append(f"| {r['page_id']} | {r['kind']} | [{r['start']},{r['end']}) "
                   f"| [{r['orig_start']},{r['orig_end']}) | {tgt} "
                   f"| {'✅' if r['detected'] else '❌'} | {r['basis'] or '未检出'} |")
    out += ["", f"A2 失败项：{'；'.join(a2_failures) if a2_failures else '（无）'}", ""]

    out += ["## 盲区清单（A5 抽检设计必读）", ""]
    out += [f"{i}. {spot}" for i, spot in enumerate(REPORT_BLIND_SPOTS, 1)]
    out += ["", "## 单行判定", "", f"```", line, "```", ""]
    return "\n".join(out)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="verify_engine",
        description="合成集验证闸 A1/A2（Issue#56 M1 出口）——需 CLOUD_VL_TOKEN")
    parser.add_argument("--synthetic-dir", required=True, help="合成集目录")
    parser.add_argument("--manifest", required=True, help="manifest.json 路径")
    parser.add_argument("--work", required=True, help="GT 工作目录（pack 与报告落盘根）")
    parser.add_argument("--clients", default="cloud:PP-OCRv6,cloud:PaddleOCR-VL",
                        help="转录客户端 spec（同 run_pipeline，双云必选）")
    ner_group = parser.add_mutually_exclusive_group()
    ner_group.add_argument("--ner-base", default=None, help="NER 端点 URL（启用 NER 通道）")
    ner_group.add_argument("--ner", choices=["off"], default=None, help="--ner off 关闭 NER 通道")
    parser.add_argument("--segment", default="first", help="卷内段位（默认 first）")
    parser.add_argument("--seed", type=int, default=56, help="A2 注毒随机种子（默认 56，确定性）")
    parser.add_argument("--report", action="store_true",
                        help="写报告 {work}/verify/A1A2-<日期>.md")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """CLI 入口：单行判定打印 stdout；exit 0 = A1、A2 双过，1 = 任一未过/出错。"""
    args = _parse_args(argv)
    try:
        return _run(args)
    except Exception as e:  # CLI 边界：错误进 stderr、非零退出
        print(f"[verify] 错误: {type(e).__name__}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
