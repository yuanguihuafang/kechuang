import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from core.contracts.tools import ToolContext
from core.contracts.workflows import LifecycleDecisionRequest
from services.application.drafts import DraftApplicationService
from services.persistence.postgres import InMemoryDraftStore
from contextlib import contextmanager


def test_postgres_transition_detects_concurrent_version_change(monkeypatch):
    from services.persistence.repositories import workflow_repository as repository
    store = object.__new__(repository.PostgresDraftStore)
    store.dsn = "postgresql://test"
    store.get = lambda *_a, **_kw: {"status": "CONFIRMED", "version": 2,
                                   "user_id": "owner", "user_role": "external", "draft": {}}
    statements = []

    class Connection:
        def exec_driver_sql(self, sql, parameters):
            statements.append((sql, parameters))
            return SimpleNamespace(rowcount=0)

    @contextmanager
    def connection(_dsn):
        yield Connection()

    monkeypatch.setattr(repository, "_pooled_connection", connection)
    with pytest.raises(ValueError, match="version mismatch"):
        store.transition("draft", user_id="owner", target_status="PENDING_REVIEW",
                         operator_id="owner", operator_role="external", expected_version=2)
    assert "_draft_version" in statements[0][0]


def test_application_transition_rejects_stale_version(monkeypatch):
    monkeypatch.setattr("services.application.drafts._audit_user_event", lambda *_a, **_kw: None)
    store = InMemoryDraftStore()
    context = ToolContext(user_id="owner", user_role="external", trace_id="trace")
    record = store.create(user_id="owner", user_role="external", thread_id=None,
                          draft={"draft_type": "demand", "title": "需求"})
    store.update(record["draft_id"], user_id="owner", draft={"title": "更新需求"})
    service = DraftApplicationService(SimpleNamespace(require_draft_store=lambda: store))
    with pytest.raises(HTTPException) as error:
        asyncio.run(service.transition(record["draft_id"], "PENDING_REVIEW", context=context,
                                       expected_version=record["version"]))
    assert error.value.status_code == 409
    assert "version mismatch" in error.value.detail
    assert store.get(record["draft_id"], user_id="owner")["status"] == "CONFIRMED"


def test_user_unpublish_cannot_target_payload_owner():
    store = InMemoryDraftStore()
    record = store.create(user_id="other", user_role="external", thread_id=None,
                          draft={"draft_type": "demand"})
    store.transition(record["draft_id"], user_id="other", target_status="PUBLISHED",
                     operator_id="other", operator_role="external")
    service = DraftApplicationService(SimpleNamespace(require_draft_store=lambda: store))
    context = ToolContext(user_id="owner", user_role="external", trace_id="trace")
    with pytest.raises(HTTPException) as error:
        asyncio.run(service.unpublish(record["draft_id"],
            LifecycleDecisionRequest(user_id="other", reason_code="OUTDATED"), context=context))
    assert error.value.status_code == 404
    assert store.get(record["draft_id"], user_id="other")["status"] == "PUBLISHED"


def test_user_delete_records_timestamp_and_auditable_draft_context(monkeypatch):
    events = []
    monkeypatch.setattr(
        "services.application.drafts._audit_user_event",
        lambda event, **fields: events.append((event, fields)),
    )
    store = InMemoryDraftStore()
    record = store.create(
        user_id="owner",
        user_role="external",
        draft={
            "draft_type": "solution",
            "title": "方案标题",
            "attachments": [{"document_id": "doc-1", "file_name": "资料.pdf"}],
        },
    )
    result = asyncio.run(
        DraftApplicationService(SimpleNamespace(require_draft_store=lambda: store)).delete(
            record["draft_id"],
            context=ToolContext(user_id="owner", user_role="external", trace_id="trace"),
        )
    )

    assert result["status"] == "DELETED"
    assert result["draft"]["draft"]["deleted_at"]
    assert store.list(user_id="owner") == []
    event, fields = events[0]
    assert event == "user_draft_deleted"
    assert fields["draft_title"] == "方案标题"
    assert fields["attachment_names"] == ["资料.pdf"]
    assert fields["document_ids"] == ["doc-1"]


