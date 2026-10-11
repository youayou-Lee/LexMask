# Issue#56 M3 Task 3+4 —— 工作台 HTTP 服务 workbench_server.py 与页面渲染器
# render.py 的离线单测（FastAPI TestClient，零网络、零真实案卷数据）。
# 覆盖：全部端点正路径 + 400（中文错误）/ 409（缺项清单）负路径 + 渲染器
# 可选依赖降级（无 pypdfium2 返回 False；有则真渲染，skipif 门控同 T9）。
import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "eval"))
from gt import render, workbench, workbench_server  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

HAS_PYPDFIUM2 = importlib.util.find_spec("pypdfium2") is not None


# ---- 夹具：最小合法 pack（合成占位符，同 test_gt_workbench 模式） ----------------

def _pack_p1():  # body 页：2 个 consistent 实体 + 2 条 disputed 仲裁（各挂一条）
    return {
        "page_id": "p1", "page_type": "body",
        "source": {"file_sha256": "0" * 64, "page": 0,
                   "carrier": "text_pdf", "segment": "first"},
        "transcript_gt": {"text": "张三号码110122198110227771",
                          "normalized_text": "张三号码110122198110227771",
                          "fidelity": "machine"},
        "entities": [
            {"text": "张三", "type": "姓名",
             "span_original": [0, 2], "span_normalized": [0, 2],
             "origin": "regex", "verify": "consistent", "note": None},
            {"text": "110122198110227771", "type": "身份证号",
             "span_original": [4, 22], "span_normalized": [4, 22],
             "origin": "regex", "verify": "consistent", "note": None}],
        "adjudications": [
            {"models": ["a", "b"], "rule": "R6", "verdict": "disputed",
             "candidates": {"a": [{"text": "张三", "type": "姓名",
                                   "span_original": [0, 2]}],
                            "b": []}},
            {"models": ["a", "b"], "rule": "R5", "verdict": "disputed",
             "candidates": {"a": [{"text": "110122198110227771", "type": "身份证号",
                                   "span_original": [4, 22]}],
                            "b": [{"text": "110122198110227771", "type": "手机号",
                                   "span_original": [4, 22]}]}}]}


def _pack_p2():  # table 页：3 个同型 consistent 实体（抽样/finalize 用）
    text = "甲乙丙丁戊己庚辛壬癸"
    ents = [{"text": c, "type": "姓名",
             "span_original": [i, i + 1], "span_normalized": [i, i + 1],
             "origin": "regex", "verify": "consistent", "note": None}
            for i, c in enumerate("甲乙丙")]
    return {
        "page_id": "p2", "page_type": "table",
        "source": {"file_sha256": "1" * 64, "page": 1,
                   "carrier": "text_pdf", "segment": "second"},
        "transcript_gt": {"text": text, "normalized_text": text,
                          "fidelity": "machine"},
        "entities": ents,
        "adjudications": []}


@pytest.fixture()
def work(tmp_path):
    for pid, pack in (("p1", _pack_p1()), ("p2", _pack_p2())):
        d = tmp_path / "pages" / pid
        d.mkdir(parents=True)
        (d / "pack.json").write_text(json.dumps(pack, ensure_ascii=False),
                                     encoding="utf-8")
    return tmp_path


@pytest.fixture()
def client(work):
    return TestClient(workbench_server.create_app(work))


# ---- Task 3: 只读端点 ------------------------------------------------------------

def test_stats_and_disputes(client):
    r = client.get("/api/stats")
    assert r.status_code == 200
    assert r.json()["consistent"] == 5
    r = client.get("/api/disputes")
    assert r.status_code == 200
    ds = r.json()
    assert list(ds) == ["p1"] and len(ds["p1"]) == 2


def test_page_detail(client):
    r = client.get("/api/page/p1")
    assert r.status_code == 200
    body = r.json()
    assert body["page_id"] == "p1"
    assert body["image_url"] == "/img/p1"
    assert len(body["entities"]) == 2


def test_page_not_found(client):
    r = client.get("/api/page/nope")
    assert r.status_code == 404
    assert "error" in r.json()


# ---- Task 3: resolve / undo（含 400 负路径） --------------------------------------

