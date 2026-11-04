from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from admin.api.v1.admin import build_admin_router
from admin.governance import AdminGovernanceMixin
from services.persistence.repositories.observability_repository import PostgresAuditSink


def test_audit_api_accepts_optional_bounded_trace_id():
    calls = []
    service = SimpleNamespace(
        audit_events=lambda **kwargs: calls.append(kwargs) or {"events": [], "total": 0}
    )
    runtime = SimpleNamespace(require_admin=lambda: service)
    app = FastAPI()
    app.include_router(build_admin_router(runtime))
    client = TestClient(app)

    assert (
        client.get("/api/v1/admin/audit-events", params={"trace_id": "trace-1"}).status_code == 200
    )
    assert calls[-1]["trace_id"] == "trace-1"
    assert client.get("/api/v1/admin/audit-events").status_code == 200
    assert calls[-1]["trace_id"] is None
    assert (
        client.get("/api/v1/admin/audit-events", params={"trace_id": "x" * 129}).status_code == 422
    )


def test_audit_service_filters_list_and_total_without_changing_pagination(monkeypatch):
    calls = []

    class Sink:
        def count(self, **kwargs):
            calls.append(("count", kwargs))
            return 9

        def list(self, **kwargs):
            calls.append(("list", kwargs))
            return [{"trace_id": "trace-1"}]

    monkeypatch.setattr("admin.governance.PostgresAuditSink", Sink)
    service = AdminGovernanceMixin()
    result = service.audit_events(page=2, page_size=4, service="supervisor", trace_id="trace-1")

    assert result == {
        "events": [{"trace_id": "trace-1"}],
        "total": 9,
        "page": 2,
        "page_size": 4,
        "total_pages": 3,
    }
    assert [name for name, _ in calls] == ["count", "list"]
    assert all(kwargs["trace_id_filter"] == "trace-1" for _, kwargs in calls)
    assert calls[1][1]["limit"] == 4 and calls[1][1]["offset"] == 4
    assert all(kwargs["service"] == "supervisor" for _, kwargs in calls)


def test_audit_repository_filters_list_and_count_by_exact_trace_id():
    statements = []

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def execute(self, statement):
            statements.append(statement)
            return self

        def mappings(self):
            return self

        def all(self):
            return []

        def scalar_one(self):
            return 0

    sink = PostgresAuditSink(engine=SimpleNamespace(connect=lambda: Connection()))
    sink.list(user_only=None, trace_id_filter="trace-1")
    sink.count(user_only=None, trace_id_filter="trace-1")
    for statement in statements:
        compiled = statement.compile()
        assert "platform_audit_events.trace_id =" in str(compiled)
        assert "trace-1" in compiled.params.values()

    statements.clear()
    sink.list(user_only=None)
    sink.count(user_only=None)
    assert all("platform_audit_events.trace_id =" not in str(statement) for statement in statements)
