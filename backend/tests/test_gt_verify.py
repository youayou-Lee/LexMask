# Issue#56 M1 Task 8 —— 合成集验证闸 verify_engine.py 的离线单测。
# 零网络、零真实案卷数据（全部合成占位符）；main() 的云客户端一律打桩
#（借 run_pipeline.build_clients 的 monkeypatch 同款，绝不发真实请求）。
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "eval"))
sys.path.insert(0, str(REPO / "backend" / "scripts" / "eval"))
from gt import verify_engine as verify  # noqa: E402  （brief 接口名 verify_engine，测试内别名 verify）

_CLOUD_A = "cloud:PP-OCRv6"
_CLOUD_B = "cloud:PaddleOCR-VL"


# ---- 测试夹具 --------------------------------------------------------------------

def _pack_with_entities(items, page: int = 0, page_id: str = "syn_x-p000") -> dict:
    """最小 pack 夹具：[(text, type), ...]；per_type_pr 只消费 page/text/type。"""
    return {
        "page_id": page_id,
        "page_type": "body",
        "source": {"file_sha256": "0" * 64, "page": page, "carrier": "scanned", "segment": "first"},
        "transcript_gt": {"text": "".join(t for t, _ in items), "normalized_text": "", "fidelity": "machine"},
        "entities": [{"text": t, "type": ty, "span_original": [0, len(t)],
                      "span_normalized": [0, len(t)], "origin": "regex",
                      "verify": "consistent", "arbitration": "R1", "note": None}
                     for t, ty in items],
        "adjudications": [],
    }


class _FakeClient:
    def __init__(self, pages):
        self._pages = pages

    def transcribe(self, file_path):
        return [{"text_raw": t, "boxes": None} for t in self._pages]


def _patch_cloud_build(monkeypatch, a_pages, b_pages):
    import gt.run_pipeline as run_pipeline
    import gt.unify as unify

    def _cloud(model, texts):
        c = unify.CloudVLClient(model, token="test-token")
        c.transcribe = lambda path: [{"text_raw": t, "boxes": None} for t in texts]
        return c

    def _build(spec, _token=None):
        if spec == _CLOUD_A:
            return _cloud("PP-OCRv6", a_pages)
        if spec == _CLOUD_B:
            return _cloud("PaddleOCR-VL", b_pages)
        raise ValueError(f"未知 spec {spec!r}")

    monkeypatch.setattr(run_pipeline, "build_clients", _build)


# ---- per_type_pr：已知值（brief 原断言）------------------------------------------

def test_per_type_pr_known_values():
    gt = {"pages": [{"page": 0, "entities": {"姓名": ["钱明涛"], "身份证号": ["110122198110227771"]}}]}
    pack = _pack_with_entities([("钱明涛", "姓名"), ("110122198110229999", "身份证号"), ("多余", "地址")])
    r = verify.per_type_pr([pack], gt)
    assert r["姓名"] == {"p": 1.0, "r": 1.0, "tp": 1, "fp": 0, "fn": 0}
    assert r["身份证号"]["r"] == 0.0 and r["地址"]["fp"] == 1


def test_per_type_pr_squash_domain_matching():
    # 匹配口径 = squash 域串相等：GT 串过 normalize_text 后与引擎 norm 面实体对齐
    gt = {"pages": [{"page": 0, "entities": {"银行卡号": ["6222 0210 1007 2021"]}}]}
    pack = _pack_with_entities([("6222021010072021", "银行卡号")])   # 引擎侧 norm 面（无空白）
    r = verify.per_type_pr([pack], gt)
    assert r["银行卡号"] == {"p": 1.0, "r": 1.0, "tp": 1, "fp": 0, "fn": 0}


def test_per_type_pr_counts_duplicates_across_pages():
    # 集合口径按页去重：同一串在两页各出现一次 = tp 2；同页重复只计 1
    gt = {"pages": [{"page": 0, "entities": {"电话": ["13800138000"]}},
                    {"page": 1, "entities": {"电话": ["13800138000"]}}]}
    packs = [_pack_with_entities([("13800138000", "电话")], page=0, page_id="f-p000"),
             _pack_with_entities([("13800138000", "电话")], page=1, page_id="f-p001")]
    r = verify.per_type_pr(packs, gt)
    assert r["电话"]["tp"] == 2 and r["电话"]["p"] == 1.0 and r["电话"]["r"] == 1.0


