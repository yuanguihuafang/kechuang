import asyncio
from contextlib import contextmanager
from threading import Event

import pytest

from services.persistence.retention import storage_reference_guard


def test_nested_source_operations_share_one_postgres_lock(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://test")
    events = []

    @contextmanager
    def database_lock(dsn, key, *, shared):
        assert dsn == "postgresql://test" and key == 0x4B43534F55524345 and shared is False
        events.append("acquired")
        try:
            yield
        finally:
            events.append("released")

    monkeypatch.setattr("services.persistence.sqlalchemy.postgres_advisory_lock", database_lock)

    async def run():
        async with storage_reference_guard():
            async with storage_reference_guard():
                assert events == ["acquired"]
    asyncio.run(run())
    assert events == ["acquired", "released"]


def test_cancelled_lock_acquisition_is_released(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://test")
    entered, release, exited = Event(), Event(), Event()

    @contextmanager
    def database_lock(*_args, **_kwargs):
        entered.set()
        assert release.wait(2)
        try:
            yield
        finally:
            exited.set()

    monkeypatch.setattr("services.persistence.sqlalchemy.postgres_advisory_lock", database_lock)

    async def write():
        async with storage_reference_guard():
            pytest.fail("cancelled operation must not mutate sources")

    async def run():
        pending = asyncio.create_task(write())
        assert await asyncio.to_thread(entered.wait, 2)
        pending.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert exited.is_set()
    asyncio.run(run())
