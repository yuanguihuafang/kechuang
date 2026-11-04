import asyncio
import inspect
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from services.application.public_chat_result import PUBLIC_CHAT_TIMEOUT_MESSAGE
from assistant.api.v1.chat.routes import build_chat_router
from assistant.api.v1.chat.threads import build_threads_router
from assistant.api.v1.documents.catalog import build_documents_router
from assistant.api.v1.documents.lifecycle import build_lifecycle_router
from assistant.api.v1.drafts.publish import build_publish_router
from assistant.api.v1.drafts.routes import build_draft_router
from assistant.api.v1.execution.supervisor import _require_stream_identity, build_supervisor_router
from assistant.api.v1.identity.dependencies import require_request_user_id
from assistant.api.v1.identity.routes import build_identity_router
from assistant.api.v1.schemas import (
    ChatSessionRenameRequest,
    ChatStreamRequest,
    DraftCreateRequest,
    RetrievalChunkDetailRequest,
    WeComContactRequest,
)
from core.contracts.workflows import (
    DraftPublishRequest,
    LifecycleDecisionRequest,
)


def test_stream_request_allows_guest_payload_to_reach_auth_guard():
    request = ChatStreamRequest(message="联网搜索工业机器人最新进展")

    assert request.user_id is None


def test_stream_guest_gets_structured_auth_required_error():
    request = SimpleNamespace(state=SimpleNamespace())

    with pytest.raises(HTTPException) as error:
        _require_stream_identity(request, lambda _user_id: None)

    assert error.value.status_code == 401
    assert error.value.detail == {
        "code": "AUTH_REQUIRED",
        "message": "请先注册并登录后使用智能助手",
        "details": {"login_required": True},
    }


def test_stream_uses_bearer_user_id_for_identity_resolution():
    request = SimpleNamespace(state=SimpleNamespace(user_id="token-user"))
    captured: list[str] = []

    def resolve(user_id: str):
        captured.append(user_id)
        return user_id, "external", None

    assert _require_stream_identity(request, resolve) == (
        "token-user",
        "external",
        None,
    )
    assert captured == ["token-user"]


def test_user_facing_request_models_do_not_require_user_id():
    assert ChatSessionRenameRequest(title="标题").user_id is None
    assert DraftCreateRequest(draft={}).user_id is None
    assert DraftPublishRequest().user_id is None
    assert LifecycleDecisionRequest().user_id is None
    assert WeComContactRequest(query="联系我").user_id is None
    assert (
        RetrievalChunkDetailRequest(
            document_id="doc-1",
            chunk_role="content",
            chunk_index=0,
        ).user_id
        is None
    )
    assert RetrievalChunkDetailRequest(
        document_id="doc-1",
        chunk_role="content",
        chunk_index=0,
    ).include_summary is True


def test_chunk_detail_can_skip_llm_summary_for_image_prefetch():
    detail = {"image_urls": ["/api/v1/perception/images/doc-1/image-1"]}

    class FakeSearchService:
        async def get_chunk_detail(self, **_kwargs):
            assert _kwargs["matched_draft_record_id"] == "draft-best"
            return detail

        async def summarize_chunk_detail(self, *_args):
            raise AssertionError("image prefetch must not call the LLM summarizer")

    runtime = SimpleNamespace(
        embedded_retrieval=SimpleNamespace(
            agent=SimpleNamespace(search_service=FakeSearchService())
        )
    )
    router = build_supervisor_router(runtime, lambda user_id: (user_id, "external", None))
    route = next(route for route in router.routes if route.path == "/api/v1/knowledge/chunk-detail")
    request = SimpleNamespace(state=SimpleNamespace(user_id="user-1"))

    result = asyncio.run(
        route.endpoint(
            RetrievalChunkDetailRequest(
                document_id="doc-1",
                chunk_role="content",
                chunk_index=0,
                include_summary=False,
                matched_draft_record_id="draft-best",
            ),
            request,
        )
    )

    assert result == detail


