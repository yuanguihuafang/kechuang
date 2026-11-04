import asyncio
import threading
import pytest
import time
import warnings
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from types import SimpleNamespace

from sqlalchemy import create_engine

import services.vector.milvus.milvus_service as milvus_module
from admin.service import AdminService
from core.contracts.knowledge import CleanedKnowledgeRecord
from services.persistence.repositories.runtime_repository import PostgresMysqlSyncStateStore
from services.persistence.sqlalchemy import metadata, milvus_count_snapshots
from services.vector.milvus.milvus_service import MilvusService


def test_pymilvus_orm_deprecation_warning_is_filtered():
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        milvus_module._configure_pymilvus_warning_filter()
        warnings.warn_explicit(
            "`connections.connect` is an ORM-style PyMilvus API and will be removed. "
            "Use `MilvusClient` instead.",
            DeprecationWarning,
            "pymilvus/orm/connections.py",
            1,
            module="pymilvus.orm.connections",
        )

    assert caught == []


def test_mysql_upsert_defers_flush_until_sync_finishes():
    class Collection:
        def __init__(self):
            self.flush_calls = 0

        def upsert(self, _rows):
            return None

        def flush(self):
            self.flush_calls += 1

    collection = Collection()
    service = object.__new__(MilvusService)
    service._collection = collection
    service.requires_user_id = False
    service._collection_loaded = True
    service._ensure_connection = lambda: None

    service.upsert_chunks(
        [
            {
                "id": "chunk-1",
                "document_id": "mysql:demand",
                "mysql_id": "1",
                "source_id": "",
                "chunk_index": 0,
                "source_locator": "mysql:1",
                "chunk_text": "同步内容",
                "dense_vector": [0.0] * 1024,
                "chunk_role": "mysql",
                "field_names": [],
                "status": "active",
                "update_time": "2026-09-16T00:00:00+00:00",
            }
        ]
    )

    assert collection.flush_calls == 0
    service.flush_for_mysql_sync()
    assert collection.flush_calls == 1


def test_mysql_sync_runner_serializes_milvus_writers():
    service = object.__new__(AdminService)
    state = {"active": 0, "peak": 0}
    guard = threading.Lock()

    async def fake_run(_task_id, _payload, *, stop_event=None):
        with guard:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
        time.sleep(0.02)
        with guard:
            state["active"] -= 1

    service._run_mysql_sync = fake_run
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(
            pool.map(
                lambda task_id: service._run_mysql_sync_serialized(
                    task_id, None, threading.Event()
                ),
                ("sync-1", "sync-2"),
            )
        )

    assert state["peak"] == 1


def test_mysql_sync_caps_requested_batch_size_before_reading(monkeypatch):
    import admin.mysql_sync as service_module

    limits = []

    class Source:
        def count_for_sync(self, _knowledge_type):
            return 0

        def records_for_sync(self, _knowledge_type, *, after_id, limit):
            limits.append(limit)
            return []

        def list_for_sync(self, _knowledge_type, *, after_id, limit):
            limits.append(limit)
            return []

    monkeypatch.setattr(service_module, "MySQLBusinessSearchService", Source)
    state = {
        "task_id": "sync-1",
        "status": "QUEUED",
        "processed": 0,
        "indexed_count": 0,
        "skipped_count": 0,
        "last_id": 0,
        "replace_existing": True,
        "stop_requested": False,
    }

    class Tasks:
        def get(self, _task_id):
            return dict(state)

        def update(self, _task_id, **fields):
            state.update(fields)
            return dict(state)

    target = SimpleNamespace(
        count_by_mysql_records=lambda _prefix: 0,
        flush_for_mysql_sync=lambda: None,
    )
    runtime = SimpleNamespace(
        task_store=Tasks(),
        sync_state_store=None,
        embedded_retrieval=SimpleNamespace(
            agent=SimpleNamespace(
                public_milvus={"demand": target},
            )
        ),
    )

    asyncio.run(
        AdminService(runtime)._run_mysql_sync(
            "sync-1",
            SimpleNamespace(knowledge_type="demand", batch_size=200),
            stop_event=threading.Event(),
        )
    )

    assert limits == [64, 64]