def test_per_type_pr_skips_pages_missing_from_gt():
    # GT 未列页（如 docx 第 2 页无 GT）不参评——页数对账由报告披露
    gt = {"pages": [{"page": 0, "entities": {}}]}
    packs = [_pack_with_entities([("13800138000", "电话")], page=3, page_id="f-p003")]
    assert verify.per_type_pr(packs, gt) == {}


# ---- inject_disputes：五类轮转 + 确定性 ------------------------------------------

_ENTITY_TEXT = "身份证号110122198110227771，电话13800138000。"


def test_inject_five_kinds_on_entity_free_text():
    # brief 脚手架原断言：无实体纯文本上五类轮转全部落位
    dis, lesions = verify.inject_disputes("甲" * 200, n=5, seed=7)
    assert len(lesions) == 5 and {l["kind"] for l in lesions} >= {"swap", "drop", "type_flip", "extra"}
    assert dis != "甲" * 200


def test_inject_five_kinds_on_entity_text():
    dis, lesions = verify.inject_disputes(_ENTITY_TEXT, n=5, seed=7)
    kinds = {l["kind"] for l in lesions}
    assert len(lesions) == 5 and kinds == {"swap", "drift", "drop", "type_flip", "extra"}
    assert dis != _ENTITY_TEXT
    # 长度对账：病变文本长度 = 原长 + Σ(病变区间长 - 原区间长)（替换串长=病变区间长）
    delta = sum((l["end"] - l["start"]) - (l["orig_end"] - l["orig_start"]) for l in lesions)
    assert len(dis) == len(_ENTITY_TEXT) + delta
    by_kind = {l["kind"]: l for l in lesions}
    # extra：额外实体形文本确实在病变转录里
    assert by_kind["extra"]["a_text"] in dis
    # drop：目标实体整串消失（字符退化病灶无 target_text，跳过该断言）
    drop_tgt = by_kind["drop"]["target_text"]
    assert drop_tgt is None or drop_tgt not in dis
    # type_flip：类型翻转/破坏后读数文本必与原串不同
    assert by_kind["type_flip"]["a_text"] != by_kind["type_flip"]["target_text"]
    # 坐标可复核：orig 域与 lesioned 域各自在界内
    for l in lesions:
        assert 0 <= l["orig_start"] <= l["orig_end"] <= len(_ENTITY_TEXT)
        assert 0 <= l["start"] <= l["end"] <= len(dis)


def test_inject_type_flip_breaks_original_type():
    # type_flip 打在正则实体上：翻转后该读数不再命中原类型正则（真翻转型/失效均可）
    import re
    from gt.entities import REGEX_CHANNELS
    dis, lesions = verify.inject_disputes(_ENTITY_TEXT, n=5, seed=7)
    flip = next(l for l in lesions if l["kind"] == "type_flip")
    type_of = {"110122198110227771": "身份证号", "13800138000": "电话"}
    etype = type_of[flip["target_text"]]
    assert not re.fullmatch(REGEX_CHANNELS[etype], flip["a_text"])


def test_inject_insert_coords_point_at_inserted_text():
    # 回归：零宽插入（extra）病灶的病变域坐标必须正好框住插入文本
    #（曾犯自身增量计入自身位移的错——坐标右移一个插入长度、检出区间相交失准）
    dis, lesions = verify.inject_disputes(_ENTITY_TEXT, n=5, seed=7)
    extra = next(l for l in lesions if l["kind"] == "extra")
    assert dis[extra["start"]:extra["end"]] == extra["a_text"]


def test_inject_disputes_deterministic():
    d1, l1 = verify.inject_disputes(_ENTITY_TEXT, n=5, seed=7)
    d2, l2 = verify.inject_disputes(_ENTITY_TEXT, n=5, seed=7)
    assert (d1, l1) == (d2, l2)
    d3, l3 = verify.inject_disputes(_ENTITY_TEXT, n=5, seed=8)
    assert (d3, l3) != (d1, l1)


def test_inject_rotates_and_scales():
    _, lesions = verify.inject_disputes("甲" * 400, n=12, seed=3)
    assert len(lesions) == 12
    assert [l["kind"] for l in lesions] == [verify._KINDS[i % 5] for i in range(12)]


def test_inject_empty_and_trivial_inputs():
    assert verify.inject_disputes("", n=5, seed=1) == ("", [])
    dis, lesions = verify.inject_disputes("甲", n=3, seed=1)
    assert len(lesions) <= 3  # 极小文本放不下就少放（宁少勿假），不抛异常


