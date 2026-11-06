import asyncio
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from agents.retrieval_agent.application.retrieval_service import RetrievalAgent
from agents.retrieval_agent.schemas import IndexResult
from core.contracts.knowledge import DocumentUnpublishRequest
from runtime.tool_adapters.adapters import InProcessToolAdapter
from services.application.document_lifecycle import (
    DocumentLifecycleApplicationService,
    cleanup_document_version,
)
from services.vector.milvus.milvus_service import MilvusService
from services.persistence.postgres import InMemoryDraftStore
from runtime.workflows.task_store import SupervisorTaskStore
from services.persistence.repositories.runtime_repository import PostgresStateStore
from services.persistence.sqlalchemy import metadata
from sqlalchemy import create_engine


@pytest.fixture(autouse=True)
def isolated_database(monkeypatch):
    # main may have loaded .env.local during collection; never acquire its database lock.
    monkeypatch.delenv("DATABASE_URL", raising=False)

    def forbidden(*args, **kwargs):
        raise AssertionError("A lifecycle unit test must not connect to PostgreSQL")

    monkeypatch.setattr("services.persistence.sqlalchemy.postgres_advisory_lock", forbidden)


class MemoryMilvus(MilvusService):
    def __init__(self, rows=None, *, private=False):
        self.rows = rows if rows is not None else [
            {"id": "current", "document_id": "doc", "user_id": "u", "status": "active"}
        ]
        self.requires_user_id = private

    def _write_guard(self):
        return nullcontext()

    def _existing_collection(self):
        return self

    def _with_collection_recovery_retry(self, operation):
        return operation()

    def delete(self, expr):
        self.rows.clear()

    def flush(self):
        pass

    def _query_batches(self, collection, expr, fields):
        assert collection is self
        if self.requires_user_id:
            assert 'user_id == "u"' in expr
        yield [dict(row) for row in self.rows]

    def _upsert_rows(self, rows, **kwargs):
        self.rows[:] = rows


def retrieval_agent(target, *, active="doc:v2"):
    agent = object.__new__(RetrievalAgent)
    agent.public_milvus = {"solution": target}
    agent.milvus = target
    agent.private_milvus = target
    agent.lifecycle_store = SimpleNamespace(
        get_document=lambda *_a, **_kw: {
            "document_id": "doc", "active_version_id": active, "scope_id": "public"
        },
        get_version=lambda version: {
            "document_id": "doc", "version_id": version, "status": "PUBLISHED",
            "scope_id": "public", "knowledge_type": "solution", "user_id": "u",
        },
    )
    return agent


def request(**fields):
    return DocumentUnpublishRequest(
        knowledge_type="solution", identity={"user_id": "u", "user_role": "internal"},
        **fields,
    )


@pytest.mark.parametrize("remove", [False, True])
def test_old_version_cannot_change_current_vectors(remove):
    target = MemoryMilvus()
    result = asyncio.run(retrieval_agent(target).unpublish_document(
        "doc", request(version_id="doc:v1", remove_vectors=remove)
    ))
    assert result.status == "FAILED"
    assert target.rows[0]["status"] == "active"


def test_old_version_republish_rejects_before_mysql_vector_mutation():
    effects = []
    target = SimpleNamespace(set_publication_status_by_mysql_id=lambda *_a, **_kw: effects.append(1) or 1)
    result = asyncio.run(retrieval_agent(target).republish_document(
        "doc", request(version_id="doc:v1", mysql_id="7")
    ))
    assert result.status == "FAILED"
    assert effects == []


@pytest.mark.parametrize("operation", ["unpublish_document", "republish_document"])
@pytest.mark.parametrize("by", [{}, {"version_id": "doc:v2"}, {"mysql_id": "7"}])
def test_zero_matching_vectors_is_not_success(operation, by):
    target = MemoryMilvus(rows=[])
    target.set_publication_status_by_mysql_id = lambda *_a, **_kw: 0
    result = asyncio.run(getattr(retrieval_agent(target), operation)("doc", request(**by)))
    assert result.status == "FAILED"
    assert result.indexed_count == 0


