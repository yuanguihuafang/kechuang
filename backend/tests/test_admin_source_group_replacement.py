from types import SimpleNamespace

import pytest

from admin.documents import _cleanup_superseded_source_drafts


@pytest.fixture(autouse=True)
def _isolate_database_url(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)


def test_cleanup_rewrites_only_superseded_rows_in_same_source_group():
    source_id = "source:document"
    rows = [
        {
            "draft_id": draft_id,
            "user_id": "admin",
            "draft_type": "solution",
            "status": "PUBLISHED",
            "created_at": "2026-11-02" if draft_id.startswith("current") else "2026-11-01",
            "draft": {"source_id": source_id, "mysql_record_id": mysql_id},
        }
        for draft_id, mysql_id in (
            ("current-1", "mysql-1"),
            ("current-2", "mysql-2"),
            ("retry-old", "mysql-old"),
            ("retry-shared", "mysql-protected"),
        )
    ]
    rows.extend(
        [
            {
                "draft_id": "other-source",
                "user_id": "admin",
                "draft_type": "solution",
                "status": "PUBLISHED",
                "draft": {"source_id": "source:other", "mysql_record_id": "mysql-other"},
            },
            {
                "draft_id": "other-user",
                "user_id": "someone-else",
                "draft_type": "solution",
                "status": "PUBLISHED",
                "draft": {"source_id": source_id, "mysql_record_id": "mysql-user"},
            },
        ]
    )
    logical_deleted = []

    class DraftStore:
        def list_for_source_id(self, value):
            return rows if value in {source_id, "source:batch:document"} else []

        def list_business_references(self, _draft_type, _mysql_id, *, user_id):
            assert user_id is None
            return [{"draft_id": "outside-source-group"}] if _mysql_id == "mysql-protected" else []

        def logical_delete(self, draft_id, **kwargs):
            logical_deleted.append((draft_id, kwargs))

    class Sink:
        def __init__(self):
            self.deleted = []

        def find_by_id(self, *, record_type, record_id):
            assert record_type == "solution"
            return record_id

        def delete(self, **kwargs):
            self.deleted.append(kwargs)

    class Milvus:
        def __init__(self):
            self.deleted = []

        def delete_by_mysql_id(self, value):
            self.deleted.append(value)

    sink = Sink()
    milvus = Milvus()
    runtime = SimpleNamespace(
        require_draft_store=lambda: DraftStore(),
        require_business_record_sink=lambda: sink,
        embedded_retrieval=SimpleNamespace(
            agent=SimpleNamespace(public_milvus={"solution": milvus})
        ),
    )

    result = _cleanup_superseded_source_drafts(
        runtime,
        source_id=source_id,
        user_id="admin",
        draft_type="solution",
        retained_draft_ids={"current-1", "current-2"},
    )

    assert result == {"drafts": 2, "mysql_records": 1, "milvus_records": 1}
    assert milvus.deleted == ["mysql-old"]
    assert sink.deleted == [
        {"record_type": "solution", "record_id": "mysql-old", "user_id": "admin"}
    ]
    assert [draft_id for draft_id, _ in logical_deleted] == ["retry-old", "retry-shared"]


@pytest.mark.parametrize("previous_version_id", [None, "doc-1:v1"])
def test_cleanup_requires_explicit_previous_version_when_source_id_changed(previous_version_id):
    old = {
        "draft_id": "old-parse",
        "user_id": "admin",
        "draft_type": "solution",
        "status": "PUBLISHED",
        "draft": {
            "source_id": "source:old-task",
            "mysql_record_id": "mysql-old",
            "attachments": [{"document_id": "doc-1", "version_id": "doc-1:v1"}],
        },
    }
    current = {
        "draft_id": "new-parse",
        "user_id": "admin",
        "draft_type": "solution",
        "status": "PUBLISHED",
        "draft": {
            "source_id": "source:new-task",
            "mysql_record_id": "mysql-new",
            "attachments": [{"document_id": "doc-1", "version_id": "doc-1:v2"}],
        },
    }
    deleted = []

    class DraftStore:
        def list_for_document(self, document_id):
            assert document_id == "doc-1"
            return [old, current]

        def list_for_source_id(self, _source_id):
            return [current]

        def list_business_references(self, *_args, **_kwargs):
            return []

        def logical_delete(self, draft_id, **_kwargs):
            deleted.append(draft_id)

    class Sink:
        def find_by_id(self, **_kwargs):
            return True

        def delete(self, **_kwargs):
            pass

    class Milvus:
        def __init__(self):
            self.deleted = []

        def delete_by_mysql_id(self, mysql_id):
            self.deleted.append(mysql_id)

    milvus = Milvus()
    runtime = SimpleNamespace(
        require_draft_store=lambda: DraftStore(),
        require_business_record_sink=lambda: Sink(),
        embedded_retrieval=SimpleNamespace(
            agent=SimpleNamespace(public_milvus={"solution": milvus})
        ),
    )

    result = _cleanup_superseded_source_drafts(
        runtime,
        source_id="source:new-task",
        document_id="doc-1",
        previous_version_id=previous_version_id,
        user_id="admin",
        draft_type="solution",
        retained_draft_ids={"new-parse"},
    )

    expected_count = int(previous_version_id is not None)
    assert result == {"drafts": expected_count, "mysql_records": expected_count, "milvus_records": expected_count}
    assert deleted == (["old-parse"] if previous_version_id else [])
    assert milvus.deleted == (["mysql-old"] if previous_version_id else [])


