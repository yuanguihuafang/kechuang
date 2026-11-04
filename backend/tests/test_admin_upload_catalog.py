from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from admin.api.v1.admin import build_admin_router
from services.persistence.document_lifecycle import DocumentLifecycleStore
from services.persistence.sqlalchemy import metadata


def _store():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    metadata.create_all(engine)
    return DocumentLifecycleStore(engine=engine)


def test_upload_catalog_fuzzy_search_keeps_all_admin_and_only_published_user_docs():
    store = _store()
    for document_id, source_name, user_id, publication_status in (
        ("admin-draft-1", "Admin planning notes.pdf", "admin", "UNPUBLISHED"),
        ("user-published-1", "Published planning guide.pdf", "user-1", "PUBLISHED"),
        ("user-draft-1", "Unpublished planning draft.pdf", "user-2", "UNPUBLISHED"),
        ("other-1", "Unrelated file.pdf", "user-3", "PUBLISHED"),
    ):
        store.create_version(
            document_id=document_id,
            source_name=source_name,
            storage_key=f"uploads/{document_id}.pdf",
            scope_id="public",
            user_id=user_id,
            version_number=1,
            knowledge_type="solution",
            status="PUBLISHED" if publication_status == "PUBLISHED" else "UPLOADED",
            publication_status=publication_status,
        )

    page = store.list_documents_page(
        knowledge_type="solution",
        search="PLANNING",
        published_or_admin_only=True,
        page=1,
        page_size=1,
    )

    assert page["total"] == 2
    assert page["total_pages"] == 2
    assert len(page["documents"]) == 1

    second_page = store.list_documents_page(
        knowledge_type="solution",
        search="planning",
        published_or_admin_only=True,
        page=2,
        page_size=1,
    )
    assert second_page["total"] == 2
    assert {page["documents"][0]["document_id"], second_page["documents"][0]["document_id"]} == {
        "admin-draft-1",
        "user-published-1",
    }

    by_id = store.list_documents_page(
        search="published-1",
        published_or_admin_only=True,
        page=1,
        page_size=20,
    )
    assert [item["document_id"] for item in by_id["documents"]] == ["user-published-1"]



def test_admin_documents_route_forwards_upload_catalog_filters():
    calls = []

    class Admin:
        def document_catalog(self, **kwargs):
            calls.append(kwargs)
            return {"documents": [], "total": 0, "page": 1, "page_size": 20, "total_pages": 0}

    runtime = type("Runtime", (), {"require_admin": lambda self: Admin()})()
    app = FastAPI()
    app.include_router(build_admin_router(runtime))

    with TestClient(app) as client:
        response = client.get(
            "/api/v1/admin/documents?search=plan&published_or_admin_only=true"
        )

    assert response.status_code == 200
    assert calls == [
        {
            "scope_id": None,
            "user_id": None,
            "knowledge_type": None,
            "status": None,
            "search": "plan",
            "published_or_admin_only": True,
            "page": 1,
            "page_size": 20,
        }
    ]
