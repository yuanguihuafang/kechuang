"""Lifecycle rules for the short-lived context created by fixed quick entries."""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

QUICK_ENTRY_CONTEXT_TTL_SECONDS = 30 * 60
_MAX_FAILURES = 1
_CLEAR_STATE = {
    "pending_deerflow_context": None,
    "pending_deerflow_context_created_at": None,
    "pending_deerflow_context_failures": None,
    "pending_deerflow_context_intent": None,
}
_FAILURE_STATUSES = {"FAILED", "ERROR", "TIMEOUT", "CANCELLED"}
_ABANDON_PREFIXES = (
    "取消",
    "算了",
    "不查了",
    "别查了",
    "不找了",
    "别找了",
    "不用了",
    "不需要了",
    "先不做",
    "先不弄",
    "换个方向",
    "换成",
    "换到",
    "改成",
    "改为",
    "切换到",
    "重新开始",
)


def new_quick_entry_context_state(
    messages: list[dict[str, str]], intent_hint: str | None
) -> dict[str, Any]:
    return {
        "pending_deerflow_context": messages,
        "pending_deerflow_context_created_at": time.time(),
        "pending_deerflow_context_failures": 0,
        "pending_deerflow_context_intent": str(intent_hint or "").strip().lower() or None,
    }


def load_quick_entry_context(
    state: Mapping[str, Any],
    *,
    message: str,
    intent_hint: str | None,
    flow_action: str | None = None,
    now: float | None = None,
) -> tuple[list[dict[str, str]] | None, int, bool]:
    messages = state.get("pending_deerflow_context")
    if messages is None:
        return None, 0, False
    if (
        not isinstance(messages, list)
        or len(messages) != 2
        or any(
            not isinstance(item, dict)
            or not isinstance(item.get("content"), str)
            or not item["content"].strip()
            for item in messages
        )
        or messages[0].get("role") != "user"
        or messages[1].get("role") != "assistant"
    ):
        return None, 0, True

    created_at = state.get("pending_deerflow_context_created_at")
    failures = state.get("pending_deerflow_context_failures")
    if (
        isinstance(created_at, bool)
        or not isinstance(created_at, (int, float))
        or type(failures) is not int
        or failures < 0
        or failures > _MAX_FAILURES
    ):
        return None, 0, True

    current_time = time.time() if now is None else now
    if created_at > current_time or current_time - created_at >= QUICK_ENTRY_CONTEXT_TTL_SECONDS:
        return None, 0, True

    action = str(flow_action or "").strip().lower()
    normalized_message = str(message or "").strip().lower()
    if action == "back":
        return messages, failures, False
    if action in {"switch", "interrupt"} or normalized_message.startswith(
        _ABANDON_PREFIXES
    ):
        return None, 0, True

    saved_intent = str(state.get("pending_deerflow_context_intent") or "").strip().lower()
    requested_intent = str(intent_hint or "").strip().lower()
    if saved_intent and requested_intent and saved_intent != requested_intent:
        return None, 0, True
    return messages, failures, False


def quick_entry_context_result_state(
    failure_count: int, *, status: str | None, response_valid: bool
) -> dict[str, Any]:
    normalized_status = str(status or "").strip().upper()
    if normalized_status == "CANCELLED" or (
        response_valid and normalized_status and normalized_status not in _FAILURE_STATUSES
    ) or failure_count >= _MAX_FAILURES:
        return dict(_CLEAR_STATE)
    return {"pending_deerflow_context_failures": failure_count + 1}


def clear_quick_entry_context_state() -> dict[str, Any]:
    return dict(_CLEAR_STATE)
