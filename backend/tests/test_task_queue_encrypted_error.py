"""Issue #32（PR #31 评审 P2-2）：加密 PDF 异常必须穿透流水线内层 except。

钉住两件事：
1. pipeline 内层 ``except Exception`` 不得吞掉 PdfEncryptedError——必须原样
   re-raise，否则 task_queue 顶层 ``except PdfEncryptedError`` 成死代码；
2. worker 顶层捕获后写入条目的 error_message 是友好中文（exc.user_message），
   而非 "PdfEncryptedError: ..." 原始异常串。
"""
from __future__ import annotations

import asyncio

from app.services.file_parser import PdfEncryptedError
from app.services.job_models import JobItemStatus, JobStatus
from app.services.task_queue import SimpleTaskQueue, TaskItem

_USER_MSG = "该 PDF 已加密，请先输入打开密码解锁后再重试"


class _MiniStore:
    """足以支撑识别/匿名化流水线跑通 try 块的最小 JobStore 替身。"""

    def __init__(self) -> None:
        self.status_updates: list[tuple[str, str, str | None]] = []

    def get_job(self, job_id):
        return {"status": JobStatus.PROCESSING.value, "config_json": "{}"}

    def get_item(self, item_id):
        return {"status": JobItemStatus.PENDING.value}

    def update_item_status(self, item_id, status, error_message=None) -> None:
        self.status_updates.append((item_id, getattr(status, "value", str(status)), error_message))

    def update_item_performance(self, item_id, patch) -> None:
        pass

    def touch_job_updated(self, job_id) -> None:
        pass

    def update_job_status(self, job_id, status) -> None:
        pass

    def list_items(self, job_id) -> list:
        return [{"status": JobItemStatus.PENDING.value}]


def _make_task(item_id: str, task_type: str = "recognition") -> TaskItem:
    return TaskItem(
        job_id="job-0032",
        item_id=item_id,
        file_id=f"file-{item_id}",
        task_type=task_type,
        meta={},
    )


def test_pipeline_reraises_pdf_encrypted_error():
    """内层 except Exception 吞不掉 PdfEncryptedError，原样上抛且不写 FAILED。"""

    async def main() -> None:
        queue = SimpleTaskQueue(concurrency=1)
        store = _MiniStore()
        queue._get_store = lambda: store

        async def fake_parse(task: TaskItem) -> None:
            raise PdfEncryptedError(_USER_MSG, error_code="PDF_ENCRYPTED_NEEDS_PASSWORD", needs_password=True)

        async def fake_ner(task: TaskItem, cfg) -> None:  # pragma: no cover
            raise AssertionError("parse 抛出后不应继续进入 NER 阶段")

        queue._parse_file = fake_parse
        queue._run_ner_or_vision = fake_ner

        task = _make_task("enc-1")
        try:
            await queue._run_recognition(task)
        except PdfEncryptedError as exc:
            assert exc.user_message == _USER_MSG
        else:
            raise AssertionError("PdfEncryptedError 应穿透流水线内层 except 上抛")

        # 流水线自己不得把条目标成 FAILED（留给 worker 顶层统一处理）
        assert not [u for u in store.status_updates if u[1] == "failed"]

    asyncio.run(main())


def test_worker_error_message_is_friendly_chinese():
    """worker 顶层捕获后 error_message 写 exc.user_message，而非原始异常串。"""

    async def main() -> None:
        queue = SimpleTaskQueue(concurrency=1)
        store = _MiniStore()
        queue._get_store = lambda: store

        async def fake_parse(task: TaskItem) -> None:
            raise PdfEncryptedError(_USER_MSG, error_code="PDF_ENCRYPTED_NEEDS_PASSWORD", needs_password=True)

        queue._parse_file = fake_parse
        queue.start()
        try:
            queue.enqueue(_make_task("enc-2"))
            for _ in range(500):
                if any(u[0] == "enc-2" and u[1] == "failed" for u in store.status_updates):
                    break
                await asyncio.sleep(0.01)
            failed = [u for u in store.status_updates if u[0] == "enc-2" and u[1] == "failed"]
            assert len(failed) == 1
            msg = failed[0][2] or ""
            assert msg == _USER_MSG
            assert "PdfEncryptedError" not in msg
        finally:
            tasks = queue.stop()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(main())
