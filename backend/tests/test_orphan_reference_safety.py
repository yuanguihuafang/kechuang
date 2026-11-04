"""Orphan cleanup must see every reference and fail closed on lookup failure."""

import asyncio
from contextlib import contextmanager, nullcontext
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from services.persistence import retention
from services.storage.minio.minio_storage import MinioUploadService


def test_references_include_history_and_confirmed_body():
    refs = retention.collect_storage_references(
        documents=[{
            "document_id": "doc-current", "storage_key": "uploads/current.pdf",
            "version_history": [{"storage_key": "uploads/old.pdf"}],
            "artifact_refs": {"confirmed_index_request": {
                "document_id": "doc-current", "storage_key": "derived/doc-current/confirmed.json",
            }},
        }],
        drafts=[{"draft": {"attachments": [{"document_id": "doc-linked", "storage_key": "uploads/linked.pdf"}]}}],
        tasks=[{"state": {"document_id": "doc-task", "index_request_ref": "derived/doc-task/request.json"}}],
    )
    assert refs.document_ids == {"doc-current", "doc-linked", "doc-task"}
    assert refs.storage_keys == {
        "uploads/current.pdf", "uploads/old.pdf", "uploads/linked.pdf",
        "derived/doc-current/confirmed.json", "derived/doc-task/request.json",
    }


def test_reference_query_keeps_live_history_when_current_version_is_purged():
    sql = retention._STORAGE_REFERENCES_SQL
    # 历史记录的筛选必须独立于当前记录的状态。
    assert "CROSS JOIN LATERAL jsonb_array_elements(version_history)" in sql
    assert "version->>'status' IS DISTINCT FROM 'PURGED'" in sql


def test_daily_cleanup_is_scheduled_for_beijing_0200():
    from zoneinfo import ZoneInfo

    cleanup_zone = ZoneInfo("Asia/Shanghai")
    now = datetime(2026, 10, 20, 1, 59, tzinfo=cleanup_zone)
    assert retention.seconds_until_daily_cleanup(now) == 60
    assert retention.seconds_until_daily_cleanup(
        datetime(2026, 10, 20, 2, 1, tzinfo=cleanup_zone)
    ) == 23 * 60 * 60 + 59 * 60


def test_references_include_direct_artifact_keys_and_nested_lists():
    refs = retention.collect_storage_references(
        documents=[{"artifact_refs": {"parse_result": "derived/other/result.json",
                                     "manifest": "derived/other/manifest.json"}}],
        drafts=[{"attachments": [{"storage_key": "uploads/draft.pdf"}]}],
        tasks=[{"artifact_ref": {"parse_result_key": "derived/task/result.json",
                                  "other": ["derived/task/image.png"]}}],
    )
    assert refs.storage_keys == {
        "derived/other/result.json", "derived/other/manifest.json", "uploads/draft.pdf",
        "derived/task/result.json", "derived/task/image.png",
    }


def test_published_draft_index_snapshot_protects_nested_references():
    refs = retention.collect_storage_references(documents=[], tasks=[], drafts=[{
        "draft": {"_index_snapshot": {
            "document_id": "published-draft",
            "chunks": [{"metadata": {
                "document_id": "attachment", "minio_object_key": "derived/attachment/image.png",
            }}],
            "metadata": {"attachments": [{"storage_key": "uploads/original.pdf"}],
                         "index_request_ref": "derived/attachment/confirmed.json"},
        }},
    }])
    assert refs.document_ids == {"published-draft", "attachment"}
    assert refs.storage_keys == {
        "derived/attachment/image.png", "uploads/original.pdf", "derived/attachment/confirmed.json",
    }