def test_chunk_detail_keeps_source_when_optional_draft_selection_fails():
    detail = {"text": "original", "draft": "", "draft_chunks": [],
              "draft_selection_error": "未能确定关联草稿，仅返回原文详情"}

    class FakeSearchService:
        async def get_chunk_detail(self, **kwargs):
            return detail

    runtime = SimpleNamespace(embedded_retrieval=SimpleNamespace(
        agent=SimpleNamespace(search_service=FakeSearchService())
    ))
    router = build_supervisor_router(runtime, lambda user_id: (user_id, "external", None))
    route = next(route for route in router.routes if route.path == "/api/v1/knowledge/chunk-detail")
    result = asyncio.run(route.endpoint(
        RetrievalChunkDetailRequest(document_id="doc-1", chunk_role="agent_doc", chunk_index=0,
                                    include_summary=False),
        SimpleNamespace(state=SimpleNamespace(user_id="user-1")),
    ))
    assert result == detail


def test_request_user_id_comes_only_from_authenticated_request_state():
    request = SimpleNamespace(state=SimpleNamespace(user_id="token-user"))
    assert require_request_user_id(request) == "token-user"

    guest = SimpleNamespace(state=SimpleNamespace())
    with pytest.raises(HTTPException) as error:
        require_request_user_id(guest)
    assert error.value.status_code == 401


def test_user_facing_routes_do_not_expose_user_id_parameter():
    routers = [
        build_identity_router(object()),
        build_threads_router(object()),
        build_draft_router(object(), lambda user_id: (user_id, "external", None)),
        build_publish_router(object(), lambda user_id: (user_id, "external", None)),
        build_documents_router(object(), object()),
        build_lifecycle_router(object(), lambda *_args, **_kwargs: None),
        build_chat_router(object(), lambda user_id: (user_id, "external", None)),
        build_supervisor_router(object(), lambda user_id: (user_id, "external", None)),
    ]
    routes = [
        route
        for router in routers
        for route in router.routes
        if route.path
        in {
            "/api/v1/auth/me",
            "/api/v1/me/profile",
            "/api/v1/me/conversations",
            "/api/v1/chat/sessions/{thread_id}/messages",
            "/api/v1/chat/sessions/{thread_id}",
            "/api/v1/me/drafts",
            "/api/v1/drafts",
            "/api/v1/drafts/{draft_id}",
            "/api/v1/drafts/{draft_id}/publish",
            "/api/v1/chat/materials/prepare",
            "/api/v1/chat/materials/parse",
            "/api/v1/chat/materials/parse/stream",
            "/api/v1/me/contact",
            "/api/v1/knowledge/chunk-detail",
            "/api/v1/me/documents",
            "/api/v1/me/documents/{document_id}",
            "/api/v1/knowledge/documents/{document_id}/file",
            "/api/v1/knowledge/documents/{document_id}",
            "/api/v1/me/documents/{document_id}/publish",
            "/api/v1/me/documents/{document_id}/unpublish",
            "/api/v1/chat/stream",
        }
    ]
    assert routes
    assert all("user_id" not in inspect.signature(route.endpoint).parameters for route in routes)


def test_stream_is_the_only_registered_chat_endpoint():
    router = build_supervisor_router(object(), lambda user_id: (user_id, "external", None))
    paths = {route.path for route in router.routes}

    assert "/api/v1/chat/stream" in paths
    assert "/api/v1/chat" not in paths
    assert "/api/v1/chat/async" not in paths
    assert "/api/v1/chat/tasks/{task_id}" not in paths


def test_stream_leaves_no_background_tasks_when_deerflow_finishes():
    class FakeSupervisor:
        session_store = SimpleNamespace(get=lambda _: None, upsert=lambda *a, **kw: None,
                                        append_message=lambda *a, **kw: None)

        def record_chat_result(self, *_args, **_kwargs):
            return None

    supervisor = FakeSupervisor()

    class FakeRunner:
        async def stream(self, _request):
            yield {"type": "end", "data": {"result": {"answer": "ok"}}}

    class FakeRuntime:
        match_result_store = None
        deerflow_runner = FakeRunner()

        def require_conversation(self):
            return supervisor

    router = build_supervisor_router(FakeRuntime(), lambda user_id: (user_id, "external", None))
    route = next(route for route in router.routes if route.path == "/api/v1/chat/stream")
    request = SimpleNamespace(
        state=SimpleNamespace(user_id="user-1", trace_id="trace-1"),
        headers={},
    )

    async def consume():
        response = await route.endpoint(ChatStreamRequest(message="测试"), request)
        chunks = [chunk async for chunk in response.body_iterator]

        assert any("event: done" in chunk for chunk in chunks)
        assert not [
            task
            for task in asyncio.all_tasks()
            if task is not asyncio.current_task() and not task.done()
        ]

    asyncio.run(consume())


