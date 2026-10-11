"""eval-ng e2e 对齐层 gold test：同一 mock backend 驱动新旧两实现，质量口径逐位对照（AC2 本地锁）。

CI 无真端点：httpx MockTransport；wall_s 类实时字段不比，质量字段（P/R/F1/tp/fp/fn、
digital 分级、数字闸判定、宽松口径）必须完全一致。
"""

import httpx

import evalng_bootstrap as boot

e2e = boot.load("engine/e2e.py")

REPO = boot.REPO


_OLD = None


def _load_old():
    global _OLD
    if _OLD is not None:
        return _OLD
    import importlib.util
    import sys

    for p in (str(REPO / "eval" / "scripts"), str(REPO / "backend" / "scripts" / "eval")):
        if p not in sys.path:
            sys.path.insert(0, p)
    spec = importlib.util.spec_from_file_location("old_run_eval", REPO / "eval" / "scripts" / "run_eval.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["old_run_eval"] = mod
    spec.loader.exec_module(mod)
    _OLD = mod
    return mod


def _vision_boxes(page_seed):
    return {"bounding_boxes": [
        {"type": "PERSON_NAME", "text": f" 张三 {page_seed} "},
        {"type": "ID_NUMBER", "text": "110101199001010011"},
        {"type": "PHONE_NUMBER", "text": "138 0013 8000"},
        {"type": "UNKNOWN_TYPE_X", "text": "未映射类型保留"},
    ],
        "duration_ms": {"ocr_ms": 100, "ner_ms": 50},
        "pipeline_status": {"ocr": "ok"}}


def _handler(state):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/auth/login"):
            return httpx.Response(200, json={"access_token": "tok"})
        if path.endswith("/files/upload"):
            body = request.content
            if b"encrypted" in body:
                return httpx.Response(422, json={"detail": "encrypted pdf not supported"})
            return httpx.Response(200, json={"file_id": "f1"})
        if "/vision" in path:
            page = int(request.url.params.get("page", "1"))
            import time

            time.sleep(0.001)  # 墙钟非零（旧 perf_only 分支 sum(walls)=0 会除零，真环境不存在）
            return httpx.Response(200, json=_vision_boxes(page))
        if path.endswith("/parse") or path.endswith("/ner/hybrid"):
            import time

            time.sleep(0.001)
            if path.endswith("/parse"):
                return httpx.Response(200, json={"pages": []})
            return httpx.Response(200, json={"entities": [
                {"type": "PERSON_NAME", "text": "张三 文档级"},
                {"type": "BANK_CARD", "text": "6222020200112233445"},
            ]})
        if request.method == "DELETE":
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404, json={})
    return handler


def _make_api(cls):
    api = cls.__new__(cls)
    api.client = httpx.Client(base_url="http://mock", trust_env=False,
                              transport=httpx.MockTransport(_handler(None)))
    api.client.headers["Authorization"] = "Bearer tok"
    return api


def _specs(tmp_path):
    import json

    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4 mock")
    enc = tmp_path / "encrypted.pdf"
    enc.write_bytes(b"enc")
    docx = tmp_path / "doc.docx"
    docx.write_bytes(b"docx")
    txt = tmp_path / "doc.txt"
    txt.write_bytes(b"txt")
    gt_pages = [
        {"page": 1, "entities": {"姓名": ["张三 1"], "身份证号": ["110101199001010011"],
                                  "电话": ["13800138000"]}},
        {"page": 2, "entities": {"姓名": ["李四"], "银行卡号": ["6222020200112233445"]}}]
    # 旧线 spec["gt"] 是相对其 _REPO_ROOT/eval/datasets 的路径——monkeypatch 到 tmp 并落盘
    old_root = tmp_path / "oldroot" / "eval" / "datasets"
    old_root.mkdir(parents=True, exist_ok=True)
    (old_root / "pdf1.json").write_text(json.dumps({"pages": gt_pages}, ensure_ascii=False),
                                        encoding="utf-8")
    (old_root / "docx1.json").write_text(json.dumps(
        {"pages": [{"page": 1, "entities": {"姓名": ["张三 文档级"],
                                            "银行卡号": ["6222020200112233445"]}}]},
        ensure_ascii=False), encoding="utf-8")
    (old_root / "txt1.json").write_text(json.dumps(
        {"pages": [{"page": 1, "entities": {"姓名": ["张三 文档级"]}}]},
        ensure_ascii=False), encoding="utf-8")
    old = _load_old()
    old._REPO_ROOT = tmp_path / "oldroot"
    specs = [
        {"id": "pdf1", "path": str(pdf), "carrier": "scanned_pdf", "doc_type": "contract",
         "density": "mid", "pages": 2, "gt_pages": gt_pages, "gt": "pdf1.json"},
        {"id": "docx1", "path": str(docx), "carrier": "docx", "doc_type": "contract",
         "density": "mid", "pages": 2,
         "gt_pages": [{"page": 1, "entities": {"姓名": ["张三 文档级"],
                                               "银行卡号": ["6222020200112233445"]}}],
         "gt": "docx1.json"},
        {"id": "txt1", "path": str(txt), "carrier": "txt", "doc_type": "bank_statement",
         "density": "mid", "pages": 1,
         "gt_pages": [{"page": 1, "entities": {"姓名": ["张三 文档级"]}}], "gt": "txt1.json"},
        {"id": "enc1", "path": str(enc), "carrier": "encrypted_pdf", "doc_type": "contract",
         "density": "mid", "pages": 1, "gt_pages": []},
        {"id": "perf1", "path": str(pdf), "carrier": "scanned_pdf", "doc_type": "contract",
         "density": "mid", "pages": 1, "gt_pages": []},
    ]
    return specs


