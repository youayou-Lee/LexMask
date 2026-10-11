#!/usr/bin/env python3
"""GT 工作台端到端自测（Issue#56 M3 / Task 5）——HTTP 层孪生走查。

对真实 uvicorn 实例（后台线程、随机端口、临时 work 夹具）走一遍完整业务流，
作为 M3 的自动化出口证据；内置浏览器的人工走查由控制器另行执行。

- 夹具：5 页最小合法 pack（合成占位符，模式抄 backend/tests/test_gt_workbench.py；
  R6 分歧的 candidates 额外补一路 ``md`` 读数以覆盖前端各模型读数并排契约）。
  p3 为 T5 评审 Critical#1 回归钉：同文两处实体（法律文书常态），分歧只挂
  第二处 span——pin 前端必须按 span 值比较命中第二处（text 兜底会错锚第一处）。
  p4/p5 为终审修复波（2026-10-07）真实引擎形状回归钉（结构抄 arbitrate.py/
  pagepack.py，零真实案卷内容）：R6 整页升级页 0 实体（gap 条目
  candidates={"detail":[...]} + 被否读数条目）与 R3 采信页（被否 a 读数留痕、
  不在 entities）——旧三键在真实 M1 引擎产出上 94% 分歧无法关单的卡死路径。
  零真实案卷、零网络外呼（仅 127.0.0.1）、不碰任何 git 仓（铁律 1）。
- 流程：首页关键元素 id + 前端 linkedEntity 值比较回归钉 + 第四键/显式关单/
  位置确认源码钉 + v2 高亮联动钉（争议候选读数蓝 mk-dispute 段构造器 node
  真执行、点卡滚动接线、争议 payload 候选 span）→ 未抽样 404 口径（/api/trust、
  /api/sample）→ 一单裁对 →
  同文第二处裁决（第二处 confirmed、第一处不动、定向关单）→ 一单补漏 →
  一单裁错（带 correction）→ 撤销一次（快照还原、分歧复现，且不波及 p2
  补漏 / p3 裁决）→ finalize 期待 409 缺项清单（含「未裁决」）→ 真实形状
  三步回归：gap 卡「机器正确」关单（不碰实体）、R6 0 实体页「漏」（补录并
  关单 user:漏）、被否 R3 读数「机器正确」（实体原封不动）→ 抽样（ratio
  0.5：分层确定性，前两条钉死）→ finalize 未复审硬门禁 409（I1 终审修复）
  → 抽检一错余对（抽检错顺带定向关单）→ 可信率已知值 3/4 → finalize 200 +
  gt_v1.jsonl 落盘（5 页，ack/漏 关单与改判/补漏写回全在案）。
- 每步打印 PASS/FAIL（node 缺失时前端执行钉打印 SKIP，不算失败）；
  全绿 exit 0，任一失败 exit 1。

依赖注记：HTTP 客户端用 httpx——backend/requirements.txt 已携带（fastapi
TestClient 同源）；requests 亦为既有依赖（同文件 requests>=2.31.0，GT 预标
转录客户端在用），二者皆零新依赖，取 httpx 以与 TestClient 同源。
"""
from __future__ import annotations

import json
import shutil
import subprocess
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


def skip(name: str, detail: str = "") -> None:
    """条件性检查跳过（如 node 缺失）：打印 SKIP，不计失败。"""
    line = f"[SKIP] {name}"
    if detail:
        line += f" —— {detail}"
    print(line, flush=True)


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


