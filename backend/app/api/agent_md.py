"""
Issue#75 喂Agent模式 API 路由

Thin routing layer — business logic lives in
app.services.agent_md_pipeline_service.

五端点：上传（含加密 PDF 处理，复用 Issue#30 file_parser 既有链路与结构化
错误码）/ 状态轮询 / 映射表 / 确认出稿 / 三件套产物下载。所有结构化错误走
AppError envelope（前端 localizeError 按 error_code 映射）。
"""
import asyncio
import logging
import os
import uuid

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import FileResponse

from app.core.auth import require_auth
from app.core.config import settings
from app.core.errors import AppError
from app.core.file_validation import safe_path_in_dir
from app.services import agent_md_pipeline_service as pipeline_mod
from app.services import file_management_service as fms
from app.services.agent_md_types import TaskState
from app.services.file_parser import (
    PdfEncryptedError,
    decrypt_pdf_with_password,
    ensure_pdf_accessible,
)

logger = logging.getLogger(__name__)

router = APIRouter()

_ARTIFACT_SUFFIX = {"md": ".md", "mapping": ".mapping.json", "retained": ".retained_fields.json"}


async def _prepare_pdf(file_bytes: bytes, password: str | None, stored_path: str) -> None:
    """字节落盘 + 加密态处理（Issue#30 既有链路复用，阻塞解密放线程池）。

    未给密码：ensure_pdf_accessible——未加密原样通过；仅权限密码空密码自动
    解除；设了打开密码抛 PdfEncryptedError(PDF_ENCRYPTED_NEEDS_PASSWORD)。
    给了密码：decrypt_pdf_with_password——密码错误抛 PDF_WRONG_PASSWORD，
    正确则解密版本原子替换；未加密幂等通过。
    """
    with open(stored_path, "wb") as f:
        f.write(file_bytes)
    if password:
        await asyncio.to_thread(decrypt_pdf_with_password, stored_path, password)
    else:
        await asyncio.to_thread(ensure_pdf_accessible, stored_path)


@router.post("/agent-md/upload")
async def upload(
    file: UploadFile | None = File(None),
    file_id: str | None = Form(None),
    password: str | None = Form(None),
    owner_id: str = Depends(require_auth),
):
    """上传 PDF 建任务：返回管线 task_id，Stage1（解析→NER）后台进行。

    密码仅在表单出现一次、即用即弃：不落日志、不进错误响应。
    file_id 二选一：给 file_id 时复用 file_store 已登记的上传文件（跳过重复
    上传，playground 分流入口）；给 file 时走常规 multipart 上传。
    """
    if file_id:
        info = fms.file_store.get(file_id)
        if not isinstance(info, dict):
            raise AppError(status_code=404, error_code="FILE_NOT_FOUND", message="文件不存在")
        path = info.get("file_path")
        if not path or not os.path.exists(path):
            raise AppError(status_code=404, error_code="FILE_NOT_FOUND", message="原上传文件已缺失，请重新上传")
        if os.path.splitext(str(path))[1].lower() != ".pdf":
            raise AppError(status_code=400, error_code="UNSUPPORTED_FILE_TYPE", message="仅支持 PDF 文件")
        if not safe_path_in_dir(os.path.realpath(str(path)), settings.UPLOAD_DIR):
            raise AppError(status_code=403, error_code="FORBIDDEN_PATH", message="禁止访问该路径")
        stored = os.path.realpath(str(path))
        filename = str(info.get("original_filename") or info.get("filename") or "upload.pdf")
    else:
        if file is None:
            raise AppError(status_code=400, error_code="MISSING_FILE", message="缺少文件或 file_id")
        ext = os.path.splitext(file.filename or "")[1].lower()
        if ext != ".pdf":
            raise AppError(status_code=400, error_code="UNSUPPORTED_FILE_TYPE", message="仅支持 PDF 文件")
        file_bytes = await file.read()
        os.makedirs(settings.UPLOAD_DIR, exist_ok=True)
        stored = os.path.realpath(os.path.join(settings.UPLOAD_DIR, f"{uuid.uuid4()}.pdf"))
        filename = file.filename or "upload.pdf"
        try:
            await _prepare_pdf(file_bytes, password, stored)
        except PdfEncryptedError as exc:
            # 对齐 files.py decrypt 端点：结构化错误码供前端弹密码框；409=补密码可重试
            raise AppError(status_code=409, error_code=exc.error_code, message=exc.user_message)
    task = await pipeline_mod.get_agent_md_pipeline().create_task(
        stored, filename, owner_id=owner_id
    )
    # 注意：落盘文件名是本端点的 uuid4，返回的是管线 task.task_id，二者不是同一个
    return {"task_id": task.task_id}


