# Issue#56 M2 Task 9 —— 选卷矩阵脚本 select_pages.py（classify_page / select /
# reconcile / build_pool / CLI main）的离线单测。
# 零网络、零真实案卷数据（tmp_path 合成树 + 手工拼装的最小 PDF 字节 + 纯字节
# 假 PDF）；清单不写案名的隐私约定由 e2e 用例双向锁定（清单无案名、sidecar 有）。
#
# 环境说明：pypdf 为可选依赖（实现内延迟 import）。本套件在无 pypdf 环境
# 走 hints-only 路径全绿；有 pypdf 的环境额外执行 @skipif 守卫的用例
# （文本层/扫描页分类、≥10 页大卷首/中段切分）。两环境对同一 fixture 树的
# 候选页数与页型判定保持一致（fixture 均为单页文本层 PDF 或纯字节假 PDF）。
#
# 与计划底稿脚手架的差异（机械修正，实现 API 不变，见 task-9-report）：
#   底稿以"真实文本层 PDF"测 classify 全部分支——pypdf 是可选依赖，离线环境
#   无 pypdf，改为：classify 的文本规则经 text_hint 参数（调用方已抽取文本
#   的合法入口）测试，pypdf 相关分支以 skipif 守卫补充。
import hashlib
import importlib.util
import json
import re
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "eval"))
from gt import gt_schema, select_pages  # noqa: E402

HAS_PYPDF = importlib.util.find_spec("pypdf") is not None
# e2e 断言的清单 classifier 取值随环境而定（两环境都必须各自自洽）
EXPECTED_CLASSIFIER = "pypdf" if HAS_PYPDF else "hints-only"


# ---- 测试夹具：手工最小 PDF（无压缩、Helvetica 文本层，页数可控） --------------

def _make_pdf(path: Path, pages: int = 1, text: str = "Body text page for selection",
              image: bool = False) -> Path:
    """在 path 落一个手工拼装的最小合法 PDF，返回 path。

    仅测试用：对象编号方案 1=Catalog 2=Pages 3=Font [4=Image]，此后每页两
    对象（内容流、页）。有 pypdf 的环境须满足 len(reader.pages)==pages。
    text="" 时该页无文本层（配合 image=True 造"扫描页"、image=False 造
    "数字空白页"）；image=True 且 text 非空时不使用（语义含混）。
    """
    assert not (image and text), "夹具语义：图像页与文本页不混用（见 docstring）"
    base = 5 if image else 4
    content_nums = [base + 2 * i for i in range(pages)]
    page_nums = [base + 2 * i + 1 for i in range(pages)]

    objs: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: ("<< /Type /Pages /Kids [%s] /Count %d >>"
            % (" ".join(f"{n} 0 R" for n in page_nums), pages)).encode(),
        3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    }
    if image:
        objs[4] = (b"<< /Type /XObject /Subtype /Image /Width 1 /Height 1 "
                   b"/ColorSpace /DeviceGray /BitsPerComponent 8 /Length 1 >>"
                   b"\nstream\n\x00\nendstream")
    for i in range(pages):
        parts = []
        if image:
            parts.append("q 612 0 0 792 0 0 cm /Im0 Do Q")
        if text:
            esc = text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
            parts.append(f"BT /F1 12 Tf 72 720 Td ({esc}) Tj ET")
        body = " ".join(parts).encode("latin-1")
        objs[content_nums[i]] = (b"<< /Length %d >>\nstream\n%s\nendstream"
                                 % (len(body), body))
        res = "/Font << /F1 3 0 R >>" + (" /XObject << /Im0 4 0 R >>" if image else "")
        objs[page_nums[i]] = ("<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                              "/Resources << %s >> /Contents %d 0 R >>"
                              % (res, content_nums[i])).encode()

    out = bytearray(b"%PDF-1.4\n")
    offsets: dict[int, int] = {}
    for num in sorted(objs):
        offsets[num] = len(out)
        out += f"{num} 0 obj\n".encode() + objs[num] + b"\nendobj\n"
    xref_pos = len(out)
    count = max(objs) + 1
    out += f"xref\n0 {count}\n".encode() + b"0000000000 65535 f \n"
    for num in range(1, count):
        out += f"{offsets[num]:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {count} /Root 1 0 R >>\nstartxref\n{xref_pos}\n"
            f"%%EOF\n").encode()
    path.write_bytes(bytes(out))
    return path


