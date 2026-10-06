# Issue#56 M1 Task 7 —— 逐页预标流水线 pagepack.py（run_page）与 CLI
# run_pipeline.py 的离线单测。
# 零网络、零真实案卷数据（全部合成占位符）；转录一律 _FakeClient / 打桩的
# CloudVLClient.transcribe，绝不实例化可用网络的真实客户端发起请求
#（CloudVLClient 仅借构造器做 isinstance 判型，transcribe 被测试替换）。
#
# 与计划底稿脚手架的差异（机械修正，实现 API 不变，见 task-7-report）：
#   底稿 run_page("syn_x.pdf", ...) 传入不存在的文件路径——run_page 需对原件
#   算 file_sha256（页 ID 回溯锚点，D 闸），文件缺失必须 fail-fast，故测试
#   改为在 tmp_path 下真实落一个 syn_x.pdf 再传路径。
import hashlib
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "eval"))
from gt import gt_schema, pagepack, run_pipeline  # noqa: E402
from gt import unify  # noqa: E402
from gt.entities import NEROff  # noqa: E402
from gt.normalize import FaceMap  # noqa: E402


# ---- 测试夹具 ------------------------------------------------------------------

class _FakeClient:
    """测试夹具：实现 TranscriptionClient，transcribe() 返回给定页文本。"""

    def __init__(self, pages):
        self._pages = pages
        self.calls: list[str] = []

    def transcribe(self, file_path):
        self.calls.append(file_path)
        return [{"text_raw": t, "boxes": None} for t in self._pages]


def _fake_clients(a_pages, b_pages, md_pages=None):
    clients = {"a": _FakeClient(a_pages), "b": _FakeClient(b_pages)}
    if md_pages is not None:
        clients["md"] = _FakeClient(md_pages)
    return clients


def _make_sample(tmp_path: Path, name: str = "syn_x.pdf", content: bytes = b"synthetic") -> Path:
    """真实落盘样本文件（run_page 要算 file_sha256，文件必须存在）。"""
    p = tmp_path / name
    p.write_bytes(content)
    return p


# R3 场景文本：双云身份证号读数分歧、电话一致；md 与 b 侧同读 → R3 采 VL 面。
_A_RAW = "身份证号 110122198110227771，电话13800138000，签名。"
_B_RAW = "身份证号 310110199203144512，电话13800138000，签名。"


# ---- run_page：pack 组装与落盘 ---------------------------------------------------

def test_run_page_produces_valid_pack(tmp_path):
    sample = _make_sample(tmp_path)
    work = tmp_path / "work"
    clients = _fake_clients(["甲行 乙行"], ["甲行乙行"])
    pack = pagepack.run_page(str(sample), 0, "body", clients, NEROff(), work)
    assert gt_schema.validate_pagepack(pack) == []
    assert (work / "pages" / pack["page_id"] / "pack.json").exists()


def test_run_page_pack_contract_fields(tmp_path):
    sample = _make_sample(tmp_path, content=b"abc123")
    pack = pagepack.run_page(str(sample), 7, "body", _fake_clients(["甲行 乙行"], ["甲行乙行"]),
                             NEROff(), tmp_path / "w", carrier="scanned_pdf", segment="mid")
    assert pack["page_id"] == "syn_x-p007"  # f"{stem}-p{page_no:03d}"
    assert pack["page_type"] == "body"
    assert pack["source"] == {"file_sha256": hashlib.sha256(b"abc123").hexdigest(),
                              "page": 7, "carrier": "scanned_pdf", "segment": "mid"}
    assert pack["transcript_gt"] == {"text": "甲行 乙行",  # 默认采 a 侧原文
                                     "normalized_text": "甲行乙行",
                                     "fidelity": "machine"}
    # pack.json 内容与返回值一致（中文不转义）
    disk = json.loads((tmp_path / "w" / "pages" / "syn_x-p007" / "pack.json")
                      .read_text(encoding="utf-8"))
    assert disk == pack


def test_run_page_no_md_client_means_md_silent(tmp_path):
    sample = _make_sample(tmp_path)
    pack = pagepack.run_page(str(sample), 0, "body", _fake_clients([_A_RAW], [_B_RAW]),
                             NEROff(), tmp_path / "w")
    # md 缺席 → R5（转录分歧未获实体级解释）：采 a 侧、全部独有读数 disputed 留痕
    assert pack["transcript_gt"]["text"] == _A_RAW
    assert all(adj["rule"] in ("R1", "R2", "R5") for adj in pack["adjudications"])
    assert any(adj["verdict"] == "disputed" and adj["rule"] == "R5"
               for adj in pack["adjudications"])
    assert all("vl-md" not in adj["models"] for adj in pack["adjudications"])


