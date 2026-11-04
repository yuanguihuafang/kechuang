from agents.perception_agent.application.draft_extractor import compact_match_summary


from assistant.api.v1.schemas import (
    ChatStreamRequest,
)
from core.contracts.chat import ChatRequest, ChatResponse
from services.application.chat_response import (
    build_recommendation_items,
    match_result_identity,
    select_match_results,
)
from services.application.conversation_state import (
    ConversationStateService,
    _compact_retrieval_item,
)
from services.application.public_chat_result import _public_intent
from services.memory.memory import extract_memory_candidates


def test_match_card_deduplicates_original_and_draft_and_prefers_original():
    from services.application.chat_response import compact_evidences

    draft = {"record_id": "draft", "score": 0.99, "knowledge_type": "solution",
             "mysql_id": "24", "source_id": "shared-source", "chunk_role": "agent_draft",
             "chunk_index": 0, "text": "草稿"}
    original = {"record_id": "original", "score": 0.8, "knowledge_type": "solution",
                "mysql_id": None, "source_id": "shared-source", "document_id": "doc-1",
                "matched_draft_mysql_id": "24",
                "chunk_role": "agent_doc", "chunk_index": 2, "text": "原文"}
    other = {**draft, "record_id": "other", "mysql_id": "25", "score": 0.9}
    assert select_match_results([draft, other, original], limit=1) == [original]
    assert select_match_results([draft, other, original]) == [original, other]
    cards = build_recommendation_items([draft, original])
    assert len(cards) == 1
    assert cards[0]["id"] == "original"
    sibling_cards = build_recommendation_items([draft, other, original])
    assert len(sibling_cards) == 2
    assert {card["id"] for card in sibling_cards} == {"original", "other"}
    assert compact_evidences([draft, original])[0]["record_id"] == "original"
    other_type = {**original, "knowledge_type": "demand"}
    assert len(select_match_results([draft, original, other_type])) == 2


class SessionStore:
    def __init__(self):
        self.flows = {}

    def get(self, thread_id):
        return {"state": {"chat_flow": self.flows.get(thread_id, {})}}

    def upsert(self, thread_id, *, state):
        self.flows[thread_id] = state["chat_flow"]


def test_public_intent_keeps_generated_query_for_debug_panel():
    public = _public_intent({"name": "supply_demand_match", "query": "工业机器人视觉质检"})
    assert public["query"] == "工业机器人视觉质检"


def test_match_summary_is_compact_and_deduplicated():
    summary = compact_match_summary(
        "字段：名称：工业机器人\n字段：名称：工业机器人\n" + "字段：描述：" + "x" * 5000
    )
    assert summary.count("工业机器人") == 1
    assert len(summary) <= 3060


def test_match_summary_retains_existing_bounded_truncation_behavior():
    assert compact_match_summary("x" * 20, max_chars=10) == "x" * 10


def test_retrieval_context_is_bounded_for_session_state():
    compact = _compact_retrieval_item(
        {
            "title": "结果一",
            "text": "x" * 5000,
            "nested": {"summary": "y" * 5000},
            "items": list(range(20)),
        }
    )
    assert len(compact["text"]) == 2000
    assert len(compact["nested"]["summary"]) == 2000
    assert len(compact["items"]) == 10


def test_llm_match_direction_is_persisted_for_the_next_turn():
    flow = ConversationStateService(SessionStore())
    flow.session_store.flows = {}
    request = ChatRequest(message="根据资料找需求方", user_id="user-1")
    result = ChatResponse(
        status="SUCCESS",
        answer="已完成匹配",
        intent={
            "name": "supply_demand_match",
            "business_intent": "supply_demand_match",
            "entities": {"match_direction": "supply_to_demand"},
        },
        match_results=[{"id": "demand-1", "knowledge_type": "demand"}],
    )

    flow.record_chat_result("thread-1", result, request)

    state = flow._get_chat_flow_state("thread-1")
    assert state["match_direction"] == "supply_to_demand"
    assert state["request_context"]["draft_context"]["match_direction"] == "supply_to_demand"


def test_record_chat_result_accepts_deerflow_stream_request():
    flow = ConversationStateService(SessionStore())
    flow.session_store.flows = {}

    flow.record_chat_result(
        "thread-1",
        ChatResponse(status="SUCCESS", answer="已检索。"),
        ChatStreamRequest(
            message="查找相关技术方案",
            user_id="user-1",
            intent_hint="supply_demand_match",
            knowledge_type="solution",
        ),
    )

    state = flow._get_chat_flow_state("thread-1")
    assert state["last_user_message"] == "查找相关技术方案"
    assert state["request_context"]["intent_hint"] == "supply_demand_match"
    assert state["request_context"]["knowledge_type"] == "solution"


