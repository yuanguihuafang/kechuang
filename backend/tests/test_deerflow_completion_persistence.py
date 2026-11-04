import asyncio
from types import SimpleNamespace

import pytest

from core.contracts.chat import ChatResponse
from assistant.application.chat_stream import is_natural_next_match_batch_request
from assistant.api.v1.execution.supervisor import build_supervisor_router
from assistant.api.v1.schemas import (
    ChatStreamRequest,
)


@pytest.mark.parametrize(
    "message",
    [
        "再找一批",
        "再给我几个类似的",
        "再找几家类似的企业",
        "找几个类似的",
        "再来一批类似的方案",
        "还有类似的吗",
        "还有其他需求方吗？",
        "请帮我再看看其他结果。",
    ],
)
def test_natural_next_batch_requires_an_explicit_request_and_saved_scope(message):
    flow = {
        "match_batch": {
            "current_query": "保存的检索主题",
            "shown_result_ids": ["enterprise:source:old"],
            "business_intent": "supply_demand_match",
            "direction": "demand_to_supply",
        }
    }

    assert is_natural_next_match_batch_request(message, flow)


@pytest.mark.parametrize(
    ("message", "flow"),
    [
        (
            "再找一批环保设备企业",
            {
                "match_batch": {
                    "current_query": "保存的检索主题",
                    "shown_result_ids": ["enterprise:source:old"],
                    "business_intent": "supply_demand_match",
                    "direction": "demand_to_supply",
                }
            },
        ),
        ("再找一批", {}),
        (
            "再找一批",
            {
                "match_batch": {
                    "current_query": "保存的检索主题",
                    "shown_result_ids": [],
                    "business_intent": "supply_demand_match",
                    "direction": "demand_to_supply",
                }
            },
        ),
    ],
)
def test_natural_next_batch_does_not_consume_new_query_or_missing_state(message, flow):
    assert not is_natural_next_match_batch_request(message, flow)


def test_deerflow_next_batch_does_not_persist_transient_session_marker(monkeypatch):
    calls = {"upserts": [], "runner": [], "search": [], "recorded": []}
    initial_state = {
        "chat_flow": {
            "match_direction": "demand_to_supply",
            "match_batch": {
                "original_query": "扫地机器人采购",
                "shown_result_ids": ["expert:source:first", "enterprise:source:second"],
                "direction": "demand_to_supply",
                "business_intent": "supply_demand_match",
                "knowledge_types": ["solution", "achievement"],
            },
        },
    }

    class SessionStore:
        def get(self, _thread_id):
            return {"user_id": "user-1", "state": initial_state}

        def upsert(self, *_args, **kwargs):
            calls["upserts"].append(kwargs.get("state"))

        def append_message(self, *_args, **_kwargs):
            pass

    class Supervisor:
        session_store = SessionStore()

        def record_chat_result(self, _thread_id, response, _request):
            calls["recorded"].append(response)
            return "MATCHED"

    class Runner:
        async def stream(self, upstream):
            calls["runner"].append(upstream)
            yield {
                "type": "end",
                "data": {
                    "result": {
                        "status": "SUCCESS",
                        "answer": "找到新结果",
                        "intent": {
                            "name": "supply_demand_match",
                            "query": "扫地机器人采购",
                            "entities": {"next_match_batch": True},
                        },
                        "match_results": [{"id": "solution:new", "knowledge_type": "solution"}],
                    }
                },
            }

    class Runtime:
        deerflow_runner = None
        tool_adapter = SimpleNamespace(
            search_knowledge=lambda arguments, *, context: _search(arguments, context)
        )
        match_result_store = SimpleNamespace(save=lambda **_kwargs: None)

        def require_conversation(self):
            return Supervisor()

    class AuditSink:
        def record_chat_summary(self, **_kwargs):
            pass

    async def _search(arguments, context):
        calls["search"].append((arguments, context))
        return {
            "status": "SUCCESS",
            "trace_id": context.trace_id,
            "data": {
                "evidences": [
                    {
                        "record_id": "solution:new",
                        "source_knowledge_type": "solution",
                        "text": "新方案",
                    }
                ]
            },
        }

    monkeypatch.setattr("assistant.application.chat_stream.PostgresAuditSink", AuditSink)
    router = build_supervisor_router(Runtime(), lambda user_id: (user_id, "external", None))
    route = next(route for route in router.routes if route.path == "/api/v1/chat/stream")
    request = SimpleNamespace(
        state=SimpleNamespace(user_id="user-1", trace_id="trace-next"),
        headers={},
    )

    async def consume():
        response = await route.endpoint(
            ChatStreamRequest(
                message="换一批匹配结果",
                thread_id="thread-1",
                intent_hint="supply_demand_match",
                intent_confirmation="confirmed",
                flow_action="next_match_batch",
            ),
            request,
        )
        return [chunk async for chunk in response.body_iterator]

    chunks = asyncio.run(consume())

    assert calls["runner"] == []
    arguments, _context = calls["search"][0]
    assert arguments["query"] == "扫地机器人采购"
    assert arguments["knowledge_types"] == ["solution", "achievement"]
    assert any(
        (item or {}).get("request_flow_action") == "next_match_batch"
        for item in calls["upserts"]
    )
    assert not any("pending_match_batch" in item for item in calls["upserts"] if item)
    intent = calls["recorded"][0].intent
    assert intent["query"] == "扫地机器人采购"
    assert intent["entities"]["next_match_batch"] is True
    assert any("event: done" in chunk for chunk in chunks)