@pytest.mark.parametrize("private", [False, True])
def test_current_version_status_reports_actual_matching_count(private):
    target = MemoryMilvus(private=private)
    agent = retrieval_agent(target)
    if private:
        agent.lifecycle_store.get_version = lambda version: {
            "document_id": "doc", "version_id": version, "status": "PUBLISHED",
            "scope_id": "internal", "knowledge_type": "solution", "user_id": "u",
        }
    result = asyncio.run(agent.unpublish_document(
        "doc", request(version_id="doc:v2", scope_id="internal" if private else "public")
    ))
    assert result.status == "UNPUBLISHED"
    assert result.indexed_count == 1
    assert target.rows[0]["status"] == "withdrawn"


@pytest.mark.parametrize("operation", ["delete", "status"])
def test_milvus_version_methods_require_verified_current_version(operation):
    target = MemoryMilvus()
    with pytest.raises(ValueError, match="活动版本"):
        if operation == "delete":
            target.delete_by_version_id("doc:v1")
        else:
            target.set_publication_status_by_version_id("doc:v1", "UNPUBLISHED")
    assert target.rows[0]["status"] == "active"


def lifecycle_runtime(*, status="UNPUBLISHED", indexed=1, active="doc:v2", original=True):
    effects = []
    version = {
        "document_id": "doc", "version_id": "doc:v2", "status": status,
        "storage_key": "uploads/new.pdf", "knowledge_type": "solution", "scope_id": "public",
        "index_status": "INDEXED" if indexed else "NOT_INDEXED", "indexed_count": indexed,
        "publication_status": "PUBLISHED" if status == "PUBLISHED" else "UNPUBLISHED",
    }
    document = {**version, "active_version_id": active}

    def update(_version, **fields):
        effects.append("postgres")
        version.update(fields)
        return dict(version)

    store = SimpleNamespace(
        get_version=lambda _id: version, get_document=lambda *_a, **_kw: document,
        update_version=update, list_versions=lambda _doc: [version],
        is_storage_key_referenced=lambda *_a, **_kw: False,
    )

    async def republish(*args):
        effects.append("publish")
        return IndexResult(status="PUBLISHED", document_id="doc", indexed_count=indexed)

    async def unpublish(*args):
        effects.append("unpublish")
        return IndexResult(status="UNPUBLISHED", document_id="doc", indexed_count=indexed)

    async def inspect(key):
        effects.append("inspect")
        return {"storage_key": key, "size": 1} if original else None

    drafts = SimpleNamespace(list_for_document=lambda _doc: [], list_storage_key_references=lambda _key: [])
    runtime = SimpleNamespace(
        require_lifecycle=lambda: store, lifecycle_service=store,
        require_draft_store=lambda: drafts, draft_store_service=drafts,
        task_store=None, outbox_repository=None,
        storage_service=SimpleNamespace(inspect_object=inspect),
        embedded_retrieval=SimpleNamespace(agent=SimpleNamespace(
            republish_document=republish, unpublish_document=unpublish,
        )),
    )
    return runtime, effects, version


@pytest.mark.parametrize("action,status", [
    ("publish", "UPLOADED"), ("publish", "RECYCLED"),
    ("unpublish", "UPLOADED"), ("unpublish", "RECYCLED"),
    ("restore", "UNPUBLISHED"), ("invalid", "UNPUBLISHED"),
])
def test_illegal_lifecycle_transition_does_not_call_vectors(action, status):
    runtime, effects, _ = lifecycle_runtime(status=status, indexed=0)
    with pytest.raises(ValueError):
        asyncio.run(DocumentLifecycleApplicationService(runtime).run_action(action, "doc:v2", "u"))
    assert effects == []


