import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from enum import IntEnum
from threading import Lock
from time import sleep
from types import SimpleNamespace

import pytest

import agents.retrieval_agent.retrieval.search_service as search_module
import services.vector.milvus.milvus_service as milvus_module
from services.vector.milvus.milvus_service import MilvusService


def test_milvus_service_requires_the_worker_connection_manager():
    with pytest.raises(TypeError):
        MilvusService(collection_name="enterprise")


def test_search_service_shares_one_connection_manager_between_collections(monkeypatch):
    managers = []
    aliases = []
    manager_options = []
    manager = milvus_module.MilvusConnectionManager(
        uri="http://milvus",
        database="kechuang",
        username="user",
        password="secret",
    )

    class FakeMilvusService:
        def __init__(self, *, collection_name, connection_manager, alias_name=None, **_kwargs):
            self.collection_name = collection_name
            aliases.append(alias_name)
            managers.append(connection_manager)

    monkeypatch.setattr(search_module, "MilvusService", FakeMilvusService)
    monkeypatch.setattr(search_module, "EmbeddingService", lambda: object())
    monkeypatch.setattr(search_module, "RerankService", lambda: object())
    monkeypatch.setattr(search_module, "QueryService", lambda: object())
    monkeypatch.setattr(
        search_module,
        "get_settings",
        lambda: SimpleNamespace(
            rerank_top_k=3,
            milvus_uri="http://milvus",
            milvus_database="kechuang",
            milvus_username="user",
            milvus_password="secret",
        ),
    )
    monkeypatch.setattr(
        search_module,
        "MilvusConnectionManager",
        lambda **kwargs: manager_options.append(kwargs) or manager,
    )

    service = search_module.SearchService()

    assert len(managers) == len(milvus_module.PUBLIC_COLLECTIONS) + 1
    assert aliases[:-1] == list(milvus_module.PUBLIC_COLLECTION_ALIASES.values())
    assert aliases[-1] is None
    assert all(item is manager for item in managers)
    assert manager_options == [
        {
            "uri": "http://milvus",
            "database": "kechuang",
            "username": "user",
            "password": "secret",
        }
    ]
    assert manager.generation == 0
    assert set(service.public_milvus) == set(milvus_module.PUBLIC_COLLECTIONS)


def test_search_service_stops_collection_recovery_before_closing_shared_connection():
    calls = []

    class Milvus:
        def stop_collection_load_recovery(self):
            calls.append("stop_recovery")

    class ConnectionManager:
        def close(self):
            calls.append("close_connection")

    service = object.__new__(search_module.SearchService)
    service.public_milvus = {"demand": Milvus(), "solution": Milvus()}
    service.private_milvus = Milvus()
    service.connection_manager = ConnectionManager()

    service.close()

    assert calls == [
        "stop_recovery",
        "stop_recovery",
        "stop_recovery",
        "close_connection",
    ]


def test_connection_manager_serializes_concurrent_first_connect(monkeypatch):
    calls = []
    guard = Lock()

    class Connections:
        def connect(self, **kwargs):
            sleep(0.02)
            with guard:
                calls.append(kwargs)

    monkeypatch.setattr(
        milvus_module,
        "_lazy_import_pymilvus",
        lambda: (None, None, None, None, None, None, None, None, Connections(), None),
    )
    manager = milvus_module.MilvusConnectionManager(
        uri="http://milvus",
        database="kechuang",
        username="user",
        password="secret",
    )

    with ThreadPoolExecutor(max_workers=12) as pool:
        generations = list(pool.map(lambda _index: manager.ensure_connected(), range(12)))

    assert len(calls) == 1
    assert len({item["alias"] for item in calls}) == 1
    assert set(generations) == {manager.generation}


