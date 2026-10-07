# Issue#56 M1 Task 1 —— 统一转录接口 unify.py 的离线单测。
# requests / time 全部 monkeypatch，零网络、零真实退避等待。
# 与计划底稿的两处差异（均为测试脚手架修正，见 task-1-report）：
#   1) fake_get 按 URL 分派：jsonUrl（bcebos）返回 `pages` 逐行 JSONL——底稿里
#      `pages` 变量未接线、下载 GET 会拿到作业状态信封，任何实现都无法通过断言；
#   2) 云端两条用例补 tmp_path 假 PDF：transcribe 会真实 open 文件路径。
import json, sys
from pathlib import Path
import pytest
import requests

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "eval"))
from gt import unify  # noqa: E402

class FakeResp:
    def __init__(self, payload, status=200):
        self._p, self.status_code, self.text = payload, status, json.dumps(payload)
    def json(self): return self._p

def test_cloud_v6_pages_sorted_by_box(monkeypatch, tmp_path):
    submits, polls = [], []
    def fake_post(url, headers=None, data=None, files=None, timeout=None):
        submits.append(data["model"]); return FakeResp({"data": {"jobId": "J1"}})
    pages = json.dumps([{"result": {"ocrResults": [
        {"prunedResult": {"rec_texts": ["乙行", "甲行"], "rec_boxes": [[200, 100, 300, 120], [10, 10, 80, 30]]}}]}}])
    jsonl = "\n".join(json.dumps(item) for item in json.loads(pages))  # 每行一个 JSON，与真 API 一致
    def fake_get(url, headers=None, timeout=None):
        polls.append(url)
        if len(polls) == 1:
            return FakeResp({"data": {"state": "running", "extractProgress": {"totalPages": 1, "extractedPages": 0}}})
        if "bcebos" not in url:
            return FakeResp({"data": {"state": "done", "resultUrl": {"jsonUrl": "https://x.bj.bcebos.com/r.json?authorization=SECRET"}}})
        out = FakeResp(None)
        out.text = jsonl  # jsonUrl 下载：返回 JSONL 文本
        return out
    monkeypatch.setattr(unify.requests, "post", fake_post)
    monkeypatch.setattr(unify.requests, "get", fake_get)
    monkeypatch.setattr(unify.time, "sleep", lambda s: None)  # 跳过真实退避等待
    pdf = tmp_path / "fake.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    c = unify.CloudVLClient("PP-OCRv6", token="t")
    got = c.transcribe(str(pdf))
    assert got[0]["text_raw"] == "甲行乙行" and got[0]["boxes"][0] == [10, 10, 80, 30]

def test_missing_token_raises(monkeypatch):
    monkeypatch.delenv("CLOUD_VL_TOKEN", raising=False)
    with pytest.raises(unify.MissingTokenError):
        unify.CloudVLClient("PP-OCRv6")

def test_poll_timeout_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(unify.requests, "post", lambda *a, **k: FakeResp({"data": {"jobId": "J"}}))
    monkeypatch.setattr(unify.requests, "get", lambda *a, **k: FakeResp({"data": {"state": "running"}}))
    monkeypatch.setattr(unify.time, "sleep", lambda s: None)
    pdf = tmp_path / "f.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    with pytest.raises(unify.PollTimeoutError):
        unify.CloudVLClient("PP-OCRv6", token="t", poll_interval=0, timeout=0).transcribe(str(pdf))

def test_local_vlmd(monkeypatch):
    def fake_post(url, json=None, timeout=None):
        assert json == {"path": "/x.pdf"} and "/parse" in url
        return FakeResp({"markdown": "# 页", "elapsed_s": 1.0})
    monkeypatch.setattr(unify.requests, "post", fake_post)
    got = unify.LocalVLClient("http://127.0.0.1:8095").transcribe("/x.pdf")
    assert got[0]["text_raw"] == "# 页"

def test_build_clients_spec():
    assert isinstance(unify.build_clients("vlmd:http://h:1"), unify.LocalVLClient)
    monkey_token = "t"
    c = unify.build_clients("cloud:PP-OCRv6", _token=monkey_token)
    assert c.model == "PP-OCRv6"

