"""问答 HTTP 路由：只执行 DeerFlow，业务结果按统一 SSE 合同返回。"""

from __future__ import annotations

import asyncio
import logging
import re
from time import perf_counter
from typing import Any

import httpx

from assistant.application.pending_draft_material import (
    attach_pending_draft_material,
    pending_draft_material_for_response,
    valid_pending_draft_material,
)
from assistant.application.quick_entry_context import (
    clear_quick_entry_context_state,
    load_quick_entry_context,
    new_quick_entry_context_state,
    quick_entry_context_result_state,
)
from assistant.integrations.deerflow_adapter import (
    _merge_business_tool_result,
    encode_sse_event,
    iter_public_deerflow_events,
)
from assistant.application.retrieval_relevance import judge_and_fallback
from assistant.integrations.mcp_server import (
    MATCH_RESULT_LIMIT,
    RetrievalToolInput,
    apply_pending_retrieval_batch,
    build_retrieval_arguments,
    filter_match_result,
    resolve_next_match_batch,
)
from core.contracts.chat import ChatResponse
from core.contracts.tools import ToolContext
from runtime.container import GatewayRuntime
from runtime.thread_identity import resolve_deerflow_thread_id
from services.application.draft_session import (
    load_session_draft_cards,
    load_session_draft_context,
)
from services.application.public_chat_result import (
    PUBLIC_CHAT_FAILURE_MESSAGE,
    PUBLIC_CHAT_TIMEOUT_MESSAGE,
    public_chat_result,
)
from services.persistence.repositories.observability_repository import PostgresAuditSink

logger = logging.getLogger(__name__)
_sse = encode_sse_event
_NEXT_MATCH_BATCH_PHRASES = re.compile(
    r"(?:换一批(?:匹配结果|结果)?|再(?:找|来)(?:一批|一些|几个|几家|几条)(?:类似的)?(?:结果|方案|企业|需求方)?|"
    r"再给我(?:几个|几家|几条|一些)(?:类似的)?(?:结果|方案|企业|需求方)?|"
    r"(?:再)?(?:找|推荐)(?:一些|几个|几家|几条)?类似的(?:结果|方案|企业|需求方)?|"
    r"再推荐(?:几个|几家|一些)(?:类似的)?(?:结果|方案|企业|需求方)?|"
    r"再看看(?:其他|别的)(?:结果|方案|企业|需求方)|"
    r"(?:还有|有没有)(?:没有)?(?:其他|别的|类似的)(?:结果|方案|企业|需求方)?(?:吗)?|"
    r"继续(?:找|检索)(?:一批|一些)(?:类似的)?(?:结果|方案|企业|需求方)?)(?:吧|可以吗)?"
)
_CONTACT_REQUEST_PHRASES = re.compile(
    r"(?:联系(?:一下|下)?(?:人工)?客服|联系客服|联系人工(?:客服)?|"
    r"转(?:接)?人工(?:客服)?|人工对接|申请人工(?:客服|对接))"
)
_CONTACT_REQUEST_NEGATIONS = re.compile(
    r"(?:不需要|不想|不要|别|无需|不用)(?:再)?(?:联系(?:一下|下)?(?:人工)?客服|"
    r"联系客服|联系人工(?:客服)?|转(?:接)?人工(?:客服)?|申请人工(?:客服|对接)|人工对接)"
)
_DRAFT_CARD_REQUEST = re.compile(
    r"^(?:请|麻烦)?(?:帮我)?(?:整理|显示|查看|调出|打开)(?:一下)?"
    r"(?:(?:本会话|当前|已有|现有|全部|所有))*草稿(?:卡片)?$"
)


