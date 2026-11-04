import asyncio
from io import BytesIO
from types import SimpleNamespace

from fastapi import Request
from starlette.datastructures import UploadFile

import assistant.application.materials as chat_routes
from agents.perception_agent.schemas import PerceptionResult, UploadResponse
from assistant.api.v1.chat.routes import build_chat_router
from assistant.application.materials import _match_direction_from_text


class _Supervisor:
    session_store = None


class _Runtime:
    def require_conversation(self):
        return _Supervisor()


class _MaterialSupervisor:
    def __init__(self):
        self.session_store = _MaterialSessionStore()

    def record_chat_result(self, *_args, **_kwargs):
        return None


class _MaterialSessionStore:
    def __init__(self):
        self.sessions = {}

    def get(self, thread_id):
        return self.sessions.get(thread_id)

    def upsert(self, thread_id, *, user_id, user_role, state):
        self.sessions[thread_id] = {
            "user_id": user_id,
            "user_role": user_role,
            "state": state,
        }


class _MaterialRunner:
    async def stream(self, _request):
        yield {"type": "end", "data": {"result": {"answer": "ok"}}}


class _MaterialTaskStore:
    def __init__(self):
        self.created = None

    def create(self, **kwargs):
        self.created = kwargs
        return {"task_id": "task-1"}

    def update(self, *_args, **_kwargs):
        return None


class _MaterialWorkflow:
    def __init__(self):
        self.task_store = _MaterialTaskStore()


class _MaterialPerception:
    async def upload_document(self, **_kwargs):
        return UploadResponse(
            status="SUCCESS",
            document_id="doc-1",
            storage_key="uploads/doc-1/brief.txt",
            file_name="brief.txt",
            file_size=5,
            version_id="doc-1:v1",
        )

    async def parse_stored_document(self, **_kwargs):
        return PerceptionResult(status="SUCCESS", source_name="brief.txt",
                                raw_text="这是机器人柔性上下料技术方案，具备视觉定位和自动调度能力。可用于汽车制造生产线，支持多类型零件抓取，满足工业连续运行的要求。")


class _MaterialRuntime:
    deerflow_runner = _MaterialRunner()

    def __init__(self):
        self.embedded_perception = _MaterialPerception()
        self.workflow = _MaterialWorkflow()
        self.lifecycle_service = _MaterialLifecycle()
        self.supervisor = _MaterialSupervisor()

    def require_conversation(self):
        return self.supervisor

    def require_file_workflow(self):
        return self.workflow

    def require_lifecycle(self):
        return self.lifecycle_service


class _MaterialLifecycle:
    def get_document(self, _document_id, include_purged=False):
        return None

    def create_version(self, **fields):
        self.created = fields
        return fields

    def purge_document(self, _document_id):
        return True


def _endpoint(path):
    router = build_chat_router(
        _Runtime(),
        lambda user_id: (user_id or "user-1", "external", None),
    )
    return next(route.endpoint for route in router.routes if getattr(route, "path", "") == path)


def test_upload_intent_required_returns_the_session_id_for_follow_up_upload():
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/chat/materials/parse",
            "state": {"user_id": "user-1"},
        }
    )

    result = asyncio.run(
        _endpoint("/api/v1/chat/materials/prepare")(
            request,
            1,
            None,
            None,
        )
    )

    assert result["status"] == "UPLOAD_INTENT_REQUIRED"
    assert result["thread_id"].startswith("session-user-1-")


def test_material_task_keeps_upload_version_id_in_attachment_metadata(monkeypatch):
    runtime = _MaterialRuntime()
    monkeypatch.setattr(
        chat_routes,
        "PostgresAuditSink",
        lambda: SimpleNamespace(record_chat_summary=lambda **_kwargs: None),
    )

    async def completed_run(*_args, **_kwargs):
        yield (
            "done",
            {
                "result": {
                    "status": "SUCCESS",
                    "answer": "处理完成。",
                    "intent": {"name": "demand_discovery"},
                    "match_results": [],
                    "ui_blocks": [],
                }
            },
        )

    monkeypatch.setattr(chat_routes, "iter_public_deerflow_events", completed_run)
    async def facts(*_args, **_kwargs):
        return "机器人柔性上下料技术方案", None

    monkeypatch.setattr(chat_routes, "extract_material_facts", facts)
    router = build_chat_router(
        runtime,
        lambda user_id: (user_id or "user-1", "external", None),
    )
    endpoint = next(
        route.endpoint
        for route in router.routes
        if getattr(route, "path", "") == "/api/v1/chat/materials/parse"
    )
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/chat/materials/parse",
            "headers": [],
            "state": {"user_id": "user-1"},
        }
    )

    asyncio.run(
        endpoint(
            request=request,
            file=UploadFile(filename="brief.txt", file=BytesIO(b"brief")),
            thread_id="thread-1",
            message="process upload",
            intent_hint="demand_discovery",
            knowledge_type="",
            attachment_mode="new",
            debug=False,
        )
    )

    assert runtime.workflow.task_store.created["attachments"][0]["version_id"] == "doc-1:v1"
    assert runtime.supervisor.session_store.get("thread-1")["user_id"] == "user-1"


def test_upload_inherits_supply_to_demand_direction_from_previous_request():
    assert _match_direction_from_text("我有技术成果，帮我寻找潜在客户") == "supply_to_demand"
