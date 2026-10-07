"""按页型分域转录比对（Issue#56 M1 / Task 4）。

GT 管线比对层：消费 Task 2 归一化产物（``FaceMap.norm``），按页型分三个
比对域裁决 GT 与转录方两侧的一致性，产出 verdict + 结构化争议供下游仲裁
消费（争议分支编号属 Task 5，本模块不引用）。

页型与分域（有效页型以 Task 6 ``gt_schema.PAGE_TYPES`` 为单一事实源，本模块
re-export——``body``/``table``/``seal_handwriting``/``edge``；未知页型直接
``ValueError``）：

- edge / 低密度域：``page_type == "edge"`` **或任一侧** 归一化长度 < 20 字
  （``LOW_DENSITY_CHARS``，内部低密度探测：短文本无结构可比）。该域严格
  全等比对：一致 → ``consistent``；任一差异均视为「单方多字」→
  ``single_side`` 争议（空页探针实证：空页 VL 吐水印字）。
- 表格域（``table`` 且两侧均 ≥ 20 字）：复用 Task 3 ``extract_regex`` 在
  **两侧归一化转录各配各的 FaceMap** 上抽实体（实体 ``text`` 即归一化面文本，
  仅用正则通道——NER 需端点、不在离线比对域），按 ``(type, text)`` 集合互含：
  全含 → ``consistent``；不全含 → ``set_mismatch`` 争议。双侧实体集皆空时
  集合域无信号，退回正文窗域裁决。
- 正文窗域（``body``；``seal_handwriting`` 同样以正文窗兜底——印章/手写页
  密时即散文文本）：双向滑窗覆盖率，两方向均 ≥ ``cov_threshold``（默认 0.9）
  → ``consistent``；任一方向不足 → ``low_coverage`` 争议。

格式残差自动放行：Task 2 只剥行首 ``#``，行中 ``#``/``##`` 在归一化面存活。
正文域两侧并非全等、但剥去 ``#`` 残差后全等（即双向覆盖率 100%）→
``auto_ok_format``（归一化后自动放行的格式级差异，非争议）。该判定先于
覆盖率阈值；残差之外尚有真实文本差异的仍按覆盖率裁决。edge/低密度域不做
残差放行——近空页上任何字符差异都值得记争议。

比对域声明（计划所称「复用 ``common_api.squash`` 域」的落地方式）：
``FaceMap.norm`` 已完成去空白（含全角空格/换行/制表），故滑窗直接在传入的
归一化串上运行、不再二次 squash；**调用方必须传归一化产物**——若传原始
转录，空白差异将计入窗口脱靶。表格域 ``coverage`` 字段为实体集合包含率
（空集按 1.0），其余域为滑窗覆盖率；该字段在表格域仅作参考，裁决依据是
集合互含。

窗口口径：``src`` 长度 ≤ ``win`` 时按整串包含计（短文本退化为包含判断），
否则按全部 ``win``-窗口命中比例计；``src`` 为空记 1.0。
"""
from __future__ import annotations

import re

from gt.entities import extract_regex
from gt.gt_schema import PAGE_TYPES  # 单一事实源 re-export（收编裁定见模块 docstring）
from gt.normalize import FaceMap

# 低密度阈值：任一侧归一化长度低于该值 → 按 edge/低密度域处理
LOW_DENSITY_CHARS = 20

# 行中格式残差：Task 2 仅剥行首 #，行中 #/## 存活；正文域自动放行判定用
_FMT_RESIDUE = re.compile(r"#+")

# 争议 detail 中样本截断长度（detail 保持单行可读）
_DETAIL_SAMPLE_CHARS = 24

CompareResult = dict


def _window_coverage(src: str, dst: str, win: int) -> float:
    """src 的 win-滑窗被 dst 包含的比例（src 空记 1.0；短串退化为整串包含）。"""
    if not src:
        return 1.0
    if len(src) <= win:
        return 1.0 if src in dst else 0.0
    windows = [src[i:i + win] for i in range(len(src) - win + 1)]
    hits = sum(1 for w in windows if w in dst)
    return hits / len(windows)


def _entity_set(text_norm: str) -> set[tuple[str, str]]:
    """表格域实体集合：Task 3 正则通道跑在归一化转录 + 各自 FaceMap 上。

    入参即归一化转录（实体 ``text`` 因此落在归一化面）；``FaceMap.from_raw``
    对已归一化串为恒等映射，仅为满足抽取器的双面 span 回填契约。
    """
    face_map = FaceMap.from_raw(text_norm)
    return {(e["type"], e["text"]) for e in extract_regex(face_map.norm, face_map)}