def _pack_p3():  # table 页：同文两处实体 + 分歧只挂第二处 span（T5 评审 Critical#1）
    text = "甲方张三与乙方张三于同日签订"  # len 14；张三 落在 [2,4) 与 [7,9)
    return {
        "page_id": "p3", "page_type": "table",
        "source": {"file_sha256": "2" * 64, "page": 2,
                   "carrier": "text_pdf", "segment": "first"},
        "transcript_gt": {"text": text, "normalized_text": text,
                          "fidelity": "machine"},
        "entities": [
            {"text": "张三", "type": "姓名",
             "span_original": [2, 4], "span_normalized": [2, 4],
             "origin": "regex", "verify": "consistent", "note": None},
            {"text": "张三", "type": "姓名",
             "span_original": [7, 9], "span_normalized": [7, 9],
             "origin": "regex", "verify": "consistent", "note": None}],
        "adjudications": [
            {"models": ["a", "b"], "rule": "R6", "verdict": "disputed",
             "candidates": {"a": [{"text": "张三", "type": "姓名",
                                   "span_original": [7, 9]}],
                            "b": []}}]}


def _pack_p4():  # body 页：R6 整页升级真实形状（结构抄 arbitrate._arbitrate_r6 /
    # pagepack._pack_adjudications）——0 实体 + 页级 gap 条目（candidates=detail
    # 字符串列表）+ 被否读数条目（读数不在 entities）
    return {
        "page_id": "p4", "page_type": "body",
        "source": {"file_sha256": "3" * 64, "page": 3,
                   "carrier": "scanned", "segment": "first"},
        "transcript_gt": {"text": "甲乙丙丁",
                          "normalized_text": "甲乙丙丁",
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


def _pack_p5():  # table 页：R3 采 VL 面真实形状（结构抄 arbitrate._arbitrate_r3_r4_r5）
    # ——采信读数入库（consistent/arbitrated），被否 a 读数留痕 disputed、
    # 不在 entities（真实引擎从不把 disputed 读数放进 entities）
    return {
        "page_id": "p5", "page_type": "table",
        "source": {"file_sha256": "4" * 64, "page": 4,
                   "carrier": "text_pdf", "segment": "first"},
        "transcript_gt": {"text": "甲乙丙丁",
                          "normalized_text": "甲乙丙丁",
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


def build_work() -> Path:
    """临时工作目录：pages/p1..p5 五份合法 pack。"""
    work = Path(tempfile.mkdtemp(prefix="gt-workbench-selftest-"))
    for pid, pack in (("p1", _pack_p1()), ("p2", _pack_p2()), ("p3", _pack_p3()),
                      ("p4", _pack_p4()), ("p5", _pack_p5())):
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


# ---- 前端 linkedEntity 回归钉（T5 评审 Critical#1） --------------------------------

# 同文两处实体，分歧候选只挂第二处 span [7,9]：正确的 linkedEntity 必须返回 1
_NODE_PACK = {"entities": [
    {"text": "张三", "type": "姓名", "span_original": [2, 4], "span_normalized": [2, 4]},
    {"text": "张三", "type": "姓名", "span_original": [7, 9], "span_normalized": [7, 9]}]}
_NODE_ADJ = {"candidates": {"a": [{"text": "张三", "type": "姓名",
                                   "span_original": [7, 9]}], "b": []}}

_NODE_CODE = ("const fs = require('fs');"
              "eval(fs.readFileSync(process.argv[1], 'utf8'));"
              "const pack = JSON.parse(process.argv[2]);"
              "const adj = JSON.parse(process.argv[3]);"
              "console.log(linkedEntity(pack, adj));")


def frontend_link_pin(html: str) -> None:
    """双钉：源码钉（值比较在案、旧引用比较已移除）+ node 真执行钉（有 node 才跑）。"""
    # 源码钉（永远执行）：spansEqual 值比较助手在案；旧引用比较写法已移除
    has_helper = "function spansEqual" in html and "every((v, i) => v === b[i])" in html
    old_bug_gone = "e[key] === ents[i][key]" not in html
    step("前端源码钉：spansEqual 值比较在案、旧引用比较已移除",
         has_helper and old_bug_gone)

    # 真执行钉：从真实下发的 HTML 抽出 spansEqual + linkedEntity 交给 node 跑
    try:
        js = html[html.index("function spansEqual"):html.index("function candOrder")]
    except ValueError:
        step("前端执行钉：未能从 HTML 抽出 linkedEntity 源", False)
        return
    if not shutil.which("node"):
        skip("前端执行钉（node 真跑 linkedEntity）", "node 不可用，源码钉已覆盖")
        return
    with tempfile.TemporaryDirectory() as td:
        js_path = Path(td) / "linked_entity.js"
        js_path.write_text(js, encoding="utf-8")
        try:
            r = subprocess.run(
                ["node", "-e", _NODE_CODE, str(js_path),
                 json.dumps(_NODE_PACK, ensure_ascii=False),
                 json.dumps(_NODE_ADJ, ensure_ascii=False)],
                capture_output=True, text=True, timeout=10)
        except subprocess.TimeoutExpired:
            step("前端执行钉：node 执行超时", False)
            return
    ok = r.returncode == 0 and r.stdout.strip() == "1"
    step("前端执行钉：linkedEntity（同文第二处 span）→ 1", ok,
         f"stdout={r.stdout.strip()!r} stderr={r.stderr.strip()[:200]!r}"
         if not ok else "同文两处实体、分歧挂第二处：命中第二处（span 值比较优先）")


def frontend_real_shape_pins(html: str) -> None:
    """终审修复波前端源码钉：第四键 / 显式关单 / 0 实体页可用键 / 位置确认 / gap 直出。"""
    step("前端源码钉：第四键「机器正确」在案（data-act=ack、每张卡都有）",
         'data-act="ack"' in html and "机器正确" in html)
    step("前端源码钉：resolve 恒传 adjudication_index（对/ack/错/漏 ≥3 处）",
         html.count("adjudication_index:") >= 3,
         f"出现 {html.count('adjudication_index:')} 处")
    step("前端源码钉：补漏 entity_index 可空（null 直传）+ 0 实体页提示在案",
         "entity_index: verdict === \"漏\" ? null : entIdx" in html
         and "未找到与该分歧关联的实体" in html)
    step("前端源码钉：I2 位置确认（settleSpans + span-confirm 勾选 + 手工定位区）",
         "function settleSpans" in html and 'name="span-confirm"' in html
         and "位置确认" in html and "manual-span" in html)
    step("前端源码钉：M2 gap detail 字符串候选直出",
         'typeof c === "object"' in html)


def frontend_highlight_pins(html: str) -> None:
    """v2 高亮联动源码钉：争议候选读数蓝色高亮 / 无 DOM 段构造器 / 点卡滚动接线。"""
    step("前端源码钉：争议读数蓝色高亮（mk-dispute 类 + CSS 规则在案）",
         "mk-dispute" in html and "mark.mk-dispute{" in html)
    step("前端源码钉：无 DOM 段构造器 transcriptSegments（自测可直跑）",
         "function transcriptSegments" in html)
    step("前端源码钉：点卡片滚动到争议高亮（scrollIntoView + mk-dispute 定位在案）",
         "scrollIntoView" in html and 'querySelector("mark.mk-dispute")' in html)


# 争议卡蓝高亮真执行钉：transcriptSegments（无 DOM）对候选读数 span 产出 mk-dispute 段
_NODE_SEG_CODE = ("const fs = require('fs');"
                  "eval(fs.readFileSync(process.argv[1], 'utf8'));"
                  "const pack = JSON.parse(process.argv[2]);"
                  "const segs = transcriptSegments(pack, -1, [[0, 2]]);"
                  "console.log(JSON.stringify(segs));")

_SEG_PACK = {"transcript_gt": {"text": "张三号码110122198110227771"},
             "entities": [{"text": "张三", "type": "姓名", "span_original": [0, 2]},
                          {"text": "110122198110227771", "type": "身份证号",
                           "span_original": [4, 22]}]}


def frontend_highlight_exec_pin(html: str) -> None:
    """真执行钉：争议候选读数 span → 蓝 mk-dispute 段，已采纳实体 → 黄段（node 直跑）。"""
    try:
        js = html[html.index("function transcriptSegments"):
                  html.index("function highlightTranscript")]
    except ValueError:
        step("前端执行钉：未能从 HTML 抽出 transcriptSegments 源", False)
        return
    if not shutil.which("node"):
        skip("前端执行钉（node 真跑 transcriptSegments）", "node 不可用，源码钉已覆盖")
        return
    with tempfile.TemporaryDirectory() as td:
        js_path = Path(td) / "segments.js"
        js_path.write_text(js, encoding="utf-8")
        try:
            r = subprocess.run(
                ["node", "-e", _NODE_SEG_CODE, str(js_path),
                 json.dumps(_SEG_PACK, ensure_ascii=False)],
                capture_output=True, text=True, timeout=10)
        except subprocess.TimeoutExpired:
            step("前端执行钉：node 执行超时", False)
            return
    ok, detail = False, (f"stdout={r.stdout.strip()[:200]!r} "
                         f"stderr={r.stderr.strip()[:200]!r}")
    if r.returncode == 0:
        try:
            segs = json.loads(r.stdout.strip())
            ok = (bool(segs) and segs[0]["mark"] == "mk-dispute"
                  and segs[0]["text"] == "张三"
                  and any(s["mark"] == "ent" and s["text"] == "110122198110227771"
                          for s in segs))
        except (ValueError, KeyError, IndexError):
            pass
    step("前端执行钉：争议 span → 蓝 mk-dispute 段 + 已采纳实体黄段", ok,
         "" if ok else detail)


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

    # 2. 前端 linkedEntity 值比较回归钉（T5 评审 Critical#1）
    frontend_link_pin(html)

    # 2.5 终审修复波前端源码钉（第四键/显式关单/位置确认/gap 直出）
    frontend_real_shape_pins(html)

    # 2.7 v2 高亮联动钉（蓝色争议读数高亮 + 点卡滚动 + 段构造器真执行）
    frontend_highlight_pins(html)
    frontend_highlight_exec_pin(html)

    # 3. 未抽样口径：/api/trust 与 /api/sample 均 404 + 中文原因（前端 boot 依赖）
    r_trust = client.get("/api/trust")
    step("GET /api/trust（未抽样）→ 404 + error",
         r_trust.status_code == 404 and "error" in r_trust.json(),
         r_trust.text if r_trust.status_code != 404 else "")
    r_sample = client.get("/api/sample")
    step("GET /api/sample（未抽样）→ 404 + error",
         r_sample.status_code == 404 and "error" in r_sample.json(),
         r_sample.text if r_sample.status_code != 404 else "")

    # 4. stats / disputes / page 契约（p4 0 实体、p5 含 1 arbitrated）
    r = client.get("/api/stats")
    stats = r.json() if r.status_code == 200 else {}
    step("GET /api/stats → consistent=8、arbitrated=1、user-confirmed=0",
         r.status_code == 200 and stats.get("consistent") == 8
         and stats.get("arbitrated") == 1
         and stats.get("user-confirmed") == 0, str(stats))
    r = client.get("/api/disputes")
    ds = r.json() if r.status_code == 200 else {}
    step("GET /api/disputes → p1×2 + p3×1 + p4×2 + p5×1（真实形状页在列）",
         r.status_code == 200 and list(ds) == ["p1", "p3", "p4", "p5"]
         and len(ds.get("p1", [])) == 2 and len(ds.get("p3", [])) == 1
         and len(ds.get("p4", [])) == 2 and len(ds.get("p5", [])) == 1,
         str(r.text if r.status_code != 200 else {k: len(v) for k, v in ds.items()}))
    # 4.5 v2 高亮联动数据钉：争议 payload 携带 UI 所需的候选读数 span
    #（卡=蓝高亮锚，前端 firstCandidateSpan 按 a→b→md 取第一路合法 span）
    first_p1 = (ds.get("p1") or [{}])[0]
    span = ((first_p1.get("candidates") or {}).get("a") or [{}])[0].get("span_original")
    step("争议 payload 携候选读数 span（p1#0 a 路 span_original=[0,2]）",
         span == [0, 2], str(span))
    r = client.get("/api/page/p1")
    page = r.json() if r.status_code == 200 else {}
    step("GET /api/page/p1 → image_url=/img/p1、2 条实体",
         r.status_code == 200 and page.get("image_url") == "/img/p1"
         and len(page.get("entities", [])) == 2)
    r = client.get("/api/page/p4")
    page4 = r.json() if r.status_code == 200 else {}
    step("GET /api/page/p4 → 0 条实体（R6 整页升级真实形状）",
         r.status_code == 200 and page4.get("entities") == []
         and len(page4.get("adjudications", [])) == 2)

    # 5. /img 无 pdf_root → 404 + 原因（前端转录高亮兜底的触发口径）
    r = client.get("/img/p1")
    step("GET /img/p1（无 pdf_root）→ 404 + error",
         r.status_code == 404 and "error" in r.json(), r.text)

    # 6. preset 类型名集（改判/补漏表单下拉数据源）
    r = client.get("/api/preset-types")
    names = r.json() if r.status_code == 200 else []
    step("GET /api/preset-types → 200 且含 姓名/身份证号",
         r.status_code == 200 and "姓名" in names and "身份证号" in names)

    # 7. 一单裁对：p1 实体0（张三/姓名）→ user-confirmed，p1 分歧 2 → 1
    r = client.post("/api/resolve", json={"page_id": "p1", "entity_index": 0,
                                          "verdict": "对", "correct": None,
                                          "note": None})
    ok = r.status_code == 200 and r.json().get("verify") == "user-confirmed"
    step("裁对 p1#0 → 200 verify=user-confirmed", ok, r.text)
    ds = client.get("/api/disputes").json()
    step("裁对后分歧 6 → 5（p1 1 / p3 1 / p4 2 / p5 1）",
         len(ds.get("p1", [])) == 1 and len(ds.get("p3", [])) == 1
         and len(ds.get("p4", [])) == 2 and len(ds.get("p5", [])) == 1,
         str({k: len(v) for k, v in ds.items()}))

    # 8. 同文重复裁决（Critical#1 流程钉）：分歧挂 p3 第二处 span[7,9]，
    #    按第二处实体（entity_index=1）裁决——第一处不得被牵连
    r = client.post("/api/resolve", json={"page_id": "p3", "entity_index": 1,
                                          "verdict": "对", "correct": None,
                                          "note": None})
    ok = r.status_code == 200 and r.json().get("verify") == "user-confirmed"
    step("同文裁决 p3#1（第二处）→ 200 verify=user-confirmed", ok, r.text)
    page3 = client.get("/api/page/p3").json()
    ents3 = page3.get("entities", [])
    adj3 = (page3.get("adjudications") or [{}])[0]
    step("p3 状态钉：第一处 consistent / 第二处 user-confirmed / 分歧关单 user:对",
         len(ents3) == 2 and ents3[0]["verify"] == "consistent"
         and ents3[1]["verify"] == "user-confirmed"
         and adj3.get("verdict") == "user:对")

    # 9. 一单补漏：p2 补「丁」（漏，不带 adjudication_index=旧客户端兼容路径）
    #    → 新增实体 user-confirmed，关单数不变
    r = client.post("/api/resolve", json={
        "page_id": "p2", "entity_index": 0, "verdict": "漏",
        "correct": {"text": "丁", "type": "姓名",
                    "span_original": [3, 4], "span_normalized": [3, 4]},
        "note": None})
    step("补漏 p2（丁/姓名，旧兼容无 index）→ 200", r.status_code == 200, r.text)
    page2 = client.get("/api/page/p2").json()
    ents2 = page2.get("entities", [])
    step("补漏后 p2 共 4 条实体且末条为 丁/user-confirmed",
         len(ents2) == 4 and ents2[-1]["text"] == "丁"
         and ents2[-1]["verify"] == "user-confirmed")
    ds = client.get("/api/disputes").json()
    step("旧兼容补漏不关单：分歧仍 p1 1 / p4 2 / p5 1",
         len(ds.get("p1", [])) == 1 and len(ds.get("p4", [])) == 2
         and len(ds.get("p5", [])) == 1)

    # 10. 一单裁错（带 correction）：p1 实体1 身份证号 → 电话（采信 b 读数），定向关单
    r = client.post("/api/resolve", json={
        "page_id": "p1", "entity_index": 1, "verdict": "错",
        "correct": {"text": "110122198110227771", "type": "电话",
                    "span_original": [4, 22], "span_normalized": [4, 22]},
        "note": "采信 b 读数：实为电话号码"})
    ok = r.status_code == 200 and r.json().get("verify") == "user-corrected"
    step("裁错 p1#1（改型 电话）→ 200 verify=user-corrected", ok, r.text)
    ds = client.get("/api/disputes").json()
    step("裁错定向关单后仅剩真实形状页：p4 2 / p5 1",
         list(ds) == ["p4", "p5"] and len(ds["p4"]) == 2 and len(ds["p5"]) == 1,
         str({k: len(v) for k, v in ds.items()}))

    # 11. 撤销一次：快照还原 → 裁错被回退（实体还原、分歧复现），他页操作不受影响
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
    step("撤销后分歧复现：p1 剩 1 条 disputed",
         len(ds.get("p1", [])) == 1 and len(ds.get("p4", [])) == 2
         and len(ds.get("p5", [])) == 1)
    page2 = client.get("/api/page/p2").json()
    step("撤销不影响补漏：p2 仍 4 条实体", len(page2.get("entities", [])) == 4)
    page3 = client.get("/api/page/p3").json()
    step("撤销不影响同文裁决：p3 第二处仍 user-confirmed",
         page3.get("entities", [{}])[-1].get("verify") == "user-confirmed")

    # 12. finalize 前置不满足 → 409 缺项清单（未裁决 4 + 抽检未完成）
    r = client.post("/api/finalize")
    missing = r.json().get("missing", []) if r.status_code == 409 else []
    step("finalize（4 条分歧未抽样）→ 409 缺项清单",
         r.status_code == 409 and any("未裁决 4" in m for m in missing)
         and any("抽检" in m for m in missing), str(missing))

    # 13. 真实引擎形状回归（终审修复波 C1；评审在 ~/gt-work 实测的卡死路径）：
    #     gap 卡「机器正确」/ R6 0 实体页「漏」/ 被否 R3 读数「机器正确」
    r = client.post("/api/resolve", json={
        "page_id": "p4", "entity_index": None, "adjudication_index": 0,
        "verdict": "ack", "correct": None, "note": None})
    ok = r.status_code == 200 and r.json().get("verdict") == "ack" \
        and r.json().get("verify") is None
    step("gap 卡「机器正确」（ack）→ 200 且不碰实体", ok, r.text)
    page4 = client.get("/api/page/p4").json()
    step("ack 后 p4 仍 0 实体、gap 条目关单 user:ack",
         page4.get("entities") == []
         and page4["adjudications"][0]["verdict"] == "user:ack")

    r = client.post("/api/resolve", json={
        "page_id": "p4", "entity_index": None, "adjudication_index": 1,
        "verdict": "漏",
        "correct": {"text": "戊", "type": "姓名",
                    "span_original": [3, 4], "span_normalized": [3, 4]},
        "note": None})
    ok = r.status_code == 200 and r.json().get("verify") == "user-confirmed"
    step("R6 0 实体页「漏」（entity_index=null）→ 200 verify=user-confirmed", ok, r.text)
    page4 = client.get("/api/page/p4").json()
    ents4 = page4.get("entities", [])
    step("0 实体页补漏后 p4 = 1 条实体（戊/origin=user）且条目关单 user:漏",
         len(ents4) == 1 and ents4[0]["text"] == "戊"
         and ents4[0]["origin"] == "user"
         and ents4[0]["verify"] == "user-confirmed"
         and page4["adjudications"][1]["verdict"] == "user:漏")
    step("I2 服务端非致命警告：span 处转录「丁」≠提交「戊」→ warnings 留痕",
         isinstance(r.json().get("warnings"), list)
         and any("不一致" in w for w in r.json()["warnings"]),
         str(r.json().get("warnings")))

    r = client.post("/api/resolve", json={
        "page_id": "p5", "entity_index": None, "adjudication_index": 2,
        "verdict": "ack", "correct": None, "note": None})
    ok = r.status_code == 200 and r.json().get("verify") is None
    step("被否 R3 读数「机器正确」→ 200 且实体原封不动", ok, r.text)
    page5 = client.get("/api/page/p5").json()
    ents5 = page5.get("entities", [])
    step("p5 状态钉：采信实体不动（consistent/arbitrated）、被否条目 user:ack",
         len(ents5) == 2 and ents5[0]["verify"] == "consistent"
         and ents5[1]["verify"] == "arbitrated"
         and page5["adjudications"][2]["verdict"] == "user:ack")

    ds = client.get("/api/disputes").json()
    step("真实形状三步关单后仅剩 p1 1 条", list(ds) == ["p1"] and len(ds["p1"]) == 1,
         str({k: len(v) for k, v in ds.items()}))

    # 14. 抽样：ratio=0.5、seed=42 → 3 条一致集实体；body×身份证号层钉死首条
    #     [p1,1]；table×姓名 5 取 2（p2×3 + p3#0 + p5#0）——该 seed 恰不抽中
    #     p3#0：同文第一处终态保持 consistent（终态强钉）
    r = client.post("/api/sample", json={"ratio": 0.5, "seed": 42})
    sample = r.json() if r.status_code == 200 else {}
    selected = sample.get("selected", [])
    step("POST /api/sample → 200 共 3 条、首条 [p1,1]（分层确定性）",
         r.status_code == 200 and len(selected) == 3 and selected[0] == ["p1", 1],
         str(selected))
    step("sample_seed.json 落盘", (work / "sample_seed.json").is_file())

    # 15. I1 硬门禁：已抽样未复审 → 409 且缺项报「未复审 3 条 / 共 3 条」
    r = client.post("/api/finalize")
    missing = r.json().get("missing", []) if r.status_code == 409 else []
    step("finalize（未复审 3 条）→ 409 报未复审计数",
         r.status_code == 409 and any("未复审 3" in m for m in missing)
         and any("共 3 条" in m for m in missing), str(missing))

    # 16. 抽检一错余对：p1#1（剩余分歧挂靠实体）判错带 correction（定向关单），
    #     其余判对
    target = next((s for s in selected if s == ["p1", 1]), None)
    ok = target is not None
    step("抽样清单含 [p1,1]（可构造抽检错定向关单）", ok, str(selected))
    r = client.post("/api/sample-verdict", json={
        "page_id": target[0], "entity_index": target[1], "ok": False,
        "correct": {"text": "110122198110227771", "type": "电话",
                    "span_original": [4, 22], "span_normalized": [4, 22]}})
    ok = r.status_code == 200 and "trust" in r.json()
    step("抽检 p1#1 判错（ok=False + correct）→ 200 回带 trust", ok, r.text)
    all_ok = True
    for pid, idx in selected:
        if [pid, idx] == target:
            continue
        rr = client.post("/api/sample-verdict", json={
            "page_id": pid, "entity_index": idx, "ok": True, "correct": None})
        all_ok = all_ok and rr.status_code == 200
    step(f"抽检其余 {len(selected) - 1} 条判对 → 全部 200", all_ok)

    # 17. 可信率已知值：2/3 维持原判（p2 两条 confirmed、p1#1 corrected）
    r = client.get("/api/trust")
    trust = r.json() if r.status_code == 200 else {}
    step("GET /api/trust → checked=3 confirmed=2 corrected=1 unreviewed=0 rate=2/3",
         r.status_code == 200 and trust.get("checked") == 3
         and trust.get("confirmed") == 2 and trust.get("corrected") == 1
         and trust.get("unreviewed") == 0
         and abs(trust.get("rate", -1) - 2 / 3) < 1e-9, str(trust))

    # 18. 抽检错顺带定向关单：分歧清零
    ds = client.get("/api/disputes").json()
    step("抽检改判定向关单后分歧清零", ds == {}, str(ds))

    # 19. finalize → 200 + gt_v1.jsonl 落盘（5 页；ack/漏/改判/补漏写回全在案）
    r = client.post("/api/finalize")
    ok = r.status_code == 200 and r.json().get("ok") is True and r.json().get("pages") == 5
    step("finalize（前置全满足）→ 200 pages=5", ok, r.text)
    out = work / "gt_v1.jsonl"
    step("gt_v1.jsonl 落盘", out.is_file(), str(out))
    rows = [json.loads(ln) for ln in
            out.read_text(encoding="utf-8").splitlines() if ln.strip()] \
        if out.is_file() else []
    by_pid = {row.get("page_id"): row for row in rows}
    row1, row2, row3 = by_pid.get("p1", {}), by_pid.get("p2", {}), by_pid.get("p3", {})
    row4, row5 = by_pid.get("p4", {}), by_pid.get("p5", {})
    step("gt_v1.jsonl 内容：5 页 / v1 / 电话改判 / 补漏丁 / 同文第一处保持 consistent",
         len(rows) == 5
         and all(row.get("gt_version") == "v1" for row in rows)
         and len(row1.get("entities", [])) == 2
         and row1["entities"][1]["type"] == "电话"
         and row1["entities"][1]["verify"] == "user-corrected"
         and len(row2.get("entities", [])) == 4
         and row2["entities"][-1]["text"] == "丁"
         and len(row3.get("entities", [])) == 2
         and row3["entities"][0]["verify"] == "consistent"
         and row3["entities"][1]["verify"] == "user-confirmed"
         and row3["adjudications"][0]["verdict"] == "user:对")
    step("gt_v1.jsonl 真实形状钉：p4 ack/漏 关单 + 补录戊；p5 采信实体不动 + 被否条目 ack",
         len(row4.get("entities", [])) == 1
         and row4["entities"][0]["text"] == "戊"
         and row4["entities"][0]["origin"] == "user"
         and [a["verdict"] for a in row4["adjudications"]] == ["user:ack", "user:漏"]
         and len(row5.get("entities", [])) == 2
         and row5["entities"][0]["verify"] == "consistent"
         and row5["entities"][1]["verify"] == "arbitrated"
         and [a["verdict"] for a in row5["adjudications"]]
         == ["consistent", "auto:b", "user:ack"])

    # 20. journal 汇总：10 条 resolve（裁对/同文/补漏/裁错/ack/漏/ack/抽检×3）
    #     + 1 条 undo（时间正序）
    r = client.get("/api/journal?n=50")
    lines = r.json() if r.status_code == 200 else []
    ops = [ln.get("op") for ln in lines]
    step("journal → 11 行（resolve×10 + undo×1）",
         r.status_code == 200 and len(ops) == 11
         and ops.count("resolve") == 10 and ops.count("undo") == 1, str(ops))

    # 21. M4：journal n 钳非负（负值不再触发 [-n:] 漂移语义）
    r = client.get("/api/journal?n=-3")
    step("GET /api/journal?n=-3 → 200 且钳为空列表",
         r.status_code == 200 and r.json() == [], str(r.text[:120]))


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
