import asyncio
from types import SimpleNamespace

import pytest

from admin.service import AdminService
from services.persistence.postgres import InMemoryDraftStore


def test_admin_can_purge_only_deleted_draft_record():
    store = InMemoryDraftStore()
    draft = store.create(user_id="user-1", user_role="external", draft={"draft_type": "solution", "title": "old"})
    runtime = SimpleNamespace(task_store=None, require_draft_store=lambda: store)
    service = AdminService(runtime)

    with pytest.raises(LookupError):
        service.purge_deleted_draft(draft["draft_id"])

    store.logical_delete(draft["draft_id"], user_id="user-1")
    assert service.purge_deleted_draft(draft["draft_id"]) == {
        "draft_id": draft["draft_id"], "status": "PURGED",
    }
    assert store.list_all(status="DELETED", limit=10) == []


def test_admin_can_delete_confirmed_draft_and_associated_data():
    store = InMemoryDraftStore()
    draft = store.create(
        user_id="user-1", user_role="internal",
        draft={
            "draft_type": "solution", "title": "solution-1", "mysql_record_id": "mysql-1",
            "attachments": [{"document_id": "doc-1", "storage_key": "uploads/doc-1.pdf"}],
        },
    )
    deleted = []

    class Target:
        def delete_by_mysql_id(self, value):
            deleted.append(("mysql", value))

        def delete_by_source_id(self, value):
            deleted.append(("source", value))

    class Retrieval:
        def __init__(self):
            self.public_milvus = {"solution": Target()}

        async def unpublish_document(self, document_id, _request):
            deleted.append(("vector", document_id))
            return SimpleNamespace(status="SUCCESS", error=None)

    class Storage:
        async def find_by_document_id(self, _document_id):
            return []

        async def delete_document(self, storage_key, document_id):
            deleted.append(("minio", storage_key, document_id))

    class Sink:
        def delete(self, **kwargs):
            deleted.append(("mysql-db", kwargs["record_id"]))

    class Lifecycle:
        def list_versions(self, _document_id):
            return []

        def purge_document(self, document_id):
            deleted.append(("postgres-document", document_id))

    runtime = SimpleNamespace(
        task_store=None,
        require_draft_store=lambda: store,
        embedded_retrieval=SimpleNamespace(agent=Retrieval()),
        storage_service=Storage(),
        lifecycle_service=Lifecycle(),
        business_record_sink=Sink(),
    )

    result = asyncio.run(AdminService(runtime).delete_draft(draft["draft_id"]))

    assert result == {"draft_id": draft["draft_id"], "status": "PURGED", "document_count": 1}
    assert ("mysql-db", "mysql-1") in deleted
    assert ("vector", "doc-1") in deleted
    assert ("minio", "uploads/doc-1.pdf", "doc-1") in deleted
    assert ("postgres-document", "doc-1") in deleted
    assert any(kind == "source" for kind, *_ in deleted)
    assert store.list_all(limit=10) == []


def test_admin_delete_any_batch_draft_purges_all_siblings_and_shared_source_vectors():
    store = InMemoryDraftStore()
    first = store.create(
        user_id="user-1",
        user_role="internal",
        thread_id="thread-1",
        draft={
            "draft_type": "demand",
            "title": "first",
            "source_id": "batch-1",
            "mysql_record_id": "mysql-1",
        },
    )
    second = store.create(
        user_id="user-1",
        user_role="internal",
        thread_id="thread-1",
        draft={
            "draft_type": "demand",
            "title": "second",
            "source_id": "batch-1",
            "mysql_record_id": "mysql-2",
        },
    )
    deleted = []

    class Target:
        def delete_by_mysql_id(self, value):
            deleted.append(("mysql", value))

        def delete_by_source_id(self, value):
            deleted.append(("source", value))

    class Retrieval:
        def __init__(self):
            self.public_milvus = {"demand": Target()}

    class Sink:
        def delete(self, **kwargs):
            deleted.append(("business", kwargs["record_id"]))

    runtime = SimpleNamespace(
        task_store=None,
        require_draft_store=lambda: store,
        embedded_retrieval=SimpleNamespace(agent=Retrieval()),
        business_record_sink=Sink(),
    )

    result = asyncio.run(AdminService(runtime).delete_draft(first["draft_id"]))

    assert result["status"] == "PURGED"
    assert ("business", "mysql-1") in deleted
    assert ("business", "mysql-2") in deleted
    assert ("mysql", "mysql-1") in deleted
    assert ("mysql", "mysql-2") in deleted
    assert deleted.count(("source", "batch-1")) == 1
    assert store.get_admin(first["draft_id"]) is None
    assert store.get_admin(second["draft_id"]) is None