def _make_entry(case_ref: str = "case-001", file: str = "f" * 64, page: int = 0,
                page_type: str = "body", segment: str = "first") -> dict:
    return {"case_ref": case_ref, "file": file, "page": page,
            "page_type": page_type, "segment": segment}


def _pool(counts: dict[str, int]) -> list[dict]:
    """按页型计数造候选池（页号/卷号依次编造，合成占位符）。"""
    pool: list[dict] = []
    for ptype, n in counts.items():
        for i in range(n):
            pool.append(_make_entry(case_ref=f"case-{ptype}-{i:03d}",
                                    file=hashlib.sha256(f"{ptype}-{i}".encode()).hexdigest(),
                                    page=i, page_type=ptype))
    return pool


# ---- 常量（单一事实源） ---------------------------------------------------------

def test_quotas_verbatim_and_keys_are_page_types():
    # spec §2 配额逐字：body 50 / table 25 / seal_handwriting 15 / edge 10
    assert select_pages.QUOTAS == {"body": 50, "table": 25,
                                   "seal_handwriting": 15, "edge": 10}
    # 键集 = gt_schema.PAGE_TYPES（唯一事实源，Task 4/5 同款收编）
    assert set(select_pages.QUOTAS) == gt_schema.PAGE_TYPES


def test_make_pdf_fixture_xref_offsets_wellformed(tmp_path):
    # 夹具自检（不依赖 pypdf）：xref 每条记录须指向对应 "N 0 obj" 起始——
    # 保证有 pypdf 的机器上 _make_pdf 产出可开卷的合法 PDF。
    p = _make_pdf(tmp_path / "s.pdf", pages=3)
    raw = p.read_bytes()
    assert raw.startswith(b"%PDF-1.4")
    tail = raw.rsplit(b"startxref", 1)[1]
    xref_pos = int(tail.split()[0])
    lines = raw[xref_pos:].split(b"\n")
    assert lines[0] == b"xref"
    count = int(lines[1].split()[1])
    for num in range(1, count):
        offset = int(lines[2 + num][:10])
        assert raw[offset:].startswith(f"{num} 0 obj".encode()), f"obj {num} 偏移错位"


# ---- classify_page：文本规则经 text_hint 测（离线环境无 pypdf 也可全跑） --------

def test_classify_table_by_keyword():
    for kw in ("本卷目录如下", "附表格一份", "银行流水明细"):
        assert select_pages.classify_page("/x/plain.pdf", 0, text_hint=f"前言 {kw} 后记") == "table"


def test_classify_table_by_column_density():
    text = "\n".join(["姓名  金额  日期"] * 6)  # 6 行 ≥4 且全部 ≥2 空格分列
    assert select_pages.classify_page("/x/plain.pdf", 0, text_hint=text) == "table"


def test_classify_edge_near_empty_text():
    assert select_pages.classify_page("/x/plain.pdf", 0, text_hint="   ") == "edge"


def test_classify_seal_by_filename_hints():
    for name in ("授权书", "询问笔录", "讯问笔录", "现场照片", "送达回执"):
        assert select_pages.classify_page(f"/x/{name}材料.pdf", 0) == "seal_handwriting"


def test_classify_body_long_text_without_signal():
    assert select_pages.classify_page("/x/plain.pdf", 0,
                                      text_hint="这一页是足够长的正文文本内容") == "body"


def test_classify_precedence_table_over_hint():
    # 路径含提示词但文本层命中表格关键词 → table 优先（裁定：文本规则 > 文件名提示）
    assert select_pages.classify_page("/x/询问笔录.pdf", 0, text_hint="目录：第一章") == "table"


def test_classify_precedence_edge_over_hint():
    assert select_pages.classify_page("/x/询问笔录.pdf", 0, text_hint=" ") == "edge"


def test_classify_hint_applies_even_with_text_layer():
    assert select_pages.classify_page("/x/讯问笔录.pdf", 0,
                                      text_hint="这一页是足够长的正文文本内容") == "seal_handwriting"


def test_classify_hints_only_corrupt_file_is_graceful():
    # pypdf 打不开的"文件"（此处纯字节假 PDF）不得抛异常 → 退化为纯 hints 判定
    assert select_pages.classify_page("/not/exist/plain.pdf", 0) == "body"
    assert select_pages.classify_page("/not/exist/询问笔录.pdf", 3) == "seal_handwriting"