def test_resolve_ok_and_dispute_closes(client, work):
    r = client.post("/api/resolve", json={
        "page_id": "p1", "entity_index": 0, "verdict": "对",
        "correct": None, "note": None})
    assert r.status_code == 200
    assert r.json()["verify"] == "user-confirmed"
    pack = json.loads((work / "pages/p1/pack.json").read_text(encoding="utf-8"))
    assert pack["adjudications"][0]["verdict"] == "user:对"
    assert client.get("/api/disputes").json()["p1"]  # 另一条 disputed 仍在


def test_resolve_bad_verdict_400_chinese(client):
    r = client.post("/api/resolve", json={
        "page_id": "p1", "entity_index": 0, "verdict": "maybe",
        "correct": None, "note": None})
    assert r.status_code == 400
    assert "verdict" in r.json()["error"]


def test_resolve_missing_page_400(client):
    r = client.post("/api/resolve", json={
        "page_id": "ghost", "entity_index": 0, "verdict": "对",
        "correct": None, "note": None})
    assert r.status_code == 400
    assert "ghost" in r.json()["error"]


def test_resolve_wrong_needs_correct_400(client):
    r = client.post("/api/resolve", json={
        "page_id": "p1", "entity_index": 0, "verdict": "错",
        "correct": None, "note": None})
    assert r.status_code == 400
    assert "correct" in r.json()["error"]


def test_undo_roundtrip_and_empty_400(client, work):
    client.post("/api/resolve", json={"page_id": "p1", "entity_index": 0,
                                      "verdict": "对", "correct": None,
                                      "note": None})
    r = client.post("/api/undo")
    assert r.status_code == 200
    pack = json.loads((work / "pages/p1/pack.json").read_text(encoding="utf-8"))
    assert pack["entities"][0]["verify"] == "consistent"
    r = client.post("/api/undo")  # resolve 已被标记撤销：journal 已无可撤
    assert r.status_code == 400
    assert "error" in r.json()


# ---- Task 3: sample / sample-verdict / trust / journal ----------------------------

def test_sample_trust_journal_flow(client, work):
    r = client.post("/api/sample", json={"ratio": 0.5, "seed": 7})
    assert r.status_code == 200
    assert r.json()["seed"] == 7
    assert (work / "sample_seed.json").is_file()
    sample = r.json()
    assert sample["selected"]
    pid, idx = sample["selected"][0]
    r = client.post("/api/sample-verdict", json={
        "page_id": pid, "entity_index": idx, "ok": True, "correct": None})
    assert r.status_code == 200
    assert "trust" in r.json()
    r = client.get("/api/trust")
    assert r.status_code == 200 and r.json()["checked"] >= 1
    r = client.get("/api/journal")
    assert r.status_code == 200
    assert [ln["op"] for ln in r.json()] == ["resolve"]


def test_sample_bad_ratio_400(client):
    assert client.post("/api/sample", json={"ratio": 0, "seed": 1}).status_code == 400


def test_sample_verdict_not_in_sample_400(client):
    client.post("/api/sample", json={"ratio": 0.5, "seed": 7})
    r = client.post("/api/sample-verdict", json={
        "page_id": "p2", "entity_index": 99, "ok": True, "correct": None})
    assert r.status_code == 400


# ---- Task 3: finalize（409 缺项清单 / 落 gt_v1.jsonl） -----------------------------

def test_finalize_conflict_lists_missing(client):
    r = client.post("/api/finalize")
    assert r.status_code == 409
    missing = r.json()["missing"]
    assert any("未裁决" in m for m in missing)
    assert any("抽检" in m for m in missing)


