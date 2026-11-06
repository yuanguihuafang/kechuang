import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

from admin.service import AdminService
from core.contracts.workflows import FileConfirmRequest, UnifiedDeleteRequest


@pytest.fixture(autouse=True)
def _isolate_database_url(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)


@pytest.mark.parametrize("embedded_preview", [False, True])
def test_admin_preview_delegates_source_validation_even_for_embedded_results(monkeypatch, embedded_preview):
    task = {"task_id": "old-task", "document_id": "doc-1", "version_id": "doc-1:v1", "storage_key": "old.pdf"}
    if embedded_preview:
        task["parsed_result"] = {"document_id": "doc-1", "storage_key": "current.pdf", "raw_text": "new version text"}
    before = deepcopy(task)
    calls = []
    service = AdminService.__new__(AdminService)
    service.runtime = SimpleNamespace()

    async def load(_):
        pytest.fail("admin must delegate validation before loading any preview")

    service.parse_result = load

    class LifecycleService:
        def __init__(self, runtime):
            assert runtime is service.runtime

        async def with_parse_result(self, value, *, load_parse_result):
            calls.append((value, load_parse_result))
            return {**{key: item for key, item in value.items() if key != "parsed_result"}, "parse_result_error_code": "STALE_SOURCE"}

    monkeypatch.setattr("admin.documents.DocumentLifecycleApplicationService", LifecycleService)
    result = asyncio.run(service.with_parse_result(task))

    assert calls == [(task, load)]
    assert result["parse_result_error_code"] == "STALE_SOURCE"
    assert "parsed_result" not in result
    assert task == before


@pytest.mark.parametrize("source", ["current", "old_task", "wrong_task_key", "wrong_parse_key", "wrong_parse_document", "wrong_parse_version", "embedded_wrong_key"])
def test_http_task_preview_never_mixes_current_text_into_stale_source(source):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    from admin.api.v1.admin import build_admin_router
    from services.persistence.document_lifecycle import DocumentLifecycleStore
    from services.persistence.sqlalchemy import metadata

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    metadata.create_all(engine)
    lifecycle = DocumentLifecycleStore(engine=engine)
    for number, key in ((1, "old.pdf"), (2, "current.pdf")):
        lifecycle.create_version(
            document_id="doc-1", source_name="source.pdf", storage_key=key,
            scope_id="public", user_id="admin", version_number=number,
            knowledge_type="solution", status="PUBLISHED", publication_status="PUBLISHED",
        )
    task = {
        "task_id": "task-1", "status": "SUCCEEDED", "document_id": "doc-1",
        "version_id": "doc-1:v2", "storage_key": "current.pdf",
        "scope_id": "public", "user_id": "admin", "knowledge_type": "solution",
    }
    parsed = {
        "document_id": "doc-1", "version_id": "doc-1:v2", "storage_key": "current.pdf",
        "raw_text": "current version text", "blocks": [{"text": "current version text"}],
        "metadata": {"images": [{"url": "https://example.test/hydrated-image"}]},
    }
    if source == "old_task":
        task.update(version_id="doc-1:v1", storage_key="old.pdf")
    elif source == "wrong_task_key":
        task["storage_key"] = "wrong.pdf"
    elif source in {"wrong_parse_key", "embedded_wrong_key"}:
        parsed["storage_key"] = "wrong.pdf"
    elif source == "wrong_parse_document":
        parsed["document_id"] = "doc-other"
    elif source == "wrong_parse_version":
        parsed["version_id"] = "doc-1:v1"
    if source == "embedded_wrong_key":
        task["parsed_result"] = deepcopy(parsed)
    before = deepcopy(task)
    loads = []

    async def load(document_id):
        loads.append(document_id)
        return deepcopy(parsed)

    runtime = SimpleNamespace(
        require_lifecycle=lambda: lifecycle, lifecycle_service=lifecycle,
        require_file_workflow=lambda: SimpleNamespace(status=lambda _: deepcopy(task)),
    )
    service = AdminService.__new__(AdminService)
    service.runtime = runtime
    service.parse_result = load
    runtime.require_admin = lambda: service
    app = FastAPI()
    app.include_router(build_admin_router(runtime))
    with TestClient(app) as client:
        response = client.get("/api/v1/admin/tasks/task-1")

    assert response.status_code == 200, response.text
    result = response.json()
    assert result["status"] == "SUCCEEDED"
    assert task == before
    if source == "current":
        assert result["parsed_result"] == parsed
        assert loads == ["doc-1"]
    else:
        assert result["parsed_result"] is None
        assert result["error_code"] == "STALE_SOURCE"
        if source in {"old_task", "wrong_task_key"}:
            assert loads == []


