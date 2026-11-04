"""Business-owned Tavily search and page extraction MCP tools."""

from typing import Any

from mcp.server.fastmcp import Context
from pydantic import BaseModel, ConfigDict, Field


class WebSearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    query: str = Field(min_length=1, max_length=500)
    max_results: int = Field(default=5, ge=1, le=20)


class WebFetchInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    url: str = Field(min_length=1, max_length=2048)


def register_web_search_tools(
    mcp: Any,
    runtime: Any,
    annotations: Any,
    request_from_context: Any,
    resolve_context: Any,
    failure: Any,
) -> None:
    web_annotations = annotations.model_copy(update={"openWorldHint": True})

    @mcp.tool(
        name="kc_web_search",
        description=(
            "Search the public web through the business Tavily service when the user's goal calls for "
            "public web information, or the user requests/selects that source. Choose it from the full "
            "goal, not from an entity mention or generic 'look up' wording alone."
        ),
        annotations=web_annotations,
        structured_output=True,
    )
    async def kc_web_search(params: WebSearchInput, ctx: Context) -> dict[str, Any]:
        context = resolve_context(runtime, request_from_context(ctx))
        adapter = getattr(runtime, "tool_adapter", None)
        if adapter is None:
            return failure(context, "联网搜索服务尚未就绪")
        try:
            return await adapter.search_tavily(
                {"query": params.query, "max_results": params.max_results},
                context=context,
            )
        except Exception:  # noqa: BLE001 - 保持 MCP 错误响应结构稳定
            return failure(context, "联网搜索执行失败")

    @mcp.tool(
        name="kc_web_fetch",
        description="Extract one public web page through the business Tavily service.",
        annotations=web_annotations,
        structured_output=True,
    )
    async def kc_web_fetch(params: WebFetchInput, ctx: Context) -> dict[str, Any]:
        context = resolve_context(runtime, request_from_context(ctx))
        adapter = getattr(runtime, "tool_adapter", None)
        client = getattr(adapter, "web_search", None)
        if client is None:
            return failure(context, "网页提取服务尚未就绪")
        try:
            result = await client.extract(params.url)
            data = {key: result[key] for key in ("url", "content", "error_code") if key in result}
            return {
                "status": result["status"],
                "data": data,
                "error": result.get("error"),
                "trace_id": context.trace_id,
            }
        except Exception:  # noqa: BLE001 - 保持 MCP 错误响应结构稳定
            return failure(context, "网页提取执行失败")
