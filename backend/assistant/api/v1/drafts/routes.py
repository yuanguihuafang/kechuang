"""草稿查询与状态变更接口。"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, HTTPException, Query, Request

from assistant.api.v1.identity.dependencies import require_request_user_id
from assistant.api.v1.schemas import (
    DraftAttachmentRemovalRequest,
    DraftCreateRequest,
    DraftSimilarityRequest,
    DraftTransitionRequest,
    DraftUpdateRequest,
)
from core.contracts.tools import ToolContext
from core.contracts.workflows import (
    LifecycleDecisionRequest,
)
from runtime.container import GatewayRuntime
from services.application.drafts import DraftApplicationService

IdentityResolver = Callable[..., tuple[str, str, object | None]]


def _draft_expected_version(request: Request, draft: dict[str, object]) -> int | None:
    raw_version: object = request.headers.get("if-match")
    if raw_version is None:
        for key in ("expected_version", "version", "revision"):
            if key in draft:
                raw_version = draft.pop(key)
                break
    if raw_version is None:
        return None
    try:
        return int(str(raw_version).strip().strip('"'))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="草稿版本必须是整数") from exc


def build_draft_router(
    runtime: GatewayRuntime,
    resolve_effective_identity: IdentityResolver,
) -> APIRouter:
    router = APIRouter(tags=["草稿管理"])

    service = DraftApplicationService(runtime)

    def context(request: Request, thread_id: str | None = None) -> ToolContext:
        user_id, role, _ = resolve_effective_identity(require_request_user_id(request))
        return ToolContext(user_id=user_id, user_role=role, thread_id=thread_id,
                           trace_id=getattr(request.state, "trace_id", None) or "draft-http")

    @router.get("/api/v1/me/drafts", summary="获取我的草稿", description="返回当前用户的草稿列表。")
    async def my_drafts(
        request: Request,
        limit: int = Query(default=50, ge=1, le=100),
    ) -> list[dict[str, object]]:
        return service.list(context=context(request), limit=limit)

    @router.post(
        "/api/v1/drafts", status_code=201, summary="创建草稿", description="创建一条业务草稿。"
    )
    async def create_draft(payload: DraftCreateRequest, request: Request) -> dict[str, object]:
        return await service.create(payload.draft, context=context(request, payload.thread_id))

    @router.get(
        "/api/v1/drafts", summary="获取草稿列表", description="查询当前用户可见的草稿列表。"
    )
    async def list_drafts(
        request: Request,
        limit: int = Query(default=50, ge=1, le=100),
    ) -> list[dict[str, object]]:
        return await my_drafts(request, limit=limit)

    @router.post(
        "/api/v1/drafts/similarity-check",
        summary="检查草稿相似度",
        description="检查草稿内容与已有记录的相似度。",
    )
    async def check_draft_similarity(
        payload: DraftSimilarityRequest, request: Request
    ) -> dict[str, object]:
        user_id, _role, _profile = resolve_effective_identity(require_request_user_id(request))
        if payload.draft.get("draft_type") == "customer_visit" and _role != "internal":
            raise HTTPException(status_code=403, detail="仅内部用户可操作拜访草稿")
        store = runtime.require_draft_store()
        finder = getattr(store, "find_similar", None)
        matches = (
            finder(
                user_id=user_id,
                draft={
                    **payload.draft,
                    "draft_id": payload.draft_id or payload.draft.get("draft_id"),
                },
                threshold=payload.threshold,
            )
            if finder
            else []
        )
        return {"matches": matches, "decision_required": bool(matches)}

    @router.get(
        "/api/v1/drafts/{draft_id}",
        summary="获取草稿详情",
        description="返回指定草稿的完整内容和状态。",
    )
    async def get_draft(draft_id: str, request: Request) -> dict[str, object]:
        return await service.get(draft_id, context=context(request))

    @router.patch(
        "/api/v1/drafts/{draft_id}", summary="修改草稿", description="保存用户编辑后的草稿内容。"
    )
    async def update_draft(
        draft_id: str, payload: DraftUpdateRequest, request: Request
    ) -> dict[str, object]:
        content = dict(payload.draft)
        version = _draft_expected_version(request, content)
        return await service.save(draft_id, content, context=context(request, payload.thread_id),
                                  expected_version=version, status=payload.status)

    @router.post(
        "/api/v1/drafts/{draft_id}/attachments/remove",
        summary="解除草稿附件关联",
        description="只从指定草稿中移除附件关联，不删除原文件或其他草稿的附件。",
    )
    async def remove_draft_attachment(
        draft_id: str, payload: DraftAttachmentRemovalRequest, request: Request
    ) -> dict[str, object]:
        return await service.remove_attachment(
            draft_id,
            context=context(request),
            document_id=payload.document_id,
            storage_key=payload.storage_key,
        )

    @router.post(
        "/api/v1/drafts/{draft_id}/transition",
        summary="修改草稿状态",
        description="按流程修改草稿状态。",
    )
    async def transition_draft(
        draft_id: str, payload: DraftTransitionRequest, request: Request
    ) -> dict[str, object]:
        return await service.transition(draft_id, payload.target_status, context=context(request), note=payload.note)

    @router.delete(
        "/api/v1/me/drafts/{draft_id}",
        summary="逻辑删除我的草稿",
        description="只隐藏个人中心草稿记录，不删除已保存的解析原文或附件。",
    )
    async def delete_my_draft(draft_id: str, request: Request) -> dict[str, object]:
        return await service.delete(draft_id, context=context(request))

    @router.post(
        "/api/v1/me/drafts/{draft_id}/unpublish",
        summary="下架草稿",
        description="下架已发布草稿及其检索内容。",
    )
    async def unpublish_my_draft(
        draft_id: str, payload: LifecycleDecisionRequest, request: Request
    ) -> dict[str, object]:
        ctx = context(request)
        return await service.unpublish(draft_id, payload.model_copy(update={"user_id": ctx.user_id}), context=ctx)

    @router.post(
        "/api/v1/me/drafts/{draft_id}/republish",
        summary="重新上架草稿",
        description="未编辑时只恢复 Milvus；下架后编辑过才同步 MySQL 并重建该草稿索引。",
    )
    async def republish_my_draft(
        draft_id: str, payload: LifecycleDecisionRequest, request: Request
    ) -> dict[str, object]:
        return await service.republish(draft_id, payload, context=context(request))

    return router