@pytest.mark.parametrize("action,status", [
    ("publish", "UNPUBLISHED"), ("unpublish", "PUBLISHED"),
    ("recycle", "PUBLISHED"), ("restore", "RECYCLED"),
])
def test_non_active_lifecycle_action_is_rejected(action, status):
    runtime, effects, _ = lifecycle_runtime(status=status, active="doc:v3")
    with pytest.raises(ValueError, match="活动版本"):
        asyncio.run(DocumentLifecycleApplicationService(runtime).run_action(action, "doc:v2", "u"))
    assert effects == []


def test_republish_requires_readable_original_before_touching_vectors():
    runtime, effects, version = lifecycle_runtime(original=False)
    result, _ = asyncio.run(DocumentLifecycleApplicationService(runtime).run_action("publish", "doc:v2", "u"))
    assert result is None
    assert version["status"] == "UNPUBLISHED"
    assert effects == ["inspect"]


def test_zero_vector_result_cannot_mark_lifecycle_published():
    runtime, effects, version = lifecycle_runtime()

    async def zero(*args):
        return IndexResult(status="PUBLISHED", document_id="doc", indexed_count=0)

    runtime.embedded_retrieval.agent.republish_document = zero
    result, _ = asyncio.run(DocumentLifecycleApplicationService(runtime).run_action("publish", "doc:v2", "u"))
    assert result is None
    assert version["status"] == "UNPUBLISHED"
    assert "postgres" not in effects


def test_restore_never_published_document_returns_to_confirmation_without_vectors():
    runtime, effects, version = lifecycle_runtime(status="RECYCLED", indexed=0)
    result, _ = asyncio.run(DocumentLifecycleApplicationService(runtime).run_action("restore", "doc:v2", "u"))
    assert result["status"] == "UPLOADED"
    assert result["publication_status"] == "UNPUBLISHED"
    assert "publish" not in effects


def cleanup_adapter(*, active="doc:v2", replacement_status="PUBLISHED", shared=False):
    effects = []
    versions = {
        "doc:v1": {"document_id": "doc", "version_id": "doc:v1", "storage_key": "uploads/old.pdf", "status": "UNPUBLISHED"},
        "doc:v2": {"document_id": "doc", "version_id": "doc:v2", "storage_key": "uploads/new.pdf", "status": replacement_status,
                   "publication_status": "PUBLISHED" if replacement_status in {"PUBLISHED", "INDEXED"} else "UNPUBLISHED"},
    }
    store = SimpleNamespace(
        get_version=versions.get,
        get_document=lambda *_a, **_kw: {"document_id": "doc", "active_version_id": active},
        list_versions=lambda _doc: list(versions.values()),
        update_version=lambda version, **fields: versions[version].update(fields) or versions[version],
        is_storage_key_referenced=lambda key, exclude_version_ids=(): any(
            row["version_id"] not in exclude_version_ids and row["status"] != "PURGED"
            and row["storage_key"] == key for row in versions.values()
        ),
    )

    async def vectors(*args):
        effects.append("vectors")
        return IndexResult(status="UNPUBLISHED", document_id="doc")

    async def delete_original(**kwargs):
        effects.append(kwargs["storage_key"])
        return {"status": "DELETED"}

    drafts = SimpleNamespace(
        list_for_document=lambda _doc: [],
        list_storage_key_references=lambda _key: [{"draft_id": "shared", "status": "PUBLISHED"}] if shared else [],
    )
    adapter = InProcessToolAdapter(
        SimpleNamespace(delete_original_file=delete_original),
        SimpleNamespace(unpublish_document=vectors), store,
    )
    adapter.draft_store = drafts
    payload = {
        "document_id": "doc", "version_id": "doc:v1", "storage_key": "uploads/old.pdf",
        "replacement_version_id": "doc:v2", "replacement_storage_key": "uploads/new.pdf",
        "preserve_current_vectors": True, "knowledge_type": "solution",
    }
    return adapter, store, payload, effects, versions