def is_natural_next_match_batch_request(message: str, chat_flow: dict[str, Any]) -> bool:
    """Match only explicit continuation wording with a usable saved retrieval batch."""

    normalized = re.sub(r"[\s，。！？、,.!?；;：:]", "", message).strip()
    normalized = re.sub(r"^(?:麻烦帮我|请帮我|麻烦|请|帮我)", "", normalized)
    if not _NEXT_MATCH_BATCH_PHRASES.fullmatch(normalized):
        return False
    batch = chat_flow.get("match_batch")
    if not isinstance(batch, dict):
        return False
    if not str(batch.get("current_query") or batch.get("original_query") or "").strip():
        return False
    shown_result_ids = batch.get("shown_result_ids")
    if not isinstance(shown_result_ids, list) or not any(
        str(item).strip() for item in shown_result_ids
    ):
        return False
    business_intent = str(batch.get("business_intent") or "").strip().lower()
    if business_intent == "demand_discovery":
        return True
    if business_intent == "knowledge_search":
        allowed_types = {"demand", "solution", "achievement", "expert", "enterprise"}
        knowledge_types = batch.get("knowledge_types")
        return batch.get("knowledge_type") in allowed_types or (
            isinstance(knowledge_types, list)
            and any(item in allowed_types for item in knowledge_types)
        )
    return business_intent == "supply_demand_match" and batch.get("direction") in {
        "demand_to_supply",
        "supply_to_demand",
    }


def is_explicit_contact_request(message: str) -> bool:
    """Recognize an explicit request for human customer service, not a passing mention."""

    clauses = re.split(r"[，。！？、,.!?；;：:]+", message)
    return any(
        _CONTACT_REQUEST_PHRASES.search(re.sub(r"\s+", "", clause))
        and not _CONTACT_REQUEST_NEGATIONS.search(re.sub(r"\s+", "", clause))
        for clause in clauses
    )


def is_existing_draft_card_request(message: str) -> bool:
    normalized = re.sub(r"[\s，。！？、,.!?；;：:]+", "", message).strip()
    return bool(_DRAFT_CARD_REQUEST.fullmatch(normalized))


async def _contact_request_events(payload: Any, *, thread_id: str):
    """Return a trusted contact action without asking the LLM to infer identity or routing."""

    yield "start", {"thread_id": thread_id}
    yield "done", {
        "result": {
            "status": "SUCCESS",
            "answer": "已为您提供联系客服入口，请点击下方按钮继续。",
            "intent": {"name": "contact_request", "business_intent": "contact_request"},
            "actions": [
                {
                    "id": "contact_wecom",
                    "type": "CONTACT_WECOM",
                    "label": "联系客服",
                    "enabled": True,
                    "query": str(payload.message or "").strip(),
                    "document_ids": [],
                }
            ],
        }
    }


async def _internal_contact_request_events(*, thread_id: str):
    """说明内部身份当前没有可用的客服转接入口。"""

    yield "start", {"thread_id": thread_id}
    yield "done", {
        "result": {
            "status": "SUCCESS",
            "answer": "当前内部账号暂不提供客服转接入口，您可以直接在会话中描述需要协助的事项。",
            "intent": {"name": "contact_request", "business_intent": "contact_request"},
            "actions": [],
        }
    }


async def _back_flow_events(*, thread_id: str):
    """Acknowledge the back action without changing the conversation state."""

    yield "start", {"thread_id": thread_id}
    yield "done", {
        "result": {
            "status": "SUCCESS",
            "answer": "已返回上一步。接下来您想做什么？",
            "actions": [],
        }
    }


async def _session_draft_cards_events(drafts: list[dict[str, Any]], *, thread_id: str):
    yield "start", {"thread_id": thread_id}
    yield "done", {
        "result": {
            "status": "SUCCESS",
            "answer": "已调出本会话草稿，请查看下方卡片。",
            "draft": drafts[0],
            "drafts": drafts,
            "ui_blocks": [{"type": "draft_card", "data": {"draft": drafts[0]}}],
        }
    }


def apply_pending_material_confirmation(payload: Any, pending: dict[str, Any]) -> Any | None:
    """Apply only a clear affirmative reply to the intent saved in this conversation."""

    intent_hint = str(pending.get("intent_hint") or "").strip().lower()
    if intent_hint not in {
        "supply_demand_match",
        "demand_discovery",
        "knowledge_search",
        "enterprise_profile",
    }:
        return None
    affirmative = re.sub(r"[\s，。！？、,.!?；;：:]+", "", str(payload.message or "")).lower()
    if payload.intent_confirmation != "confirmed" and affirmative not in {
        "是",
        "是的",
        "好",
        "好的",
        "可以",
        "确认",
        "确认开始",
        "同意",
        "按这个来",
        "开始吧",
    }:
        return None
    context = dict(pending.get("draft_context") or {})
    if pending.get("match_direction"):
        context["match_direction"] = pending["match_direction"]
    query = str(pending.get("query") or context.get("text") or "").strip()
    return payload.model_copy(
        update={
            "intent_hint": intent_hint,
            "intent_confirmation": "confirmed",
            "knowledge_type": pending.get("knowledge_type"),
            "draft_context": context or None,
            "message": (
                f"用户已确认按“{intent_hint}”处理上传资料。"
                f"保存的检索条件：{query}\n用户确认：{payload.message}"
            ),
        }
    )