def test_user_cannot_read_deleted_draft_by_id():
    store = InMemoryDraftStore()
    record = store.create(user_id="owner", user_role="external", draft={"draft_type": "solution"})
    store.logical_delete(record["draft_id"], user_id="owner")
    service = DraftApplicationService(SimpleNamespace(require_draft_store=lambda: store))

    with pytest.raises(HTTPException) as error:
        asyncio.run(
            service.get(
                record["draft_id"],
                context=ToolContext(user_id="owner", user_role="external", trace_id="trace"),
            )
        )

    assert error.value.status_code == 404


def test_unpublish_marks_pinned_attachment_version_unsearchable_with_reason(monkeypatch):
    monkeypatch.setattr("services.application.drafts._audit_user_event", lambda *_a, **_kw: None)
    store = InMemoryDraftStore()
    record = store.create(
        user_id="owner",
        user_role="external",
        draft={
            "draft_type": "solution",
            "title": "方案",
            "mysql_record_id": "mysql-1",
            "attachments": [{"document_id": "doc-1", "version_id": "doc-1:v2"}],
        },
    )
    store.transition(
        record["draft_id"], user_id="owner", target_status="PUBLISHED",
        operator_id="owner", operator_role="external",
    )
    calls = []

    class Result:
        status = "SUCCESS"
        indexed_count = 1

    class Retrieval:
        async def unpublish_document(self, document_id, request):
            calls.append(("unpublish", document_id, request))
            return Result()

        async def republish_document(self, document_id, request):
            calls.append(("republish", document_id, request))
            return Result()

    class Lifecycle:
        def get_version(self, version_id):
            return {"version_id": version_id, "document_id": "doc-1", "version": 2,
                    "status": "INDEXED", "publication_status": "PUBLISHED"}

        def update_version(self, version_id, **fields):
            calls.append(("version", version_id, fields))
            return {"version_id": version_id}

    context = ToolContext(user_id="owner", user_role="external", trace_id="trace")
    service = DraftApplicationService(SimpleNamespace(
        require_draft_store=lambda: store,
        embedded_retrieval=SimpleNamespace(agent=Retrieval()),
        lifecycle_service=Lifecycle(),
    ))
    result = asyncio.run(service.unpublish(
        record["draft_id"],
        LifecycleDecisionRequest(user_id="owner", reason_code="OUTDATED", reason_text="方案过期"),
        context=context,
    ))

    assert result["draft"]["status"] == "WITHDRAWN"
    source_call = next(call for call in calls if call[0] == "unpublish" and call[1] == "doc-1")
    assert source_call[2].version_id == "doc-1:v2"
    version_call = next(call for call in calls if call[0] == "version")
    assert version_call[1] == "doc-1:v2"
    assert version_call[2]["publication_status"] == "UNPUBLISHED"
    assert version_call[2]["unpublish_reason_text"] == "方案过期"
    assert result["draft"]["draft"]["status_note"] == "OUTDATED:方案过期"


def test_republish_restores_pinned_attachment_version_and_clears_reason(monkeypatch):
    monkeypatch.setattr("services.application.drafts._audit_user_event", lambda *_a, **_kw: None)
    store = InMemoryDraftStore()
    record = store.create(
        user_id="owner",
        user_role="external",
        draft={
            "draft_type": "solution",
            "title": "方案",
            "mysql_record_id": "mysql-1",
            "attachments": [{"document_id": "doc-1", "version_id": "doc-1:v2"}],
        },
    )
    store.transition(
        record["draft_id"], user_id="owner", target_status="PUBLISHED",
        operator_id="owner", operator_role="external",
    )
    store.transition(
        record["draft_id"], user_id="owner", target_status="WITHDRAWN",
        operator_id="owner", operator_role="external", note="OUTDATED:方案过期",
    )
    calls = []

    class Result:
        status = "SUCCESS"
        indexed_count = 1

        def model_dump(self):
            return {"status": self.status, "indexed_count": self.indexed_count}

    class Retrieval:
        async def republish_document(self, document_id, request):
            calls.append(("republish", document_id, request))
            return Result()

    class Lifecycle:
        def get_version(self, version_id):
            return {"version_id": version_id, "document_id": "doc-1", "version": 2,
                    "status": "UNPUBLISHED", "publication_status": "UNPUBLISHED"}

        def update_version(self, version_id, **fields):
            calls.append(("version", version_id, fields))
            return {"version_id": version_id}

    context = ToolContext(user_id="owner", user_role="external", trace_id="trace")
    service = DraftApplicationService(SimpleNamespace(
        require_draft_store=lambda: store,
        embedded_retrieval=SimpleNamespace(agent=Retrieval()),
        lifecycle_service=Lifecycle(),
    ))
    result = asyncio.run(service.republish(
        record["draft_id"], LifecycleDecisionRequest(user_id="owner"), context=context,
    ))

    assert result["draft"]["status"] == "PUBLISHED"
    assert result["draft"]["draft"].get("status_note") is None
    source_call = next(call for call in calls if call[0] == "republish" and call[1] == "doc-1")
    assert source_call[2].version_id == "doc-1:v2"
    version_call = next(call for call in calls if call[0] == "version")
    assert version_call[2]["publication_status"] == "PUBLISHED"
    assert version_call[2]["unpublish_reason_code"] is None