def test_old_cleanup_only_deletes_old_original_and_preserves_current_vectors():
    adapter, store, payload, effects, versions = cleanup_adapter()
    result = asyncio.run(cleanup_document_version(adapter, store, payload))
    assert result["status"] == "DELETED"
    assert effects == ["uploads/old.pdf"]
    assert versions["doc:v1"]["status"] == "PURGED"


@pytest.mark.parametrize("changed", [
    {"version_id": "doc:v2"}, {"storage_key": "uploads/wrong.pdf"},
    {"replacement_version_id": "doc:v3"}, {"replacement_storage_key": "uploads/wrong.pdf"},
    {"preserve_current_vectors": False},
])
def test_ambiguous_old_cleanup_never_deletes(changed):
    adapter, _store, payload, effects, _versions = cleanup_adapter()
    result = asyncio.run(adapter.delete_version({**payload, **changed}))
    assert result["status"] == "FAILED"
    assert effects == []


def test_shared_original_cleanup_is_skipped_without_purging_version():
    adapter, store, payload, effects, versions = cleanup_adapter(shared=True)
    result = asyncio.run(cleanup_document_version(adapter, store, payload))
    assert result["status"] == "SKIPPED"
    assert effects == []
    assert versions["doc:v1"]["status"] == "UNPUBLISHED"


def test_cleanup_requires_successfully_published_replacement():
    adapter, _store, payload, effects, _versions = cleanup_adapter(replacement_status="FAILED")
    result = asyncio.run(adapter.delete_version(payload))
    assert result["status"] == "FAILED"
    assert effects == []


def test_indexed_confirmed_replacement_can_cleanup_only_old_original():
    adapter, store, payload, effects, versions = cleanup_adapter(replacement_status="INDEXED")
    result = asyncio.run(cleanup_document_version(adapter, store, payload))
    assert result["status"] == "DELETED"
    assert effects == ["uploads/old.pdf"]


def direct_delete_runtime(*, shared=False, fail_commit=False):
    runtime, effects, version = lifecycle_runtime(status="PUBLISHED")
    versions = {
        version["version_id"]: version,
        "doc:v1": {**version, "version_id": "doc:v1", "storage_key": "uploads/old.pdf"},
    }
    store = runtime.lifecycle_service
    store.get_version = versions.get
    store.list_versions = lambda _doc: list(versions.values())
    failures = [fail_commit]

    def purge(_doc):
        if failures[0]:
            failures[0] = False
            return False
        for row in versions.values():
            row.update(status="PURGED", publication_status="UNPUBLISHED", index_status="DELETED")
        return True

    store.purge_document = purge
    drafts = InMemoryDraftStore()
    linked = drafts.create(user_id="u", user_role="internal", draft={
        "draft_type": "solution", "mysql_record_id": "7", "source_id": "source:doc",
        "attachments": [{"document_id": "doc", "version_id": "doc:v2", "storage_key": "uploads/new.pdf"}],
    })
    drafts.transition(linked["draft_id"], user_id="u", target_status="PUBLISHED", operator_id="u", operator_role="internal")
    if shared:
        external = drafts.create(user_id="other", user_role="internal", draft={
            "draft_type": "solution", "mysql_record_id": "7", "source_id": "source:other",
            "attachments": [{"document_id": "other", "storage_key": "uploads/old.pdf"}],
        })
        drafts.transition(external["draft_id"], user_id="other", target_status="PUBLISHED", operator_id="other", operator_role="internal")
    runtime.require_draft_store = lambda: drafts
    runtime.draft_store_service = drafts
    runtime.business_record_sink = SimpleNamespace(delete=lambda **_kw: effects.append("mysql"))
    runtime.embedded_retrieval.agent.public_milvus = {
        "solution": SimpleNamespace(delete_by_mysql_id=lambda _id: effects.append("mysql-vector"))
    }

    async def delete_original(*, storage_key):
        effects.append(storage_key)

    async def delete_document(**kwargs):
        effects.append("derived")

    runtime.embedded_perception = SimpleNamespace(
        delete_original_file=delete_original, delete_stored_document_internal=delete_document,
    )
    events = []
    runtime.outbox_repository = SimpleNamespace(append=lambda *args, **kwargs: events.append(args))
    return runtime, effects, versions, drafts, events


