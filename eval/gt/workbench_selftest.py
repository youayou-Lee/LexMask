#!/usr/bin/env python3
"""GT 工作台端到端自测（Issue#56 M3 / Task 5）——HTTP 层孪生走查。

对真实 uvicorn 实例（后台线程、随机端口、临时 work 夹具）走一遍完整业务流，
作为 M3 的自动化出口证据；内置浏览器的人工走查由控制器另行执行。

- 夹具：2 页最小合法 pack（合成占位符，模式抄 backend/tests/test_gt_workbench.py；
  R6 分歧的 candidates 额外补一路 ``md`` 读数以覆盖前端 a/b/md 三方并排契约）。
  零真实案卷、零网络外呼（仅 127.0.0.1）、不碰任何 git 仓（铁律 1）。
- 流程：首页关键元素 id → 未抽样 404 口径（/api/trust、/api/sample）→ 一单裁对
  → 一单补漏 → 一单裁错（带 correction）→ 撤销一次（快照还原、分歧复现）→
  finalize 期待 409 缺项清单（含「未裁决」）→ 抽样 → 抽检一错余对（抽检错顺带
  定向关单）→ 可信率已知值 0.75 → finalize 200 + gt_v1.jsonl 落盘。
- 每步打印 PASS/FAIL；全绿 exit 0，任一失败 exit 1。

依赖注记：HTTP 客户端用 httpx（backend/requirements.txt 已携带，fastapi
TestClient 同源）——计划文本写的 requests 未入 requirements，为守「零新依赖」
约束改用 httpx 同步客户端。
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]  # eval/gt/workbench_selftest.py → 仓根
if str(REPO / "eval") not in sys.path:
    sys.path.insert(0, str(REPO / "eval"))

import httpx  # noqa: E402  backend/requirements.txt 携带
import uvicorn  # noqa: E402

from gt import workbench_server  # noqa: E402

# 与 backend/tests/test_gt_workbench_server.py::test_index_contains_key_element_ids 同口径
KEY_IDS = ("stats-bar", "mode-toggle", "dispute-list",
           "sample-list", "trust-card", "undo-btn", "finalize-btn")

FAILURES: list[str] = []


def step(name: str, ok: bool, detail: str = "") -> bool:
    """打印一步 PASS/FAIL；失败记入 FAILURES。"""
    line = f"[{'PASS' if ok else 'FAIL'}] {name}"
    if detail:
        line += f" —— {detail}"
    print(line, flush=True)
    if not ok:
        FAILURES.append(name)
    return ok


# ---- 夹具（模式抄 backend/tests/test_gt_workbench.py） ----------------------------

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
                            "b": [],
                            "md": [{"text": "张三", "type": "姓名",
                                    "span_original": [0, 2]}]}},
            {"models": ["a", "b"], "rule": "R5", "verdict": "disputed",
             "candidates": {"a": [{"text": "110122198110227771", "type": "身份证号",
                                   "span_original": [4, 22]}],
                            "b": [{"text": "110122198110227771", "type": "手机号",
                                   "span_original": [4, 22]}]}}]}


def _pack_p2():  # table 页：3 个同型 consistent 实体（分层抽样/补漏用）
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


def build_work() -> Path:
    """临时工作目录：pages/p1、pages/p2 两份合法 pack。"""
    work = Path(tempfile.mkdtemp(prefix="gt-workbench-selftest-"))
    for pid, pack in (("p1", _pack_p1()), ("p2", _pack_p2())):
        d = work / "pages" / pid
        d.mkdir(parents=True)
        (d / "pack.json").write_text(json.dumps(pack, ensure_ascii=False),
                                     encoding="utf-8")
    return work


def serve(work: Path):
    """后台线程起真实 uvicorn（127.0.0.1 随机端口），返回 (server, thread, base_url)。"""
    app = workbench_server.create_app(work)
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    if not server.started:
        raise RuntimeError("uvicorn 15s 内未完成启动")
    port = server.servers[0].sockets[0].getsockname()[1]
    return server, thread, f"http://127.0.0.1:{port}"


# ---- 全流程 -----------------------------------------------------------------------

def run_flow(client: httpx.Client, work: Path) -> None:
    # 1. 首页骨架：关键元素 id（与红转绿单测同一口径）
    r = client.get("/")
    ok = r.status_code == 200 and "text/html" in r.headers.get("content-type", "")
    step("GET / → 200 text/html", ok, f"status={r.status_code}")
    html = r.text
    missing_ids = [eid for eid in KEY_IDS if f'id="{eid}"' not in html]
    step("首页含关键元素 id（7 个钉子）", not missing_ids,
         f"缺失：{missing_ids}" if missing_ids else "stats-bar 等 7 个全在")

    # 2. 未抽样口径：/api/trust 与 /api/sample 均 404 + 中文原因（前端 boot 依赖）
    r_trust = client.get("/api/trust")
    step("GET /api/trust（未抽样）→ 404 + error",
         r_trust.status_code == 404 and "error" in r_trust.json(),
         r_trust.text if r_trust.status_code != 404 else "")
    r_sample = client.get("/api/sample")
    step("GET /api/sample（未抽样）→ 404 + error",
         r_sample.status_code == 404 and "error" in r_sample.json(),
         r_sample.text if r_sample.status_code != 404 else "")

    # 3. stats / disputes / page 契约
    r = client.get("/api/stats")
    stats = r.json() if r.status_code == 200 else {}
    step("GET /api/stats → consistent=5、user-confirmed=0",
         r.status_code == 200 and stats.get("consistent") == 5
         and stats.get("user-confirmed") == 0, str(stats))
    r = client.get("/api/disputes")
    ds = r.json() if r.status_code == 200 else {}
    step("GET /api/disputes → p1 挂 2 条 disputed",
         r.status_code == 200 and list(ds) == ["p1"] and len(ds.get("p1", [])) == 2,
         str(r.text if r.status_code != 200 else {k: len(v) for k, v in ds.items()}))
    r = client.get("/api/page/p1")
    page = r.json() if r.status_code == 200 else {}
    step("GET /api/page/p1 → image_url=/img/p1、2 条实体",
         r.status_code == 200 and page.get("image_url") == "/img/p1"
         and len(page.get("entities", [])) == 2)

    # 4. /img 无 pdf_root → 404 + 原因（前端转录高亮兜底的触发口径）
    r = client.get("/img/p1")
    step("GET /img/p1（无 pdf_root）→ 404 + error",
         r.status_code == 404 and "error" in r.json(), r.text)

    # 5. preset 类型名集（改判/补漏表单下拉数据源）
    r = client.get("/api/preset-types")
    names = r.json() if r.status_code == 200 else []
    step("GET /api/preset-types → 200 且含 姓名/身份证号",
         r.status_code == 200 and "姓名" in names and "身份证号" in names)

    # 6. 一单裁对：p1 实体0（张三/姓名）→ user-confirmed，分歧 2 → 1
    r = client.post("/api/resolve", json={"page_id": "p1", "entity_index": 0,
                                          "verdict": "对", "correct": None,
                                          "note": None})
    ok = r.status_code == 200 and r.json().get("verify") == "user-confirmed"
    step("裁对 p1#0 → 200 verify=user-confirmed", ok, r.text)
    ds = client.get("/api/disputes").json()
    step("裁对后分歧 2 → 1", len(ds.get("p1", [])) == 1, str({k: len(v) for k, v in ds.items()}))

    # 7. 一单补漏：p2 补「丁」（漏）→ 新增实体 user-confirmed，分歧数不变
    r = client.post("/api/resolve", json={
        "page_id": "p2", "entity_index": 0, "verdict": "漏",
        "correct": {"text": "丁", "type": "姓名",
                    "span_original": [3, 4], "span_normalized": [3, 4]},
        "note": None})
    step("补漏 p2（丁/姓名）→ 200", r.status_code == 200, r.text)
    page2 = client.get("/api/page/p2").json()
    ents2 = page2.get("entities", [])
    step("补漏后 p2 共 4 条实体且末条为 丁/user-confirmed",
         len(ents2) == 4 and ents2[-1]["text"] == "丁"
         and ents2[-1]["verify"] == "user-confirmed")
    ds = client.get("/api/disputes").json()
    step("补漏不关单：分歧仍 1 条", len(ds.get("p1", [])) == 1)

    # 8. 一单裁错（带 correction）：p1 实体1 身份证号 → 电话（采信 b 读数），分歧清零
    r = client.post("/api/resolve", json={
        "page_id": "p1", "entity_index": 1, "verdict": "错",
        "correct": {"text": "110122198110227771", "type": "电话",
                    "span_original": [4, 22], "span_normalized": [4, 22]},
        "note": "采信 b 读数：实为电话号码"})
    ok = r.status_code == 200 and r.json().get("verify") == "user-corrected"
    step("裁错 p1#1（改型 电话）→ 200 verify=user-corrected", ok, r.text)
    ds = client.get("/api/disputes").json()
    step("裁错定向关单后分歧清零", ds == {}, str(ds))

    # 9. 撤销一次：快照还原 → 裁错被回退（实体还原、分歧复现），补漏不受影响
    r = client.post("/api/undo")
    line = r.json() if r.status_code == 200 else {}
    step("POST /api/undo → 200 op=undo（回退最近一条 resolve）",
         r.status_code == 200 and line.get("op") == "undo"
         and line.get("undo_of_op") == "resolve", r.text)
    pack1 = client.get("/api/page/p1").json()
    e1 = pack1.get("entities", [None, {}])[1]
    step("撤销后 p1 实体1 还原（身份证号/consistent）",
         e1 is not None and e1.get("type") == "身份证号"
         and e1.get("verify") == "consistent")
    ds = client.get("/api/disputes").json()
    step("撤销后分歧复现：p1 剩 1 条 disputed", len(ds.get("p1", [])) == 1)
    page2 = client.get("/api/page/p2").json()
    step("撤销不影响补漏：p2 仍 4 条实体", len(page2.get("entities", [])) == 4)

    # 10. finalize 前置不满足 → 409 缺项清单（未裁决 + 抽检未完成）
    r = client.post("/api/finalize")
    missing = r.json().get("missing", []) if r.status_code == 409 else []
    step("finalize（有分歧未抽样）→ 409 缺项清单",
         r.status_code == 409 and any("未裁决" in m for m in missing)
         and any("抽检" in m for m in missing), str(missing))

    # 11. 抽样：ratio=1.0、seed=42 → 4 条一致集实体；body 层排序在前 ⇒ 首条 = [p1, 1]
    r = client.post("/api/sample", json={"ratio": 1.0, "seed": 42})
    sample = r.json() if r.status_code == 200 else {}
    selected = sample.get("selected", [])
    step("POST /api/sample → 200 共 4 条、首条 [p1,1]（分层确定性）",
         r.status_code == 200 and len(selected) == 4 and selected[0] == ["p1", 1],
         str(selected))
    step("sample_seed.json 落盘", (work / "sample_seed.json").is_file())

    # 12. 抽检一错余对：首条（p1#1）判错带 correction，其余判对
    r = client.post("/api/sample-verdict", json={
        "page_id": selected[0][0], "entity_index": selected[0][1], "ok": False,
        "correct": {"text": "110122198110227771", "type": "电话",
                    "span_original": [4, 22], "span_normalized": [4, 22]}})
    ok = r.status_code == 200 and "trust" in r.json()
    step("抽检首条判错（ok=False + correct）→ 200 回带 trust", ok, r.text)
    all_ok = True
    for pid, idx in selected[1:]:
        rr = client.post("/api/sample-verdict", json={
            "page_id": pid, "entity_index": idx, "ok": True, "correct": None})
        all_ok = all_ok and rr.status_code == 200
    step(f"抽检其余 {len(selected) - 1} 条判对 → 全部 200", all_ok)

    # 13. 可信率已知值：3/4 维持原判 = 0.75
    r = client.get("/api/trust")
    trust = r.json() if r.status_code == 200 else {}
    step("GET /api/trust → checked=4 confirmed=3 corrected=1 rate=0.75",
         r.status_code == 200 and trust.get("checked") == 4
         and trust.get("confirmed") == 3 and trust.get("corrected") == 1
         and abs(trust.get("rate", -1) - 0.75) < 1e-9, str(trust))

    # 14. 抽检错顺带定向关单：分歧清零
    ds = client.get("/api/disputes").json()
    step("抽检改判定向关单后分歧清零", ds == {}, str(ds))

    # 15. finalize → 200 + gt_v1.jsonl 落盘（2 页、gt_version=v1、改判/补漏写回在案）
    r = client.post("/api/finalize")
    ok = r.status_code == 200 and r.json().get("ok") is True and r.json().get("pages") == 2
    step("finalize（前置全满足）→ 200 pages=2", ok, r.text)
    out = work / "gt_v1.jsonl"
    step("gt_v1.jsonl 落盘", out.is_file(), str(out))
    rows = [json.loads(ln) for ln in
            out.read_text(encoding="utf-8").splitlines() if ln.strip()] \
        if out.is_file() else []
    row1 = next((row for row in rows if row.get("page_id") == "p1"), {})
    row2 = next((row for row in rows if row.get("page_id") == "p2"), {})
    step("gt_v1.jsonl 内容：2 页 / v1 / 改判为电话 / p2 含补漏丁",
         len(rows) == 2
         and all(row.get("gt_version") == "v1" for row in rows)
         and len(row1.get("entities", [])) == 2
         and row1["entities"][1]["type"] == "电话"
         and row1["entities"][1]["verify"] == "user-corrected"
         and len(row2.get("entities", [])) == 4
         and row2["entities"][-1]["text"] == "丁")

    # 16. journal 汇总：7 条 resolve + 1 条 undo（时间正序）
    r = client.get("/api/journal?n=50")
    lines = r.json() if r.status_code == 200 else []
    ops = [ln.get("op") for ln in lines]
    step("journal → 8 行（resolve×7 + undo×1）",
         r.status_code == 200 and len(ops) == 8
         and ops.count("resolve") == 7 and ops.count("undo") == 1, str(ops))


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    work = build_work()
    server, thread, base = serve(work)
    print(f"自测服务：{base}（work={work}）", flush=True)
    try:
        with httpx.Client(base_url=base, timeout=10.0) as client:
            run_flow(client, work)
    except Exception as exc:  # 流程中断也按失败计
        step(f"流程异常中断：{exc!r}", False)
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        shutil.rmtree(work, ignore_errors=True)
    if FAILURES:
        print(f"\n自测未通过：{len(FAILURES)} 步失败 —— {FAILURES}", flush=True)
        return 1
    print("\n自测全部通过（exit 0）", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