def test_services_refresh_collection_handles_after_shared_connection_reconnect(monkeypatch):
    calls = {"connect": [], "disconnect": [], "collections": []}

    class Connections:
        def connect(self, **kwargs):
            calls["connect"].append(kwargs)

        def disconnect(self, *, alias):
            calls["disconnect"].append(alias)

    class Collection:
        def __init__(self, name, *, using):
            self.name = name
            self.using = using
            calls["collections"].append(self)

    class Utility:
        @staticmethod
        def has_collection(_name, *, using):
            return True

    monkeypatch.setattr(
        milvus_module,
        "_lazy_import_pymilvus",
        lambda: (Collection, None, None, None, None, None, None, None, Connections(), Utility),
    )
    manager = milvus_module.MilvusConnectionManager(
        uri="http://milvus",
        database="kechuang",
        username=None,
        password=None,
    )
    first = MilvusService(collection_name="enterprise", connection_manager=manager)
    second = MilvusService(collection_name="expert", connection_manager=manager)
    first._assert_source_fields = lambda _collection: None
    second._assert_source_fields = lambda _collection: None

    first_collection = first._existing_collection(load=False)
    second_collection = second._existing_collection(load=False)
    assert first_collection is not second_collection
    assert len(calls["connect"]) == 1
    assert first_collection.using == second_collection.using

    manager.invalidate()
    refreshed = first._existing_collection(load=False)
    second._ensure_connection()

    assert refreshed is not first_collection
    assert second._collection is None
    assert len(calls["connect"]) == 2
    assert calls["disconnect"] == [first.connection_alias]


class LoadState(IntEnum):
    NotExist = 0
    NotLoad = 1
    Loading = 2
    Loaded = 3


@pytest.fixture
def milvus_lifecycle(monkeypatch, request):
    calls = SimpleNamespace(
        connect=[], disconnect=[], collections=[], load=[], state=[], events=[], segments=[]
    )
    state = SimpleNamespace(names={"enterprise", "expert"}, load=LoadState.NotLoad)

    class Connections:
        def connect(self, **kwargs):
            calls.connect.append(kwargs)

        def disconnect(self, *, alias):
            calls.disconnect.append(alias)

    class Collection:
        def __init__(self, name, *, using, schema=None):
            self.name = name
            self.using = using
            self.schema = schema or SimpleNamespace(
                fields=[SimpleNamespace(name=field) for field in (
                    "id", "document_id", "mysql_id", "source_id", "chunk_role",
                    "chunk_index", "source_locator", "chunk_text", "dense_vector",
                    "sparse_vector", "field_names", "status", "update_time",
                )]
            )
            calls.collections.append(self)
            if schema is not None:
                state.names.add(name)

        def load(self, *, timeout=None):
            calls.load.append(timeout)
            if isinstance(state.load, Exception):
                raise state.load
            state.load = LoadState.Loaded

        def create_index(self, **kwargs):
            calls.events.append(("index", kwargs["field_name"]))

        def query(self, **kwargs):
            assert state.load == LoadState.Loaded
            calls.events.append(("query", kwargs))
            return [{"id": "chunk-1"}]

        def search(self, **kwargs):
            assert state.load == LoadState.Loaded
            calls.events.append(("search", kwargs))
            return []

        def hybrid_search(self, **kwargs):
            assert state.load == LoadState.Loaded
            calls.events.append(("hybrid_search", kwargs))
            return []

        def upsert(self, rows):
            calls.events.append(("upsert", rows))

        def flush(self):
            calls.events.append(("flush",))

    class Utility:
        @staticmethod
        def has_collection(name, *, using):
            return name in state.names

        @staticmethod
        def list_collections(*, using):
            return list(state.names)

        @staticmethod
        def load_state(name, *, using, timeout):
            calls.state.append((name, using, timeout))
            if isinstance(state.load, Exception):
                raise state.load
            return state.load

        @staticmethod
        def get_query_segment_info(name, *, using, timeout):
            calls.segments.append((name, using, timeout))
            return [
                SimpleNamespace(
                    segmentID=123,
                    collectionID=456,
                    partitionID=789,
                    nodeID=1,
                    nodeIds=[1],
                    state=3,
                    num_rows=10,
                    mem_size=1024,
                    index_name="dense",
                    indexID=2,
                    level=1,
                    is_sorted=True,
                    storage_version=2,
                )
            ]

    monkeypatch.setattr(
        milvus_module,
        "_lazy_import_pymilvus",
        lambda: (
            Collection, None, None, None, None, None,
            lambda **kwargs: kwargs, lambda **kwargs: kwargs, Connections(), Utility,
        ),
    )
    monkeypatch.setattr(milvus_module.time, "sleep", lambda _seconds: None)
    manager = milvus_module.MilvusConnectionManager(
        uri="http://milvus", database="kechuang", username=None, password=None,
    )
    service = MilvusService(collection_name="enterprise", connection_manager=manager)
    request.addfinalizer(service.stop_collection_load_recovery)
    return SimpleNamespace(service=service, manager=manager, calls=calls, state=state)