def _clip(sample: str) -> str:
    return sample if len(sample) <= _DETAIL_SAMPLE_CHARS else sample[:_DETAIL_SAMPLE_CHARS] + "…"


def _result(page_type: str, verdict: str, coverage: dict, disputes: list) -> CompareResult:
    return {"page_type": page_type, "verdict": verdict,
            "coverage": coverage, "disputes": disputes}


def compare_transcripts(a_norm: str, b_norm: str, page_type: str,
                        win: int = 8, cov_threshold: float = 0.9) -> CompareResult:
    """按页型分域比对两侧归一化转录，返回结构化裁决。

    返回 ``{"page_type", "verdict", "coverage", "disputes"}``；``verdict`` ∈
    ``consistent``（一致）/ ``dispute``（争议，``disputes`` 非空）/
    ``auto_ok_format``（归一化后自动放行的格式级差异）。``disputes`` 元素形如
    ``{"kind": "single_side"|"low_coverage"|"set_mismatch", "detail": str}``。
    """
    if page_type not in PAGE_TYPES:
        raise ValueError(f"未知页型 {page_type!r}，有效页型：{sorted(PAGE_TYPES)}")
    a_norm = a_norm or ""
    b_norm = b_norm or ""

    # ---- edge / 低密度域（页型 edge，或任一侧 < 20 字）：严格全等 ----
    if page_type == "edge" or len(a_norm) < LOW_DENSITY_CHARS or len(b_norm) < LOW_DENSITY_CHARS:
        coverage = {"a_in_b": _window_coverage(a_norm, b_norm, win),
                    "b_in_a": _window_coverage(b_norm, a_norm, win)}
        if a_norm == b_norm:
            return _result(page_type, "consistent", coverage, [])
        if not a_norm:
            detail = f"边缘/低密度页：b 侧单方多字 {len(b_norm)} 字（a 侧空），样本：{_clip(b_norm)}"
        elif not b_norm:
            detail = f"边缘/低密度页：a 侧单方多字 {len(a_norm)} 字（b 侧空），样本：{_clip(a_norm)}"
        else:
            detail = (f"边缘/低密度页：两侧文本不一致（a {len(a_norm)} 字 / b {len(b_norm)} 字），"
                      f"a 样本：{_clip(a_norm)}；b 样本：{_clip(b_norm)}")
        return _result(page_type, "dispute", coverage,
                       [{"kind": "single_side", "detail": detail}])

    # ---- 表格域：实体集合互含（双侧皆无实体则退回正文窗域） ----
    if page_type == "table":
        set_a = _entity_set(a_norm)
        set_b = _entity_set(b_norm)
        if set_a or set_b:
            shared = len(set_a & set_b)
            coverage = {"a_in_b": shared / len(set_a) if set_a else 1.0,
                        "b_in_a": shared / len(set_b) if set_b else 1.0}
            if set_a <= set_b and set_b <= set_a:
                return _result(page_type, "consistent", coverage, [])
            miss_in_b = sorted(f"{ty}={tx}" for ty, tx in set_a - set_b)
            miss_in_a = sorted(f"{ty}={tx}" for ty, tx in set_b - set_a)
            parts = []
            if miss_in_b:
                parts.append("b 侧缺 " + "、".join(miss_in_b))
            if miss_in_a:
                parts.append("a 侧缺 " + "、".join(miss_in_a))
            return _result(page_type, "dispute", coverage,
                           [{"kind": "set_mismatch", "detail": "表格实体集互含失败：" + "；".join(parts)}])
        # 双侧实体集皆空：集合域无信号，落到正文窗域裁决

    # ---- 正文窗域（body；seal_handwriting 及表格空集兜底）：双向滑窗覆盖率 ----
    coverage = {"a_in_b": _window_coverage(a_norm, b_norm, win),
                "b_in_a": _window_coverage(b_norm, a_norm, win)}
    if a_norm == b_norm:
        return _result(page_type, "consistent", coverage, [])
    if _FMT_RESIDUE.sub("", a_norm) == _FMT_RESIDUE.sub("", b_norm):
        detail = (f"归一化后仅剩行中 #/## 类格式残差（自动放行），"
                  f"a 样本：{_clip(a_norm)}；b 样本：{_clip(b_norm)}")
        return _result(page_type, "auto_ok_format", coverage, [])
    ca, cb = coverage["a_in_b"], coverage["b_in_a"]
    if ca >= cov_threshold and cb >= cov_threshold:
        return _result(page_type, "consistent", coverage, [])
    detail = (f"双向滑窗覆盖率不足（阈值 {cov_threshold:.2f}）："
              f"a_in_b={ca:.3f} / b_in_a={cb:.3f}")
    return _result(page_type, "dispute", coverage,
                   [{"kind": "low_coverage", "detail": detail}])