def test_empty_next_turn_does_not_overwrite_uploaded_material_context():
    flow = ConversationStateService(SessionStore())
    flow.session_store.flows = {
        "thread-1": {
            "request_context": {
                "draft_context": {
                    "text": "陈教授主要研究高光谱图像处理。",
                    "attachments": [{"document_id": "doc-1"}],
                },
            },
        },
    }
    result = ChatResponse(status="CONVERSATION_RESPONSE", answer="请说明您要找哪一方。")

    flow.record_chat_result(
        "thread-1",
        result,
        ChatRequest(message="找需求方", user_id="user-1"),
    )

    saved = flow._get_chat_flow_state("thread-1")["request_context"]["draft_context"]
    assert saved["text"] == "陈教授主要研究高光谱图像处理。"
    assert saved["attachments"] == [{"document_id": "doc-1"}]


def test_generated_draft_is_saved_once_as_active_draft():
    flow = ConversationStateService(SessionStore())
    flow.session_store.flows = {}
    draft = {
        "draft_id": "draft-1",
        "draft_type": "demand",
        "title": "智能化改造需求",
        "text": "需要进行生产线智能化改造",
        "fields": {"company": "青岛檬豆"},
    }
    result = ChatResponse(
        status="DRAFT_GENERATED",
        answer="草稿已生成",
        draft=draft,
        intent={"name": "demand", "operation": "draft_create"},
    )

    flow.record_chat_result(
        "thread-1",
        result,
        ChatRequest(message="生成草稿", user_id="user-1"),
    )

    state = flow._get_chat_flow_state("thread-1")
    assert state["active_draft"] == draft
    assert state["request_context"]["draft_context"]["draft_id"] == "draft-1"
    assert "fields" not in state["request_context"]["draft_context"]


def test_active_draft_keeps_one_copy_of_each_attachment():
    flow = ConversationStateService(SessionStore())
    flow.session_store.flows = {}
    attachment = {"document_id": "doc-1", "file_name": "资料.docx"}
    draft = {
        "draft_id": "draft-1",
        "draft_type": "demand",
        "title": "智能化改造需求",
        "attachments": [attachment, dict(attachment), dict(attachment)],
    }

    flow.record_chat_result(
        "thread-1",
        ChatResponse(status="DRAFT_GENERATED", answer="草稿已生成", draft=draft),
        ChatRequest(
            message="继续补充资料",
            user_id="user-1",
            draft_context={
                "attachments": [dict(attachment)],
            },
        ),
    )

    state = flow._get_chat_flow_state("thread-1")
    assert state["active_draft"]["attachments"] == [attachment]
    assert "attachments" not in state["request_context"]["draft_context"]


def test_active_draft_id_is_reused_when_next_response_omits_it():
    flow = ConversationStateService(SessionStore())
    flow.session_store.flows = {
        "thread-1": {
            "active_draft_id": "draft-1",
            "active_draft": {"draft_id": "draft-1", "draft_type": "demand"},
            "draft_registry": {
                "draft-1": {"draft_id": "draft-1", "draft_type": "demand"},
            },
        },
    }

    flow.record_chat_result(
        "thread-1",
        ChatResponse(
            status="DRAFT_GENERATED",
            answer="草稿已更新",
            draft={"draft_type": "demand"},
        ),
        ChatRequest(message="补充需求", user_id="user-1"),
    )

    state = flow._get_chat_flow_state("thread-1")
    assert state["active_draft_id"] == "draft-1"


def test_server_generated_draft_id_replaces_active_identity():
    flow = ConversationStateService(SessionStore())
    flow.session_store.flows = {
        "thread-1": {
            "last_status": "DRAFT_GENERATED",
            "active_draft_id": "draft-1",
            "active_draft": {"draft_id": "draft-1", "draft_type": "demand"},
            "draft_registry": {
                "draft-1": {"draft_id": "draft-1", "draft_type": "demand"},
            },
        },
    }

    flow.record_chat_result(
        "thread-1",
        ChatResponse(
            status="DRAFT_GENERATED",
            answer="草稿已更新",
            draft={"draft_id": "draft-generated-again", "draft_type": "demand"},
        ),
        ChatRequest(message="补充需求", user_id="user-1"),
    )

    state = flow._get_chat_flow_state("thread-1")
    assert state["active_draft_id"] == "draft-generated-again"
    assert state["active_draft"]["draft_id"] == "draft-generated-again"