@pytest.mark.parametrize("fail_prefix", ["uploads/", "derived/"])
def test_incomplete_object_inventory_aborts_all_deletion(fail_prefix):
    service, deleted = _storage()
    old = datetime.now(timezone.utc) - timedelta(days=3)

    class Paginator:
        def paginate(self, *, Bucket, Prefix):
            yield {"Contents": [{"Key": Prefix + "doc-new/file", "LastModified": old}]}
            if Prefix == fail_prefix:
                raise RuntimeError("incomplete object inventory")

    service.client.get_paginator = lambda name: Paginator()
    with pytest.raises(RuntimeError, match="incomplete object inventory"):
        asyncio.run(service.cleanup_orphaned_objects(
            referenced_document_ids=set(), referenced_storage_keys=set(),
            older_than=datetime.now(timezone.utc) - timedelta(days=2),
            is_referenced=lambda *_: False,
        ))
    assert deleted == []


def test_incomplete_prefix_inventory_aborts_all_deletion():
    service, deleted = _storage()

    class Paginator:
        def paginate(self, **kwargs):
            yield {"Contents": [{"Key": "asr/old", "LastModified": datetime(2000, 1, 1, tzinfo=timezone.utc)}]}
            raise RuntimeError("incomplete object inventory")

    service.client.get_paginator = lambda name: Paginator()
    with pytest.raises(RuntimeError, match="incomplete object inventory"):
        service._cleanup_prefix_objects_sync("asr/", datetime.now(timezone.utc))
    assert deleted == []


@pytest.mark.parametrize("failure", ["page", "total", "versions"])
def test_admin_cleanup_incomplete_inventory_aborts_all_deletion(failure):
    deleted = []

    class Lifecycle:
        def list_documents_page(self, *, knowledge_type, page, page_size, include_purged):
            assert include_purged is True
            if page == 2 and failure == "page":
                raise RuntimeError("incomplete page")
            return {"documents": [{"document_id": "doc", "created_at": "2000-01-01"}]
                    if page == 1 else [], "total": 2 if failure == "total" else 1}

        def list_versions(self, document_id):
            if failure == "versions":
                raise RuntimeError("incomplete versions")
            return [{"storage_key": "uploads/old.pdf"}]

    async def delete(*args):
        deleted.append(args)

    with pytest.raises(RuntimeError, match="[Ii]ncomplete"):
        asyncio.run(retention.cleanup_expired_admin_documents(
            Lifecycle(), SimpleNamespace(delete_document=delete)))
    assert deleted == []


def test_admin_cleanup_inventory_exceeds_old_limit_and_keeps_all_versions():
    deleted = []
    expired = []

    class Lifecycle:
        def list_documents_page(self, *, knowledge_type, page, page_size, include_purged):
            total = 10001 if knowledge_type == "enterprise" else 0
            start = (page - 1) * page_size
            return {"total": total, "documents": [
                {"document_id": str(i), "updated_at": "2000-01-01" if i == 10000 else "2999-01-01"}
                for i in range(start, min(start + page_size, total))
            ]}

        def list_versions(self, document_id):
            return [{"publication_status": "UNPUBLISHED", "storage_key": f"uploads/{document_id}-v1.pdf"},
                    {"publication_status": "UNPUBLISHED", "storage_key": f"uploads/{document_id}-v2.pdf"}]

        def expire_document(self, document_id):
            expired.append(document_id)
            return True

    async def delete(key, document_id):
        deleted.append(key)

    counts = asyncio.run(retention.cleanup_expired_admin_documents(
        Lifecycle(), SimpleNamespace(delete_document=delete)))
    assert set(deleted) == {"uploads/10000-v1.pdf", "uploads/10000-v2.pdf"}
    assert expired == ["10000"]
    assert counts == {"admin_documents_expired": 1, "admin_document_objects_deleted": 2}


def test_admin_cleanup_never_deletes_published_document_versions():
    deleted = []
    expired = []

    class Lifecycle:
        def list_documents_page(self, *, knowledge_type, page, page_size, include_purged):
            return {"total": 1, "documents": [{
                "document_id": "published", "updated_at": "2000-01-01",
                "status": "INDEXED",
            }] if page == 1 else []}

        def list_versions(self, _document_id):
            return [{"status": "PUBLISHED", "publication_status": "PUBLISHED",
                     "storage_key": "uploads/published.pdf"}]

        def expire_document(self, document_id):
            expired.append(document_id)
            return True

    async def delete(key, document_id):
        deleted.append(key)

    asyncio.run(retention.cleanup_expired_admin_documents(
        Lifecycle(), SimpleNamespace(delete_document=delete)))
    assert deleted == []
    assert expired == []


