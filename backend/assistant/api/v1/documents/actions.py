"""文档动作路由：删除文档及其索引、原文件和解析产物。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from assistant.api.v1.identity.dependencies import require_request_user_id
from core.contracts.workflows import (
    UnifiedDeleteRequest,
)
from runtime.container import GatewayRuntime
from services.application.document_access import can_delete_attachment


def build_document_actions_router(runtime: GatewayRuntime, resolve_effective_identity) -> APIRouter:
    router = APIRouter(tags=["文档权限"])

    @router.delete(
        "/api/v1/knowledge/documents/{document_id}",
        summary="删除文档附件",
        description="普通用户只能删除自己上传的附件；外部用户不能删除其他用户的附件。管理员请使用管理员专用删除接口。",
    )
    async def delete_document(
        document_id: str, payload: UnifiedDeleteRequest, request: Request
    ) -> Response:
        """统一删除：先删 Milvus 索引，再删 MinIO 原始文件及解析产物。"""

        requested_user_id = require_request_user_id(request)
        if requested_user_id != "admin":
            _user_id, role, _profile = resolve_effective_identity(requested_user_id)
            if role == "external":
                versions = runtime.require_lifecycle().list_versions(document_id)
                if not can_delete_attachment(
                    user_role=role,
                    user_id=requested_user_id,
                    document_id=document_id,
                    storage_key=payload.storage_key,
                    records=versions,
                ):
                    raise HTTPException(status_code=403, detail="外部用户只能删除自己上传的附件")

        if runtime.tool_adapter is None:
            raise HTTPException(status_code=503, detail="Supervisor Tool Adapter 尚未就绪")
        result = await runtime.tool_adapter.delete_document(
            {
                "document_id": document_id,
                "storage_key": payload.storage_key,
                "scope_id": payload.scope_id,
                "knowledge_type": payload.knowledge_type,
                "user_id": requested_user_id,
            }
        )
        return JSONResponse(
            status_code=200 if result.get("status") == "DELETED" else 502,
            content={**result, "document_id": document_id},
        )

    return router