def test_direct_delete_purges_all_versions_and_linked_business_data():
    runtime, effects, versions, drafts, _events = direct_delete_runtime()
    result, _ = asyncio.run(DocumentLifecycleApplicationService(runtime).run_action(
        "purge", "doc:v2", "admin", user_role="admin", direct_delete=True
    ))
    assert result["status"] == "PURGED"
    assert all(row["status"] == "PURGED" for row in versions.values())
    assert drafts.list_all(status="PUBLISHED") == []
    assert {"unpublish", "mysql", "mysql-vector", "uploads/old.pdf", "uploads/new.pdf", "derived"} <= set(effects)


def test_direct_delete_preserves_external_shared_original_and_business_vectors():
    runtime, effects, versions, drafts, _events = direct_delete_runtime(shared=True)
    result, _ = asyncio.run(DocumentLifecycleApplicationService(runtime).run_action(
        "purge", "doc:v2", "admin", user_role="admin", direct_delete=True
    ))
    assert result["status"] == "PURGED"
    assert all(row["status"] == "PURGED" for row in versions.values())
    assert len(drafts.list_all(status="PUBLISHED")) == 1
    assert "uploads/old.pdf" not in effects
    assert "uploads/new.pdf" in effects
    assert "mysql" not in effects and "mysql-vector" not in effects
    assert "derived" not in effects


def test_direct_delete_partial_commit_remains_recoverable_and_keeps_delete_scope():
    runtime, effects, versions, drafts, events = direct_delete_runtime(fail_commit=True)
    service = DocumentLifecycleApplicationService(runtime)
    result, _ = asyncio.run(service.run_action(
        "purge", "doc:v2", "admin", user_role="admin", direct_delete=True
    ))
    assert result is None
    assert versions["doc:v2"]["status"] == "PUBLISHED"
    assert events[0][2]["direct_delete"] is True
    asyncio.run(service.recover({"operation_id": events[0][0], "payload": events[0][2]}))
    assert all(row["status"] == "PURGED" for row in versions.values())
    assert effects.count("mysql") == 1
    before = effects.copy()
    asyncio.run(service.recover({"payload": events[0][2]}))
    assert effects == before


def test_indexed_published_document_can_unpublish():
    runtime, _effects, version = lifecycle_runtime(status="INDEXED")
    version["publication_status"] = "PUBLISHED"
    result, _ = asyncio.run(DocumentLifecycleApplicationService(runtime).run_action(
        "unpublish", "doc:v2", "u"
    ))
    assert result["status"] == "UNPUBLISHED"


def test_purging_unpublished_replacement_keeps_previous_published_index():
    runtime, effects, version = lifecycle_runtime(status="RECYCLED", indexed=0)
    runtime.lifecycle_service.list_versions = lambda _doc: [version, {
        **version, "version_id": "doc:v1", "storage_key": "uploads/old.pdf",
        "status": "PUBLISHED", "index_status": "INDEXED", "indexed_count": 1,
    }]

    async def original(**kwargs):
        effects.append(kwargs["storage_key"])

    runtime.embedded_perception = SimpleNamespace(delete_original_file=original)
    result, _ = asyncio.run(DocumentLifecycleApplicationService(runtime).run_action(
        "purge", "doc:v2", "admin", user_role="admin"
    ))
    assert result["status"] == "PURGED"
    assert "unpublish" not in effects
    assert effects == ["uploads/new.pdf", "postgres"]


