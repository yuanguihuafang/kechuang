"""Inject request-scoped headers into the Kechuang MCP connection."""

from collections.abc import Mapping
from typing import Any

from deerflow.config.extensions_config import ExtensionsConfig

_KC_MCP_SERVER = "kechuang"
_RUN_CONTEXT_IDENTITIES = {"kc_thread_id": "thread_id", "kc_trace_id": "run_id"}


def _load_header_mapping() -> dict[str, str]:
    config = ExtensionsConfig.from_file()
    server = config.mcp_servers.get(_KC_MCP_SERVER)
    options = (server.model_extra or {}).get("headers_from_context") if server else None
    if not isinstance(options, Mapping) or options.get("enabled") is not True:
        raise ValueError("kechuang headers_from_context must be enabled")
    if options.get("on_missing") != "deny":
        raise ValueError("kechuang headers_from_context must deny missing secrets")

    headers = options.get("headers")
    if not isinstance(headers, Mapping) or not headers:
        raise ValueError("kechuang headers_from_context.headers must be configured")
    if any(
        not isinstance(header, str)
        or not header.strip()
        or not isinstance(secret_name, str)
        or not secret_name.strip()
        for header, secret_name in headers.items()
    ):
        raise ValueError("kechuang headers_from_context contains an invalid mapping")
    return dict(headers)


def build_auth_interceptor() -> Any:
    """Build the configured, fail-closed Kechuang context-header interceptor."""
    try:
        header_mapping = _load_header_mapping()
        config_error = False
    except Exception:
        # DeerFlow skips a builder that raises; retain an interceptor that denies
        # only Kechuang calls so malformed config cannot silently drop the headers.
        header_mapping = {}
        config_error = True

    async def interceptor(request: Any, handler: Any) -> Any:
        if request.server_name != _KC_MCP_SERVER:
            return await handler(request)
        if config_error:
            raise PermissionError("Kechuang MCP context-header configuration is invalid")

        runtime = getattr(request, "runtime", None)
        context = getattr(runtime, "context", None)
        if not isinstance(context, Mapping):
            context = {}
        secrets = context.get("secrets", {})
        if not isinstance(secrets, Mapping):
            secrets = {}

        injected_headers: dict[str, str] = {}
        for header, secret_name in header_mapping.items():
            context_key = _RUN_CONTEXT_IDENTITIES.get(secret_name)
            value = context.get(context_key) if context_key is not None and context_key in context else secrets.get(secret_name)
            if not isinstance(value, str) or not value.strip():
                raise PermissionError(f"Required MCP context secret is missing: {secret_name}")
            value = value.strip()
            if "\r" in value or "\n" in value:
                raise PermissionError(f"MCP context secret has an invalid value: {secret_name}")
            injected_headers[header] = value

        headers = dict(request.headers or {})
        headers.update(injected_headers)
        return await handler(request.override(headers=headers))

    return interceptor