def test_deerflow_completion_persists_match_and_restores_published_draft(monkeypatch):
    calls = {"messages": [], "flow": [], "matches": [], "audit": [], "runner": []}
    published_draft = {
        "draft_id": "draft-1",
        "draft_type": "demand",
        "status": "PUBLISHED",
        "match_direction": "demand_to_supply",
        "text": "需要工业视觉质检方案",
        "fields": {"industry": "制造业"},
    }

    class SessionStore:
        def get(self, _thread_id):
            return {
                "user_id": "user-1",
                "state": {
                    "chat_flow": {
                        "last_status": "PUBLISHED",
                        "active_draft_id": "draft-1",
                        "active_draft": published_draft,
                    }
                },
            }

        def upsert(self, *args, **kwargs):
            calls["flow"].append((args, kwargs))

        def append_message(self, *args, **kwargs):
            calls["messages"].append((args, kwargs))

    class Supervisor:
        session_store = SessionStore()

        def record_chat_result(self, thread_id, result, request):
            calls["flow"].append((thread_id, result, request))
            return "MATCHED"

    class Runner:
        async def stream(self, request):
            calls["runner"].append(request)
            yield {
                "type": "end",
                "data": {
                    "result": {
                        "status": "SUCCESS",
                        "answer": "找到 1 条匹配结果。",
                        "intent": {
                            "name": "supply_demand_match",
                            "business_intent": "supply_demand_match",
                            "query": "工业视觉质检方案",
                            "entities": {"match_direction": "demand_to_supply"},
                        },
                        "match_results": [{"id": "solution-1", "title": "视觉检测方案"}],
                        "processing_elapsed_ms": 15.5,
                    }
                },
            }

    class Runtime:
        deerflow_runner = Runner()
        match_result_store = SimpleNamespace(save=lambda **kwargs: calls["matches"].append(kwargs))

        def require_draft_store(self):
            return SimpleNamespace(get=lambda draft_id, user_id: {
                "draft_id": draft_id, "draft_type": "demand", "status": "PUBLISHED",
                "version": 1, "draft": published_draft,
            } if user_id == "user-1" else None)

        def require_conversation(self):
            return Supervisor()

    class AuditSink:
        def record_chat_summary(self, **kwargs):
            calls["audit"].append(kwargs)

    monkeypatch.setattr("assistant.application.chat_stream.PostgresAuditSink", AuditSink)
    router = build_supervisor_router(Runtime(), lambda user_id: (user_id, "external", None))
    route = next(route for route in router.routes if route.path == "/api/v1/chat/stream")
    request = SimpleNamespace(
        state=SimpleNamespace(user_id="user-1", trace_id="trace-1"),
        headers={},
    )

    async def consume():
        response = await route.endpoint(
            ChatStreamRequest(message="帮我匹配这个已发布需求", thread_id="thread-1"),
            request,
        )
        chunks = [chunk async for chunk in response.body_iterator]
        assert any("event: done" in chunk for chunk in chunks)

    asyncio.run(consume())

    upstream = calls["runner"][0]
    assert upstream["metadata"]["draft_context"]["draft_id"] == "draft-1"
    assert upstream["metadata"]["draft_context"]["match_direction"] == "demand_to_supply"
    assert upstream["metadata"]["draft_context"]["text"] == published_draft["text"]
    assert [item[1]["role"] for item in calls["messages"]] == ["user", "assistant"]
    recorded = [item for item in calls["flow"] if len(item) == 3]
    assert recorded[-1][2].draft_context["draft_id"] == "draft-1"
    assert calls["audit"][0]["result"].flow_state == "MATCHED"
    assert calls["matches"][0]["user_id"] == "user-1"
    assert calls["matches"][0]["session_id"] == "thread-1"
    assert calls["matches"][0]["draft_id"] == "draft-1"
    assert calls["audit"][0]["trace_id"] == "trace-1"
    assert isinstance(calls["audit"][0]["result"], ChatResponse)


