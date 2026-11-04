import asyncio
import json
from pathlib import Path

import httpx

from assistant.integrations.deerflow_gateway import DeerFlowGatewayRunner


def test_gateway_runner_decodes_native_sse_and_maps_request():
    seen: list[tuple[str, dict[str, str], bytes]] = []
    run_ids: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), dict(request.headers), request.read()))
        if str(request.url).endswith("/api/threads"):
            return httpx.Response(200, json={"thread_id": "thread-1"})
        return httpx.Response(
            200,
            headers={
                "content-type": "text/event-stream",
                "Content-Location": "/api/threads/thread-1/runs/run-123",
            },
            content=(
                "event: messages\n"
                'data: {"type":"ai","content":"回答"}\n\n'
                "event: messages\n"
                'data: [{"type":"tool","name":"kc_draft_create","artifact":{"structured_content":{"status":"SUCCESS"}}},{"langgraph_node":"tools"}]\n\n'
                "event: end\n"
                "data: null\n\n"
            ).encode(),
        )

    runner = DeerFlowGatewayRunner(
        "http://deerflow.test/",
        api_key="secret",
        internal_auth_token="internal-secret",
        agent_owner_user_id="fixed-opie-user",
        internal_token_header="X-DeerFlow-Internal-Token",
        owner_user_id_header="X-DeerFlow-Owner-User-Id",
        transport=httpx.MockTransport(handler),
    )

    async def collect():
        return [
            event
            async for event in runner.stream(
                {
                    "thread_id": "thread-1",
                    "message": "问题",
                    "prior_messages": [
                        {"role": "user", "content": "企业画像"},
                        {"role": "assistant", "content": "请提供企业名称"},
                    ],
                    "metadata": {"intent_hint": "knowledge_search", "kc_trace_id": "server-trace"},
                    "context": {"user_id": "u-1", "user_role": "internal"},
                    "model_name": "kc-main",
                    "secrets": {
                        "kc_access_token": "Bearer request-token",
                        "kc_thread_id": "thread-1",
                        "kc_trace_id": "server-trace",
                    },
                    "on_run_id": run_ids.append,
                }
            )
        ]

    assert asyncio.run(collect()) == [
        {"type": "messages", "data": {"type": "ai", "content": "回答"}},
        {
            "type": "messages",
            "data": [
                {
                    "type": "tool",
                    "name": "kc_draft_create",
                    "artifact": {"structured_content": {"status": "SUCCESS"}},
                },
                {"langgraph_node": "tools"},
            ],
        },
        {"type": "end", "data": {}},
    ]
    assert run_ids == ["run-123"]
    assert [request[0] for request in seen] == [
        "http://deerflow.test/api/threads",
        "http://deerflow.test/api/threads/thread-1/runs/stream",
    ]
    assert seen[0][1]["x-deerflow-internal-token"] == "internal-secret"
    assert seen[0][1]["x-deerflow-owner-user-id"] == "fixed-opie-user"
    assert json.loads(seen[0][2]) == {
        "thread_id": "thread-1",
        "assistant_id": "kechuang",
        "metadata": {"intent_hint": "knowledge_search", "kc_trace_id": "server-trace"},
    }
    assert json.loads(seen[1][2])["assistant_id"] == "kechuang"
    assert json.loads(seen[1][2])["input"]["messages"] == [
        {"role": "user", "content": "企业画像"},
        {"role": "assistant", "content": "请提供企业名称"},
        {"role": "user", "content": "问题"},
    ]
    assert json.loads(seen[1][2])["context"] == {
        "user_id": "u-1",
        "user_role": "internal",
        "model_name": "kc-main",
    }
    assert json.loads(seen[1][2])["config"] == {
        "context": {
            "secrets": {
                "kc_access_token": "Bearer request-token",
                "kc_thread_id": "thread-1",
                "kc_trace_id": "server-trace",
            }
        }
    }


