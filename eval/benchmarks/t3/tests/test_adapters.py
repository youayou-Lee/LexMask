"""adapters.py 单测：mock HTTP（本地线程 http.server）验证四臂请求/响应与失败路径。

零真实服务：LA/sigdet 用线程内 HTTP server 回放固定响应；MinerU 用三段
（submit→poll→result zip）状态机 mock；超时/5xx/连接拒绝路径打闭死端口。
"""

from __future__ import annotations

import base64
import io
import json
import sys
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from eval.benchmarks.t3 import adapters

PAGE_W, PAGE_H = 1000, 500


@pytest.fixture(scope="module")
def page_image(tmp_path_factory):
    from PIL import Image

    path = tmp_path_factory.mktemp("pages") / "vol1#p1.png"
    Image.new("RGB", (PAGE_W, PAGE_H), "white").save(path)
    return str(path)


@pytest.fixture(scope="module")
def dead_url():
    """闭死端口：连接必然失败。"""
    server = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    port = server.server_address[1]
    server.server_close()  # 只关 socket 不 shutdown（serve_forever 从未启动，shutdown 会死锁）
    return f"http://127.0.0.1:{port}"


class ReplayingHandler(BaseHTTPRequestHandler):
    routes: dict[str, list] = {}  # path -> [(status, body, delay_s), ...] 弹出式
    requests: list[dict] = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        ReplayingHandler.requests.append({"path": self.path, "body": body})
        self._reply()

    def do_GET(self):
        ReplayingHandler.requests.append({"path": self.path, "body": b""})
        self._reply()

    def _reply(self):
        queue = ReplayingHandler.routes.get(self.path)
        if not queue:
            self.send_error(404)
            return
        if len(queue) > 1:
            item = queue.pop(0)
        else:
            item = queue[0]
        status, payload, delay = item[0], item[1], item[2]
        ctype = item[3] if len(item) > 3 else "application/json"
        if delay:
            time.sleep(delay)
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):  # 静音
        pass


@pytest.fixture()
def mock_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), ReplayingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    ReplayingHandler.routes = {}
    ReplayingHandler.requests = []
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


# ---- LA --------------------------------------------------------------------


def test_la_parses_raw_1000_answer(mock_server, page_image):
    raw = (
        "[official_seal] <ref>公章</ref><box><100><100><200><300></box>\n"
        "[signature] <ref>签字</ref><box><500><50><700><100></box>"
    )
    ReplayingHandler.routes["/detect"] = [
        (200, {"boxes": [], "raw_answer": raw, "elapsed": 1.0}, 0)
    ]
    preds = adapters.run_la(page_image, base_url=mock_server)
    kinds = sorted(p["kind"] for p in preds)
    assert kinds == ["seal", "signature"]
    seal = next(p for p in preds if p["kind"] == "seal")
    assert seal["box"] == {"x": 100.0, "y": 50.0, "w": 100.0, "h": 100.0}  # 0-1000 × 页宽高
    sig = next(p for p in preds if p["kind"] == "signature")
    assert sig["box"] == {"x": 500.0, "y": 25.0, "w": 200.0, "h": 25.0}
    # 请求走 official_seal+signature 双通道
    sent = json.loads(ReplayingHandler.requests[0]["body"])
    assert sent["categories"] == ["official_seal", "signature"]
    assert base64.b64decode(sent["image_base64"])[:8].startswith(b"\x89PNG")


def test_la_falls_back_to_normalized_boxes(mock_server, page_image):
    ReplayingHandler.routes["/detect"] = [
        (
            200,
            {
                "boxes": [
                    {"x": 0.5, "y": 0.2, "width": 0.25, "height": 0.4, "category": "official_seal"}
                ],
                "raw_answer": "",
            },
            0,
        )
    ]
    preds = adapters.run_la(page_image, base_url=mock_server)
    assert len(preds) == 1 and preds[0]["kind"] == "seal"
    assert preds[0]["box"] == {"x": 500.0, "y": 100.0, "w": 250.0, "h": 200.0}


