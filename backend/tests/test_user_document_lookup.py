from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from assistant.api.v1.documents.lifecycle import build_lifecycle_router


def lifecycle_client(action, *, version_status=None):
    calls = {"list": [], "get": [], "action": []}
    documents = {
        "doc-old": {"user_id": "owner", "active_version_id": "version-old"},
        "doc-other": {"user_id": "other", "active_version_id": "version-other"},
    }

    class Lifecycle:
        def list_documents(self, **kwargs):
            calls["list"].append(kwargs)
            return [{"document_id": f"recent-{index}"} for index in range(200)]

        def get_document(self, document_id, *, user_id):
            calls["get"].append((document_id, user_id))
            document = documents.get(document_id)
            return document if document and document["user_id"] == user_id else None

        def get_version(self, version_id):
            return {
                "version_id": version_id,
                "publication_status": version_status
                or ("UNPUBLISHED" if action == "publish" else "PUBLISHED"),
            }

    async def run_action(*args, **kwargs):
        calls["action"].append((args, kwargs))
        return {"document_id": "doc-old"}, {"task_id": "task-old"}

    app = FastAPI()

    @app.middleware("http")
    async def identity(request, call_next):
        request.state.user_id = "owner"
        return await call_next(request)

    app.include_router(
        build_lifecycle_router(SimpleNamespace(require_lifecycle=lambda: Lifecycle()), run_action)
    )
    return TestClient(app), calls


@pytest.mark.parametrize("action", ["publish", "unpublish"])
def test_lifecycle_finds_owned_document_outside_list_window(action):
    client, calls = lifecycle_client(action)
    with client:
        response = client.post(
            f"/api/v1/me/documents/doc-old/{action}",
            json={"user_id": "other", "reason_code": "OUTDATED"},
        )

    assert response.status_code == 200
    assert response.json() == {
        "document_id": "doc-old",
        "task_id": "task-old",
        **({"reason_label": "内容已过期"} if action == "unpublish" else {}),
    }
    assert calls["get"] == [("doc-old", "owner")]
    assert calls["list"] == []
    assert calls["action"][0][0] == (action, "version-old", "owner")


@pytest.mark.parametrize("action", ["publish", "unpublish"])
@pytest.mark.parametrize("document_id", ["doc-other", "doc-missing"])
def test_lifecycle_rejects_unowned_or_missing_document(action, document_id):
    client, calls = lifecycle_client(action)
    with client:
        response = client.post(
            f"/api/v1/me/documents/{document_id}/{action}",
            json={"user_id": "other", "reason_code": "OUTDATED"},
        )

    assert response.status_code == 404
    assert calls["get"] == [(document_id, "owner")]
    assert calls["list"] == []
    assert calls["action"] == []


@pytest.mark.parametrize(
    ("action", "version_status"), [("publish", "PUBLISHED"), ("unpublish", "UNPUBLISHED")]
)
def test_lifecycle_preserves_publication_state_checks(action, version_status):
    client, calls = lifecycle_client(action, version_status=version_status)
    with client:
        response = client.post(
            f"/api/v1/me/documents/doc-old/{action}", json={"reason_code": "OUTDATED"}
        )

    assert response.status_code == 409
    assert calls["action"] == []
