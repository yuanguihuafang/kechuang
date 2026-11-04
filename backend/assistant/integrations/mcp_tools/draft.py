"""Authenticated MCP operations for the existing draft workflow."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, Literal

from fastapi import HTTPException
from mcp.server.fastmcp import Context
from pydantic import BaseModel, ConfigDict, Field, create_model, field_validator, model_validator

from core.contracts.workflows import (
    DraftPublishRequest,
    LifecycleDecisionRequest,
)
from core.contracts.draft_contracts import (
    DRAFT_FIELD_DESCRIPTIONS,
    DRAFT_FIELD_LABELS,
    DRAFT_FIELD_MAX_CHARS,
    draft_fields,
)
from core.contracts.mysql_schema import business_fields
from core.contracts.tools import ToolContext
from services.application.draft_publishing import DraftPublishingApplicationService
from services.application.drafts import DraftApplicationService

DraftType = Literal["demand", "solution", "achievement", "customer_visit"]
_BUSINESS_FIELDS = frozenset(
    field
    for draft_type in ("demand", "solution", "achievement", "customer_visit")
    for field in business_fields(draft_type)
)
_CONTROL_FIELDS = {
    "user_id",
    "user_role",
    "role",
    "identity",
    "operator_id",
    "thread_id",
    "draft_id",
    "status",
    "approval",
    "deleted_by",
    "mysql_record_id",
    "_draft_version",
    "_needs_mysql_sync_on_republish",
    "source_document_id",
}
_DRAFT_FIELD_MODELS = {
    draft_type: create_model(
        f"{draft_type.title()}DraftBusinessFields",
        __config__=ConfigDict(extra="forbid", str_strip_whitespace=True),
        **{
            field: (
                str | None,
                Field(
                    default=None,
                    max_length=DRAFT_FIELD_MAX_CHARS,
                    description=(
                        DRAFT_FIELD_DESCRIPTIONS.get(draft_type, {}).get(field)
                        or DRAFT_FIELD_LABELS.get(draft_type, {}).get(field)
                    ),
                ),
            )
            for field in draft_fields(draft_type)
        },
    )
    for draft_type in ("demand", "solution", "achievement", "customer_visit")
}
_DraftBusinessFields = (
    _DRAFT_FIELD_MODELS["demand"]
    | _DRAFT_FIELD_MODELS["solution"]
    | _DRAFT_FIELD_MODELS["achievement"]
    | _DRAFT_FIELD_MODELS["customer_visit"]
)


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class DraftContent(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    draft_type: DraftType | None = None
    title: str | None = Field(default=None, max_length=DRAFT_FIELD_MAX_CHARS)
    summary: str | None = Field(default=None, max_length=DRAFT_FIELD_MAX_CHARS)
    attachments: list[dict[str, Any]] | None = None
    source_blocks: list[dict[str, Any]] | None = None
    fields: _DraftBusinessFields | None = Field(
        default=None,
        description="仅填写当前草稿类型对应的业务字段。",
    )
    mysql_fields: _DraftBusinessFields | None = Field(
        default=None,
        description="仅填写当前草稿类型对应的 MySQL 业务字段。",
    )


class _ContentInput(_Input):
    draft: DraftContent

    @field_validator("draft", mode="before")
    @classmethod
    def validate_content(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        if _CONTROL_FIELDS.intersection(value):
            raise ValueError("草稿内容不能指定身份或内部控制字段")
        if (set(value) & _BUSINESS_FIELDS) - {"title"}:
            raise ValueError("业务字段必须放在 fields 或 mysql_fields 对象中")
        pending: list[Any] = list(value.values())
        while pending:
            item = pending.pop()
            if isinstance(item, dict):
                if {
                    "user_id",
                    "user_role",
                    "role",
                    "identity",
                    "operator_id",
                    "thread_id",
                }.intersection(item):
                    raise ValueError("草稿内容不能指定用户身份")
                pending.extend(item.values())
            elif isinstance(item, list):
                pending.extend(item)
        if any(
            key in value and not isinstance(value[key], dict) for key in ("fields", "mysql_fields")
        ):
            raise ValueError("草稿字段必须是对象")
        if any(
            key in value and not isinstance(value[key], list)
            for key in ("attachments", "source_blocks")
        ):
            raise ValueError("草稿附件和来源块必须是列表")
        if len(json.dumps(value, ensure_ascii=False, default=str)) > 65536:
            raise ValueError("草稿内容过长")
        return value


class DraftCreateInput(_ContentInput):
    draft_type: DraftType

    @model_validator(mode="after")
    def matching_type(self) -> DraftCreateInput:
        if self.draft.draft_type not in (None, self.draft_type):
            raise ValueError("草稿类型不一致")
        _validate_draft_fields(self.draft_type, self.draft)
        return self


class DraftIdInput(_Input):
    draft_id: str = Field(min_length=1, max_length=128)


class DraftListInput(_Input):
    limit: int = Field(default=50, ge=1, le=100)


class DraftSaveInput(_ContentInput):
    draft_id: str = Field(min_length=1, max_length=128)
    expected_version: int = Field(ge=1)

    @model_validator(mode="after")
    def no_type_change(self) -> DraftSaveInput:
        if self.draft.draft_type is not None:
            raise ValueError("不能修改草稿类型")
        return self


def _validate_draft_fields(draft_type: str, draft: DraftContent) -> None:
    model = _DRAFT_FIELD_MODELS[draft_type]
    for key in ("fields", "mysql_fields"):
        values = getattr(draft, key)
        if values is not None:
            model.model_validate(values.model_dump(exclude_unset=True, exclude_none=True))


def _dump_draft(draft: DraftContent) -> dict[str, Any]:
    return draft.model_dump(exclude_unset=True, exclude_none=True)


class DraftPublishInput(DraftIdInput):
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=256)


class DraftUnpublishInput(DraftIdInput):
    reason_code: str = Field(min_length=1, max_length=64)
    reason_text: str | None = Field(default=None, max_length=500)


def register_draft_tools(
    mcp: Any,
    runtime: Any,
    annotations: Any,
    request_from_context: Callable[[Any], Any],
    resolve_context: Callable[[Any, Any], ToolContext],
    failure: Callable[[ToolContext, str], dict[str, Any]],
) -> None:
    """Register only owner-scoped draft operations on kechuang."""

    write_annotations = annotations.model_copy(
        update={
            "readOnlyHint": False,
            "idempotentHint": False,
        }
    )
    destructive_annotations = write_annotations.model_copy(update={"destructiveHint": True})

    def identity_for(context: ToolContext) -> Callable[[str], tuple[str, str, None]]:
        def identity(user_id: str) -> tuple[str, str, None]:
            if user_id != context.user_id:
                raise PermissionError("草稿不属于当前用户")
            return context.user_id, context.user_role, None

        return identity

    service = DraftApplicationService(runtime)

    async def invoke(
        ctx: Context, stage: str, action: Callable[[ToolContext], Any]
    ) -> dict[str, Any]:
        context = resolve_context(runtime, request_from_context(ctx))
        try:
            data = await action(context)
            return {"status": "SUCCESS", "data": data, "error": None, "trace_id": context.trace_id}
        except HTTPException as exc:
            detail = exc.detail
            message = (
                detail.get("message", str(detail)) if isinstance(detail, dict) else str(detail)
            )
            result = failure(context, message)
            result["data"] = {"stage": stage, "status_code": exc.status_code}
            return result
        except (ValueError, PermissionError) as exc:
            result = failure(context, str(exc))
            result["data"] = {"stage": stage}
            return result
        except Exception:
            result = failure(context, "草稿操作失败")
            result["data"] = {"stage": stage}
            return result

    def selected_demand_category(context: ToolContext) -> str:
        if not context.thread_id:
            return ""
        session_store = getattr(runtime.require_conversation(), "session_store", None)
        session = session_store.get(context.thread_id) if session_store is not None else None
        if not isinstance(session, dict) or str(session.get("user_id") or "") != context.user_id:
            return ""
        flow = (session.get("state") or {}).get("chat_flow") or {}
        request_context = flow.get("request_context") or {}
        draft_context = request_context.get("draft_context") or {}
        if draft_context.get("draft_type") != "demand":
            return ""
        return str(draft_context.get("demand_type") or "").strip()

    def owned_record(context: ToolContext, draft_id: str) -> dict[str, Any]:
        record = runtime.require_draft_store().get(draft_id, user_id=context.user_id)
        if record is None or record.get("draft_type") not in _DRAFT_FIELD_MODELS:
            raise HTTPException(status_code=404, detail="草稿不存在")
        if record["draft_type"] == "customer_visit" and context.user_role != "internal":
            raise HTTPException(status_code=403, detail="当前暂不支持处理客户拜访记录")
        return record

    def public_record(context: ToolContext, draft_id: str) -> dict[str, Any]:
        record = owned_record(context, draft_id)
        if record["draft_type"] == "customer_visit":
            raise HTTPException(
                status_code=409, detail="拜访记录不对外发布，请使用拜访记录保存工具"
            )
        return record

    @mcp.tool(
        name="kc_draft_create",
        description=(
            "按字段合同创建需求、方案、成果或内部客户拜访草稿，返回可编辑卡片。仅在用户目标是"
            "创建草稿或已确认创建时调用；拜访前准备属于资料检索与总结，不是拜访记录草稿。"
        ),
        annotations=write_annotations,
        structured_output=True,
    )
    async def kc_draft_create(params: DraftCreateInput, ctx: Context) -> dict[str, Any]:
        async def action(context: ToolContext) -> Any:
            if params.draft_type == "customer_visit" and context.user_role != "internal":
                raise HTTPException(status_code=403, detail="当前暂不支持处理客户拜访记录")
            draft = _dump_draft(params.draft)
            if params.draft_type == "demand":
                category = selected_demand_category(context)
                fields = dict(draft.get("fields") or {})
                mysql_fields = draft.get("mysql_fields") or {}
                existing_category = fields.get("category") or mysql_fields.get("category")
                if category and not str(existing_category or "").strip():
                    fields["category"] = category
                    draft["fields"] = fields
            return await service.create({**draft, "draft_type": params.draft_type}, context=context)

        return await invoke(ctx, "create", action)

    @mcp.tool(
        name="kc_draft_get",
        description="Read one draft owned by the authenticated user.",
        annotations=annotations,
        structured_output=True,
    )
    async def kc_draft_get(params: DraftIdInput, ctx: Context) -> dict[str, Any]:
        async def action(context: ToolContext) -> Any:
            owned_record(context, params.draft_id)
            return await service.get(params.draft_id, context=context)

        return await invoke(ctx, "get", action)

    @mcp.tool(
        name="kc_draft_list",
        description="List the authenticated user's drafts.",
        annotations=annotations,
        structured_output=True,
    )
    async def kc_draft_list(params: DraftListInput, ctx: Context) -> dict[str, Any]:
        async def action(context: ToolContext) -> Any:
            items = service.list(context=context, limit=params.limit)
            return {
                "items": [
                    item
                    for item in items
                    if item.get("draft_type") in _DRAFT_FIELD_MODELS
                    and (
                        item.get("draft_type") != "customer_visit"
                        or context.user_role == "internal"
                    )
                ]
            }

        return await invoke(ctx, "list", action)

    @mcp.tool(
        name="kc_draft_save",
        description="Save a draft edit with its expected version.",
        annotations=write_annotations,
        structured_output=True,
    )
    async def kc_draft_save(params: DraftSaveInput, ctx: Context) -> dict[str, Any]:
        async def action(context: ToolContext) -> Any:
            record = owned_record(context, params.draft_id)
            draft_type = str(record["draft_type"])
            _validate_draft_fields(draft_type, params.draft)
            return await service.save(params.draft_id, _dump_draft(params.draft), context=context,
                                      expected_version=params.expected_version)

        return await invoke(ctx, "save", action)

    @mcp.tool(
        name="kc_draft_publish",
        description="Publish an owned draft through the existing MySQL and Milvus workflow.",
        annotations=write_annotations,
        structured_output=True,
    )
    async def kc_draft_publish(params: DraftPublishInput, ctx: Context) -> dict[str, Any]:
        async def action(context: ToolContext) -> Any:
            public_record(context, params.draft_id)
            return await DraftPublishingApplicationService(runtime, identity_for(context)).publish(
                params.draft_id,
                DraftPublishRequest(
                    user_id=context.user_id,
                    thread_id=context.thread_id,
                    idempotency_key=params.idempotency_key,
                ),
            )

        return await invoke(ctx, "publish", action)

    @mcp.tool(
        name="kc_draft_unpublish",
        description="Withdraw an owned published draft and remove its retrieval index.",
        annotations=destructive_annotations,
        structured_output=True,
    )
    async def kc_draft_unpublish(params: DraftUnpublishInput, ctx: Context) -> dict[str, Any]:
        async def action(context: ToolContext) -> Any:
            record = public_record(context, params.draft_id)
            if record.get("status") == "WITHDRAWN":
                return {
                    "draft": record,
                    "index_result": {
                        "status": "UNPUBLISHED",
                        "document_id": params.draft_id,
                        "updated_count": 0,
                    },
                }
            return await service.unpublish(
                params.draft_id,
                LifecycleDecisionRequest(
                    user_id=context.user_id, reason_code=params.reason_code, reason_text=params.reason_text
                ),
                context=context,
            )

        return await invoke(ctx, "unpublish", action)

    @mcp.tool(
        name="kc_draft_delete",
        description="Logically delete an owned draft after withdrawal when published.",
        annotations=destructive_annotations,
        structured_output=True,
    )
    async def kc_draft_delete(params: DraftIdInput, ctx: Context) -> dict[str, Any]:
        async def action(context: ToolContext) -> Any:
            record = public_record(context, params.draft_id)
            if record.get("status") == "DELETED":
                return {"draft": record, "status": "DELETED"}
            return await service.delete(params.draft_id, context=context)

        return await invoke(ctx, "delete", action)
