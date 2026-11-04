from pathlib import Path


def test_mysql_sync_status_get_is_exempt_from_gateway_rate_limit() -> None:
    source = (Path(__file__).parents[1] / "main.py").read_text(encoding="utf-8")

    assert 'request.method == "GET" and path.startswith("/api/v1/admin/knowledge/sync-mysql/")' in source


def test_gateway_rate_limit_is_partitioned_by_identity_and_route_group() -> None:
    source = (Path(__file__).parents[1] / "main.py").read_text(encoding="utf-8")

    assert "request.state.user_id" in source
    assert 'return "chat"' in source
    assert 'return "upload"' in source
    assert 'return "retrieve"' in source
    assert 'return "sync_stop"' in source
    assert 'f"{identity}:{client_ip}:{group}"' in source


def test_frontend_streams_forward_auth_and_expose_retry_after() -> None:
    api_source = (Path(__file__).parents[1].parent / "frontend" / "src" / "api.js").read_text(encoding="utf-8")
    request_source = (Path(__file__).parents[1].parent / "frontend" / "src" / "api" / "request.js").read_text(encoding="utf-8")

    assert "kechuang-agent-token" in api_source
    assert "Authorization: `Bearer ${token}`" in api_source
    assert "error.retryAfter" in api_source
    assert "response.headers.get('Retry-After')" in request_source