@pytest.mark.parametrize("sources, expected_source, failed", [
    (["source:restored"], "source:restored", False),
    ([], None, False),
    (["source:a", "source:b"], None, True),
])
def test_mysql_sync_preserves_unambiguous_restored_source_id(monkeypatch, sources, expected_source, failed):
    import admin.mysql_sync as service_module

    state = {
        "status": "QUEUED",
        "stop_requested": False,
        "processed": 0,
        "indexed_count": 0,
        "skipped_count": 0,
        "last_id": 0,
    }
    returned = False

    class Source:
        def count_for_sync(self, _knowledge_type):
            return 1

        def records_for_sync(self, _knowledge_type, *, after_id, limit):
            nonlocal returned
            if returned:
                return []
            returned = True
            return [CleanedKnowledgeRecord(record_id="1", mysql_id="1", text="记录正文")]

        def list_for_sync(self, *_args, **_kwargs):
            return []

    class Tasks:
        def get(self, _task_id):
            return dict(state)

        def update(self, _task_id, **fields):
            state.update(fields)
            return dict(state)

    imported = []
    target = SimpleNamespace(
        published_draft_mysql_ids=lambda _ids: set(),
        source_ids_by_mysql_ids=lambda ids: {"1": sources},
        count_by_mysql_records=lambda _prefix: 1,
        flush_for_mysql_sync=lambda: None,
    )

    async def import_business_data(request):
        imported.extend(request.records)
        return SimpleNamespace(status="SUCCESS", indexed_count=1)

    runtime = SimpleNamespace(
        task_store=Tasks(),
        sync_state_store=None,
        embedded_retrieval=SimpleNamespace(
            agent=SimpleNamespace(
                public_milvus={"demand": target},
                import_business_data=import_business_data,
            )
        ),
    )
    service = AdminService(runtime)
    service._refresh_mysql_counts_after_sync = lambda _knowledge_type: None
    monkeypatch.setattr(service_module, "MySQLBusinessSearchService", Source)

    asyncio.run(
        service._run_mysql_sync(
            "sync-1",
            SimpleNamespace(knowledge_type="demand", batch_size=64),
            stop_event=threading.Event(),
        )
    )

    if failed:
        assert imported == []
        assert state["status"] == "FAILED"
        assert "source_id" in state["error"]
    else:
        from services.application.business_records import stable_source_id
        assert len(imported) == 1
        assert imported[0].source_id == (expected_source or stable_source_id("1"))


def test_mysql_sync_stops_before_next_batch_when_shutdown_is_requested(monkeypatch):
    import admin.mysql_sync as service_module

    stop_event = threading.Event()
    state = {
        "status": "RUNNING",
        "stop_requested": False,
        "processed": 0,
        "indexed_count": 0,
        "skipped_count": 0,
        "last_id": 0,
    }

    class Source:
        def count_for_sync(self, _knowledge_type):
            return 12

        def records_for_sync(self, *_args, **_kwargs):
            stop_event.set()
            return [SimpleNamespace(mysql_id=1)]

    class Tasks:
        def get(self, _task_id):
            return dict(state)

        def update(self, _task_id, **fields):
            state.update(fields)
            return dict(state)

    calls = []
    target = SimpleNamespace(
        count_by_mysql_records=lambda _prefix: 0,
        flush_for_mysql_sync=lambda: calls.append("flush"),
    )

    async def import_business_data(_request):
        raise AssertionError("收到停止信号后不能开始写入当前批次")

    runtime = SimpleNamespace(
        task_store=Tasks(),
        sync_state_store=None,
        embedded_retrieval=SimpleNamespace(
            agent=SimpleNamespace(
                public_milvus={"demand": target},
                import_business_data=import_business_data,
            )
        ),
    )
    monkeypatch.setattr(service_module, "MySQLBusinessSearchService", Source)

    asyncio.run(
        AdminService(runtime)._run_mysql_sync(
            "sync-1",
            SimpleNamespace(knowledge_type="demand", batch_size=64),
            stop_event=stop_event,
        )
    )

    assert state["status"] == "STOPPED"
    assert calls == ["flush"]