def test_deerflow_next_turn_keeps_saved_match_scope_without_forcing_old_intent(monkeypatch):
    calls = {"runner": []}
    state = {
        "selected_match_direction": "supply_to_demand",
        "chat_flow": {
            "active_intent": "supply_demand_match",
            "match_direction": "demand_to_supply",
            "request_context": {
                "intent_hint": "supply_demand_match",
                "draft_context": {"match_direction": "demand_to_supply"},
            },
        }
    }

    class SessionStore:
        def get(self, _thread_id):
            return {"user_id": "user-1", "state": state}

        def upsert(self, *_args, **_kwargs):
            pass

        def append_message(self, *_args, **_kwargs):
            pass

    class Supervisor:
        session_store = SessionStore()

        def record_chat_result(self, *_args, **_kwargs):
            return "MATCHED"

    class Runner:
        async def stream(self, request):
            calls["runner"].append(request)
            yield {
                "type": "end",
                "data": {"result": {"status": "SUCCESS", "answer": "继续匹配。"}},
            }

    class Runtime:
        deerflow_runner = Runner()
        match_result_store = SimpleNamespace(save=lambda **_kwargs: None)

        def require_conversation(self):
            return Supervisor()

    class AuditSink:
        def record_chat_summary(self, **_kwargs):
            pass

    monkeypatch.setattr("assistant.application.chat_stream.PostgresAuditSink", AuditSink)
    router = build_supervisor_router(Runtime(), lambda user_id: (user_id, "external", None))
    route = next(route for route in router.routes if route.path == "/api/v1/chat/stream")
    request = SimpleNamespace(
        state=SimpleNamespace(user_id="user-1", trace_id="trace-direction"),
        headers={},
    )

    async def consume():
        response = await route.endpoint(
            ChatStreamRequest(
                message="继续匹配",
                thread_id="thread-1",
                draft_context={"match_direction": "demand_to_supply"},
            ),
            request,
        )
        return [chunk async for chunk in response.body_iterator]

    asyncio.run(consume())

    metadata = calls["runner"][0]["metadata"]
    assert "intent_hint" not in metadata
    assert "intent_confirmation" not in metadata
    assert metadata["draft_context"]["match_direction"] == "demand_to_supply"


