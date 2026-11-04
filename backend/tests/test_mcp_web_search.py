import asyncio
from types import SimpleNamespace

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import ValidationError

from core.contracts.tools import ToolContext
from assistant.integrations.mcp_tools.web_search import WebFetchInput, WebSearchInput, register_web_search_tools


class _MCP:
    def __init__(self):
        self.tools = {}

    def tool(self, *, name, **_options):
        def register(function):
            self.tools[name] = function
            return function

        return register


def _registered(runtime):
    mcp = _MCP()
    context = ToolContext(
        user_id="17", user_role="internal", thread_id="thread-1", trace_id="trace-1"
    )
    register_web_search_tools(
        mcp,
        runtime,
        ToolAnnotations(readOnlyHint=True),
        lambda ctx: ctx,
        lambda _runtime, _request: context,
        lambda tool_context, message: {
            "status": "FAILED",
            "data": {},
            "error": message,
            "trace_id": tool_context.trace_id,
        },
    )
    return mcp.tools, context


def test_web_search_mcp_schema_rejects_model_identity_and_bounds_inputs():
    for model, payload in (
        (WebSearchInput, {"query": "芯片", "user_id": "spoofed"}),
        (WebFetchInput, {"url": "https://example.com", "trace_id": "spoofed"}),
        (WebSearchInput, {"query": "x" * 501}),
        (WebFetchInput, {"url": "x" * 2049}),
    ):
        with pytest.raises(ValidationError):
            model.model_validate(payload)


def test_fastmcp_registers_search_and_fetch_without_model_context():
    mcp = FastMCP("web-test")
    register_web_search_tools(
        mcp,
        SimpleNamespace(),
        ToolAnnotations(readOnlyHint=True),
        lambda ctx: ctx.request,
        lambda _runtime, _request: None,
        lambda _context, message: {"error": message},
    )
    tools = {tool.name: tool for tool in mcp._tool_manager.list_tools()}
    assert set(tools) == {"kc_web_search", "kc_web_fetch"}
    assert "ctx" not in tools["kc_web_search"].parameters["properties"]
    assert "ctx" not in tools["kc_web_fetch"].parameters["properties"]


def test_web_search_mcp_uses_existing_adapter_and_authenticated_context():
    calls = []

    class Adapter:
        async def search_tavily(self, arguments, *, context):
            calls.append((arguments, context))
            return {
                "status": "SUCCESS",
                "data": {"results": [{"url": "https://example.com"}]},
                "trace_id": context.trace_id,
            }

    tools, context = _registered(SimpleNamespace(tool_adapter=Adapter()))
    result = asyncio.run(
        tools["kc_web_search"](WebSearchInput(query="芯片", max_results=2), object())
    )
    assert result["status"] == "SUCCESS"
    assert result["trace_id"] == "trace-1"
    assert calls == [({"query": "芯片", "max_results": 2}, context)]


def test_web_fetch_mcp_uses_existing_tavily_client_and_authenticated_context():
    calls = []

    class Client:
        async def extract(self, url):
            calls.append(url)
            return {"status": "SUCCESS", "url": url, "content": "page text"}

    tools, _ = _registered(SimpleNamespace(tool_adapter=SimpleNamespace(web_search=Client())))
    result = asyncio.run(
        tools["kc_web_fetch"](WebFetchInput(url="https://example.com/a"), object())
    )
    assert calls == ["https://example.com/a"]
    assert result == {
        "status": "SUCCESS",
        "data": {"url": "https://example.com/a", "content": "page text"},
        "error": None,
        "trace_id": "trace-1",
    }
