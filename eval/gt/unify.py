"""统一转录客户端层（Issue#56 M1 / Task 1）。

两类转录客户端（云双模型 / 本地 vl-md）适配同一 TranscriptionClient 协议，
GT 管线后续阶段（normalize / compare / entities）只面向本接口，不感知具体通道：

- CloudVLClient   PaddleOCR AI Studio 云 API（PP-OCRv6 框级 / PaddleOCR-VL 版面级），
                  异步作业：multipart 提交 → 轮询 → 下载 JSONL。端点、鉴权、
                  optionalPayload、状态机均为实测值，正本见私有仓
                  docs/deploy/云VL-API使用方案.md。
- LocalVLClient   本地 vl-md(8095)（PaddleOCR-VL 1.6，主仓 Issue#40 固化），
                  同步 POST /parse {"path"} → markdown。

安全约定（红线）：
- token 只经环境变量 CLOUD_VL_TOKEN 读取（测试经 _token 显式注入），零硬编码，
  Authorization 头永不入日志/异常；
- resultUrl.jsonUrl 是带签名凭证的公网 URL（约 7 天有效）：只解析不打印，
  任何日志/异常文本里出现 URL 一律先过 _mask() 截去 `?` 之后的签名参数；
  留档只允许记 jobId。
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Protocol

import requests

logger = logging.getLogger(__name__)

JOB_URL = "https://paddleocr.aistudio-app.com/api/v2/ocr/jobs"

POLL_INTERVAL_START = 5.0   # 轮询间隔起点（秒）
POLL_INTERVAL_CAP = 30.0    # ×1.5 几何退避封顶（秒）
POLL_TIMEOUT = 300.0        # 轮询总时长上限（秒）
HTTP_TIMEOUT = 120.0        # 单次 HTTP 请求超时（秒）


class MissingTokenError(RuntimeError):
    """CLOUD_VL_TOKEN 未设置且未显式传入 token。"""


class PollTimeoutError(RuntimeError):
    """云作业轮询超过 timeout 秒仍未 done。"""


def _mask(url: str) -> str:
    """打码 URL：截去 `?` 之后的签名参数（resultUrl 红线，宁过掩不泄漏）。"""
    return url.split("?", 1)[0]


class TranscriptionPage(dict):
    """一页转录结果：{"text_raw": str, "boxes": list|None}。

    v6：text_raw 为按 rec_boxes (y, x) 阅读序拼接的 rec_texts，boxes 为排序后框列表；
    VL / vl-md：text_raw 为整页 markdown.text，boxes 为 None。
    """

    def __init__(self, text_raw: str, boxes: list | None = None):
        super().__init__(text_raw=text_raw, boxes=boxes)


class TranscriptionClient(Protocol):
    """统一转录协议：本地文件路径进，逐页转录结果出。"""

    def transcribe(self, file_path: str) -> list[TranscriptionPage]:
        ...


class CloudVLClient:
    """PaddleOCR AI Studio 云作业客户端（异步：提交 → 轮询 → 下载 JSONL）。"""

    def __init__(self, model: str, token: str | None = None, base: str = JOB_URL,
                 poll_interval: float = POLL_INTERVAL_START, timeout: float = POLL_TIMEOUT):
        self.model = model
        self.base = base.rstrip("/")
        self.poll_interval = poll_interval
        self.timeout = timeout
        self.token = token if token is not None else os.environ.get("CLOUD_VL_TOKEN")
        if not self.token:
            raise MissingTokenError(
                "环境变量 CLOUD_VL_TOKEN 未设置且未显式传入 token"
                "（获取方式见私有仓 docs/deploy/云VL-API使用方案.md）")

    # -- TranscriptionClient 协议 ------------------------------------------

    def transcribe(self, file_path: str) -> list[TranscriptionPage]:
        job_id = self._submit(file_path)
        json_url = self._poll(job_id)
        pages = self._download_parse(json_url)
        logger.info("云转录完成 jobId=%s model=%s 页数=%d jsonUrl=%s",
                    job_id, self.model, len(pages), _mask(json_url))
        return pages

    # -- 作业三步 -----------------------------------------------------------

    def _headers(self) -> dict:
        # Authorization 头只在请求里用，任何日志/异常都不打印它。
        return {"Authorization": f"bearer {self.token}"}

    def _submit(self, file_path: str) -> str:
        optional_payload = json.dumps({
            "useDocOrientationClassify": False,
            "useDocUnwarping": False,
            "useTextlineOrientation": False,
        })
        with open(file_path, "rb") as f:
            resp = requests.post(
                self.base,
                headers=self._headers(),
                data={"model": self.model, "optionalPayload": optional_payload},
                files={"file": f},
                timeout=HTTP_TIMEOUT,
            )
        if resp.status_code != 200:
            raise RuntimeError(
                f"云作业提交失败 status={resp.status_code} body[:500]={_mask(resp.text[:500])}")
        job_id = (resp.json().get("data") or {}).get("jobId")
        if not job_id:
            raise RuntimeError("云作业提交响应缺 data.jobId")
        logger.info("云作业提交成功 jobId=%s model=%s", job_id, self.model)
        return job_id

    def _poll(self, job_id: str) -> str:
        """轮询直到 done，返回 resultUrl.jsonUrl（只解析，永不打印原文）。"""
        url = f"{self.base}/{job_id}"
        deadline = time.monotonic() + self.timeout
        wait = self.poll_interval
        while True:
            resp = requests.get(url, headers=self._headers(), timeout=30)
            if resp.status_code != 200:
                raise RuntimeError(
                    f"云作业轮询失败 jobId={job_id} status={resp.status_code} "
                    f"body[:500]={_mask(resp.text[:500])}")
            data = resp.json().get("data") or {}
            state = data.get("state")
            if state == "done":
                json_url = (data.get("resultUrl") or {}).get("jsonUrl")
                if not json_url:
                    raise RuntimeError(f"云作业 done 但缺 resultUrl.jsonUrl jobId={job_id}")
                return json_url
            if state not in ("pending", "running"):
                raise RuntimeError(
                    f"云作业终态失败 jobId={job_id} state={state} "
                    f"errorMsg={_mask(data.get('errorMsg') or '')}")
            if time.monotonic() >= deadline:
                raise PollTimeoutError(
                    f"云作业轮询超时（>{self.timeout:g}s）jobId={job_id} "
                    f"lastState={state} url={_mask(url)}")
            time.sleep(wait)
            wait = min(max(wait * 1.5, POLL_INTERVAL_START), POLL_INTERVAL_CAP)

    def _download_parse(self, json_url: str) -> list[TranscriptionPage]:
        resp = requests.get(json_url, timeout=HTTP_TIMEOUT)
        if resp.status_code != 200:
            raise RuntimeError(
                f"结果 JSONL 下载失败 status={resp.status_code} url={_mask(json_url)}")
        pages: list[TranscriptionPage] = []
        for line in resp.text.splitlines():
            line = line.strip()
            if not line:
                continue
            result = json.loads(line).get("result") or {}
            if self.model == "PP-OCRv6":
                pages.extend(self._pages_v6(result))
            elif self.model == "PaddleOCR-VL":
                pages.extend(self._pages_vl(result))
            else:
                raise ValueError(
                    f"未知模型 {self.model!r}：响应顶层结构必须按模型分派"
                    "（PP-OCRv6 → ocrResults / PaddleOCR-VL → layoutParsingResults）")
        return pages

    # -- 双模型响应结构分派（私有仓方案 §3：两模型顶层结构不同） ------------

    @staticmethod
    def _pages_v6(result: dict) -> list[TranscriptionPage]:
        pages = []
        for item in result.get("ocrResults") or []:
            pruned = item.get("prunedResult") or {}
            texts = pruned.get("rec_texts") or []
            boxes = pruned.get("rec_boxes") or []
            if boxes and len(boxes) == len(texts):
                # 阅读序 = 按 rec_boxes 的 y（再 x）排序；boxes 同步重排。
                pairs = sorted(zip(texts, boxes), key=lambda tb: (tb[1][1], tb[1][0]))
                pages.append(TranscriptionPage(
                    text_raw="".join(t for t, _ in pairs),
                    boxes=[list(b) for _, b in pairs]))
            else:
                # rec_boxes 缺失或与 rec_texts 不对齐：退化为原序拼接（不丢文本）。
                pages.append(TranscriptionPage(text_raw="".join(texts), boxes=None))
        return pages

    @staticmethod
    def _pages_vl(result: dict) -> list[TranscriptionPage]:
        return [
            TranscriptionPage(
                text_raw=(item.get("markdown") or {}).get("text") or "",
                boxes=None)
            for item in result.get("layoutParsingResults") or []
        ]


class LocalVLClient:
    """本地 vl-md(8095) 同步客户端：POST /parse {"path"} → {"markdown", ...}。"""

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    def transcribe(self, file_path: str) -> list[TranscriptionPage]:
        resp = requests.post(f"{self.base_url}/parse", json={"path": file_path}, timeout=300)
        if resp.status_code != 200:
            raise RuntimeError(
                f"vl-md /parse 失败 status={resp.status_code} body[:500]={_mask(resp.text[:500])}")
        body = resp.json()
        return [TranscriptionPage(text_raw=body.get("markdown") or "", boxes=None)]


def build_clients(spec: str, _token: str | None = None) -> TranscriptionClient:
    """按 spec 构建转录客户端。

    - "cloud:PP-OCRv6" / "cloud:PaddleOCR-VL" → CloudVLClient（token 缺省走 CLOUD_VL_TOKEN）
    - "vlmd:http://127.0.0.1:8095"            → LocalVLClient

    `_token` 仅供测试注入（绕过环境变量），生产调用方不要使用。
    """
    kind, _, value = spec.partition(":")
    if kind == "cloud":
        return CloudVLClient(value, token=_token)
    if kind == "vlmd":
        return LocalVLClient(value)
    raise ValueError(f"未知的转录客户端 spec: {spec!r}（支持 cloud:<model> / vlmd:<base_url>）")
