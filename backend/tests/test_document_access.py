from services.application.document_access import (
    can_delete_attachment,
    can_download_document,
)


def test_admin_router_is_registered_from_admin_api_package():
    from pathlib import Path

    router_source = (Path(__file__).parents[1] / "assistant" / "api" / "v1" / "router.py").read_text(encoding="utf-8")
    main_source = (Path(__file__).parents[1] / "main.py").read_text(encoding="utf-8")

    assert "from admin.api.v1.admin import build_admin_router" not in router_source
    assert "from admin.api.v1.admin import build_admin_router" in main_source


def test_document_permission_routes_publish_openapi_descriptions():
    from admin.api.v1.admin import build_admin_router
    from assistant.api.v1.documents.actions import build_document_actions_router
    from assistant.api.v1.documents.catalog import build_documents_router

    admin_delete = next(
        route for route in build_admin_router(object()).routes
        if route.path == "/api/v1/admin/documents/{document_id}" and "DELETE" in route.methods
    )
    document_delete = next(
        route for route in build_document_actions_router(object(), lambda _: ("", "", None)).routes
        if route.path == "/api/v1/knowledge/documents/{document_id}" and "DELETE" in route.methods
    )
    download = next(
        route for route in build_documents_router(object(), object()).routes
        if route.path == "/api/v1/knowledge/documents/{document_id}/file" and "GET" in route.methods
    )

    assert admin_delete.tags == ["管理员接口"]
    assert admin_delete.summary
    assert admin_delete.description
    assert document_delete.tags == ["文档权限"]
    assert document_delete.summary
    assert document_delete.description
    assert download.tags == ["文档权限"]
    assert download.summary
    assert download.description


def test_external_can_download_public_document_owned_by_another_user():
    assert can_download_document(
        user_role="external",
        requested_user_id="external-1",
        owner_id="external-2",
        scope_id="public",
        publication_status="PUBLISHED",
    )


def test_external_can_delete_only_own_attachment_and_admin_is_allowed():
    records = [{
        "user_id": "external-1",
        "document_id": "doc-1",
        "storage_key": "uploads/doc-1",
    }]

    assert can_delete_attachment(
        user_role="external", user_id="external-1", document_id="doc-1",
        storage_key="uploads/doc-1", records=records,
    )
    assert not can_delete_attachment(
        user_role="external", user_id="external-2", document_id="doc-1",
        storage_key="uploads/doc-1", records=records,
    )
    assert can_delete_attachment(
        user_role="internal", user_id="admin", document_id="doc-1",
        storage_key="uploads/doc-1", records=[],
    )