def test_published_draft_gets_a_new_id_for_the_next_draft():
    flow = ConversationStateService(SessionStore())
    flow.session_store.flows = {
        "thread-1": {
            "last_status": "PUBLISHED",
            "active_draft_id": "draft-1",
            "active_draft": {"draft_id": "draft-1", "draft_type": "demand", "status": "PUBLISHED"},
            "draft_registry": {
                "draft-1": {"draft_id": "draft-1", "draft_type": "demand", "status": "PUBLISHED"},
            },
        },
    }

    flow.record_chat_result(
        "thread-1",
        ChatResponse(
            status="DRAFT_GENERATED",
            answer="新草稿已生成",
            draft={"draft_type": "demand"},
        ),
        ChatRequest(message="创建新的需求草稿", user_id="user-1"),
    )

    state = flow._get_chat_flow_state("thread-1")
    assert state["active_draft_id"] != "draft-1"


def test_match_results_are_limited_to_three_and_have_stable_identities():
    items = [
        {"id": f"result-{index}", "knowledge_type": "solution", "score": index}
        for index in range(5)
    ]
    selected = select_match_results(items)
    assert len(selected) == 3
    assert [match_result_identity(item) for item in selected] == [
        "solution:result-4",
        "solution:result-3",
        "solution:result-2",
    ]


def test_match_results_deduplicate_mysql_and_document_draft_for_same_business_record():
    mysql_result = {
        "id": "mysql:solution:24:0",
        "knowledge_type": "solution",
        "chunk_role": "mysql",
        "mysql_id": "24",
        "source_id": "document-24",
        "score": 0.95,
    }
    document_draft = {
        "id": "agent-draft:24:0",
        "knowledge_type": "solution",
        "chunk_role": "agent_draft",
        "mysql_id": "24",
        "source_id": "document-24",
        "score": 0.94,
    }

    selected = select_match_results([mysql_result, document_draft])

    assert selected == [mysql_result]
    assert match_result_identity(mysql_result) == match_result_identity(document_draft)
    assert len(build_recommendation_items([mysql_result, document_draft])) == 1


def test_next_match_batch_accumulates_shown_result_ids():
    first = ChatResponse(
        status="SUCCESS",
        answer="",
        match_results=[
            {"id": "a", "knowledge_type": "solution"},
            {"id": "b", "knowledge_type": "solution"},
            {"id": "c", "knowledge_type": "solution"},
        ],
        intent={
            "name": "supply_demand_match",
            "business_intent": "supply_demand_match",
            "query": "原始需求",
            "entities": {"match_direction": "demand_to_supply"},
        },
    )
    next_batch = ChatResponse(
        status="SUCCESS",
        answer="",
        match_results=[
            {"id": "d", "knowledge_type": "solution"},
            {"id": "e", "knowledge_type": "solution"},
            {"id": "f", "knowledge_type": "solution"},
        ],
        intent={
            "name": "supply_demand_match",
            "business_intent": "supply_demand_match",
            "query": "改写需求",
            "entities": {"next_match_batch": "true"},
        },
    )
    state = ConversationStateService._next_match_batch_state({}, first)
    state = ConversationStateService._next_match_batch_state({"match_batch": state}, next_batch)
    assert state["batch_index"] == 2
    assert len(state["shown_result_ids"]) == 6
    assert state["current_query"] == "原始需求"
    assert state["direction"] == "demand_to_supply"
    assert state["business_intent"] == "supply_demand_match"


def test_knowledge_search_batch_saves_scope_and_reuses_previous_query():
    first = ChatResponse(
        status="SUCCESS",
        answer="",
        match_results=[{"id": "expert:1", "knowledge_type": "expert"}],
        intent={"operation": "knowledge_search", "query": "语音识别专家"},
    )
    next_batch = ChatResponse(
        status="SUCCESS",
        answer="",
        match_results=[{"id": "expert:2", "knowledge_type": "expert"}],
        intent={
            "operation": "knowledge_search",
            "query": "重新生成的错误查询",
            "entities": {"next_match_batch": True},
        },
    )

    state = ConversationStateService._next_match_batch_state({}, first)
    state = ConversationStateService._next_match_batch_state({"match_batch": state}, next_batch)

    assert state["current_query"] == "语音识别专家"
    assert state["business_intent"] == "knowledge_search"
    assert state["knowledge_type"] == "expert"
    assert state["knowledge_types"] == ["expert"]
    assert state["shown_result_ids"] == ["expert:expert:1", "expert:expert:2"]


def test_match_batch_persists_llm_selected_scope_even_when_only_one_collection_hits():
    result = ChatResponse(
        status="SUCCESS",
        answer="找到企业记录",
        match_results=[{"id": "enterprise:1", "knowledge_type": "enterprise"}],
        intent={
            "operation": "supply_demand_match",
            "query": "上下游合作",
            "entities": {"knowledge_types": ["enterprise", "solution"]},
        },
    )

    state = ConversationStateService._next_match_batch_state({}, result)

    assert state["knowledge_types"] == ["enterprise", "solution"]


