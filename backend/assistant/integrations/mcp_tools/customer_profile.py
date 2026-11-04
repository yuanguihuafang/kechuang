"""Authenticated profile search and customer visit operations."""

from __future__ import annotations

import asyncio
import logging
import threading
import weakref
from collections.abc import Callable
from typing import Any, Literal

from fastapi.encoders import jsonable_encoder
from mcp.server.fastmcp import Context
from pydantic import BaseModel, ConfigDict, Field

from core.contracts.business_records import ConfirmedBusinessRecordRequest
from core.contracts.conversation import material_search_confirmation_required
from assistant.application.retrieval_relevance import judge_and_fallback
from core.contracts.draft_contracts import (
    draft_field_length_errors,
    draft_fields,
    draft_required_fields,
)
from services.application.business_records import BusinessRecordApplicationService

logger = logging.getLogger(__name__)
_VISIT_LOCKS: weakref.WeakValueDictionary[
    tuple[asyncio.AbstractEventLoop, str, str], asyncio.Lock
] = weakref.WeakValueDictionary()
_VISIT_LOCKS_GUARD = threading.Lock()


def _visit_lock(user_id: str, thread_id: str) -> asyncio.Lock:
    # ponytail: 每个事件循环内采用 single-flight；跨 Worker 保证恰好执行一次需要持久化认领记录。
    key = (asyncio.get_running_loop(), user_id, thread_id)
    with _VISIT_LOCKS_GUARD:
        lock = _VISIT_LOCKS.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _VISIT_LOCKS[key] = lock
        return lock


def _audit_tool_call(context: Any, tool: str, status: str, **details: Any) -> None:
    extras = " ".join(f"{key}={value}" for key, value in details.items())
    log = logger.info if status in {"SUCCESS", "WEB_FALLBACK", "NO_EVIDENCE"} else logger.warning
    log(
        "MCP tool call tool=%s status=%s user_id=%s user_role=%s thread_id=%s trace_id=%s%s",
        tool,
        status,
        context.user_id,
        context.user_role,
        context.thread_id or "-",
        context.trace_id,
        f" {extras}" if extras else "",
    )


class ProfileSearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    query: str = Field(
        min_length=1,
        max_length=200,
        description="企业/专家检索词；允许公开回退时只填公开主体或主题，不包含私有资料。",
    )
    user_goal: str | None = Field(
        default=None,
        max_length=20000,
        description="用户本轮完整原话，保留目的、场景和来源限制；不要只传检索关键词。",
    )
    allow_web_fallback: bool = Field(
        default=True,
        description="只有用户未限定内部来源且 query 仅含用户原话中的公开主体/主题时才为 true；来源限定、query 来自附件/草稿/内部资料或含私密信息时设为 false。",
    )
    knowledge_type: Literal["enterprise", "expert"]


class CustomerVisitsInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    limit: int = Field(default=50, ge=1, le=200)
    query: str | None = Field(default=None, min_length=1, max_length=200)


class CustomerVisitSaveInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    draft_id: str = Field(min_length=1, max_length=128, description="当前会话已有拜访草稿 ID。")
    expected_version: int = Field(ge=1, description="用户确认保存的草稿版本。")


def _customer_record_matches(record: dict[str, Any], query: str) -> bool:
    structured_data = record.get("structured_data")
    searchable_values = list(structured_data.values()) if isinstance(structured_data, dict) else []
    searchable_values.append(record.get("raw_text"))
    text = " ".join(
        " ".join(str(value).casefold().split())
        for value in searchable_values
        if isinstance(value, str) and value.strip()
    )
    return " ".join(query.casefold().split()) in text


