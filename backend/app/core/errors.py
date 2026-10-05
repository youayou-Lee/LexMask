"""Unified error response handling."""
from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.request_id import request_id_var

# 校验错误回显中禁止出现的字段名：pydantic errors() 会带 input 原值，密码
# 字段校验失败时绝不能把明文回显进 422 响应（Issue #32，评审 P2-4）。
_SENSITIVE_INPUT_FIELDS = frozenset({"password"})


def _sanitize_validation_errors(errors: list[dict]) -> list[dict]:
    sanitized: list[dict] = []
    for err in errors:
        if isinstance(err, dict) and _SENSITIVE_INPUT_FIELDS.intersection(
            loc for loc in err.get("loc", ()) if isinstance(loc, str)
        ):
            err = {k: ("[REDACTED]" if k == "input" else v) for k, v in err.items()}
        sanitized.append(err)
    return sanitized


class AppError(Exception):
    """Application error with error code."""
    def __init__(self, status_code: int, error_code: str, message: str, detail: dict = None):
        self.status_code = status_code
        self.error_code = error_code
        self.message = message
        self.detail = detail or {}


def error_response(
    status_code: int,
    error_code: str,
    message: str,
    detail: dict | None = None,
    *,
    extra: dict | None = None,
) -> JSONResponse:
    """Build the public error envelope used by routes and middleware.

    ``extra`` exists only for backwards-compatible top-level aliases (for
    example the legacy ``license`` field).  Every response still exposes the
    canonical error fields.
    """
    # 使用 request_id middleware 中已设置的请求 ID，而非每次生成新 uuid
    rid = request_id_var.get("")
    content = {
        "error_code": error_code,
        "message": message,
        "detail": detail or {},
        "request_id": rid,
    }
    if extra:
        content.update(extra)
    return JSONResponse(status_code=status_code, content=content)


async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    return error_response(exc.status_code, exc.error_code, exc.message, exc.detail)


async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    # 避免将内部异常细节泄露给客户端
    if isinstance(exc.detail, str):
        # 仅对 4xx 暴露原始消息，5xx 使用通用消息
        message = exc.detail if exc.status_code < 500 else "服务器内部错误"
    else:
        message = "请求错误"
    return error_response(
        exc.status_code,
        f"HTTP_{exc.status_code}",
        message,
        exc.detail if isinstance(exc.detail, dict) else {},
    )


async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    return error_response(
        422,
        "VALIDATION_ERROR",
        "请求参数校验失败",
        {"errors": _sanitize_validation_errors(exc.errors())},
    )
