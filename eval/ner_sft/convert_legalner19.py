"""LegalNER-19（maheran123/LegalNER-19，CC BY-NC 4.0）→ UIE SFT 格式转换（Issue#90 round3）。

输入 GitHub data/*.jsonl：{"id","text","entities":[{type,start,end,text}]}（[start,end) span）。
19 类→考纲冻结映射（2026-10-11）：
  def/vic/par/adv→姓名 · tim→日期 · loc→地址 · amt/fee→金额 · doc→文书编号
  cas→案号+编号（双 prompt） · law/cri/pri/add→法律记录 · cou/pro/pol→机关单位
  org→组织机构/公司名称（后缀规则） · obj 丢弃计数
每篇文档已是整页分布，直接单文档转换、同类型合并 result_list（与考场调用同构）。
"""
import argparse
import json
from collections import Counter
from pathlib import Path

ORG_COMPANY = ("公司", "集团", "厂", "银行")

# 考纲冻结：LegalNER-19 映射表（round3）
TYPE_MAP = {
    "def": ["姓名"], "vic": ["姓名"], "par": ["姓名"], "adv": ["姓名"],
    "tim": ["日期"], "loc": ["地址"], "amt": ["金额"], "fee": ["金额"],
    "doc": ["文书编号"], "cas": ["案号", "编号"],
    "law": ["法律记录"], "cri": ["法律记录"], "pri": ["法律记录"], "add": ["法律记录"],
    "cou": ["机关单位"], "pro": ["机关单位"], "pol": ["机关单位"],
}


def map_org(span: str) -> list[str]:
    if any(k in span for k in ORG_COMPANY):
        return ["公司名称"]
    return ["组织机构"]


def convert(path: Path) -> tuple[list[dict], Counter]:
    out, stats = [], Counter()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        text = rec["text"]
        by_prompt: dict[str, list[dict]] = {}
        for ent in rec["entities"]:
            typ, s, e = ent["type"], ent["start"], ent["end"]
            prompts = TYPE_MAP.get(typ) or (map_org(text[s:e]) if typ == "org" else None)
            if not prompts:
                stats[f"drop:{typ}"] += 1
                continue
            span = text[s:e]
            if not span or text[s:e] != ent.get("text", span):
                # 官方 span 与 text 字段一致性校验（防止坐标漂移静默入库）
                assert text[s:e] == ent["text"], f"{rec['id']}: {typ} {text[s:e]!r}!={ent['text']!r}"
            for p in prompts:
                by_prompt.setdefault(p, []).append({"text": span, "start": s, "end": e})
                stats[p] += 1
        for p, spans in by_prompt.items():
            out.append({"content": text, "prompt": p, "result_list": spans})
    return out, stats


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args()
    samples, stats = convert(args.src)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n".join(json.dumps(s, ensure_ascii=False) for s in samples) + "\n",
                        encoding="utf-8")
    print(f"docs -> samples={len(samples)}")
    print("stats:", dict(stats))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