def test_admin_batch_delete_purges_published_sibling_and_shared_source():
    store = InMemoryDraftStore()
    first = store.create(
        user_id="user-1",
        user_role="internal",
        draft={"draft_type": "demand", "title": "first", "source_id": "batch-2"},
    )
    second = store.create(
        user_id="user-1",
        user_role="internal",
        draft={"draft_type": "demand", "title": "second", "source_id": "batch-2"},
    )
    store._items[second["draft_id"]]["status"] = "PUBLISHED"
    side_effects = []

    class Target:
        def delete_by_source_id(self, source_id):
            side_effects.append(("source-delete", source_id))

    class Retrieval:
        def __init__(self):
            self.public_milvus = {"demand": Target()}

    class Sink:
        def delete(self, **kwargs):
            side_effects.append(("business-delete", kwargs["record_id"]))

    runtime = SimpleNamespace(
        task_store=None,
        require_draft_store=lambda: store,
        embedded_retrieval=SimpleNamespace(agent=Retrieval()),
        business_record_sink=Sink(),
    )

    result = asyncio.run(AdminService(runtime).delete_draft(first["draft_id"]))

    assert result["status"] == "PURGED"
    assert ("source-delete", "batch-2") in side_effects
    assert store.get_admin(first["draft_id"]) is None
    assert store.get_admin(second["draft_id"]) is None


def test_admin_batch_delete_preserves_document_with_outside_reference():
    store = InMemoryDraftStore()
    attachment = {"document_id": "shared-doc", "storage_key": "shared/file.pdf"}
    first = store.create(
        user_id="user-1",
        user_role="internal",
        draft={
            "draft_type": "solution",
            "title": "first",
            "source_id": "batch-3",
            "attachments": [attachment],
        },
    )
    store.create(
        user_id="user-1",
        user_role="internal",
        draft={
            "draft_type": "solution",
            "title": "second",
            "source_id": "batch-3",
            "attachments": [attachment],
        },
    )
    outside = store.create(
        user_id="user-2",
        user_role="internal",
        draft={
            "draft_type": "solution",
            "title": "outside",
            "source_id": "other-batch",
            "attachments": [attachment],
        },
    )
    deleted = []

    class Target:
        def delete_by_source_id(self, value):
            deleted.append(("source", value))

    class Retrieval:
        def __init__(self):
            self.public_milvus = {"solution": Target()}

        async def unpublish_document(self, document_id, _request):
            deleted.append(("document-vector", document_id))
            return SimpleNamespace(status="SUCCESS", error=None)

    class Storage:
        async def find_by_document_id(self, _document_id):
            return [{"storage_key": "shared/file.pdf"}]

        async def delete_document(self, storage_key, document_id):
            deleted.append(("storage", storage_key, document_id))

    class Lifecycle:
        def list_versions(self, _document_id):
            return []

        def purge_document(self, document_id):
            deleted.append(("catalog", document_id))

    runtime = SimpleNamespace(
        task_store=None,
        require_draft_store=lambda: store,
        embedded_retrieval=SimpleNamespace(agent=Retrieval()),
        storage_service=Storage(),
        lifecycle_service=Lifecycle(),
    )

    asyncio.run(AdminService(runtime).delete_draft(first["draft_id"]))

    assert ("source", "batch-3") in deleted
    assert not any(kind in {"document-vector", "storage", "catalog"} for kind, *_ in deleted)
    assert store.get_admin(outside["draft_id"]) is not None
