from concurrent.futures import ThreadPoolExecutor
from contextvars import Context

import pytest

from agents.retrieval_agent import routes


class _Agent:
    def __init__(self):
        self.closed = 0

    def close(self):
        self.closed += 1


def _reset_routes():
    if hasattr(routes, "close"):
        routes.close()
    else:
        routes._route_context.set(None)


def test_retrieval_route_context_is_shared_across_contextvars(monkeypatch):
    _reset_routes()
    created = []
    monkeypatch.setattr(routes, "RetrievalAgent", lambda: created.append(_Agent()) or created[-1])
    monkeypatch.setattr(routes, "SupervisorTaskStore", lambda **_kwargs: object())

    first = Context().run(routes._context)
    second = Context().run(routes._context)

    assert second is first
    assert second.agent is first.agent
    assert len(created) == 1
    routes.close()


def test_retrieval_route_context_initializes_once_concurrently(monkeypatch):
    _reset_routes()
    created = []
    monkeypatch.setattr(routes, "RetrievalAgent", lambda: created.append(_Agent()) or created[-1])
    monkeypatch.setattr(routes, "SupervisorTaskStore", lambda **_kwargs: object())
    contexts = [Context() for _ in range(12)]

    with ThreadPoolExecutor(max_workers=len(contexts)) as pool:
        results = list(pool.map(lambda context: context.run(routes._context), contexts))

    assert all(context is results[0] for context in results)
    assert len(created) == 1
    routes.close()


def test_close_releases_route_context_for_the_next_lifespan(monkeypatch):
    _reset_routes()
    created = []
    monkeypatch.setattr(routes, "RetrievalAgent", lambda: created.append(_Agent()) or created[-1])
    monkeypatch.setattr(routes, "SupervisorTaskStore", lambda **_kwargs: object())

    first = routes._context()
    routes.close()
    second = routes._context()

    assert first.agent.closed == 1
    assert second is not first
    assert len(created) == 2
    routes.close()


def test_close_clears_route_context_when_agent_cleanup_fails(monkeypatch):
    _reset_routes()
    created = []

    class BrokenAgent(_Agent):
        def close(self):
            raise RuntimeError("close failed")

    monkeypatch.setattr(
        routes, "RetrievalAgent", lambda: created.append(BrokenAgent()) or created[-1]
    )
    monkeypatch.setattr(routes, "SupervisorTaskStore", lambda **_kwargs: object())
    routes._context()

    with pytest.raises(RuntimeError, match="close failed"):
        routes.close()

    monkeypatch.setattr(routes, "RetrievalAgent", lambda: created.append(_Agent()) or created[-1])
    next_context = routes._context()
    assert next_context.agent is created[-1]
    assert len(created) == 2
    routes.close()


def test_route_close_closes_shared_milvus_connection_once(monkeypatch):
    import agents.retrieval_agent.retrieval.search_service as search_module
    import services.vector.milvus.milvus_service as milvus_module
    from agents.retrieval_agent.application.retrieval_service import RetrievalAgent

    _reset_routes()
    calls = []

    class Connections:
        def disconnect(self, *, alias):
            calls.append(alias)

    connections = Connections()
    monkeypatch.setattr(
        milvus_module,
        "_lazy_import_pymilvus",
        lambda: (None, None, None, None, None, None, None, None, connections, None),
    )
    manager = milvus_module.MilvusConnectionManager(
        uri="http://milvus",
        database="kechuang",
        username=None,
        password=None,
    )
    manager._connected = True
    manager._generation = 1
    search_service = object.__new__(search_module.SearchService)
    search_service.connection_manager = manager
    agent = object.__new__(RetrievalAgent)
    agent.search_service = search_service
    monkeypatch.setattr(routes, "RetrievalAgent", lambda: agent)
    monkeypatch.setattr(routes, "SupervisorTaskStore", lambda **_kwargs: object())

    routes._context()
    routes.close()
    routes.close()

    assert calls == [manager.alias]
