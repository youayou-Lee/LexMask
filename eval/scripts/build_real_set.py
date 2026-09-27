"""真实案卷私有评测子集构建（Issue #37 v2）：从本地案卷库挑选、切分、生成 manifest.private.json。

铁律：真实案卷数据与 manifest.private.json（含真实路径/案名）**均不入 GitHub**——
只存在本地数据目录与云实例私有目录；仓库只提交本脚本与 manifest.private.example.json。

用法（仓库根执行）：
  python eval/scripts/build_real_set.py \
      --src /path/to/local-case-dir \
      --out /path/to/output/eval-set \
      --manifest eval/datasets/manifest.private.json

选样原则（覆盖矩阵，非凑数；每条 SELECTION 带理由）：
  - 载体：真实文本层 PDF / 混合卷 / 纯扫描卷（不同案件=不同扫描仪=不同质量）；
  - 内容：判决书（文字密集）/ 证据卷（票据、盖章噪声）/ 司法会计鉴定（数字表格最密）/
    裁定书（最小文件）/ 加密卷（稳健性边界：系统必须明确报错）；
  - 轻量为先（用户口径）：每卷只取前 5 页（select 保留原始图像字节，不重采样）；
  - 效果 GT：真实文本层卷可走化名管线人工复核建 GT（后续与用户做）；扫描卷 GT 需人工标注，
    v1 先入「速度/稳健性」子集（gt=null），不虚构 GT。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

import fitz

# 选样矩阵：id → (相对源路径, 页范围或 None=全文, 类别, 选样理由)
# 案件源目录用代号（caseA…caseF）；真实目录名映射属内部运维信息，不入库分发。
SELECTION: list[dict] = [
    dict(id="real_caseA_judgment", src="caseA/[非案卷]一审判决书.pdf", pages=None,
         category="text_pdf", why="真实文本层判决书：真实 PII 密度+文字密集，可走化名管线建效果 GT"),
    dict(id="real_caseA_evidence2_p1-5", src="caseA/[证据卷]2.pdf", pages=(1, 5),
         category="hybrid_pdf", why="混合载体证据卷：真实文本层与扫描图混排，测载体路由"),
    dict(id="real_caseB_wenshu_p1-5", src="caseB/[文书卷]文书卷.pdf", pages=(1, 5),
         category="scanned_pdf", why="小型扫描文书卷开头：文字密集扫描件基线"),
    dict(id="real_caseC_wenshu_p1-5",
         src="caseC/[文书卷]文书卷.pdf",
         pages=(1, 5), category="scanned_pdf", why="另一案件扫描文书卷：不同扫描仪质量对比"),
    dict(id="real_caseD_zj1_p1-5",
         src="caseD/[证据卷]证据卷1.pdf",
         pages=(1, 5), category="scanned_pdf", why="扫描证据卷：票据/收据类，盖章与手写噪声"),
    dict(id="real_caseE_zj5_p1-5",
         src="caseE/[其他卷]证据卷5.pdf",
         pages=(1, 5), category="scanned_pdf", why="大卷（214 页）开头：扫描证据卷样本"),
    dict(id="real_caseC_zj6_p1-5", src="caseC/[证据卷]证据卷6.pdf",
         pages=(1, 5), category="scanned_pdf",
         why="诈骗案证据卷：银行流水/转账凭证最密集的真实数字样本"),
    dict(id="real_caseF_hei_p1-5", src="caseF/disc1/大型多被告案主卷.pdf",
         pages=(1, 5), category="scanned_pdf",
         why="大型多被告案件文书卷：第三种扫描来源；该源大卷多加密，此为少数可开样本"),
    dict(id="real_caseE_caiding", src="caseE/[非案卷]裁定书.pdf",
         pages=None, category="scanned_pdf", why="最小文件（2 页）：上传/解析开销下限"),
    dict(id="real_caseF_encrypted_supplement", src="caseF/补充侦查卷.pdf",
         pages=None, category="encrypted_pdf",
         why="加密边界样本（需密码）：系统必须明确报错，不得静默零框或挂死"),
]


def split_pdf(src: Path, out_path: Path, page_range: tuple[int, int] | None) -> int:
    """按页范围切分（1 起、含端点），保留原始图像字节不重采样；返回页数。"""
    doc = fitz.open(str(src))
    try:
        if page_range:
            start, end = page_range
            end = min(end, doc.page_count)
            keep = list(range(start - 1, end))
            doc.select(keep)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        doc.save(str(out_path), garbage=3, deflate=True)
        return doc.page_count
    finally:
        doc.close()


def build_all(src_root: Path, out_dir: Path, manifest_path: Path) -> list[dict]:
    entries = []
    for spec in SELECTION:
        src = src_root / spec["src"]
        if not src.exists():
            print(f"⚠️ 跳过（源不存在）: {spec['id']} <- {src}")
            continue
        out_name = f"{spec['id']}{src.suffix.lower()}"
        out_path = out_dir / out_name
        if spec["category"] == "encrypted_pdf":
            out_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(src), str(out_path))  # 加密卷不可 select，原样复制
            pages = -1
        else:
            pages = split_pdf(src, out_path, spec["pages"])
        size_mb = round(out_path.stat().st_size / 1048576, 1)
        print(f"built {out_name}（{pages} 页，{size_mb}MB）— {spec['why']}")
        entries.append({
            "id": spec["id"], "path": str(out_path), "gt": None, "source": "real",
            "carrier": spec["category"], "doc_type": "real", "density": "n/a",
            "pages": pages, "generator": f"build_real_set.py <- {spec['src']}"
                          + (f" p{spec['pages'][0]}-{spec['pages'][1]}" if spec["pages"] else " 全文"),
            "levels": ["e2e"], "access": "private", "why": spec["why"], "notes": spec["why"],
        })
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(
        {"version": 1, "access": "private",
         "notice": "真实案卷私有子集：本文件与 path 指向的数据均不入 GitHub（铁律）；"
                   "gt=null 条目仅参与速度/稳健性评测，不参与效果指标"},
        ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return entries


def main() -> int:
    parser = argparse.ArgumentParser(description="真实案卷私有评测子集构建（Issue #37）")
    parser.add_argument("--src", required=True, type=Path, help="案卷源根目录（本地/云各自路径）")
    parser.add_argument("--out", required=True, type=Path, help="切分产物目录（不入库）")
    parser.add_argument("--manifest", default="eval/datasets/manifest.private.json", type=Path)
    args = parser.parse_args()
    entries = build_all(args.src, args.out, args.manifest)
    # manifest 的 files 字段单独写（build_all 先写头再补，保证中途失败不留半文件）
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    manifest["files"] = entries
    args.manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    total_pages = sum(e["pages"] for e in entries if e["pages"] > 0)
    print(f"OK: {len(entries)} 条目 / 约 {total_pages} 页 -> {args.manifest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