def test_squash_parity_with_old_common_api():
    import sys

    for p in (str(REPO / "eval" / "scripts"),):
        if p not in sys.path:
            sys.path.insert(0, p)
    import common_api  # noqa: E402

    samples = ["张 三", "  a\tb\nc  ", "138 0013 8000", "", None, "无空白"]
    for s in samples:
        assert e2e.squash(s) == common_api.squash(s), repr(s)


def _args(warmup=1):
    import argparse

    return argparse.Namespace(warmup_pages=warmup)


def test_gold_e2e_file_parity(tmp_path):
    old = _load_old()
    old_api = _make_api(old.common_api.EvalApi)
    new_api = _make_api(e2e.E2eApi)
    for spec in _specs(tmp_path):
        om = old.run_e2e_file(old_api, dict(spec), _args())
        nm = e2e.run_e2e_file(new_api, spec, warmup_pages=1)
        if "overall" in om:
            # 质量口径逐位一致（AC2 本地锁）
            for key in ("precision", "recall", "f1", "tp", "fp", "fn"):
                assert om["overall"][key] == nm["overall"][key], (spec["id"], key)
            assert om["digital"] == nm["digital"], spec["id"]
            assert om["digital_gate"] == nm["digital_gate"], spec["id"]
            assert om["loose"] == nm["loose"], spec["id"]
            # records 含实测 latency_sec（两实现各测各的，数值必不同）——剔除后比较
            _strip = lambda rs: [{k: v for k, v in r.items() if k != "latency_sec"} for r in rs]
            assert _strip(om["records"]) == _strip(nm["records"]), spec["id"]
            assert om["file"] == nm["file"], spec["id"]
            # perf 结构一致（实时墙钟数值除外）
            assert om["perf"]["pages_total"] == nm["perf"]["pages_total"]
            assert om["perf"]["steady_pages"] == nm["perf"]["steady_pages"]
        else:
            assert om.get("robustness", {}).get("outcome") == nm.get("robustness", {}).get("outcome"), spec["id"]


def test_gold_e2e_level_parity(tmp_path):
    old = _load_old()
    specs = _specs(tmp_path)
    old_api = _make_api(old.common_api.EvalApi)
    new_api = _make_api(e2e.E2eApi)
    # 旧 run_e2e_level(args) 内部 load_manifest 且签名只收 args——聚合侧用旧模块同构装配
    # （逐文件 run_e2e_file + 页级 records 拼装 + merge_file_digital + digital_gate_pass）
    import argparse

    args = argparse.Namespace(warmup_pages=1)
    per_file_old = [old.run_e2e_file(old_api, dict(s), args) for s in specs]
    quality_old = [fm for fm in per_file_old if "overall" in fm]
    all_records = []
    for fm in quality_old:
        for rec in fm["records"]:
            all_records.append({"page_id": f"{fm['file']['id']}#{rec['page_id']}",
                                "gt": rec["gt"], "pred": rec["pred"],
                                "latency_sec": rec["latency_sec"]})
    oo = old.e2e_core_metrics(all_records, {}, {})
    oo["digital"] = old.merge_file_digital(quality_old)
    gate_ok, gate_failures = old.nerq.digital_gate_pass(oo)
    oo["digital_gate"] = {"pass": gate_ok, "failures": gate_failures}
    o = {"overall": oo, "rejected": [fm["file"]["id"] for fm in per_file_old
                                     if fm.get("robustness", {}).get("outcome") == "rejected"]}

    n = e2e.run_e2e_level(new_api, specs, warmup_pages=1)
    assert o["rejected"] == n["rejected"]
    assert [fm["file"]["id"] for fm in per_file_old] == [fm["file"]["id"] for fm in n["per_file"]]
    no = n["overall"]
    for key in ("precision", "recall", "f1", "tp", "fp", "fn"):
        assert oo["overall"][key] == no["overall"][key], key
    assert oo["digital"] == no["digital"]
    assert oo["digital_gate"] == no["digital_gate"]
    assert oo["loose"] == no["loose"]