def test_expired_draft_read_has_no_storage_deletion(monkeypatch):
    monkeypatch.setattr("services.storage.minio.minio_storage.MinioUploadService",
                        lambda: pytest.fail("read must not delete shared source objects"))
    store = InMemoryDraftStore()
    record = store.create(user_id="owner", user_role="external", thread_id=None, draft={
        "draft_type": "demand", "attachments": [{"storage_key": "uploads/shared.pdf",
        "temporary": True, "expires_at": "2020-01-01T00:00:00+00:00"}]})
    service = DraftApplicationService(SimpleNamespace(require_draft_store=lambda: store))
    with pytest.raises(HTTPException) as error:
        asyncio.run(service.get(record["draft_id"], context=ToolContext(
            user_id="owner", user_role="external", trace_id="trace")))
    assert error.value.status_code == 410


def test_save_of_published_draft_preserves_attachment_and_association_metadata(monkeypatch):
    monkeypatch.setattr("services.application.drafts._audit_user_event", lambda *_a, **_kw: None)
    store = InMemoryDraftStore()
    context = ToolContext(user_id="owner", user_role="external", trace_id="trace")
    record = store.create(
        user_id="owner",
        user_role="external",
        thread_id="thread-1",
        draft={
            "draft_type": "solution",
            "title": "旧标题",
            "fields": {"summary": "旧摘要", "tech_type": "材料技术"},
            "attachments": [{"document_id": "doc-1", "storage_key": "uploads/doc-1.pdf"}],
            "source_blocks": [{"text": "原文块"}],
            "mysql_record_id": "mysql-1",
            "source_id": "source:legacy-attachment-hash",
        },
    )
    published = store.transition(
        record["draft_id"],
        user_id="owner",
        target_status="PUBLISHED",
        operator_id="owner",
        operator_role="external",
    )
    service = DraftApplicationService(SimpleNamespace(require_draft_store=lambda: store))

    saved = asyncio.run(
        service.save(
            record["draft_id"],
            {"fields": {"summary": "新摘要"}},
            context=context,
            expected_version=published["version"],
        )
    )

    assert saved["status"] == "CONFIRMED"
    assert saved["draft"]["fields"] == {"summary": "新摘要", "tech_type": "材料技术"}
    assert saved["draft"]["attachments"] == record["draft"]["attachments"]
    assert saved["draft"]["source_blocks"] == record["draft"]["source_blocks"]
    assert saved["draft"]["mysql_record_id"] == "mysql-1"
    assert saved["draft"]["source_id"] == "source:legacy-attachment-hash"


def test_clients_cannot_supply_internal_index_snapshots(monkeypatch):
    monkeypatch.setattr("services.application.drafts._audit_user_event", lambda *_a, **_kw: None)
    store = InMemoryDraftStore()
    context = ToolContext(user_id="owner", user_role="external", trace_id="trace")
    service = DraftApplicationService(SimpleNamespace(require_draft_store=lambda: store))
    created = asyncio.run(service.create({
        "draft_type": "solution", "fields": {"contect": "方案正文"},
        "_index_snapshot": {"request": {"identity": {"user_role": "admin"}}},
    }, context=context))
    assert "_index_snapshot" not in created["draft"]
    saved = asyncio.run(service.save(created["draft_id"], {
        "_index_snapshot": {"request": {"identity": {"user_role": "admin"}}},
    }, context=context))
    assert "_index_snapshot" not in saved["draft"]