# ---- run_page：仲裁采信面与实体装配 ---------------------------------------------

def test_run_page_r3_adopts_vl_side(tmp_path):
    sample = _make_sample(tmp_path)
    pack = pagepack.run_page(str(sample), 0, "body",
                             _fake_clients([_A_RAW], [_B_RAW], md_pages=[_B_RAW]),
                             NEROff(), tmp_path / "w")
    # R3：md 确认 b(VL) 独有读数 → 采 VL 面
    assert pack["transcript_gt"]["text"] == _B_RAW
    assert pack["transcript_gt"]["normalized_text"] == FaceMap.from_raw(_B_RAW).norm
    assert pack["transcript_gt"]["fidelity"] == "machine"
    # 实体落在采信面：b 侧身份证（R3 采信）+ 双侧一致电话（R1）
    types = {(e["type"], e["text"]) for e in pack["entities"]}
    assert ("身份证号", "310110199203144512") in types
    assert ("电话", "13800138000") in types
    assert all("110122198110227771" not in e["text"] for e in pack["entities"])  # 被否读数不进实体
    by_rule = {(e["type"], e["text"]): e["verify"] for e in pack["entities"]}
    assert by_rule[("身份证号", "310110199203144512")] == "arbitrated"
    assert by_rule[("电话", "13800138000")] == "consistent"
    arb_tag = {(e["type"], e["text"]): e["arbitration"] for e in pack["entities"]}
    assert arb_tag[("身份证号", "310110199203144512")] == "R3"
    assert arb_tag[("电话", "13800138000")] == "R1"
    # 裁决留痕：auto:b / 被否 a 读数进 disputed candidates（A4 无静默丢弃）
    assert any(adj["rule"] == "R3" and adj["verdict"] == "auto:b"
               for adj in pack["adjudications"])
    rejected = [adj for adj in pack["adjudications"]
                if adj["verdict"] == "disputed" and adj["rule"] == "R3"]
    assert rejected and any(
        any(e["text"] == "110122198110227771" for e in adj["candidates"]["a"])
        for adj in rejected)
    assert all("vl-md" in adj["models"] for adj in pack["adjudications"])


def test_run_page_r6_edge_adopts_a_side_no_entities(tmp_path):
    sample = _make_sample(tmp_path)
    pack = pagepack.run_page(str(sample), 0, "edge", _fake_clients(["甲"], ["甲水印字"]),
                             NEROff(), tmp_path / "w")
    # R6：单方多字整页升级——零采信、全部读数 disputed 留痕、仍采 a 侧文本
    assert pack["transcript_gt"]["text"] == "甲"
    assert pack["entities"] == []
    rules = {adj["rule"] for adj in pack["adjudications"]}
    assert rules == {"R6"}
    assert all(adj["verdict"] == "disputed" for adj in pack["adjudications"])
    assert any("gap" in adj for adj in pack["adjudications"])  # 页级 gap 记录


def test_run_page_r7_format_residue_auto_ok(tmp_path):
    a_raw = "电话13800138000请联系代理律师张三丰办理相关手续事宜。"
    b_raw = "电话13800138000请联系#代理律师张三丰办理相关手续事宜。"
    sample = _make_sample(tmp_path)
    pack = pagepack.run_page(str(sample), 0, "body", _fake_clients([a_raw], [b_raw]),
                             NEROff(), tmp_path / "w")
    # R7：剥行中 # 残差后全等 → 自动放行，采 a 侧，同读实体 arbitrated(R7)
    assert pack["transcript_gt"]["text"] == a_raw
    assert len(pack["entities"]) == 1
    ent = pack["entities"][0]
    assert (ent["type"], ent["text"]) == ("电话", "13800138000")
    assert ent["verify"] == "arbitrated" and ent["arbitration"] == "R7"


