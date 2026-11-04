import asyncio
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from admin.api.v1.admin import build_admin_router
from admin.service import AdminService
from services.application.temporary_materials import _cleanup_saved_draft_attachments
from runtime.workflows.task_store import SupervisorTaskStore
from services.persistence.document_lifecycle import DocumentLifecycleStore
from services.persistence.postgres import InMemoryDraftStore
from services.persistence.sqlalchemy import metadata


def _store():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    metadata.create_all(engine)
    return DocumentLifecycleStore(engine=engine)


def test_document_catalog_pages_are_filtered_and_do_not_load_version_history(monkeypatch):
    store = _store()
    for index in range(23):
        store.create_version(
            document_id=f"doc-{index:02}",
            source_name=f"doc-{index:02}.pdf",
            storage_key=f"uploads/doc-{index:02}.pdf",
            scope_id="public",
            user_id=f"user-{index % 3}",
            version_number=1,
            knowledge_type="demand" if index < 21 else "solution",
            status="PUBLISHED",
            publication_status="PUBLISHED",
            index_status="INDEXED",
        )

    monkeypatch.setattr(
        store,
        "get_version",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("N+1 get_version")),
    )
    monkeypatch.setattr(
        store,
        "list_versions",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("N+1 list_versions")),
    )

    first = store.list_documents_page(knowledge_type="demand", page=1, page_size=10)
    second = store.list_documents_page(knowledge_type="demand", page=2, page_size=10)
    third = store.list_documents_page(knowledge_type="demand", page=3, page_size=10)

    assert (first["total"], first["page"], first["page_size"], first["total_pages"]) == (
        21,
        1,
        10,
        3,
    )
    assert len(first["documents"]) == 10
    assert len(second["documents"]) == 10
    assert len(third["documents"]) == 1
    page_ids = [
        {item["document_id"] for item in page["documents"]} for page in (first, second, third)
    ]
    assert not page_ids[0] & page_ids[1]
    assert not page_ids[0] & page_ids[2]
    assert not page_ids[1] & page_ids[2]
    item = first["documents"][0]
    assert item["active_version"]["publication_status"] == "PUBLISHED"
    assert "versions" not in item


def test_document_catalog_includes_admin_and_user_uploads_but_hides_purged_records():
    store = _store()
    for document_id, user_id in (
        ("admin-upload", "admin"),
        ("user-upload", "user-1"),
        ("cleaned-upload", "user-2"),
    ):
        store.create_version(
            document_id=document_id,
            source_name=f"{document_id}.pdf",
            storage_key=f"uploads/{document_id}.pdf",
            scope_id="public",
            user_id=user_id,
            version_number=1,
            knowledge_type="demand",
            status="UPLOADED",
        )
    store.purge_document("cleaned-upload")

    result = store.list_documents_page(knowledge_type="demand", page=1, page_size=20)

    assert result["total"] == 2
    assert {item["user_id"] for item in result["documents"]} == {"admin", "user-1"}


def test_saved_draft_attachment_cleanup_purges_catalog_only_after_storage_delete():
    store = _store()
    for document_id in ("expired-draft-file", "published-file", "failed-draft-file"):
        store.create_version(
            document_id=document_id,
            source_name=f"{document_id}.pdf",
            storage_key=f"uploads/{document_id}.pdf",
            scope_id="public",
            user_id="user-1",
            version_number=1,
            knowledge_type="demand",
            status="UPLOADED",
        )

    class DraftStore:
        def update(self, *_args, **_kwargs):
            return None

        def list_storage_key_references(self, _key):
            return []

        def list_for_document(self, _document_id):
            return []

    class Storage:
        def __init__(self):
            self.deleted = []

        async def delete_document(self, storage_key, document_id):
            if document_id == "failed-draft-file":
                raise OSError("storage unavailable")
            self.deleted.append((storage_key, document_id))

    storage = Storage()
    old = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
    drafts = [
        {
            "draft_id": "draft-1",
            "user_id": "user-1",
            "status": "CONFIRMED",
            "updated_at": old,
            "draft": {
                "attachments": [
                    {
                        "document_id": "expired-draft-file",
                        "storage_key": "uploads/expired-draft-file.pdf",
                    }
                ]
            },
        },
        {
            "draft_id": "draft-2",
            "user_id": "user-1",
            "status": "CONFIRMED",
            "updated_at": old,
            "draft": {
                "attachments": [
                    {
                        "document_id": "published-file",
                        "storage_key": "uploads/published-file.pdf",
                    }
                ]
            },
        },
        {
            "draft_id": "draft-3",
            "user_id": "user-1",
            "status": "CONFIRMED",
            "updated_at": old,
            "draft": {
                "attachments": [
                    {
                        "document_id": "failed-draft-file",
                        "storage_key": "uploads/failed-draft-file.pdf",
                    }
                ]
            },
        },
    ]

    cleaned = asyncio.run(
        _cleanup_saved_draft_attachments(
            DraftStore(),
            storage,
            drafts,
            {"published-file"},
            store,
        )
    )

    assert cleaned == 1
    assert storage.deleted == [("uploads/expired-draft-file.pdf", "expired-draft-file")]
    assert store.get_document("expired-draft-file") is None
    assert store.get_document("expired-draft-file", include_purged=True)["status"] == "PURGED"
    assert store.get_document("published-file")["status"] == "UPLOADED"
    assert store.get_document("failed-draft-file")["status"] == "UPLOADED"


