from datetime import datetime, timezone
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from admin.api.v1.admin import build_admin_router
from admin.service import AdminService
from services.persistence.postgres import InMemoryDraftStore
from services.persistence.repositories.runtime_repository import PostgresSessionStore
from services.persistence.repositories import workflow_repository
from services.persistence.repositories.workflow_repository import PostgresDraftStore
from services.persistence.sqlalchemy import metadata


def test_session_store_reads_only_requested_page_and_returns_total():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE platform_drafts (thread_id TEXT, status TEXT)")
    store = PostgresSessionStore(engine=engine)
    for index in range(5):
        store.upsert(
            f"session-{index}",
            user_id=f"user-{index}",
            user_role="external",
            state={"title": f"会话 {index}"},
        )

    result = store.list_all_sessions_page(page=2, page_size=2)

    assert result["total"] == 5
    assert result["page"] == 2
    assert result["page_size"] == 2
    assert result["total_pages"] == 3
    assert len(result["items"]) == 2
    assert {item["session_id"] for item in result["items"]}.isdisjoint({"session-3", "session-4"})


def test_admin_conversations_only_enriches_current_page_sessions():
    class SessionStore:
        def list_all_sessions_page(self, *, page, page_size):
            assert (page, page_size) == (2, 20)
            return {
                "items": [{"session_id": "session-21", "user_id": None, "user_role": None}],
                "total": 41,
                "page": page,
                "page_size": page_size,
                "total_pages": 3,
            }

    class DraftStore:
        def owner_hints_for_threads(self, thread_ids):
            assert thread_ids == ["session-21"]
            return {"session-21": {"user_id": "admin-1", "user_role": "admin"}}

        def list_all(self, **_kwargs):
            raise AssertionError("session catalog must not scan all drafts")

    runtime = SimpleNamespace(
        task_store=None,
        sync_state_store=None,
        user_identity_reader=None,
        conversation_service=SimpleNamespace(session_store=SessionStore()),
    )
    runtime.require_draft_store = lambda: DraftStore()

    result = AdminService(runtime, session_store=runtime.conversation_service.session_store).conversations(page=2, page_size=20)

    assert (result["total"], result["page"], result["page_size"], result["total_pages"]) == (
        41,
        2,
        20,
        3,
    )
    assert result["items"][0]["user_id"] == "admin-1"
    assert result["items"][0]["user_name"] == "管理员"


def test_knowledge_updates_reads_filtered_database_page_not_a_global_list(monkeypatch):
    class DraftStore:
        def list_page(self, *, status, draft_types, page, page_size):
            assert (status, draft_types, page, page_size) == (None, ["demand"], 2, 20)
            return {
                "items": [
                    {
                        "draft_id": "draft-21",
                        "draft_type": "demand",
                        "status": "CONFIRMED",
                        "user_id": "user-1",
                        "thread_id": "session-1",
                        "draft": {
                            "title": "分页需求",
                            "fields": {},
                            "attachments": [],
                        },
                        "updated_at": "2026-09-26T10:00:00+00:00",
                    }
                ],
                "total": 21,
                "page": page,
                "page_size": page_size,
                "total_pages": 2,
            }

        def list_all(self, **_kwargs):
            raise AssertionError("knowledge update list must not load all drafts")

    monkeypatch.setattr("admin.governance.PostgresAuditSink.list", lambda *_args, **_kwargs: [])
    runtime = SimpleNamespace(task_store=None, sync_state_store=None, lifecycle_service=None)
    runtime.require_draft_store = lambda: DraftStore()

    result = AdminService(runtime).knowledge_updates(
        page=2,
        page_size=20,
        published_only=False,
        knowledge_type="demand",
    )

    assert (result["total"], result["page"], result["page_size"], result["total_pages"]) == (
        21,
        2,
        20,
        2,
    )
    assert [item["draft_id"] for item in result["items"]] == ["draft-21"]


