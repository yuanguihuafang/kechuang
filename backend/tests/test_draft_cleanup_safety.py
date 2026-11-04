import asyncio
from types import SimpleNamespace

import pytest

from admin.service import AdminService
from services.application import draft_publishing


def test_new_business_source_id_has_no_mysql_prefix_and_preserves_digest():
    import hashlib
    from services.application.business_records import stable_source_id

    expected = "source:" + hashlib.sha256(b"511").hexdigest()[:32]
    assert stable_source_id("511") == expected
    assert stable_source_id(" 511 ") == expected
    assert stable_source_id("514") != expected


def record(draft_id, *, status="WITHDRAWN", draft_type="solution", mysql_id="7", user_id="u"):
    return {
        "draft_id": draft_id, "status": status, "draft_type": draft_type,
        "user_id": user_id, "draft": {
            "mysql_record_id": mysql_id,
            "attachments": [{"document_id": "doc-shared", "storage_key": "uploads/shared.pdf"}],
        },
    }


class References:
    def __init__(self, rows):
        self.rows = rows

    def list_business_references(self, draft_type, mysql_record_id, *, user_id):
        return [item for item in self.rows if item["draft_type"] == draft_type
                and item["draft"]["mysql_record_id"] == mysql_record_id
                and (user_id is None or item["user_id"] == user_id)]

    def list_for_document(self, document_id):
        return self.rows

    def list_storage_key_references(self, storage_key):
        return self.rows


def test_cleanup_keeps_shared_business_vectors_and_files():
    a, b = record("a"), record("b", status="PUBLISHED")
    plan = draft_publishing.plan_draft_cleanup(References([a, b]), [a])
    assert plan["business_keys"] == set()
    assert plan["source_keys"] == set()
    assert plan["files"] == set()
    assert plan["document_ids"] == set()


def test_business_key_does_not_confuse_types_or_private_owners():
    a = record("a", draft_type="customer_visit", user_id="u-a")
    b = record("b", draft_type="customer_visit", user_id="u-b")
    plan = draft_publishing.plan_draft_cleanup(References([a, b]), [a])
    assert plan["business_keys"] == {("customer_visit", "7", "u-a")}


def test_published_target_rejected_before_any_external_write():
    with pytest.raises(ValueError, match="发布"):
        draft_publishing.plan_draft_cleanup(References([]), [record("a", status="PUBLISHED")])


def test_published_target_can_be_cleaned_when_session_delete_allows_it():
    item = record("a", status="PUBLISHED")

    plan = draft_publishing.plan_draft_cleanup(
        References([item]), [item], allow_published_targets=True
    )

    assert plan["business_keys"] == {("solution", "7", None)}
    assert len(plan["source_keys"]) == 1
    assert next(iter(plan["source_keys"]))[0] == "solution"
    assert plan["files"] == {("uploads/shared.pdf", "doc-shared")}
    assert plan["document_ids"] == {"doc-shared"}


def test_cleanup_prefers_persisted_source_id_over_legacy_attachment_hash():
    item = record("a", status="PUBLISHED")
    item["draft"]["source_id"] = "source:mysql:stable-record-key"

    plan = draft_publishing.plan_draft_cleanup(
        References([item]), [item], allow_published_targets=True
    )

    assert plan["source_keys"] == {
        ("solution", "source:mysql:stable-record-key", None)
    }


def test_cleanup_compares_legacy_batch_source_id_with_canonical_id():
    target, shared = record("a", status="PUBLISHED"), record("b", status="PUBLISHED")
    target["draft"]["source_id"] = "source:batch:shared"
    shared["draft"]["source_id"] = "source:shared"

    plan = draft_publishing.plan_draft_cleanup(
        References([target, shared]), [target], allow_published_targets=True
    )

    assert plan["source_keys"] == set()


