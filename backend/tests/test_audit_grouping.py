from types import SimpleNamespace

from services.persistence.repositories.observability_repository import PostgresAuditSink


def test_chat_summary_groups_one_interaction_without_message_content():
    result = SimpleNamespace(
        status="SUCCESS",
        intent={"name": "knowledge_search", "operation": "knowledge_search"},
        drafts=[{"draft_title": "测试草稿"}],
        draft=None,
        recommendations=[{"id": "match-1"}],
        related_references=[],
        evidences=[{"id": "evidence-1"}],
        retrieval_elapsed_ms=120.5,
        error=None,
        flow_state="COMPLETED",
    )

    payload = PostgresAuditSink._chat_summary_payload(
        result=result,
        draft_context={"attachments": [{"file_name": "demo.pdf"}]},
        duration_ms=300.0,
        user_name="测试用户",
        company_name="测试企业",
    )

    assert payload == {
        "status": "SUCCESS",
        "user_name": "测试用户",
        "company_name": "测试企业",
        "intent": "knowledge_search",
        "intent_operation": "knowledge_search",
        "draft_generated": True,
        "draft_title": "测试草稿",
        "attachment_uploaded": True,
        "attachment_count": 1,
        "attachment_names": ["demo.pdf"],
        "match_generated": True,
        "match_count": 1,
        "evidence_count": 1,
        "retrieval_duration_ms": 120.5,
        "duration_ms": 300.0,
        "flow_state": "COMPLETED",
    }


def test_internal_supervisor_steps_are_not_persisted_as_audit_rows():
    assert "chat_completed" in PostgresAuditSink._KEY_EVENTS
    assert "supervisor_intent_selected" not in PostgresAuditSink._KEY_EVENTS
    assert "supervisor_reasoning_end" not in PostgresAuditSink._KEY_EVENTS


def test_chat_summary_persists_trace_id():
    sink = object.__new__(PostgresAuditSink)
    recorded = []
    sink.record = lambda event, **fields: recorded.append((event, fields))

    sink.record_chat_summary(
        user_id="user-1",
        session_id="thread-1",
        request_id="request-1",
        trace_id="trace-1",
        result=SimpleNamespace(status="SUCCESS", intent={}, drafts=[], draft=None),
        draft_context=None,
        duration_ms=12.0,
    )

    assert recorded[0][0] == "chat_completed"
    assert recorded[0][1]["trace_id"] == "trace-1"
