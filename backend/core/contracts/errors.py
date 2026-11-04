"""REST 网关和业务任务共用的机器可读错误码。"""

from __future__ import annotations

from typing import Mapping


class ErrorCode:
    BAD_REQUEST = "BAD_REQUEST"
    AUTHENTICATION_REQUIRED = "AUTHENTICATION_REQUIRED"
    FORBIDDEN = "FORBIDDEN"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    RATE_LIMITED = "RATE_LIMITED"
    UPSTREAM_ERROR = "UPSTREAM_ERROR"
    DEPENDENCY_UNAVAILABLE = "DEPENDENCY_UNAVAILABLE"
    HTTP_ERROR = "HTTP_ERROR"
    INTERNAL_ERROR = "INTERNAL_ERROR"


HTTP_ERROR_CODES: Mapping[int, str] = {
    400: ErrorCode.BAD_REQUEST,
    401: ErrorCode.AUTHENTICATION_REQUIRED,
    403: ErrorCode.FORBIDDEN,
    404: ErrorCode.NOT_FOUND,
    409: ErrorCode.CONFLICT,
    422: ErrorCode.VALIDATION_ERROR,
    429: ErrorCode.RATE_LIMITED,
    502: ErrorCode.UPSTREAM_ERROR,
    503: ErrorCode.DEPENDENCY_UNAVAILABLE,
}


def error_code_for_status(status_code: int) -> str:
    return HTTP_ERROR_CODES.get(
        status_code,
        ErrorCode.HTTP_ERROR if status_code < 500 else ErrorCode.INTERNAL_ERROR,
    )


__all__ = ["ErrorCode", "HTTP_ERROR_CODES", "error_code_for_status"]
