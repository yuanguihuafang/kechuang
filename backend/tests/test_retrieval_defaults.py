from agents.retrieval_agent.schemas import RetrievalRequest
from core.contracts.flows import get_intent_flow
from assistant.api.v1.schemas import (
    SupervisorSearchRequest,
)
from core.governance.access_policy import ManualIdentity
from runtime.config import Settings


def test_retrieval_defaults_to_rerank_and_top_three():
    request = RetrievalRequest(
        query="测试检索",
        identity=ManualIdentity(user_role="internal", user_id="test-user"),
    )

    assert request.top_k == 3
    assert request.use_rerank is True
    assert SupervisorSearchRequest(query="测试检索").top_k == 3
    assert Settings().rerank_enabled is True


def test_business_flows_use_separate_libraries_and_match_without_solution():
    assert get_intent_flow("enterprise_profile").knowledge_types == ("enterprise",)
    assert get_intent_flow("demand_discovery").knowledge_types == ("demand",)
    assert get_intent_flow("expert_search").knowledge_types == ("expert",)
    assert get_intent_flow("supply_demand_match").knowledge_types == (
        "enterprise",
        "expert",
        "solution",
        "achievement",
    )