def test_classify_custom_hint_re_kwarg():
    # --hints 追加正则经 hint_re 可选参数进入（默认内置提示不受影响）
    assert select_pages.classify_page("/x/情况说明.pdf", 0) == "body"
    custom = re.compile(select_pages._HINT_RE.pattern + "|情况说明")
    assert select_pages.classify_page("/x/情况说明.pdf", 0, hint_re=custom) == "seal_handwriting"


def test_classify_real_pdf_no_text_layer_paths():
    # 纯字节假 PDF 落盘再分类：与"文件不存在"同走 hints-only 退化，不打异常
    with tempfile.TemporaryDirectory() as td:
        fake = Path(td) / "假扫描材料.pdf"
        fake.write_bytes(b"not a pdf at all")
        assert select_pages.classify_page(str(fake), 0) == "body"


@pytest.mark.skipif(not HAS_PYPDF, reason="pypdf 可选依赖未安装（离线环境走 hints-only）")
class TestClassifyWithPypdf:
    """pypdf 在场时的真实开卷分支（本环境跳过，有 pypdf 的机器补跑）。"""

    def test_text_layer_pdf_is_body(self, tmp_path):
        p = _make_pdf(tmp_path / "plain.pdf", text="Body text page for selection")
        assert select_pages.classify_page(str(p), 0) == "body"

    def test_blank_digital_page_is_edge(self, tmp_path):
        p = _make_pdf(tmp_path / "blank.pdf", text="")
        assert select_pages.classify_page(str(p), 0) == "edge"

    def test_image_only_page_is_body_scanned_initial_guess(self, tmp_path):
        # 扫描页（有图像无文本层）初判 body——设计 §2：初判由用户过目纠正
        p = _make_pdf(tmp_path / "scan.pdf", text="", image=True)
        assert select_pages.classify_page(str(p), 0) == "body"

    def test_page_out_of_range_degrades_to_hints(self, tmp_path):
        p = _make_pdf(tmp_path / "plain.pdf", pages=2)
        assert select_pages.classify_page(str(p), 99) == "body"


# ---- select：分层随机 + 缺口全取 + seed 复现 ------------------------------------

def test_select_fills_each_quota_cell():
    pool = _pool({"body": 60, "table": 30, "seal_handwriting": 20, "edge": 12})
    got = select_pages.select(pool, dict(select_pages.QUOTAS), seed=42)
    by_type: dict[str, list[dict]] = {}
    for e in got:
        by_type.setdefault(e["page_type"], []).append(e)
    assert {k: len(v) for k, v in by_type.items()} == dict(select_pages.QUOTAS)
    for e in got:
        assert set(e) == {"case_ref", "file", "page", "page_type", "segment", "reason"}
        assert e["reason"] == f"quota:{e['page_type']}"


def test_select_insufficient_cell_takes_all():
    pool = _pool({"body": 2, "table": 0, "seal_handwriting": 0, "edge": 0})
    got = select_pages.select(pool, dict(select_pages.QUOTAS), seed=1)
    assert len(got) == 2
    assert all(e["page_type"] == "body" for e in got)
    recon = select_pages.reconcile(pool, dict(select_pages.QUOTAS), got)
    assert recon["body"] == {"quota": 50, "available": 2, "selected": 2, "shortfall": 48}
    assert recon["table"]["shortfall"] == 25 and recon["table"]["available"] == 0
    assert recon["seal_handwriting"]["shortfall"] == 15
    assert recon["edge"]["shortfall"] == 10


def test_select_same_seed_reproducible():
    pool = _pool({"body": 60, "table": 30, "seal_handwriting": 20, "edge": 12})
    a = select_pages.select(pool, dict(select_pages.QUOTAS), seed=7)
    b = select_pages.select(pool, dict(select_pages.QUOTAS), seed=7)
    assert a == b  # 同 seed 两次选样逐条一致（清单可复现）


def test_select_no_duplicates_and_subset_of_pool():
    pool = _pool({"body": 60, "table": 30, "seal_handwriting": 20, "edge": 12})
    got = select_pages.select(pool, dict(select_pages.QUOTAS), seed=9)
    ids = [(e["case_ref"], e["file"], e["page"]) for e in got]
    assert len(ids) == len(set(ids))  # 无重复选入
    pool_ids = {(e["case_ref"], e["file"], e["page"]) for e in pool}
    assert set(ids) <= pool_ids