def test_gateway_runner_resumes_same_run_after_stream_gap():
    seen: list[tuple[str, str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.headers.get("last-event-id")))
        if request.url.path == "/api/threads":
            return httpx.Response(200, json={"thread_id": "thread-1"})
        if request.method == "POST":
            return httpx.Response(
                200,
                headers={
                    "content-type": "text/event-stream",
                    "Content-Location": "/api/threads/thread-1/runs/run-123",
                },
                content=(
                    b"event: gap\n"
                    b'data: {"code":"stream_replay_gap","latest_available_event_id":"311",'
                    b'"recovery":"reload_durable_state"}\n\n'
                ),
            )
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=(
                "event: messages\n"
                'data: {"type":"ai","content":"已恢复的回答"}\n\n'
                "event: end\n"
                "data: null\n\n"
            ).encode(),
        )

    runner = DeerFlowGatewayRunner(
        "http://deerflow.test",
        transport=httpx.MockTransport(handler),
    )

    async def collect():
        return [
            event async for event in runner.stream({"thread_id": "thread-1", "message": "问题"})
        ]

    assert asyncio.run(collect()) == [
        {"type": "messages", "data": {"type": "ai", "content": "已恢复的回答"}},
        {"type": "end", "data": {}},
    ]
    assert seen == [
        ("POST", "/api/threads", None),
        ("POST", "/api/threads/thread-1/runs/stream", None),
        ("GET", "/api/threads/thread-1/runs/run-123/join", "311"),
    ]


def test_gateway_runner_sends_csrf_pair_with_internal_auth():
    seen_headers: list[dict[str, str]] = []
    seen_run_messages = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.append(dict(request.headers))
        if request.url.path == "/api/threads":
            return httpx.Response(200, json={"thread_id": "thread-1"})
        seen_run_messages.append(json.loads(request.read())["input"]["messages"])
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=b"event: end\ndata: null\n\n",
        )

    runner = DeerFlowGatewayRunner(
        "http://deerflow.test",
        internal_auth_token="internal-secret",
        agent_owner_user_id="fixed-opie-user",
        internal_token_header="X-DeerFlow-Internal-Token",
        owner_user_id_header="X-DeerFlow-Owner-User-Id",
        transport=httpx.MockTransport(handler),
    )

    async def collect():
        return [
            event
            async for event in runner.stream(
                {"thread_id": "thread-1", "message": "hello", "context": {}}
            )
        ]

    assert asyncio.run(collect()) == [{"type": "end", "data": {}}]
    assert len(seen_headers) == 2
    assert seen_run_messages == [[{"role": "user", "content": "hello"}]]
    csrf_tokens = [headers.get("x-csrf-token") for headers in seen_headers]
    assert csrf_tokens[0]
    assert csrf_tokens[0] == csrf_tokens[1]
    assert all(
        headers.get("cookie") == f"csrf_token={csrf_token}"
        and headers.get("x-deerflow-internal-token") == "internal-secret"
        for headers, csrf_token in zip(seen_headers, csrf_tokens)
    )


def test_gateway_runner_does_not_inherit_system_proxy(monkeypatch):
    client_options: dict[str, object] = {}
    original_client = httpx.AsyncClient

    def capture_client(**kwargs):
        client_options.update(kwargs)
        return original_client(**kwargs)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/threads":
            return httpx.Response(200, json={"thread_id": "t"})
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=b"event: end\ndata: null\n\n",
        )

    monkeypatch.setattr(httpx, "AsyncClient", capture_client)
    runner = DeerFlowGatewayRunner(
        "http://127.0.0.1:8001",
        transport=httpx.MockTransport(handler),
    )

    async def collect():
        return [
            event
            async for event in runner.stream({"thread_id": "t", "message": "hello", "context": {}})
        ]

    assert asyncio.run(collect()) == [{"type": "end", "data": {}}]
    assert client_options["trust_env"] is False


def test_kechuang_example_maps_trace_header_from_runtime_secret():
    config_path = (
        Path(__file__).resolve().parents[2] / "OPIEos" / "extensions_config.example.json"
    )
    config = json.loads(config_path.read_text(encoding="utf-8"))
    headers = config["mcpServers"]["kechuang"]["headers_from_context"]

    assert headers == {
        "enabled": True,
        "headers": {
            "X-KC-Thread-ID": "kc_thread_id",
            "X-Trace-ID": "kc_trace_id",
        },
        "on_missing": "deny",
    }