def test_admin_cleanup_uses_last_update_for_unpublished_document_ttl():
    deleted = []
    expired = []

    class Lifecycle:
        def list_documents_page(self, *, knowledge_type, page, page_size, include_purged):
            return {"total": 1, "documents": [{
                "document_id": "recent", "updated_at": datetime.now(timezone.utc).isoformat(),
                "status": "INDEXED",
            }] if page == 1 else []}

        def list_versions(self, _document_id):
            return [{"status": "UNPUBLISHED", "publication_status": "UNPUBLISHED",
                     "storage_key": "uploads/recent.pdf"}]

        def expire_document(self, document_id):
            expired.append(document_id)
            return True

    async def delete(key, document_id):
        deleted.append(key)

    asyncio.run(retention.cleanup_expired_admin_documents(
        Lifecycle(), SimpleNamespace(delete_document=delete)))
    assert deleted == []
    assert expired == []


def test_deleted_draft_cleanup_waits_for_shared_reference_and_purges_after_ttl():
    from services.application.temporary_materials import _cleanup_deleted_drafts

    deleted_at = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
    target = {
        "draft_id": "deleted-old", "user_id": "u1", "status": "DELETED",
        "draft_type": "solution", "draft": {
            "deleted_at": deleted_at,
            "attachments": [{"document_id": "shared", "storage_key": "uploads/shared.pdf"}],
        },
    }
    newer_deleted = {
        "draft_id": "deleted-new", "user_id": "u2", "status": "DELETED",
        "draft_type": "solution", "draft": {
            "deleted_at": datetime.now(timezone.utc).isoformat(),
            "attachments": [{"document_id": "shared", "storage_key": "uploads/shared.pdf"}],
        },
    }
    purged = []

    async def purge(draft_id):
        purged.append(draft_id)
        return {"draft_id": draft_id, "status": "PURGED"}

    runtime = SimpleNamespace(
        admin_service=SimpleNamespace(delete_draft=purge),
        lifecycle_service=SimpleNamespace(list_versions=lambda _document_id: []),
    )
    assert asyncio.run(_cleanup_deleted_drafts(runtime, [target, newer_deleted])) == 0
    assert purged == []

    newer_deleted["draft"]["attachments"] = []
    assert asyncio.run(_cleanup_deleted_drafts(runtime, [target, newer_deleted])) == 1
    assert purged == ["deleted-old"]


def test_cleanup_page_scan_fails_closed_when_reported_rows_are_missing():
    from services.application.temporary_materials import _all_cleanup_pages

    def truncated_query(*, page, page_size, **_filters):
        return {"items": [{"draft_id": "one"}] if page == 1 else [], "total": 2}

    with pytest.raises(RuntimeError, match="Incomplete cleanup inventory"):
        _all_cleanup_pages(truncated_query)


def test_truncated_object_inventory_without_token_aborts_deletion():
    service, deleted = _storage()
    service.client.get_paginator = lambda name: SimpleNamespace(paginate=lambda **kwargs: iter([
        {"IsTruncated": True, "Contents": []},
    ]))
    with pytest.raises(RuntimeError, match="Incomplete"):
        service._cleanup_orphaned_objects_sync(set(), set(), datetime.now(timezone.utc), lambda *_: False)
    assert deleted == []



def test_storage_reference_scan_has_no_total_limit(monkeypatch):
    class Rows:
        def __init__(self, rows):
            self.rows = iter(rows)

        def fetchmany(self, size):
            result = []
            for _ in range(size):
                row = next(self.rows, None)
                if row is None:
                    break
                result.append((row,))
            return result

    class Connection:
        def exec_driver_sql(self, query, *args, **kwargs):
            assert "LIMIT" not in query.upper()
            if "FROM platform_documents" in query:
                return Rows({"document_id": f"doc-{i}", "storage_key": f"uploads/{i}.pdf"} for i in range(10001))
            return Rows([])

    @contextmanager
    def connection(_dsn):
        yield Connection()

    monkeypatch.setattr(retention, "_pooled_connection", connection)
    refs = retention.PostgresRetentionService("postgresql://test").storage_references()
    assert len(refs.document_ids) == 10001
    assert "uploads/10000.pdf" in refs.storage_keys


