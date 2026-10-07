"""MinerU 3.4 sidecar 客户端（Issue#75 喂Agent并行模式）：提交/轮询/取产物。

线上契约以 Task 0 实测校准为准（.superpowers/sdd/2026-10-08-issue75-agentmd-mineru-plan/
task-0-report.md，MinerU 3.4.5 实测逐字记录），要点：

- POST /tasks（multipart，字段名 ``files``）→ 202 ``{"task_id": ...}``。
  必须显式 ``backend=pipeline``（默认 hybrid-engine 在实例上因显存不足必失败）；
  ``response_format_zip=true``（结果端点回 ZIP 而非 JSON）；
  ``return_content_list=true``（zip 内才有 ``*_content_list_v2.json``）；
  ``return_images=true``（zip 内才有 ``images/``）。
- GET /tasks/{task_id} → 200，``status`` ∈ {pending, processing, completed, failed}，
  **无任何进度字段**（仅 status + 时间戳 + queued_ahead），状态终态只看字面值。
- GET /tasks/{task_id}/result → application/zip（``<task_id>.zip``），zip 内路径
  ``<文件stem>/<parse_method>/<stem>.md``、``<stem>/<parse_method>/<stem>_content_list_v2.json``、
  ``<stem>/<parse_method>/images/*``（纯文本 PDF 可能无 images/）。
- 任务结果保留 24h（task_retention_seconds=86400）；failed 任务取件 → 409；
  未知 task_id → 404。

错误语义对齐 OCRServiceError：MineruParseError(message, transient)。
"""

import asyncio
import io
import json
import logging
import time
import zipfile
from dataclasses import dataclass, field

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

# Task 0 §5：终态字面值只有 completed / failed（pending/processing 及未知一律视为运行中）
_DONE = {"completed"}
_FAILED = {"failed"}


class MineruParseError(Exception):
    """MinerU sidecar 调用失败(不可达/超时/任务失败/产物损坏)。"""

    def __init__(self, message: str, transient: bool = False):
        super().__init__(message)
        self.transient = transient


@dataclass
class MineruResult:
    md: str
    content_list_v2: list[dict] = field(default_factory=list)
    images: dict[str, bytes] = field(default_factory=dict)


