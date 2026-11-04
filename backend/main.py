"""科创大模型统一网关的组合根。"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv

# Perception / Retrieval 保持独立 Agent 目录，但统一网关也支持直接执行
# ``python main.py``；启动时显式加入两个包根目录，避免依赖 editable install。
_BACKEND_ROOT = Path(__file__).resolve().parent
# psycopg 异步连接不支持 Windows Proactor 事件循环；必须在创建应用和
# 启动 ASGI 生命周期之前设置，不能只放在 `__main__` 分支里。
if os.name == "nt":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# 让直接执行 `python main.py` 时也能读取本地/服务器端口配置。
# 服务器只放 .env；本地同时存在时，.env.local 覆盖 .env。
load_dotenv(_BACKEND_ROOT / ".env", override=False)
load_dotenv(_BACKEND_ROOT / ".env.local", override=True)

from fastapi import FastAPI, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware

from admin.api.v1.admin import build_admin_router
from assistant.api.v1.identity.dependencies import verify_access_token
from assistant.api.v1.router import build_router
from api.routes import (
    build_core_router,
    collect_persistent_metrics,
)
from core.contracts.errors import error_code_for_status
from runtime.bootstrap import (
    build_runtime,
    selector_loop_factory,
)
from runtime.bootstrap import (
    lifespan as runtime_lifespan,
)
from runtime.config import get_settings
from runtime.container import GatewayRuntime
from services.observability.logging import setup_logger
from services.observability.trace_context import (
    bind_trace_context,
    get_trace_context,
    reset_trace_context,
)

runtime = build_runtime()

logger = setup_logger("gateway", log_path=_BACKEND_ROOT.parent / "logs" / "app.log")


class GatewayTraceMiddleware(BaseHTTPMiddleware):
    """为整条网关到 Agent 的调用链建立同一个 Trace ID。"""

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        token = bind_trace_context(request.headers.get("X-Trace-ID"))
        trace_id = get_trace_context().trace_id
        request.state.trace_id = trace_id
        try:
            response = await call_next(request)
            response.headers["X-Trace-ID"] = trace_id
            return response
        finally:
            reset_trace_context(token)


class AgentAuthMiddleware(BaseHTTPMiddleware):
    """把已签发的智能体 token 转为后端内部 user_id。"""

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        authorization = request.headers.get("Authorization", "")
        if authorization.lower().startswith("bearer ") and not request.url.path.endswith(
            "/auth/login"
        ):
            try:
                user_id = verify_access_token(authorization[7:].strip())
            except StarletteHTTPException as exc:
                return _error_response(
                    request,
                    status_code=exc.status_code,
                    code=_error_code(exc.status_code),
                    message=str(exc.detail or "请求失败"),
                )
            request.state.user_id = user_id
            # 管理员接口使用管理员服务自己的操作身份，不能被普通 Token 的 user_id 覆盖。
            propagate_user_id = not (
                request.url.path.startswith("/api/v1/admin/") or request.url.path.startswith("/mcp")
            )
            if propagate_user_id and request.method in {"GET", "DELETE", "PATCH", "POST", "PUT"}:
                query = dict(request.query_params)
                query["user_id"] = user_id
                from urllib.parse import urlencode

                request.scope["query_string"] = urlencode(query).encode()
            if propagate_user_id and request.headers.get("content-type", "").startswith(
                "application/json"
            ):
                body = await request.body()
                try:
                    data = json.loads(body or b"{}")
                except json.JSONDecodeError:
                    data = None
                if isinstance(data, dict):
                    data["user_id"] = user_id
                    body = json.dumps(data, ensure_ascii=False).encode()
                    request._body = body
        return await call_next(request)


def _trace_id(request: Request) -> str:
    return getattr(request.state, "trace_id", None) or get_trace_context().trace_id


def _error_code(status_code: int) -> str:
    return error_code_for_status(status_code)


def _error_response(
    request: Request,
    *,
    status_code: int,
    code: str,
    message: str,
    details: object = None,
) -> JSONResponse:
    response = JSONResponse(
        status_code=status_code,
        content={
            "error": {"code": code, "message": message, "details": details},
            "trace_id": _trace_id(request),
        },
        headers={"X-Trace-ID": _trace_id(request)},
    )
    return response


class GatewayRateLimitMiddleware(BaseHTTPMiddleware):
    """单实例入口限流；多副本部署时由 Redis 等共享限流器替换。"""

    def __init__(self, app, limit: int = 120, window_seconds: int = 60) -> None:
        super().__init__(app)
        self.limit = max(0, int(limit))
        self.window_seconds = max(1, int(window_seconds))

        def group_limit(env_name: str, default: int) -> int:
            configured = os.getenv(env_name)
            return max(1, int(configured)) if configured is not None else min(self.limit, default)

        self._limits = {
            "default": self.limit,
            "chat": group_limit("API_RATE_LIMIT_CHAT_PER_MINUTE", 30),
            "upload": group_limit("API_RATE_LIMIT_UPLOAD_PER_MINUTE", 10),
            "retrieve": group_limit("API_RATE_LIMIT_RETRIEVE_PER_MINUTE", 60),
            "sync_stop": group_limit("API_RATE_LIMIT_SYNC_STOP_PER_MINUTE", 5),
            "sync_start": group_limit("API_RATE_LIMIT_SYNC_START_PER_MINUTE", 5),
        }
        self._requests: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    @staticmethod
    def _request_group(request: Request) -> str | None:
        path = request.url.path
        if (
            request.method == "OPTIONS"
            or path in {"/health", "/ready", "/metrics"}
            or path.startswith(("/api/v1/health", "/api/v1/metrics"))
            or (request.method == "GET" and path.startswith("/api/v1/perception/images/"))
            or (request.method == "GET" and path.startswith("/api/v1/admin/tasks"))
            or (request.method == "GET" and path.startswith("/api/v1/admin/knowledge/sync-mysql/"))
        ):
            return None
        if path.startswith("/api/v1/admin/knowledge/sync-mysql/") and path.endswith("/stop"):
            return "sync_stop"
        if path == "/api/v1/admin/knowledge/sync-mysql":
            return "sync_start"
        if path.startswith("/api/v1/chat/materials/"):
            return "upload"
        if path.startswith("/api/v1/chat/"):
            return "chat"
        if path in {"/api/v1/knowledge/retrieve", "/api/v1/admin/knowledge/retrieve"}:
            return "retrieve"
        return "default"

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        group = self._request_group(request)
        if self.limit == 0 or group is None:
            return await call_next(request)

        identity = str(getattr(request.state, "user_id", None) or "anonymous")
        client_ip = request.client.host if request.client else "unknown"
        client_key = f"{identity}:{client_ip}:{group}"
        limit = self._limits.get(group, self.limit)
        now = time.monotonic()
        with self._lock:
            requests = self._requests[client_key]
            cutoff = now - self.window_seconds
            while requests and requests[0] <= cutoff:
                requests.popleft()
            if len(requests) >= limit:
                retry_after = max(1, int(requests[0] + self.window_seconds - now + 0.999))
                response = _error_response(
                    request,
                    status_code=429,
                    code="RATE_LIMITED",
                    message="请求过于频繁，请稍后再试",
                    details={"retry_after_seconds": retry_after},
                )
                response.headers["Retry-After"] = str(retry_after)
                return response
            requests.append(now)
        return await call_next(request)


def register_error_handlers(application: FastAPI) -> None:
    """安装网关统一错误响应；路由继续保留原有 HTTP 状态码。"""

    async def validation_error(request: Request, exc: RequestValidationError):
        return _error_response(
            request,
            status_code=422,
            code="VALIDATION_ERROR",
            message="请求参数校验失败",
            details=jsonable_encoder(exc.errors()),
        )

    async def http_error(request: Request, exc: StarletteHTTPException):
        detail = exc.detail
        custom_code = None
        if isinstance(detail, dict):
            nested_error = detail.get("error")
            custom_code = detail.get("error_code") or detail.get("code")
            if isinstance(nested_error, dict):
                custom_code = custom_code or nested_error.get("code")
        if isinstance(detail, dict):
            if exc.status_code >= 500:
                message = {
                    502: "上游服务调用失败",
                    503: "依赖服务不可用",
                }.get(exc.status_code, "服务内部错误")
                details = {
                    key: value for key, value in detail.items() if key not in {"message", "error"}
                } or None
            else:
                message = str(detail.get("message") or detail.get("error") or "请求失败")
                details = detail
        else:
            if exc.status_code >= 500:
                message = {
                    502: "上游服务调用失败",
                    503: "依赖服务不可用",
                }.get(exc.status_code, "服务内部错误")
            else:
                message = str(detail or "请求失败")
            details = None
        return _error_response(
            request,
            status_code=exc.status_code,
            code=str(custom_code or _error_code(exc.status_code)),
            message=message,
            details=details,
        )

    async def unexpected_error(request: Request, exc: Exception):
        logger.error(
            "gateway_unhandled_error",
            exc_info=exc,
            extra={
                "structured": {"event": "gateway_unhandled_error", "error_type": type(exc).__name__}
            },
        )
        return _error_response(
            request,
            status_code=500,
            code="INTERNAL_ERROR",
            message="服务内部错误",
        )

    application.add_exception_handler(RequestValidationError, validation_error)
    application.add_exception_handler(StarletteHTTPException, http_error)
    application.add_exception_handler(Exception, unexpected_error)


@asynccontextmanager
async def app_lifespan(_app: FastAPI):
    """将统一运行时生命周期接入 FastAPI。"""

    async with runtime_lifespan(runtime):
        yield


def _make_lifespan(runtime_context: GatewayRuntime):
    @asynccontextmanager
    async def _lifespan(_app: FastAPI):
        async with runtime_lifespan(runtime_context):
            mcp_manager = getattr(_app.state, "kc_mcp_session_manager", None)
            if mcp_manager is None:
                yield
            else:
                async with mcp_manager.run():
                    yield

    return _lifespan


def _supervisor_metrics(runtime_context: GatewayRuntime = runtime) -> dict[str, object]:
    service = runtime_context.conversation_service
    if service is None:
        return {"count": 0, "status_counts": {}, "error_count": 0}
    return service.metrics.snapshot()


def create_app(runtime_context: GatewayRuntime = runtime) -> FastAPI:
    """创建统一网关应用。"""

    application = FastAPI(
        title="科创大模型知识库平台网关",
        lifespan=_make_lifespan(runtime_context),
    )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    application.add_middleware(
        GatewayRateLimitMiddleware,
        limit=int(os.getenv("API_RATE_LIMIT_PER_MINUTE", "120")),
        window_seconds=60,
    )
    application.add_middleware(AgentAuthMiddleware)
    application.add_middleware(GatewayTraceMiddleware)
    register_error_handlers(application)
    application.include_router(
        build_core_router(
            perception=runtime_context.embedded_perception,
            retrieval=runtime_context.embedded_retrieval,
            supervisor_metrics=lambda: _supervisor_metrics(runtime_context),
            persistent_metrics=lambda: collect_persistent_metrics(),
        )
    )
    application.include_router(build_router(runtime_context))
    application.include_router(build_admin_router(runtime_context))
    settings = get_settings()
    if settings.kc_mcp_enabled:
        from assistant.integrations.mcp_server import build_mcp_app

        mcp_application = build_mcp_app(runtime_context)
        application.state.kc_mcp_session_manager = getattr(
            mcp_application.state, "kc_mcp_session_manager", None
        )
        application.mount(settings.kc_mcp_path, mcp_application)
    return application


app = create_app()


if __name__ == "__main__":
    import uvicorn

    # 依赖初始化可能重置事件循环策略；在 Uvicorn 创建事件循环前再次固定
    # Selector，否则 psycopg async checkpoint 会拒绝 ProactorEventLoop。
    if os.name == "nt":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    # Uvicorn 0.36+ 支持循环工厂；旧版本只接受循环名称。
    loop = selector_loop_factory if not hasattr(uvicorn.config, "LOOP_SETUPS") else "asyncio"
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.getenv("GATEWAY_PORT", "8793")),
        loop=loop,
        log_config=None,
    )
