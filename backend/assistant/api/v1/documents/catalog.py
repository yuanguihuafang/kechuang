"""文档查询、版本列表与原始文件下载接口。"""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response

from services.application.identity import TestIdentityResolver
from assistant.api.v1.identity.dependencies import require_request_user_id
from runtime.container import GatewayRuntime
from services.application.document_access import can_download_document


def build_documents_router(
    runtime: GatewayRuntime,
    identity_resolver: TestIdentityResolver,
) -> APIRouter:
    router = APIRouter(tags=["文档权限"])

    @router.get(
        "/api/v1/me/documents", summary="获取我的文档", description="返回当前用户上传的文档列表。"
    )
    async def my_documents(
        request: Request,
        limit: int = Query(default=50, ge=1, le=200),
    ) -> dict[str, object]:
        user_id = require_request_user_id(request)
        documents = runtime.require_lifecycle().list_documents(user_id=user_id, limit=limit)
        return {"documents": documents, "total": len(documents), "user_id": user_id}

    @router.get(
        "/api/v1/me/documents/{document_id}",
        summary="获取我的文档详情",
        description="返回文档及其版本信息。",
    )
    async def my_document_detail(
        document_id: str,
        request: Request,
    ) -> dict[str, object]:
        """个人中心文档详情，仅返回当前用户自己提交的资料和版本。"""

        user_id = require_request_user_id(request)
        document = runtime.require_lifecycle().get_document(document_id, user_id=user_id)
        if document is None:
            raise HTTPException(status_code=404, detail="资料不存在或不属于当前用户")
        return {"document": document, "versions": document.get("versions") or []}

    @router.get(
        "/api/v1/knowledge/documents/{document_id}/file",
        summary="下载文档原文",
        description="预览或下载保存的原始附件。",
    )
    async def download_document_file(
        request: Request,
        document_id: str,
        inline: bool = Query(default=False),
    ) -> Response:
        """下载原始文档；登录身份只从 Bearer Token 获取，公开文档允许匿名下载。"""

        requested_user_id = str(getattr(request.state, "user_id", "") or "").strip()
        effective_role: str | None = None
        if requested_user_id == "admin":
            effective_role = "admin"
        elif requested_user_id:
            _user_id, effective_role, _profile = identity_resolver.resolve(requested_user_id)

        lifecycle = runtime.require_lifecycle()
        version = None
        # 登录用户优先从生命周期记录解析，草稿记录只作为临时附件回退。
        if effective_role in {None, "external", "internal", "admin"}:
            versions = lifecycle.list_versions(document_id)
            version = next(
                (item for item in versions if item.get("status") not in {"PURGED", "RECYCLED"}),
                None,
            )
        # 兼容早期直接上传到 MinIO、尚未写入生命周期表的已索引资料。
        if version is None:
            try:
                storage_files = (
                    await runtime.storage_service.list_files(500) if runtime.storage_service else []
                )
                stored_file = next(
                    (
                        item
                        for item in storage_files
                        if str(item.get("document_id") or "") == document_id
                    ),
                    None,
                )
            except Exception:  # noqa: BLE001 - 存储兜底失败时继续走其他来源
                stored_file = None
            if stored_file is not None:
                version = {
                    "document_id": document_id,
                    "user_id": stored_file.get("user_id"),
                    "scope_id": stored_file.get("scope_id") or "public",
                    "publication_status": "PUBLISHED",
                    "storage_key": stored_file.get("storage_key"),
                    "source_name": stored_file.get("file_name") or document_id,
                }
        attachment = None
        if version is None and effective_role in {"admin", "external"}:
            records = []
            draft_store = getattr(runtime, "draft_store_service", None)
            task_store = getattr(runtime, "task_store", None)
            try:
                if draft_store is not None:
                    if effective_role == "external":
                        records.extend(draft_store.list(user_id=requested_user_id, limit=10000))
                    else:
                        records.extend(draft_store.list_all(limit=10000))
                if task_store is not None:
                    records.extend(task_store.list(operation="user_draft", limit=10000))
            except Exception:  # noqa: BLE001 - 管理员下载回退不能影响版本下载
                records = []
            for record in records:
                if (
                    effective_role == "external"
                    and str(record.get("user_id") or "") != requested_user_id
                ):
                    continue
                draft = record.get("draft") or record.get("draft_preview") or {}
                candidates = [*(draft.get("attachments") or []), *(record.get("attachments") or [])]
                attachment = next(
                    (
                        item
                        for item in candidates
                        if isinstance(item, dict)
                        and str(item.get("document_id") or "") == document_id
                    ),
                    None,
                )
                if attachment is not None:
                    break
            if attachment is not None:
                version = {
                    "document_id": document_id,
                    "user_id": record.get("user_id"),
                    "scope_id": "public",
                    "publication_status": "PUBLISHED",
                    "storage_key": attachment.get("storage_key"),
                    "source_name": attachment.get("file_name") or document_id,
                }
        if not version or str(version.get("document_id")) != document_id:
            raise HTTPException(status_code=404, detail="文档版本不存在")
        if not can_download_document(
            user_role=effective_role,
            requested_user_id=requested_user_id,
            owner_id=version.get("user_id"),
            scope_id=version.get("scope_id"),
            publication_status=version.get("publication_status"),
        ):
            raise HTTPException(status_code=403, detail="当前身份无权下载该文件")
        if effective_role is None and (
            str(version.get("scope_id") or "public") != "public"
            or str(version.get("publication_status") or "") != "PUBLISHED"
        ):
            raise HTTPException(status_code=403, detail="该文件需要登录后访问")
        storage_key = str(version.get("storage_key") or "")
        if not storage_key:
            raise HTTPException(status_code=404, detail="文档原始文件不存在")
        try:
            storage = runtime.storage_service
            if storage is None:
                raise HTTPException(status_code=503, detail="MinIO 存储服务尚未就绪")
            content, media_type, file_name = await storage.download_file(storage_key)
        except (FileNotFoundError, ValueError):
            raise HTTPException(status_code=404, detail="文档原始文件不存在") from None
        disposition = "inline" if inline and media_type == "application/pdf" else "attachment"
        return Response(
            content=content,
            media_type=media_type,
            headers={"Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(file_name)}"},
        )

    return router
