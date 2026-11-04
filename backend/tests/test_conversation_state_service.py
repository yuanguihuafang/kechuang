from copy import deepcopy

from core.contracts.chat import ChatResponse
from core.contracts.conversation import ChatFlowState, state_for_response
from services.application.conversation_state import ConversationStateService


class SessionStore:
    def __init__(self, flow=None):
        self.state = {"chat_flow": flow or {}}

    def get(self, _thread_id):
        return {"state": deepcopy(self.state)}

    def upsert(self, _thread_id, *, state):
        self.state.update(deepcopy(state))


def test_conversation_state_records_result_without_request_or_model():
    store = SessionStore()
    service = ConversationStateService(store)
    result = ChatResponse(status="SUCCESS", answer="已完成检索")
    assert service.record_chat_result("thread-1", result) == "COMPLETED"
    assert store.state["chat_flow"]["last_answer"] == "已完成检索"
    assert not hasattr(service, "answer_question")
    assert not hasattr(service, "_graph")


def test_conversation_state_preserves_server_draft_identity():
    store = SessionStore(
        {
            "active_draft_id": "old-id",
            "active_draft": {"draft_id": "old-id", "status": "DRAFT"},
        }
    )
    service = ConversationStateService(store)
    result = ChatResponse(
        status="DRAFT_GENERATED",
        answer="草稿已更新",
        draft={"draft_id": "server-id", "draft_type": "demand", "fields": {"title": "机器人"}},
    )
    service.record_chat_result("thread-1", result)
    assert result.draft["draft_id"] == "server-id"
    assert store.state["chat_flow"]["active_draft_id"] == "server-id"


def test_conversation_state_indexes_batch_drafts_in_response_order():
    store = SessionStore()
    service = ConversationStateService(store)
    result = ChatResponse(
        status="DRAFT_GENERATED",
        answer="已生成两份草稿",
        drafts=[
            {"draft_id": "draft-1", "draft_type": "solution", "title": "业务接单"},
            {"draft_id": "draft-2", "draft_type": "solution", "title": "工艺知识库"},
        ],
    )

    service.record_chat_result("thread-1", result)

    assert [
        (item["draft_id"], item["ordinal"], item["title"])
        for item in store.state["chat_flow"]["draft_index"]
    ] == [("draft-1", 1, "业务接单"), ("draft-2", 2, "工艺知识库")]


def test_multi_draft_confirmation_maps_to_waiting_for_user_input():
    assert state_for_response("DRAFT_MULTI_CONFIRM_REQUIRED") == ChatFlowState.INFO_REQUIRED