def test_admin_delete_uses_legacy_source_id_for_vectors_but_canonical_cleanup_key():
    item = record("a", status="PUBLISHED")
    item["draft"]["source_id"] = "source:batch:legacy"
    item["draft"]["attachments"] = []
    rows = References([item])
    rows.get_admin = lambda _draft_id: item
    rows.list_for_source_id = lambda source_id: [item] if source_id == "source:batch:legacy" else []
    rows.logical_delete = lambda *_args, **_kwargs: item
    rows.purge_deleted = lambda _draft_id: True
    deleted = []
    target = SimpleNamespace(delete_by_source_id=lambda source_id, **_kwargs: deleted.append(source_id))
    runtime = SimpleNamespace(
        task_store=None,
        require_draft_store=lambda: rows,
        embedded_retrieval=SimpleNamespace(
            agent=SimpleNamespace(public_milvus={"solution": target})
        ),
        business_record_sink=SimpleNamespace(delete=lambda **_kwargs: None),
    )

    result = asyncio.run(AdminService(runtime).delete_draft("a"))

    assert result["status"] == "PURGED"
    assert deleted == ["source:batch:legacy"]


def test_session_delete_keeps_published_data_referenced_outside_session():
    target, shared = (
        record("target", status="PUBLISHED"),
        record("other-session", status="PUBLISHED"),
    )

    plan = draft_publishing.plan_draft_cleanup(
        References([target, shared]), [target], allow_published_targets=True
    )

    assert plan["business_keys"] == set()
    assert plan["source_keys"] == set()
    assert plan["files"] == set()
    assert plan["document_ids"] == set()


def test_admin_delete_protects_shared_mysql_record():
    a, b = record("a"), record("b", status="PUBLISHED")
    rows = References([a, b])
    rows.get_admin = lambda draft_id: a
    rows.list_all = lambda **kwargs: [a, b]
    rows.logical_delete = lambda *args, **kwargs: a
    rows.purge_deleted = lambda draft_id: True
    target = SimpleNamespace(
        delete_by_mysql_id=lambda *args, **kwargs: pytest.fail("shared vectors deleted"),
        delete_by_source_id=lambda *args, **kwargs: pytest.fail("shared source deleted"),
    )
    runtime = SimpleNamespace(
        task_store=None, require_draft_store=lambda: rows,
        embedded_retrieval=SimpleNamespace(agent=SimpleNamespace(public_milvus={"solution": target})),
        business_record_sink=SimpleNamespace(delete=lambda **kwargs: pytest.fail("shared MySQL deleted")),
    )
    result = asyncio.run(AdminService(runtime).delete_draft("a"))
    assert result["status"] == "PURGED"
    assert result["document_count"] == 0


def test_lookup_failure_before_mysql_deletion():
    a = record("a")
    rows = References([a])
    rows.get_admin = lambda draft_id: a
    rows.list_all = lambda **kwargs: [a]

    def unavailable(*args, **kwargs):
        raise RuntimeError("references unavailable")

    rows.list_business_references = unavailable
    runtime = SimpleNamespace(
        task_store=None, require_draft_store=lambda: rows,
        business_record_sink=SimpleNamespace(delete=lambda **kwargs: pytest.fail("deleted before lookup")),
    )
    with pytest.raises(RuntimeError, match="references unavailable"):
        asyncio.run(AdminService(runtime).delete_draft("a"))


def test_document_source_lookup_finishes_before_mysql_delete():
    a = record("a")
    rows = References([a])
    rows.get_admin = lambda draft_id: a
    effects = []

    def unavailable(document_id):
        raise RuntimeError("document versions unavailable")

    async def unpublish_document(*args):
        return SimpleNamespace(status="SUCCESS")

    runtime = SimpleNamespace(
        task_store=None, require_draft_store=lambda: rows,
        lifecycle_service=SimpleNamespace(list_versions=unavailable),
        embedded_retrieval=SimpleNamespace(agent=SimpleNamespace(public_milvus={}, unpublish_document=unpublish_document)),
        business_record_sink=SimpleNamespace(delete=lambda **kwargs: effects.append("mysql")),
    )
    with pytest.raises(RuntimeError, match="document versions unavailable"):
        asyncio.run(AdminService(runtime).delete_draft("a"))
    assert effects == []
