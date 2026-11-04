"""统一会话流程状态；响应状态只在这里映射，避免前后端各自猜测。"""

from collections.abc import Mapping
from enum import StrEnum
from typing import Any

CONVERSATION_STATE_DEFAULTS: dict[str, Any] = {
    "active_intent": "",
    "stage": "IDLE",
    "match_direction": "",
    "draft_id": None,
    "draft_type": None,
    "document_ids": [],
    "pending_action": "",
    "last_user_message": "",
    "last_assistant_message": "",
    "match_batch": {},
}


def normalize_conversation_state(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """填充空会话默认值，不覆盖已有业务字段。"""
    state = dict(value or {})
    normalized = {**CONVERSATION_STATE_DEFAULTS, **state}
    normalized["document_ids"] = list(
        dict.fromkeys(
            str(item) for item in (normalized.get("document_ids") or []) if str(item).strip()
        )
    )
    return normalized


def material_search_confirmation_required(session_state: Mapping[str, Any]) -> bool:
    """Whether uploaded material still requires explicit intent confirmation."""

    flow = session_state.get("chat_flow")
    return isinstance(flow, Mapping) and flow.get("material_intent_confirmation_required") is True


class ChatFlowState(StrEnum):
    IDLE = "IDLE"
    INTENT_CONFIRM = "INTENT_CONFIRM"
    INFO_REQUIRED = "INFO_REQUIRED"
    DRAFT_EDITING = "DRAFT_EDITING"
    DRAFT_GENERATING = "DRAFT_GENERATING"
    RECORDING = "RECORDING"
    MATCHING = "MATCHING"
    QUERY_REQUIRED = "QUERY_REQUIRED"
    WAITING_PUBLISH = "WAITING_PUBLISH"
    PUBLISHING = "PUBLISHING"
    INDEXED = "INDEXED"
    PUBLISH_FAILED = "PUBLISH_FAILED"
    MATCH_CONFIRM = "MATCH_CONFIRM"
    EXECUTING = "EXECUTING"
    COMPLETED = "COMPLETED"
    INTERRUPTED = "INTERRUPTED"
    ROLLED_BACK = "ROLLED_BACK"
    SWITCHING = "SWITCHING"
    ERROR = "ERROR"


_STATUS_STATES = {
    "INTENT_CONFIRM_REQUIRED": ChatFlowState.INTENT_CONFIRM,
    "INTERNAL_INTENT_CONFIRM_REQUIRED": ChatFlowState.INTENT_CONFIRM,
    "FLOW_ROLLED_BACK": ChatFlowState.ROLLED_BACK,
    "FLOW_INTERRUPTED": ChatFlowState.INTERRUPTED,
    "DRAFT_INFO_REQUIRED": ChatFlowState.INFO_REQUIRED,
    "DRAFT_MULTI_CONFIRM_REQUIRED": ChatFlowState.INFO_REQUIRED,
    "CUSTOMER_VISIT_RECORDING": ChatFlowState.INFO_REQUIRED,
    "DRAFT_GENERATED": ChatFlowState.DRAFT_EDITING,
    "DRAFT_SAVED": ChatFlowState.DRAFT_EDITING,
    "INTENT_CONFIRMED": ChatFlowState.INFO_REQUIRED,
    "RETRIEVAL_CONTEXT_REQUIRED": ChatFlowState.INFO_REQUIRED,
    "FLOW_CONTEXT_REQUIRED": ChatFlowState.INFO_REQUIRED,
    "MATCH_DIRECTION_REQUIRED": ChatFlowState.MATCH_CONFIRM,
    "MATCH_CONFIRM_REQUIRED": ChatFlowState.MATCH_CONFIRM,
    "INDEXING": ChatFlowState.EXECUTING,
    "SEARCHING": ChatFlowState.EXECUTING,
    "SUCCESS": ChatFlowState.COMPLETED,
    "CONVERSATION_RESPONSE": ChatFlowState.COMPLETED,
    "PUBLISHED": ChatFlowState.COMPLETED,
    "ACCESS_DENIED": ChatFlowState.ERROR,
    "INTENT_CLASSIFICATION_UNAVAILABLE": ChatFlowState.ERROR,
    "INTENT_CLASSIFICATION_INVALID": ChatFlowState.ERROR,
}

_TASK_STATUS_STATES = {
    "UPLOADED": ChatFlowState.INFO_REQUIRED,
    "PARSING": ChatFlowState.EXECUTING,
    "INDEXING": ChatFlowState.EXECUTING,
    "RUNNING": ChatFlowState.EXECUTING,
    "QUEUED": ChatFlowState.EXECUTING,
    "WAITING_INTENT_CONFIRM": ChatFlowState.INTENT_CONFIRM,
    "WAITING_DRAFT_CONFIRM": ChatFlowState.DRAFT_EDITING,
    "WAITING_CONFIRM": ChatFlowState.INFO_REQUIRED,
    "SUCCEEDED": ChatFlowState.COMPLETED,
    "COMPLETED": ChatFlowState.COMPLETED,
    "PUBLISHED": ChatFlowState.COMPLETED,
    "FAILED": ChatFlowState.ERROR,
    "CANCELLED": ChatFlowState.INTERRUPTED,
    "INTERRUPTED": ChatFlowState.INTERRUPTED,
}


def state_for_response(status: str | None) -> ChatFlowState:
    return _STATUS_STATES.get(str(status or "").upper(), ChatFlowState.ERROR)


def state_for_task_status(status: str | None) -> ChatFlowState:
    """把文件/任务状态映射到同一套用户流程状态。"""

    return _TASK_STATUS_STATES.get(str(status or "").upper(), ChatFlowState.ERROR)


def with_flow_state(payload: dict[str, object]) -> dict[str, object]:
    """为聊天响应或文件任务补充统一流程状态。"""

    if payload.get("flow_state"):
        return dict(payload)
    status = str(payload.get("status") or "")
    state = (
        state_for_task_status(status)
        if status.upper() in _TASK_STATUS_STATES
        else state_for_response(status)
    )
    return {**payload, "flow_state": state.value}
