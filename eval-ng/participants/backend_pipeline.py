"""backend 生产管线参赛者：HTTP 调用生产 API，不改 backend。

语义对齐旧 eval/scripts/common_api.py + run_eval.py e2e 层（旧文件冻结只读）：
- pdf：逐页 vision（force=True）→ 每页一个 Answer（ENTITY_TABLE / PERF_ONLY）
- docx/txt：parse + hybrid NER → 单个 Answer（ARTIFACT_FILE，产出成品路径由调用方给）
- encrypted_pdf：期望拒绝 → ROBUSTNESS 形态
"""

from __future__ import annotations

import httpx

from ..engine.protocol import Answer, AnswerForm, ExamItem


class BackendPipelineParticipant:
    name = "backend-pipeline"
    answer_form = AnswerForm.ARTIFACT_FILE  # 缺省形态；probe 按 item.form 决定
    supports = None

    DOC_LEVEL_CARRIERS = {"docx", "txt"}

    def __init__(self, base_url: str, username: str, password: str, timeout: float = 600.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._client = httpx.AsyncClient(timeout=timeout)
        self._token: str | None = None
        self._username = username
        self._password = password

    async def _ensure_login(self):
        if self._token:
            return
        resp = await self._client.post(f"{self.base_url}/api/v1/auth/login",
                                       json={"username": self._username,
                                             "password": self._password})
        if resp.status_code != 200:
            resp = await self._client.post(f"{self.base_url}/api/v1/auth/register",
                                           json={"username": self._username,
                                                 "password": self._password})
            resp.raise_for_status()
            resp = await self._client.post(f"{self.base_url}/api/v1/auth/login",
                                           json={"username": self._username,
                                                 "password": self._password})
        resp.raise_for_status()
        self._token = resp.json()["access_token"]
        self._client.headers["Authorization"] = f"Bearer {self._token}"

    async def _upload(self, path: str) -> str:
        await self._ensure_login()
        with open(path, "rb") as fh:
            resp = await self._client.post(f"{self.base_url}/api/v1/files/upload",
                                           files={"file": (path.rsplit("/", 1)[-1], fh)})
        resp.raise_for_status()
        return resp.json()["id"]

    async def _delete(self, file_id: str):
        try:
            await self._client.delete(f"{self.base_url}/api/v1/files/{file_id}")
        except Exception:  # noqa: BLE001 — 尽力清理
            pass

    async def probe(self, item: ExamItem) -> Answer:
        path = item.payload.get("path", "")
        carrier = item.payload.get("carrier") or path.rsplit(".", 1)[-1].lower()
        try:
            if item.form is AnswerForm.ROBUSTNESS:
                rejected = await self._probe_reject(path)
                return Answer(form=AnswerForm.ROBUSTNESS, rejected=rejected, latency_sec=0.0)
            if carrier == "pdf":
                return await self._probe_pdf(path, item)
            # docx/txt 族：parse + hybrid NER
            import time

            await self._ensure_login()
            started = time.perf_counter()
            file_id = await self._upload(path)
            try:
                parse = await self._client.get(f"{self.base_url}/api/v1/files/{file_id}/parse")
                parse.raise_for_status()
                ner = await self._client.post(f"{self.base_url}/api/v1/files/{file_id}/ner/hybrid")
                ner.raise_for_status()
                if ner.json().get("status") == "recognition_failed":
                    raise RuntimeError("recognition_failed")
            finally:
                await self._delete(file_id)
            latency = time.perf_counter() - started
            return Answer(form=AnswerForm.ARTIFACT_FILE,
                          artifact_path=path, latency_sec=latency)
        except Exception as exc:  # noqa: BLE001
            return Answer(form=item.form, latency_sec=0.0, warnings=[f"error: {exc}"])

    async def _probe_reject(self, path: str) -> bool:
        """上传后首个请求 4xx/5xx = 拒绝。"""
        file_id = await self._upload(path)
        try:
            resp = await self._client.post(f"{self.base_url}/api/v1/redaction/{file_id}/vision",
                                           json={"page": 1, "force": True})
            return resp.status_code >= 400
        finally:
            await self._delete(file_id)

    async def _probe_pdf(self, path: str, item: ExamItem) -> Answer:
        """逐页 vision；单文件一个 Answer，实体聚合+逐页耗时在 pages 内。"""
        import time

        total_pages = int(item.payload.get("pages") or 0) or 1
        file_id = await self._upload(path)
        entities: dict[str, list[str]] = {}
        walls: list[float] = []
        try:
            for page in range(1, total_pages + 1):
                started = time.perf_counter()
                resp = await self._client.post(
                    f"{self.base_url}/api/v1/redaction/{file_id}/vision",
                    json={"page": page, "force": True})
                resp.raise_for_status()
                walls.append(time.perf_counter() - started)
                for box in resp.json().get("bounding_boxes", []):
                    value = box.get("original_text") or ""
                    etype = box.get("type") or ""
                    if value and etype:
                        entities.setdefault(etype, []).append(value)
        finally:
            await self._delete(file_id)
        return Answer(form=AnswerForm.ENTITY_TABLE, entities=entities,
                      latency_sec=sum(walls))

    async def aclose(self):
        await self._client.aclose()