def test_interrupted_mysql_sync_is_not_queued_again_after_restart():
    state = {
        "task_id": "sync-1",
        "operation": "mysql_sync",
        "status": "INTERRUPTED",
        "knowledge_type": "demand",
        "last_id": 12,
    }
    launched = []

    class Tasks:
        def list(self, *, operation, limit):
            return [dict(state)]

        def update(self, task_id, **fields):
            state.update(fields)
            return dict(state)

    runtime = SimpleNamespace(task_store=Tasks(), sync_state_store=None)
    service = AdminService(runtime)
    service._launch_mysql_sync = lambda task_id, payload: launched.append((task_id, payload))

    asyncio.run(service.resume_mysql_syncs())

    assert state["status"] == "INTERRUPTED"
    assert state["stop_requested"] is True
    assert launched == []


def test_milvus_readiness_failure_discards_stale_connection(monkeypatch):
    import services.vector.milvus.milvus_service as milvus_module

    class Connections:
        def __init__(self):
            self.disconnected = False

        def disconnect(self, *, alias):
            self.disconnected = True

    connections = Connections()

    class Utility:
        @staticmethod
        def list_collections(*, using):
            raise RuntimeError("Milvus connection reset")

    monkeypatch.setattr(
        milvus_module,
        "_lazy_import_pymilvus",
        lambda: (None, None, None, None, None, None, None, None, connections, Utility),
    )
    service = object.__new__(MilvusService)
    service.connection_manager = milvus_module.MilvusConnectionManager(
        uri="http://milvus",
        database="kechuang",
        username=None,
        password=None,
    )
    service.connection_manager._connected = True
    service.connection_manager._generation = 1
    service.alias = service.connection_manager.alias
    service._connection_generation = service.connection_manager.generation
    service._connected = True
    service._collection = object()

    try:
        service._check_ready_sync()
    except RuntimeError as exc:
        assert str(exc) == "Milvus connection reset"
    else:
        raise AssertionError("expected readiness failure")

    assert service._connected is False
    assert service._collection is None
    assert connections.disconnected is True


