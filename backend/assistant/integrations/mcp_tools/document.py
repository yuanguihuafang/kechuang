"""Owner-scoped document tools for the business MCP server."""

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class DocumentListInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    limit: int = Field(default=50, ge=1, le=200)


class DocumentIdInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    document_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


_DOCUMENT_FIELDS = (
    "document_id",
    "source_name",
    "scope_id",
    "knowledge_type",
    "status",
    "active_version_id",
    "created_at",
    "updated_at",
)
_VERSION_FIELDS = (
    "version_id",
    "version_number",
    "status",
    "publication_status",
    "index_status",
    "parsed_block_count",
    "chunk_count",
    "indexed_count",
    "created_at",
    "updated_at",
)


def _public_document(document: dict[str, Any]) -> dict[str, Any]:
    result = {key: document[key] for key in _DOCUMENT_FIELDS if key in document}
    result["versions"] = [
        {key: version[key] for key in _VERSION_FIELDS if key in version}
        for version in document.get("versions") or []
    ]
    return result


def _parse_summary(
    document_id: str, result: dict[str, Any], *, storage_key: str = ""
) -> dict[str, Any]:
    raw_text = str(result.get("raw_text") or result.get("text") or "")
    if storage_key:
        raw_text = raw_text.replace(storage_key, "[redacted]")
    metadata = result.get("metadata")
    raw_images = metadata.get("images") if isinstance(metadata, dict) else None
    raw_images = raw_images if isinstance(raw_images, list) else []
    images = []
    for image in raw_images:
        if not isinstance(image, dict):
            continue
        image_id = str(image.get("image_id") or "")
        if len(image_id) > 128 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", image_id):
            continue
        page = image.get("pageNumber")
        images.append({"image_id": image_id, "page_number": page if type(page) is int else None})
        if len(images) == 20:
            break
    return {
        "document_id": document_id,
        "parse_status": result.get("status"),
        "raw_text": raw_text[:8000],
        "truncated": len(raw_text) > 8000,
        "page_count": len(result.get("page_texts") or {}),
        "table_count": len(result.get("tables") or []),
        "block_count": len(result.get("blocks") or []),
        "images": images,
        "image_count": len(raw_images),
    }


def register_document_tools(
    mcp: Any,
    runtime: Any,
    annotations: Any,
    request_from_context: Any,
    resolve_context: Any,
    failure: Any,
) -> None:
    """Register document tools using the MCP server's authenticated request context."""

    from mcp.server.fastmcp import Context
    from mcp.types import ToolAnnotations

    def owner_context(ctx: Any) -> Any:
        return resolve_context(runtime, request_from_context(ctx))

    @mcp.tool(
        name="kc_document_list",
        description="List documents uploaded by the authenticated user.",
        annotations=annotations,
        structured_output=True,
    )
    async def kc_document_list(params: DocumentListInput, ctx: Context) -> dict[str, Any]:
        context = owner_context(ctx)
        lifecycle = getattr(runtime, "lifecycle_service", None)
        if lifecycle is None:
            return failure(context, "Document lifecycle service unavailable")
        try:
            documents = lifecycle.list_documents(user_id=context.user_id, limit=params.limit)
            visible = [_public_document(document) for document in documents]
            return {
                "status": "SUCCESS",
                "data": {"documents": visible, "total": len(visible)},
                "error": None,
                "trace_id": context.trace_id,
            }
        except Exception:  # noqa: BLE001 - 保持 MCP 错误响应结构稳定
            return failure(context, "Document list failed")

    @mcp.tool(
        name="kc_document_detail",
        description="Get an owned document and its version status by document_id.",
        annotations=annotations,
        structured_output=True,
    )
    async def kc_document_detail(params: DocumentIdInput, ctx: Context) -> dict[str, Any]:
        context = owner_context(ctx)
        lifecycle = getattr(runtime, "lifecycle_service", None)
        if lifecycle is None:
            return failure(context, "Document lifecycle service unavailable")
        try:
            document = lifecycle.get_document(params.document_id, user_id=context.user_id)
            if document is None:
                return failure(context, "Document not found or not owned by current user")
            return {
                "status": "SUCCESS",
                "data": {"document": _public_document(document)},
                "error": None,
                "trace_id": context.trace_id,
            }
        except Exception:  # noqa: BLE001 - 保持 MCP 错误响应结构稳定
            return failure(context, "Document detail failed")

    @mcp.tool(
        name="kc_document_parse_result",
        description="Read a saved parsing result for an owned document by document_id.",
        annotations=annotations,
        structured_output=True,
    )
    async def kc_document_parse_result(params: DocumentIdInput, ctx: Context) -> dict[str, Any]:
        context = owner_context(ctx)
        lifecycle = getattr(runtime, "lifecycle_service", None)
        storage = getattr(runtime, "storage_service", None)
        if lifecycle is None or storage is None:
            return failure(context, "Document parsing result service unavailable")
        try:
            document = lifecycle.get_document(params.document_id, user_id=context.user_id)
            if document is None:
                return failure(context, "Document not found or not owned by current user")
            result = await storage.load_parse_result(params.document_id)
            if result is None:
                return failure(context, "No saved parsing result for this document")
            version = document.get("active_version") or {}
            return {
                "status": "SUCCESS",
                "data": _parse_summary(
                    params.document_id, result, storage_key=str(version.get("storage_key") or "")
                ),
                "error": None,
                "trace_id": context.trace_id,
            }
        except Exception:  # noqa: BLE001 - 保持 MCP 错误响应结构稳定
            return failure(context, "Document parsing result failed")

    @mcp.tool(
        name="kc_document_parse",
        description="Parse the authenticated user's stored document by document_id.",
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False
        ),
        structured_output=True,
    )
    async def kc_document_parse(params: DocumentIdInput, ctx: Context) -> dict[str, Any]:
        context = owner_context(ctx)
        lifecycle = getattr(runtime, "lifecycle_service", None)
        adapter = getattr(runtime, "tool_adapter", None)
        storage = getattr(runtime, "storage_service", None)
        if lifecycle is None or adapter is None or storage is None:
            return failure(context, "Document parsing service unavailable")
        try:
            document = lifecycle.get_document(params.document_id, user_id=context.user_id)
            if document is None:
                return failure(context, "Document not found or not owned by current user")
            version = document.get("active_version") or {}
            storage_key = str(version.get("storage_key") or "")
            if not storage_key or version.get("version_id") != document.get("active_version_id"):
                return failure(context, "Document active version unavailable")
            parsed = await adapter.parse_document(
                {
                    "document_id": params.document_id,
                    "storage_key": storage_key,
                    "file_name": version.get("source_name") or document.get("source_name") or "",
                    "knowledge_type": version.get("knowledge_type")
                    or document.get("knowledge_type")
                    or "",
                },
                context=context,
            )
            if parsed.get("status") != "SUCCESS":
                return failure(context, "Document parsing failed")
            saved = await storage.load_parse_result(params.document_id)
            if saved is None:
                return failure(context, "Saved parsing result unavailable")
            return {
                "status": "SUCCESS",
                "data": _parse_summary(params.document_id, saved, storage_key=storage_key),
                "error": None,
                "trace_id": context.trace_id,
            }
        except Exception:  # noqa: BLE001 - 不暴露存储或解析器内部细节
            return failure(context, "Document parsing failed")
