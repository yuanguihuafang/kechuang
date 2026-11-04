import asyncio

import services.persistence.checkpoint as checkpoint_module


def test_persistent_checkpointer_uses_async_pool_lifecycle(monkeypatch):
    events = []
    monkeypatch.setenv("LANGGRAPH_CHECKPOINT_POOL_MIN_SIZE", "1")
    monkeypatch.setenv("LANGGRAPH_CHECKPOINT_POOL_MAX_SIZE", "4")
    monkeypatch.setenv("LANGGRAPH_CHECKPOINT_POOL_TIMEOUT_SECONDS", "10")

    class FakePool:
        def __init__(self, **kwargs):
            events.append(("construct", kwargs))

        async def open(self, *, wait):
            events.append(("open", wait))

        async def close(self):
            events.append(("close",))

    class FakeSaver:
        def __init__(self, pool, *, serde):
            events.append(("saver", pool, serde))

        async def setup(self):
            events.append(("setup",))

    monkeypatch.setattr(checkpoint_module, "AsyncConnectionPool", FakePool)
    monkeypatch.setattr(checkpoint_module, "AsyncPostgresSaver", FakeSaver)

    persistent = checkpoint_module.PersistentCheckpointer("postgresql://test")
    saver = asyncio.run(persistent.start())
    asyncio.run(persistent.close())

    assert isinstance(saver, FakeSaver)
    assert [item[0] for item in events] == ["construct", "open", "saver", "setup", "close"]
    assert events[0][1]["kwargs"]["autocommit"] is True
    assert events[0][1]["kwargs"]["prepare_threshold"] == 0
    assert events[0][1]["min_size"] == 1
    assert events[0][1]["max_size"] == 4
    assert events[0][1]["timeout"] == 10


def test_persistent_checkpointer_rejects_minimum_above_pool_limit(monkeypatch):
    monkeypatch.setenv("LANGGRAPH_CHECKPOINT_POOL_MIN_SIZE", "5")
    monkeypatch.setenv("LANGGRAPH_CHECKPOINT_POOL_MAX_SIZE", "4")

    class FakePool:
        def __init__(self, **_kwargs):
            raise AssertionError("invalid pool configuration must fail before pool creation")

    monkeypatch.setattr(checkpoint_module, "AsyncConnectionPool", FakePool)
    persistent = checkpoint_module.PersistentCheckpointer("postgresql://test")

    try:
        asyncio.run(persistent.start())
    except ValueError as exc:
        assert "LANGGRAPH_CHECKPOINT_POOL_MIN_SIZE" in str(exc)
    else:
        raise AssertionError("expected invalid checkpoint pool size to fail")