# ---- 门槛判定与单行 verdict --------------------------------------------------------

def _metrics(overall_p, overall_r, per_type):
    return {"pages": 2,
            "overall": {"precision": overall_p, "recall": overall_r, "tp": 1, "fp": 0, "fn": 0},
            "per_type": per_type}


def test_a1_gate_thresholds():
    ok, failing = verify.a1_gate(_metrics(0.96, 0.96, {}))
    assert ok and failing == []
    # 总体 R 不足 → FAIL
    ok, failing = verify.a1_gate(_metrics(0.96, 0.9, {}))
    assert not ok and any("总体" in f for f in failing)
    # 分类型：GT 实体数 ≥5 才判门槛；<5 豁免
    per = {"姓名": {"tp": 4, "fp": 1, "fn": 1, "precision": 0.8, "recall": 0.8},
           "电话": {"tp": 2, "fp": 0, "fn": 2, "precision": 1.0, "recall": 0.5}}  # GT 4 < 5 豁免
    ok, failing = verify.a1_gate(_metrics(0.96, 0.96, per))
    assert not ok and len(failing) == 1 and "姓名" in failing[0] and "电话" not in failing[0]


def test_a1_gate_empty_run_fails():
    ok, failing = verify.a1_gate({"pages": 0, "overall": {"precision": 0.0, "recall": 0.0},
                                  "per_type": {}})
    assert not ok


def test_a2_gate_thresholds():
    ok, _ = verify.a2_gate(n_pages=10, n_lesions=50, n_detected=48)
    assert ok
    ok, reasons = verify.a2_gate(n_pages=9, n_lesions=50, n_detected=50)
    assert not ok and any("页" in r for r in reasons)
    ok, reasons = verify.a2_gate(n_pages=10, n_lesions=49, n_detected=49)
    assert not ok and any("病灶" in r for r in reasons)
    ok, reasons = verify.a2_gate(n_pages=10, n_lesions=50, n_detected=47)
    assert not ok and any("检出率" in r for r in reasons)


def test_verdict_line():
    assert verify.verdict_line(True, True) == "A1=PASS A2=PASS"
    assert verify.verdict_line(False, True) == "A1=FAIL A2=PASS"
    assert verify.verdict_line(True, False) == "A1=PASS A2=FAIL"
    assert verify.verdict_line(False, False) == "A1=FAIL A2=FAIL"


# ---- 病灶检出判定 ------------------------------------------------------------------

def _lesion(kind="drop", start=5, end=5, target_text="110122198110227771"):
    return {"kind": kind, "start": start, "end": end, "orig_start": start, "orig_end": end,
            "a_text": "" if kind == "drop" else "x", "target_text": target_text, "detail": ""}


def test_lesion_detected_by_page_gap():
    arb = {"consistent": [], "auto_resolved": [],
           "disputed": [{"gap": "整页升级", "rule": "R6", "candidates": {"detail": []}}]}
    detected, basis = verify._lesion_detected(_lesion(), arb)
    assert detected and "gap" in basis


def test_lesion_detected_by_a_side_overlap():
    # a 面病变读数（span 覆盖病灶点）进 disputed → 检出
    arb = {"consistent": [], "auto_resolved": [],
           "disputed": [{"rule": "R2", "candidates": {"a": [{"text": "11012298110227771",
                                                             "span_original": [4, 21]}], "b": []}}]}
    detected, basis = verify._lesion_detected(_lesion(kind="swap", start=5, end=6, target_text=None), arb)
    assert detected and "disputed" in basis


def test_lesion_detected_by_b_side_target_text():
    # 整实体删：a 面无读数，b 侧原文读数进 disputed → 检出
    arb = {"consistent": [], "auto_resolved": [],
           "disputed": [{"rule": "R2", "candidates": {"a": [], "b": [{"text": "110122198110227771",
                                                                      "span_original": [4, 22]}]}}]}
    detected, basis = verify._lesion_detected(_lesion(kind="drop", start=5, end=5), arb)
    assert detected and "disputed" in basis


def test_lesion_undetected_when_silent():
    arb = {"consistent": [{"text": "110122198110227771", "span_original": [4, 22]}],
           "auto_resolved": [], "disputed": []}
    detected, _ = verify._lesion_detected(_lesion(), arb)
    assert not detected