def register_customer_profile_tools(
    mcp: Any,
    runtime: Any,
    annotations: Any,
    request_from_context: Callable[[Any], Any],
    resolve_context: Callable[[Any, Any], Any],
    failure: Callable[[Any, str], dict[str, Any]],
) -> None:
    write_annotations = annotations.model_copy(
        update={
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": True,
        }
    )

    @mcp.tool(
        name="kc_profile",
        description=(
            "Search enterprise or expert profiles visible to the authenticated user when the goal is "
            "to obtain profile information. Do not invoke it from a company name alone; consider the "
            "user's full purpose, such as pre-visit preparation, before selecting tools. Pass the "
            "complete original request in user_goal. Set allow_web_fallback=false only when the user "
            "restricts sources to internal materials, or query includes private attachments/drafts instead "
            "of only public terms from the user's original request."
        ),
        annotations=annotations,
        structured_output=True,
    )
    async def kc_profile(params: ProfileSearchInput, ctx: Context) -> dict[str, Any]:
        context = resolve_context(runtime, request_from_context(ctx))
        try:
            session_store = runtime.require_conversation().session_store
            session = session_store.get(context.thread_id) if session_store is not None else None
            if material_search_confirmation_required((session or {}).get("state") or {}):
                blocked = failure(context, "请先确认根据上传资料识别出的检索意图")
                blocked["status"] = "INTENT_CONFIRM_REQUIRED"
                return blocked
            if context.user_role not in {"internal", "external"}:
                _audit_tool_call(context, "kc_profile", "FAILED", error_type="PermissionError")
                return failure(context, "当前身份无权检索企业或专家")
            adapter = getattr(runtime, "tool_adapter", None)
            if adapter is None:
                _audit_tool_call(
                    context, "kc_profile", "FAILED", error_type="ServiceUnavailable"
                )
                return failure(context, "知识检索服务尚未就绪")
            arguments = {
                "query": params.query,
                "knowledge_type": params.knowledge_type,
                "top_k": 3,
                "use_rerank": True,
                "identity": context.identity(),
                "thread_id": context.thread_id,
                "trace_id": context.trace_id,
            }
            if context.user_role == "external":
                arguments["scope_id"] = "public"
            result = await adapter.search_knowledge(arguments, context=context)
            result = await judge_and_fallback(
                result,
                user_goal=params.user_goal or params.query,
                retrieval_query=params.query,
                adapter=adapter,
                context=context,
                allow_web_fallback=params.allow_web_fallback,
            )
            result["_web_fallback_allowed"] = params.allow_web_fallback
            _audit_tool_call(
                context,
                "kc_profile",
                str(result.get("status") or "FAILED"),
                knowledge_type=params.knowledge_type,
            )
            return result
        except Exception as exc:
            _audit_tool_call(context, "kc_profile", "FAILED", error_type=type(exc).__name__)
            return failure(context, "企业/专家检索失败")

    @mcp.tool(
        name="kc_customer",
        description="List only the authenticated internal user's saved customer visits.",
        annotations=annotations,
        structured_output=True,
    )
    async def kc_customer(params: CustomerVisitsInput, ctx: Context) -> dict[str, Any]:
        context = resolve_context(runtime, request_from_context(ctx))
        # Sink 对非数字用户 ID 的回退查询不限定范围，因此在此处拒绝该输入。
        if context.user_role != "internal" or not context.user_id.isdecimal():
            _audit_tool_call(context, "kc_customer", "FAILED", error_type="PermissionError")
            return failure(context, "当前暂不支持处理客户拜访记录")
        try:
            records = await asyncio.to_thread(
                runtime.require_business_record_sink().list,
                user_id=context.user_id,
                record_type="customer_visit",
            )
            owned = [
                item
                for item in records
                if isinstance(item, dict)
                and isinstance(item.get("structured_data"), dict)
                and str(item["structured_data"].get("user_card_id")) == context.user_id
                and str(item["structured_data"].get("inquiry_status")) == "1"
                and str(item["structured_data"].get("is_deleted")) == "0"
                and (params.query is None or _customer_record_matches(item, params.query))
            ][: params.limit]
            _audit_tool_call(context, "kc_customer", "SUCCESS", result_count=len(owned))
            return {
                "status": "SUCCESS",
                "data": {"records": jsonable_encoder(owned)},
                "error": None,
                "trace_id": context.trace_id,
            }
        except Exception as exc:
            _audit_tool_call(context, "kc_customer", "FAILED", error_type=type(exc).__name__)
            return failure(context, "拜访记录读取失败")

    @mcp.tool(
        name="kc_customer_save_visit",
        description="用户明确要求保存时，按已有拜访草稿 ID 和版本写入私有业务记录及索引。不要重新生成字段；重复确认同一版本不重复写入。",
        annotations=write_annotations,
        structured_output=True,
    )
    async def kc_customer_save_visit(
        params: CustomerVisitSaveInput, ctx: Context
    ) -> dict[str, Any]:
        context = resolve_context(runtime, request_from_context(ctx))
        if (
            context.user_role != "internal"
            or not (context.user_id.isascii() and context.user_id.isdecimal())
            or not context.thread_id
        ):
            _audit_tool_call(
                context, "kc_customer_save_visit", "FAILED", error_type="PermissionError"
            )
            return failure(context, "当前暂不支持处理客户拜访记录")
        async with _visit_lock(context.user_id, context.thread_id):
            try:
                store = runtime.require_draft_store()
                existing = store.get(params.draft_id, user_id=context.user_id)
                if (
                    not existing
                    or existing.get("draft_type") != "customer_visit"
                    or existing.get("thread_id") != context.thread_id
                    or existing.get("status") == "DELETED"
                ):
                    return failure(context, "当前会话的拜访草稿不存在")
                draft = dict(existing["draft"])
                version = existing["version"]
                if (
                    draft.get("_visit_confirmed_version") == params.expected_version
                    and version == params.expected_version + 1
                    and draft.get("mysql_record_id")
                ):
                    return {
                        "status": "SUCCESS",
                        "data": {
                            "draft_id": params.draft_id,
                            "record_id": params.draft_id,
                            "business_record": {"business_record_id": draft["mysql_record_id"]},
                            "visit_record": jsonable_encoder(existing),
                        },
                        "error": None,
                        "trace_id": context.trace_id,
                    }
                if version != params.expected_version:
                    return failure(context, "草稿版本已变更，请读取最新草稿后保存")
                fields = draft.get("fields") or {}
                if not fields or set(fields) - set(draft_fields("customer_visit")):
                    return failure(context, "拜访草稿必须按字段合同填写有效内容")
                if any(not fields.get(key) for key in draft_required_fields("customer_visit")):
                    return failure(context, "拜访草稿缺少合同规定的必填字段")
                if draft_field_length_errors(draft):
                    return failure(context, "拜访草稿字段过长，请归纳后保存")
                payload = ConfirmedBusinessRecordRequest(
                    user_id=context.user_id,
                    confirmed_by=context.user_id,
                    thread_id=context.thread_id,
                    record_type="customer_visit",
                    confirmed=True,
                    structured_data={**draft, "_visit_confirmed_version": params.expected_version},
                    idempotency_key=f"visit:{params.draft_id}:{params.expected_version}",
                    draft_id=params.draft_id,
                    expected_draft_version=params.expected_version,
                )
                data = await BusinessRecordApplicationService(runtime).confirm(payload)
                _audit_tool_call(
                    context,
                    "kc_customer_save_visit",
                    "SUCCESS",
                    record_id=data.get("business_record", {}).get("business_record_id"),
                )
                return {
                    "status": "SUCCESS",
                    "data": jsonable_encoder(data),
                    "error": None,
                    "trace_id": context.trace_id,
                }
            except Exception as exc:
                _audit_tool_call(
                    context,
                    "kc_customer_save_visit",
                    "FAILED",
                    error_type=type(exc).__name__,
                )
                return failure(context, "拜访记录保存失败；请读取最新草稿状态后重试")