def test_deerflow_stream_rejects_foreign_thread_before_remote_run():
    calls: list[dict[str, object]] = []

    class SessionStore:
        def get(self, _thread_id):
            return {"user_id": "another-user"}

        def upsert(self, *_args, **_kwargs):
            calls.append({"operation": "upsert"})

    class FakeSupervisor:
        session_store = SessionStore()

    class FakeRunner:
        async def stream(self, _request):
            calls.append({"operation": "remote"})
            if False:
                yield {}

    class FakeRuntime:
        deerflow_runner = FakeRunner()

        def require_conversation(self):
            return FakeSupervisor()

    router = build_supervisor_router(FakeRuntime(), lambda user_id: (user_id, "external", None))
    route = next(route for route in router.routes if route.path == "/api/v1/chat/stream")
    request = SimpleNamespace(
        state=SimpleNamespace(user_id="token-user", trace_id="trace-1"),
        headers={},
    )

    async def consume():
        response = await route.endpoint(
            ChatStreamRequest(message="测试", thread_id="foreign-thread"),
            request,
        )
        chunks = [chunk async for chunk in response.body_iterator]

        assert any("event: error" in chunk for chunk in chunks)
        assert not any(call["operation"] == "remote" for call in calls)
        assert not any(call["operation"] == "upsert" for call in calls)

    asyncio.run(consume())


def test_deerflow_stream_persists_compatible_session_messages():
    messages: list[dict[str, object]] = []

    class SessionStore:
        def get(self, _thread_id):
            return None

        def upsert(self, *args, **kwargs):
            messages.append({"operation": "upsert", "args": args, "kwargs": kwargs})

        def append_message(self, *args, **kwargs):
            messages.append({"operation": "message", "args": args, "kwargs": kwargs})

    class FakeSupervisor:
        session_store = SessionStore()

    class FakeRunner:
        async def stream(self, _request):
            yield {"type": "messages-tuple", "data": {"type": "ai", "content": "回答"}}
            yield {"type": "end", "data": {"result": {"answer": "回答"}}}

    class FakeRuntime:
        deerflow_runner = FakeRunner()

        def require_conversation(self):
            return FakeSupervisor()

    router = build_supervisor_router(FakeRuntime(), lambda user_id: (user_id, "external", None))
    route = next(route for route in router.routes if route.path == "/api/v1/chat/stream")
    request = SimpleNamespace(
        state=SimpleNamespace(user_id="token-user", trace_id="trace-1"),
        headers={},
    )

    async def consume():
        response = await route.endpoint(
            ChatStreamRequest(message="测试", thread_id="thread-1"),
            request,
        )
        chunks = [chunk async for chunk in response.body_iterator]

        assert any("event: done" in chunk for chunk in chunks)
        persisted = [item for item in messages if item["operation"] == "message"]
        assert [item["kwargs"]["role"] for item in persisted] == ["user", "assistant"]
        assert persisted[0]["kwargs"]["content"] == "测试"
        assert persisted[1]["kwargs"]["content"] == "回答"

    asyncio.run(consume())


def test_deerflow_stream_preserves_public_timeout_error_semantics():
    class FakeRunner:
        async def stream(self, _request):
            raise TimeoutError("internal timeout details")
            yield {}

    class FakeRuntime:
        deerflow_runner = FakeRunner()

        def require_conversation(self):
            return SimpleNamespace(session_store=SimpleNamespace(
                get=lambda _: None, upsert=lambda *a, **kw: None, append_message=lambda *a, **kw: None))

    router = build_supervisor_router(FakeRuntime(), lambda user_id: (user_id, "external", None))
    route = next(route for route in router.routes if route.path == "/api/v1/chat/stream")
    request = SimpleNamespace(
        state=SimpleNamespace(user_id="user-1", trace_id="trace-1"),
        headers={},
    )

    async def consume():
        response = await route.endpoint(ChatStreamRequest(message="问题"), request)
        chunks = [chunk async for chunk in response.body_iterator]

        error = next(chunk for chunk in chunks if "event: error" in chunk)
        assert PUBLIC_CHAT_TIMEOUT_MESSAGE in error
        assert "internal timeout details" not in error

    asyncio.run(consume())