def test_admin_catalog_enriches_each_user_once_and_never_loads_drafts_or_tasks():
    class Lifecycle:
        def list_documents_page(self, **kwargs):
            assert kwargs["page"] == 1
            assert kwargs["page_size"] == 20
            return {
                "documents": [
                    {"document_id": "doc-1", "user_id": "user-1", "knowledge_type": "demand"},
                    {"document_id": "doc-2", "user_id": "user-1", "knowledge_type": "demand"},
                    {"document_id": "doc-3", "user_id": "user-2", "knowledge_type": "solution"},
                ],
                "total": 3,
                "page": 1,
                "page_size": 20,
                "total_pages": 1,
            }

    class IdentityReader:
        def __init__(self):
            self.calls = []

        def get(self, user_id):
            self.calls.append(user_id)
            return type(
                "Profile",
                (),
                {
                    "name": user_id,
                    "company": f"company-{user_id}",
                    "is_inner_user": False,
                },
            )()

    class MustNotList:
        def list(self, **_kwargs):
            raise AssertionError("directory request loaded records outside its page summary")

    reader = IdentityReader()
    runtime = type(
        "Runtime",
        (),
        {
            "require_lifecycle": lambda self: Lifecycle(),
            "user_identity_reader": reader,
            "draft_store_service": MustNotList(),
            "task_store": MustNotList(),
        },
    )()

    result = AdminService(runtime).document_catalog(
        scope_id=None,
        user_id=None,
        knowledge_type=None,
        status=None,
        page=1,
        page_size=20,
    )

    assert reader.calls == ["user-1", "user-2"]
    assert result["total"] == 3
    assert result["page_size"] == 20
    assert "task_records" not in result
    assert all(
        "published_drafts" not in item and "versions" not in item for item in result["documents"]
    )


def test_linked_drafts_and_tasks_are_loaded_only_in_document_detail():
    class Lifecycle:
        def get_document(self, document_id, **_kwargs):
            return {"document_id": document_id, "versions": [{"version_id": "version-1"}]}

    class DraftStore:
        def list_for_document(self, document_id):
            assert document_id == "doc-1"
            return [{"draft_id": "draft-1", "status": "PUBLISHED"}]

    class TaskStore:
        def list_page(self, *, document_id, page, page_size):
            assert (document_id, page, page_size) == ("doc-1", 1, 100)
            return {"items": [{"task_id": "task-1", "document_id": document_id}]}

    runtime = type(
        "Runtime",
        (),
        {
            "require_lifecycle": lambda self: Lifecycle(),
            "draft_store_service": DraftStore(),
            "task_store": TaskStore(),
        },
    )()

    detail = AdminService(runtime).document_detail("doc-1")
    assert detail["associated_drafts"] == [{"draft_id": "draft-1", "status": "PUBLISHED"}]
    assert detail["task_records"] == [{"task_id": "task-1", "document_id": "doc-1"}]


def test_document_scoped_draft_and_task_queries_filter_and_paginate():
    drafts = InMemoryDraftStore()
    published = drafts.create(
        user_id="user-1",
        user_role="external",
        draft={
            "draft_type": "demand",
            "attachments": [{"document_id": value} for value in ("doc-1", "doc-2")],
        },
    )
    deleted = drafts.create(
        user_id="user-1",
        user_role="external",
        draft={"draft_type": "demand", "attachments": [{"document_id": "doc-2"}]},
    )
    drafts.logical_delete(deleted["draft_id"], user_id="user-1")
    assert [item["draft_id"] for item in drafts.list_for_document("doc-2")] == [
        published["draft_id"]
    ]

    tasks = SupervisorTaskStore()
    tasks.create(operation="file_ingestion", status="FAILED", document_id="doc-1")
    tasks.create(operation="file_ingestion", status="SUCCEEDED", document_id="doc-2")
    page = tasks.list_page(document_id="doc-1", page=1, page_size=1)
    assert page["total"] == 1
    assert [item["document_id"] for item in page["items"]] == ["doc-1"]


def test_admin_routes_expose_document_and_task_pagination_defaults():
    calls = []

    class Lifecycle:
        def list_documents_page(self, **kwargs):
            calls.append(("documents", kwargs))
            return {
                "documents": [],
                "total": 0,
                "page": kwargs["page"],
                "page_size": kwargs["page_size"],
                "total_pages": 0,
            }

    class TaskStore:
        def list_page(self, **kwargs):
            calls.append(("tasks", kwargs))
            return {
                "items": [],
                "total": 0,
                "page": kwargs["page"],
                "page_size": kwargs["page_size"],
                "total_pages": 0,
            }

    runtime = type(
        "Runtime",
        (),
        {
            "require_lifecycle": lambda self: Lifecycle(),
            "task_store": TaskStore(),
            "require_admin": lambda self: AdminService(self),
        },
    )()
    app = FastAPI()
    app.include_router(build_admin_router(runtime))

    with TestClient(app) as client:
        documents = client.get("/api/v1/admin/documents?knowledge_type=demand")
        tasks = client.get("/api/v1/admin/tasks?document_id=doc-1")

    assert documents.status_code == tasks.status_code == 200
    assert documents.json()["page_size"] == 20
    assert "task_records" not in documents.json()
    assert calls == [
        (
            "documents",
            {
                "scope_id": None,
                "user_id": None,
                "knowledge_type": "demand",
                "status": None,
                    "search": None,
                    "published_or_admin_only": False,
                "page": 1,
                "page_size": 20,
            },
        ),
        (
            "tasks",
            {
                "operation": None,
                "document_id": "doc-1",
                "page": 1,
                "page_size": 100,
            },
        ),
    ]
