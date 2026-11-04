import asyncio
import json
from types import SimpleNamespace

import pytest

from assistant.api.v1.drafts.routes import build_draft_router
from assistant.api.v1.schemas import (
    DraftCreateRequest,
    DraftUpdateRequest,
)
from services.persistence.postgres import InMemoryDraftStore, _merge_draft
from services.persistence.repositories import workflow_repository


@pytest.mark.parametrize("replace_fields", [False, True])
@pytest.mark.parametrize("field_group", ["fields", "mysql_fields"])
@pytest.mark.parametrize("empty_value", ["", None, [], {}])
def test_explicit_field_clear_keeps_absent_keys(replace_fields, field_group, empty_value):
    result = _merge_draft(
        {field_group: {"contect": "OLD", "title": "KEEP"}},
        {field_group: {"contect": empty_value}},
        replace_fields=replace_fields,
    )
    assert result[field_group] == {
        "contect": empty_value if replace_fields else "OLD", "title": "KEEP",
    }


@pytest.mark.parametrize("postgres", [False, True])
def test_store_update_forwards_explicit_field_clear(monkeypatch, postgres):
    initial = {"draft_type": "solution", "fields": {"contect": "OLD", "title": "KEEP"},
               "mysql_fields": {"contect": "OLD", "title": "KEEP"}}
    if postgres:
        store = workflow_repository.PostgresDraftStore("postgresql://test")
        current = {"draft_id": "draft-1", "version": 1, "draft": initial}

        class Connection:
            def exec_driver_sql(self, query, parameters):
                current["draft"] = json.loads(parameters[0])
                return SimpleNamespace(rowcount=1)

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        monkeypatch.setattr(workflow_repository, "_pooled_connection", lambda _: Connection())
        monkeypatch.setattr(store, "get", lambda *a, **kw: current)
    else:
        store = InMemoryDraftStore()
        current = store.create(user_id="user-1", user_role="internal", draft=initial)
    result = store.update(
        current["draft_id"], user_id="user-1", replace_fields=True,
        draft={"fields": {"contect": ""}, "mysql_fields": {"contect": ""}},
    )
    assert result["draft"]["fields"] == {"contect": "", "title": "KEEP"}
    assert result["draft"]["mysql_fields"] == {"contect": "", "title": "KEEP"}


@pytest.fixture(autouse=True)
def isolate_audit_database(monkeypatch):
    monkeypatch.setattr("services.application.drafts._audit_user_event", lambda *args, **kwargs: None)


def test_create_draft_binds_only_current_users_session_upload_attachments(monkeypatch):
    monkeypatch.setattr("services.application.drafts._audit_user_event", lambda *a, **kw: None)
    uploaded = {
        "document_id": "doc-current",
        "version_id": "version-current",
        "file_name": "需求资料.pdf",
        "storage_key": "user-1/doc-current.pdf",
        "temporary": True,
        "expires_at": "2099-01-01T00:00:00+00:00",
    }

    class TaskStore:
        def list(self, *, operation, limit):
            assert operation == "user_draft"
            assert limit >= 1
            return [
                {
                    "operation": "user_draft",
                    "status": "PARSING",
                    "user_id": "user-1",
                    "thread_id": "thread-1",
                    "attachments": [uploaded],
                },
                {
                    "operation": "user_draft",
                    "status": "WAITING_DRAFT_CONFIRM",
                    "user_id": "user-2",
                    "thread_id": "thread-1",
                    "attachments": [{**uploaded, "document_id": "doc-other-user"}],
                },
                {
                    "operation": "user_draft",
                    "status": "WAITING_DRAFT_CONFIRM",
                    "user_id": "user-1",
                    "thread_id": "thread-2",
                    "attachments": [{**uploaded, "document_id": "doc-other-thread"}],
                },
                {
                    "operation": "user_draft",
                    "status": "FAILED",
                    "user_id": "user-1",
                    "thread_id": "thread-1",
                    "attachments": [{**uploaded, "document_id": "doc-failed"}],
                },
            ]

    store = InMemoryDraftStore()
    runtime = SimpleNamespace(
        require_draft_store=lambda: store,
        require_file_workflow=lambda: SimpleNamespace(task_store=TaskStore()),
        require_conversation=lambda: None,
    )
    endpoint = next(
        route.endpoint
        for route in build_draft_router(runtime, lambda user_id: (user_id, "external", None)).routes
        if route.name == "create_draft"
    )
    request = SimpleNamespace(state=SimpleNamespace(user_id="user-1"), headers={})

    result = asyncio.run(
        endpoint(
            DraftCreateRequest(
                thread_id="thread-1",
                draft={"draft_type": "demand", "title": "智能设备采购"},
            ),
            request,
        )
    )

    assert result["draft"]["attachments"] == [
        {
            "document_id": "doc-current",
            "version_id": "version-current",
            "file_name": "需求资料.pdf",
            "storage_key": "user-1/doc-current.pdf",
        }
    ]


