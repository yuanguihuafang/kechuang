import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException

from services.application.chat_response import compose_business_response
from core.contracts.chat import ChatRequest, ChatResponse
from assistant.api.v1.execution.supervisor import build_supervisor_router
from assistant.api.v1.schemas import (
    ChatStreamRequest,
)
from assistant.integrations.deerflow_gateway import DeerFlowGatewayRunner


class _SessionStore:
    def get(self, _thread_id):
        return None

    def upsert(self, *_args, **_kwargs):
        pass

    def append_message(self, *_args, **_kwargs):
        pass


def test_gateway_pins_thread_and_run_to_kechuang_even_if_request_tries_to_override():
    requests: list[tuple[str, dict[str, object]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.url.path, json.loads(request.read())))
        if request.url.path == "/api/threads":
            return httpx.Response(200, json={"thread_id": "thread-1"})
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=b"event: end\ndata: null\n\n",
        )

    runner = DeerFlowGatewayRunner(
        "http://deerflow.test",
        internal_auth_token="internal-token",
        agent_owner_user_id="fixed-opie-user",
        internal_token_header="X-DeerFlow-Internal-Token",
        owner_user_id_header="X-DeerFlow-Owner-User-Id",
        transport=httpx.MockTransport(handler),
    )

    async def collect():
        return [
            event
            async for event in runner.stream(
                {
                    "thread_id": "thread-1",
                    "message": "test",
                    "assistant_id": "another-agent",
                }
            )
        ]

    asyncio.run(collect())

    assert requests[0][1]["assistant_id"] == "kechuang"
    assert requests[1][1]["assistant_id"] == "kechuang"


def test_deerflow_failure_does_not_fall_back_to_supervisor(monkeypatch):
    class FailingRunner:
        async def stream(self, _request):
            raise RuntimeError("remote gateway failed")
            yield {}

    class FakeSupervisor:
        session_store = _SessionStore()
        answer_calls = 0

        async def answer_question(self, *_args, **_kwargs):
            self.answer_calls += 1
            return ChatResponse(status="SUCCESS", answer="legacy")

    supervisor = FakeSupervisor()
    runtime = SimpleNamespace(
        deerflow_runner=FailingRunner(),
        require_conversation=lambda: supervisor,
    )
    monkeypatch.setattr(
        "assistant.application.chat_stream.PostgresAuditSink",
        lambda: SimpleNamespace(record_chat_summary=lambda **_kwargs: None),
    )
    route = next(
        route
        for route in build_supervisor_router(
            runtime,
            lambda user_id: (user_id, "external", None),
        ).routes
        if route.path == "/api/v1/chat/stream"
    )
    request = SimpleNamespace(
        state=SimpleNamespace(user_id="user-1", trace_id="trace-1"),
        headers={},
    )

    async def consume():
        response = await route.endpoint(ChatStreamRequest(message="question"), request)
        return [chunk async for chunk in response.body_iterator]

    chunks = asyncio.run(consume())

    assert any("event: error" in chunk for chunk in chunks)
    assert not any("event: done" in chunk for chunk in chunks)
    assert supervisor.answer_calls == 0


def test_business_response_does_not_default_unknown_match_direction_to_demand():
    request = ChatRequest(
        message="匹配结果",
        user_id="user-1",
        user_role="internal",
        intent_hint="supply_demand_match",
    )
    blocks, actions = compose_business_response(
        request=request,
        answer="已找到匹配结果",
        status="SUCCESS",
        evidences=[
            {
                "record_id": "match-1",
                "title": "匹配项",
                "text": "内容",
                "score": 0.9,
                "knowledge_type": "solution",
            }
        ],
        images=[],
        documents=[],
        match_results=[
            {
                "record_id": "match-1",
                "title": "匹配项",
                "summary": "内容",
                "score": 0.9,
                "knowledge_type": "solution",
            }
        ],
    )

    assert not any(
        action.get("type") == "MATCH_RESULT_CONFIRM" and action.get("value") == "start_draft"
        for action in actions
    )
    assert not any(
        block.get("type") == "match_follow_up"
        and "需求草稿" in block.get("data", {}).get("text", "")
        for block in blocks
    )


