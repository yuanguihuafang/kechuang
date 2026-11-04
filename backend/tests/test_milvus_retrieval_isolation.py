import asyncio
import logging
from types import SimpleNamespace

import pytest

from agents.retrieval_agent.application.retrieval_service import RetrievalAgent
from agents.retrieval_agent.retrieval.search_service import SearchResult, SearchService
from agents.retrieval_agent.schemas import RetrievalHit


class _Embedding:
    async def encode_dense(self, _texts):
        return [[0.0]]


class _Milvus:
    def __init__(self, name, *, hybrid_error=None, prefix_error=None, hits=None):
        self.collection_name = name
        self.hybrid_error = hybrid_error
        self.prefix_error = prefix_error
        self.hits = hits or []
        self.calls = []

    def search_hybrid(self, **_kwargs):
        self.calls.append("hybrid")
        if self.hybrid_error:
            raise self.hybrid_error
        return self.hits

    def search_text_prefix(self, **_kwargs):
        self.calls.append("prefix")
        if self.prefix_error:
            raise self.prefix_error
        return self.hits


def _service(milvus_services):
    service = object.__new__(SearchService)
    service.embedding_service = _Embedding()
    service.public_milvus = {item.collection_name: item for item in milvus_services}
    return service


def test_recall_keeps_other_collections_when_one_collection_load_fails(caplog):
    broken = _Milvus("demand", hybrid_error=RuntimeError("pk_column != nullptr"))
    healthy = _Milvus(
        "enterprise",
        hits=[RetrievalHit(record_id="ok", score=1.0, source="milvus", metadata={})],
    )
    service = _service([broken, healthy])

    with caplog.at_level(logging.ERROR):
        results = asyncio.run(
            service.recall(
                ["query"], [(broken, None), (healthy, None)], recall_k=3
            )
        )

    assert results.hit_lists == [[], healthy.hits]
    assert results.unavailable_collections == ["demand"]
    assert broken.calls == ["hybrid"]
    assert healthy.calls == ["hybrid"]
    record = next(
        item for item in caplog.records
        if getattr(item, "structured", {}).get("event") == "milvus_knowledge_unavailable"
    )
    assert record.structured["collection"] == "demand"
    assert record.structured["error"] == "pk_column != nullptr"
    assert record.exc_info is not None


def test_recall_does_not_run_text_fallback_for_a_failed_collection(caplog):
    broken = _Milvus("demand", hybrid_error=RuntimeError("load failed"))
    service = _service([broken])

    with caplog.at_level(logging.ERROR), pytest.raises(RuntimeError, match="all Milvus"):
        asyncio.run(service.recall(["query"], [(broken, None)], recall_k=3))

    assert broken.calls == ["hybrid"]


def test_recall_runs_text_fallback_for_healthy_empty_collection():
    healthy = _Milvus("enterprise", hits=[])
    service = _service([healthy])

    results = asyncio.run(service.recall(["query"], [(healthy, None)], recall_k=3))

    assert results.hit_lists == [[]]
    assert results.unavailable_collections == []
    assert healthy.calls == ["hybrid", "prefix"]


def test_format_evidence_exposes_degraded_collection_without_claiming_no_matches():
    agent = object.__new__(RetrievalAgent)

    state = asyncio.run(
        agent._format_evidence(
            {
                "request": SimpleNamespace(query="query"),
                "result": SearchResult(
                    hits=[], unavailable_collections=["demand"]
                ),
            }
        )
    )

    response = state["response"]
    assert response.status == "DEGRADED"
    assert response.trace["unavailable_collections"] == ["demand"]


def test_format_evidence_reports_partial_success_when_other_collection_has_hits():
    class ImageManifest:
        async def find_many(self, _locations):
            return [[]]

    agent = object.__new__(RetrievalAgent)
    agent.image_manifest = ImageManifest()

    state = asyncio.run(
        agent._format_evidence(
            {
                "request": SimpleNamespace(query="query"),
                "result": SearchResult(
                    hits=[
                        RetrievalHit(
                            record_id="available",
                            score=1.0,
                            source="milvus",
                            metadata={
                                "chunk_text": "available evidence",
                                "source_id": "source-available",
                            },
                        )
                    ],
                    unavailable_collections=["demand"],
                ),
            }
        )
    )

    response = state["response"]
    assert response.status == "PARTIAL_SUCCESS"
    assert response.trace["unavailable_collections"] == ["demand"]
