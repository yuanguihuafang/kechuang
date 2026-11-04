"""Optional Streamable HTTP MCP boundary for business capability tools."""

import logging
import os
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from assistant.application.retrieval_relevance import judge_and_fallback
from core.contracts.conversation import material_search_confirmation_required
from core.contracts.flows import get_intent_flow
from core.contracts.retrieval import is_match_result_excluded
from core.contracts.tools import ToolContext

KnowledgeType = Literal["demand", "solution", "achievement", "expert", "enterprise"]
MatchDirection = Literal["demand_to_supply", "supply_to_demand"]
MATCH_RESULT_LIMIT = 3
_MATCH_DEFAULT_KNOWLEDGE_TYPES: dict[MatchDirection, tuple[KnowledgeType, ...]] = {
    "demand_to_supply": ("solution", "achievement", "expert", "enterprise"),
    "supply_to_demand": ("demand",),
}
_KNOWLEDGE_TYPES = {"demand", "solution", "achievement", "expert", "enterprise"}
logger = logging.getLogger(__name__)


class RetrievalToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    query: str = Field(
        min_length=1,
        max_length=20000,
        description="Concise retrieval terms; if public fallback is allowed, keep only public entity/topic terms, never private material.",
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
    scope_id: str | None = Field(default=None, max_length=128)
    knowledge_type: KnowledgeType | None = None
    knowledge_types: list[KnowledgeType] = Field(
        default_factory=list,
        max_length=5,
        description=(
            "用户明确或语义上限定某类资料库时，由模型填写对应库；明确只查一种时只填该库。"
            "未限定时留空，沿用当前业务流程原有检索范围；供需匹配按方向使用相关库，可并行检索。"
            "不要把未限定解释为全库，也不要因无命中扩大范围。类型：demand=用户需求，"
            "solution=技术方案，achievement=科研成果，expert=专家，enterprise=企业。"
        ),
    )
    document_id: str | None = Field(default=None, max_length=128)
    expand_query: bool = False
    next_batch: bool = Field(
        default=False, description="当前用户明确要求查看同一检索的其他结果时设为 true"
    )


class MatchToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    query: str = Field(
        min_length=1,
        max_length=20000,
        description="Concise retrieval terms; if public fallback is allowed, keep only public entity/topic terms, never private material.",
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
    direction: MatchDirection
    scope_id: str | None = Field(default=None, max_length=128)
    knowledge_types: list[KnowledgeType] = Field(
        default_factory=list,
        max_length=5,
        description=(
            "用户明确或语义上限定某类资料库时，由模型填写对应库；明确只查一种时只填该库。"
            "未限定时留空，沿用供需方向的原有检索范围：需求找供给并行搜方案、成果、专家、企业；"
            "供给找需求只搜需求库。不要因无命中扩大范围。类型：demand=用户需求，"
            "solution=技术方案，achievement=科研成果，expert=专家，enterprise=企业。"
        ),
    )
    expand_query: bool = False
    next_batch: bool = Field(
        default=False, description="当前用户明确要求查看同一匹配的其他结果时设为 true"
    )


def _header(request: Any, name: str) -> str:
    headers = getattr(request, "headers", {}) or {}
    return str(headers.get(name.lower(), "") or "").strip()


def resolve_mcp_tool_context(runtime: Any, request: Any) -> ToolContext:
    """Resolve the business identity from the owning Kechuang chat session."""

    state = getattr(request, "state", None)
    thread_id = _header(request, "X-KC-Thread-ID")
    if not thread_id:
        raise PermissionError("MCP 科创会话上下文缺失")

    session_store = getattr(runtime.require_conversation(), "session_store", None)
    session = session_store.get(thread_id) if session_store is not None else None
    user_id = str(session.get("user_id") or "").strip() if isinstance(session, Mapping) else ""
    if not user_id:
        raise PermissionError("MCP 科创会话不存在或未关联用户")

    try:
        from services.application.identity import TestIdentityResolver

        normalized_id, role, profile = TestIdentityResolver(
            lambda: getattr(runtime, "user_identity_reader", None)
        ).resolve(user_id)
    except Exception as exc:
        raise PermissionError("MCP user identity unavailable") from exc

    trace_id = str(
        getattr(state, "trace_id", "") or _header(request, "X-Trace-ID") or f"mcp-{uuid4().hex}"
    ).strip()
    return ToolContext(
        user_id=normalized_id,
        user_role=role,
        company=str(getattr(profile, "company", "") or "").strip() or None,
        thread_id=thread_id,
        trace_id=trace_id,
    )


def build_retrieval_arguments(
    params: RetrievalToolInput,
    context: ToolContext,
    *,
    excluded_ids: set[str] | None = None,
) -> dict[str, Any]:
    arguments = params.model_dump(exclude_none=True)
    arguments.pop("next_batch", None)
    arguments.pop("user_goal", None)
    arguments.pop("allow_web_fallback", None)
    arguments.update(
        top_k=MATCH_RESULT_LIMIT,
        exclude_result_ids=sorted(excluded_ids or set()),
        use_rerank=True,
        identity=context.identity(),
        thread_id=context.thread_id,
        trace_id=context.trace_id,
    )
    if context.user_role == "external":
        arguments["scope_id"] = "public"
    return arguments


def build_match_arguments(
    params: MatchToolInput,
    context: ToolContext,
    *,
    excluded_ids: set[str] | None = None,
) -> dict[str, Any]:
    excluded = sorted(excluded_ids or set())
    arguments: dict[str, Any] = {
        "query": params.query,
        "knowledge_types": params.knowledge_types,
        "top_k": MATCH_RESULT_LIMIT,
        "exclude_result_ids": excluded,
        "use_rerank": True,
        "expand_query": params.expand_query,
        "identity": context.identity(),
        "thread_id": context.thread_id,
        "trace_id": context.trace_id,
    }
    if params.scope_id is not None:
        arguments["scope_id"] = params.scope_id
    if context.user_role == "external":
        arguments["scope_id"] = "public"
    return arguments


def apply_pending_match_batch(
    params: MatchToolInput,
    pending: Mapping[str, Any] | None,
) -> tuple[MatchToolInput, set[str]]:
    """Apply trusted per-thread batch state instead of relying on the LLM."""

    if pending is None:
        if not params.knowledge_types:
            params = params.model_copy(
                update={
                    "knowledge_types": list(_MATCH_DEFAULT_KNOWLEDGE_TYPES[params.direction])
                }
            )
        return params, set()
    query = str(pending.get("query") or "").strip()
    direction = pending.get("direction")
    if not query or direction not in {"demand_to_supply", "supply_to_demand"}:
        raise ValueError("换批匹配状态缺少有效查询条件")
    knowledge_types = [
        item
        for item in pending.get("knowledge_types", [])
        if item in {"demand", "solution", "achievement", "expert", "enterprise"}
    ]
    if not knowledge_types:
        raise ValueError("换批匹配状态缺少知识库范围")
    excluded_ids = {
        str(item).strip() for item in pending.get("shown_result_ids", []) if str(item).strip()
    }
    if not excluded_ids:
        raise ValueError("换批匹配状态缺少已展示结果")
    return params.model_copy(
        update={"query": query, "direction": direction, "knowledge_types": knowledge_types}
    ), excluded_ids


def apply_pending_retrieval_batch(
    params: RetrievalToolInput,
    pending: Mapping[str, Any] | None,
    *,
    default_knowledge_types: list[KnowledgeType] | None = None,
) -> tuple[RetrievalToolInput, set[str]]:
    """Apply a trusted per-thread next-batch query and collection."""

    if pending is None:
        if not params.knowledge_types:
            knowledge_type = params.knowledge_type
            selected_types = (
                [knowledge_type]
                if knowledge_type in _KNOWLEDGE_TYPES
                else list(default_knowledge_types or [])
            )
            if selected_types:
                params = params.model_copy(update={"knowledge_types": selected_types})
        return params, set()
    query = str(pending.get("query") or "").strip()
    business_intent = str(pending.get("business_intent") or "").strip().lower()
    direction = str(pending.get("direction") or "").strip().lower()
    if business_intent == "supply_demand_match" and direction not in {
        "demand_to_supply",
        "supply_to_demand",
    }:
        raise ValueError("换批匹配状态缺少有效匹配方向")
    knowledge_types = [
        item
        for item in pending.get("knowledge_types", [])
        if item in _KNOWLEDGE_TYPES
    ]
    knowledge_type = pending.get("knowledge_type")
    if not knowledge_types and knowledge_type in _KNOWLEDGE_TYPES:
        knowledge_types = [knowledge_type]
    if not knowledge_types:
        raise ValueError("换批检索状态缺少知识库范围")
    selected_type = knowledge_types[0] if len(knowledge_types) == 1 else None
    if business_intent == "demand_discovery":
        update = {
            "query": query,
            "knowledge_type": selected_type,
            "knowledge_types": knowledge_types,
        }
    elif business_intent in {"supply_demand_match", "knowledge_search"}:
        update = {
            "query": query,
            "knowledge_type": selected_type,
            "knowledge_types": knowledge_types,
        }
    else:
        raise ValueError("换批检索状态缺少有效业务方向")
    excluded_ids = {
        str(item).strip() for item in pending.get("shown_result_ids", []) if str(item).strip()
    }
    if not query or not excluded_ids:
        raise ValueError("换批检索状态缺少查询条件或已展示结果")
    return params.model_copy(update=update), excluded_ids


def _default_retrieval_knowledge_types(
    params: RetrievalToolInput,
    state: Mapping[str, Any],
) -> list[KnowledgeType]:
    """Preserve the established intent scope when the model did not narrow it."""

    if params.knowledge_types or params.knowledge_type in _KNOWLEDGE_TYPES:
        return []
    request_context = state.get("request_context")
    request_context = request_context if isinstance(request_context, Mapping) else {}
    requested_type = request_context.get("knowledge_type")
    if requested_type in _KNOWLEDGE_TYPES:
        return [requested_type]
    intent = str(
        request_context.get("intent_hint") or state.get("active_intent") or ""
    ).strip().lower()
    flow = get_intent_flow(intent)
    if flow is None:
        return []
    if intent == "supply_demand_match":
        draft_context = request_context.get("draft_context")
        draft_context = draft_context if isinstance(draft_context, Mapping) else {}
        direction = str(
            request_context.get("match_direction")
            or draft_context.get("match_direction")
            or state.get("match_direction")
            or ""
        ).strip().lower()
        if direction in _MATCH_DEFAULT_KNOWLEDGE_TYPES:
            return list(_MATCH_DEFAULT_KNOWLEDGE_TYPES[direction])
    return [item for item in flow.knowledge_types if item in _KNOWLEDGE_TYPES]


def resolve_next_match_batch(
    requested: bool,
    state: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Resolve a one-call next-batch request from completed chat-flow history."""

    requested = requested or state.get("request_flow_action") == "next_match_batch"
    if not requested:
        return None
    flow = state.get("chat_flow")
    batch = flow.get("match_batch") if isinstance(flow, Mapping) else None
    if not isinstance(batch, Mapping):
        raise ValueError("当前会话没有可继续的检索结果")
    query = str(batch.get("current_query") or batch.get("original_query") or "").strip()
    shown_result_ids = list(
        dict.fromkeys(
            str(item).strip() for item in batch.get("shown_result_ids", []) if str(item).strip()
        )
    )
    if not query or not shown_result_ids:
        raise ValueError("当前会话没有可排除的已展示结果")
    return {
        "query": query,
        "user_goal": str(batch.get("user_goal") or query)[:4000],
        "web_fallback_allowed": batch.get("web_fallback_allowed") is True,
        "business_intent": str(batch.get("business_intent") or "").strip().lower(),
        "direction": str(batch.get("direction") or "").strip().lower(),
        "knowledge_type": batch.get("knowledge_type"),
        "knowledge_types": list(batch.get("knowledge_types") or []),
        "shown_result_ids": shown_result_ids,
    }


def filter_match_result(
    result: Mapping[str, Any],
    excluded_ids: set[str],
    *,
    top_k: int,
) -> dict[str, Any]:
    from services.application.chat_response import select_match_results

    top_k = max(0, min(int(top_k), MATCH_RESULT_LIMIT))
    filtered = dict(result)
    data = dict(filtered.get("data")) if isinstance(filtered.get("data"), Mapping) else None
    for body in (filtered, data):
        if body is None:
            continue
        for key in ("evidences", "records"):
            values = body.get(key)
            if not isinstance(values, list):
                continue
            body[key] = select_match_results([
                item
                for item in values
                if isinstance(item, dict) and not is_match_result_excluded(item, excluded_ids)
            ], limit=top_k)
    if data is not None:
        filtered["data"] = data
    return filtered


def _request_from_context(context: Any) -> Any:
    request_context = getattr(context, "request_context", None)
    request = getattr(request_context, "request", None)
    if request is None:
        raise PermissionError("MCP request context unavailable")
    headers = {
        str(key).lower(): value
        for key, value in (getattr(request, "headers", {}) or {}).items()
    }
    meta_headers = getattr(getattr(request_context, "meta", None), "headers", None)
    if isinstance(meta_headers, Mapping):
        headers.update({str(key).lower(): value for key, value in meta_headers.items()})
    return SimpleNamespace(headers=headers, state=getattr(request, "state", None))


def _failure(
    context: ToolContext,
    message: str,
    *,
    next_batch: bool = False,
) -> dict[str, Any]:
    result = {"status": "FAILED", "data": {}, "error": message, "trace_id": context.trace_id}
    if next_batch:
        result["_next_match_batch"] = True
    return result


def _no_more_match_batch(context: ToolContext) -> dict[str, Any]:
    return {
        "status": "NO_EVIDENCE",
        "answer": "当前知识库没有更多匹配结果。若还想获取更多信息，请明确告诉我“联网搜索”，我再联网查询。",
        "data": {},
        "evidences": [],
        "match_results": [],
        "ui_blocks": [],
        "actions": [],
        "trace_id": context.trace_id,
        "_next_match_batch": True,
    }


def _has_match_batch_items(result: Mapping[str, Any]) -> bool:
    bodies = [result]
    if isinstance(result.get("data"), Mapping):
        bodies.append(result["data"])
    return any(
        isinstance(body.get(key), list) and body[key]
        for body in bodies
        for key in ("evidences", "records", "match_results")
    )


def _finish_match_batch(result: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    if (
        result.get("status") in {"SUCCESS", "NO_EVIDENCE"}
        and not result.get("error")
        and not _has_match_batch_items(result)
    ):
        return _no_more_match_batch(context)
    result["_next_match_batch"] = True
    return result


def _mcp_allowed_hosts() -> list[str]:
    configured = os.getenv("KC_MCP_ALLOWED_HOSTS", "")
    if configured.strip():
        return [item.strip() for item in configured.split(",") if item.strip()]
    return [
        "business-backend:*",
        "kechuang-business-backend:*",
        "localhost:*",
        "127.0.0.1:*",
        "host.docker.internal:*",
    ]


def build_mcp_app(runtime: Any) -> Any:
    """Build the MCP ASGI app only when the optional SDK is installed."""

    try:
        from mcp.server.fastmcp import Context, FastMCP
        from mcp.server.transport_security import TransportSecuritySettings
        from mcp.types import ToolAnnotations
    except ImportError as exc:  # pragma: no cover - 在部署环境中验证
        raise RuntimeError(
            "MCP dependency is not installed; install the backend mcp extra"
        ) from exc

    mcp = FastMCP(
        "kechuang",
        mount_path="/",
        streamable_http_path="/",
        stateless_http=True,
        transport_security=TransportSecuritySettings(
            allowed_hosts=_mcp_allowed_hosts(),
        ),
    )
    annotations = ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )

    @mcp.tool(
        name="kc_retrieval",
        description=(
            "Search the authenticated user's permitted knowledge collections. When the user's wording "
            "explicitly or clearly limits the relevant data kind, set knowledge_types to exactly the "
            "relevant collection(s); an explicit single-library request must use only that library. "
            "Otherwise leave it empty so the existing business-flow scope applies. For supply-demand "
            "matching, keep the established direction scope and parallelize its relevant collections. "
            "Never expand to every collection just because scope is omitted or results are empty. "
            "Choose this tool from the user's full goal, not solely from an entity mention or generic "
            "'look up' wording. Pass the full original request in user_goal, including its purpose and "
            "source restrictions; query may be a concise retrieval query. Set allow_web_fallback to "
            "false when the user restricts sources to internal materials, or query includes content from "
            "private attachments/drafts instead of only public terms from the user's original request."
        ),
        annotations=annotations,
        structured_output=True,
    )
    async def kc_retrieval(params: RetrievalToolInput, ctx: Context) -> dict[str, Any]:
        request = _request_from_context(ctx)
        tool_context = resolve_mcp_tool_context(runtime, request)
        adapter = getattr(runtime, "tool_adapter", None)
        if adapter is None:
            return _failure(tool_context, "知识检索服务尚未就绪", next_batch=params.next_batch)
        try:
            supervisor = runtime.require_conversation()
            session_store = supervisor.session_store
            session = (
                session_store.get(tool_context.thread_id)
                if session_store is not None and tool_context.thread_id
                else None
            )
            if session and str(session.get("user_id") or "") != tool_context.user_id:
                raise PermissionError("当前用户无权读取该会话")
            state = (session or {}).get("state") or {}
            if material_search_confirmation_required(state):
                return {
                    **_failure(tool_context, "请先确认根据上传资料识别出的检索意图"),
                    "status": "INTENT_CONFIRM_REQUIRED",
                }
            try:
                pending = resolve_next_match_batch(params.next_batch, state)
            except ValueError:
                if params.next_batch or state.get("request_flow_action") == "next_match_batch":
                    return _no_more_match_batch(tool_context)
                raise
            effective_params, excluded_ids = apply_pending_retrieval_batch(
                params,
                pending,
                default_knowledge_types=_default_retrieval_knowledge_types(params, state),
            )
            result = await adapter.search_knowledge(
                build_retrieval_arguments(
                    effective_params,
                    tool_context,
                    excluded_ids=excluded_ids,
                ),
                context=tool_context,
            )
            filtered = filter_match_result(result, excluded_ids, top_k=MATCH_RESULT_LIMIT)
            filtered = await judge_and_fallback(
                filtered,
                user_goal=(pending or {}).get("user_goal") or params.user_goal or params.query,
                retrieval_query=effective_params.query,
                adapter=adapter,
                context=tool_context,
                allow_web_fallback=(
                    bool(pending.get("web_fallback_allowed"))
                    if pending is not None
                    else params.allow_web_fallback
                ),
            )
            filtered["_web_fallback_allowed"] = (
                bool(pending.get("web_fallback_allowed"))
                if pending is not None
                else params.allow_web_fallback
            )
            filtered["query"] = effective_params.query
            filtered["knowledge_types"] = effective_params.knowledge_types
            if pending is not None:
                return _finish_match_batch(filtered, tool_context)
            return filtered
        except Exception:  # noqa: BLE001 - 保持工具响应结构稳定
            logger.exception(
                "MCP knowledge retrieval failed thread_id=%s trace_id=%s",
                tool_context.thread_id or "-",
                tool_context.trace_id,
            )
            return _failure(tool_context, "知识检索执行失败", next_batch=params.next_batch)

    @mcp.tool(
        name="kc_match",
        description=(
            "Find supply-demand matches in the authenticated user's permitted data. When the user's "
            "wording explicitly or clearly limits the relevant data kind, set knowledge_types to exactly "
            "the relevant collection(s); an explicit single-library request must use only that library. "
            "Otherwise leave it empty: demand-to-supply searches solution, achievement, expert, and "
            "enterprise in parallel; supply-to-demand searches demand only. Never widen the scope when "
            "results are empty. Use this tool when the user's goal is to find compatible supply-demand "
            "candidates; do not invoke it merely because the user mentions a company, person, or topic. "
            "Pass the full original request in user_goal. Set allow_web_fallback=false only if the user "
            "restricts sources to internal materials, or query includes private attachments/drafts instead "
            "of only public terms from the user's original request."
        ),
        annotations=annotations,
        structured_output=True,
    )
    async def kc_match(params: MatchToolInput, ctx: Context) -> dict[str, Any]:
        request = _request_from_context(ctx)
        tool_context = resolve_mcp_tool_context(runtime, request)
        adapter = getattr(runtime, "tool_adapter", None)
        if adapter is None:
            return _failure(tool_context, "匹配检索服务尚未就绪", next_batch=params.next_batch)
        try:
            supervisor = runtime.require_conversation()
            session_store = supervisor.session_store
            session = (
                session_store.get(tool_context.thread_id)
                if session_store is not None and tool_context.thread_id
                else None
            )
            if session and str(session.get("user_id") or "") != tool_context.user_id:
                raise PermissionError("当前用户无权读取该会话")
            state = (session or {}).get("state") or {}
            if material_search_confirmation_required(state):
                return {
                    **_failure(tool_context, "请先确认根据上传资料识别出的匹配意图"),
                    "status": "INTENT_CONFIRM_REQUIRED",
                }
            try:
                pending = resolve_next_match_batch(params.next_batch, state)
            except ValueError:
                if params.next_batch or state.get("request_flow_action") == "next_match_batch":
                    return _no_more_match_batch(tool_context)
                raise
            effective_params, excluded_ids = apply_pending_match_batch(params, pending)
            if excluded_ids:
                logger.info(
                    "MCP match batch applied thread_id=%s trace_id=%s excluded_count=%s query_length=%s",
                    tool_context.thread_id or "-",
                    tool_context.trace_id,
                    len(excluded_ids),
                    len(effective_params.query),
                )
            result = await adapter.search_knowledge(
                build_match_arguments(effective_params, tool_context, excluded_ids=excluded_ids),
                context=tool_context,
            )
            filtered = filter_match_result(
                result,
                excluded_ids,
                top_k=MATCH_RESULT_LIMIT,
            )
            filtered = await judge_and_fallback(
                filtered,
                user_goal=(pending or {}).get("user_goal") or params.user_goal or params.query,
                retrieval_query=effective_params.query,
                adapter=adapter,
                context=tool_context,
                allow_web_fallback=(
                    bool(pending.get("web_fallback_allowed"))
                    if pending is not None
                    else params.allow_web_fallback
                ),
            )
            filtered["_web_fallback_allowed"] = (
                bool(pending.get("web_fallback_allowed"))
                if pending is not None
                else params.allow_web_fallback
            )
            filtered["query"] = effective_params.query
            filtered["match_direction"] = effective_params.direction
            filtered["knowledge_types"] = effective_params.knowledge_types
            if pending is not None:
                return _finish_match_batch(filtered, tool_context)
            return filtered
        except Exception:  # noqa: BLE001 - 保持工具响应结构稳定
            logger.exception(
                "MCP supply-demand match failed thread_id=%s trace_id=%s",
                tool_context.thread_id or "-",
                tool_context.trace_id,
            )
            return _failure(tool_context, "供需匹配执行失败", next_batch=params.next_batch)

    from assistant.integrations.mcp_tools.customer_profile import register_customer_profile_tools
    from assistant.integrations.mcp_tools.document import register_document_tools
    from assistant.integrations.mcp_tools.draft import register_draft_tools
    from assistant.integrations.mcp_tools.web_search import register_web_search_tools

    shared = (mcp, runtime, annotations, _request_from_context, resolve_mcp_tool_context, _failure)
    register_document_tools(*shared)
    register_draft_tools(*shared)
    register_customer_profile_tools(*shared)
    register_web_search_tools(*shared)

    application = mcp.streamable_http_app()
    # Starlette mount 不会运行被挂载应用的生命周期；
    # 父 FastAPI 应用会在自身生命周期中显式启动该管理器。
    application.state.kc_mcp_session_manager = mcp.session_manager
    return application