def test_finalize_after_full_resolution(client, work):
    # 正确裁决路径：p1 两条 disputed 分别挂实体 0（张三/姓名）与实体 1（身份证）
    for idx in (0, 1):
        r = client.post("/api/resolve", json={
            "page_id": "p1", "entity_index": idx, "verdict": "对",
            "correct": None, "note": None})
        assert r.status_code == 200, r.text
    assert client.get("/api/disputes").json() == {}
    r = client.post("/api/finalize")
    assert r.status_code == 409  # 还差抽检
    assert any("抽检" in m for m in r.json()["missing"])
    # 抽检并全部确认
    sample = client.post("/api/sample", json={"ratio": 1.0, "seed": 42}).json()
    for pid, idx in sample["selected"]:
        r = client.post("/api/sample-verdict", json={
            "page_id": pid, "entity_index": idx, "ok": True, "correct": None})
        assert r.status_code == 200, r.text
    r = client.post("/api/finalize")
    assert r.status_code == 200, r.text
    out = work / "gt_v1.jsonl"
    assert out.is_file()
    rows = [json.loads(ln) for ln in
            out.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert {row["page_id"] for row in rows} == {"p1", "p2"}
    assert all(row["gt_version"] == "v1" for row in rows)


def test_finalize_invalid_pack_400_not_500(client, work):
    """定稿前从未被触过的页此时才首次校验：非法 pack → 400 带中文错误（非 500）。

    非法页须避开抽检硬门禁（I1 终审修复：sample-verdict 写回即触发 pack 校验，
    抽中的非法页会先死在抽检）——故用 0 实体 + 非法仲裁 rule 的页（无一致集
    实体、永不入样），校验错误只在 finalize 的 write_gt_jsonl 首次暴露。
    """
    pack = _pack_p2()
    pack["entities"] = []
    pack["adjudications"] = [{"models": ["v6", "vl"], "rule": "R99",
                              "verdict": "consistent"}]
    (work / "pages/p2/pack.json").write_text(json.dumps(pack, ensure_ascii=False),
                                             encoding="utf-8")
    for idx in (0, 1):
        assert client.post("/api/resolve", json={
            "page_id": "p1", "entity_index": idx, "verdict": "对",
            "correct": None, "note": None}).status_code == 200
    assert client.post("/api/sample", json={"ratio": 1.0, "seed": 42}).status_code == 200
    r = client.post("/api/finalize")
    assert r.status_code == 400
    assert "校验失败" in r.json()["error"] or "R99" in r.json()["error"]


def test_img_bad_sha_404(work):
    """source.file_sha256 非 64 位十六进制 → 404（不构造路径）。"""
    pdf_root = work / "pdfs"
    pdf_root.mkdir()
    pack = _pack_p1()
    pack["source"]["file_sha256"] = "../escape"
    d = work / "pages" / "p1"
    (d / "pack.json").write_text(json.dumps(pack, ensure_ascii=False),
                                 encoding="utf-8")
    client = TestClient(workbench_server.create_app(work, pdf_root=pdf_root))
    r = client.get("/img/p1")
    assert r.status_code == 404
    assert "file_sha256" in r.json()["error"]


# ---- 终审修复波（2026-10-07）：真实引擎形状契约 + finalize 硬门禁 ------------------

def _pack_gap():  # R6 整页升级真实形状：0 实体 + gap 条目（candidates=detail 字符串）
    return {
        "page_id": "pg", "page_type": "body",
        "source": {"file_sha256": "3" * 64, "page": 3,
                   "carrier": "scanned", "segment": "first"},
        "transcript_gt": {"text": "甲乙丙丁", "normalized_text": "甲乙丙丁",
                          "fidelity": "machine"},
        "entities": [],
        "adjudications": [
            {"models": ["v6", "vl"], "rule": "R6", "verdict": "disputed",
             "candidates": {"detail": ["单侧多出：v6 面读数「戊」、VL 面无"]},
             "gap": "单方多字/集合不合，整页升级（R6）"},
            {"models": ["v6", "vl"], "rule": "R6", "verdict": "disputed",
             "candidates": {"a": [{"text": "戊", "type": "姓名",
                                   "span_original": [3, 4]}],
                            "b": [], "md": []}}]}


def _pack_r3():  # R3 采 VL 面真实形状：被否 a 读数留痕、不在 entities
    return {
        "page_id": "pr", "page_type": "table",
        "source": {"file_sha256": "4" * 64, "page": 4,
                   "carrier": "text_pdf", "segment": "first"},
        "transcript_gt": {"text": "甲乙丙丁", "normalized_text": "甲乙丙丁",
                          "fidelity": "machine"},
        "entities": [
            {"text": "甲乙", "type": "姓名",
             "span_original": [0, 2], "span_normalized": [0, 2],
             "origin": "regex", "verify": "consistent", "note": None},
            {"text": "丙", "type": "姓名",
             "span_original": [2, 3], "span_normalized": [2, 3],
             "origin": "regex", "verify": "arbitrated", "arbitration": "R3",
             "note": None}],
        "adjudications": [
            {"models": ["v6", "vl"], "rule": "R1", "verdict": "consistent"},
            {"models": ["v6", "vl", "vl-md"], "rule": "R3", "verdict": "auto:b"},
            {"models": ["v6", "vl", "vl-md"], "rule": "R3", "verdict": "disputed",
             "candidates": {"a": [{"text": "戊", "type": "姓名",
                                   "span_original": [3, 4]}],
                            "b": [], "md": []}}]}


@pytest.fixture()
def work_real(tmp_path):
    for pid, pack in (("pg", _pack_gap()), ("pr", _pack_r3())):
        d = tmp_path / "pages" / pid
        d.mkdir(parents=True)
        (d / "pack.json").write_text(json.dumps(pack, ensure_ascii=False),
                                     encoding="utf-8")
    return tmp_path


@pytest.fixture()
def client_real(work_real):
    return TestClient(workbench_server.create_app(work_real))


def test_api_accepts_ack_and_miss_on_real_shapes(client_real, work_real):
    """C1#5 服务端契约钉：gap 卡与 0 实体页的「机器正确」「漏」API 全收。"""
    r = client_real.post("/api/resolve", json={
        "page_id": "pg", "entity_index": None, "adjudication_index": 0,
        "verdict": "ack", "correct": None, "note": None})
    assert r.status_code == 200, r.text
    assert r.json()["verify"] is None
    r = client_real.post("/api/resolve", json={
        "page_id": "pg", "entity_index": None, "adjudication_index": 1,
        "verdict": "漏",
        "correct": {"text": "戊", "type": "姓名",
                    "span_original": [3, 4], "span_normalized": [3, 4]},
        "note": None})
    assert r.status_code == 200, r.text
    assert r.json()["verify"] == "user-confirmed"
    pack = json.loads((work_real / "pages/pg/pack.json").read_text(encoding="utf-8"))
    assert len(pack["entities"]) == 1 and pack["entities"][0]["origin"] == "user"
    assert [a["verdict"] for a in pack["adjudications"]] == ["user:ack", "user:漏"]
    ds = client_real.get("/api/disputes").json()
    assert list(ds) == ["pr"] and len(ds["pr"]) == 1  # 仅剩被否 R3 条目
    # 被否 R3 读数同样 ack 可关
    r = client_real.post("/api/resolve", json={
        "page_id": "pr", "entity_index": None, "adjudication_index": 2,
        "verdict": "ack", "correct": None, "note": None})
    assert r.status_code == 200, r.text
    assert client_real.get("/api/disputes").json() == {}


def test_finalize_gate_requires_full_review(client_real, work_real):
    """I1：抽检硬门禁——0 复审 → 409 报未复审计数；全部复审后放行。

    分歧只用 ack/漏 关（回归钉：仅 ack/漏 关单即可走到抽检与定稿）。
    """
    for body in (
        {"page_id": "pg", "entity_index": None, "adjudication_index": 0,
         "verdict": "ack", "correct": None, "note": None},
        {"page_id": "pg", "entity_index": None, "adjudication_index": 1,
         "verdict": "漏",
         "correct": {"text": "戊", "type": "姓名",
                     "span_original": [3, 4], "span_normalized": [3, 4]},
         "note": None},
        {"page_id": "pr", "entity_index": None, "adjudication_index": 2,
         "verdict": "ack", "correct": None, "note": None},
    ):
        assert client_real.post("/api/resolve", json=body).status_code == 200, body
    sample = client_real.post("/api/sample", json={"ratio": 1.0, "seed": 42}).json()
    total = len(sample["selected"])
    assert total >= 1
    r = client_real.post("/api/finalize")
    assert r.status_code == 409
    missing = r.json()["missing"]
    assert any(f"未复审 {total}" in m and f"共 {total} 条" in m for m in missing), missing
    # 复审一条后计数递减
    pid, idx = sample["selected"][0]
    client_real.post("/api/sample-verdict", json={
        "page_id": pid, "entity_index": idx, "ok": True, "correct": None})
    r = client_real.post("/api/finalize")
    if total > 1:
        assert r.status_code == 409
        assert any(f"未复审 {total - 1}" in m for m in r.json()["missing"]), r.text
    for pid, idx in sample["selected"][1:]:
        r = client_real.post("/api/sample-verdict", json={
            "page_id": pid, "entity_index": idx, "ok": True, "correct": None})
        assert r.status_code == 200, r.text
    r = client_real.post("/api/finalize")
    assert r.status_code == 200, r.text
    # 落盘内容抽钉：仅 ack/漏 关单的页定稿在案（gt_v1.jsonl 5 行 → 2 页）
    out = work_real / "gt_v1.jsonl"
    assert out.is_file()
    rows = [json.loads(ln) for ln in
            out.read_text(encoding="utf-8").splitlines() if ln.strip()]
    by_pid = {row["page_id"]: row for row in rows}
    assert [a["verdict"] for a in by_pid["pg"]["adjudications"]] == ["user:ack", "user:漏"]
    assert by_pid["pr"]["adjudications"][2]["verdict"] == "user:ack"


def test_journal_negative_n_clamped(client):
    """M4：/api/journal n 钳非负——负值不再触发 [-n:] 漂移语义。"""
    client.post("/api/resolve", json={"page_id": "p1", "entity_index": 0,
                                      "verdict": "对", "correct": None,
                                      "note": None})
    r = client.get("/api/journal?n=-5")
    assert r.status_code == 200 and r.json() == []
    r = client.get("/api/journal?n=0")
    assert r.status_code == 200 and r.json() == []
    r = client.get("/api/journal?n=5")
    assert r.status_code == 200 and len(r.json()) == 1


def test_launcher_argparse_wiring(monkeypatch, tmp_path):
    """I4：`python3 -m gt.workbench_server --work ...` 启动器——host 固定回环。"""
    calls = {}

    class FakeUvicorn:
        @staticmethod
        def run(app, host, port, log_level):
            calls["host"], calls["port"] = host, port

    monkeypatch.setitem(sys.modules, "uvicorn", FakeUvicorn)
    rc = workbench_server._main(["--work", str(tmp_path), "--port", "1234"])
    assert rc == 0
    assert calls == {"host": "127.0.0.1", "port": 1234}
    with pytest.raises(SystemExit):
        workbench_server._main(["--work", str(tmp_path), "--pdf-root", "x",
                                "--port", "0", "--bogus"])


# ---- 静态首页 --------------------------------------------------------------------

def test_index_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


def test_index_contains_key_element_ids(client):
    """Task 5 前端骨架：关键元素 id 必须在首页 HTML 中（离线 DOM 结构自检）。"""
    html = client.get("/").text
    for eid in ("stats-bar", "mode-toggle", "dispute-list",
                "finalize-btn", "sample-list", "trust-card", "undo-btn"):
        assert f'id="{eid}"' in html, f"首页缺关键元素 id={eid!r}"


def test_index_dispute_highlight_linkage(client):
    """v2 高亮联动：争议候选读数蓝高亮（mk-dispute）、无 DOM 段构造器、
    点卡滚动接线必须在服务出的首页 HTML 中。"""
    html = client.get("/").text
    assert "mark.mk-dispute{" in html             # 蓝色争议读数高亮 CSS
    assert "function transcriptSegments" in html  # 无 DOM 段构造器（自测直跑）
    assert "scrollIntoView" in html               # 点卡片滚动到争议高亮
    assert "firstCandidateSpan" in html           # 候选读数 span 提取


def test_sample_read_endpoint(client):
    """GET /api/sample：抽样前 404 + 中文原因；抽样后返回 seed/selected。"""
    r = client.get("/api/sample")
    assert r.status_code == 404
    assert "error" in r.json()
    saved = client.post("/api/sample", json={"ratio": 0.5, "seed": 7}).json()
    r = client.get("/api/sample")
    assert r.status_code == 200
    assert r.json()["seed"] == 7
    assert r.json()["selected"] == saved["selected"]


def test_trust_before_sample_404(client):
    """Task 5：前端 boot 即拉 /api/trust——无抽样须 404 + 中文原因（非 500）。"""
    r = client.get("/api/trust")
    assert r.status_code == 404
    assert "error" in r.json()


def test_preset_types_endpoint(client):
    """Task 5 前端改判/补漏表单下拉数据源：preset 类型名集（排序、单一事实源）。"""
    r = client.get("/api/preset-types")
    assert r.status_code == 200
    names = r.json()
    assert isinstance(names, list) and names == sorted(names)
    assert "姓名" in names and "身份证号" in names


# ---- Task 4: /img/{page_id}（渲染不可用 → 404 + 原因） -----------------------------

def test_img_without_pdf_root_404(client):
    r = client.get("/img/p1")
    assert r.status_code == 404
    assert "error" in r.json()


def test_img_missing_pdf_file_404(work):
    pdf_root = work / "pdfs"
    pdf_root.mkdir()
    app = workbench_server.create_app(work, pdf_root=pdf_root)
    client = TestClient(app)
    r = client.get("/img/p1")
    assert r.status_code == 404
    assert "error" in r.json()


def test_img_bad_pdf_404_both_envs(work):
    """两环境通用（Important#1 根治）：垃圾字节 PDF 无论有无 pypdfium2 都打不开
    → render False → 404 + 原因（200 真渲染路径走下方 skipif 门控用例）。"""
    pdf_root = work / "pdfs"
    pdf_root.mkdir()
    fake = pdf_root / f"{'0' * 64}.pdf"
    fake.write_bytes(b"not a pdf")
    app = workbench_server.create_app(work, pdf_root=pdf_root)
    client = TestClient(app)
    r = client.get("/img/p1")
    assert r.status_code == 404
    assert "error" in r.json()


@pytest.mark.skipif(not HAS_PYPDFIUM2,
                    reason="pypdfium2 可选依赖未安装（离线环境跳过真渲染 200 路径）")
def test_img_renders_png_with_dep(work):
    """有 pypdfium2 的环境：真 PDF（pypdfium2 自建）→ 200 + PNG + 缓存文件。"""
    import pypdfium2 as pdfium
    pdf_root = work / "pdfs"
    pdf_root.mkdir()
    doc = pdfium.PdfDocument.new()
    doc.new_page(100, 100)
    doc.save(str(pdf_root / f"{'0' * 64}.pdf"))
    app = workbench_server.create_app(work, pdf_root=pdf_root)
    client = TestClient(app)
    r = client.get("/img/p1")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert (pdf_root / "render_cache" / f"{'0' * 64}-p000.png").is_file()


# ---- Task 4: render_page_png 单测 --------------------------------------------------

def test_render_page_png_absent_dep_returns_false(tmp_path):
    """无 pypdfium2 环境：任何输入都优雅返回 False（不抛异常）。"""
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"not a pdf")
    assert render.render_page_png(str(pdf), 0, tmp_path / "out.png") is False