@pytest.mark.parametrize("operation", ["confirm", "extract"])
@pytest.mark.parametrize("invalid_source", ["old_version", "wrong_key", "missing_version", "purged", "recycled"])
def test_stale_task_is_rejected_before_any_admin_mutation(operation, invalid_source):
    from runtime.workflows.file_workflow import FileWorkflowService

    task = {
        "task_id": "old-task", "status": "SUCCEEDED", "document_id": "doc-1",
        "version_id": "doc-1:v1", "storage_key": "old.pdf", "user_id": "admin",
        "knowledge_type": "solution", "draft_ids": ["published-draft"],
    }
    before = deepcopy(task)
    version = {**task, "status": "PUBLISHED", "_is_current": True}
    if invalid_source == "old_version":
        version["_is_current"] = False
    elif invalid_source == "wrong_key":
        version["storage_key"] = "current.pdf"
    elif invalid_source == "missing_version":
        version = None
    elif invalid_source == "purged":
        version["status"] = "PURGED"
    elif invalid_source == "recycled":
        version["status"] = "RECYCLED"
    workflow = object.__new__(FileWorkflowService)
    workflow.lifecycle_store = SimpleNamespace(get_version=lambda _: version)
    workflow.task_store = SimpleNamespace(
        get=lambda _: deepcopy(task),
        update=lambda *_, **__: pytest.fail("stale task must not be mutated"),
    )
    service = AdminService.__new__(AdminService)
    service.runtime = SimpleNamespace(require_file_workflow=lambda: workflow)

    if operation == "confirm":
        result = asyncio.run(service.confirm_task(
            "old-task", FileConfirmRequest(drafts=[{"draft_type": "solution"}]),
        ))
    else:
        result = asyncio.run(service.extract_fields_task("old-task"))

    assert result["error_code"] == "STALE_SOURCE"
    assert task == before


@pytest.mark.parametrize("status", ["UPLOADED", "PARSING", "RECYCLED", "PURGED", "FAILED"])
def test_confirm_payload_cannot_bypass_task_stage(status):
    task = {"task_id": "task-1", "status": status, "draft_ids": ["draft-1"]}
    workflow = SimpleNamespace(
        status=lambda _: dict(task), validate_task_source=lambda _: None,
        task_store=SimpleNamespace(update=lambda *_, **__: pytest.fail("must not update")),
    )
    service = AdminService.__new__(AdminService)
    service.runtime = SimpleNamespace(require_file_workflow=lambda: workflow)

    result = asyncio.run(service.confirm_task(
        "task-1", FileConfirmRequest(drafts=[{"draft_type": "solution"}]),
    ))

    assert result["error_code"] == "INVALID_STAGE"
    assert task["status"] == status


def test_confirm_cannot_publish_while_field_extraction_is_running():
    task = {"task_id": "task-1", "status": "WAITING_DRAFT_CONFIRM", "draft_status": "RUNNING", "draft_ids": ["old-draft"]}
    workflow = SimpleNamespace(status=lambda _: dict(task), validate_task_source=lambda _: None)
    service = AdminService.__new__(AdminService)
    service.runtime = SimpleNamespace(require_file_workflow=lambda: workflow)

    result = asyncio.run(service.confirm_task("task-1", FileConfirmRequest(drafts=[{"draft_type": "solution"}])))

    assert result["error_code"] == "INVALID_STAGE"
    assert task["draft_status"] == "RUNNING"


