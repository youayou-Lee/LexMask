"""防泄漏硬校验（Issue#90 阶段1 A1）：训练料 vs 全部考场桶。

两级检测，交集/命中必须为 0，非 0 exit 1 停线：
1. 规范化文本哈希交集（去空白后精确匹配，含 content 双向子串互含检测）；
2. 字符 3-gram Jaccard > 0.8 高相似克隆检测（抽样阈值内全检）。
检测范围：训练集 + 早停 dev 全部样本 content，对 hardcase/公开/合成全部考场桶文本。
输出报告 JSON 落私有区，stdout 打印结论。
"""
import argparse
import json
import re
from pathlib import Path


def norm(t: str) -> str:
    return re.sub(r"\s+", "", t)


def ngrams(t: str, n: int = 3) -> set[str]:
    t = norm(t)
    return {t[i:i + n] for i in range(max(1, len(t) - n + 1))}


def load_texts(paths: list[Path]) -> list[tuple[str, str]]:
    """(来源标签, 文本)。SFT 文件取 content；考场 jsonl 取 text。"""
    out = []
    for p in paths:
        for line in p.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            e = json.loads(line)
            text = e.get("content") or e.get("text")
            out.append((f"{p.parent.name}/{p.name}", text))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sft", required=True, nargs="+", type=Path,
                    help="训练/ dev SFT jsonl（可多个）")
    ap.add_argument("--buckets-dir", required=True, type=Path,
                    help="考场桶目录（rglob *.jsonl 全部视为禁区）")
    ap.add_argument("--report", required=True, type=Path)
    ap.add_argument("--jaccard-threshold", type=float, default=0.8)
    args = ap.parse_args()

    train = load_texts(args.sft)
    FORBIDDEN = load_texts(sorted(args.buckets_dir.rglob("*.jsonl")))
    bucket_norm = {norm(t) for _, t in FORBIDDEN}
    bucket_grams = [(norm(t), ngrams(t)) for _, t in FORBIDDEN]

    exact, contained, similar = [], [], []
    for src, text in train:
        nt = norm(text)
        if nt in bucket_norm:
            exact.append({"src": src, "norm": nt[:80]})
            continue
        if any(nt and (nt in bn or bn in nt) for bn in bucket_norm if bn):
            contained.append({"src": src, "norm": nt[:80]})
            continue
        g = ngrams(text)
        if not g:
            continue
        for bn, bg in bucket_grams:
            j = len(g & bg) / len(g | bg)
            if j > args.jaccard_threshold:
                similar.append({"src": src, "bucket_norm": bn[:80], "jaccard": round(j, 4)})
                break

    ok = not (exact or contained or similar)
    report = {
        "train_samples": len(train), "bucket_texts": len(FORBIDDEN),
        "exact_hits": exact, "contained_hits": contained, "similar_hits": similar,
        "jaccard_threshold": args.jaccard_threshold, "verdict": "PASS" if ok else "FAIL",
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"train={len(train)} buckets={len(FORBIDDEN)} exact={len(exact)} "
          f"contained={len(contained)} similar={len(similar)} -> {report['verdict']}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