def test_run_page_dual_face_spans(tmp_path):
    # 全角数字原文 → norm 面半角命中；span_original 落在原文面（全角区）、
    # span_normalized 落在归一化面（半角区），两面各自的切片回读对得上。
    raw = "身份证：１１０１２２１９８１１０２２７７７１号"
    sample = _make_sample(tmp_path)
    pack = pagepack.run_page(str(sample), 0, "body", _fake_clients([raw], [raw]),
                             NEROff(), tmp_path / "w")
    assert gt_schema.validate_pagepack(pack) == []
    ent = next(e for e in pack["entities"] if e["type"] == "身份证号")
    text, norm = pack["transcript_gt"]["text"], pack["transcript_gt"]["normalized_text"]
    o0, o1 = ent["span_original"]
    n0, n1 = ent["span_normalized"]
    assert text[o0:o1] == "１１０１２２１９８１１０２２７７７１"   # 原文面（全角）
    assert norm[n0:n1] == ent["text"] == "110122198110227771"  # 归一化面（半角）


def test_run_page_does_not_mutate_arbitration_output(tmp_path, monkeypatch):
    # T5 裁定：仲裁输出实体浅拷贝、T7 只读——pack 结构必须新建，不得原地改写
    sample = _make_sample(tmp_path)
    captured = {}
    orig = pagepack.arbitrate_page

    def _spy(cmp, ents_a, ents_b, ents_md, page_type):
        arb = orig(cmp, ents_a, ents_b, ents_md, page_type)
        captured["arb"] = arb
        return arb

    monkeypatch.setattr(pagepack, "arbitrate_page", _spy)
    raw = "身份证：１１０１２２１９８１１０２２７７７１号"
    pack = pagepack.run_page(str(sample), 0, "body", _fake_clients([raw], [raw]),
                             NEROff(), tmp_path / "w")
    arb = captured["arb"]
    assert arb["consistent"], "前置：一致桶非空"
    for src in arb["consistent"]:
        assert "verify" not in src and "arbitration" not in src  # 仲裁输出未被改写
    for ent in pack["entities"]:
        assert all(ent is not src for src in arb["consistent"])  # pack 实体是新对象


# ---- run_page：页选择与 fail-fast ------------------------------------------------

def test_run_page_multipage_selection(tmp_path):
    sample = _make_sample(tmp_path)
    pages = ["第一页文本", "第二页文本", "第三页文本"]
    pack = pagepack.run_page(str(sample), 2, "body", _fake_clients(pages, pages),
                             NEROff(), tmp_path / "w")
    assert pack["transcript_gt"]["text"] == "第三页文本"  # len>1 → pages[page_no]


def test_run_page_single_page_ignores_page_no(tmp_path):
    sample = _make_sample(tmp_path)
    pack = pagepack.run_page(str(sample), 5, "body", _fake_clients(["唯一页"], ["唯一页"]),
                             NEROff(), tmp_path / "w")
    assert pack["transcript_gt"]["text"] == "唯一页"  # len==1 → pages[0]


def test_run_page_page_no_out_of_range(tmp_path):
    sample = _make_sample(tmp_path)
    pages = ["一", "二", "三"]
    with pytest.raises(ValueError, match="页"):
        pagepack.run_page(str(sample), 9, "body", _fake_clients(pages, pages),
                          NEROff(), tmp_path / "w")


def test_run_page_unknown_page_type_rejected(tmp_path):
    sample = _make_sample(tmp_path)
    with pytest.raises(ValueError, match="页型"):
        pagepack.run_page(str(sample), 0, "empty", _fake_clients(["甲"], ["甲"]),
                          NEROff(), tmp_path / "w")


def test_run_page_requires_both_cloud_channels(tmp_path):
    sample = _make_sample(tmp_path)
    clients = {"a": _FakeClient(["甲"])}  # 缺 b
    with pytest.raises(ValueError, match="clients"):
        pagepack.run_page(str(sample), 0, "body", clients, NEROff(), tmp_path / "w")


def test_run_page_missing_sample_file_fails_fast(tmp_path):
    # sha256 是页 ID 回溯锚点：文件不存在必须报错，不能静默产出 pack
    clients = _fake_clients(["甲"], ["甲"])
    with pytest.raises(OSError):
        pagepack.run_page(str(tmp_path / "nope.pdf"), 0, "body", clients,
                          NEROff(), tmp_path / "w")