@pytest.mark.parametrize("repository", [False, True])
def test_direct_delete_scope_survives_real_task_store_completion_and_reload(tmp_path, repository):
    runtime, _effects, versions, _drafts, events = direct_delete_runtime(fail_commit=True)
    path = tmp_path / "tasks.json"
    engine = create_engine("sqlite://")
    metadata.create_all(engine)

    def task_store():
        if not repository:
            return SupervisorTaskStore(path)
        tasks = object.__new__(SupervisorTaskStore)
        tasks._memory = None
        tasks._postgres = PostgresStateStore(service="supervisor", engine=engine)
        return tasks

    runtime.task_store = task_store()
    result, task = asyncio.run(DocumentLifecycleApplicationService(runtime).run_action(
        "purge", "doc:v2", "admin", user_role="admin", direct_delete=True
    ))
    assert result is None
    reloaded = task_store().get(task["task_id"])
    assert reloaded["compensation"]["direct_delete"] is True
    persisted = PostgresStateStore._state_fields(reloaded)
    assert persisted["compensation"]["direct_delete"] is True
    runtime.task_store = task_store()
    asyncio.run(DocumentLifecycleApplicationService(runtime).recover({
        "operation_id": task["task_id"], "payload": events[0][2],
    }))
    assert all(row["status"] == "PURGED" for row in versions.values())
    finished = task_store().get(task["task_id"])
    assert finished["compensation"]["direct_delete"] is True
    assert finished["status"] == "SUCCEEDED"
    engine.dispose()


def test_restore_recovery_after_postgres_commit_only_finishes_original_task(tmp_path):
    runtime, effects, version = lifecycle_runtime(status="RECYCLED")
    runtime.task_store = SupervisorTaskStore(tmp_path / "tasks.json")
    complete = runtime.task_store.complete
    failures = [True]

    def fail_after_commit(task_id, **kwargs):
        if kwargs["status"] == "SUCCEEDED" and failures[0]:
            failures[0] = False
            raise RuntimeError("task completion interrupted")
        return complete(task_id, **kwargs)

    runtime.task_store.complete = fail_after_commit
    events = []
    runtime.outbox_repository = SimpleNamespace(append=lambda *args: events.append(args))
    service = DocumentLifecycleApplicationService(runtime)
    result, task = asyncio.run(service.run_action("restore", "doc:v2", "u"))
    assert result is None
    assert version["status"] == "PUBLISHED"
    before = effects.copy()
    asyncio.run(service.recover({"operation_id": task["task_id"], "payload": events[0][2]}))
    assert effects == before
    assert runtime.task_store.get(task["task_id"])["status"] == "SUCCEEDED"


@pytest.mark.parametrize("action,status", [("publish", "UNPUBLISHED"), ("unpublish", "PUBLISHED"), ("recycle", "PUBLISHED")])
def test_lifecycle_rejects_unexpected_or_empty_vector_result(action, status):
    runtime, effects, version = lifecycle_runtime(status=status)

    async def unexpected(*args):
        return IndexResult(status="PARTIAL_SUCCESS", document_id="doc", indexed_count=1)

    runtime.embedded_retrieval.agent.republish_document = unexpected
    runtime.embedded_retrieval.agent.unpublish_document = unexpected
    result, _ = asyncio.run(DocumentLifecycleApplicationService(runtime).run_action(action, "doc:v2", "u"))
    assert result is None
    assert version["status"] == status
    assert "postgres" not in effects


def test_admin_private_unpublish_scopes_vectors_to_owner_and_audit_to_admin():
    runtime, _effects, version = lifecycle_runtime(status="PUBLISHED")
    version.update(scope_id="internal", user_id="owner")

    async def owner_vectors(document_id, request):
        assert request.identity.user_id == "owner"
        return IndexResult(status="UNPUBLISHED", document_id=document_id, indexed_count=1)

    runtime.embedded_retrieval.agent.unpublish_document = owner_vectors
    result, _ = asyncio.run(DocumentLifecycleApplicationService(runtime).run_action(
        "unpublish", "doc:v2", "admin", user_role="admin"
    ))
    assert result["status"] == "UNPUBLISHED"
    assert result["unpublished_by"] == "admin"