def test_la_prefers_structured_boxes_over_raw(mock_server, page_image):
    """I5：结构化 `boxes` 是现役管线消费的同一契约（已去重/过退化过滤）→ 优先用它。

    旧实现先解析 raw_answer，会绕开服务端过滤，使 LA 框与上线行为不一致、
    误报上界随之变松。顺带钉住 confidence=0.0 不得被 `or 1.0` 抬成最高优先。
    """
    raw = "[official_seal] <ref>公章</ref><box><100><100><200><300></box>"
    ReplayingHandler.routes["/detect"] = [
        (
            200,
            {
                "boxes": [
                    {"x": 0.5, "y": 0.2, "width": 0.25, "height": 0.4,
                     "category": "official_seal", "confidence": 0.0}
                ],
                "raw_answer": raw,
            },
            0,
        )
    ]
    preds = adapters.run_la(page_image, base_url=mock_server)
    assert len(preds) == 1
    assert preds[0]["box"] == {"x": 500.0, "y": 100.0, "w": 250.0, "h": 200.0}
    assert preds[0]["confidence"] == 0.0
    assert preds[0]["raw"] == ""


def test_la_raw_fallback_swaps_dedupes_and_drops_degenerate():
    """I5：raw 回退路径对齐现役解析器（反向坐标交换、重复框去重、退化框丢弃）。"""
    raw = (
        "[signature] <ref>签</ref><box><700><100><500><50></box>\n"      # 反向 → 交换
        "[signature] <ref>签</ref><box><500><50><700><100></box>\n"      # 与上同框 → 去重
        "[official_seal] <ref>章</ref><box><100><100><100><300></box>"   # 零宽 → 丢弃
    )
    preds = adapters.parse_la_raw_answer(raw, 1000, 500)
    assert len(preds) == 1
    assert preds[0]["kind"] == "signature"
    assert preds[0]["box"] == {"x": 500.0, "y": 25.0, "w": 200.0, "h": 25.0}


def test_la_boxes_missing_confidence_defaults_only_when_none():
    """I5：结构化框仅在 confidence 缺失时填 1.0；既有合法 0.0 原样保留。"""
    boxes = [
        {"x": 0.1, "y": 0.1, "width": 0.1, "height": 0.1, "category": "signature"},
        {"x": 0.3, "y": 0.3, "width": 0.1, "height": 0.1, "category": "signature",
         "confidence": 0.0},
        {"x": 0.5, "y": 0.5, "width": 0.1, "height": 0.1, "category": "official_seal",
         "confidence": 0.4},
    ]
    preds = adapters.parse_la_boxes(boxes, 1000, 500)
    assert [p["confidence"] for p in preds] == [1.0, 0.0, 0.4]
    assert [p["kind"] for p in preds] == ["signature", "signature", "seal"]


def test_la_5xx_retries_once_then_raises(mock_server, page_image):
    ReplayingHandler.routes["/detect"] = [
        (500, {"detail": "boom"}, 0),
        (500, {"detail": "boom"}, 0),
    ]
    with pytest.raises(adapters.ArmError, match="HTTP 500"):
        adapters.run_la(page_image, base_url=mock_server, timeout=5)
    assert len(ReplayingHandler.requests) == 2  # 重试恰 1 次


def test_la_timeout_is_arm_error(dead_url, page_image):
    with pytest.raises(adapters.ArmError):
        adapters.run_la(page_image, base_url=dead_url, timeout=0.2)


def test_la_4xx_no_retry(mock_server, page_image):
    ReplayingHandler.routes["/detect"] = [(422, {"detail": "bad"}, 0)]
    with pytest.raises(adapters.ArmError, match="HTTP 422"):
        adapters.run_la(page_image, base_url=mock_server)
    assert len(ReplayingHandler.requests) == 1


# ---- MinerU ----------------------------------------------------------------