def test_run_page_self_gate_refuses_invalid_pack(tmp_path):
    # 产出前自校验（错误拒绝落盘，warning 放行）：注入非法仲裁输出（类型不在 preset）
    sample = _make_sample(tmp_path)
    raw = "电话13800138000联系代理律师张三丰办理相关手续事宜等事项"
    clients = _fake_clients([raw], [raw])
    orig = pagepack.arbitrate_page

    def _bad_arbitrate(cmp, ents_a, ents_b, ents_md, page_type):
        return {"consistent": [],
                "auto_resolved": [{"entity": {"text": "13800138000", "type": "不存在的类型",
                                              "span_original": [2, 13],
                                              "span_normalized": [2, 13],
                                              "origin": "regex"},
                                   "rule": "R2", "source": "a"}],
                "disputed": []}

    pagepack.arbitrate_page = _bad_arbitrate
    try:
        with pytest.raises(ValueError, match="校验失败"):
            pagepack.run_page(str(sample), 0, "body", clients, NEROff(), tmp_path / "w")
    finally:
        pagepack.arbitrate_page = orig
    assert not (tmp_path / "w" / "pages").exists()  # 坏包不落盘


# ---- HTTPNERClient（端点适配） ---------------------------------------------------

class _FakeResp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body if body is not None else {}
        self.text = json.dumps(self._body, ensure_ascii=False)

    def json(self):
        return self._body


def test_http_ner_client_roundtrip(tmp_path, monkeypatch):
    seen = {}

    def _post(url, json=None, timeout=None):
        seen["url"], seen["json"], seen["timeout"] = url, json, timeout
        return _FakeResp(body={"entities": {"姓名": ["张三"], "电话": []}})

    monkeypatch.setattr(pagepack.requests, "post", _post)
    ner = pagepack.HTTPNERClient("http://127.0.0.1:9999/ner")
    assert ner.ner("张三的电话") == {"姓名": ["张三"], "电话": []}
    assert seen["url"] == "http://127.0.0.1:9999/ner"
    assert seen["json"]["text"] == "张三的电话"
    assert seen["json"]["types"] == sorted(gt_schema.PRESET_TYPE_NAMES)  # preset 名全集


def test_http_ner_client_shape_mismatch(monkeypatch):
    monkeypatch.setattr(pagepack.requests, "post",
                        lambda *a, **k: _FakeResp(body={"entities": "not-a-dict"}))
    ner = pagepack.HTTPNERClient("http://127.0.0.1:9999/ner")
    with pytest.raises(RuntimeError, match="形状"):
        ner.ner("文本")


def test_http_ner_client_http_error(monkeypatch):
    monkeypatch.setattr(pagepack.requests, "post",
                        lambda *a, **k: _FakeResp(status=500, body={"detail": "boom"}))
    ner = pagepack.HTTPNERClient("http://127.0.0.1:9999/ner")
    with pytest.raises(RuntimeError, match="500"):
        ner.ner("文本")


def test_http_ner_client_custom_types():
    ner = pagepack.HTTPNERClient("http://x", types=["姓名"])
    assert ner.types == ["姓名"]


# ---- CachedTranscriptionClient（批量防重复云作业） -------------------------------

def test_cached_transcription_client_transcribes_once(tmp_path):
    inner = _FakeClient(["甲", "乙"])
    cached = pagepack.CachedTranscriptionClient(inner)
    first = cached.transcribe("a.pdf")
    second = cached.transcribe("a.pdf")
    assert inner.calls == ["a.pdf"]  # 同一路径只透传一次
    assert first is second
    cached.transcribe("b.pdf")
    assert inner.calls == ["a.pdf", "b.pdf"]  # 换路径重新透传


# ---- CLI：单页模式 ---------------------------------------------------------------

_CLOUD_A = "cloud:PP-OCRv6"
_CLOUD_B = "cloud:PaddleOCR-VL"


def _patch_cloud_build(monkeypatch, a_pages, b_pages, md_pages=None):
    """替换 run_pipeline.build_clients：真实 CloudVLClient/LocalVLClient 判型 +
    transcribe 打桩（绝不发网络请求）。"""

    def _cloud(model, texts):
        c = unify.CloudVLClient(model, token="test-token")
        c.transcribe = lambda path: [{"text_raw": t, "boxes": None} for t in texts]
        return c

    def _build(spec, _token=None):
        if spec == _CLOUD_A:
            return _cloud("PP-OCRv6", a_pages)
        if spec == _CLOUD_B:
            return _cloud("PaddleOCR-VL", b_pages)
        if spec.startswith("vlmd:"):
            c = unify.LocalVLClient(spec.split(":", 1)[1])
            c.transcribe = lambda path: [{"text_raw": t, "boxes": None} for t in md_pages]
            return c
        raise ValueError(f"未知 spec {spec!r}")

    monkeypatch.setattr(run_pipeline, "build_clients", _build)