def test_in_memory_draft_store_filters_before_paging():
    store = InMemoryDraftStore()
    for index in range(25):
        store.create(
            user_id=f"user-{index}",
            user_role="external",
            draft={"draft_type": "demand" if index < 21 else "solution", "title": str(index)},
        )

    result = store.list_page(status=None, draft_types=["demand"], page=2, page_size=20)

    assert (result["total"], result["page"], result["total_pages"]) == (21, 2, 2)
    assert len(result["items"]) == 1
    assert result["items"][0]["draft_type"] == "demand"


def test_admin_conversation_detail_draft_query_keeps_deleted_records():
    store = InMemoryDraftStore()
    draft = store.create(
        user_id="user-1",
        user_role="external",
        thread_id="session-1",
        draft={"draft_type": "demand", "title": "已删除草稿"},
    )
    store.logical_delete(draft["draft_id"], user_id="user-1")

    result = store.list_for_thread("session-1")

    assert [item["status"] for item in result] == ["DELETED"]


def test_postgres_draft_store_filters_and_limits_before_fetching(monkeypatch):
    now = datetime.now(timezone.utc)
    row = (
        "draft-21",
        "user-1",
        "external",
        "demand",
        "PUBLISHED",
        "session-1",
        None,
        "按页需求",
        {"title": "按页需求"},
        now,
        now,
    )

    class Result:
        def scalar_one(self):
            return 21

        def fetchall(self):
            return [row]

    class Connection:
        def __init__(self):
            self.calls = []

        def exec_driver_sql(self, query, params):
            self.calls.append((query, params))
            return Result()

    connection = Connection()

    class PooledConnection:
        def __enter__(self):
            return connection

        def __exit__(self, *_exc):
            return None

    monkeypatch.setattr(workflow_repository, "_pooled_connection", lambda _dsn: PooledConnection())

    result = PostgresDraftStore("postgresql://test").list_page(
        status="PUBLISHED",
        draft_types=["demand"],
        page=2,
        page_size=20,
    )

    assert (result["total"], result["page"], result["total_pages"]) == (21, 2, 2)
    assert result["items"][0]["draft_id"] == "draft-21"
    count_query, count_params = connection.calls[0]
    page_query, page_params = connection.calls[1]
    assert "WHERE status=%s AND draft_type IN (%s)" in count_query
    assert count_params == ("PUBLISHED", "demand")
    assert "ORDER BY updated_at DESC, draft_id DESC LIMIT %s OFFSET %s" in page_query
    assert page_params == ("PUBLISHED", "demand", 20, 20)


def test_admin_list_routes_use_page_contract_defaults_and_filters():
    calls = []

    def capture(name):
        return lambda **kwargs: (
            calls.append((name, kwargs))
            or {
                "items": [],
                "events": [],
                "total": 0,
                "page": kwargs.get("page", 1),
                "page_size": kwargs.get("page_size", 20),
                "total_pages": 0,
            }
        )

    service = SimpleNamespace(
        knowledge_updates=capture("knowledge_updates"),
        conversations=capture("conversations"),
        audit_events=capture("audit_events"),
    )
    app = FastAPI()
    app.include_router(build_admin_router(SimpleNamespace(require_admin=lambda: service)))

    with TestClient(app) as client:
        assert (
            client.get(
                "/api/v1/admin/knowledge-updates",
                params={
                    "published_only": "false",
                    "knowledge_type": "demand",
                },
            ).status_code
            == 200
        )
        assert (
            client.get(
                "/api/v1/admin/conversations",
                params={
                    "page": 2,
                    "page_size": 40,
                },
            ).status_code
            == 200
        )
        assert client.get("/api/v1/admin/audit-events").status_code == 200

    assert calls == [
        (
            "knowledge_updates",
            {
                "page": 1,
                "page_size": 20,
                "published_only": False,
                "knowledge_type": "demand",
            },
        ),
        ("conversations", {"page": 2, "page_size": 40}),
        (
            "audit_events",
            {
                "page": 1,
                "page_size": 20,
                "service": None,
                "user_id": None,
                "task_id": None,
                "document_id": None,
                "event_name": None,
                "trace_id": None,
            },
        ),
    ]
