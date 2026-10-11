"""帮信罪裁判文书 NER（Mendeley scpttyz6t5，CC BY 4.0）→ UIE SFT 格式转换。

Issue#90 阶段1 A 路训练料。输入 train1.jsonl（每行 {id,text,label:[[s,e,type],...]}），
输出 PaddleNLP UIE finetune 标准 JSONL：{content, result_list:[{text,start,end}], prompt}。
类型映射（考纲冻结 2026-10-11，见 issue90 阶段1 方案）：
  name→姓名 · org→机关单位/公司名称/组织机构（后缀三分） · location→地址
  time→日期 · num→案号+编号（双 prompt） · money→金额
law/prison 丢弃计数。同一 span 多 prompt 时各出一条样本。
"""
import argparse
import json
import random
import re
from collections import Counter
from pathlib import Path

ORG_GOV = ("检察院", "公安", "法院", "分局", "司法局", "监狱管理局")
ORG_COMPANY = ("公司", "集团", "厂", "银行")

# 考纲冻结：A 路类型映射表（变更须改此处并重新过防泄漏与评审）
TYPE_MAP = {"name": ["姓名"], "location": ["地址"], "time": ["日期"],
            "num": ["案号", "编号"], "money": ["金额"]}
DROP_TYPES = {"law", "prison"}


def map_org(text: str, s: int, e: int) -> list[str]:
    span = text[s:e]
    if any(k in span for k in ORG_GOV):
        return ["机关单位"]
    if any(k in span for k in ORG_COMPANY):
        return ["公司名称"]
    return ["组织机构"]


def convert(records: list[dict]) -> tuple[list[dict], Counter]:
    out, stats = [], Counter()
    for rec in records:
        text = rec["text"]
        for s, e, typ in rec["label"]:
            if typ in DROP_TYPES:
                stats[f"drop:{typ}"] += 1
                continue
            prompts = TYPE_MAP.get(typ) or (map_org(text, s, e) if typ == "org" else None)
            if not prompts:
                stats[f"unmapped:{typ}"] += 1
                continue
            span = text[s:e]
            if not span:
                continue
            for p in prompts:
                out.append({"content": text, "prompt": p,
                            "result_list": [{"text": span, "start": s, "end": e}]})
                stats[p] += 1
    return out, stats


def split_dev(samples: list[dict], dev_ratio: float, seed: int) -> tuple[list[dict], list[dict]]:
    """按「段落」留出 dev：同一 content 的所有样本同进同出，防 span 拆散泄漏。"""
    by_content: dict[str, list[dict]] = {}
    for smp in samples:
        by_content.setdefault(smp["content"], []).append(smp)
    keys = sorted(by_content)
    rng = random.Random(seed)
    rng.shuffle(keys)
    n_dev = max(1, int(len(keys) * dev_ratio))
    dev_keys = set(keys[:n_dev])
    train = [s for k in keys[n_dev:] for s in by_content[k]]
    dev = [s for k in keys[:n_dev] for s in by_content[k]]
    return train, dev


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, type=Path, help="帮信罪 train1.json（JSONL）")
    ap.add_argument("--out-train", required=True, type=Path)
    ap.add_argument("--out-dev", required=True, type=Path)
    ap.add_argument("--dev-ratio", type=float, default=0.1)
    ap.add_argument("--seed", type=int, default=90)
    args = ap.parse_args()

    records = [json.loads(l) for l in args.src.read_text(encoding="utf-8").splitlines() if l.strip()]
    samples, stats = convert(records)
    train, dev = split_dev(samples, args.dev_ratio, args.seed)
    args.out_train.parent.mkdir(parents=True, exist_ok=True)
    args.out_train.write_text("\n".join(json.dumps(s, ensure_ascii=False) for s in train) + "\n", encoding="utf-8")
    args.out_dev.write_text("\n".join(json.dumps(s, ensure_ascii=False) for s in dev) + "\n", encoding="utf-8")
    print(f"segments={len(records)} samples={len(samples)} train={len(train)} dev={len(dev)}")
    print("type stats:", dict(stats))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