def test_cli_single_mode_writes_pack(tmp_path, monkeypatch):
    sample = _make_sample(tmp_path)
    work = tmp_path / "work"
    _patch_cloud_build(monkeypatch, [_A_RAW], [_B_RAW])
    rc = run_pipeline.main(["--sample", str(sample), "--page", "0", "--page-type", "body",
                            "--clients", f"{_CLOUD_A},{_CLOUD_B}", "--ner", "off",
                            "--work", str(work)])
    assert rc == 0
    pack = json.loads((work / "pages" / "syn_x-p000" / "pack.json").read_text(encoding="utf-8"))
    assert pack["source"]["carrier"] == "scanned"   # 默认值
    assert pack["source"]["segment"] == "first"


def test_cli_single_mode_carrier_segment_override(tmp_path, monkeypatch):
    sample = _make_sample(tmp_path, name="s.pdf")
    _patch_cloud_build(monkeypatch, [_A_RAW], [_B_RAW], md_pages=[_B_RAW])
    rc = run_pipeline.main(["--sample", str(sample), "--page", "0", "--page-type", "body",
                            "--clients", f"{_CLOUD_A},{_CLOUD_B},vlmd:http://127.0.0.1:8095",
                            "--ner", "off", "--work", str(tmp_path / "w"),
                            "--carrier", "scanned_pdf", "--segment", "mid"])
    assert rc == 0
    pack = json.loads((tmp_path / "w" / "pages" / "s-p000" / "pack.json").read_text(encoding="utf-8"))
    assert pack["source"]["carrier"] == "scanned_pdf"
    assert pack["source"]["segment"] == "mid"
    assert all("vl-md" in adj["models"] for adj in pack["adjudications"])


def test_cli_ner_base_builds_http_client(tmp_path, monkeypatch):
    sample = _make_sample(tmp_path, name="n.pdf")
    _patch_cloud_build(monkeypatch, ["甲行乙行"], ["甲行乙行"])
    assert isinstance(run_pipeline.build_ner("http://127.0.0.1:9999", None),
                      pagepack.HTTPNERClient)
    assert isinstance(run_pipeline.build_ner(None, "off"), NEROff)
    assert isinstance(run_pipeline.build_ner(None, None), NEROff)  # 缺省即关闭（零网络）


def test_cli_ner_flags_mutually_exclusive(tmp_path):
    with pytest.raises(SystemExit):
        run_pipeline.main(["--sample", "x.pdf", "--ner-base", "http://x", "--ner", "off",
                           "--work", str(tmp_path)])


def test_cli_requires_sample_or_suite(tmp_path):
    with pytest.raises(SystemExit):
        run_pipeline.main(["--work", str(tmp_path)])


def test_cli_clients_require_both_clouds(tmp_path, monkeypatch):
    sample = _make_sample(tmp_path, name="c.pdf")
    _patch_cloud_build(monkeypatch, ["甲"], ["甲"])

    def _build(spec, _token=None):
        if spec == _CLOUD_A:
            c = unify.CloudVLClient("PP-OCRv6", token="t")
            c.transcribe = lambda path: [{"text_raw": "甲", "boxes": None}]
            return c
        raise ValueError(spec)

    monkeypatch.setattr(run_pipeline, "build_clients", _build)
    rc = run_pipeline.main(["--sample", str(sample), "--clients", _CLOUD_A,
                            "--ner", "off", "--work", str(tmp_path / "w")])
    assert rc == 1  # 缺 b 通道：报错退出，不产 pack
    assert not (tmp_path / "w" / "pages").exists()


# ---- CLI：批量模式（synthetic manifest） -----------------------------------------

def _write_manifest(tmp_path: Path, entries: list[dict]) -> Path:
    manifest = {"version": 1, "files": entries}
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return path


