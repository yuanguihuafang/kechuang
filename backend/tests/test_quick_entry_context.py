import time

from assistant.application.quick_entry_context import (
    QUICK_ENTRY_CONTEXT_TTL_SECONDS,
    load_quick_entry_context,
    new_quick_entry_context_state,
    quick_entry_context_result_state,
)


def _state(*, failures=0, created_at=None, intent="enterprise_profile"):
    state = new_quick_entry_context_state(
        [
            {"role": "user", "content": "企业画像"},
            {"role": "assistant", "content": "请提供企业名称"},
        ],
        intent,
    )
    state["pending_deerflow_context_failures"] = failures
    if created_at is not None:
        state["pending_deerflow_context_created_at"] = created_at
    return state


def test_quick_entry_context_is_discarded_after_expiry_or_intent_switch():
    now = time.time()
    expired = _state(created_at=now - QUICK_ENTRY_CONTEXT_TTL_SECONDS)
    switched = _state()

    assert load_quick_entry_context(
        expired, message="公司名称", intent_hint="enterprise_profile", now=now
    ) == (None, 0, True)
    assert load_quick_entry_context(
        switched, message="找需求", intent_hint="demand_discovery", now=now
    ) == (None, 0, True)


def test_quick_entry_context_is_discarded_after_explicit_cancel():
    assert load_quick_entry_context(
        _state(), message="算了，不用了", intent_hint=None
    ) == (None, 0, True)


def test_back_flow_keeps_quick_entry_context_for_the_next_message():
    state = _state()
    messages = state["pending_deerflow_context"]

    assert load_quick_entry_context(
        state,
        message="返回上一步",
        intent_hint=None,
        flow_action="back",
    ) == (messages, 0, False)


def test_quick_entry_context_allows_one_failed_retry_then_clears():
    assert quick_entry_context_result_state(
        0, status="FAILED", response_valid=True
    ) == {"pending_deerflow_context_failures": 1}
    assert quick_entry_context_result_state(
        1, status="FAILED", response_valid=True
    )["pending_deerflow_context"] is None


def test_quick_entry_context_clears_after_success_or_cancellation():
    assert quick_entry_context_result_state(
        0, status="SUCCESS", response_valid=True
    )["pending_deerflow_context"] is None
    assert quick_entry_context_result_state(
        0, status="CANCELLED", response_valid=False
    )["pending_deerflow_context"] is None
