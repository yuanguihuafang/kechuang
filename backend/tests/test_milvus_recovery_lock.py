from contextlib import contextmanager
from types import SimpleNamespace

import pytest

import services.persistence.sqlalchemy as sqlalchemy_module
import services.vector.milvus.milvus_service as milvus_module
from services.vector.milvus.milvus_service import MilvusService


@pytest.fixture
def service(monkeypatch):
    monkeypatch.setattr(
        "runtime.config.get_settings",
        lambda: SimpleNamespace(
            milvus_uri="http://milvus",
            milvus_database="kechuang",
            milvus_username=None,
            milvus_password=None,
            database_url="postgresql://db/platform",
        ),
    )
    manager = milvus_module.MilvusConnectionManager(
        uri="http://milvus", database="kechuang", username=None, password=None
    )
    return MilvusService(
        collection_name="demand",
        alias_name="kc_public_demand",
        connection_manager=manager,
    )


def test_public_write_fence_uses_shared_advisory_lock_and_nests_safely(monkeypatch, service):
    calls = []

    @contextmanager
    def lock(dsn, key, *, shared):
        calls.append((dsn, key, shared))
        yield

    monkeypatch.setattr(sqlalchemy_module, "postgres_advisory_lock", lock, raising=False)

    with service._write_guard():
        with service._write_guard():
            pass

    assert len(calls) == 1
    assert calls[0][0] == "postgresql://db/platform"
    assert calls[0][2] is True


def test_public_write_fence_fails_closed_without_database_url(monkeypatch):
    monkeypatch.setattr(
        "runtime.config.get_settings",
        lambda: SimpleNamespace(
            milvus_uri="http://milvus",
            milvus_database="kechuang",
            milvus_username=None,
            milvus_password=None,
            database_url=None,
        ),
    )
    manager = milvus_module.MilvusConnectionManager(
        uri="http://milvus", database="kechuang", username=None, password=None
    )
    service = MilvusService(
        collection_name="demand", alias_name="kc_public_demand", connection_manager=manager
    )

    with pytest.raises(RuntimeError, match="PostgreSQL.*写入栅栏"):
        with service._write_guard():
            pass


def test_collection_recovery_maintenance_guard_uses_exclusive_advisory_lock(
    monkeypatch, service
):
    calls = []

    @contextmanager
    def lock(dsn, key, *, shared):
        calls.append((dsn, key, shared))
        yield

    monkeypatch.setattr(sqlalchemy_module, "postgres_advisory_lock", lock, raising=False)

    with service._maintenance_guard():
        pass

    assert len(calls) == 1
    assert calls[0][0] == "postgresql://db/platform"
    assert calls[0][1] == milvus_module._milvus_write_lock_key("kc_public_demand")
    assert calls[0][2] is False


def test_shadow_insert_deduplicates_existing_and_in_batch_primary_keys(caplog):
    service = object.__new__(MilvusService)
    service.alias_name = None
    service.collection_name = "demand__g1"
    service._chunk_to_row = lambda chunk: dict(chunk)
    service._existing_collection = lambda: SimpleNamespace(query=lambda **_: [{"id": "old"}])
    inserted = []
    service._insert_rows = lambda rows, *, flush: inserted.extend(rows)

    count = service.insert_chunks(
        [{"id": "old"}, {"id": "new"}, {"id": "new"}], flush=False
    )

    assert count == 3
    assert inserted == [{"id": "new"}]
    assert any(
        getattr(record, "structured", {}).get("duplicate_count") == 2
        for record in caplog.records
    )


@pytest.mark.parametrize("existing", [False, True])
def test_shadow_insert_rejects_conflicting_primary_key_without_writing(existing):
    service = object.__new__(MilvusService)
    service.alias_name = None
    service.collection_name = "solution__shadow"
    service._chunk_to_row = lambda chunk: dict(chunk)
    service._existing_collection = lambda: SimpleNamespace(
        query=lambda **_: [{"id": "shared", "chunk_text": "original"}]
    )
    service._insert_rows = lambda *_args, **_kwargs: pytest.fail("conflict must fail before insert")
    rows = [{"id": "shared", "chunk_text": "changed"}]
    if not existing:
        rows.insert(0, {"id": "shared", "chunk_text": "original"})

    with pytest.raises(ValueError, match="主键内容冲突"):
        service.insert_chunks(rows)


def test_shadow_insert_accepts_identical_source_on_second_request():
    service = object.__new__(MilvusService)
    service.alias_name = None
    service.collection_name = "solution__shadow"
    service._chunk_to_row = lambda chunk: dict(chunk)
    stored = []

    def query(**kwargs):
        assert kwargs["consistency_level"] == "Strong"
        return stored[:]

    service._existing_collection = lambda: SimpleNamespace(query=query)
    service._insert_rows = lambda rows, **_: stored.extend(rows)
    chunk = {"id": "source:shared:document:1", "chunk_text": "source text", "update_time": "first"}
    assert service.insert_chunks([chunk]) == 1
    assert service.insert_chunks([{**chunk, "update_time": "second"}]) == 1
    assert stored == [chunk]