def test_legacy_vl_model_translated_to_16(monkeypatch, tmp_path):
    # 2026-10-07 实测：旧串 "PaddleOCR-VL" 被云侧派发到 1.0 产品线（结果 URL
    # 路径 pp-ocr-vl-10）且不计量——构造时必须翻译为 "PaddleOCR-VL-1.6"，
    # 且提交体、结果分派都用翻译后的串。
    submits = []
    def fake_post(url, headers=None, data=None, files=None, timeout=None):
        submits.append(data["model"]); return FakeResp({"data": {"jobId": "J1"}})
    jsonl = "\n".join(json.dumps({"result": {"layoutParsingResults": [{"markdown": {"text": t}}]}})
                      for t in ["第一页", "第二页"])
    def fake_get(url, headers=None, timeout=None):
        if "bcebos" not in url:
            return FakeResp({"data": {"state": "done", "resultUrl": {"jsonUrl": "https://x.bj.bcebos.com/r.json?authorization=SECRET"}}})
        out = FakeResp(None)
        out.text = jsonl
        return out
    monkeypatch.setattr(unify.requests, "post", fake_post)
    monkeypatch.setattr(unify.requests, "get", fake_get)
    monkeypatch.setattr(unify.time, "sleep", lambda s: None)
    pdf = tmp_path / "fake.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    c = unify.CloudVLClient("PaddleOCR-VL", token="t")  # legacy 串
    assert c.model == "PaddleOCR-VL-1.6"  # 构造即翻译
    got = c.transcribe(str(pdf))
    assert submits == ["PaddleOCR-VL-1.6"]  # 提交体用 1.6 串
    assert [p["text_raw"] for p in got] == ["第一页", "第二页"]  # VL 分派不受影响

def test_pages_vl_jsonl_two_lines(monkeypatch, tmp_path):
    # T1 deferred minor：_pages_vl（VL JSONL 解析）此前零覆盖——
    # 两行 JSONL（每行一个 result.layoutParsingResults）必须出两页、文本各就各位。
    jsonl = "\n".join([
        json.dumps({"result": {"layoutParsingResults": [{"markdown": {"text": "页甲正文"}}]}}),
        json.dumps({"result": {"layoutParsingResults": [{"markdown": {"text": "页乙正文"}}]}}),
    ])
    def fake_post(url, headers=None, data=None, files=None, timeout=None):
        return FakeResp({"data": {"jobId": "J1"}})
    def fake_get(url, headers=None, timeout=None):
        if "bcebos" not in url:
            return FakeResp({"data": {"state": "done", "resultUrl": {"jsonUrl": "https://x.bj.bcebos.com/r.json?authorization=SECRET"}}})
        out = FakeResp(None)
        out.text = jsonl
        return out
    monkeypatch.setattr(unify.requests, "post", fake_post)
    monkeypatch.setattr(unify.requests, "get", fake_get)
    monkeypatch.setattr(unify.time, "sleep", lambda s: None)
    pdf = tmp_path / "fake.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    got = unify.CloudVLClient("PaddleOCR-VL-1.6", token="t").transcribe(str(pdf))
    assert len(got) == 2
    assert [p["text_raw"] for p in got] == ["页甲正文", "页乙正文"]
    assert all(p["boxes"] is None for p in got)

def test_download_conn_error_masks_url(monkeypatch, tmp_path):
    # 评审 Fix#1：连接级失败（requests.ConnectionError）的消息会内嵌完整签名 URL
    # （urllib3 "Max retries exceeded with url: /r.json?authorization=..."）——
    # 转抛的异常文本必须过 _mask，不含 authorization/SECRET/任何 URL query。
    def fake_post(url, headers=None, data=None, files=None, timeout=None):
        return FakeResp({"data": {"jobId": "J"}})
    json_url = "https://x.bj.bcebos.com/r.json?authorization=SECRET"
    def fake_get(url, headers=None, timeout=None):
        if "bcebos" in url:
            raise requests.ConnectionError("Connection aborted. Max retries exceeded with url: /r.json?authorization=SECRET")
        return FakeResp({"data": {"state": "done", "resultUrl": {"jsonUrl": json_url}}})
    monkeypatch.setattr(unify.requests, "post", fake_post)
    monkeypatch.setattr(unify.requests, "get", fake_get)
    monkeypatch.setattr(unify.time, "sleep", lambda s: None)
    pdf = tmp_path / "f.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    with pytest.raises(RuntimeError) as ei:
        unify.CloudVLClient("PP-OCRv6", token="t").transcribe(str(pdf))
    msg = str(ei.value)
    assert "authorization" not in msg and "SECRET" not in msg and "?" not in msg
    assert "x.bj.bcebos.com/r.json" in msg  # host/path 允许保留（打码只去 ? 之后）
