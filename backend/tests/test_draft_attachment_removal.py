import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from assistant.api.v1.drafts.routes import build_draft_router
from assistant.api.v1.schemas import DraftAttachmentRemovalRequest
from services.persistence.postgres import InMemoryDraftStore
from services.persistence.repositories import workflow_repository


def test_remove_attachment_changes_only_the_selected_draft():
    store = InMemoryDraftStore()
    selected = store.create(
        user_id="user-1",
        user_role="external",
        draft={
            "draft_type": "demand",
            "attachments": [
                {"document_id": "doc-1", "storage_key": "one"},
                {"document_id": "doc-2", "storage_key": "two"},
            ],
            "source_blocks": [{"text": "parsed source"}],
        },
    )
    sibling = store.create(
        user_id="user-1",
        user_role="external",
        draft={"draft_type": "demand", "attachments": [{"document_id": "doc-1"}]},
    )
    runtime = SimpleNamespace(
        require_draft_store=lambda: store,
        require_conversation=lambda: SimpleNamespace(session_store=None),
    )
    endpoint = next(
        route.endpoint
        for route in build_draft_router(runtime, lambda user_id: (user_id, "external", None)).routes
        if route.name == "remove_draft_attachment"
    )
    request = SimpleNamespace(state=SimpleNamespace(user_id="user-1"), headers={})

    result = asyncio.run(
        endpoint(
            selected["draft_id"],
            DraftAttachmentRemovalRequest(document_id="doc-1"),
            request,
        )
    )

    assert result["draft"]["attachments"] == [{"document_id": "doc-2", "storage_key": "two"}]
    assert result["draft"]["source_blocks"] == []
    assert store.get(sibling["draft_id"], user_id="user-1")["draft"]["attachments"] == [
        {"document_id": "doc-1"}
    ]


def test_remove_attachment_requires_a_target():
    store = InMemoryDraftStore()
    record = store.create(user_id="user-1", user_role="external", draft={"draft_type": "demand"})
    endpoint = next(
        route.endpoint
        for route in build_draft_router(
            SimpleNamespace(
                require_draft_store=lambda: store,
                require_conversation=lambda: SimpleNamespace(session_store=None),
            ),
            lambda user_id: (user_id, "external", None),
        ).routes
        if route.name == "remove_draft_attachment"
    )
    request = SimpleNamespace(state=SimpleNamespace(user_id="user-1"), headers={})

    with pytest.raises(HTTPException) as error:
        asyncio.run(endpoint(record["draft_id"], DraftAttachmentRemovalRequest(), request))

    assert error.value.status_code == 422


def test_remove_attachment_rejects_published_draft_without_mutating_it():
    store = InMemoryDraftStore()
    record = store.create(
        user_id="user-1",
        user_role="external",
        draft={"draft_type": "demand", "attachments": [{"document_id": "doc-1"}]},
    )
    store._items[record["draft_id"]]["status"] = "PUBLISHED"
    before = store.get(record["draft_id"], user_id="user-1")

    with pytest.raises(ValueError, match="已发布草稿"):
        store.remove_attachment(record["draft_id"], user_id="user-1", document_id="doc-1")

    assert store.get(record["draft_id"], user_id="user-1") == before


def test_postgres_attachment_removal_locks_and_updates_in_one_transaction(monkeypatch):
    now = datetime.now(timezone.utc)
    row = (
        "draft-1",
        "user-1",
        "external",
        "demand",
        "CONFIRMED",
        None,
        "doc-1",
        "需求",
        {
            "draft_type": "demand",
            "attachments": [{"document_id": "doc-1"}, {"document_id": "doc-2"}],
            "source_blocks": [{"text": "source"}],
            "_draft_version": 4,
        },
        now,
        now,
    )
    saved_row = list(row)
    saved_row[6] = "doc-2"
    saved_row[8] = {
        "draft_type": "demand",
        "attachments": [{"document_id": "doc-2"}],
        "source_blocks": [],
        "_draft_version": 5,
    }

    class Connection:
        def __init__(self):
            self.calls = []

        def exec_driver_sql(self, query, parameters):
            self.calls.append((query, parameters))
            return SimpleNamespace(
                fetchone=lambda: row if len(self.calls) == 1 else tuple(saved_row)
            )

    connection = Connection()

    class ConnectionContext:
        def __enter__(self):
            return connection

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(workflow_repository, "_pooled_connection", lambda _dsn: ConnectionContext())
    store = workflow_repository.PostgresDraftStore("postgresql://test")

    result = store.remove_attachment("draft-1", user_id="user-1", document_id="doc-1")

    assert len(connection.calls) == 2
    assert "FOR UPDATE" in connection.calls[0][0]
    written = json.loads(connection.calls[1][1][0])
    assert written["attachments"] == [{"document_id": "doc-2"}]
    assert written["source_blocks"] == []
    assert written["_draft_version"] == 5
    assert result["version"] == 5
