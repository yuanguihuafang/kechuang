import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from assistant.api.v1.execution.supervisor import build_supervisor_router
from assistant.api.v1.schemas import (
    WeComContactRequest,
)
from core.contracts.chat import ChatRequest
from services.application.chat_response import compose_business_response


def _contact_actions(actions):
    return [action for action in actions if action.get("type") == "CONTACT_WECOM"]


def test_match_results_return_one_consistent_contact_action_for_external_user():
    request = ChatRequest(
        message="工业机器人采购",
        user_id="user-1",
        user_role="external",
        intent_hint="supply_demand_match",
    )

    _, actions = compose_business_response(
        request,
        answer="已找到匹配结果。",
        status="SUCCESS",
        evidences=[
            {
                "record_id": "solution-1",
                "source_knowledge_type": "solution",
                "text": "工业机器人方案",
                "score": 0.9,
            }
        ],
        images=[],
        documents=[],
    )

    assert _contact_actions(actions) == [
        {
            "id": "contact_wecom",
            "type": "CONTACT_WECOM",
            "label": "联系客服",
            "enabled": True,
        }
    ]


@pytest.mark.parametrize("intent_hint", ["supply_demand_match", "demand_discovery", None])
def test_no_results_return_contact_action_only_for_external_user(intent_hint):
    request = ChatRequest(
        message="没有结果的查询",
        user_id="user-1",
        user_role="external",
        intent_hint=intent_hint,
    )

    blocks, actions = compose_business_response(
        request,
        answer="没有找到资料。",
        status="NO_EVIDENCE",
        evidences=[],
        images=[],
        documents=[],
    )

    assert _contact_actions(actions) == [
        {
            "id": "contact_wecom",
            "type": "CONTACT_WECOM",
            "label": "联系客服",
            "enabled": True,
        }
    ]
    if intent_hint is None:
        assert "人工对接" not in str(blocks)

    request.user_role = "internal"
    internal_blocks, internal_actions = compose_business_response(
        request,
        answer="没有找到资料。",
        status="NO_EVIDENCE",
        evidences=[],
        images=[],
        documents=[],
    )
    assert not _contact_actions(internal_actions)
    assert "联系客服" not in str(internal_blocks)
    assert "联系人工客服" not in str(internal_blocks)


def test_contact_endpoint_returns_action_without_task_store_for_external_user():
    router = build_supervisor_router(
        object(),
        lambda user_id: (user_id, "external", None),
    )
    endpoint = next(route.endpoint for route in router.routes if route.path == "/api/v1/me/contact")
    request = SimpleNamespace(state=SimpleNamespace(user_id="user-1"))

    result = asyncio.run(
        endpoint(
            WeComContactRequest(query="希望对接高端装备研发企业", document_ids=["doc-1"]),
            request,
        )
    )

    assert result["status"] == "CONTACT_REQUIRED"
    assert result["action"] == {
        "id": "contact_wecom",
        "type": "CONTACT_WECOM",
        "label": "联系客服",
        "enabled": True,
        "query": "希望对接高端装备研发企业",
        "document_ids": ["doc-1"],
    }
    assert "task_id" not in result


def test_contact_endpoint_rejects_internal_user():
    router = build_supervisor_router(
        object(),
        lambda user_id: (user_id, "internal", None),
    )
    endpoint = next(route.endpoint for route in router.routes if route.path == "/api/v1/me/contact")
    request = SimpleNamespace(state=SimpleNamespace(user_id="user-1"))

    with pytest.raises(HTTPException) as error:
        asyncio.run(endpoint(WeComContactRequest(query="联系人工"), request))

    assert error.value.status_code == 403
