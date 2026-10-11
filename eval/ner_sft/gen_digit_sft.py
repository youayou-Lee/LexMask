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

def _find_repo() -> Path:
    """从脚本位置或 CWD 向上探测仓根（含 backend/scripts/eval/make_ner_gt_corpus.py），
    兼容实例上脚本被拷出仓布局运行（如 /root/ner90/）。"""
    for base in [Path(__file__).resolve().parent, Path.cwd(), *Path.cwd().parents]:
        cand = base
        for _ in range(4):
            if (cand / "backend/scripts/eval/make_ner_gt_corpus.py").exists():
                return cand
            cand = cand.parent
    raise SystemExit("找不到仓根（make_ner_gt_corpus.py）——请把脚本放仓内或用 NER_SFT_REPO 指定")


import os
_REPO = Path(os.environ.get("NER_SFT_REPO") or _find_repo())
_spec = importlib.util.spec_from_file_location(
    "make_ner_gt_corpus", _REPO / "backend" / "scripts" / "eval" / "make_ner_gt_corpus.py")
corp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(corp)

ETHNIC = ["汉族", "壮族", "回族", "满族", "苗族", "彝族", "土家族", "藏族", "蒙古族", "侗族"]

# round2：每类型多模板轮换（round1 每类型单模板导致模型学模板触发词而非实体形态——
# digit-confusion 桶电话/银行卡 exact=0 实锤）。模板间句式差异刻意拉大。
TEMPLATES = {
    "身份证号": [
        lambda i: f"经查，流动人口{corp.person_name(i)}（身份证号{corp.id_card(i)}）暂住本市务工。",
        lambda i: f"{corp.person_name(i)}，身份证号码{corp.id_card(i)}，因无固定职业被列为重点走访对象。",
        lambda i: f"身份信息核验：{corp.id_card(i)}，持有人{corp.person_name(i)}，核查无异常。",
        lambda i: f"该院办理的{corp.person_name(i)}盗窃一案中，被告人自报身份证号为{corp.id_card(i)}。",
        lambda i: f"联网核查记录显示{corp.id_card(i)}对应人员{corp.person_name(i)}已于三日前往酒店入住。",
        lambda i: f"民警在检查站核对了{corp.person_name(i)}的证件，号码{corp.id_card(i)}与本人一致。",
        lambda i: f"实名登记信息：乘车人{corp.person_name(i)}（证件编号{corp.id_card(i)}）。",
        lambda i: f"{corp.person_name(i)}使用身份证{corp.id_card(i)}办理了三张电话卡。",
    ],
    "出生日期": [
        lambda i: f"被告人{corp.person_name(i)}，男，{corp.date_cn(i)}出生，案发时已成年。",
        lambda i: f"{corp.person_name(i)}（{corp.date_cn(i)}生）户籍地经查为虚构。",
        lambda i: f"到案后如实供述。经核，{corp.person_name(i)}出生于{corp.date_cn(i)}。",
        lambda i: f"人口信息系统载明：{corp.person_name(i)}，生日期{corp.date_cn(i)}。",
    ],
    "电话": [
        lambda i: f"证人{corp.person_name(i)}证实，其用手机号{corp.phone(i)}与对方联系过三次。",
        lambda i: f"聊天记录绑定手机号码{corp.phone(i)}，实名人为{corp.person_name(i)}。",
        lambda i: f"联系方式：{corp.person_name(i)}，{corp.phone(i)}。",
        lambda i: f"被告人{corp.person_name(i)}所持号码{corp.phone(i)}案发前一日开通。",
        lambda i: f"办案民警拨打{corp.phone(i)}，接机人为{corp.person_name(i)}。",
        lambda i: f"登记电话{corp.phone(i)}由{corp.person_name(i)}代持。",
    ],
    "银行卡号": [
        lambda i: f"涉案资金经银行卡（卡号{corp.bank_card(i)}）流转，户主为{corp.person_name(i)}。",
        lambda i: f"{corp.person_name(i)}名下尾号账户{corp.bank_card(i)}收到转账两笔。",
        lambda i: f"银行流水显示账号{corp.bank_card(i)}于案发当日进账，开户人{corp.person_name(i)}。",
        lambda i: f"其将赃款存入{corp.person_name(i)}提供的银行卡{corp.bank_card(i)}内。",
        lambda i: f"支付结算账户信息：{corp.bank_card(i)}（{corp.person_name(i)}）。",
    ],
    "银行账号": [
        lambda i: f"收款账户账号{corp.bank_card(i + 7)}，开户人{corp.person_name(i)}，交易备注为货款。",
        lambda i: f"{corp.person_name(i)}在讯问中供认该账号{corp.bank_card(i + 7)}系其本人使用。",
        lambda i: f"对公转私流水：转入账号{corp.bank_card(i + 7)}，户名{corp.person_name(i)}。",
        lambda i: f"经查，账号{corp.bank_card(i + 7)}绑定手机号为其本人实名。",
    ],
    "车牌号": [
        lambda i: f"监控显示牌照为{_plate(i)}的车辆驶离现场，驾驶人{corp.person_name(i)}。",
        lambda i: f"涉事车辆（车牌{_plate(i)}）登记在{corp.person_name(i)}名下。",
        lambda i: f"卡口抓拍记录：{_plate(i)}，驾驶人经辨认为{corp.person_name(i)}。",
    ],
    "性别": [
        lambda i: f"网上在逃人员{corp.person_name(i)}，{'男' if i % 2 else '女'}性。",
        lambda i: f"在押人员{corp.person_name(i)}，性别{'男' if i % 2 else '女'}。",
        lambda i: f"{corp.person_name(i)}，{'男' if i % 2 else '女'}，现年三十岁。",
    ],
    "民族": [
        lambda i: f"被告人{corp.person_name(i)}，{ETHNIC[i % len(ETHNIC)]}，无前科。",
        lambda i: f"{corp.person_name(i)}，{ETHNIC[i % len(ETHNIC)]}族，初中文化。",
        lambda i: f"身份登记：{corp.person_name(i)}——{ETHNIC[i % len(ETHNIC)]}。",
    ],
    "文书编号": [
        lambda i: f"该案另有补充侦查决定书一份，文号{_doc_no(i)}。",
        lambda i: f"见{_doc_no(i)}卷宗。",
        lambda i: f"送达回证载明文书编号{_doc_no(i)}。",
    ],
    "金额": [
        lambda i: f"{corp.person_name(i)}分三次转账共计{1000 * (i % 900) + 37}元至指定账户。",
        lambda i: f"涉案金额合计{1000 * (i % 900) + 37}元，经办人{corp.person_name(i)}。",
        lambda i: f"经审计，{corp.person_name(i)}账户流入{1000 * (i % 900) + 37}元。",
    ],
}