def _storage():
    service = object.__new__(MinioUploadService)
    service.bucket = "test"
    deleted = []
    old = datetime.now(timezone.utc) - timedelta(days=3)

    class Paginator:
        def paginate(self, *, Bucket, Prefix):
            yield {"Contents": [{"Key": Prefix + "doc-new/file", "LastModified": old}]}

    service.client = SimpleNamespace(
        get_paginator=lambda name: Paginator(),
        head_object=lambda **kwargs: {"Metadata": {"document-id": "doc-new"}},
        delete_object=lambda **kwargs: deleted.append(kwargs["Key"]),
    )
    return service, deleted


def test_cleanup_rechecks_new_reference():
    service, deleted = _storage()
    counts = asyncio.run(service.cleanup_orphaned_objects(
        referenced_document_ids=set(), referenced_storage_keys=set(),
        older_than=datetime.now(timezone.utc) - timedelta(days=2),
        is_referenced=lambda key, document_id: document_id == "doc-new",
    ))
    assert deleted == []
    assert counts["total"] == 0


def test_cleanup_lookup_failure_stops_deletion():
    service, deleted = _storage()

    def unavailable(key, document_id):
        raise RuntimeError("reference lookup unavailable")

    with pytest.raises(RuntimeError, match="reference lookup unavailable"):
        asyncio.run(service.cleanup_orphaned_objects(
            referenced_document_ids=set(), referenced_storage_keys=set(),
            older_than=datetime.now(timezone.utc) - timedelta(days=2),
            is_referenced=unavailable,
        ))
    assert deleted == []


def test_scan_failure_does_not_start_storage_deletion(monkeypatch):
    from services.application import temporary_materials as bootstrap
    monkeypatch.setenv("DATABASE_URL", "postgresql://test")
    monkeypatch.setattr("services.persistence.sqlalchemy.postgres_advisory_lock", lambda *_a, **_kw: nullcontext())

    def unavailable(self):
        raise RuntimeError("reference scan unavailable")

    monkeypatch.setattr(retention.PostgresRetentionService, "storage_references", unavailable, raising=False)
    storage = SimpleNamespace(cleanup_orphaned_objects=lambda **kwargs: pytest.fail("must not delete"))
    runtime = SimpleNamespace(
        lifecycle_service=SimpleNamespace(list_storage_references=lambda **kwargs: []),
        draft_store_service=SimpleNamespace(list_all=lambda **kwargs: []),
        task_store=SimpleNamespace(list=lambda **kwargs: []),
    )
    with pytest.raises(RuntimeError, match="reference scan unavailable"):
        asyncio.run(bootstrap._cleanup_orphaned_storage(runtime, storage))


def test_retention_protects_unmigrated_confirmation_task(monkeypatch):
    statements = []

    class Connection:
        def exec_driver_sql(self, sql, parameters=()):
            statements.append((sql, parameters))
            return SimpleNamespace(rowcount=0, fetchone=lambda: (None,))

    @contextmanager
    def connection(_dsn):
        yield Connection()

    monkeypatch.setattr(retention, "_pooled_connection", connection)
    retention.PostgresRetentionService("postgresql://test").cleanup_once()
    sql = next(sql for sql, _ in statements if "DELETE FROM platform_tasks" in sql)
    assert "version_history" in sql
    assert "confirmed_index_request" in sql
    assert "index_request_ref" in sql
    assert "NOT EXISTS" in sql
def test_cleanup_pages_are_not_truncated():
    from services.application.temporary_materials import _all_cleanup_pages
    rows = list(range(10001))

    def query(*, page, page_size):
        start = (page - 1) * page_size
        return {"items": rows[start:start + page_size]}

    assert _all_cleanup_pages(query) == rows