async def _next_match_batch_result(
    runtime: GatewayRuntime,
    payload: Any,
    *,
    context: ToolContext,
    chat_flow: dict[str, Any],
) -> dict[str, Any]:
    """Execute an explicit next-batch action against its saved retrieval scope."""

    try:
        pending = resolve_next_match_batch(True, {"chat_flow": chat_flow})
    except ValueError:
        return {
            "status": "NO_EVIDENCE",
            "answer": "当前知识库没有更多匹配结果。若还想获取更多信息，请明确告诉我“联网搜索”，我再联网查询。",
            "evidences": [],
            "match_results": [],
            "ui_blocks": [],
            "actions": [],
            "intent": {},
        }
    if pending is None:
        raise ValueError("当前会话没有可继续的检索结果")
    knowledge_types = pending.get("knowledge_types") or (
        [pending["knowledge_type"]] if pending.get("knowledge_type") else []
    )
    if not knowledge_types:
        return {
            "status": "NO_EVIDENCE",
            "answer": "当前会话没有保存检索范围，无法安全换批。请重新提出原始检索需求。",
            "evidences": [],
            "match_results": [],
            "ui_blocks": [],
            "actions": [],
            "intent": {},
        }
    adapter = getattr(runtime, "tool_adapter", None)
    if adapter is None:
        raise RuntimeError("知识检索服务尚未就绪")

    params = RetrievalToolInput(
        query=pending["query"],
        scope_id=pending.get("scope_id"),
        knowledge_type=pending.get("knowledge_type"),
        knowledge_types=knowledge_types,
        next_batch=True,
    )
    effective_params, excluded_ids = apply_pending_retrieval_batch(params, pending)
    started_at = perf_counter()
    tool_result = await adapter.search_knowledge(
        build_retrieval_arguments(
            effective_params,
            context,
            excluded_ids=excluded_ids,
        ),
        context=context,
    )
    filtered = filter_match_result(
        tool_result,
        excluded_ids,
        top_k=MATCH_RESULT_LIMIT,
    )
    filtered = await judge_and_fallback(
        filtered,
        user_goal=str(pending.get("user_goal") or pending["query"]),
        retrieval_query=effective_params.query,
        adapter=adapter,
        context=context,
        allow_web_fallback=bool(pending.get("web_fallback_allowed")),
    )
    filtered["query"] = effective_params.query
    filtered["knowledge_types"] = effective_params.knowledge_types
    filtered["_next_match_batch"] = True

    business_intent = pending["business_intent"]
    direction = pending["direction"]
    result: dict[str, Any] = {
        "status": "SUCCESS",
        "answer": "已为您检索下一批结果。",
        "evidences": [],
        "match_results": [],
        "ui_blocks": [],
        "actions": [],
        "intent": {
            "name": business_intent,
            "business_intent": business_intent,
            "operation": business_intent,
            "query": effective_params.query,
            "entities": {
                "next_match_batch": True,
                "knowledge_types": effective_params.knowledge_types,
                **(
                    {"match_direction": direction}
                    if direction in {"demand_to_supply", "supply_to_demand"}
                    else {}
                ),
            },
        },
        "processing_elapsed_ms": round((perf_counter() - started_at) * 1000, 2),
    }
    status = str(filtered.get("status") or "SUCCESS").upper()
    if status == "NO_EVIDENCE":
        result.update(
            status="NO_EVIDENCE",
            answer="当前知识库没有更多匹配结果。若还想获取更多信息，请明确告诉我“联网搜索”，我再联网查询。",
        )
        return result
    if status not in {"SUCCESS", "PARTIAL_SUCCESS", "WEB_FALLBACK"}:
        result.update(status="FAILED", answer="换批检索失败，请稍后重试。")
        return result

    tool_name = "kc_match" if business_intent == "supply_demand_match" else "kc_retrieval"
    merge_payload = payload.model_dump(exclude_none=True)
    merge_payload["intent_hint"] = business_intent
    _merge_business_tool_result(
        result,
        {
            "type": "tool",
            "name": tool_name,
            "artifact": {"structured_content": filtered},
        },
        merge_payload,
        user_role=context.user_role,
    )
    if result["match_results"]:
        if not any(
            action.get("id") == "next_match_batch"
            for action in result["actions"]
            if isinstance(action, dict)
        ):
            result["actions"].append(
                {
                    "id": "next_match_batch",
                    "type": "NEXT_MATCH_BATCH",
                    "label": "换一批",
                    "message": "换一批匹配结果",
                    "enabled": True,
                    "intent_hint": business_intent,
                }
            )
    elif result.get("web_fallback"):
        result["status"] = "WEB_FALLBACK"
        result["actions"] = []
    else:
        result.update(
            status="NO_EVIDENCE",
            answer="当前知识库没有更多匹配结果。若还想获取更多信息，请明确告诉我“联网搜索”，我再联网查询。",
            match_results=[],
            ui_blocks=[],
            actions=[],
        )
    result["processing_elapsed_ms"] = round((perf_counter() - started_at) * 1000, 2)
    return result