@pytest.mark.parametrize("access", ["collection", "ensure_collection", "_existing_collection"])
def test_cached_collection_checks_shared_connection_generation(milvus_lifecycle, access):
    env = milvus_lifecycle
    first = env.service.collection
    sibling = MilvusService(collection_name="expert", connection_manager=env.manager)
    sibling_collection = sibling.collection
    env.manager.invalidate()

    refreshed = (
        env.service.collection if access == "collection" else getattr(env.service, access)()
    )

    assert refreshed is not first
    assert sibling.collection is not sibling_collection
    assert len(env.calls.connect) == 2
    assert env.calls.disconnect == [env.manager.alias]
    assert env.calls.load == [30, 30, 30, 30]


@pytest.mark.parametrize("access", ["collection", "ensure_collection", "_existing_collection"])
def test_read_only_handle_does_not_skip_loading_or_repeat_load_rpc(milvus_lifecycle, access):
    env = milvus_lifecycle
    handle = env.service._existing_collection(load=False)
    assert env.calls.load == []

    for _ in range(3):
        collection = (
            env.service.collection if access == "collection" else getattr(env.service, access)()
        )
        assert collection is handle

    assert env.calls.load == [30]


def test_new_collection_uses_the_same_finite_load_timeout(milvus_lifecycle, monkeypatch):
    env = milvus_lifecycle
    env.state.names.clear()
    monkeypatch.setattr(env.service, "_build_schema", lambda: SimpleNamespace(fields=[]))

    env.service.ensure_collection()

    assert env.calls.load == [30]
    assert env.calls.events == [("index", "dense_vector"), ("index", "sparse_vector")]


def test_failed_collection_load_recovers_in_background(milvus_lifecycle, monkeypatch):
    env = milvus_lifecycle
    collection = env.service._existing_collection(load=False)
    load_attempts = []

    def load(*, timeout=None):
        load_attempts.append(timeout)
        if len(load_attempts) == 1:
            raise RuntimeError("pk_column != nullptr")
        env.state.load = LoadState.Loaded

    collection.load = load
    monkeypatch.setattr(
        env.service,
        "_existing_collection",
        lambda *, load=True: collection,
    )
    monkeypatch.setattr(
        milvus_module,
        "_COLLECTION_BACKGROUND_RECOVERY_DELAYS_SECONDS",
        (0, 0),
        raising=False,
    )

    with pytest.raises(RuntimeError, match="pk_column"):
        env.service._load_collection(collection)

    recovery_thread = env.service._load_recovery_thread
    recovery_thread.join(timeout=2)

    assert not recovery_thread.is_alive()
    assert load_attempts == [30, 30]
    assert env.service._collection_loaded is True
    assert env.state.load == LoadState.Loaded