def test_select_does_not_mutate_input_pool():
    pool = _pool({"body": 5, "table": 5, "seal_handwriting": 5, "edge": 5})
    snapshot = [dict(e) for e in pool]
    select_pages.select(pool, dict(select_pages.QUOTAS), seed=3)
    assert pool == snapshot  # 入选理由只写选出副本，候选池不被原地改写


def test_select_zero_quota_cell_selects_nothing():
    quotas = {"body": 0, "table": 0, "seal_handwriting": 0, "edge": 0}
    pool = _pool({"body": 5, "table": 5, "seal_handwriting": 5, "edge": 5})
    assert select_pages.select(pool, quotas, seed=5) == []


def test_select_rejects_bad_quotas():
    pool = _pool({"body": 1, "table": 1, "seal_handwriting": 1, "edge": 1})
    with pytest.raises(ValueError):
        select_pages.select(pool, {"body": 50, "table": 25, "seal_handwriting": 15}, 0)  # 缺 edge
    with pytest.raises(ValueError):
        select_pages.select(pool, {**dict(select_pages.QUOTAS), "misc": 1}, 0)  # 多余键
    with pytest.raises(ValueError):
        select_pages.select(pool, {**dict(select_pages.QUOTAS), "body": -1}, 0)  # 负配额
    with pytest.raises(ValueError):
        select_pages.select(pool, {**dict(select_pages.QUOTAS), "body": True}, 0)  # bool 非配额


def test_select_rejects_bad_pool_entries():
    quotas = dict(select_pages.QUOTAS)
    bad_type = _make_entry(page_type="封面")
    with pytest.raises(ValueError):
        select_pages.select([bad_type], quotas, 0)
    bad_key = {"case_ref": "c", "file": "f" * 64, "page": 0, "segment": "first"}  # 缺 page_type
    with pytest.raises(ValueError):
        select_pages.select([bad_key], quotas, 0)


def test_reconcile_deterministic_cell_order():
    pool = _pool({"body": 1, "table": 1, "seal_handwriting": 1, "edge": 1})
    got = select_pages.select(pool, dict(select_pages.QUOTAS), 0)
    recon = select_pages.reconcile(pool, dict(select_pages.QUOTAS), got)
    assert list(recon) == sorted(gt_schema.PAGE_TYPES)  # 对账表格序确定（可 diff）
    assert all(set(v) == {"quota", "available", "selected", "shortfall"} for v in recon.values())


# ---- build_pool：树遍历 / 大卷切段 / case_ref 编号（pypdf 分支 skipif 守卫） -----

def test_build_pool_walks_tree_case_refs_and_mapping(tmp_path):
    src = tmp_path / "src"
    (src / "卷一").mkdir(parents=True)
    _make_pdf(src / "卷一" / "正文材料.pdf", text="Body text page for selection")
    (src / "卷一" / "假扫描材料.pdf").write_bytes(b"not a pdf at all")
    (src / "stray.txt").write_text("不是 pdf，不收")
    pool, mapping = select_pages.build_pool(src)
    # 无 pypdf：两文件各 1 候选；有 pypdf：假扫描开卷失败仍 1 候选 → 两环境同形
    assert len(pool) == 2
    assert {e["page_type"] for e in pool} == {"body"}
    assert all(e["page"] == 0 and e["segment"] == "first" for e in pool)
    refs = [e["case_ref"] for e in pool]
    assert refs == sorted(refs) and len(set(refs)) == 2  # 顺序透明编号，一一对应
    assert set(mapping) == set(refs)
    for ref, info in mapping.items():
        assert set(info) == {"path", "sha256", "pages_total", "classifier"}
        assert Path(info["path"]).is_file()
        assert info["sha256"] == hashlib.sha256(Path(info["path"]).read_bytes()).hexdigest()
        # 分类器按文件有效性分档：真 PDF → 有 pypdf 记 "pypdf"，否则退化；假字节恒退化
        expected = EXPECTED_CLASSIFIER if Path(info["path"]).name == "正文材料.pdf" else "hints-only"
        assert info["classifier"] == expected


