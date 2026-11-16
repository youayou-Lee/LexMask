"""实体框质量净化 pass（Issue#308）。

视觉线（OCR+HaS）识别结果的五个纯函数 pass，分两层：
- L1 值级（匹配前，动 entities）：2b 编号/金额核心提取、2c 碎片值过滤；
- L2 框级（匹配后，动 regions）：① 同值跨类型消解、④ 断行拆分并值、
  ③ 页脚页码过滤、⑤ 页眉页码过滤（#335）、⑥ 无锚裸数字闸（#335）。

设计约束（底稿：LexMask-workspace/github-issues/2026-11-15-issue308-box-quality-polish.md）：
- 全部 pass 同页作用域（跨页同值是合法重复），跨页永不合并；
- source=visual_features（LA/视觉特征框）不参与值判据（0710 指纹实证保护）；
- ③ 页面尺寸不可得（0,0）时 fail-open 不丢框；
- ④ 拆分对「并值不并框」——union 会被真卷像素证据否决（罩住两整行无辜文字）。

各 pass 只收缩不扩张：消解只对同值重叠对、碎片过滤只对数字空的家族值、
页脚过滤四条件合取、并值只改 text 不动几何。误杀面的反向护栏由
test_issue308_box_quality_pass.py 的负样本钉守住。
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

logger = logging.getLogger(__name__)

if TYPE_CHECKING:  # 循环依赖规避：主模块顶层 import 本模块，此处只留类型视图
    from app.services.ocr_has_vision_service import SensitiveRegion

# 家族类型：编号形态（值应有数字/编号结构）+ 金额。家族外的类型
# （PERSON/ORG 等）永不参与值级改写与碎片过滤——没有形状先验，防误修剪。
NUMBER_SHAPED_TYPES = frozenset({"LEGAL_CASE_ID", "CASE_NUMBER", "DOCUMENT_NUMBER"})
AMOUNT_TYPE = "AMOUNT"
_QUALITY_PASS_TYPES = NUMBER_SHAPED_TYPES | {AMOUNT_TYPE}

# 编号核心形态：与 preset LEGAL_CASE_ID 正则的命名组核心同构
# （（YYYY）起、号收尾，允许编号内部的空白/年份后缀）。
CASE_ID_CORE_RE = re.compile(r"[（(]\s*\d{4}\s*[）)][\s一-龥0-9]{0,26}号")
# ④ 拼接完整性判据：两半拼起来的全文必须**恰好**是一个编号（防误合并）。
_CASE_ID_CORE_FULL_RE = re.compile(r"^[（(]\s*\d{4}\s*[）)][\s一-龥0-9]{0,26}号$")
# ④ 左半资格：含（YYYY）开头但没走到「号」收尾（被 OCR 行断打断）。
_CASE_ID_HEAD_PREFIX_RE = re.compile(r"[（(]\s*\d{4}\s*[）)][:：]?\s*[\s一-龥0-9]{0,26}$")

# 金额核心：数字游程（含千分位/小数分隔与中文数字）+ 可选万亿级 + 币值单位
# （+ 可选「整」）。真金额必有单位锚或本身即纯数字——两者之外的值不动。
_AMOUNT_AMOUNT_DIGIT = "0-9０-９〇零一二三四五六七八九十百千万壹贰叁肆伍陆柒捌玖拾佰仟两"
AMOUNT_CORE_RE = re.compile(
    f"[{_AMOUNT_AMOUNT_DIGIT}][{_AMOUNT_AMOUNT_DIGIT},，.．]*\\s*(?:[万亿]\\s*)?[元圆块钱]整?"
)
# 金额无单位可锚时的尾锚：叙述性吞句的尾部数字游程（p4 基线实证形态）。
_AMOUNT_TRAILING_RUN_RE = re.compile(r"[0-9０-９][0-9０-９,，.．]*$")

# 中文数字字符集（2c 碎片判定：家族值必须含阿拉伯或中文数字，否则是碎片）。
_CHINESE_NUMERIC_CHARS = frozenset("〇零一二两三四五六七八九十百千万亿壹贰叁肆伍陆柒捌玖拾佰仟兆")

# ① 消解时的主类型优先级。法律文书编号形态值存活为 LEGAL_CASE_ID/
# DOCUMENT_NUMBER（产品语义：法律编号归法律类型）；非编号形态值
# （纯数字业务编号等）回退业务类型优先——CASE_NUMBER 在前（门⑥ I-1）。
_PREFERRED_CODE_TYPES = ("LEGAL_CASE_ID", "DOCUMENT_NUMBER")
_CODE_SHAPED_MASTER_PRIORITY = _PREFERRED_CODE_TYPES + ("CASE_NUMBER",)
_NON_CODE_MASTER_PRIORITY = ("CASE_NUMBER",) + _PREFERRED_CODE_TYPES
# ① 重叠判据：交集面积 / 较小框面积（同值 + 明显重叠才算同一处出现）。
_SAME_VALUE_OVERLAP_RATIO = 0.3
# ③ 页脚带：页底 8%；宽度 ≤ 5% 页宽（孤立短数字的几何侧证据）。
_FOOTER_TOP_FRACTION = 0.92
_FOOTER_MAX_WIDTH_FRACTION = 0.05
_FOOTER_MAX_DIGITS = 3
_FOOTER_DROP_TYPES = _QUALITY_PASS_TYPES

# ⑤ 页眉带（#335）：页顶 8%，与页脚带对称——页码可能在页首。
_HEADER_BOTTOM_FRACTION = 0.08

# ⑥ 无锚裸数字闸（#335）：AGE / CASE_NUMBER 的裸短纯数字是误打
# （8 页样本实证：AGE 18 / CASE_NUMBER 6，仅 1 个在页脚带内）。
# 拍板（2026-11-17）：裸数字直接丢框；CASE_NUMBER 丢弃线 ≤4 位
# （5 位以上可能是发票号/流水号，保留）。
_BARE_NUMBER_DROP_TYPES = frozenset({"AGE", "CASE_NUMBER"})
_AGE_MAX_BARE_DIGITS = 3
_CASE_NUMBER_MAX_BARE_DIGITS = 4
# CASE_NUMBER 编号结构锚：字母前缀 / （YYYY）括号年 / 连字符。
# 有任一锚即真编号形状；全无锚才进裸数字判定。
_CASE_NUMBER_ANCHOR_RE = re.compile(r"[A-Za-z]|[（(]\s*\d{4}\s*[）)]|[-–—]")
# 裸数字形状：只剩数字与千分位/小数分隔残段（`500`、`0. 00`、
# `500, 0. 00`）。含 CJK（`第3页`、`100号`）不是裸数字形状，不进射程。
_BARE_VALUE_SHAPE_RE = re.compile(r"^[\d,，.．、\s\u3000]+$")


def _has_numeric_content(text: str) -> bool:
    return any(ch.isdigit() or ch in _CHINESE_NUMERIC_CHARS for ch in text)


def _strip_ws(text: str) -> str:
    return re.sub(r"[\s\u3000]+", "", text or "")


# ---------------------------------------------------------------------------
# L1 值级 pass（entities: [{"type": ..., "text": ...}, ...]）
# ---------------------------------------------------------------------------

def normalize_number_shaped_entities(entities: list[dict]) -> list[dict]:
    """2b：家族类型值内嵌编号/金额核心时收缩为核心（前后缀叙述不入实体）。

    编号类：取值内 (YYYY)…号 核心（`防城区法院作出（2021）…` /
    `[案号（2023）…]` 括号包裹形态）；金额类：优先单位锚定，无单位时
    尾部数字游程（吞整句框 `……账户内的201280`）。核心==全值时原样返回。
    """
    out: list[dict] = []
    for ent in entities:
        etype = str(ent.get("type") or "")
        text = str(ent.get("text") or "")
        if etype in NUMBER_SHAPED_TYPES and text:
            m = CASE_ID_CORE_RE.search(text)
            if m and m.group(0) != text:
                text = m.group(0)
        elif etype == AMOUNT_TYPE and text:
            m = AMOUNT_CORE_RE.search(text)
            if m:
                if m.group(0) != text:
                    text = m.group(0)
            else:
                m2 = _AMOUNT_TRAILING_RUN_RE.search(text)
                if m2 and m2.group(0) != text:
                    text = m2.group(0)
        out.append({**ent, "text": text})
    return out


def drop_fragment_entities(entities: list[dict]) -> list[dict]:
    """2c：家族类型里数字全空的值是碎片（`事调解书》`、吞整句无数字金额）。

    真编号/真金额不可能没有任何数字（含中文大写）——数字空即 HaS 把叙述
    文字误标成实体。家族外类型不参与。
    """
    kept: list[dict] = []
    for ent in entities:
        etype = str(ent.get("type") or "")
        if etype in _QUALITY_PASS_TYPES and not _has_numeric_content(str(ent.get("text") or "")):
            continue
        kept.append(ent)
    return kept


# ---------------------------------------------------------------------------
# L2 框级 pass（regions: list[SensitiveRegion]，页内像素坐标）
# ---------------------------------------------------------------------------

def _overlap_ratio(a: SensitiveRegion, b: SensitiveRegion) -> float:
    ix = max(0, min(a.left + a.width, b.left + b.width) - max(a.left, b.left))
    iy = max(0, min(a.top + a.height, b.top + b.height) - max(a.top, b.top))
    inter = ix * iy
    if inter <= 0:
        return 0.0
    smaller = min(a.width * a.height, b.width * b.height)
    if smaller <= 0:
        return 0.0
    return inter / smaller


def _master_rank(region: SensitiveRegion, value: str) -> tuple:
    """连通分量的存活排序键：编号形态值法律编号类型优先；非编号形态值
    业务类型（CASE_NUMBER）优先（门⑥ I-1：与设计稿 §L2① 一致）；
    同优先级取面积更大者（遮盖面永不收缩），最后按几何键定全序。"""
    etype = region.entity_type
    code_shaped = bool(CASE_ID_CORE_RE.search(value)) or value.rstrip().endswith("号")
    priority = _CODE_SHAPED_MASTER_PRIORITY if code_shaped else _NON_CODE_MASTER_PRIORITY
    try:
        pri = priority.index(etype)
    except ValueError:
        pri = len(priority)
    return (pri, -(region.width * region.height), region.top, region.left)


def dedupe_same_value_regions(regions: list[SensitiveRegion]) -> list[SensitiveRegion]:
    """①：同页 + 去空白同值 + 重叠的框是同一处出现 → 每个重叠簇存活一框。

    **只在家族类型内消解**（编号类+AMOUNT，#308 点名的双框形态
    LEGAL_CASE_ID+CASE_NUMBER）——家族外跨类型同值重叠是合法共存
    （COMPANY_NAME×WORK_UNIT 同文双框；0710 指纹判例：不同类型=不同
    实体），消解它们是预览栈实测抓出的越界（ORG 10→0/WORK_UNIT 74→2）。
    p2 ×4 处合法出现（几何分离）全保留；多行值逐行矩形（同文不同行、
    互不重叠）不互消解；visual_features（LA/指纹类）不参与。
    同类型重叠的防御路径取面积更大者（与下游 IoU 去重「大者存活」同哲学）。
    """
    if len(regions) <= 1:
        return list(regions)
    # 分组键只含值（去空白）——跨类型同值双份正是要消解的对象。
    groups: dict[str, list[int]] = {}
    for idx, r in enumerate(regions):
        if r.source in ("visual_features", "manual"):
            continue  # 值判据对视觉特征框无意义（指纹/手写双实体判例）；人工框永存（#280）
        if r.entity_type not in _QUALITY_PASS_TYPES:
            continue  # 家族外类型不参与（0710 判例 + 预览栈越界实证）
        groups.setdefault(_strip_ws(r.text), []).append(idx)
    drop: set[int] = set()
    for idxs in groups.values():
        if len(idxs) < 2:
            continue
        # 重叠连通分量：每簇只留 master
        adj: dict[int, set[int]] = {i: set() for i in idxs}
        for a in range(len(idxs)):
            for b in range(a + 1, len(idxs)):
                ra, rb = regions[idxs[a]], regions[idxs[b]]
                if _overlap_ratio(ra, rb) >= _SAME_VALUE_OVERLAP_RATIO:
                    adj[idxs[a]].add(idxs[b])
                    adj[idxs[b]].add(idxs[a])
        seen: set[int] = set()
        for start in idxs:
            if start in seen:
                continue
            comp: list[int] = []
            stack = [start]
            seen.add(start)
            while stack:
                cur = stack.pop()
                comp.append(cur)
                for nxt in adj[cur]:
                    if nxt not in seen:
                        seen.add(nxt)
                        stack.append(nxt)
            if len(comp) < 2:
                continue
            value = _strip_ws(regions[comp[0]].text)
            master = min(comp, key=lambda i: _master_rank(regions[i], value))
            drop.update(i for i in comp if i != master)
    if not drop:
        return list(regions)
    return [r for i, r in enumerate(regions) if i not in drop]


def merge_wrapped_number_regions(regions: list[SensitiveRegion]) -> list[SensitiveRegion]:
    """④：OCR 行断把同一编号拆成「行尾头半 + 次行尾半」两框 → 并值不并框。

    触发条件合取：同类型、上框含（YYYY）编号头但未到「号」收尾、两框垂直
    相邻（行距容差内）、**拼接全文恰好是一个完整编号**（防误合并的关键
    判据）。动作：两框 text 均改为拼接完整值，几何原样（union 会罩住两
    整行无辜文字——真卷 p4 像素证据）。跨页在单页管线内结构上不可能。
    """
    if len(regions) < 2:
        return list(regions)
    # 克隆走 r.__class__（本模块对 SensitiveRegion 只有 TYPE_CHECKING 视图，
    # 与 split_regions_across_lines 的克隆手法一致）。
    out = [r.__class__(
        text=r.text, entity_type=r.entity_type, left=r.left, top=r.top,
        width=r.width, height=r.height, confidence=r.confidence,
        source=r.source, color=r.color,
    ) for r in regions]
    order = sorted(range(len(out)), key=lambda i: (out[i].top, out[i].left))
    consumed: set[int] = set()
    merged = 0
    for pos in range(len(order)):
        ai = order[pos]
        a = out[ai]
        if ai in consumed:
            continue
        if a.entity_type not in NUMBER_SHAPED_TYPES:
            continue
        head = a.text.strip()
        if not head or len(head) < 2:
            continue
        if not _CASE_ID_HEAD_PREFIX_RE.search(head) or head.rstrip().endswith("号"):
            continue
        if CASE_ID_CORE_RE.search(head):
            # 头半已含完整编号核心（核心+叙述后缀形态）——它是完整值而非
            # 断裂头半，拿去拼接会产出垃圾并值（门⑥ M-3）
            continue
        # 向后扫描（y 序）首个未消费同类型候选——两半之间允许夹其他区域
        for q in range(pos + 1, len(order)):
            bi = order[q]
            b = out[bi]
            if bi in consumed or b.entity_type != a.entity_type:
                continue
            # 垂直相邻（先于长度检查）：y 序扫描中超出容差带的候选，其后的
            # 同类型候选只会更远 → 直接终止；带内候选不合尾半形状才继续找。
            if b.top < a.top + a.height - max(a.height, b.height):
                break
            gap = b.top - (a.top + a.height)
            if gap > max(a.height, b.height) * 1.5:
                break
            tail = b.text.strip()
            if not tail or len(tail) >= len(head):
                continue  # 带内但不成尾半，看带内下一个候选
            concat = head + tail
            if not _CASE_ID_CORE_FULL_RE.match(concat):
                continue  # 该候选不成尾半，继续找下一个（互不干扰）
            a.text = concat
            b.text = concat
            consumed.update((ai, bi))
            merged += 1
            break
    if merged:
        logger.info(
            "box_quality_pass: merged %d wrapped number pair(s) (value-level, rects kept)", merged
        )
    return out


def drop_footer_pagenumber_regions(
    regions: list[SensitiveRegion],
    page_width: int,
    page_height: int,
) -> list[SensitiveRegion]:
    """③：页脚带的孤立纯短数字（页码）不是实体 → 丢框。

    四条件合取：家族类型 + 值纯数字 ≤3 位 + 框顶进入页底 8% 带 + 框宽
    ≤5% 页宽。完整案号（含非数字字符）天然不触发；页面尺寸不可得（0,0，
    文本层线）时 fail-open 不丢框。
    """
    if page_height <= 0 or page_width <= 0:
        return list(regions)
    footer_top = page_height * _FOOTER_TOP_FRACTION
    max_width = page_width * _FOOTER_MAX_WIDTH_FRACTION
    kept: list[SensitiveRegion] = []
    dropped = 0
    for r in regions:
        value = (r.text or "").strip()
        if (
            r.entity_type in _FOOTER_DROP_TYPES
            and re.fullmatch(rf"[0-9]{{1,{_FOOTER_MAX_DIGITS}}}", value)
            and r.top >= footer_top
            and r.width <= max_width
        ):
            dropped += 1
            continue
        kept.append(r)
    if dropped:
        logger.info(
            "box_quality_pass: dropped %d footer page-number box(es)", dropped
        )
    return kept


def drop_header_pagenumber_regions(
    regions: list[SensitiveRegion],
    page_width: int,
    page_height: int,
) -> list[SensitiveRegion]:
    """⑤（#335）：页眉带的孤立纯短数字（页码）不是实体 → 丢框。

    与 ③ 页脚带对称（#308 只拦页底 8% 带，页眉页码穿透）：四条件合取
    同构——家族类型 + 值纯数字 ≤3 位 + 框顶进入页顶 8% 带 + 框宽
    ≤5% 页宽。首页页眉的完整案号（含非数字字符）天然不触发；页面尺寸
    不可得（0,0）时 fail-open 不丢框。
    """
    if page_height <= 0 or page_width <= 0:
        return list(regions)
    header_bottom = page_height * _HEADER_BOTTOM_FRACTION
    max_width = page_width * _FOOTER_MAX_WIDTH_FRACTION
    kept: list[SensitiveRegion] = []
    dropped = 0
    for r in regions:
        value = (r.text or "").strip()
        if (
            r.entity_type in _FOOTER_DROP_TYPES
            and re.fullmatch(rf"[0-9]{{1,{_FOOTER_MAX_DIGITS}}}", value)
            and r.top <= header_bottom
            and r.width <= max_width
        ):
            dropped += 1
            continue
        kept.append(r)
    if dropped:
        logger.info(
            "box_quality_pass: dropped %d header page-number box(es)", dropped
        )
    return kept


def drop_unanchored_number_regions(regions: list[SensitiveRegion]) -> list[SensitiveRegion]:
    """⑥（#335）：无锚裸数字不是实体 → 丢框（值形状闸，与几何无关）。

    射程与判据（拍板 2026-11-17）：
    - `AGE`：值是 1–3 位纯数字（无「岁」锚）→ 丢。表格年龄列的裸数字
      （8 页样本 `'41'` 同值 ×12）是 HaS 把「数字+单位」示例泛化成了裸数；
      带「岁/周岁」的真值、中文数字、4 位以上（年份残段）不进射程。
    - `CASE_NUMBER`：值无任何编号结构锚（字母 / （YYYY）/ 连字符）**且**
      是纯数字+分隔符形状，且数字 ≤4 位**或**含千分位/小数分隔残段
      （`500`、`0. 00`、`500, 0. 00`）→ 丢。5 位以上无结构纯数字可能是
      发票号/流水号，保留（漏打面趋零）。

    source=visual_features 不参与值判据（0710 指纹实证保护，模块约束）；
    manual 框永存（#280）。AMOUNT 裸数字是合法金额真值，不在射程。
    """
    kept: list[SensitiveRegion] = []
    dropped_by_type: dict[str, int] = {}
    for r in regions:
        if r.source in ("visual_features", "manual"):
            kept.append(r)
            continue
        if r.entity_type not in _BARE_NUMBER_DROP_TYPES:
            kept.append(r)
            continue
        value = _strip_ws(r.text)
        dropped = False
        if r.entity_type == "AGE":
            # 纯数字 fullmatch 通过即「无岁锚」（数字值不可能含「岁」）；
            # `41 岁` 经 _strip_ws 成 `41岁`，fullmatch 失败 → 真值保留。
            dropped = bool(re.fullmatch(rf"[0-9]{{1,{_AGE_MAX_BARE_DIGITS}}}", value))
        else:  # CASE_NUMBER
            digit_count = sum(ch.isdigit() for ch in value)
            has_separator = bool(re.search(r"[,，.．、]", value))
            dropped = (
                not _CASE_NUMBER_ANCHOR_RE.search(value)
                and bool(_BARE_VALUE_SHAPE_RE.match(value))
                and (digit_count <= _CASE_NUMBER_MAX_BARE_DIGITS or has_separator)
            )
        if dropped:
            dropped_by_type[r.entity_type] = dropped_by_type.get(r.entity_type, 0) + 1
            continue
        kept.append(r)
    if dropped_by_type:
        breakdown = ", ".join(f"{t}={n}" for t, n in sorted(dropped_by_type.items()))
        logger.info("box_quality_pass: dropped bare unanchored number box(es): %s", breakdown)
    return kept


def drop_edge_and_unanchored_number_regions(
    regions: list[SensitiveRegion],
    page_width: int,
    page_height: int,
) -> list[SensitiveRegion]:
    """③+⑤+⑥ 组合入口（#308 页脚 + #335 页眉 + #335 裸数字闸）。

    两条视觉线调用点（vision_service 文本层线与图像线）的单接线点：
    几何两闸在页面尺寸不可得（0,0）时 fail-open，值形状闸不受尺寸影响
    照常生效。
    """
    regions = drop_footer_pagenumber_regions(regions, page_width, page_height)
    regions = drop_header_pagenumber_regions(regions, page_width, page_height)
    return drop_unanchored_number_regions(regions)