@pytest.mark.parametrize(
    ("incoming_attachments", "expected_attachments"),
    [
        ([], []),
        (None, [{"document_id": "doc-existing", "file_name": "existing.pdf"}]),
    ],
)
def test_update_draft_replaces_only_explicit_attachment_list(
    incoming_attachments, expected_attachments
):
    store = InMemoryDraftStore()
    existing_attachment = {"document_id": "doc-existing", "file_name": "existing.pdf"}
    record = store.create(
        user_id="user-1",
        user_role="external",
        draft={"draft_type": "demand", "title": "需求", "attachments": [existing_attachment]},
    )
    runtime = SimpleNamespace(require_draft_store=lambda: store, require_conversation=lambda: None)
    endpoint = next(
        route.endpoint
        for route in build_draft_router(runtime, lambda user_id: (user_id, "external", None)).routes
        if route.name == "update_draft"
    )
    request = SimpleNamespace(state=SimpleNamespace(user_id="user-1"), headers={})
    updated_draft = {"draft_type": "demand", "title": "需求已编辑"}
    if incoming_attachments is not None:
        updated_draft["attachments"] = incoming_attachments

    result = asyncio.run(
        endpoint(record["draft_id"], DraftUpdateRequest(draft=updated_draft), request)
    )

    assert result["draft"].get("attachments", []) == expected_attachments


def test_admin_confirmed_source_blocks_replace_old_attachment_text():
    store = InMemoryDraftStore()
    old_block = {"block_id": "page-1", "text": "旧原文"}
    edited_block = {"block_id": "page-1", "text": "管理员修改后的原文"}
    record = store.create(
        user_id="admin",
        user_role="admin",
        draft={
            "draft_type": "solution",
            "attachments": [{"document_id": "doc-1"}],
            "source_blocks": [old_block],
        },
    )

    updated = store.update(
        record["draft_id"],
        user_id="admin",
        draft={"source_blocks": [edited_block]},
        replace_source_blocks=True,
    )

    assert updated["draft"]["source_blocks"] == [edited_block]


def test_incremental_source_block_updates_still_append_by_default():
    store = InMemoryDraftStore()
    old_block = {"block_id": "page-1", "text": "旧原文"}
    new_block = {"block_id": "page-2", "text": "补充原文"}
    record = store.create(
        user_id="user-1",
        user_role="external",
        draft={"draft_type": "solution", "source_blocks": [old_block]},
    )

    updated = store.update(
        record["draft_id"],
        user_id="user-1",
        draft={"source_blocks": [new_block]},
    )

    assert updated["draft"]["source_blocks"] == [old_block, new_block]


def test_postgres_draft_store_persists_explicit_attachment_removal(monkeypatch):
    attachment = {"document_id": "doc-existing", "file_name": "existing.pdf"}
    current = {
        "draft_id": "draft-1",
        "user_id": "user-1",
        "draft_type": "demand",
        "status": "CONFIRMED",
        "version": 3,
        "draft": {"draft_type": "demand", "title": "需求", "attachments": [attachment]},
    }
    updated = {
        **current,
        "version": 4,
        "draft": {"draft_type": "demand", "title": "需求", "attachments": []},
    }
    reads = iter([current, updated])
    store = workflow_repository.PostgresDraftStore("postgresql://test")
    monkeypatch.setattr(store, "get", lambda *args, **kwargs: next(reads))

    class Connection:
        parameters = None

        def exec_driver_sql(self, _query, parameters):
            self.parameters = parameters
            return SimpleNamespace(rowcount=1)

    connection = Connection()

    class ConnectionContext:
        def __enter__(self):
            return connection

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(workflow_repository, "_pooled_connection", lambda _dsn: ConnectionContext())

    result = store.update(
        "draft-1",
        user_id="user-1",
        draft={"draft_type": "demand", "attachments": []},
        expected_version=3,
    )

    assert json.loads(connection.parameters[0])["attachments"] == []
    assert connection.parameters[3] is None
    assert result["draft"]["attachments"] == []