def test_direct_delete_rejects_missing_active_version_when_live_versions_remain():
    lifecycle = SimpleNamespace(
        get_document=lambda *_, **__: {"document_id": "doc-1"},
        list_versions=lambda _: [{"version_id": "doc-1:v2", "status": "PUBLISHED"}],
    )
    service = AdminService.__new__(AdminService)
    service.runtime = SimpleNamespace(require_lifecycle=lambda: lifecycle)

    with pytest.raises(ValueError, match="活动版本"):
        asyncio.run(service.delete_document("doc-1", UnifiedDeleteRequest(storage_key="old.pdf", knowledge_type="solution")))


def test_admin_recovery_preserves_direct_document_delete_mode():
    from admin.api.v1.schemas import SupervisorRecoveryRequest

    task = {"task_id": "purge-1", "operation": "document_purge", "lifecycle_action": "purge", "version_id": "doc-1:v2", "compensation": {"direct_delete": True}}
    calls = []
    service = AdminService.__new__(AdminService)
    service.runtime = SimpleNamespace(task_store=SimpleNamespace(get=lambda _: task))

    async def action(*args, **kwargs):
        calls.append((args, kwargs))
        return {"status": "PURGED"}, {**task, "status": "SUCCEEDED"}

    service.lifecycle_action = action
    result = asyncio.run(service.recover_task("purge-1", SupervisorRecoveryRequest(operator_id="admin")))

    assert calls == [(("purge", "doc-1:v2", "admin"), {"task": task, "direct_delete": True})]
    assert result["status"] == "PURGED"
    assert result["task_id"] == "purge-1"


def test_admin_delete_uses_current_version_lifecycle_not_adapter(monkeypatch):
    calls = []
    task = {"task_id": "purge-1", "status": "SUCCEEDED"}
    version = {"document_id": "doc-1", "version_id": "doc-1:v2", "status": "PUBLISHED"}

    class LifecycleService:
        def __init__(self, runtime):
            pass

        async def run_action(self, *args, **kwargs):
            calls.append((args, kwargs))
            return {**version, "status": "PURGED"}, task

    monkeypatch.setattr("admin.documents.DocumentLifecycleApplicationService", LifecycleService)
    lifecycle = SimpleNamespace(
        get_document=lambda *_, **__: {"document_id": "doc-1", "active_version_id": "doc-1:v2"},
        get_version=lambda version_id: version if version_id == "doc-1:v2" else None,
    )
    service = AdminService.__new__(AdminService)
    service.runtime = SimpleNamespace(require_lifecycle=lambda: lifecycle)

    result = asyncio.run(service.delete_document("doc-1", UnifiedDeleteRequest(
        storage_key="untrusted-old.pdf", knowledge_type="solution", user_id="admin",
    )))

    assert result["status"] == "DELETED"
    assert result["task_id"] == "purge-1"
    assert calls[0][0][:3] == ("purge", "doc-1:v2", "admin")
    assert calls[0][1] == {"user_role": "admin", "direct_delete": True}


