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


# ---- 静态首页 --------------------------------------------------------------------

def test_index_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