def test_business_response_offers_both_match_directions_when_direction_is_unknown():
    request = ChatRequest(
        message="开始供需匹配",
        user_id="user-1",
        user_role="external",
        intent_hint="supply_demand_match",
    )

    _, actions = compose_business_response(
        request=request,
        answer="请先选择供需匹配方向。",
        status="SUCCESS",
        evidences=[],
        images=[],
        documents=[],
        match_results=[],
    )

    assert [
        (action["type"], action["value"])
        for action in actions
        if action["type"] == "SELECT_MATCH_DIRECTION"
    ] == [
        ("SELECT_MATCH_DIRECTION", "demand_to_supply"),
        ("SELECT_MATCH_DIRECTION", "supply_to_demand"),
    ]

    selected_request = request.model_copy(
        update={"draft_context": {"match_direction": "demand_to_supply"}},
    )
    _, selected_actions = compose_business_response(
        request=selected_request,
        answer="已按您选择的方向继续。",
        status="SUCCESS",
        evidences=[],
        images=[],
        documents=[],
        match_results=[],
    )
    assert not any(action["type"] == "SELECT_MATCH_DIRECTION" for action in selected_actions)


def test_deerflow_gateway_error_event_becomes_public_error_not_success(monkeypatch):
    class ErrorRunner:
        async def stream(self, _request):
            yield {"type": "error", "data": {"message": "private provider details"}}
            yield {"type": "end", "data": None}

    class FakeSupervisor:
        session_store = _SessionStore()

        async def answer_question(self, *_args, **_kwargs):
            raise AssertionError("configured DeerFlow must remain selected")

    runtime = SimpleNamespace(deerflow_runner=ErrorRunner(), require_conversation=FakeSupervisor)
    monkeypatch.setattr(
        "assistant.application.chat_stream.PostgresAuditSink",
        lambda: SimpleNamespace(record_chat_summary=lambda **_kwargs: None),
    )
    route = next(
        route
        for route in build_supervisor_router(
            runtime,
            lambda user_id: (user_id, "internal", None),
        ).routes
        if route.path == "/api/v1/chat/stream"
    )
    request = SimpleNamespace(
        state=SimpleNamespace(user_id="user-1", trace_id="trace-1"),
        headers={},
    )

    async def consume():
        response = await route.endpoint(ChatStreamRequest(message="question"), request)
        return [chunk async for chunk in response.body_iterator]

    chunks = asyncio.run(consume())

    assert any("event: error" in chunk for chunk in chunks)
    assert not any("event: done" in chunk for chunk in chunks)
    assert any("问答处理失败，请稍后重试。" in chunk for chunk in chunks)
    assert not any("private provider details" in chunk for chunk in chunks)


def test_business_response_uses_confirmed_direction_for_supplement_action():
    request = ChatRequest(
        message="匹配结果",
        user_id="user-1",
        user_role="internal",
        intent_hint="supply_demand_match",
        draft_context={"match_direction": "demand_to_supply"},
    )
    _, actions = compose_business_response(
        request=request,
        answer="已找到匹配结果",
        status="SUCCESS",
        evidences=[
            {
                "record_id": "match-1",
                "title": "匹配项",
                "text": "内容",
                "score": 0.9,
                "knowledge_type": "solution",
            }
        ],
        images=[],
        documents=[],
        match_results=[
            {
                "record_id": "match-1",
                "title": "匹配项",
                "summary": "内容",
                "score": 0.9,
                "knowledge_type": "solution",
            }
        ],
    )

    supplement = next(
        action
        for action in actions
        if action.get("type") == "MATCH_RESULT_CONFIRM" and action.get("value") == "start_draft"
    )
    assert supplement["draft_type"] == "demand"


def test_unconfigured_deerflow_rejects_chat_without_legacy_calls(monkeypatch):
    class FakeSupervisor:
        session_store = None
        answer_calls = 0

        async def answer_question(self, *_args, **_kwargs):
            self.answer_calls += 1
            return ChatResponse(status="SUCCESS", answer="legacy")

        def record_chat_result(self, *_args, **_kwargs):
            return None

    supervisor = FakeSupervisor()
    runtime = SimpleNamespace(deerflow_runner=None, require_conversation=lambda: supervisor)
    monkeypatch.setattr(
        "assistant.application.chat_stream.PostgresAuditSink",
        lambda: SimpleNamespace(record_chat_summary=lambda **_kwargs: None),
    )
    route = next(
        route
        for route in build_supervisor_router(
            runtime,
            lambda user_id: (user_id, "external", None),
        ).routes
        if route.path == "/api/v1/chat/stream"
    )
    request = SimpleNamespace(
        state=SimpleNamespace(user_id="user-1", trace_id="trace-1"),
        headers={},
    )

    async def consume():
        response = await route.endpoint(ChatStreamRequest(message="question"), request)
        return [chunk async for chunk in response.body_iterator]

    with pytest.raises(HTTPException) as error:
        asyncio.run(consume())
    assert error.value.status_code == 503
    assert supervisor.answer_calls == 0