@pytest.mark.parametrize("new_source_id", ["source:old-task", "source:new-task"])
def test_successful_confirmation_replay_preserves_new_confirmed_draft_and_business_data(monkeypatch, new_source_id):
    from runtime.workflows.file_workflow import FileWorkflowService
    from services.persistence.postgres import InMemoryDraftStore

    drafts = InMemoryDraftStore()
    published = drafts.create(user_id="admin", user_role="admin", draft={
        "draft_type": "solution", "source_id": "source:old-task", "mysql_record_id": "old",
        "fields": {"title": "published"},
        "attachments": [{"document_id": "doc-1", "version_id": "doc-1:v1", "storage_key": "current.pdf"}],
    })
    published = drafts.get(published["draft_id"], user_id="admin")
    published["status"] = "PUBLISHED"
    new = drafts.create(user_id="admin", user_role="admin", draft={
        "draft_type": "solution", "source_id": new_source_id, "mysql_record_id": "new",
        "fields": {"title": "new confirmed"},
        "attachments": [{"document_id": "doc-1", "version_id": "doc-1:v1", "storage_key": "current.pdf"}],
    })
    task = {
        "task_id": "old-task", "status": "SUCCEEDED", "document_id": "doc-1",
        "version_id": "doc-1:v1", "storage_key": "current.pdf", "knowledge_type": "solution",
        "user_id": "admin", "draft_ids": [published["draft_id"]],
    }
    validator = object.__new__(FileWorkflowService)
    validator.lifecycle_store = SimpleNamespace(get_version=lambda _: {**task, "status": "PUBLISHED", "_is_current": True})

    def update(_task_id, **fields):
        task.update(fields)
        return dict(task)

    mysql = {"old": {"title": "published"}, "new": {"title": "new confirmed"}}
    vectors = {"old": ["old-vector"], "new": ["new-vector"]}
    before = deepcopy((published, new, mysql, vectors))
    workflow = SimpleNamespace(
        status=lambda _: dict(task), validate_task_source=validator.validate_task_source,
        task_store=SimpleNamespace(update=update),
        cleanup_previous_version=lambda _: asyncio.sleep(0, result={}),
    )
    service = AdminService.__new__(AdminService)
    service.runtime = SimpleNamespace(
        require_file_workflow=lambda: workflow, require_draft_store=lambda: drafts,
        require_business_record_sink=lambda: SimpleNamespace(
            find_by_id=lambda **kwargs: mysql.get(kwargs["record_id"]),
            delete=lambda **kwargs: mysql.pop(kwargs["record_id"]),
        ),
        embedded_retrieval=SimpleNamespace(agent=SimpleNamespace(public_milvus={
            "solution": SimpleNamespace(delete_by_mysql_id=lambda mysql_id: vectors.pop(mysql_id)),
        })),
    )

    class Publisher:
        def __init__(self, *_):
            pass

        async def publish(self, *_):
            pytest.fail("successful replay must not republish unchanged draft")

    monkeypatch.setattr("admin.documents.DraftPublishingApplicationService", Publisher)
    result = asyncio.run(service.confirm_task("old-task", FileConfirmRequest(drafts=[deepcopy(published["draft"])])))

    assert result["status"] == "SUCCEEDED"
    assert result["indexed_count"] == 0
    assert drafts.get(new["draft_id"], user_id="admin")["status"] == "CONFIRMED"
    assert (drafts.get(published["draft_id"], user_id="admin"), drafts.get(new["draft_id"], user_id="admin"), mysql, vectors) == before
    assert result["replacement_cleanup"]["source_group_replacement"] == {"drafts": 0, "mysql_records": 0, "milvus_records": 0}


@pytest.mark.parametrize("catalog_exists", [False, True])
def test_direct_delete_keeps_missing_version_orphan_purge_idempotent(catalog_exists):
    catalog = {"document_id": "doc-orphan"} if catalog_exists else None
    files = {"orphan.pdf": "bytes"}
    vectors = {"doc-orphan": ["vector"]}

    def purge_document(_document_id):
        catalog["status"] = "PURGED"
        return dict(catalog)

    async def find(_):
        return [{"storage_key": key} for key in files]

    async def delete(storage_key, document_id):
        files.pop(storage_key)

    lifecycle = SimpleNamespace(
        get_document=lambda *_, **__: catalog, get_version=lambda _: None,
        list_versions=lambda _: [], purge_document=purge_document,
    )
    service = AdminService.__new__(AdminService)
    service.runtime = SimpleNamespace(
        require_lifecycle=lambda: lifecycle,
        require_draft_store=lambda: SimpleNamespace(list_for_document=lambda _: []),
        embedded_retrieval=SimpleNamespace(agent=SimpleNamespace(public_milvus={
            "solution": SimpleNamespace(delete_by_document_id=lambda doc: vectors.pop(doc, None)),
        })),
        storage_service=SimpleNamespace(find_by_document_id=find, delete_document=delete),
    )
    payload = UnifiedDeleteRequest(storage_key="ignored.pdf", knowledge_type="solution")

    first = asyncio.run(service.delete_document("doc-orphan", payload))
    second = asyncio.run(service.delete_document("doc-orphan", payload))

    assert first["status"] == second["status"] == "DELETED"
    assert first["orphaned"] is second["orphaned"] is True
    assert first["deleted_file_count"] == 1
    assert second["deleted_file_count"] == 0
    assert files == vectors == {}
    if catalog_exists:
        assert catalog["status"] == "PURGED"


