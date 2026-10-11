"""B 路合成数字/低频类型 SFT 训练料（Issue#90 阶段1）。

复用 make_ner_gt_corpus 的确定性实体函数，但用**独立序号段与全新上下文模板**：
与考场 digit-confusion 桶（build_entry i∈[0,50)，模板固定）不同文本分布，防泄漏由
leakage_guard.py 硬校验兜底。考纲冻结类型：身份证号/出生日期/电话/银行卡号/银行账号/
车牌号/性别/民族/文书编号/金额。
"""
import argparse
import importlib.util
import json
import random
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "make_ner_gt_corpus", _REPO / "backend" / "scripts" / "eval" / "make_ner_gt_corpus.py")
corp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(corp)

ETHNIC = ["汉族", "壮族", "回族", "满族", "苗族", "彝族", "土家族", "藏族", "蒙古族", "侗族"]
TEMPLATES = [
    ("身份证号", lambda i: (f"经查，流动人口{corp.person_name(i)}（身份证号{corp.id_card(i)}）"
                          f"自{corp.address(i)}暂住本市务工。")),
    ("出生日期", lambda i: (f"被告人{corp.person_name(i)}，男，{corp.date_cn(i)}出生，"
                          f"案发时已成年。")),
    ("电话", lambda i: (f"证人{corp.person_name(i)}证实，其用手机号{corp.phone(i)}"
                       f"与对方联系过三次。")),
    ("银行卡号", lambda i: (f"涉案资金经银行卡（卡号{corp.bank_card(i)}）流转，"
                          f"户主为{corp.person_name(i)}。")),
    ("银行账号", lambda i: (f"收款账户账号{corp.bank_card(i + 7)}，开户人{corp.person_name(i)}，"
                          f"交易备注为货款。")),
    ("车牌号", lambda i: (f"监控显示牌照为{corp.plate(i) if hasattr(corp, 'plate') else _plate(i)}"
                          f"的车辆驶离现场，驾驶人{corp.person_name(i)}。")),
    ("性别", lambda i: f"网上在逃人员{corp.person_name(i)}，{'男' if i % 2 else '女'}性。"),
    ("民族", lambda i: f"被告人{corp.person_name(i)}，{ETHNIC[i % len(ETHNIC)]}，无前科。"),
    ("文书编号", lambda i: (f"该案另有补充侦查决定书一份，文号{_doc_no(i)}。")),
    ("金额", lambda i: (f"{corp.person_name(i)}分三次转账共计{1000 * (i % 900) + 37}元"
                       f"至指定账户。")),
]


def _plate(i: int) -> str:
    return f"京{chr(65 + i % 26)}·{10000 + (i * 53) % 89999}"


def _doc_no(i: int) -> str:
    return f"{['京', '沪', '粤', '浙', '川'][i % 5]}公（刑）补侦字〔202{4 + i % 2}〕{100 + i}号"


def build(n_per_type: int, seq_start: int, seed: int) -> list[dict]:
    """seq_start 取大偏移（默认 100000）确保与考场生成序号段不重叠。"""
    rng = random.Random(seed)
    out = []
    for typ, tmpl in TEMPLATES:
        for k in range(n_per_type):
            i = seq_start + rng.randrange(1_000_000)
            text = tmpl(i)
            # 实体串必须唯一可命中：在模板文本里反查类型值
            val = _find_value(typ, i)
            s = text.find(val)
            assert s >= 0, f"{typ}: {val!r} 不在模板文本中"
            out.append({"content": text, "prompt": typ,
                        "result_list": [{"text": val, "start": s, "end": s + len(val)}]})
    rng.shuffle(out)
    return out


def _find_value(typ: str, i: int) -> str:
    if typ == "身份证号":
        return corp.id_card(i)
    if typ == "出生日期":
        return corp.date_cn(i)
    if typ == "电话":
        return corp.phone(i)
    if typ == "银行卡号":
        return corp.bank_card(i)
    if typ == "银行账号":
        return corp.bank_card(i + 7)
    if typ == "车牌号":
        return _plate(i)
    if typ == "性别":
        return "男" if i % 2 else "女"
    if typ == "民族":
        return ETHNIC[i % len(ETHNIC)]
    if typ == "文书编号":
        return _doc_no(i)
    if typ == "金额":
        return f"{1000 * (i % 900) + 37}元"
    raise KeyError(typ)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--n-per-type", type=int, default=300)
    ap.add_argument("--seq-start", type=int, default=100000)
    ap.add_argument("--seed", type=int, default=90101)
    args = ap.parse_args()
    samples = build(args.n_per_type, args.seq_start, args.seed)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n".join(json.dumps(s, ensure_ascii=False) for s in samples) + "\n",
                        encoding="utf-8")
    from collections import Counter
    print(f"synthetic sft samples={len(samples)}",
          dict(Counter(s["prompt"] for s in samples)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