def test_exhausted_collection_recovery_logs_every_attempt(milvus_lifecycle, monkeypatch, caplog):
    env = milvus_lifecycle
    env.state.load = RuntimeError("pk_column != nullptr")
    monkeypatch.setattr(
        milvus_module,
        "_COLLECTION_BACKGROUND_RECOVERY_DELAYS_SECONDS",
        (0, 0),
        raising=False,
    )

    with caplog.at_level(logging.ERROR, logger="kechuang.retrieval.milvus"):
        with pytest.raises(RuntimeError, match="pk_column"):
            env.service._load_collection(env.service._existing_collection(load=False))
        recovery_thread = env.service._load_recovery_thread
        recovery_thread.join(timeout=2)

    failures = [
        record
        for record in caplog.records
        if getattr(record, "structured", {}).get("event")
        == "milvus_collection_load_recovery_failed"
    ]
    assert len(failures) == 2
    assert all(record.exc_info is not None for record in failures)
    assert all(record.structured["collection"] == "enterprise" for record in failures)
    assert all("RuntimeError" in record.structured["load_state"] for record in failures)
    assert all("pk_column != nullptr" in record.structured["error"] for record in failures)
    assert failures[0].structured["database"] == "kechuang"
    diagnostics = [
        record
        for record in caplog.records
        if getattr(record, "structured", {}).get("event")
        == "milvus_collection_load_recovery_diagnostics"
    ]
    assert len(diagnostics) == 2
    assert diagnostics[0].structured["database"] == "kechuang"
    assert diagnostics[0].structured["schema_fields"][0]["name"] == "id"
    assert diagnostics[0].structured["milvus_load_state"] == "unavailable"
    assert "pk_column != nullptr" in diagnostics[0].structured["diagnostics_unavailable"][
        "milvus_load_state"
    ]
    assert diagnostics[0].structured["query_segment_state"][0]["segmentID"] == 123
    exhausted = next(
        record
        for record in caplog.records
        if getattr(record, "structured", {}).get("event")
        == "milvus_collection_load_recovery_exhausted"
    )
    assert exhausted.exc_info is not None
    assert exhausted.structured["attempts"] == 2
    assert exhausted.structured["database"] == "kechuang"


@pytest.mark.parametrize("operation", ["count", "dense", "hybrid"])
def test_queries_after_read_only_inspection_load_once(milvus_lifecycle, operation):
    env = milvus_lifecycle
    env.service._existing_collection(load=False)

    for _ in range(3):
        if operation == "count":
            assert env.service.count_by_document_id("doc-1") == 1
        elif operation == "dense":
            assert env.service.search_dense([0.0] * milvus_module.DIM) == []
        else:
            assert env.service.search_hybrid("query", [0.0] * milvus_module.DIM) == []

    assert env.calls.load == [30]
    assert len(env.calls.events) == 3


def test_readiness_rejects_a_missing_required_collection_without_creating_it(milvus_lifecycle):
    env = milvus_lifecycle
    env.state.names = {"expert"}

    with pytest.raises(RuntimeError, match="enterprise.*不存在"):
        asyncio.run(env.service.check_ready())

    assert env.calls.collections == []
    assert env.calls.load == env.calls.disconnect == []


@pytest.mark.parametrize("load_state", [LoadState.NotLoad, LoadState.Loading, LoadState.NotExist])
def test_readiness_reports_non_ready_load_state_without_loading_or_disconnect(
    milvus_lifecycle, load_state,
):
    env = milvus_lifecycle
    _ = env.service.collection
    env.calls.load.clear()
    generation = env.manager.generation
    env.state.load = load_state

    with pytest.raises(RuntimeError, match=load_state.name):
        env.service._check_ready_sync()

    assert env.calls.load == env.calls.disconnect == []
    assert env.manager.generation == generation
    # 就绪状态检查失败后，不能继续沿用旧的已加载标记。
    env.state.load = LoadState.NotLoad
    _ = env.service.collection
    assert env.calls.load == [30]