@pytest.mark.parametrize("shared", [False, True])
def test_http_delete_cleans_all_versions_and_published_links_but_preserves_external_references(shared):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    from admin.api.v1.admin import build_admin_router
    from services.persistence.document_lifecycle import DocumentLifecycleStore
    from services.persistence.postgres import InMemoryDraftStore
    from services.persistence.sqlalchemy import metadata

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    metadata.create_all(engine)
    lifecycle = DocumentLifecycleStore(engine=engine)
    drafts = InMemoryDraftStore()
    linked = []
    files = {"old.pdf", "current.pdf"}
    derived = {"doc-1"}
    mysql = {"old": "old business", "current": "current business"}
    field_vectors = {"old": ["old-vector"], "current": ["current-vector"]}
    for number, key, mysql_id in ((1, "old.pdf", "old"), (2, "current.pdf", "current")):
        lifecycle.create_version(
            document_id="doc-1", source_name="source.pdf", storage_key=key,
            scope_id="public", user_id="admin", version_number=number,
            knowledge_type="solution", status="PUBLISHED", publication_status="PUBLISHED",
        )
        row = drafts.create(user_id="admin", user_role="admin", draft={
            "draft_type": "solution", "source_id": f"source:task-{number}", "mysql_record_id": mysql_id,
            "attachments": [{"document_id": "doc-1", "version_id": f"doc-1:v{number}", "storage_key": key}],
        })
        drafts.get(row["draft_id"], user_id="admin")["status"] = "PUBLISHED"
        linked.append(row["draft_id"])
    external = None
    if shared:
        external = drafts.create(user_id="other-user", user_role="internal", draft={
            "draft_type": "solution", "source_id": "source:external", "mysql_record_id": "old",
            "attachments": [{"document_id": "doc-other", "storage_key": "old.pdf"}],
        })
        drafts.get(external["draft_id"], user_id="other-user")["status"] = "PUBLISHED"
        external = deepcopy(drafts.get(external["draft_id"], user_id="other-user"))

    async def unpublish(document_id, request):
        assert document_id == "doc-1"
        assert request.remove_vectors is True
        return SimpleNamespace(status="SUCCESS", model_dump=lambda: {"status": "SUCCESS"})

    async def delete_original(*, storage_key):
        files.discard(storage_key)

    async def delete_stored(*, storage_key, document_id):
        files.discard(storage_key)
        derived.discard(document_id)

    runtime = SimpleNamespace(
        task_store=None, outbox_repository=None,
        require_lifecycle=lambda: lifecycle, lifecycle_service=lifecycle,
        require_draft_store=lambda: drafts, draft_store_service=drafts,
        business_record_sink=SimpleNamespace(delete=lambda **kw: mysql.pop(kw["record_id"])),
        embedded_retrieval=SimpleNamespace(agent=SimpleNamespace(
            unpublish_document=unpublish,
            public_milvus={"solution": SimpleNamespace(delete_by_mysql_id=lambda key: field_vectors.pop(key))},
        )),
        embedded_perception=SimpleNamespace(
            delete_original_file=delete_original, delete_stored_document_internal=delete_stored,
        ),
    )
    service = AdminService.__new__(AdminService)
    service.runtime = runtime
    runtime.require_admin = lambda: service
    app = FastAPI()
    app.include_router(build_admin_router(runtime))
    with TestClient(app) as client:
        response = client.request("DELETE", "/api/v1/admin/documents/doc-1", json={
            "storage_key": "untrusted.pdf", "knowledge_type": "solution", "user_id": "admin",
        })

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "DELETED"
    assert all(row["status"] == "PURGED" for row in lifecycle.list_versions("doc-1"))
    assert all(drafts.get(draft_id, user_id="admin")["status"] == "DELETED" for draft_id in linked)
    assert files == ({"old.pdf"} if shared else set())
    assert derived == ({"doc-1"} if shared else set())
    assert mysql == ({"old": "old business"} if shared else {})
    assert field_vectors == ({"old": ["old-vector"]} if shared else {})
    if shared:
        assert drafts.get(external["draft_id"], user_id="other-user") == external
