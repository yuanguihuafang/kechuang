from unittest.mock import AsyncMock, patch
from types import SimpleNamespace

import pytest
from deerflow.config.extensions_config import ExtensionsConfig
from langchain_mcp_adapters.interceptors import MCPToolCallRequest

from my_package.mcp.auth import build_auth_interceptor


def _extensions_config(headers_from_context=None):
    rule = headers_from_context or {
        "enabled": True,
        "headers": {
            "X-KC-Thread-ID": "kc_thread_id",
            "X-Trace-ID": "kc_trace_id",
        },
        "on_missing": "deny",
    }
    return ExtensionsConfig.model_validate(
        {"mcpServers": {"kechuang": {"headers_from_context": rule}}}
    )


def _build_interceptor(headers_from_context=None):
    with patch(
        "my_package.mcp.auth.ExtensionsConfig.from_file",
        return_value=_extensions_config(headers_from_context),
    ):
        return build_auth_interceptor()


@pytest.mark.asyncio
async def test_injects_run_secrets_only_into_configured_kc_request():
    interceptor = _build_interceptor()
    request = MCPToolCallRequest(
        name="kc_draft_create",
        args={},
        server_name="kechuang",
        headers={"X-Existing": "kept"},
        runtime=SimpleNamespace(
            context={
                "secrets": {
                    "kc_thread_id": "session-3-test",
                    "kc_trace_id": "trace-test",
                }
            }
        ),
    )
    handler = AsyncMock(return_value="ok")
    assert await interceptor(request, handler) == "ok"

    forwarded = handler.await_args.args[0]
    assert forwarded.headers == {
        "X-Existing": "kept",
        "X-KC-Thread-ID": "session-3-test",
        "X-Trace-ID": "trace-test",
    }
    assert request.headers == {"X-Existing": "kept"}


@pytest.mark.asyncio
async def test_non_kc_server_is_not_changed():
    interceptor = _build_interceptor()
    request = MCPToolCallRequest(
        name="search", args={}, server_name="public-search", headers={"X-Existing": "kept"}
    )
    handler = AsyncMock(return_value="ok")

    assert await interceptor(request, handler) == "ok"

    assert handler.await_args.args[0] is request


@pytest.mark.asyncio
async def test_missing_required_context_secret_denies_kc_tool_call():
    interceptor = _build_interceptor()
    request = MCPToolCallRequest(name="kc_draft_create", args={}, server_name="kechuang")
    handler = AsyncMock(return_value="must not run")
    request.runtime = SimpleNamespace(
        context={"secrets": {"kc_trace_id": "trace-test"}}
    )

    with pytest.raises(PermissionError, match="kc_thread_id"):
        await interceptor(request, handler)

    handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_rejects_header_injection_from_context_secret():
    interceptor = _build_interceptor()
    request = MCPToolCallRequest(name="kc_draft_create", args={}, server_name="kechuang")
    request.runtime = SimpleNamespace(
        context={
            "secrets": {
                "kc_thread_id": "session-3\r\nAuthorization: Bearer injected",
                "kc_trace_id": "trace-test",
            }
        }
    )
    handler = AsyncMock(return_value="must not run")

    with pytest.raises(PermissionError, match="invalid value"):
        await interceptor(request, handler)

    handler.assert_not_awaited()