class MineruParseClient:
    """16581 MinerU sidecar 异步任务客户端：一文件一任务，提交后轮询至终态再取 zip。"""

    def __init__(self, base_url: str | None = None, timeout: float | None = None,
                 poll_interval: float | None = None,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.base_url = (base_url or settings.MINERU_API_BASE_URL).rstrip("/")
        self.timeout = timeout if timeout is not None else settings.MINERU_TASK_TIMEOUT
        self.poll_interval = (poll_interval if poll_interval is not None
                              else settings.MINERU_POLL_INTERVAL)
        self._transport = transport  # 测试注入 MockTransport 用,生产为 None

    def _async_client(self) -> httpx.AsyncClient:
        kwargs: dict = {"timeout": self.timeout}
        if self._transport is not None:
            kwargs["transport"] = self._transport
        return httpx.AsyncClient(**kwargs)

    @staticmethod
    def _raise(status_code: int, detail: str, task_id: str | None = None) -> None:
        """按状态码归一错误：5xx/429 视为瞬时(可重试)，其余为确定性失败。"""
        transient = status_code >= 500 or status_code == 429
        prefix = f"mineru task {task_id}: " if task_id else "mineru submit: "
        raise MineruParseError(f"{prefix}HTTP {status_code}: {detail}", transient=transient)

    async def submit(self, file_bytes: bytes, filename: str) -> str:
        """提交解析任务，返回 sidecar task_id（实测响应 202）。

        backend/pipeline 为实例实测唯一可用引擎（Task 0 §9：hybrid-engine 内嵌 vLLM
        需 32GiB 显存，生产服务占卡后必失败）；zip+content_list_v2+images 三个开关
        决定产物齐套，缺一 Task 5/7 拿不到对应产物。
        """
        data = {
            "backend": "pipeline",
            "response_format_zip": "true",
            "return_md": "true",
            "return_content_list": "true",
            "return_images": "true",
        }
        try:
            async with self._async_client() as client:
                resp = await client.post(
                    f"{self.base_url}/tasks",
                    data=data,
                    files={"files": (filename, file_bytes, "application/pdf")},
                )
        except httpx.HTTPError as exc:
            raise MineruParseError(
                f"mineru sidecar 不可达({self.base_url}): {exc!r}", transient=True
            ) from exc
        if resp.status_code >= 400:
            self._raise(resp.status_code, resp.text[:200])
        try:
            payload = resp.json()
            task_id = payload["task_id"]
        except (ValueError, KeyError) as exc:
            raise MineruParseError(
                f"mineru submit 响应缺 task_id: {resp.text[:200]}"
            ) from exc
        return task_id

    async def poll(self, task_id: str) -> dict:
        """查询任务状态，归一化为 {"state", "done", "total"}。

        线上无进度字段（Task 0 §8.2），done/total 恒 0，页计数由管线层本地负责；
        state 映射：processing→running，completed→done，failed→failed，未知→running。
        """
        try:
            async with self._async_client() as client:
                resp = await client.get(f"{self.base_url}/tasks/{task_id}")
        except httpx.HTTPError as exc:
            raise MineruParseError(
                f"mineru sidecar 不可达({self.base_url}): {exc!r}", transient=True
            ) from exc
        if resp.status_code == 404:
            raise MineruParseError(f"mineru task not found: {task_id}", transient=False)
        if resp.status_code >= 400:
            self._raise(resp.status_code, resp.text[:200], task_id)
        try:
            raw = resp.json()
        except ValueError as exc:
            raise MineruParseError(
                f"mineru poll 响应非 JSON: {resp.text[:200]}"
            ) from exc
        return {
            "state": self._normalize_state(raw.get("status", "")),
            "done": 0,
            "total": 0,
        }

    async def wait_result(self, task_id: str, on_progress=None) -> MineruResult:
        """轮询至终态并取产物；超时抛 transient MineruParseError。

        on_progress 为同步回调 on_progress(done, total)——sidecar 无进度字段，
        恒 (0, 0)，仅保留接缝供管线层对接（页计数由管线层本地统计）。
        """
        deadline = time.monotonic() + self.timeout
        while True:
            st = await self.poll(task_id)
            if on_progress is not None:
                on_progress(st["done"], st["total"])
            if st["state"] == "done":
                return await self._fetch_result(task_id)
            if st["state"] == "failed":
                raise MineruParseError(f"mineru task failed: {task_id}", transient=False)
            if time.monotonic() >= deadline:
                raise MineruParseError(
                    f"mineru task timeout after {self.timeout}s: {task_id}", transient=True
                )
            await asyncio.sleep(self.poll_interval)

    async def _fetch_result(self, task_id: str) -> MineruResult:
        """取结果 zip 并拆包：md / content_list_v2 / images（三者均可缺省为空）。

        zip 内同时存在 v1（*_content_list.json，扁平）与 v2（*_content_list_v2.json，
        list-of-lists），本客户端只认 v2（Task 0 §6c/§6d）；*_middle.json 忽略。
        """
        try:
            async with self._async_client() as client:
                resp = await client.get(f"{self.base_url}/tasks/{task_id}/result")
        except httpx.HTTPError as exc:
            raise MineruParseError(
                f"mineru sidecar 不可达({self.base_url}): {exc!r}", transient=True
            ) from exc
        if resp.status_code == 409:
            raise MineruParseError(
                f"mineru task failed or not retrievable: {task_id}", transient=False
            )
        if resp.status_code == 404:
            raise MineruParseError(f"mineru task not found: {task_id}", transient=False)
        if resp.status_code >= 400:
            self._raise(resp.status_code, resp.text[:200], task_id)
        md, content_list_v2, images = "", [], {}
        try:
            with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
                entries = zf.namelist()
                for name in entries:
                    if name.endswith("/"):
                        continue
                    blob = zf.read(name)
                    if name.endswith("_content_list_v2.json"):
                        content_list_v2 = json.loads(blob)
                    elif name.endswith(".md") and not md:
                        md = blob.decode("utf-8")
                    elif "images/" in name:
                        images[name] = blob
        except (zipfile.BadZipFile, ValueError) as exc:
            raise MineruParseError(
                f"mineru result 非法 zip: {task_id}: {exc!r}", transient=False
            ) from exc
        if not md:
            logger.warning("mineru result zip 缺 .md: task=%s entries=%d", task_id, len(entries))
        if not content_list_v2:
            logger.warning("mineru result zip 缺 *_content_list_v2.json: task=%s", task_id)
        return MineruResult(md=md, content_list_v2=content_list_v2, images=images)

    @staticmethod
    def _normalize_state(raw: str) -> str:
        low = (raw or "").strip().lower()
        if low in _DONE:
            return "done"
        if low in _FAILED:
            return "failed"
        return "running"

    async def is_available(self) -> bool:
        try:
            async with self._async_client() as client:
                resp = await client.get(f"{self.base_url}/health")
            return resp.status_code == 200
        except httpx.HTTPError:
            return False
