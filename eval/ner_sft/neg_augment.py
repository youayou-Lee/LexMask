"""负样本增强（Issue#90 阶段1）：纯正样本会训成见 prompt 就抓，须按比例注入空结果样本。

对每个 content，从冻结考纲类型表里抽「该文不含该类型实体」的 prompt 生成空 result_list 样本，
正:负 ≈ 1:negative_ratio（doccano.py 同款思路）。类型表=训练覆盖类型的并集（见考纲冻结）。
"""
import argparse
import json
import random
from collections import defaultdict
from pathlib import Path

# 考纲冻结：训练覆盖类型全集（convert_bangxin TYPE_MAP 值 + gen_digit_sft TEMPLATES）
COVERED_TYPES = ["姓名", "机关单位", "公司名称", "组织机构", "地址", "日期",
                 "案号", "编号", "金额", "身份证号", "出生日期", "电话",
                 "银行卡号", "银行账号", "车牌号", "性别", "民族", "文书编号"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--inputs", required=True, nargs="+", type=Path)
    ap.add_argument("--negative-ratio", type=float, default=0.2,
                    help="负样本数=正样本数×该值（0.2≈1:5）")
    ap.add_argument("--seed", type=int, default=90)
    args = ap.parse_args()

    rng = random.Random(args.seed)
    by_content: dict[str, list[str]] = defaultdict(list)
    rows = []
    for p in args.inputs:
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                rows.append(row)
                by_content[row["content"]].append(row["prompt"])

    neg = []
    for content, pos_prompts in by_content.items():
        cand = [t for t in COVERED_TYPES if t not in pos_prompts]
        k = min(len(cand), max(1, int(len(pos_prompts) * args.negative_ratio)))
        for t in rng.sample(cand, k):
            neg.append({"content": content, "prompt": t, "result_list": []})

    out = rows + neg
    rng.shuffle(out)
    print(f"positive={len(rows)} negative={len(neg)} total={len(out)}")
    dest = args.inputs[0].with_name(args.inputs[0].name.replace(".jsonl", "-neg.jsonl"))
    dest.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in out) + "\n",
                    encoding="utf-8")
    print(f"wrote {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
