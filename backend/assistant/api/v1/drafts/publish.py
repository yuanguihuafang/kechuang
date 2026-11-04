"""草稿发布路由：校验草稿归属并写入公开知识索引。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from assistant.api.v1.identity.dependencies import require_request_user_id
from core.contracts.workflows import (
    DraftPublishRequest,
)
from runtime.container import GatewayRuntime
from services.application.draft_publishing import DraftPublishingApplicationService


def build_publish_router(runtime: GatewayRuntime, resolve_effective_identity: Any) -> APIRouter:
    router = APIRouter()
    @router.post("/api/v1/drafts/{draft_id}/publish", summary="发布草稿", description="发布草稿并写入 MySQL 和 Milvus。")
    async def publish_draft(
        draft_id: str, payload: DraftPublishRequest, request: Request
    ) -> dict[str, object]:
        payload = payload.model_copy(update={"user_id": require_request_user_id(request)})
        return await DraftPublishingApplicationService(runtime, resolve_effective_identity).publish(
            draft_id, payload
        )

    return router
