import asyncio

import pytest

import runtime.bootstrap as bootstrap_module
import services.persistence.postgres as postgres_module
import services.persistence.sqlalchemy as sqlalchemy_module


def test_psycopg_pool_uses_configured_bounds(monkeypatch):
    created = []

    class FakePool:
        def __init__(self, **options):
            self.options = options
            created.append(self)

        def connection(self):
            from contextlib import nullcontext

            return nullcontext(object())

        def close(self):
            return None

    postgres_module._POOLS.clear()
    monkeypatch.setenv("POSTGRES_POOL_MIN_SIZE", "2")
    monkeypatch.setenv("POSTGRES_POOL_MAX_SIZE", "5")
    monkeypatch.setenv("POSTGRES_POOL_TIMEOUT_SECONDS", "3")
    monkeypatch.setattr(postgres_module, "ConnectionPool", FakePool)

    with postgres_module._pooled_connection("postgresql://test/db"):
        pass

    assert len(created) == 1
    assert created[0].options["min_size"] == 2
    assert created[0].options["max_size"] == 5
    assert created[0].options["timeout"] == 3
    postgres_module.close_postgres_pools()


@pytest.mark.parametrize(
    ("min_size", "max_size", "timeout"),
    [("5", "4", "10"), ("1", "8", "nan")],
)
def test_psycopg_pool_rejects_invalid_configuration(monkeypatch, min_size, max_size, timeout):
    postgres_module._POOLS.clear()
    monkeypatch.setenv("POSTGRES_POOL_MIN_SIZE", min_size)
    monkeypatch.setenv("POSTGRES_POOL_MAX_SIZE", max_size)
    monkeypatch.setenv("POSTGRES_POOL_TIMEOUT_SECONDS", timeout)

    with pytest.raises(ValueError, match="PostgreSQL pool requires"):
        with postgres_module._pooled_connection("postgresql://test/db"):
            pass


def test_sqlalchemy_engine_uses_bounded_pool_and_is_reused(monkeypatch):
    created = []

    class FakeEngine:
        def __init__(self, url, options):
            self.url = url
            self.options = options
            self.disposals = 0

        def dispose(self):
            self.disposals += 1

    def create_engine(url, **options):
        engine = FakeEngine(url, options)
        created.append(engine)
        return engine

    monkeypatch.setenv("POSTGRES_SQLALCHEMY_POOL_SIZE", "3")
    monkeypatch.setenv("POSTGRES_SQLALCHEMY_MAX_OVERFLOW", "1")
    monkeypatch.setattr(sqlalchemy_module, "create_engine", create_engine)

    try:
        first = sqlalchemy_module.get_engine("postgresql://test/db")
        second = sqlalchemy_module.get_engine("postgresql://test/db")

        assert first is second
        assert first.url == "postgresql+psycopg://test/db"
        assert first.options["pool_size"] == 3
        assert first.options["max_overflow"] == 1
        assert first.options["pool_pre_ping"] is True
    finally:
        sqlalchemy_module.close_sqlalchemy_engines()

    assert len(created) == 1
    assert created[0].disposals == 1


@pytest.mark.parametrize(
    ("pool_size", "max_overflow"),
    [("0", "1"), ("2", "-1"), ("bad", "1")],
)
def test_sqlalchemy_rejects_invalid_pool_configuration(monkeypatch, pool_size, max_overflow):
    monkeypatch.setenv("POSTGRES_SQLALCHEMY_POOL_SIZE", pool_size)
    monkeypatch.setenv("POSTGRES_SQLALCHEMY_MAX_OVERFLOW", max_overflow)

    with pytest.raises(ValueError):
        sqlalchemy_module.get_engine("postgresql://test/db")


def test_postgres_pool_shutdown_attempts_every_pool_and_sqlalchemy(monkeypatch):
    closed = []

    class FakePool:
        def __init__(self, name, fail=False):
            self.name = name
            self.fail = fail

        def close(self):
            closed.append(self.name)
            if self.fail:
                raise RuntimeError(self.name)

    postgres_module._POOLS.clear()
    postgres_module._POOLS.update(
        {
            "first": FakePool("first", fail=True),
            "second": FakePool("second"),
        }
    )
    monkeypatch.setattr(
        postgres_module,
        "close_sqlalchemy_engines",
        lambda: closed.append("sqlalchemy"),
        raising=False,
    )

    with pytest.raises(RuntimeError):
        postgres_module.close_postgres_pools()

    assert closed == ["first", "second", "sqlalchemy"]
    assert postgres_module._POOLS == {}


def test_sqlalchemy_shutdown_attempts_every_engine_after_failure():
    disposed = []

    class FakeEngine:
        def __init__(self, name, fail=False):
            self.name = name
            self.fail = fail

        def dispose(self):
            disposed.append(self.name)
            if self.fail:
                raise RuntimeError(self.name)

    sqlalchemy_module._ENGINES.clear()
    sqlalchemy_module._ENGINES.update(
        {
            "first": FakeEngine("first", fail=True),
            "second": FakeEngine("second"),
        }
    )

    with pytest.raises(RuntimeError):
        sqlalchemy_module.close_sqlalchemy_engines()

    assert disposed == ["first", "second"]
    assert sqlalchemy_module._ENGINES == {}


def test_checkpointer_close_failure_does_not_skip_postgres_pool_cleanup(monkeypatch):
    events = []

    class FakeCheckpointer:
        async def close(self):
            events.append("checkpointer")
            raise RuntimeError("close failed")

    monkeypatch.setattr(
        bootstrap_module,
        "close_postgres_pools",
        lambda: events.append("postgres_pools"),
    )

    asyncio.run(bootstrap_module._close_database_resources(FakeCheckpointer()))

    assert events == ["checkpointer", "postgres_pools"]
