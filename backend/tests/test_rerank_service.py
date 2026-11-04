import asyncio

import httpx
import pytest

from agents.retrieval_agent.retrieval.rerank_service import RerankService, RerankUnavailableError


def test_rerank_failure_never_returns_unranked_candidates(monkeypatch):
    async def fail(*_args, **_kwargs):
        raise httpx.ReadTimeout("provider unavailable")

    monkeypatch.setattr(httpx.AsyncClient, "post", fail)
    with pytest.raises(RerankUnavailableError):
        asyncio.run(RerankService().rerank("设备采购", ["候选一", "候选二"], top_k=3))


def test_rerank_http_timeout_is_one_minute(monkeypatch):
    captured = {}
    original_client = httpx.AsyncClient

    async def respond(_request):
        return httpx.Response(200, json=[{"index": 0, "relevance_score": 0.9}])

    class CapturingClient(original_client):
        def __init__(self, *args, **kwargs):
            captured["timeout"] = kwargs.get("timeout")
            kwargs["transport"] = httpx.MockTransport(respond)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", CapturingClient)
    asyncio.run(RerankService().rerank("设备采购", ["候选一"], top_k=1))

    assert captured["timeout"] == 60.0


def test_rerank_validates_and_orders_provider_scores():
    results = RerankService._normalize_results(
        [
            {"index": 0, "relevance_score": 0.1},
            {"index": 1, "relevance_score": 0.9},
            {"index": 1, "relevance_score": 1.0},
            {"index": 2, "relevance_score": 0.5},
            {"index": 3, "relevance_score": 0.8},
            {"index": 99, "relevance_score": 1.0},
        ],
        ["一", "二", "三", "四"],
        3,
    )
    assert [item.index for item in results] == [1, 3, 2]