def _zip_result(content_list: list[dict]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("doc/pipeline/doc_content_list_v2.json", json.dumps(content_list))
    return buf.getvalue()


def _mineru_task_routes(mock_server, result_bytes_or_json, *, ct="application/zip"):
    ReplayingHandler.routes["/tasks"] = [(202, {"task_id": "t1"}, 0)]
    ReplayingHandler.routes["/tasks/t1"] = [(200, {"status": "completed"}, 0)]
    if isinstance(result_bytes_or_json, bytes):
        ReplayingHandler.routes["/tasks/t1/result"] = [(200, result_bytes_or_json, 0, ct)]
    else:
        body = json.dumps(result_bytes_or_json).encode()
        ReplayingHandler.routes["/tasks/t1/result"] = [(200, body, 0, ct)]


def test_mineru_json_result_branch(mock_server, page_image):
    """单文件任务实测形态：result=application/json，content_list 是**字符串**要再 loads；
    seal 块 type=="image" && sub_type=="seal"，bbox 0-1000 归一化 ×页宽高。"""
    cl = [[
        {"type": "image", "sub_type": "seal", "bbox": [698, 9, 895, 158], "page_idx": 0},
        {"type": "text", "text": "正文"},
    ]]
    payload = {
        "backend": "pipeline", "version": "3.x",
        "results": {"p11": {"md_content": "", "content_list": json.dumps(cl, ensure_ascii=False)}},
    }
    _mineru_task_routes(mock_server, payload, ct="application/json")
    preds = adapters.run_mineru_seal(page_image, base_url=mock_server)
    # 页 1000×500：698/1000×1000=698, 9/1000×500=4.5, w=(895-698)/1000×1000=197, h=(158-9)/1000×500=74.5
    assert preds[0]["box"] == {"x": 698.0, "y": 4.5, "w": 197.0, "h": 74.5}
    assert preds[0]["confidence"] == 1.0  # 块无分数，写死 1.0
    assert preds[0]["raw"] == "type=image"  # 实证形态是 type=image + sub_type=seal


def test_mineru_json_result_content_list_already_list(mock_server, page_image):
    """content_list 已是 list（非字符串）时容错直用；type=="seal" 兼容匹配。"""
    payload = {
        "results": {"s": {"content_list": [{"type": "seal", "bbox": [500, 500, 600, 600]}]}}
    }
    _mineru_task_routes(mock_server, payload, ct="application/json")
    preds = adapters.run_mineru_seal(page_image, base_url=mock_server)
    assert preds[0]["box"] == {"x": 500.0, "y": 250.0, "w": 100.0, "h": 50.0}


def test_mineru_zip_branch_still_works(mock_server, page_image):
    """多文件任务 zip 分支保留：list-of-pages + v2 命名，bbox 0-1000 换算。"""
    cl = [
        [  # 每页 = list of blocks
            {"type": "seal", "bbox": [10, 20, 110, 120]},
            {"type": "seal", "bbox": [1, 1, 1, 1]},  # 退化框丢弃
            {"type": "text", "text": "正文"},
        ]
    ]
    result = _zip_result(cl)
    ReplayingHandler.routes["/tasks"] = [(202, {"task_id": "t1"}, 0)]
    ReplayingHandler.routes["/tasks/t1"] = [(200, {"status": "completed"}, 0)]
    ReplayingHandler.routes["/tasks/t1/result"] = [(200, result, 0, "application/zip")]
    preds = adapters.run_mineru_seal(page_image, base_url=mock_server, poll_interval=0.01)
    assert len(preds) == 1
    assert preds[0]["kind"] == "seal"
    # 0-1000 × 页 1000×500：x=10, y=10, w=100, h=50
    assert preds[0]["box"] == {"x": 10.0, "y": 10.0, "w": 100.0, "h": 50.0}
    assert preds[0]["confidence"] == 1.0
    # 请求形态对齐 T0 probe_mineru.py（不带 zip 开关 + lang_list=ch）
    body = ReplayingHandler.requests[0]["body"]
    assert b"lang_list" in body and b"return_content_list" in body


def test_mineru_flat_list_and_v1_content_list(mock_server, page_image):
    """扁平 list 也处理；zip 只有 *_content_list.json（无 v2）时回退取 v1。"""
    cl = [{"type": "seal", "bbox": [0, 0, 50, 50]}]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("doc/pipeline/doc_content_list.json", json.dumps(cl))
    ReplayingHandler.routes["/tasks"] = [(202, {"task_id": "t9"}, 0)]
    ReplayingHandler.routes["/tasks/t9"] = [(200, {"state": "done"}, 0)]  # state 键 + done 终态
    ReplayingHandler.routes["/tasks/t9/result"] = [(200, buf.getvalue(), 0, "application/zip")]
    preds = adapters.run_mineru_seal(page_image, base_url=mock_server)
    assert preds[0]["box"] == {"x": 0.0, "y": 0.0, "w": 50.0, "h": 25.0}  # h: 50/1000×500


def test_mineru_failed_task_raises(mock_server, page_image):
    ReplayingHandler.routes["/tasks"] = [(202, {"task_id": "t2"}, 0)]
    ReplayingHandler.routes["/tasks/t2"] = [(200, {"status": "failed"}, 0)]
    with pytest.raises(adapters.ArmError, match="failed"):
        adapters.run_mineru_seal(page_image, base_url=mock_server)


def test_mineru_poll_timeout(mock_server, page_image):
    ReplayingHandler.routes["/tasks"] = [(202, {"task_id": "t3"}, 0)]
    ReplayingHandler.routes["/tasks/t3"] = [(200, {"status": "processing"}, 0)]
    with pytest.raises(adapters.ArmError, match="超时"):
        adapters.run_mineru_seal(
            page_image, base_url=mock_server, poll_timeout=0.2, poll_interval=0.05
        )


# ---- paddlex ----------------------------------------------------------------


def test_paddlex_missing_raises_clear(page_image, monkeypatch):
    builtins = __import__("builtins")
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "paddlex":
            raise ImportError("No module named 'paddlex'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(RuntimeError, match="paddlex 未安装"):
        adapters.run_paddlex_seal(page_image)


class _FakePipeline:
    """seal_recognition 产线 fake：predict 回放 T4 实测 schema（task-t4-report.md）。"""

    def __init__(self, results):
        self.results = results
        self.calls = []

    def predict(self, image_path):
        self.calls.append(image_path)
        return self.results


def _install_fake_paddlex(monkeypatch, pipeline):
    """注入 fake paddlex 模块，统计 create_pipeline 调用次数。"""
    import sys
    import types

    counts = {"create": 0}

    def fake_create_pipeline(name):
        counts["create"] += 1
        counts.setdefault("names", []).append(name)
        return pipeline

    mod = types.ModuleType("paddlex")
    mod.create_pipeline = fake_create_pipeline
    monkeypatch.setitem(sys.modules, "paddlex", mod)
    adapters._PIPELINE_CACHE.clear()
    return counts


@pytest.fixture(autouse=True)
def _clean_pipeline_cache():
    """模块级 pipeline 缓存跨测试持久，逐测清防串。"""
    adapters._PIPELINE_CACHE.clear()
    yield
    adapters._PIPELINE_CACHE.clear()


def test_paddlex_real_result_schema_and_default_pipeline_name(page_image, monkeypatch):
    """真实输出 SealRecognitionResult（云端含章页 p11 实测）：
    layout_det_res.boxes 里 label=="seal" → coordinate 页像素 + score。"""
    pipeline = _FakePipeline(
        [
            {
                "layout_det_res": {
                    "boxes": [
                        {"label": "seal", "score": 0.96, "coordinate": [1874, 0, 2474, 405]},
                        {"label": "text", "score": 0.9, "coordinate": [0, 0, 100, 50]},
                    ]
                },
                "seal_res_list": [{"dt_polys": [[[1874, 10], [2470, 20]]], "rec_texts": ["XX局"]}],
            }
        ]
    )
    counts = _install_fake_paddlex(monkeypatch, pipeline)
    monkeypatch.delenv("T3_PADDLEX_PIPELINE", raising=False)
    preds = adapters.run_paddlex_seal(page_image)
    assert counts["create"] == 1
    assert counts["names"] == ["seal_recognition"]  # 官方产线名
    assert preds == [
        {"kind": "seal", "box": {"x": 1874.0, "y": 0.0, "w": 600.0, "h": 405.0},
         "confidence": 0.96, "raw": "layout:seal"}
    ]


def test_paddlex_fallback_dt_polys(page_image, monkeypatch):
    """layout 无 seal → 回退 seal_res_list dt_polys 外接矩形并集。"""
    pipeline = _FakePipeline(
        [
            {
                "layout_det_res": {"boxes": [{"label": "text", "score": 0.9, "coordinate": [0, 0, 10, 10]}]},
                "seal_res_list": [
                    {"dt_polys": [[[100, 100], [200, 100], [200, 180], [100, 180]]], "score": 0.5}
                ],
            }
        ]
    )
    _install_fake_paddlex(monkeypatch, pipeline)
    preds = adapters.run_paddlex_seal(page_image, pipeline_name="seal_recognition")
    assert preds[0]["box"] == {"x": 100.0, "y": 100.0, "w": 100.0, "h": 80.0}
    assert preds[0]["raw"] == "dt_polys"


def test_paddlex_native_coordinate_form(page_image, monkeypatch):
    """兼容旧 res["boxes"] coordinate 四元组形态（score）。"""
    pipeline = _FakePipeline(
        [{"boxes": [{"coordinate": [10, 20, 110, 120], "score": 0.7}]}]
    )
    _install_fake_paddlex(monkeypatch, pipeline)
    preds = adapters.run_paddlex_seal(page_image, pipeline_name="seal_recognition")
    assert preds[0]["box"] == {"x": 10.0, "y": 20.0, "w": 100.0, "h": 100.0}
    assert preds[0]["confidence"] == 0.7


def test_paddlex_pipeline_cached_across_pages(page_image, monkeypatch):
    """lazy 单例：同 pipeline 名两次调用只 create 一次（冷启动 35-40s 不逐页重付）。"""
    pipeline = _FakePipeline([{"boxes": [{"x": 1, "y": 2, "w": 3, "h": 4, "confidence": 0.9}]}])
    counts = _install_fake_paddlex(monkeypatch, pipeline)
    adapters._PIPELINE_CACHE.clear()
    try:
        adapters.run_paddlex_seal(page_image, pipeline_name="seal_recognition")
        adapters.run_paddlex_seal(page_image, pipeline_name="seal_recognition")
        assert counts["create"] == 1
        assert len(pipeline.calls) == 2  # predict 逐页执行
        # 不同名各建各的
        adapters.run_paddlex_seal(page_image, pipeline_name="other")
        assert counts["create"] == 2
    finally:
        adapters._PIPELINE_CACHE.clear()


# ---- sigdet -----------------------------------------------------------------


def test_sigdet_01_to_pixel(mock_server, page_image):
    ReplayingHandler.routes["/detect"] = [
        (
            200,
            {
                "boxes": [
                    {"x": 0.1, "y": 0.2, "width": 0.3, "height": 0.4, "confidence": 0.87}
                ],
                "mode": "full",
            },
            0,
        )
    ]
    preds = adapters.run_sigdet(page_image, base_url=mock_server)
    assert preds[0]["kind"] == "signature"
    assert preds[0]["box"] == {"x": 100.0, "y": 100.0, "w": 300.0, "h": 200.0}
    assert preds[0]["confidence"] == 0.87


def test_sigdet_connection_error_retries(dead_url, page_image):
    with pytest.raises(adapters.ArmError, match="重试"):
        adapters.run_sigdet(page_image, base_url=dead_url, timeout=0.2)


def test_sigdet_timeout_env_override(mock_server, page_image, monkeypatch):
    """真实扫描页 CPU 分块 >120s：sigdet 臂默认超时 300s，T3_SIGDET_TIMEOUT 可覆盖。"""
    captured = {}
    real_post = adapters._post_with_retry

    def fake_post(url, **kwargs):
        captured["timeout"] = kwargs.get("timeout")
        return real_post(url, **kwargs)

    monkeypatch.setattr(adapters, "_post_with_retry", fake_post)
    ReplayingHandler.routes["/detect"] = [(200, {"boxes": [], "mode": "full"}, 0)]
    monkeypatch.delenv("T3_SIGDET_TIMEOUT", raising=False)
    adapters.run_sigdet(page_image, base_url=mock_server)
    assert captured["timeout"] == 300.0
    monkeypatch.setenv("T3_SIGDET_TIMEOUT", "7.5")
    adapters.run_sigdet(page_image, base_url=mock_server)
    assert captured["timeout"] == 7.5
    # 显式入参最优先
    adapters.run_sigdet(page_image, base_url=mock_server, timeout=0.2)
    assert captured["timeout"] == 0.2


# ---- 统一入口 ---------------------------------------------------------------


def test_run_arm_unknown_arm(page_image):
    with pytest.raises(adapters.ArmError, match="未知臂"):
        adapters.run_arm("gpt", page_image)


def test_run_arm_dispatch(page_image, monkeypatch):
    called = {}
    monkeypatch.setattr(adapters, "run_sigdet", lambda p, **kw: called.setdefault("hit", p))
    adapters.run_arm("sigdet", page_image)
    assert called["hit"] == page_image
