import asyncio
import json

from fastapi import HTTPException, Response
from starlette.requests import Request

import main


def _request(path: str, query_string: bytes = b"") -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "scheme": "http",
            "path": path,
            "query_string": query_string,
            "headers": [(b"authorization", b"Bearer test-token")],
            "server": ("testserver", 80),
            "client": ("testclient", 1234),
        }
    )


def test_admin_route_does_not_receive_token_user_id(monkeypatch):
    monkeypatch.setattr(main, "verify_access_token", lambda _token: "ordinary-user")
    captured = {}

    async def call_next(request):
        captured["query_string"] = request.scope["query_string"]
        return Response("ok")

    request = _request("/api/v1/admin/overview", b"user_id=old-value")
    asyncio.run(main.AgentAuthMiddleware(None).dispatch(request, call_next))

    assert captured["query_string"] == b"user_id=old-value"


def test_non_admin_route_still_receives_token_user_id(monkeypatch):
    monkeypatch.setattr(main, "verify_access_token", lambda _token: "ordinary-user")
    captured = {}

    async def call_next(request):
        captured["query_string"] = request.scope["query_string"]
        return Response("ok")

    request = _request("/api/v1/me/profile", b"user_id=old-value")
    asyncio.run(main.AgentAuthMiddleware(None).dispatch(request, call_next))

    assert captured["query_string"] == b"user_id=ordinary-user"


def test_expired_token_keeps_401_response(monkeypatch):
    def reject_token(_token):
        raise HTTPException(status_code=401, detail="智能体登录已失效，请重新登录")

    monkeypatch.setattr(main, "verify_access_token", reject_token)

    async def call_next(_request):
        raise AssertionError("expired token must stop in auth middleware")

    response = asyncio.run(
        main.AgentAuthMiddleware(None).dispatch(_request("/api/v1/auth/me"), call_next)
    )

    assert response.status_code == 401
    assert json.loads(response.body)["error"]["message"] == "智能体登录已失效，请重新登录"


def test_mcp_json_rpc_is_not_rewritten_with_user_id(monkeypatch):
    monkeypatch.setattr(main, "verify_access_token", lambda _token: "ordinary-user")
    payload = b'{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'

    async def receive():
        return {"type": "http.request", "body": payload, "more_body": False}

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": "http",
            "path": "/mcp",
            "query_string": b"tenant=kc",
            "headers": [
                (b"authorization", b"Bearer test-token"),
                (b"content-type", b"application/json"),
                (b"content-length", str(len(payload)).encode()),
            ],
            "server": ("testserver", 80),
            "client": ("testclient", 1234),
        },
        receive=receive,
    )
    captured = {}

    async def call_next(next_request):
        captured["query_string"] = next_request.scope["query_string"]
        captured["body"] = await next_request.body()
        captured["user_id"] = next_request.state.user_id
        return Response("ok")

    asyncio.run(main.AgentAuthMiddleware(None).dispatch(request, call_next))

    assert captured == {
        "query_string": b"tenant=kc",
        "body": payload,
        "user_id": "ordinary-user",
    }


def test_trace_middleware_wraps_auth_middleware():
    middleware = [item.cls for item in main.app.user_middleware]
    assert middleware.index(main.GatewayTraceMiddleware) < middleware.index(
        main.AgentAuthMiddleware
    )
