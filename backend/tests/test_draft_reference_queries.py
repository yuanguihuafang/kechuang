import json
import sqlite3
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from services.persistence.postgres import InMemoryDraftStore
from services.persistence.repositories import workflow_repository


@pytest.fixture(params=["postgres", "memory"])
def reference_store(monkeypatch, request):
    """Exercise both stores with identical rows, adapting PostgreSQL JSON syntax in memory."""
    memory_store = InMemoryDraftStore() if request.param == "memory" else None
    database = sqlite3.connect(":memory:")
    database.execute(
        """CREATE TABLE platform_drafts (
            draft_id TEXT, user_id TEXT, user_role TEXT, draft_type TEXT, status TEXT,
            thread_id TEXT, source_document_id TEXT, draft_title TEXT, content TEXT,
            created_at TEXT, updated_at TEXT
        )"""
    )

    class Connection:
        def exec_driver_sql(self, query, parameters):
            assert query.lstrip().startswith("SELECT")
            query = query.replace(
                "content @> %s::jsonb",
                """EXISTS (
                    SELECT 1 FROM json_each(platform_drafts.content, '$.attachments') AS attachment,
                                  json_each(%s, '$.attachments[0]') AS filter
                    WHERE json_extract(attachment.value, '$.' || filter.key) = filter.value
                )""",
            ).replace("content->>'mysql_record_id'", "CAST(content->>'mysql_record_id' AS TEXT)")
            rows = database.execute(query.replace("%s", "?"), parameters).fetchall()
            return SimpleNamespace(
                fetchall=lambda: [(*row[:8], json.loads(row[8]), *row[9:]) for row in rows]
            )

    @contextmanager
    def connection(_dsn):
        yield Connection()

    monkeypatch.setattr(workflow_repository, "_pooled_connection", connection)

    def add(
        draft_id,
        *,
        draft_type="demand",
        user_id="owner",
        status="CONFIRMED",
        mysql_id="42",
        source_document_id=None,
        attachments=None,
    ):
        if memory_store is not None:
            record = memory_store.create(
                user_id=user_id,
                user_role="internal",
                draft={
                    "draft_type": draft_type,
                    "mysql_record_id": mysql_id,
                    "attachments": attachments or [],
                },
                thread_id="thread",
            )
            memory_store._items[draft_id] = {
                **memory_store._items.pop(record["draft_id"]),
                "draft_id": draft_id,
                "status": status,
                "source_document_id": source_document_id,
                "created_at": "2026-10-01",
                "updated_at": "2026-10-06",
            }
            return
        database.execute(
            "INSERT INTO platform_drafts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                draft_id,
                user_id,
                "internal",
                draft_type,
                status,
                "thread",
                source_document_id,
                "title",
                json.dumps(
                    {
                        "draft_type": draft_type,
                        "mysql_record_id": mysql_id,
                        "attachments": attachments or [],
                    }
                ),
                "2026-10-01",
                "2026-10-06",
            ),
        )

    store = (
        memory_store
        if memory_store is not None
        else workflow_repository.PostgresDraftStore("unused-test-dsn")
    )
    yield store, add
    database.close()


def test_document_references_are_complete_beyond_500(reference_store):
    store, add = reference_store
    for index in range(500):
        add(f"draft-{index}", source_document_id="doc-old")
    add("attachment-only", attachments=[{"document_id": "other"}, {"document_id": "doc-old"}])
    add("deleted", status="DELETED", source_document_id="doc-old")
    add("unrelated", source_document_id="other")

    references = store.list_for_document("doc-old")

    assert len(references) == 501
    assert {row["draft_id"] for row in references} == {
        *(f"draft-{index}" for index in range(500)),
        "attachment-only",
    }


@pytest.mark.parametrize(
    "draft_type", ["demand", "solution", "achievement", "enterprise", "expert"]
)
def test_business_references_are_typed_and_owner_scoped(reference_store, draft_type):
    store, add = reference_store
    for status in ("CONFIRMED", "PENDING_REVIEW", "PUBLISHED", "WITHDRAWN"):
        add(status, draft_type=draft_type, user_id="other", status=status)
    add("numeric-id", draft_type=draft_type, mysql_id=42)
    add("deleted", draft_type=draft_type, status="DELETED")
    add("different-type", draft_type="customer_visit")
    add("different-id", draft_type=draft_type, mysql_id="43")

    references = store.list_business_references(draft_type, "42", user_id=None)

    assert {row["draft_id"] for row in references} == {
        "CONFIRMED",
        "PENDING_REVIEW",
        "PUBLISHED",
        "WITHDRAWN",
        "numeric-id",
    }
    assert all(row["draft"]["mysql_record_id"] in ("42", 42) for row in references)


def test_private_business_references_require_matching_owner(reference_store):
    store, add = reference_store
    add("own", draft_type="customer_visit", status="WITHDRAWN")
    add("other-owner", draft_type="customer_visit", user_id="other")
    add("other-type")
    add("deleted", draft_type="customer_visit", status="DELETED")

    references = store.list_business_references("customer_visit", "42", user_id="owner")

    assert [row["draft_id"] for row in references] == ["own"]


@pytest.mark.parametrize("storage_key", ["uploads/shared.pdf", "uploads/' OR 1=1 --.pdf"])
def test_storage_key_references_are_complete_and_global_without_document_id(
    reference_store, storage_key
):
    store, add = reference_store
    for index in range(500):
        add(
            f"file-{index}",
            user_id=f"owner-{index}",
            draft_type="customer_visit" if index % 2 else "demand",
            status=("CONFIRMED", "PENDING_REVIEW", "PUBLISHED", "WITHDRAWN")[index % 4],
            attachments=[{"storage_key": storage_key}],
        )
    add(
        "second-attachment",
        draft_type="expert",
        attachments=[{"storage_key": "other"}, {"storage_key": storage_key}],
    )
    add("deleted", status="DELETED", attachments=[{"storage_key": storage_key}])
    add("different-key", attachments=[{"storage_key": f"{storage_key}.other"}])
    add("no-attachment")

    references = store.list_storage_key_references(storage_key)

    assert len(references) == 501
    assert {row["draft_id"] for row in references} == {
        *(f"file-{index}" for index in range(500)),
        "second-attachment",
    }


@pytest.mark.parametrize("user_id", [None, "", " "])
def test_private_business_reference_query_rejects_missing_owner(reference_store, user_id):
    store, _ = reference_store
    with pytest.raises(ValueError, match="user_id"):
        store.list_business_references("customer_visit", "42", user_id=user_id)


def test_reference_query_failure_propagates(monkeypatch):
    @contextmanager
    def broken_connection(_dsn):
        raise RuntimeError("reference read failed")
        yield

    monkeypatch.setattr(workflow_repository, "_pooled_connection", broken_connection)
    store = workflow_repository.PostgresDraftStore("unused-test-dsn")
    with pytest.raises(RuntimeError, match="reference read failed"):
        store.list_for_document("doc-old")
    with pytest.raises(RuntimeError, match="reference read failed"):
        store.list_business_references("demand", "42", user_id=None)
    with pytest.raises(RuntimeError, match="reference read failed"):
        store.list_storage_key_references("uploads/shared.pdf")