def parse_preview_runtime():
    runtime, effects, version = lifecycle_runtime()
    task = {"task_id": "task-preview", "status": "SUCCEEDED", "document_id": "doc",
            "version_id": "doc:v2", "storage_key": "uploads/new.pdf"}
    parsed = {"document_id": "doc", "storage_key": "uploads/new.pdf", "raw_text": "current body"}

    async def load(_doc):
        effects.append("read")
        return parsed

    runtime.storage_service.load_parse_result = load
    return runtime, task, parsed, effects, version


@pytest.mark.parametrize("changed", [
    {"version_id": "doc:v1", "storage_key": "uploads/old.pdf"},
    {"storage_key": "uploads/wrong.pdf"}, {"version_id": None}, {"storage_key": None},
])
def test_task_preview_rejects_stale_source_before_reading_latest_body(changed):
    runtime, task, _parsed, effects, _version = parse_preview_runtime()
    response = asyncio.run(DocumentLifecycleApplicationService(runtime).with_parse_result({**task, **changed}))
    assert not response.get("parsed_result")
    assert response["error_code"] == "STALE_SOURCE"
    assert effects == []
    assert task["status"] == "SUCCEEDED"


@pytest.mark.parametrize("changed", [
    {"document_id": "other"}, {"storage_key": "uploads/other.pdf"},
    {"version_id": "doc:v1"}, {"storage_key": None},
])
def test_task_preview_rejects_mismatched_parsed_body(changed):
    runtime, task, parsed, _effects, _version = parse_preview_runtime()
    parsed.update(changed)
    response = asyncio.run(DocumentLifecycleApplicationService(runtime).with_parse_result(task))
    assert not response.get("parsed_result")
    assert response["error_code"] == "STALE_SOURCE"


@pytest.mark.parametrize("embedded", [False, True])
def test_task_preview_preserves_matching_current_body(embedded):
    runtime, task, parsed, effects, _version = parse_preview_runtime()
    if embedded:
        task["parsed_result"] = parsed
    response = asyncio.run(DocumentLifecycleApplicationService(runtime).with_parse_result(task))
    assert response["parsed_result"]["raw_text"] == "current body"
    assert effects == ([] if embedded else ["read"])


def test_task_preview_discards_already_embedded_wrong_source_and_preserves_task_error():
    runtime, task, _parsed, effects, _version = parse_preview_runtime()
    task.update(error_code="ORIGINAL_FAILURE", error="original", parsed_result={
        "document_id": "doc", "storage_key": "uploads/old.pdf", "raw_text": "wrong body",
    })
    response = asyncio.run(DocumentLifecycleApplicationService(runtime).with_parse_result(task))
    assert not response.get("parsed_result")
    assert response["error_code"] == "ORIGINAL_FAILURE"
    assert response["error"] == "original"
    assert effects == []
    assert task["parsed_result"]["raw_text"] == "wrong body"


def test_task_preview_rechecks_current_source_after_loader_await():
    runtime, task, parsed, _effects, _version = parse_preview_runtime()

    async def replace_while_loading(_doc):
        runtime.lifecycle_service.get_document = lambda *_a, **_kw: {"active_version_id": "doc:v3"}
        return parsed

    runtime.storage_service.load_parse_result = replace_while_loading
    response = asyncio.run(DocumentLifecycleApplicationService(runtime).with_parse_result(task))
    assert not response.get("parsed_result")
    assert response["error_code"] == "STALE_SOURCE"


def test_task_preview_loader_failure_is_explicit_without_changing_task_state():
    runtime, task, _parsed, _effects, _version = parse_preview_runtime()

    async def unavailable(_doc):
        raise RuntimeError("preview unavailable")

    runtime.storage_service.load_parse_result = unavailable
    response = asyncio.run(DocumentLifecycleApplicationService(runtime).with_parse_result(task))
    assert response["error_code"] == "PARSE_RESULT_UNAVAILABLE"
    assert response["status"] == "SUCCEEDED"
    assert "unavailable" in response["error"]