@pytest.mark.parametrize("status", ["CONFIRMED", "PENDING_REVIEW"])
def test_cleanup_preserves_unpublished_drafts_in_same_source_group(status):
    from services.persistence.postgres import InMemoryDraftStore

    store = InMemoryDraftStore()
    current = store.create(user_id="admin", user_role="admin", draft={
        "draft_type": "solution", "source_id": "source:task-1", "mysql_record_id": "new",
    })
    store.get(current["draft_id"], user_id="admin")["status"] = "PUBLISHED"
    pending = store.create(user_id="admin", user_role="admin", draft={
        "draft_type": "solution", "source_id": "source:task-1", "mysql_record_id": "pending",
    })
    store.get(pending["draft_id"], user_id="admin")["status"] = status
    store.get(pending["draft_id"], user_id="admin")["created_at"] = "2020-01-01"
    runtime = SimpleNamespace(
        require_draft_store=lambda: store,
        require_business_record_sink=lambda: SimpleNamespace(
            find_by_id=lambda **_: pytest.fail("must not inspect unpublished business rows"),
        ),
        embedded_retrieval=SimpleNamespace(agent=SimpleNamespace(public_milvus={})),
    )

    result = _cleanup_superseded_source_drafts(
        runtime, source_id="source:task-1", user_id="admin", draft_type="solution",
        retained_draft_ids={current["draft_id"]},
    )

    assert result == {"drafts": 0, "mysql_records": 0, "milvus_records": 0}
    assert store.get(pending["draft_id"], user_id="admin")["status"] == status


@pytest.mark.parametrize("protected", ["newer", "missing_date", "shared_document", "other_owner", "business_reference"])
def test_cleanup_preserves_newer_and_shared_resources(protected):
    from services.persistence.postgres import InMemoryDraftStore

    store = InMemoryDraftStore()
    current = store.create(user_id="admin", user_role="admin", draft={
        "draft_type": "solution", "source_id": "source:task", "mysql_record_id": "current",
        "attachments": [{"document_id": "doc-1", "version_id": "doc-1:v2"}],
    })
    current_row = store.get(current["draft_id"], user_id="admin")
    current_row.update(status="PUBLISHED", created_at="2026-11-02")
    owner = "other-user" if protected == "other_owner" else "admin"
    candidate = store.create(user_id=owner, user_role="admin", draft={
        "draft_type": "solution", "source_id": "source:task", "mysql_record_id": "old",
        "attachments": [{"document_id": "doc-1", "version_id": "doc-1:v1"}],
    })
    row = store.get(candidate["draft_id"], user_id=owner)
    row.update(status="PUBLISHED", created_at="2026-11-01")
    if protected == "newer":
        row["created_at"] = "2026-11-03"
    elif protected == "missing_date":
        row.pop("created_at")
    elif protected == "shared_document":
        row["draft"]["attachments"].append({"document_id": "doc-other"})
    elif protected == "business_reference":
        store.create(user_id="other-user", user_role="internal", draft={
            "draft_type": "solution", "source_id": "source:other", "mysql_record_id": "old",
        })
    deleted_mysql = []
    deleted_vectors = []
    runtime = SimpleNamespace(
        require_draft_store=lambda: store,
        require_business_record_sink=lambda: SimpleNamespace(
            find_by_id=lambda **_: True, delete=lambda **kwargs: deleted_mysql.append(kwargs),
        ),
        embedded_retrieval=SimpleNamespace(agent=SimpleNamespace(public_milvus={
            "solution": SimpleNamespace(delete_by_mysql_id=deleted_vectors.append),
        })),
    )

    _cleanup_superseded_source_drafts(
        runtime, source_id="source:task", document_id="doc-1", user_id="admin",
        draft_type="solution", retained_draft_ids={current["draft_id"]},
    )

    assert deleted_mysql == []
    assert deleted_vectors == []
    assert store.get(candidate["draft_id"], user_id=owner)["status"] == (
        "DELETED" if protected == "business_reference" else "PUBLISHED"
    )
