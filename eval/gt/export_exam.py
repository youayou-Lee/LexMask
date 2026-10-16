"""评测考卷导出器（Issue#105 G4）。

把产线逐页结果（``{stem,page_no,pdf,text,entities:[{text,type}]}``）导出为
eval-ng / t2 桶同族的考卷 jsonl（``page/pdf/page_no/text/entities`` +
``gt_version``）。

**按类型子集勾选**是本模块存在的理由（2026-10-15 用户拍板：GT 类型全集
保留，评测时勾选）——出考卷时只带勾选类型，且勾选后实体为空的页默认剔除，
不留空考题。旧口径类型名（人名/机构名等）经 ``LEGACY_TYPE_MAP`` 归一到
preset 名后再参与勾选，新旧产物混用不炸。
"""
from __future__ import annotations

import json
from pathlib import Path

#: 产线早期口径 → preset 名（与 self-ner-eval/type-mapping.json 同款）
LEGACY_TYPE_MAP = {
    "人名": "姓名",
    "机构名": "机构名称",
}

#: 页号 → 卷内页标识（t2 桶族：``<stem>.p_p<N+1>``）


def page_id(stem: str, page_no: int) -> str:
    return f"{stem}.p_p{page_no + 1}"


def _norm_type(t: str) -> str:
    return LEGACY_TYPE_MAP.get(t, t)


def build_exam(pages: list[dict], types: set[str] | None = None,
               exclude_pages: set[tuple[str, int]] | None = None,
               keep_empty: bool = False) -> list[dict]:
    """逐页结果 → 考卷行列表。

    - ``types``：勾选导出的类型子集（None = 全类型）；
    - ``exclude_pages``：域排除（如流水域）``{(stem, page_no)}``；
    - ``keep_empty``：勾选后实体为空的页是否保留（默认剔除，不产空考题）。
    """
    out = []
    for p in pages:
        stem, page_no = p["stem"], p["page_no"]
        if exclude_pages and (stem, page_no) in exclude_pages:
            continue
        ents = p.get("entities") or []
        if types is not None:
            ents = [e for e in ents if _norm_type(e["type"]) in types]
        if not ents and not keep_empty:
            continue
        out.append({
            "page": page_id(stem, page_no),
            "pdf": p.get("pdf", f"{stem}.pdf"),
            "page_no": page_no,
            "text": p.get("text", ""),
            "entities": [{"text": e["text"], "type": _norm_type(e["type"])}
                         for e in ents],
        })
    return out


def exam_summary(rows: list[dict]) -> dict:
    """考卷概况：页数 / 实体数 / 分类型计数（report 与人工核对用）。"""
    by_type: dict[str, int] = {}
    for r in rows:
        for e in r["entities"]:
            by_type[e["type"]] = by_type.get(e["type"], 0) + 1
    return {
        "pages": len(rows),
        "entities": sum(by_type.values()),
        "by_type": dict(sorted(by_type.items(), key=lambda kv: -kv[1])),
    }


def build_evalng_exam(rows: list[dict], exam_id: str,
                      bucket_meta: dict | None = None) -> dict:
    """考卷行 → eval-ng 考卷文档（``{"exam_id", "items": [...]}``）。

    每页一题，``form=entity_table``，``gt={类型: [实体串, ...]}``。同页重复
    实体原样保留、忠实记录 GT（评分按集合域自然去重，与 t2 桶同族）。
    ``bucket_meta`` 可选，随考卷写入报告（与 make_exam_from_t2 的桶说明
    同槽位；产线路径暂不使用，为多案合并报告预留）。
    """
    items = []
    for r in rows:
        gt: dict[str, list[str]] = {}
        for e in r["entities"]:
            gt.setdefault(e["type"], []).append(e["text"])
        items.append({
            "id": r["page"],
            "exam_id": exam_id,
            "form": "entity_table",
            "text": r["text"],
            "types": sorted(gt),
            "gt": gt,
        })
    doc: dict = {"exam_id": exam_id, "items": items}
    if bucket_meta:
        doc["buckets"] = bucket_meta
    return doc


def write_evalng_exam(doc: dict, out_path: str | Path) -> None:
    """写 eval-ng 考卷 JSON（单文件、非 jsonl——cli.load_exam_json 的入参形状）。"""
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=1),
                    encoding="utf-8")


def write_exam_jsonl(rows: list[dict], out_path: str | Path,
                     version: str) -> None:
    """写考卷 jsonl，每行带 ``gt_version``（口径变更可追溯，#56 惯例）。"""
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps({**r, "gt_version": version},
                               ensure_ascii=False, sort_keys=True) + "\n")