@pytest.mark.skipif(not HAS_PYPDF, reason="大卷切段需 pypdf 数页数（离线环境跳过）")
def test_build_pool_big_volume_first_mid_segments(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    _make_pdf(src / "big.pdf", pages=12)
    _make_pdf(src / "small.pdf", pages=9)
    pool, mapping = select_pages.build_pool(src)
    big = [e for e in pool if mapping[e["case_ref"]]["path"].endswith("big.pdf")]
    small = [e for e in pool if mapping[e["case_ref"]]["path"].endswith("small.pdf")]
    assert [(e["page"], e["segment"]) for e in big] == [(0, "first"), (6, "mid")]
    assert [(e["page"], e["segment"]) for e in small] == [(i, "first") for i in range(9)]
    assert mapping[big[0]["case_ref"]]["pages_total"] == 12


def test_build_pool_rejects_missing_src(tmp_path):
    with pytest.raises(ValueError):
        select_pages.build_pool(tmp_path / "不存在")


def test_build_hint_re_merges_and_validates():
    assert select_pages.build_hint_re(None).pattern == select_pages._HINT_RE.pattern
    merged = select_pages.build_hint_re("情况说明")
    assert merged.search("/x/询问笔录.pdf") and merged.search("/x/情况说明.pdf")
    with pytest.raises(ValueError):
        select_pages.build_hint_re("([)")  # 非法正则显性报错（CLI 退非零码）


# ---- --out 防入仓守卫（铁律 1：sidecar 载真实案名，不得落仓内） --------------------

def test_ensure_outside_repo_allows_path_outside_fake_repo(tmp_path):
    fake_repo = tmp_path / "repo"  # 假仓根（不要求真实存在，仅作边界）
    out = tmp_path / "私有" / "清单.json"
    got = select_pages.ensure_outside_repo(out, repo_root=fake_repo)
    assert got == out.resolve()  # 返回解析后绝对路径
    assert fake_repo not in got.parents


def test_ensure_outside_repo_rejects_paths_inside_fake_repo(tmp_path):
    fake_repo = tmp_path / "repo"
    fake_repo.mkdir()
    for inside in (fake_repo,                             # 恰为仓根本身
                   fake_repo / "清单.json",               # 仓根直下
                   fake_repo / "子目录" / "m.mapping.json"):  # 仓内深层
        with pytest.raises(ValueError, match="不得落在仓库内"):
            select_pages.ensure_outside_repo(inside, repo_root=fake_repo)


def test_cli_rejects_out_inside_real_repo_root(tmp_path):
    # 端到端：--out 指向真仓根内路径 → argparse error（SystemExit 2），不落盘
    src = tmp_path / "src"
    src.mkdir()
    inside = REPO / "gt-select-守卫不许落盘.json"
    with pytest.raises(SystemExit) as ei:
        select_pages.main(["--src", str(src), "--out", str(inside), "--seed", "1"])
    assert ei.value.code == 2
    assert not inside.exists()  # 守卫先于一切写盘动作


# ---- CLI main：端到端（tmp 合成树 → 私有清单 + sidecar 映射 + 对账表） -----------

def _e2e_tree(src: Path) -> None:
    (src / "案卷甲测试名").mkdir(parents=True)
    _make_pdf(src / "案卷甲测试名" / "正文材料.pdf", text="Body text page for selection")
    _make_pdf(src / "案卷甲测试名" / "询问笔录材料.pdf", text="Body text page for selection")
    (src / "案卷甲测试名" / "UPPER.PDF").write_bytes(b"not a pdf at all")
    (src / "案卷乙测试名").mkdir()
    (src / "案卷乙测试名" / "plain_bytes.pdf").write_bytes(b"not a pdf at all")
    (src / "案卷乙测试名" / "备注.txt").write_text("非 pdf 不收")


def test_cli_end_to_end_manifest_and_sidecar(tmp_path):
    src, out = tmp_path / "src", tmp_path / "out" / "manifest.json"
    _e2e_tree(src)
    rc = select_pages.main(["--src", str(src), "--out", str(out), "--seed", "42"])
    assert rc == 0
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["kind"] == "gt-selection-manifest" and doc["version"] == 1
    assert doc["classifier"] == EXPECTED_CLASSIFIER
    assert doc["seed"] == 42
    assert doc["quotas"] == {"body": 50, "table": 25, "seal_handwriting": 15, "edge": 10}

    # 4 份 pdf（.PDF 大写后缀也收）各 1 候选：body×3 + seal×1，全部入选（配额不足全取）
    assert len(doc["selection"]) == 4
    types = sorted(e["page_type"] for e in doc["selection"])
    assert types == ["body", "body", "body", "seal_handwriting"]
    assert all(e["page"] == 0 and e["segment"] == "first" for e in doc["selection"])
    assert all(e["reason"] == f"quota:{e['page_type']}" for e in doc["selection"])

    # 隐私约定：清单只写卷编号/文件哈希/页码——案名与真实路径绝不出现
    manifest_text = out.read_text(encoding="utf-8")
    assert "案卷甲测试名" not in manifest_text and "案卷乙测试名" not in manifest_text
    assert str(src) not in manifest_text
    for e in doc["selection"]:
        assert re.fullmatch(r"case-\d{3}", e["case_ref"])
        assert re.fullmatch(r"[0-9a-f]{64}", e["file"])

    # sidecar 映射：同目录旁挂，卷编号 ↔ 真实路径/哈希，与清单逐条对得上
    sidecar = out.with_name("manifest.mapping.json")
    assert doc["mapping_file"] == sidecar.name and sidecar.is_file()
    mapping = json.loads(sidecar.read_text(encoding="utf-8"))
    assert mapping["kind"] == "gt-selection-case-mapping"
    assert set(mapping["cases"]) == {e["case_ref"] for e in doc["selection"]}
    sidecar_text = sidecar.read_text(encoding="utf-8")
    assert "案卷甲测试名" in sidecar_text and "正文材料.pdf" in sidecar_text
    for e in doc["selection"]:
        info = mapping["cases"][e["case_ref"]]
        assert info["sha256"] == e["file"]
        assert hashlib.sha256(Path(info["path"]).read_bytes()).hexdigest() == e["file"]

    # 配额对账表：available/selected/shortfall 逐格可核
    recon = doc["reconciliation"]
    assert recon["body"] == {"quota": 50, "available": 3, "selected": 3, "shortfall": 47}
    assert recon["seal_handwriting"] == {"quota": 15, "available": 1, "selected": 1,
                                         "shortfall": 14}
    assert recon["table"] == {"quota": 25, "available": 0, "selected": 0, "shortfall": 25}
    assert recon["edge"] == {"quota": 10, "available": 0, "selected": 0, "shortfall": 10}


def test_cli_same_seed_reproducible_outputs(tmp_path):
    src, out1, out2 = tmp_path / "src", tmp_path / "a.json", tmp_path / "b.json"
    _e2e_tree(src)
    assert select_pages.main(["--src", str(src), "--out", str(out1), "--seed", "7"]) == 0
    assert select_pages.main(["--src", str(src), "--out", str(out2), "--seed", "7"]) == 0
    assert json.loads(out1.read_text(encoding="utf-8"))["selection"] == \
        json.loads(out2.read_text(encoding="utf-8"))["selection"]


def test_cli_hints_flag_extends_seal_hints(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    _make_pdf(src / "情况说明.pdf", text="Body text page for selection")
    base = ["--src", str(src), "--seed", "1"]
    out_default = tmp_path / "d.json"
    out_hinted = tmp_path / "h.json"
    assert select_pages.main(base + ["--out", str(out_default)]) == 0
    assert select_pages.main(base + ["--out", str(out_hinted), "--hints", "情况说明"]) == 0
    default = json.loads(out_default.read_text(encoding="utf-8"))["selection"]
    hinted = json.loads(out_hinted.read_text(encoding="utf-8"))["selection"]
    assert [e["page_type"] for e in default] == ["body"]
    assert [e["page_type"] for e in hinted] == ["seal_handwriting"]


def test_cli_error_paths(tmp_path):
    out = tmp_path / "x.json"
    assert select_pages.main(["--src", str(tmp_path / "无此目录"),
                              "--out", str(out), "--seed", "1"]) == 1
    assert not out.exists()  # 失败不落盘
    src = tmp_path / "src"
    src.mkdir()
    assert select_pages.main(["--src", str(src), "--out", str(out),
                              "--seed", "1", "--hints", "([)"]) == 1  # 非法正则
    assert not out.exists()
    with pytest.raises(SystemExit):  # argparse 层：缺 --seed 直接退出（非 main 返回码）
        select_pages.main(["--src", str(src), "--out", str(out)])