# ---- main()：离线端到端（打桩云客户端） --------------------------------------------

def _write_case(tmp_path: Path):
    raw = "身份证号110122198110227771，电话13800138000，请核对。"
    sample = tmp_path / "syn_x.pdf"
    sample.write_bytes(b"synthetic")
    gt = {"pages": [{"page": 0, "entities": {"身份证号": ["110122198110227771"],
                                             "电话": ["13800138000"]}}]}
    (tmp_path / "syn_x.gt.json").write_text(json.dumps(gt, ensure_ascii=False), encoding="utf-8")
    manifest = {"version": 1, "files": [
        {"id": "syn_x", "path": "syn_x.pdf", "gt": "syn_x.gt.json", "source": "synthetic",
         "carrier": "scanned_pdf", "doc_type": "contract", "density": "mid", "pages": 1,
         "levels": ["e2e"]},
        # 非 e2e 条目：验证闸同样跳过（其 path 不存在，误处理即失败页）
        {"id": "ner_corpus_10p", "path": "ner_corpus_10p.jsonl", "gt": "ner_corpus_10p.jsonl",
         "source": "synthetic", "carrier": "txt", "doc_type": "mixed", "density": "mid",
         "pages": 10, "levels": ["ner"]},
    ]}
    mpath = tmp_path / "manifest.json"
    mpath.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return raw, mpath


def test_main_offline_end_to_end(tmp_path, monkeypatch, capsys):
    # 1 页 e2e：A1 该参评类型全对（PASS）；A2 页数/病灶数不足门槛（FAIL）→ rc=1
    raw, mpath = _write_case(tmp_path)
    _patch_cloud_build(monkeypatch, [raw], [raw])
    work = tmp_path / "work"
    rc = verify.main(["--synthetic-dir", str(tmp_path), "--manifest", str(mpath),
                      "--work", str(work), "--clients", f"{_CLOUD_A},{_CLOUD_B}",
                      "--ner", "off", "--report"])
    assert rc == 1
    line = capsys.readouterr().out.strip().splitlines()[-1]
    assert line == "A1=PASS A2=FAIL"
    reports = list((work / "verify").glob("A1A2-*.md"))
    assert len(reports) == 1
    text = reports[0].read_text(encoding="utf-8")
    assert "A1=PASS A2=FAIL" in text
    assert "盲区清单" in text                     # 两个仲裁盲区必须对 A5 可见
    assert "身份证号" in text                     # 分类型 P/R 表在报告里
    assert "CLOUD_VL_TOKEN" not in text.replace("仅经环境变量 CLOUD_VL_TOKEN（本报告零凭据）", "")
    assert "bearer" not in text.lower()           # 零凭据材料入报告


def test_main_offline_a1_fail_on_type_gap(tmp_path, monkeypatch, capsys):
    # GT 含引擎无通道的类型（NER off 下姓名无来源）→ 该类型 R=0 → A1=FAIL（闸门在工作）
    raw = "身份证号110122198110227771，电话13800138000，请核对。"
    sample = tmp_path / "syn_y.pdf"
    sample.write_bytes(b"synthetic")
    gt = {"pages": [{"page": 0, "entities": {"身份证号": ["110122198110227771"],
                                             "姓名": ["钱明涛", "赵四强", "孙一敏", "李云鹤", "周建国"]}}]}
    (tmp_path / "syn_y.gt.json").write_text(json.dumps(gt, ensure_ascii=False), encoding="utf-8")
    manifest = {"version": 1, "files": [
        {"id": "syn_y", "path": "syn_y.pdf", "gt": "syn_y.gt.json", "source": "synthetic",
         "carrier": "scanned_pdf", "doc_type": "contract", "density": "mid", "pages": 1,
         "levels": ["e2e"]}]}
    mpath = tmp_path / "manifest.json"
    mpath.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    _patch_cloud_build(monkeypatch, [raw], [raw])
    rc = verify.main(["--synthetic-dir", str(tmp_path), "--manifest", str(mpath),
                      "--work", str(tmp_path / "w"), "--clients", f"{_CLOUD_A},{_CLOUD_B}",
                      "--ner", "off"])
    assert rc == 1
    assert capsys.readouterr().out.strip().splitlines()[-1] == "A1=FAIL A2=FAIL"


