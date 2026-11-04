"""长期记忆 HTTP 路由。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Response

from assistant.api.v1.schemas import (
    MemoryCandidateRequest,
    MemoryCreateRequest,
    MemoryResponse,
    MemoryUpdateRequest,
)
from runtime.container import GatewayRuntime
from services.memory.memory import extract_memory_candidates


def build_memory_router(runtime: GatewayRuntime) -> APIRouter:
    router = APIRouter(tags=["长期记忆"])

    @router.post(
        "/api/v1/memory",
        response_model=MemoryResponse,
        status_code=201,
        summary="创建长期记忆",
        description="为当前用户创建一条长期记忆。",
    )
    async def create_memory(payload: MemoryCreateRequest) -> MemoryResponse:
        return MemoryResponse.model_validate(
            runtime.require_memory().create(**payload.model_dump())
        )

    @router.get(
        "/api/v1/memory",
        response_model=list[MemoryResponse],
        summary="获取长期记忆",
        description="按用户和记忆类型查询长期记忆。",
    )
    async def list_memories(
        user_id: str = Query(min_length=1, max_length=64),
        memory_type: str | None = Query(default=None, max_length=64),
        limit: int = Query(default=50, ge=1, le=100),
    ) -> list[MemoryResponse]:
        return [
            MemoryResponse.model_validate(item)
            for item in runtime.require_memory().list(
                user_id=user_id, memory_type=memory_type, limit=limit
            )
        ]

    @router.post(
        "/api/v1/memory/candidates/extract",
        response_model=list[MemoryResponse],
        status_code=201,
        summary="提取记忆候选",
        description="从输入消息中提取并保存长期记忆候选。",
    )
    async def extract_memory_candidate(payload: MemoryCandidateRequest) -> list[MemoryResponse]:
        store = runtime.require_memory()
        return [
            MemoryResponse.model_validate(store.create(user_id=payload.user_id, **candidate))
            for candidate in extract_memory_candidates(payload.message)
        ]

    @router.get(
        "/api/v1/memory/candidates",
        response_model=list[MemoryResponse],
        summary="获取记忆候选",
        description="获取当前用户待确认的记忆候选列表。",
    )
    async def list_memory_candidates(
        user_id: str = Query(min_length=1, max_length=64),
        limit: int = Query(default=50, ge=1, le=100),
    ) -> list[MemoryResponse]:
        # 长期记忆是可选能力；未装配时查询结果为空，不把关闭状态伪装成系统故障。
        if runtime.memory_service is None:
            return []
        return [
            MemoryResponse.model_validate(item)
            for item in runtime.memory_service.list_candidates(user_id=user_id, limit=limit)
        ]

    @router.post(
        "/api/v1/memory/{memory_id}/confirm",
        response_model=MemoryResponse,
        summary="确认记忆",
        description="确认一条记忆候选并将其设为有效。",
    )
    async def confirm_memory(memory_id: str, user_id: str) -> MemoryResponse:
        result = runtime.require_memory().update(
            memory_id, user_id=user_id, fields={"status": "ACTIVE", "confirmed": True}
        )
        if result is None or result.get("status") != "ACTIVE":
            raise HTTPException(status_code=404, detail="待确认记忆不存在")
        return MemoryResponse.model_validate(result)

    @router.post(
        "/api/v1/memory/{memory_id}/reject",
        response_model=MemoryResponse,
        summary="拒绝记忆",
        description="拒绝一条记忆候选并将其归档。",
    )
    async def reject_memory(memory_id: str, user_id: str) -> MemoryResponse:
        result = runtime.require_memory().update(
            memory_id, user_id=user_id, fields={"status": "ARCHIVED", "confirmed": False}
        )
        if result is None:
            raise HTTPException(status_code=404, detail="待确认记忆不存在")
        return MemoryResponse.model_validate(result)

    @router.patch(
        "/api/v1/memory/{memory_id}",
        response_model=MemoryResponse,
        summary="修改长期记忆",
        description="修改当前用户的一条长期记忆。",
    )
    async def update_memory(
        memory_id: str, user_id: str, payload: MemoryUpdateRequest
    ) -> MemoryResponse:
        result = runtime.require_memory().update(
            memory_id, user_id=user_id, fields=payload.model_dump(exclude_unset=True)
        )
        if result is None:
            raise HTTPException(status_code=404, detail="长期记忆不存在")
        return MemoryResponse.model_validate(result)

    @router.delete(
        "/api/v1/memory/{memory_id}",
        status_code=204,
        summary="删除长期记忆",
        description="删除当前用户的一条长期记忆。",
    )
    async def delete_memory(memory_id: str, user_id: str) -> Response:
        runtime.require_memory().delete(memory_id, user_id=user_id)
        return Response(status_code=204)

    @router.post(
        "/api/v1/memory/cleanup",
        summary="清理过期记忆",
        description="清理指定用户或系统中的过期长期记忆。",
    )
    async def cleanup_memories(
        user_id: str | None = Query(default=None, max_length=64),
    ) -> dict[str, int]:
        return {"deleted_count": int(runtime.require_memory().cleanup_expired(user_id=user_id))}

    @router.post(
        "/api/v1/memory/consolidate",
        summary="整合长期记忆",
        description="按用户和类型整合长期记忆。",
    )
    async def consolidate_memories(
        user_id: str = Query(min_length=1, max_length=64),
        memory_type: str | None = Query(default=None, max_length=64),
    ) -> dict[str, int]:
        return runtime.require_memory().consolidate(user_id=user_id, memory_type=memory_type)

    return router
