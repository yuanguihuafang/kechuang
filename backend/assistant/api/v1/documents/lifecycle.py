"""文档生命周期接口。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import APIRouter, HTTPException, Request

from assistant.api.v1.identity.dependencies import require_request_user_id
from core.contracts.workflows import (
    UNPUBLISH_REASON_CODES,
    LifecycleDecisionRequest,
)
from runtime.container import GatewayRuntime

LifecycleRunner = Callable[
    ..., Awaitable[tuple[dict[str, object] | None, dict[str, object] | None]]
]


def build_lifecycle_router(
    runtime: GatewayRuntime,
    run_lifecycle_action: LifecycleRunner,
) -> APIRouter:
    router = APIRouter(tags=["文档生命周期"])

    @router.post(
        "/api/v1/me/documents/{document_id}/unpublish",
        summary="下架我的文档",
        description="下架当前用户拥有的已发布文档。",
    )
    async def my_unpublish_document(
        document_id: str,
        payload: LifecycleDecisionRequest,
        request: Request,
    ) -> dict[str, object]:
        user_id = require_request_user_id(request)
        reason_code = str(payload.reason_code or "").strip()
        reason_text = str(payload.reason_text or "").strip()
        if reason_code not in UNPUBLISH_REASON_CODES:
            raise HTTPException(status_code=422, detail="请选择有效的下架原因")
        if reason_code == "OTHER" and not reason_text:
            raise HTTPException(status_code=422, detail="选择其他时必须填写下架原因")
        document = runtime.require_lifecycle().get_document(document_id, user_id=user_id)
        if document is None:
            raise HTTPException(status_code=404, detail="资料不存在或不属于当前用户")
        version_id = str(document.get("active_version_id") or "")
        version = runtime.require_lifecycle().get_version(version_id)
        if not version or version.get("publication_status") != "PUBLISHED":
            raise HTTPException(status_code=409, detail="当前资料不是可下架状态")
        result, task = await run_lifecycle_action(
            "unpublish",
            version_id,
            user_id,
            reason_code=reason_code,
            reason_text=reason_text,
        )
        if result is None:
            raise HTTPException(
                status_code=502,
                detail={"message": "下架失败", "task_id": task.get("task_id") if task else None},
            )
        return {
            **result,
            "reason_label": UNPUBLISH_REASON_CODES[reason_code],
            "task_id": task.get("task_id") if task else None,
        }

    @router.post(
        "/api/v1/me/documents/{document_id}/publish",
        summary="发布我的文档",
        description="发布当前用户拥有的文档。",
    )
    async def my_publish_document(
        document_id: str,
        payload: LifecycleDecisionRequest,
        request: Request,
    ) -> dict[str, object]:
        user_id = require_request_user_id(request)
        document = runtime.require_lifecycle().get_document(document_id, user_id=user_id)
        if document is None:
            raise HTTPException(status_code=404, detail="资料不存在或不属于当前用户")
        version_id = str(document.get("active_version_id") or "")
        version = runtime.require_lifecycle().get_version(version_id)
        if not version or version.get("publication_status") != "UNPUBLISHED":
            raise HTTPException(status_code=409, detail="当前资料不是可重新上架状态")
        result, task = await run_lifecycle_action("publish", version_id, user_id)
        if result is None:
            raise HTTPException(
                status_code=502,
                detail={
                    "message": "重新上架失败",
                    "task_id": task.get("task_id") if task else None,
                },
            )
        return {**result, "task_id": task.get("task_id") if task else None}

    return router