def _plate(i: int) -> str:
    return f"京{chr(65 + i % 26)}·{10000 + (i * 53) % 89999}"


def _doc_no(i: int) -> str:
    return f"{['京', '沪', '粤', '浙', '川'][i % 5]}公（刑）补侦字〔202{4 + i % 2}〕{100 + i}号"


def build(n_per_type: int, seq_start: int, seed: int) -> list[dict]:
    """seq_start 取大偏移（默认 100000）确保与考场生成序号段不重叠。
    round2：每类型多模板轮换 + 多实例文本（一篇 2~3 个同/异类型实例合并）。"""
    rng = random.Random(seed)
    out = []
    for typ, tmpls in TEMPLATES.items():
        for k in range(n_per_type):
            i = seq_start + rng.randrange(1_000_000)
            text = tmpls[k % len(tmpls)](i)
            val = _find_value(typ, i)
            s = text.find(val)
            assert s >= 0, f"{typ}: {val!r} 不在模板文本中"
            out.append({"content": text, "prompt": typ,
                        "result_list": [{"text": val, "start": s, "end": s + len(val)}]})
    # 多实例合并：同类型每 3 条样本拼一篇（span 坐标平移）
    rng.shuffle(out)
    merged = []
    for typ in TEMPLATES:
        rows = [r for r in out if r["prompt"] == typ]
        for base in range(0, len(rows) - 2, 3):
            grp = rows[base:base + 3]
            parts, spans, off = [], [], 0
            for r in grp:
                parts.append(r["content"])
                for sp in r["result_list"]:
                    spans.append({"text": sp["text"], "start": sp["start"] + off,
                                  "end": sp["end"] + off})
                off += len(r["content"]) + 1
            content = "\n".join(parts)
            for sp in spans:
                assert content[sp["start"]:sp["end"]] == sp["text"]
            merged.append({"content": content, "prompt": typ, "result_list": spans})
    return out + merged


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