def test_postgres_draft_store_replaces_admin_confirmed_source_blocks(monkeypatch):
    old_block = {"block_id": "page-1", "text": "旧原文"}
    edited_block = {"block_id": "page-1", "text": "管理员修改后的原文"}
    current = {
        "draft_id": "draft-1",
        "user_id": "admin",
        "draft_type": "solution",
        "status": "CONFIRMED",
        "version": 3,
        "draft": {"draft_type": "solution", "source_blocks": [old_block]},
    }
    updated = {
        **current,
        "version": 4,
        "draft": {"draft_type": "solution", "source_blocks": [edited_block]},
    }
    reads = iter([current, updated])
    store = workflow_repository.PostgresDraftStore("postgresql://test")
    monkeypatch.setattr(store, "get", lambda *args, **kwargs: next(reads))

    class Connection:
        parameters = None

        def exec_driver_sql(self, _query, parameters):
            self.parameters = parameters
            return SimpleNamespace(rowcount=1)

    connection = Connection()

    class ConnectionContext:
        def __enter__(self):
            return connection

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(workflow_repository, "_pooled_connection", lambda _dsn: ConnectionContext())

    result = store.update(
        "draft-1",
        user_id="admin",
        draft={"draft_type": "solution", "source_blocks": [edited_block]},
        replace_source_blocks=True,
        expected_version=3,
    )

    assert json.loads(connection.parameters[0])["source_blocks"] == [edited_block]
    assert result["draft"]["source_blocks"] == [edited_block]


@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("tagged", [False, True])
def test_new_document_version_replaces_only_its_old_blocks(explicit, tagged):
    old = {"text": "OLD", "document_id": "doc-a", "version_id": "v1"}
    other = {"text": "OTHER", "document_id": "doc-b", "version_id": "b1"}
    new = {"text": "NEW"}
    if tagged:
        new.update(document_id="doc-a", version_id="v2")
    existing = {
        "attachments": [
            {"document_id": "doc-a", "version_id": "v1"},
            {"document_id": "doc-b", "version_id": "b1"},
        ],
        "source_blocks": [old, other],
    }
    incoming = {
        "attachments": [{"document_id": "doc-a", "version_id": "v2"}],
        "source_blocks": [new],
    }
    result = _merge_draft(existing, incoming, replace_source_blocks=explicit)
    assert result["source_blocks"] == [other, new]
    assert existing["source_blocks"] == [old, other]


def test_block_version_alone_replaces_document_history():
    old = {"text": "OLD", "document_id": "doc-a", "version_id": "v1"}
    new = {"text": "NEW", "document_id": "doc-a", "version_id": "v2"}
    assert _merge_draft({"source_blocks": [old]}, {"source_blocks": [new]})[
        "source_blocks"
    ] == [new]


def test_explicit_same_version_edit_preserves_other_attachment_blocks():
    old = {"text": "OLD", "document_id": "doc-a", "version_id": "v1"}
    other = {"text": "OTHER", "document_id": "doc-b", "version_id": "b1"}
    new = {**old, "text": "EDITED"}
    assert _merge_draft(
        {"source_blocks": [old, other]},
        {"source_blocks": [new]},
        replace_source_blocks=True,
    )["source_blocks"] == [other, new]


def test_ambiguous_historical_blocks_reject_replacement():
    existing = {
        "attachments": [{"document_id": "doc-a", "version_id": "v1"},
                        {"document_id": "doc-b", "version_id": "b1"}],
        "source_blocks": [{"text": "UNKNOWN OWNER"}],
    }
    with pytest.raises(ValueError, match="source_blocks"):
        _merge_draft(existing, {
            "attachments": [{"document_id": "doc-a", "version_id": "v2"}],
            "source_blocks": [{"text": "NEW"}],
        })


def test_single_document_legacy_blocks_can_be_replaced_and_cleared():
    existing = {
        "attachments": [{"document_id": "doc-a", "version_id": "v1"}],
        "source_blocks": [{"text": "OLD"}],
    }
    assert _merge_draft(existing, {
        "attachments": [{"document_id": "doc-a", "version_id": "v2"}],
        "source_blocks": [],
    })["source_blocks"] == []