def test_render_page_png_missing_file_returns_false(tmp_path):
    assert render.render_page_png(str(tmp_path / "ghost.pdf"), 0,
                                  tmp_path / "out.png") is False


@pytest.mark.skipif(not HAS_PYPDFIUM2,
                    reason="pypdfium2 可选依赖未安装（离线环境跳过真渲染）")
def test_render_page_png_true_render(tmp_path):
    import pypdfium2 as pdfium  # noqa: F401  真渲染用例
    # 用 pypdfium2 自建一页最小 PDF 再渲染回来
    doc = pdfium.PdfDocument.new()
    doc.new_page(100, 100)
    pdf = tmp_path / "real.pdf"
    doc.save(str(pdf))
    out = tmp_path / "render_cache" / "real-p000.png"
    assert render.render_page_png(str(pdf), 0, out) is True
    assert out.is_file() and out.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


@pytest.mark.skipif(not HAS_PYPDFIUM2,
                    reason="pypdfium2 可选依赖未安装（离线环境跳过）")
def test_render_page_png_render_error_returns_false(tmp_path):
    import pypdfium2
    pdf = tmp_path / "bad.pdf"
    pdf.write_bytes(b"%PDF-1.4 garbage")
    try:
        pypdfium2.PdfDocument(str(pdf))
        can_open = True
    except Exception:
        can_open = False
    if not can_open:
        assert render.render_page_png(str(pdf), 0, tmp_path / "o.png") is False
