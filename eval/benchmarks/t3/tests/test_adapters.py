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
            status, payload, delay = queue.pop(0)
        else:
            status, payload, delay = queue[0]
        if delay:
            time.sleep(delay)
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
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


def test_mineru_seal_full_flow(mock_server, page_image):
    cl = [
        {"type": "image", "sub_type": "seal", "bbox": [10, 20, 110, 120]},
        {"type": "image", "sub_type": "seal", "bbox": [1, 1, 1, 1]},  # 退化框丢弃
        {"type": "text", "text": "正文"},
    ]
    result = _zip_result(cl)
    ReplayingHandler.routes["/tasks"] = [(202, {"task_id": "t1"}, 0)]
    ReplayingHandler.routes["/tasks/t1"] = [
        (200, {"status": "processing"}, 0),
        (200, {"status": "completed"}, 0),
    ]
    ReplayingHandler.routes["/tasks/t1/result"] = [(200, result, 0)]
    preds = adapters.run_mineru_seal(page_image, base_url=mock_server, poll_interval=0.01)
    assert len(preds) == 1
    assert preds[0]["kind"] == "seal"
    # TODO(T2.1) 口径：bbox 按页像素直取
    assert preds[0]["box"] == {"x": 10.0, "y": 20.0, "w": 100.0, "h": 100.0}
    assert preds[0]["confidence"] == 1.0  # 块无分数，写死 1.0


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


def test_paddlex_t4_schema_and_default_pipeline_name(page_image, monkeypatch):
    """默认产线 seal_recognition；T4 实测 boxes=[{x,y,w,h,confidence}] 页像素零换算。"""
    pipeline = _FakePipeline(
        [
            {
                "width": PAGE_W,
                "height": PAGE_H,
                "coordinate_system": "page-pixel-top-left",
                "boxes": [{"x": 779, "y": 859, "w": 278, "h": 280, "confidence": 0.959}],
            }
        ]
    )
    counts = _install_fake_paddlex(monkeypatch, pipeline)
    monkeypatch.delenv("T3_PADDLEX_PIPELINE", raising=False)
    preds = adapters.run_paddlex_seal(page_image)
    assert counts["create"] == 1
    assert counts["names"] == ["seal_recognition"]  # T4 实测官方产线名
    assert preds == [
        {"kind": "seal", "box": {"x": 779.0, "y": 859.0, "w": 278.0, "h": 280.0},
         "confidence": 0.959, "raw": ""}
    ]


def test_paddlex_native_coordinate_form(page_image, monkeypatch):
    """兼容 PaddleX 原生 coordinate 四元组形态（x1,y1,x2,y2 + score）。"""
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


# ---- 统一入口 ---------------------------------------------------------------


def test_run_arm_unknown_arm(page_image):
    with pytest.raises(adapters.ArmError, match="未知臂"):
        adapters.run_arm("gpt", page_image)


def test_run_arm_dispatch(page_image, monkeypatch):
    called = {}
    monkeypatch.setattr(adapters, "run_sigdet", lambda p, **kw: called.setdefault("hit", p))
    adapters.run_arm("sigdet", page_image)
    assert called["hit"] == page_image