def test_cleanup_supports_document_catalog_page_contract():
    from services.application.temporary_materials import _all_cleanup_pages
    result = _all_cleanup_pages(lambda *, page, page_size: {
        "documents": [{"document_id": "doc"}] if page == 1 else []
    }, items_key="documents")
    assert result == [{"document_id": "doc"}]


def test_saved_attachment_cleanup_preserves_newer_shared_draft():
    from services.application.temporary_materials import _cleanup_saved_draft_attachments
    from services.persistence.postgres import InMemoryDraftStore
    store = InMemoryDraftStore()
    attachment = {"document_id": "shared", "storage_key": "uploads/shared.pdf"}
    record = store.create(user_id="old", user_role="external", thread_id=None,
                          draft={"draft_type": "demand", "attachments": [attachment]})
    record["updated_at"] = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
    store.create(user_id="recent", user_role="external", thread_id=None,
                 draft={"draft_type": "demand", "attachments": [attachment]})
    deleted = []

    async def delete(*args):
        deleted.append(args)

    result = asyncio.run(_cleanup_saved_draft_attachments(store,
        SimpleNamespace(delete_document=delete), [record], set(),
        SimpleNamespace(purge_document=lambda *_: None)))
    assert result == 0
    assert deleted == []


def test_saved_attachment_cleanup_includes_admin_unpublished_drafts():
    from services.application.temporary_materials import _cleanup_saved_draft_attachments
    from services.persistence.postgres import InMemoryDraftStore

    store = InMemoryDraftStore()
    record = store.create(
        user_id="admin",
        user_role="internal",
        thread_id=None,
        draft={
            "draft_type": "solution",
            "attachments": [{"document_id": "admin-doc", "storage_key": "uploads/admin.pdf"}],
        },
    )
    record["updated_at"] = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
    deleted = []

    async def delete(storage_key, document_id):
        deleted.append((storage_key, document_id))

    result = asyncio.run(_cleanup_saved_draft_attachments(
        store,
        SimpleNamespace(delete_document=delete),
        [record],
        set(),
        SimpleNamespace(purge_document=lambda _document_id: None),
    ))

    assert result == 1
    assert deleted == [("uploads/admin.pdf", "admin-doc")]
    assert store.get_admin(record["draft_id"])["draft"]["attachments"][0]["storage_key"] == ""


def test_cleanup_protects_indexed_and_historical_published_versions():
    from services.application.temporary_materials import _published_document_ids

    class Lifecycle:
        def list_documents_page(self, *, page, page_size, include_purged):
            assert include_purged is True
            return {"documents": [{"document_id": "doc"}] if page == 1 else []}

        def list_versions(self, _document_id):
            return [{"status": "INDEXED", "publication_status": "PUBLISHED"},
                    {"status": "PURGED", "publication_status": "UNPUBLISHED"}]

    assert _published_document_ids(Lifecycle()) == {"doc"}


def test_reference_write_and_orphan_scan_are_serialized(monkeypatch):
    from services.application.temporary_materials import _cleanup_orphaned_storage
    monkeypatch.setenv("DATABASE_URL", "postgresql://test")
    monkeypatch.setattr("services.persistence.sqlalchemy.postgres_advisory_lock", lambda *_a, **_kw: nullcontext())
    events = []
    monkeypatch.setattr(retention.PostgresRetentionService, "storage_references",
                        lambda self: events.append("scan") or retention.StorageReferences(set(), set()))

    async def cleanup(**_kwargs):
        return {"total": 0}

    async def run():
        entered = asyncio.Event()
        release = asyncio.Event()

        @retention.serialize_storage_references
        async def write():
            entered.set()
            await release.wait()
            events.append("write")

        writer = asyncio.create_task(write())
        await entered.wait()
        reader = asyncio.create_task(_cleanup_orphaned_storage(None, SimpleNamespace(cleanup_orphaned_objects=cleanup)))
        await asyncio.sleep(0)
        assert events == []
        release.set()
        await asyncio.gather(writer, reader)
        assert events == ["write", "scan"]

    asyncio.run(run())