def test_new_version_without_source_blocks_fails_before_save():
    with pytest.raises(ValueError, match="source_blocks"):
        _merge_draft({
            "attachments": [{"document_id": "doc-a", "version_id": "v1"}],
            "source_blocks": [{"text": "OLD"}],
        }, {"attachments": [{"document_id": "doc-a", "version_id": "v2"}]})


def test_incoming_old_and_new_versions_are_rejected():
    with pytest.raises(ValueError, match="conflicting document versions"):
        _merge_draft({}, {"source_blocks": [
            {"text": "OLD", "document_id": "doc-a", "version_id": "v1"},
            {"text": "NEW", "document_id": "doc-a", "version_id": "v2"},
        ]})


def test_unowned_history_cannot_be_appended_to_a_versioned_document():
    with pytest.raises(ValueError, match="ownership is ambiguous"):
        _merge_draft({"source_blocks": [{"text": "UNKNOWN"}]}, {
            "attachments": [{"document_id": "doc-a", "version_id": "v2"}],
            "source_blocks": [{"text": "NEW"}],
        })


def test_partially_tagged_history_does_not_guess_unknown_block_owner():
    with pytest.raises(ValueError, match="ownership is ambiguous"):
        _merge_draft({
            "attachments": [{"document_id": "doc-a", "version_id": "v1"}],
            "source_blocks": [{"text": "UNKNOWN"}, {"text": "OTHER", "document_id": "doc-b"}],
        }, {
            "attachments": [{"document_id": "doc-a", "version_id": "v2"}],
            "source_blocks": [{"text": "NEW"}],
        })


def test_conflicting_block_identity_is_rejected():
    with pytest.raises(ValueError, match="conflicting document_id"):
        _merge_draft({}, {"source_blocks": [{
            "document_id": "doc-a", "metadata": {"document_id": "doc-b"}, "text": "CONFLICT",
        }]})


def test_version_metadata_resolves_old_block_owner():
    old = {"text": "OLD", "metadata": {"version_id": "v1"}}
    other = {"text": "OTHER", "metadata": {"version_id": "b1"}}
    new = {"text": "NEW", "metadata": {"document_id": "doc-a", "version_id": "v2"}}
    assert _merge_draft({
        "attachments": [{"document_id": "doc-a", "version_id": "v1"},
                        {"document_id": "doc-b", "version_id": "b1"}],
        "source_blocks": [old, other],
    }, {"source_blocks": [new]})["source_blocks"] == [other, new]


@pytest.mark.parametrize("postgres", [False, True])
def test_store_new_version_replaces_without_changing_sibling_mysql_identity(monkeypatch, postgres):
    initial = {
        "draft_type": "solution", "source_id": "shared-source", "mysql_record_id": "mysql-1",
        "attachments": [{"document_id": "doc-a", "version_id": "v1"}],
        "source_blocks": [{"text": "OLD"}],
    }
    sibling_draft = {**initial, "mysql_record_id": "mysql-2"}
    if postgres:
        store = workflow_repository.PostgresDraftStore("postgresql://test")
        current = {"draft_id": "draft-1", "version": 1, "draft": initial}

        class Connection:
            def exec_driver_sql(self, query, parameters):
                assert query.startswith("UPDATE platform_drafts")
                current["draft"] = json.loads(parameters[0])
                return SimpleNamespace(rowcount=1)

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        monkeypatch.setattr(workflow_repository, "_pooled_connection", lambda _dsn: Connection())
        monkeypatch.setattr(store, "get", lambda *args, **kwargs: current)
    else:
        store = InMemoryDraftStore()
        current, sibling = store.create_many(
            user_id="user-1", user_role="internal", drafts=[initial, sibling_draft],
        )
    new = {"text": "NEW"}
    result = store.update(current["draft_id"], user_id="user-1", draft={
        "attachments": [{"document_id": "doc-a", "version_id": "v2"}],
        "source_blocks": [new],
    })
    assert result["draft"]["source_blocks"] == [new]
    assert result["draft"]["mysql_record_id"] == "mysql-1"
    assert result["draft"]["source_id"] == "shared-source"
    if not postgres:
        rows = store.list_for_source_id("shared-source")
        assert len(rows) == 2
        assert {row["draft"]["mysql_record_id"] for row in rows} == {"mysql-1", "mysql-2"}
        assert store.get(sibling["draft_id"], user_id="user-1")["draft"] == sibling_draft