def test_readiness_accepts_loaded_collection_without_a_load_rpc(milvus_lifecycle):
    env = milvus_lifecycle
    env.state.load = LoadState.Loaded

    asyncio.run(env.service.check_ready())
    _ = env.service.collection

    assert env.calls.load == env.calls.disconnect == []
    assert env.calls.state == [("enterprise", env.manager.alias, 30)]


def test_operation_reloads_collection_once_after_server_side_release(milvus_lifecycle, monkeypatch):
    env = milvus_lifecycle
    env.state.load = LoadState.Loaded
    asyncio.run(env.service.check_ready())
    collection = env.service.collection
    env.state.load = LoadState.NotLoad
    attempts = []

    def search(**_kwargs):
        attempts.append(env.state.load)
        if env.state.load != LoadState.Loaded:
            raise RuntimeError("collection not loaded")
        return []

    monkeypatch.setattr(collection, "search", search)

    assert env.service.search_dense([0.0] * milvus_module.DIM) == []
    assert attempts == [LoadState.NotLoad, LoadState.Loaded]
    assert env.calls.load == [30]


def test_readiness_schema_failure_preserves_shared_connection(milvus_lifecycle):
    env = milvus_lifecycle
    collection = env.service.collection
    collection.schema.fields = []
    generation = env.manager.generation

    with pytest.raises(RuntimeError, match="schema"):
        env.service._check_ready_sync()

    assert env.calls.disconnect == []
    assert env.manager.generation == generation


@pytest.mark.parametrize("error", [
    RuntimeError("collection on recovering"),
    RuntimeError("load timed out"),
    RuntimeError("collection not loaded"),
    RuntimeError("load deadline exceeded"),
])
def test_load_failure_preserves_alias_and_does_not_mark_handle_loaded(milvus_lifecycle, error):
    env = milvus_lifecycle
    handle = env.service._existing_collection(load=False)
    generation = env.manager.generation
    env.state.load = error

    with pytest.raises(RuntimeError, match=str(error)):
        _ = env.service.collection

    assert env.calls.load and set(env.calls.load) == {30}
    assert env.calls.disconnect == []
    assert env.manager.generation == generation
    assert env.service._collection is None
    env.state.load = LoadState.NotLoad
    refreshed = env.service._existing_collection()
    assert refreshed is not handle
    assert env.state.load == LoadState.Loaded
    assert handle in env.calls.collections


@pytest.mark.parametrize("error", [
    RuntimeError("collection on recovering"), RuntimeError("collection not loaded"),
    RuntimeError("load timed out"), RuntimeError("schema mismatch"),
])
def test_readiness_status_errors_preserve_shared_connection(milvus_lifecycle, error):
    env = milvus_lifecycle
    env.service._existing_collection(load=False)
    generation = env.manager.generation
    env.state.load = error

    with pytest.raises(RuntimeError, match=str(error)):
        env.service._check_ready_sync()

    assert env.calls.load == env.calls.disconnect == []
    assert env.manager.generation == generation


def test_readiness_connection_error_invalidates_through_manager(milvus_lifecycle):
    env = milvus_lifecycle
    env.service._existing_collection(load=False)
    env.state.load = RuntimeError("connection reset")

    with pytest.raises(RuntimeError, match="connection reset"):
        env.service._check_ready_sync()

    assert env.calls.disconnect == [env.manager.alias]
    assert env.service._collection is None
    assert env.service._connected is False
    env.state.load = LoadState.Loaded
    env.service._check_ready_sync()
    assert len(env.calls.connect) == 2


@pytest.mark.parametrize("error", [RuntimeError("connection reset"), ConnectionError("offline")])
def test_operation_connection_error_invalidates_through_manager(milvus_lifecycle, error):
    env = milvus_lifecycle
    _ = env.service.collection

    def fail():
        raise error

    with pytest.raises(type(error), match=str(error)):
        env.service._with_collection_recovery_retry(fail)

    assert env.calls.disconnect == [env.manager.alias]
    assert env.service._collection is None
    _ = env.service.collection
    assert len(env.calls.connect) == 2