@pytest.mark.parametrize(
    (
        "business_intent",
        "direction",
        "saved_types",
        "expected_types",
        "has_results",
        "message",
        "flow_action",
    ),
    [
        ("demand_discovery", "", ["demand"], ["demand"], True, "换一批匹配结果", "next_match_batch"),
        ("knowledge_search", "", ["expert"], ["expert"], True, "换一批匹配结果", "next_match_batch"),
        (
            "supply_demand_match",
            "demand_to_supply",
            ["expert"],
            ["expert"],
            True,
            "换一批匹配结果",
            "next_match_batch",
        ),
        ("supply_demand_match", "supply_to_demand", ["solution"], ["solution"], True, "换一批匹配结果", "next_match_batch"),
        ("knowledge_search", "", ["expert"], ["expert"], False, "换一批匹配结果", "next_match_batch"),
        ("demand_discovery", "", ["demand"], ["demand"], True, "再找一批", None),
        ("knowledge_search", "", ["expert"], ["expert"], True, "再给我几个类似的", None),
        (
            "supply_demand_match",
            "demand_to_supply",
            ["expert"],
            ["expert"],
            True,
            "再来一批类似的方案",
            None,
        ),
        (
            "supply_demand_match",
            "supply_to_demand",
            ["solution"],
            ["solution"],
            True,
            "还有其他需求方吗？",
            None,
        ),
    ],
)
def test_next_match_batch_executes_saved_scope_without_calling_deerflow(
    monkeypatch,
    business_intent,
    direction,
    saved_types,
    expected_types,
    has_results,
    message,
    flow_action,
):
    calls = {"search": [], "recorded": []}
    flow = {
        "active_intent": business_intent,
        "match_batch": {
            "current_query": "保存的检索主题",
            "shown_result_ids": ["expert:source:old"],
            "business_intent": business_intent,
            "direction": direction,
            "knowledge_type": saved_types[0] if len(saved_types) == 1 else None,
            "knowledge_types": saved_types,
        },
    }

    class SessionStore:
        def get(self, _thread_id):
            return {"user_id": "user-1", "state": {"chat_flow": flow}}

        def upsert(self, *_args, **_kwargs):
            pass

        def append_message(self, *_args, **_kwargs):
            pass

    class Supervisor:
        session_store = SessionStore()

        def record_chat_result(self, _thread_id, response, _request):
            calls["recorded"].append(response)
            return "MATCHED"

    class ToolAdapter:
        async def search_knowledge(self, arguments, *, context):
            calls["search"].append((arguments, context))
            return {
                "status": "SUCCESS",
                "trace_id": context.trace_id,
                "data": {
                    "query": arguments["query"],
                    "evidences": [
                        {
                            "record_id": "result-new",
                            "source_id": "source-new",
                            "source_knowledge_type": expected_types[0],
                            "text": "新的检索内容",
                            "score": 0.9,
                        }
                    ]
                    if has_results
                    else [],
                },
            }

    class Runtime:
        deerflow_runner = None
        conversation_service = SimpleNamespace(session_store=SessionStore())
        tool_adapter = ToolAdapter()
        match_result_store = SimpleNamespace(save=lambda **_kwargs: None)

        def require_conversation(self):
            return Supervisor()

    class AuditSink:
        def record_chat_summary(self, **_kwargs):
            pass

    monkeypatch.setattr("assistant.application.chat_stream.PostgresAuditSink", AuditSink)

    async def keep_test_candidates(result, **_kwargs):
        return result

    monkeypatch.setattr(
        "assistant.application.chat_stream.judge_and_fallback", keep_test_candidates
    )
    router = build_supervisor_router(Runtime(), lambda user_id: (user_id, "external", None))
    route = next(route for route in router.routes if route.path == "/api/v1/chat/stream")
    request = SimpleNamespace(
        state=SimpleNamespace(user_id="user-1", trace_id="trace-next"),
        headers={},
    )

    async def consume():
        response = await route.endpoint(
            ChatStreamRequest(
                message=message,
                thread_id="thread-1",
                intent_hint=business_intent,
                intent_confirmation="confirmed",
                flow_action=flow_action,
            ),
            request,
        )
        return [chunk async for chunk in response.body_iterator]

    chunks = asyncio.run(consume())

    assert len(calls["search"]) == 1
    arguments, context = calls["search"][0]
    assert arguments["query"] == "保存的检索主题"
    assert arguments["knowledge_types"] == expected_types
    assert arguments["top_k"] == 3
    assert arguments["exclude_result_ids"] == ["expert:source:old"]
    assert arguments["identity"] == {"user_id": "user-1", "user_role": "external"}
    assert context.thread_id == "thread-1"
    response = calls["recorded"][0]
    if has_results:
        assert response.match_results[0]["title"] == "匹配结果"
        assert any("event: done" in chunk and "result-new" in chunk for chunk in chunks)
    else:
        assert response.status == "NO_EVIDENCE"
        assert response.match_results == []
        assert "没有更多匹配结果" in response.answer
        assert "联网搜索" in response.answer
        assert "event: done" in chunks[-1]


def test_deerflow_completion_survives_match_and_audit_store_failures(monkeypatch):
    class Runner:
        async def stream(self, _request):
            yield {
                "type": "end",
                "data": {
                    "result": {
                        "status": "SUCCESS",
                        "answer": "仍返回已完成的回答。",
                        "intent": {"business_intent": "supply_demand_match"},
                        "match_results": [{"id": "solution-1"}],
                    }
                },
            }

    class Runtime:
        deerflow_runner = Runner()
        match_result_store = SimpleNamespace(
            save=lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("db down"))
        )

        def require_conversation(self):
            return SimpleNamespace(session_store=SimpleNamespace(
                get=lambda _: None, upsert=lambda *a, **kw: None, append_message=lambda *a, **kw: None),
                record_chat_result=lambda *_args: None)

    class AuditSink:
        def record_chat_summary(self, **_kwargs):
            raise RuntimeError("audit down")

    monkeypatch.setattr("assistant.application.chat_stream.PostgresAuditSink", AuditSink)
    router = build_supervisor_router(Runtime(), lambda user_id: (user_id, "external", None))
    route = next(route for route in router.routes if route.path == "/api/v1/chat/stream")
    request = SimpleNamespace(
        state=SimpleNamespace(user_id="user-1", trace_id="trace-2"),
        headers={},
    )

    async def consume():
        response = await route.endpoint(
            ChatStreamRequest(message="匹配", thread_id="thread-1"), request
        )
        chunks = [chunk async for chunk in response.body_iterator]
        done = next(chunk for chunk in chunks if "event: done" in chunk)
        assert "仍返回已完成的回答。" in done
        assert not any("event: error" in chunk for chunk in chunks)

    asyncio.run(consume())
