"""统一网关业务 API 路由。

路由只负责 HTTP 入参校验、状态码和调用运行时服务；业务流程由
平台服务和两个进程内 Agent 实现。
"""

from __future__ import annotations

from fastapi import APIRouter

from agents.routes import build_agent_router
from assistant.api.v1.chat.routes import build_chat_router
from assistant.api.v1.chat.threads import build_threads_router
from assistant.api.v1.documents.actions import build_document_actions_router
from assistant.api.v1.documents.catalog import build_documents_router
from assistant.api.v1.documents.lifecycle import build_lifecycle_router
from assistant.api.v1.drafts.publish import build_publish_router
from assistant.api.v1.drafts.routes import build_draft_router
from assistant.api.v1.execution.business import build_business_router
from assistant.api.v1.execution.supervisor import build_supervisor_router
from services.application.identity import TestIdentityResolver
from assistant.api.v1.identity.routes import build_identity_router
from assistant.api.v1.memory.routes import build_memory_router
from runtime.container import GatewayRuntime
from services.application.document_lifecycle import DocumentLifecycleApplicationService


def build_router(runtime: GatewayRuntime) -> APIRouter:
    """创建统一网关业务路由。"""

    router = APIRouter()
    router.include_router(build_memory_router(runtime))
    router.include_router(build_business_router(runtime))
    router.include_router(build_threads_router(runtime))
    router.include_router(build_identity_router(runtime))

    # MySQL reader 在 lifespan 中装配；这里必须动态读取，不能捕获启动前的 None。
    identity_resolver = TestIdentityResolver(lambda: runtime.user_identity_reader)
    resolve_effective_identity = identity_resolver.resolve
    router.include_router(build_documents_router(runtime, identity_resolver))

    router.include_router(build_supervisor_router(runtime, resolve_effective_identity))
    router.include_router(build_chat_router(runtime, resolve_effective_identity))
    router.include_router(build_draft_router(runtime, resolve_effective_identity))
    router.include_router(build_publish_router(runtime, resolve_effective_identity))
    router.include_router(build_document_actions_router(runtime, resolve_effective_identity))
    lifecycle = DocumentLifecycleApplicationService(runtime)
    router.include_router(build_lifecycle_router(runtime, lifecycle.run_action))

    # Agent HTTP 适配器统一归网关装配，Agent 包本身不再负责应用注册。
    router.include_router(build_agent_router(runtime, resolve_effective_identity))

    return router