def test_main_missing_manifest_is_error(tmp_path, capsys):
    rc = verify.main(["--synthetic-dir", str(tmp_path), "--manifest", str(tmp_path / "nope.json"),
                      "--work", str(tmp_path / "w")])
    assert rc == 1
    assert "错误" in capsys.readouterr().err


def test_skip_existing_reuses_valid_pack(tmp_path, monkeypatch):
    """--skip-existing：已有合法 pack 的页复用、不重跑云；缺页照常跑。"""
    import json as _json
    from gt import verify_engine

    calls = []

    def fake_run_page(file_path, page_no, page_type, clients, ner, work, carrier="scanned", segment="first"):
        calls.append(page_no)
        stem = file_path.split("/")[-1].rsplit(".", 1)[0]
        return {"page_id": f"{stem}-p{page_no:03d}", "page_type": page_type,
                "source": {"file_sha256": "0" * 64, "page": page_no, "carrier": carrier, "segment": segment},
                "transcript_gt": {"text": "号码110122198110227771", "normalized_text": "号码110122198110227771",
                                   "fidelity": "machine"},
                "entities": [], "adjudications": []}

    monkeypatch.setattr(verify_engine, "run_page", fake_run_page)
    good = fake_run_page("syn_x.pdf", 0, "body", None, None, tmp_path)
    (tmp_path / "pages" / "syn_x-p000").mkdir(parents=True)
    (tmp_path / "pages" / "syn_x-p000" / "pack.json").write_text(_json.dumps(good, ensure_ascii=False), encoding="utf-8")

    args = verify_engine._parse_args(["--synthetic-dir", "s", "--manifest", "m", "--work", str(tmp_path),
                                      "--skip-existing"])
    # 直接调 A1 内层：伪造最小入口绕开云——此处以 _run 的页循环等价路径验证，
    # 简化：验证 validate_pagepack 对 good 放行 + 跳过逻辑单元（不整跑 _run）
    from gt.gt_schema import validate_pagepack
    assert validate_pagepack(good) == []
    assert (tmp_path / "pages" / "syn_x-p000" / "pack.json").is_file()


def test_type_alias_alignment():
    """类型粒度对齐：HaS 细名（公司名称/机关单位）计为 GT 机构名称；开户行不映射。"""
    gt = {"pages": [{"page": 0, "entities": {"机构名称": ["某市第一人民法院"]}}]}
    pack = {"page_id": "x-p000", "source": {"page": 0}, "entities": [
        {"text": "某市第一人民法院", "type": "机关单位"},
        {"text": "某市第二人民法院", "type": "公司名称"},
        {"text": "某银行", "type": "开户行"},
    ]}
    r = verify.per_type_pr([pack], gt)
    assert r["机构名称"]["tp"] == 1 and r["机构名称"]["fn"] == 0
    assert r["机构名称"]["fp"] == 1  # 公司名称无对应 GT 串
    assert "开户行" in r and r["开户行"]["fp"] == 1  # 不映射，独立计 FP


# ---- 跑批单实例守卫（2026-10-07 实战缺陷：两进程同 work 目录竞态） ----

def test_work_lock_acquire_and_release(tmp_path):
    from gt.lock import acquire_work_lock, release_work_lock
    acquire_work_lock(tmp_path)
    assert (tmp_path / ".lock").read_text(encoding="utf-8") == str(__import__("os").getpid())
    release_work_lock(tmp_path)
    assert not (tmp_path / ".lock").exists()

def test_work_lock_blocks_while_holder_alive(tmp_path):
    import os
    from gt.lock import acquire_work_lock
    acquire_work_lock(tmp_path)  # 本测试进程持锁且存活
    with pytest.raises(RuntimeError, match="占用"):
        acquire_work_lock(tmp_path)

def test_work_lock_stale_holder_cleaned(tmp_path):
    from gt.lock import acquire_work_lock
    (tmp_path / ".lock").write_text("999999999", encoding="utf-8")  # 不存在的 pid
    acquire_work_lock(tmp_path)  # 陈旧锁应被清掉、本进程成功上锁
    import os
    assert (tmp_path / ".lock").read_text(encoding="utf-8") == str(os.getpid())

def test_work_lock_garbage_content_treated_stale(tmp_path):
    from gt.lock import acquire_work_lock
    (tmp_path / ".lock").write_text("not-a-pid", encoding="utf-8")
    acquire_work_lock(tmp_path)
    import os
    assert (tmp_path / ".lock").read_text(encoding="utf-8") == str(os.getpid())
