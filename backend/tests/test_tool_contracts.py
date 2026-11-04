import asyncio
import importlib

import pytest

from agents.retrieval_agent.schemas import RetrievalRequest
from core.contracts.agent import AgentResult, ResultMeta
from core.contracts.tools import ToolContext
from runtime.tool_adapters import adapters as adapter_module
from runtime.tool_adapters.adapters import InProcessToolAdapter
from services.observability.metrics import MetricsStore

metrics_module = importlib.import_module("services.observability.metrics")


class _FakeRetrievalAgent:
    def __init__(self):
        self.context = None
        self.request = None

    async def invoke(self, context, request):
        self.context = context
        self.request = request
        return AgentResult(
            status="SUCCESS",
            data={"status": "SUCCESS", "records": [{"id": "r-1"}]},
            meta=ResultMeta(
                request_id=context.request_id,
                trace_id=context.trace_id,
                agent=context.agent,
                operation="search",
            ),
        )


class _FakeRetrieval:
    RetrievalRequest = RetrievalRequest

    def __init__(self, agent):
        self.agent = agent


class _FakePerception:
    async def upload_document(self, **_kwargs):
        return {"status": "SUCCESS", "document_id": "doc-1"}


def test_backend_tool_context_overrides_model_identity_and_is_traceable():
    agent = _FakeRetrievalAgent()
    adapter = InProcessToolAdapter(None, _FakeRetrieval(agent))
    backend_context = ToolContext(
        user_id="token-user",
        user_role="external",
        thread_id="thread-1",
        trace_id="trace-1",
    )

    result = asyncio.run(
        adapter.search_knowledge(
            {
                "query": "q",
                "identity": {"user_id": "model-user", "user_role": "admin"},
                "thread_id": "model-thread",
                "trace_id": "model-trace",
            },
            context=backend_context,
        )
    )

    assert agent.context.identity == {"user_id": "token-user", "user_role": "external"}
    assert agent.context.thread_id == "thread-1"
    assert agent.context.trace_id == "trace-1"
    assert agent.request.identity.user_id == "token-user"
    assert agent.request.identity.user_role.value == "external"
    assert result["status"] == "SUCCESS"
    assert result["data"]["records"] == [{"id": "r-1"}]
    assert result["error"] is None
    assert result["trace_id"] == "trace-1"


def test_search_knowledge_records_total_retrieval_duration(monkeypatch):
    monkeypatch.setattr(adapter_module, "metrics", MetricsStore(), raising=False)
    monkeypatch.setattr(metrics_module, "_persist_metric", lambda **_kwargs: None)
    adapter = InProcessToolAdapter(None, _FakeRetrieval(_FakeRetrievalAgent()))

    context = ToolContext(user_id="u", user_role="internal", trace_id="trace")
    asyncio.run(adapter.search_knowledge({"query": "q"}, context=context))

    stage = adapter_module.metrics.snapshot()["stages"]["retrieval.total"]
    assert stage["count"] == 1
    assert stage["error_count"] == 0


def test_search_knowledge_records_failed_retrieval_and_reraises(monkeypatch):
    monkeypatch.setattr(adapter_module, "metrics", MetricsStore(), raising=False)
    monkeypatch.setattr(metrics_module, "_persist_metric", lambda **_kwargs: None)
    agent = _FakeRetrievalAgent()

    async def fail_invoke(_context, _request):
        raise RuntimeError("retrieval failed")

    agent.invoke = fail_invoke
    adapter = InProcessToolAdapter(None, _FakeRetrieval(agent))

    with pytest.raises(RuntimeError, match="retrieval failed"):
        context = ToolContext(user_id="u", user_role="internal", trace_id="trace")
        asyncio.run(adapter.search_knowledge({"query": "q"}, context=context))

    stage = adapter_module.metrics.snapshot()["stages"]["retrieval.total"]
    assert stage["count"] == 1
    assert stage["error_count"] == 1


def test_tool_result_keeps_legacy_top_level_fields():
    adapter = InProcessToolAdapter(_FakePerception(), None)

    result = asyncio.run(adapter.upload_document({"file": "file", "user_id": "legacy-user"}))

    assert result["document_id"] == "doc-1"
    assert result["status"] == "SUCCESS"
    assert result["data"]["document_id"] == "doc-1"
    assert result["trace_id"]
