import asyncio
from types import SimpleNamespace
import pytest

from admin.service import AdminService


@pytest.fixture(autouse=True)
def isolated_database(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)


@pytest.mark.parametrize("action", ["publish", "unpublish", "recycle", "restore", "purge"])
def test_purged_version_cannot_be_operated_on_again(action):
    from services.application.document_lifecycle import DocumentLifecycleApplicationService

    lifecycle = SimpleNamespace(get_version=lambda _: {
        "document_id": "doc-1", "version_id": "doc-1:v1", "status": "PURGED",
        "publication_status": "UNPUBLISHED",
    })
    runtime = SimpleNamespace(require_lifecycle=lambda: lifecycle)
    with pytest.raises(ValueError, match="彻底删除"):
        asyncio.run(DocumentLifecycleApplicationService(runtime).run_action(action, "doc-1:v1", "admin"))


@pytest.mark.parametrize("catalog_exists", [False, True])
def test_missing_document_version_is_idempotently_purged(catalog_exists):
    deleted_vectors = []
    deleted_files = []
    purged_catalog = []

    class Lifecycle:
        def get_version(self, _version_id):
            return None

        def get_document(self, _document_id, include_purged=True):
            return {"document_id": "doc-1"} if catalog_exists else None

        def list_versions(self, _document_id):
            return []

        def purge_document(self, document_id):
            purged_catalog.append(document_id)
            return {"document_id": document_id, "status": "PURGED"}

    class Target:
        def delete_by_document_id(self, document_id):
            deleted_vectors.append(document_id)

    class Storage:
        async def find_by_document_id(self, _document_id):
            return [{"storage_key": "uploads/2026/09/file.pdf"}]

        async def delete_document(self, storage_key, document_id):
            deleted_files.append((storage_key, document_id))

    class Drafts:
        def list_for_document(self, document_id):
            return []

    runtime = SimpleNamespace(
        task_store=None,
        require_lifecycle=lambda: Lifecycle(),
        require_draft_store=lambda: Drafts(),
        storage_service=Storage(),
        embedded_retrieval=SimpleNamespace(
            agent=SimpleNamespace(public_milvus={name: Target() for name in (
                "enterprise", "expert", "demand", "achievement", "solution",
            )}),
        ),
    )

    result, task = asyncio.run(
        AdminService(runtime).lifecycle_action("purge", "doc-1:v1", "admin")
    )

    assert task is None
    assert result["status"] == "PURGED"
    assert result["orphaned"] is True
    assert len(deleted_vectors) == 5
    assert deleted_files == [("uploads/2026/09/file.pdf", "doc-1")]
    assert purged_catalog == (["doc-1"] if catalog_exists else [])


def test_missing_version_does_not_pretend_to_purge_another_live_version():
    lifecycle = SimpleNamespace(
        get_version=lambda _: None,
        get_document=lambda *a, **k: {"document_id": "doc-1"},
        list_versions=lambda _: [{"version_id": "doc-1:v2", "status": "PUBLISHED"}],
    )
    runtime = SimpleNamespace(task_store=None, require_lifecycle=lambda: lifecycle)
    with pytest.raises(ValueError, match="版本"):
        asyncio.run(AdminService(runtime).lifecycle_action("purge", "doc-1:v1", "admin"))


def test_purge_document_deletes_linked_mysql_record_and_milvus_vectors():
    deleted_mysql = []
    deleted_vectors = []
    logically_deleted = []

    class Drafts:
        def list_for_document(self, document_id):
            return [{
                "draft_id": "draft-1",
                "draft_type": "solution",
                "user_id": "user-1",
                "source_document_id": "doc-1",
                "draft": {
                    "draft_type": "solution",
                    "mysql_record_id": "mysql-1",
                    "attachments": [],
                },
                "status": "CONFIRMED",
            }]

        def logical_delete(self, draft_id, user_id, operator_id, **_kwargs):
            logically_deleted.append((draft_id, user_id, operator_id))
            return {"draft_id": draft_id}

        def list_business_references(self, draft_type, mysql_record_id, *, user_id):
            return self.list_for_document("doc-1")

        def list_storage_key_references(self, storage_key):
            return []

    class Sink:
        def delete(self, **kwargs):
            deleted_mysql.append(kwargs)

    class Target:
        def delete_by_mysql_id(self, mysql_id, **_kwargs):
            deleted_vectors.append(("mysql", mysql_id))

    runtime = SimpleNamespace(
        task_store=None,
        sync_state_store=None,
        require_draft_store=lambda: Drafts(),
        business_record_sink=Sink(),
        embedded_retrieval=SimpleNamespace(
            agent=SimpleNamespace(public_milvus={"solution": Target()}),
        ),
    )

    assert AdminService(runtime)._delete_linked_drafts("doc-1") == 1
    assert deleted_mysql == [{
        "record_type": "solution", "record_id": "mysql-1", "user_id": "user-1",
    }]
    assert deleted_vectors == [("mysql", "mysql-1")]
    assert logically_deleted == [("draft-1", "user-1", "admin")]


def test_document_purge_expands_to_all_same_source_drafts():
    from services.application.draft_cleanup import delete_linked_drafts
    from services.persistence.postgres import InMemoryDraftStore

    store = InMemoryDraftStore()
    store.create(
        user_id="user-1",
        user_role="internal",
        draft={
            "draft_type": "solution",
            "title": "linked",
            "source_id": "batch-doc-1",
            "mysql_record_id": "mysql-1",
            "attachments": [{"document_id": "doc-1", "storage_key": "file-1"}],
        },
    )
    store.create(
        user_id="user-1",
        user_role="internal",
        draft={
            "draft_type": "solution",
            "title": "sibling without attachment reference",
            "source_id": "batch-doc-1",
            "mysql_record_id": "mysql-2",
        },
    )
    published_sibling = store.list_for_source_id("batch-doc-1")[1]
    store._items[published_sibling["draft_id"]]["status"] = "PUBLISHED"
    deleted_business = []
    deleted_vectors = []

    class Sink:
        def delete(self, **kwargs):
            deleted_business.append(kwargs["record_id"])

    class Target:
        def delete_by_mysql_id(self, mysql_id, **_kwargs):
            deleted_vectors.append(mysql_id)

    runtime = SimpleNamespace(
        require_draft_store=lambda: store,
        business_record_sink=Sink(),
        embedded_retrieval=SimpleNamespace(
            agent=SimpleNamespace(public_milvus={"solution": Target()})
        ),
    )

    assert delete_linked_drafts(runtime, "doc-1") == 2
    assert sorted(deleted_business) == ["mysql-1", "mysql-2"]
    assert sorted(deleted_vectors) == ["mysql-1", "mysql-2"]
    assert all(
        row["status"] == "DELETED"
        for row in store.list_for_source_id("batch-doc-1")
    )