def test_cli_batch_manifest_runs_all_pages(tmp_path, monkeypatch):
    _make_sample(tmp_path, name="bank.pdf")
    _make_sample(tmp_path, name="contract.pdf")
    _make_sample(tmp_path, name="edge.pdf")
    manifest = _write_manifest(tmp_path, [
        {"id": "syn_bank_1p", "path": "bank.pdf", "carrier": "scanned_pdf",
         "doc_type": "bank_statement", "density": "dense", "pages": 2},
        {"id": "syn_contract_1p", "path": "contract.pdf", "carrier": "text_pdf",
         "doc_type": "contract", "density": "mid", "pages": 1},
        {"id": "syn_edge_3p_mixed", "path": "edge.pdf", "carrier": "scanned_pdf",
         "doc_type": "contract", "density": "mid", "pages": 1},
    ])
    bank_raw = "户名张三账号6222020200112233445余额1000"
    _patch_cloud_build(monkeypatch, [bank_raw], [bank_raw])
    work = tmp_path / "work"
    rc = run_pipeline.main(["--suite", "synthetic", "--manifest", str(manifest),
                            "--clients", f"{_CLOUD_A},{_CLOUD_B}", "--ner", "off",
                            "--work", str(work)])
    assert rc == 0
    # 页数对账：2 + 1 + 1 = 4 页，全部落 pack 且校验干净
    packs = {}
    for pack_json in sorted((work / "pages").glob("*/pack.json")):
        pack = json.loads(pack_json.read_text(encoding="utf-8"))
        assert gt_schema.validate_pagepack(pack) == []
        packs[pack["page_id"]] = pack
    assert set(packs) == {"bank-p000", "bank-p001",  # page_id = 文件名 stem-pNNN
                          "contract-p000", "edge-p000"}
    assert packs["bank-p000"]["page_type"] == "table"              # bank_statement → table
    assert packs["bank-p000"]["source"]["carrier"] == "scanned_pdf"  # manifest 透传
    assert [e["type"] for e in packs["bank-p000"]["entities"]] == ["银行卡号"]
    assert packs["contract-p000"]["page_type"] == "body"           # 其余 → body
    assert packs["edge-p000"]["page_type"] == "edge"               # edge 样本 → edge


def test_cli_batch_maps_edge_and_continues_on_failure(tmp_path, monkeypatch, capsys):
    _make_sample(tmp_path, name="ok.pdf")  # bad.pdf 故意不落盘 → 该文件各页失败
    manifest = _write_manifest(tmp_path, [
        {"id": "f_ok", "path": "ok.pdf", "carrier": "scanned_pdf",
         "doc_type": "contract", "density": "mid", "pages": 1},
        {"id": "f_bad", "path": "bad.pdf", "carrier": "scanned_pdf",
         "doc_type": "contract", "density": "mid", "pages": 2},
    ])
    _patch_cloud_build(monkeypatch, ["甲"], ["甲"])
    work = tmp_path / "work"
    rc = run_pipeline.main(["--suite", "synthetic", "--manifest", str(manifest),
                            "--clients", f"{_CLOUD_A},{_CLOUD_B}", "--ner", "off",
                            "--work", str(work)])
    assert rc == 1  # 有失败页 → 非零退出
    assert (work / "pages" / "ok-p000" / "pack.json").exists()  # 成功页不受牵连
    assert not (work / "pages" / "bad-p000").exists()
    err = capsys.readouterr().err
    assert "总 3 页" in err and "成功 1" in err and "失败 2" in err  # 末尾计数对账


def test_cli_batch_summary_on_success(tmp_path, monkeypatch, capsys):
    _make_sample(tmp_path, name="ok.pdf")
    manifest = _write_manifest(tmp_path, [
        {"id": "f_ok", "path": "ok.pdf", "carrier": "text_pdf",
         "doc_type": "contract", "density": "mid", "pages": 2},
    ])
    _patch_cloud_build(monkeypatch, ["甲行乙行"], ["甲行乙行"])
    rc = run_pipeline.main(["--suite", "synthetic", "--manifest", str(manifest),
                            "--clients", f"{_CLOUD_A},{_CLOUD_B}", "--ner", "off",
                            "--work", str(tmp_path / "w")])
    assert rc == 0
    err = capsys.readouterr().err
    assert "总 2 页" in err and "成功 2" in err and "失败 0" in err


def test_batch_missing_manifest_file_is_error(tmp_path, capsys):
    # manifest 缺失 → CLI 边界吃掉异常、stderr 报错、非零退出（不产 pack）
    rc = run_pipeline.main(["--suite", "synthetic", "--manifest", str(tmp_path / "nope.json"),
                            "--work", str(tmp_path / "w")])
    assert rc == 1
    assert "错误" in capsys.readouterr().err
    assert not (tmp_path / "w" / "pages").exists()


def test_cli_batch_requires_manifest(tmp_path):
    with pytest.raises(SystemExit):
        run_pipeline.main(["--suite", "synthetic", "--work", str(tmp_path)])