def test_delayed_failure_does_not_disconnect_a_newer_shared_connection(milvus_lifecycle):
    env = milvus_lifecycle
    _ = env.service.collection
    sibling = MilvusService(collection_name="expert", connection_manager=env.manager)
    env.manager.invalidate()
    _ = sibling.collection
    generation = env.manager.generation

    def fail_old_operation():
        raise RuntimeError("connection reset")

    with pytest.raises(RuntimeError, match="connection reset"):
        env.service._with_collection_recovery_retry(fail_old_operation)

    assert env.calls.disconnect == [env.manager.alias]
    assert env.manager.generation == generation
    _ = env.service.collection
    assert len(env.calls.connect) == 2


def test_mysql_upsert_and_flush_use_initialized_lifecycle(milvus_lifecycle):
    env = milvus_lifecycle
    chunk = {
        "id": "chunk-1", "document_id": "mysql:enterprise", "mysql_id": "1",
        "chunk_text": "content", "chunk_index": 0, "chunk_role": "mysql",
        "dense_vector": [0.0] * milvus_module.DIM,
    }

    assert env.service.upsert_chunks([chunk]) == 1
    assert [event[0] for event in env.calls.events] == ["upsert"]
    env.service.flush()

    assert [event[0] for event in env.calls.events] == ["upsert", "flush"]
    assert env.calls.load == [30]


def test_flush_retries_recovery_without_reloading_or_disconnect(milvus_lifecycle, monkeypatch):
    env = milvus_lifecycle
    collection = env.service.collection
    attempts = []

    def flush():
        attempts.append("flush")
        if len(attempts) == 1:
            raise RuntimeError("collection on recovering")

    monkeypatch.setattr(collection, "flush", flush)
    env.service.flush()

    assert attempts == ["flush", "flush"]
    assert env.calls.load == [30]
    assert env.calls.disconnect == []


def test_replace_chunks_writes_before_deleting_only_stale_ids(milvus_lifecycle, monkeypatch):
    env = milvus_lifecycle
    events = []
    chunks = [{"id": "keep"}, {"id": "new"}]
    monkeypatch.setattr(
        env.service, "_query_ids",
        lambda collection, expr: events.append(("read", collection.name, expr)) or ["old", "keep"],
    )
    monkeypatch.setattr(
        env.service, "upsert_chunks", lambda rows: events.append(("upsert", rows)) or len(rows),
    )
    monkeypatch.setattr(
        env.service, "delete_by_ids", lambda ids: events.append(("delete", ids)),
    )

    assert env.service.replace_chunks(chunks, delete_expr='mysql_id == "1"') == 2
    assert events == [
        ("read", "enterprise", 'mysql_id == "1"'), ("upsert", chunks), ("delete", {"old"}),
    ]


def test_replace_chunks_private_scope_and_missing_collection_contract(milvus_lifecycle, monkeypatch):
    env = milvus_lifecycle
    collection = env.service.collection
    env.service.requires_user_id = True
    expressions = []
    monkeypatch.setattr(
        env.service, "_query_ids", lambda _col, expr: expressions.append(expr) or [],
    )
    with pytest.raises(ValueError, match="user_id"):
        env.service.replace_chunks([], delete_expr='mysql_id == "1"')
    assert env.service.replace_chunks(
        [], delete_expr='mysql_id == "1"', user_id='user"1',
    ) == 0
    assert expressions == ['mysql_id == "1" && user_id == "user\\"1"']
    assert env.service._collection is collection

    monkeypatch.setattr(env.service, "_existing_collection", lambda: None)
    monkeypatch.setattr(env.service, "upsert_chunks", lambda rows: len(rows))
    assert env.service.replace_chunks([{"id": "new"}], delete_expr='mysql_id == "1"') == 1