@router.get("/agent-md/{task_id}/status")
async def status(task_id: str, owner_id: str = Depends(require_auth)):
    """任务状态轮询（Stage1 进度 / 失败原因）。"""
    task = pipeline_mod.get_agent_md_pipeline().get_task(task_id)
    if task is None:
        raise AppError(status_code=404, error_code="TASK_NOT_FOUND", message="任务不存在")
    return {
        "task_id": task.task_id,
        "state": task.state.value,
        "stage": task.stage,
        "pages_done": task.pages_done,
        "pages_total": task.pages_total,
        "message": task.message,
        "warnings_count": len(task.warnings),
    }


@router.get("/agent-md/{task_id}/mapping")
async def mapping(task_id: str, owner_id: str = Depends(require_auth)):
    """映射表草稿（仅 MAPPING_READY 可读）。"""
    task = _require_ready(task_id)
    return {"items": [m.__dict__ for m in task.mapping]}


@router.post("/agent-md/{task_id}/confirm")
async def confirm(task_id: str, body: dict, owner_id: str = Depends(require_auth)):
    """确认决策并出稿：返回 output_file_id 与三件套下载地址。"""
    _require_ready(task_id)
    task = await pipeline_mod.get_agent_md_pipeline().confirm(task_id, body.get("decisions", []))
    base = f"{settings.API_PREFIX}/agent-md/{task_id}/artifacts"
    return {
        "output_file_id": task.output_file_id,
        "downloads": {"md": f"{base}/md", "mapping": f"{base}/mapping", "retained": f"{base}/retained"},
    }


@router.get("/agent-md/{task_id}/artifacts/{kind}")
async def artifacts(task_id: str, kind: str, owner_id: str = Depends(require_auth)):
    """下载产物：kind ∈ md | mapping | retained。"""
    task = pipeline_mod.get_agent_md_pipeline().get_task(task_id)
    suffix = _ARTIFACT_SUFFIX.get(kind)
    if task is None or task.state != TaskState.COMPLETED or not task.output_file_id or suffix is None:
        raise AppError(status_code=404, error_code="ARTIFACT_NOT_FOUND", message="产物不存在或任务未完成")
    # 产物路径以 file_store 登记为准（confirm 落盘友好文件名，uuid 拼路径在 3f5c36c 后全 404）
    record = fms.file_store.get(task.output_file_id)
    if record is None:
        raise AppError(status_code=404, error_code="ARTIFACT_NOT_FOUND", message="产物不存在或任务未完成")
    if kind == "md":
        path = record.get("output_path")
    else:
        meta = record.get("vl_md_meta") or {}
        path = meta.get("mapping_path") if kind == "mapping" else meta.get("retained_path")
    if not path or not os.path.exists(path):
        raise AppError(status_code=404, error_code="ARTIFACT_NOT_FOUND", message="产物文件缺失")
    return FileResponse(path, filename=os.path.basename(path))


def _require_ready(task_id: str):
    task = pipeline_mod.get_agent_md_pipeline().get_task(task_id)
    if task is None:
        raise AppError(status_code=404, error_code="TASK_NOT_FOUND", message="任务不存在")
    if task.state != TaskState.MAPPING_READY:
        raise AppError(status_code=409, error_code="TASK_NOT_READY", message="映射表尚未就绪")
    return task
