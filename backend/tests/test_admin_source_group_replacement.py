from types import SimpleNamespace

from admin.documents import _cleanup_superseded_source_drafts


def test_cleanup_rewrites_only_superseded_rows_in_same_source_group():
    source_id = "source:document"
    rows = [
        {
            "draft_id": draft_id,
            "user_id": "admin",
            "draft_type": "solution",
            "status": "PUBLISHED",
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
            assert user_id == "admin"
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


def test_cleanup_finds_prior_parse_drafts_by_document_when_source_id_changed():
    old = {
        "draft_id": "old-parse",
        "user_id": "admin",
        "draft_type": "solution",
        "status": "PUBLISHED",
        "draft": {
            "source_id": "source:old-task",
            "mysql_record_id": "mysql-old",
            "attachments": [{"document_id": "doc-1"}],
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
            "attachments": [{"document_id": "doc-1"}],
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
        user_id="admin",
        draft_type="solution",
        retained_draft_ids={"new-parse"},
    )

    assert result == {"drafts": 1, "mysql_records": 1, "milvus_records": 1}
    assert deleted == ["old-parse"]
    assert milvus.deleted == ["mysql-old"]