def test_knowledge_search_batch_indexes_displayed_evidences_when_match_results_are_empty():
    result = ChatResponse(
        status="SUCCESS",
        answer="找到专家结果",
        evidences=[
            {
                "record_id": "expert-1",
                "source_knowledge_type": "expert",
                "text": "语音识别专家",
            }
        ],
        intent={"operation": "knowledge_search", "query": "语音识别专家"},
    )

    state = ConversationStateService._next_match_batch_state({}, result)

    assert state["current_query"] == "语音识别专家"
    assert state["shown_result_ids"]
    from assistant.application.chat_stream import is_natural_next_match_batch_request

    assert is_natural_next_match_batch_request("换一批", {"match_batch": state})


def test_match_batch_records_visible_cards_when_match_results_are_not_populated():
    item = {
        "id": "agent-draft:24:0",
        "knowledge_type": "solution",
        "chunk_role": "agent_draft",
        "mysql_id": "24",
        "source_id": "document-24",
        "title": "工程塑料技术方案",
    }
    result = ChatResponse(
        status="SUCCESS",
        answer="已找到方案",
        ui_blocks=[{"type": "recommendation_list", "data": {"items": [item]}}],
        actions=[
            {"type": "NEXT_MATCH_BATCH", "intent_hint": "knowledge_search", "enabled": True}
        ],
        intent={"query": "工程塑料技术方案"},
    )

    state = ConversationStateService._next_match_batch_state(
        {}, result, request_message="工程塑料技术方案"
    )

    assert state["business_intent"] == "knowledge_search"
    assert state["shown_result_ids"] == ["solution:mysql:24"]


def test_knowledge_search_batch_preserves_explicit_requested_knowledge_type():
    result = ChatResponse(
        status="SUCCESS",
        answer="命中不同来源的结果",
        match_results=[{"id": "solution:1", "knowledge_type": "solution"}],
        intent={"operation": "knowledge_search", "query": "工业技术"},
    )

    state = ConversationStateService._next_match_batch_state(
        {},
        result,
        {"intent_hint": "knowledge_search", "knowledge_type": "expert"},
        "工业技术",
    )

    assert state["knowledge_type"] == "expert"
    assert state["knowledge_types"] == ["expert"]


def test_match_batch_saves_query_and_direction_from_request_when_model_omits_them():
    result = ChatResponse(
        status="SUCCESS",
        answer="找到匹配结果",
        match_results=[{"id": "solution-1", "knowledge_type": "solution"}],
        intent={"name": "supply_demand_match", "business_intent": "supply_demand_match"},
    )

    state = ConversationStateService._next_match_batch_state(
        {},
        result,
        {
            "intent_hint": "supply_demand_match",
            "draft_context": {"match_direction": "demand_to_supply"},
        },
        "采购扫地机器人",
    )

    assert state["original_query"] == "采购扫地机器人"
    assert state["direction"] == "demand_to_supply"


def test_match_batch_is_saved_from_the_display_action_when_model_intent_is_missing():
    result = ChatResponse(
        status="SUCCESS",
        answer="已找到候选",
        match_results=[{"id": "solution-1", "knowledge_type": "solution"}],
        actions=[
            {
                "type": "NEXT_MATCH_BATCH",
                "intent_hint": "supply_demand_match",
                "enabled": True,
            }
        ],
        intent={"query": "工业机器人上下料"},
    )

    state = ConversationStateService._next_match_batch_state(
        {}, result, request_message="工业机器人上下料"
    )

    assert state["business_intent"] == "supply_demand_match"
    assert state["current_query"] == "工业机器人上下料"
    assert state["shown_result_ids"] == ["solution:solution-1"]


def test_knowledge_search_batch_is_saved_from_its_display_action_without_model_intent():
    result = ChatResponse(
        status="SUCCESS",
        answer="已找到候选",
        match_results=[{"id": "solution-1", "knowledge_type": "solution"}],
        actions=[
            {
                "type": "NEXT_MATCH_BATCH",
                "intent_hint": "knowledge_search",
                "enabled": True,
            }
        ],
        intent={"query": "玻纤增强塑料方案库"},
    )

    state = ConversationStateService._next_match_batch_state(
        {}, result, request_message="查找玻纤增强塑料方案库"
    )

    assert state["business_intent"] == "knowledge_search"
    assert state["current_query"] == "玻纤增强塑料方案库"
    assert state["knowledge_types"] == ["solution"]
    assert state["shown_result_ids"] == ["solution:solution-1"]


def test_conversation_memory_requires_confirmation():
    candidate = extract_memory_candidates("以后优先推荐青岛企业")[0]
    assert candidate["confirmed"] is False
    assert candidate["status"] == "PENDING"