async def _next_match_batch_events(
    runtime: GatewayRuntime,
    payload: Any,
    *,
    context: ToolContext,
    chat_flow: dict[str, Any],
):
    yield "start", {"thread_id": context.thread_id}
    result = await _next_match_batch_result(
        runtime,
        payload,
        context=context,
        chat_flow=chat_flow,
    )
    yield "done", {"result": result}


def persist_stream_terminal(
    runtime: GatewayRuntime,
    *,
    thread_id: str,
    user_id: str,
    trace_id: str,
    result: dict[str, Any],
    status: str,
) -> None:
    store = runtime.require_conversation().session_store
    session = store.get(thread_id) or {}
    flow = dict((session.get("state") or {}).get("chat_flow") or {})
    terminal = {**result, "status": status, "trace_id": trace_id, "thread_id": thread_id}
    flow.update(last_status=status, last_answer=str(result.get("answer") or ""))
    store.upsert(
        thread_id, user_id=user_id, state={"chat_flow": flow, "last_chat_terminal": terminal}
    )
    store.append_message(
        thread_id,
        user_id=user_id,
        role="assistant",
        content=terminal.get("answer", ""),
        response=terminal,
    )


async def deerflow_stream(
    runtime: GatewayRuntime,
    payload: Any,
    *,
    context: ToolContext,
    request_id: str,
    access_token: str,
):
    resolved_user_id = request_user_id = context.user_id
    _resolved_role = context.user_role
    thread_id = context.thread_id
    deerflow_thread_id = resolve_deerflow_thread_id(thread_id, resolved_user_id)
    supervisor = runtime.require_conversation()
    session_store = supervisor.session_store

    def persist_run_id(run_id: str) -> None:
        session_store.upsert(
            thread_id,
            user_id=resolved_user_id,
            user_role=_resolved_role,
            state={"last_deerflow_run_id": run_id},
        )

    terminal = {}
    partial_text = ""
    chat_flow_state: dict[str, Any] = {}
    pending_draft_material: dict[str, Any] | None = None
    confirmed_material_intent: dict[str, Any] | None = None
    pending_deerflow_context: list[dict[str, str]] | None = None
    pending_context_failure_count = 0
    pending_context_response_valid = False
    pending_context_status: str | None = None
    quick_entry_context_created = False
    back_flow_requested = payload.flow_action == "back"
    new_conversation = False

    def persist_quick_entry_context(messages: list[dict[str, str]]) -> None:
        nonlocal quick_entry_context_created
        if session_store is None:
            return
        session_store.upsert(
            thread_id,
            user_id=resolved_user_id,
            user_role=_resolved_role,
            state=new_quick_entry_context_state(messages, payload.intent_hint),
        )
        quick_entry_context_created = True

    def clear_pending_deerflow_context() -> None:
        if session_store is None:
            return
        try:
            session_store.upsert(
                thread_id,
                user_id=resolved_user_id,
                user_role=_resolved_role,
                state=clear_quick_entry_context_state(),
            )
        except Exception:
            logger.warning(
                "DeerFlow pending quick-entry context clear failed user_id=%s thread_id=%s trace_id=%s",
                resolved_user_id,
                thread_id,
                context.trace_id,
                exc_info=True,
            )
    try:
        agent_payload = payload
        try:
            if session_store is not None:
                session = session_store.get(thread_id) or {}
                owner_id = str(session.get("user_id") or "").strip()
                if owner_id and owner_id != request_user_id:
                    raise PermissionError("当前用户无权使用该会话")
                session_state = session.get("state") or {}
                (
                    pending_deerflow_context,
                    pending_context_failure_count,
                    discard_pending_context,
                ) = load_quick_entry_context(
                    session_state,
                    message=agent_payload.message,
                    intent_hint=agent_payload.intent_hint,
                    flow_action=agent_payload.flow_action,
                )
                new_conversation = not bool(
                    session_state.get("last_message_id") or pending_deerflow_context
                )
                if discard_pending_context:
                    clear_pending_deerflow_context()
                flow = session_state.get("chat_flow") or {}
                chat_flow_state = dict(flow)
                pending_draft_material = valid_pending_draft_material(
                    chat_flow_state.get("pending_draft_material")
                )
                if pending_draft_material:
                    agent_payload = agent_payload.model_copy(
                        update={
                            "message": attach_pending_draft_material(
                                agent_payload.message, pending_draft_material
                            )
                        }
                    )
                elif "pending_draft_material" in chat_flow_state:
                    chat_flow_state.pop("pending_draft_material", None)
                    session_store.upsert(
                        thread_id,
                        user_id=resolved_user_id,
                        user_role=_resolved_role,
                        state={"chat_flow": chat_flow_state},
                    )
                pending_material_intent = chat_flow_state.get("pending_material_intent")
                if isinstance(pending_material_intent, dict) and not back_flow_requested:
                    confirmed_payload = apply_pending_material_confirmation(
                        agent_payload, pending_material_intent
                    )
                    if confirmed_payload is not None:
                        confirmed_material_intent = dict(pending_material_intent)
                    agent_payload = confirmed_payload or agent_payload
                    chat_flow_state["material_intent_confirmation_required"] = False
                    chat_flow_state.pop("pending_material_intent", None)
                    session_store.upsert(
                        thread_id,
                        user_id=resolved_user_id,
                        user_role=_resolved_role,
                        state={"chat_flow": chat_flow_state},
                    )
                if agent_payload.flow_action is None and is_natural_next_match_batch_request(
                    agent_payload.message, chat_flow_state
                ):
                    agent_payload = agent_payload.model_copy(
                        update={"flow_action": "next_match_batch"}
                    )
                draft_context = dict(agent_payload.draft_context or {})
                selected_direction = str(draft_context.get("match_direction") or "").strip().lower()
                state_update = {
                    "last_request_id": request_id,
                    "last_trace_id": context.trace_id,
                    "deerflow_thread_id": deerflow_thread_id,
                    "request_flow_action": agent_payload.flow_action,
                }
                if selected_direction in {"demand_to_supply", "supply_to_demand"} and (
                    payload.intent_confirmation == "confirmed"
                    or payload.intent_hint == "supply_demand_match"
                ):
                    state_update["selected_match_direction"] = selected_direction
                elif agent_payload.flow_action == "switch":
                    state_update["selected_match_direction"] = None
                latest_draft = load_session_draft_context(
                    runtime,
                    thread_id=thread_id,
                    user_id=resolved_user_id,
                    user_role=_resolved_role,
                )
                if latest_draft is not None:
                    draft_context = latest_draft
                    agent_payload = agent_payload.model_copy(update={"draft_context": latest_draft})
                try:
                    session_store.upsert(
                        thread_id,
                        user_id=resolved_user_id,
                        user_role=_resolved_role,
                        state=state_update,
                    )
                    session_store.append_message(
                        thread_id,
                        user_id=resolved_user_id,
                        role="user",
                        content=payload.message,
                    )
                except PermissionError:
                    raise
                except Exception:  # 会话历史处理失败不能丢弃回答
                    logger.warning(
                        "DeerFlow user message persist failed user_id=%s thread_id=%s trace_id=%s",
                        resolved_user_id,
                        thread_id,
                        context.trace_id,
                        exc_info=True,
                    )
        except PermissionError:
            yield _sse(
                "error",
                {
                    "message": "当前会话不属于当前用户，请刷新后重试。",
                    "stage": "session",
                    "thread_id": thread_id,
                },
            )
            return
        except Exception as exc:  # 权威上下文不完整时必须终止本轮
            logger.warning(
                "DeerFlow session context load failed user_id=%s thread_id=%s trace_id=%s error_type=%s",
                resolved_user_id,
                thread_id,
                context.trace_id,
                type(exc).__name__,
                exc_info=True,
            )
            terminal = {
                "status": "FAILED",
                "answer": "",
                "error": type(exc).__name__,
                "stage": "session",
            }
            yield _sse(
                "error",
                {
                    "message": PUBLIC_CHAT_FAILURE_MESSAGE,
                    "stage": "session",
                    "thread_id": thread_id,
                },
            )
            return

        completion_saved = False
        try:
            deerflow_payload = agent_payload.model_dump(exclude_none=True)
            explicit_contact_request = is_explicit_contact_request(agent_payload.message)
            if explicit_contact_request and _resolved_role == "external":
                events = _contact_request_events(agent_payload, thread_id=thread_id)
            elif explicit_contact_request and _resolved_role == "internal":
                events = _internal_contact_request_events(thread_id=thread_id)
            elif back_flow_requested:
                events = _back_flow_events(thread_id=thread_id)
            elif is_existing_draft_card_request(payload.message) and (
                session_drafts := load_session_draft_cards(
                    runtime,
                    thread_id=thread_id,
                    user_id=resolved_user_id,
                    user_role=_resolved_role,
                )
            ):
                events = _session_draft_cards_events(session_drafts, thread_id=thread_id)
            elif agent_payload.flow_action == "next_match_batch":
                events = _next_match_batch_events(
                    runtime,
                    agent_payload,
                    context=context,
                    chat_flow=chat_flow_state,
                )
            else:
                events = iter_public_deerflow_events(
                    runtime.deerflow_runner,
                    deerflow_payload,
                    thread_id=thread_id,
                    identity={"user_id": resolved_user_id, "user_role": _resolved_role},
                    new_conversation=new_conversation,
                    trace_id=context.trace_id,
                    on_run_id=persist_run_id,
                    prior_messages=pending_deerflow_context,
                    on_quick_entry_context=persist_quick_entry_context,
                    model_name="kc-main",
                    secrets={
                        "kc_access_token": access_token,
                        "kc_thread_id": thread_id,
                    },
                )
            async for event, event_payload in events:
                if event == "token":
                    partial_text += str(event_payload.get("text") or "")
                elif event == "error":
                    terminal = {
                        "status": "FAILED",
                        "answer": partial_text,
                        "error": event_payload.get("message"),
                        "stage": event_payload.get("stage"),
                    }
                if event == "done" and not completion_saved:
                    completion_saved = True
                    result = event_payload.get("result")
                    if isinstance(result, dict):
                        response = None
                        try:
                            response = ChatResponse.model_validate(result).model_copy(
                                update={
                                    "request_id": request_id,
                                    "trace_id": context.trace_id,
                                    "thread_id": thread_id,
                                }
                            )
                            if response.status.strip().upper() == "DRAFT_MULTI_CONFIRM_REQUIRED":
                                response = response.model_copy(
                                    update={
                                        "draft": None,
                                        "drafts": [],
                                        "ui_blocks": [
                                            block
                                            for block in response.ui_blocks
                                            if not isinstance(block, dict)
                                            or block.get("type") != "draft_card"
                                        ],
                                    }
                                )
                            if (
                                response.draft
                                or response.drafts
                                or response.status.strip().upper() in {"DRAFT_GENERATED", "DRAFT_SAVED"}
                            ):
                                session_drafts = load_session_draft_cards(
                                    runtime,
                                    thread_id=thread_id,
                                    user_id=resolved_user_id,
                                    user_role=_resolved_role,
                                )
                                if session_drafts:
                                    known_ids = {
                                        str(item.get("draft_id") or "")
                                        for item in session_drafts
                                    }
                                    current_drafts = response.drafts or (
                                        [response.draft] if response.draft else []
                                    )
                                    session_drafts.extend(
                                        item for item in current_drafts
                                        if str(item.get("draft_id") or "") not in known_ids
                                    )
                                    response = response.model_copy(
                                        update={
                                            "draft": session_drafts[0],
                                            "drafts": session_drafts,
                                            "ui_blocks": [
                                                *[
                                                    block
                                                    for block in response.ui_blocks
                                                    if not isinstance(block, dict)
                                                    or block.get("type") != "draft_card"
                                                ],
                                                {"type": "draft_card", "data": {"draft": session_drafts[0]}},
                                            ],
                                        }
                                    )
                            record_chat_result = getattr(supervisor, "record_chat_result", None)
                            if callable(record_chat_result) and not back_flow_requested:
                                flow_state = record_chat_result(thread_id, response, agent_payload)
                                response = response.model_copy(update={"flow_state": flow_state})
                            response_status = response.status.strip().upper()
                            next_pending = None
                            try:
                                if (
                                    session_store is not None
                                    and response_status not in {
                                        "FAILED", "ERROR", "TIMEOUT", "CANCELLED"
                                    }
                                    and (
                                        pending_draft_material is not None
                                        or response_status
                                        in {
                                            "DRAFT_MULTI_CONFIRM_REQUIRED",
                                            "INTENT_CONFIRM_REQUIRED",
                                        }
                                    )
                                ):
                                    next_pending = pending_draft_material_for_response(
                                        response_status,
                                        user_message=payload.message,
                                        full_material=(
                                            pending_draft_material.get("text")
                                            if pending_draft_material
                                            else None
                                        ),
                                        file_name=(
                                            pending_draft_material.get("file_name")
                                            if pending_draft_material
                                            else None
                                        ),
                                    )
                                    latest_session = session_store.get(thread_id) or {}
                                    latest_state = latest_session.get("state") or {}
                                    latest_flow = dict(latest_state.get("chat_flow") or {})
                                    if next_pending != pending_draft_material:
                                        if next_pending:
                                            latest_flow["pending_draft_material"] = next_pending
                                        else:
                                            latest_flow.pop("pending_draft_material", None)
                                        session_store.upsert(
                                            thread_id,
                                            user_id=resolved_user_id,
                                            user_role=_resolved_role,
                                            state={"chat_flow": latest_flow},
                                        )
                            except Exception:
                                logger.warning(
                                    "DeerFlow pending draft material persist failed user_id=%s thread_id=%s trace_id=%s",
                                    resolved_user_id,
                                    thread_id,
                                    context.trace_id,
                                    exc_info=True,
                                )
                                if (
                                    next_pending is not None
                                    and response_status
                                    in {
                                        "DRAFT_MULTI_CONFIRM_REQUIRED",
                                        "INTENT_CONFIRM_REQUIRED",
                                    }
                                ):
                                    response = response.model_copy(
                                        update={
                                            "status": "DRAFT_CONTEXT_SAVE_FAILED",
                                            "answer": "完整资料暂存失败，尚未执行草稿操作。请重新提交资料后重试。",
                                            "draft": None,
                                            "drafts": [],
                                            "actions": [],
                                            "flow_state": "ERROR",
                                            "ui_blocks": [
                                                block
                                                for block in response.ui_blocks
                                                if not isinstance(block, dict)
                                                or block.get("type") != "draft_card"
                                            ],
                                        }
                                    )
                        except Exception:  # 流程状态持久化失败不能丢弃回答
                            logger.warning(
                                "DeerFlow flow state persist failed user_id=%s thread_id=%s trace_id=%s",
                                resolved_user_id,
                                thread_id,
                                context.trace_id,
                                exc_info=True,
                            )
                        if response is not None:
                            if pending_deerflow_context is not None:
                                pending_context_response_valid = True
                                pending_context_status = response.status
                            intent = response.intent or {}
                            business_intent = (
                                str(intent.get("business_intent") or intent.get("name") or "")
                                .strip()
                                .lower()
                            )
                            match_store = getattr(runtime, "match_result_store", None)
                            if match_store is not None and business_intent == "supply_demand_match":
                                try:
                                    match_context = agent_payload.draft_context or {}
                                    match_store.save(
                                        result_id=request_id,
                                        user_id=resolved_user_id,
                                        session_id=thread_id,
                                        query=str(
                                            intent.get("query")
                                            or match_context.get("text")
                                            or payload.message
                                        ).strip(),
                                        draft_id=match_context.get("draft_id"),
                                        result=response.model_dump(mode="json"),
                                    )
                                except Exception:  # 结果持久化失败不能丢弃回答
                                    logger.warning(
                                        "DeerFlow match result persist failed user_id=%s thread_id=%s trace_id=%s",
                                        resolved_user_id,
                                        thread_id,
                                        context.trace_id,
                                        exc_info=True,
                                    )
                            try:
                                PostgresAuditSink().record_chat_summary(
                                    user_id=resolved_user_id,
                                    session_id=thread_id,
                                    request_id=request_id,
                                    trace_id=context.trace_id,
                                    result=response,
                                    draft_context=agent_payload.draft_context,
                                    duration_ms=response.processing_elapsed_ms or 0,
                                )
                            except Exception:  # 审计失败不能丢弃回答
                                logger.warning(
                                    "DeerFlow chat audit persist failed user_id=%s thread_id=%s trace_id=%s",
                                    resolved_user_id,
                                    thread_id,
                                    context.trace_id,
                                    exc_info=True,
                                )
                            event_payload = {
                                **event_payload,
                                "result": public_chat_result(response, user_role=_resolved_role),
                            }
                if event == "done":
                    terminal = dict(event_payload.get("result") or {})
                yield _sse(event, event_payload)
                if event in {"done", "error"}:
                    break
            if not terminal:
                terminal = {
                    "status": "FAILED",
                    "answer": partial_text,
                    "error": "上游流结束但未返回终态",
                    "stage": "stream",
                }
                yield _sse(
                    "error",
                    {
                        "message": PUBLIC_CHAT_FAILURE_MESSAGE,
                        "stage": "failed",
                        "thread_id": thread_id,
                    },
                )
        except Exception as exc:  # 对外流必须以稳定的错误事件结束
            terminal = {
                "status": "FAILED",
                "answer": partial_text,
                "error": type(exc).__name__,
                "stage": "failed",
            }
            logger.warning(
                "DeerFlow stream failed thread_id=%s trace_id=%s error_type=%s",
                thread_id,
                context.trace_id,
                type(exc).__name__,
                exc_info=True,
            )
            yield _sse(
                "error",
                {
                    "message": (
                        PUBLIC_CHAT_TIMEOUT_MESSAGE
                        if isinstance(exc, (TimeoutError, httpx.TimeoutException))
                        else PUBLIC_CHAT_FAILURE_MESSAGE
                    ),
                    "stage": "failed",
                    "thread_id": thread_id,
                },
            )

    except asyncio.CancelledError:
        terminal = {
            "status": "CANCELLED",
            "answer": partial_text,
            "error": "客户端取消",
            "stage": "cancelled",
        }
        raise
    finally:
        if (
            pending_deerflow_context is not None
            and not back_flow_requested
            and not quick_entry_context_created
            and session_store is not None
        ):
            try:
                session_store.upsert(
                    thread_id,
                    user_id=resolved_user_id,
                    user_role=_resolved_role,
                    state=quick_entry_context_result_state(
                        pending_context_failure_count,
                        status=pending_context_status or terminal.get("status"),
                        response_valid=pending_context_response_valid,
                    ),
                )
            except Exception:
                logger.warning(
                    "DeerFlow pending quick-entry context update failed user_id=%s thread_id=%s trace_id=%s",
                    resolved_user_id,
                    thread_id,
                    context.trace_id,
                    exc_info=True,
                )
        if (
            confirmed_material_intent is not None
            and str(terminal.get("status") or "").upper()
            in {"FAILED", "ERROR", "TIMEOUT", "CANCELLED"}
            and session_store is not None
        ):
            chat_flow_state["material_intent_confirmation_required"] = True
            chat_flow_state["pending_material_intent"] = confirmed_material_intent
            session_store.upsert(
                thread_id,
                user_id=resolved_user_id,
                user_role=_resolved_role,
                state={"chat_flow": chat_flow_state},
            )
        if terminal:
            persist_stream_terminal(
                runtime,
                thread_id=thread_id,
                user_id=resolved_user_id,
                trace_id=context.trace_id,
                result={**terminal, "request_id": request_id},
                status=terminal["status"],
            )