def test_milvus_query_retries_collection_recovery(monkeypatch):
    import services.vector.milvus.milvus_service as milvus_module

    class Collection:
        def __init__(self):
            self.calls = 0

        def query(self, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError(
                    "failed to query: loaded collection do not found any channel in target, "
                    "collection on recovering"
                )
            return [{"id": "chunk-1"}]

    monkeypatch.setattr(milvus_module.time, "sleep", lambda _seconds: None)
    service = object.__new__(MilvusService)
    service.collection_name = "solution"
    collection = Collection()
    service._existing_collection = lambda: collection

    assert service.count_by_document_id("doc-1") == 1
    assert collection.calls == 2


def test_milvus_rebuild_retries_collection_recovery(monkeypatch):
    import services.vector.milvus.milvus_service as milvus_module

    class Utility:
        @staticmethod
        def has_collection(*_args, **_kwargs):
            return True

        @staticmethod
        def release_collection(*_args, **_kwargs):
            return None

        def __init__(self):
            self.drop_calls = 0

        def drop_collection(self, *_args, **_kwargs):
            self.drop_calls += 1
            if self.drop_calls == 1:
                raise RuntimeError("collection on recovering")

    utility = Utility()
    monkeypatch.setattr(milvus_module, "_lazy_import_pymilvus", lambda: (None, utility))
    monkeypatch.setattr(milvus_module.time, "sleep", lambda _seconds: None)
    service = object.__new__(MilvusService)
    service.collection_name = "solution"
    service.alias_name = None
    service._connected = True
    service._collection = object()
    service._ensure_connection = lambda: None
    service.ensure_collection = lambda: None

    service.rebuild_collection()

    assert utility.drop_calls == 2


def test_milvus_flush_retries_collection_recovery(monkeypatch):
    import services.vector.milvus.milvus_service as milvus_module

    class Collection:
        def __init__(self):
            self.calls = 0

        def flush(self):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("collection on recovering")

    monkeypatch.setattr(milvus_module.time, "sleep", lambda _seconds: None)
    service = object.__new__(MilvusService)
    service.collection_name = "solution"
    collection = Collection()
    service._collection = collection
    service._connected = True
    service._ensure_connection = lambda: None
    service._collection_loaded = True

    service.flush()

    assert collection.calls == 2


def test_unexpected_mysql_sync_exception_marks_task_failed():
    state = {"task_id": "sync-1", "status": "RUNNING"}

    class Tasks:
        def get(self, _task_id):
            return dict(state)

        def update(self, _task_id, **fields):
            state.update(fields)
            return dict(state)

    service = AdminService(SimpleNamespace(task_store=Tasks(), sync_state_store=None))

    async def fail(_task_id, _payload, *, stop_event=None):
        raise RuntimeError("unexpected Milvus failure")

    service._run_mysql_sync = fail
    import threading
    service._run_mysql_sync_serialized(
        "sync-1", SimpleNamespace(knowledge_type="demand"), threading.Event()
    )

    assert state["status"] == "FAILED"
    assert state["error"] == "unexpected Milvus failure"


def test_mysql_counts_default_reads_cache_until_explicit_refresh(monkeypatch):
    import admin.mysql_sync as service_module

    monkeypatch.setattr(
        service_module,
        "MYSQL_FORM_CONTRACTS",
        {
            "demand": {"public_fields": ["name"], "identity_only": False},
        },
    )
    calls = {"mysql": 0, "milvus": 0}

    class Source:
        def count_for_sync(self, _knowledge_type):
            calls["mysql"] += 1
            return 7

    class Target:
        def count_by_logical_records(self):
            calls["milvus"] += 1
            return 6

    runtime = SimpleNamespace(
        task_store=SimpleNamespace(),
        embedded_retrieval=SimpleNamespace(
            agent=SimpleNamespace(
                public_milvus={"demand": Target()},
            )
        ),
    )
    monkeypatch.setattr(service_module, "MySQLBusinessSearchService", Source)
    service = AdminService(runtime)

    assert service.mysql_sync_counts() == {}
    refreshed = service.mysql_sync_counts(refresh=True)
    assert refreshed["demand"] == {"mysql_count": 7, "milvus_count": 6, "consistent": False}
    assert calls == {"mysql": 1, "milvus": 1}
    assert service.mysql_sync_counts() == refreshed
    assert calls == {"mysql": 1, "milvus": 1}

    service.mysql_sync_counts(refresh=True)
    assert calls == {"mysql": 2, "milvus": 2}


def test_mysql_counts_refreshes_when_sync_interval_is_due(monkeypatch):
    import admin.mysql_sync as service_module

    service = object.__new__(AdminService)
    service._mysql_counts_cache = {"demand": {"milvus_count": 1}}
    service._mysql_counts_refreshed_at = 100.0
    refreshed = []
    service.mysql_sync_counts = lambda *, refresh=False: refreshed.append(refresh) or {}
    monkeypatch.setattr(service_module.time, "monotonic", lambda: 279.9)

    assert service._maybe_refresh_mysql_counts() is False
    assert refreshed == []

    monkeypatch.setattr(service_module.time, "monotonic", lambda: 280.0)
    assert service._maybe_refresh_mysql_counts() is True
    assert refreshed == [True]


def test_milvus_snapshot_compares_with_today_sync_baseline(monkeypatch):
    import services.persistence.repositories.runtime_repository as repository_module

    engine = create_engine("sqlite://")
    metadata.create_all(engine, tables=[milvus_count_snapshots])
    monkeypatch.setattr(
        repository_module,
        "_now",
        lambda: datetime(2026, 9, 17, 8, 0, tzinfo=timezone.utc),
    )
    store = PostgresMysqlSyncStateStore(engine=engine)

    store.save_milvus_count("demand", 100)
    result = store.save_milvus_count("demand", 125)

    assert result == {"today": 125, "baseline": 100, "delta": 25}
