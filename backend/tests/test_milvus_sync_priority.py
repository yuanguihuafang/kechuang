import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import services.vector.milvus.milvus_service as milvus_service_module
import services.vector.milvus.sync_priority as priority
from services.vector.milvus.milvus_service import DIM, MilvusService


def _wait_for_counter(gate, name: str, expected: int) -> None:
    with gate.condition:
        assert gate.condition.wait_for(
            lambda: getattr(gate, name) == expected,
            timeout=2,
        )


def test_searches_can_run_concurrently():
    key = f"test-{uuid.uuid4()}"
    entered = [threading.Event(), threading.Event()]
    release = threading.Event()

    def search(index: int) -> None:
        with priority.milvus_search(key):
            entered[index].set()
            assert release.wait(timeout=2)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(search, 0)
        second = pool.submit(search, 1)
        assert entered[0].wait(timeout=2)
        assert entered[1].wait(timeout=2)
        release.set()
        first.result(timeout=2)
        second.result(timeout=2)


def test_waiting_search_takes_priority_over_queued_mysql_write():
    key = f"test-{uuid.uuid4()}"
    gate = priority._gate_for(key)
    first_write_entered = threading.Event()
    release_first_write = threading.Event()
    second_search_entered = threading.Event()
    release_second_search = threading.Event()
    second_write_entered = threading.Event()
    order = []
    order_lock = threading.Lock()

    def add_order(label: str) -> None:
        with order_lock:
            order.append(label)

    def first_write() -> None:
        with priority.mysql_sync_write(key):
            first_write_entered.set()
            assert release_first_write.wait(timeout=2)

    def second_search() -> None:
        with priority.milvus_search(key):
            add_order("search")
            second_search_entered.set()
            assert release_second_search.wait(timeout=2)

    def second_write() -> None:
        with priority.mysql_sync_write(key):
            add_order("write")
            second_write_entered.set()

    with ThreadPoolExecutor(max_workers=3) as pool:
        first = pool.submit(first_write)
        assert first_write_entered.wait(timeout=2)
        writer = pool.submit(second_write)
        _wait_for_counter(gate, "waiting_writers", 1)
        second = pool.submit(second_search)
        _wait_for_counter(gate, "waiting_readers", 1)

        release_first_write.set()
        assert second_search_entered.wait(timeout=2)
        assert not second_write_entered.is_set()

        release_second_search.set()
        first.result(timeout=2)
        second.result(timeout=2)
        writer.result(timeout=2)

    assert order == ["search", "write"]


def test_mysql_upsert_yields_between_milvus_batches(monkeypatch):
    key = f"test-{uuid.uuid4()}"
    first_upsert_entered = threading.Event()
    release_first_upsert = threading.Event()
    search_entered = threading.Event()
    events = []
    events_lock = threading.Lock()

    def record(event: str) -> None:
        with events_lock:
            events.append(event)

    class Collection:
        upsert_count = 0

        def upsert(self, _rows):
            self.upsert_count += 1
            if self.upsert_count == 1:
                first_upsert_entered.set()
                assert release_first_upsert.wait(timeout=2)
            record(f"upsert-{self.upsert_count}")

        def search(self, **_kwargs):
            record("search")
            search_entered.set()
            return []

    collection = Collection()
    service = object.__new__(MilvusService)
    service.uri = "test-uri"
    service.database = "test-db"
    service.alias = key
    service.collection_name = "test-collection"
    service.requires_user_id = False
    service._collection = collection
    service._collection_loaded = True
    service._ensure_connection = lambda: None
    service._load_collection = lambda _collection: None
    service._with_collection_recovery_retry = lambda operation: operation()
    monkeypatch.setattr(milvus_service_module, "MILVUS_SAFE_UPSERT_BYTES", 7_000)

    chunks = [
        {
            "id": f"chunk-{index}",
            "document_id": "mysql:demand",
            "mysql_id": str(index),
            "source_id": "",
            "chunk_index": index,
            "source_locator": f"mysql:{index}",
            "chunk_text": "x" * 1_000,
            "dense_vector": [0.0] * DIM,
            "chunk_role": "mysql",
            "field_names": [],
            "status": "active",
            "update_time": "2026-10-10T00:00:00+00:00",
        }
        for index in range(2)
    ]

    def search() -> None:
        service.search_dense([0.0] * DIM)

    with ThreadPoolExecutor(max_workers=2) as pool:
        sync = pool.submit(service.upsert_chunks, chunks)
        assert first_upsert_entered.wait(timeout=2)
        gate = priority._gate_for(service.priority_key)
        retrieval = pool.submit(search)
        _wait_for_counter(gate, "waiting_readers", 1)
        release_first_upsert.set()
        assert search_entered.wait(timeout=2)
        retrieval.result(timeout=10)
        sync.result(timeout=10)

    assert events == ["upsert-1", "search", "upsert-2"]
