import asyncio
import json
from types import SimpleNamespace

import pytest

import assistant.application.chat_stream as application
from assistant.api.v1.schemas import ChatStreamRequest
from assistant.application.chat_stream import deerflow_stream, is_explicit_contact_request
from core.contracts.tools import ToolContext


class SessionStore:
    def __init__(self):
        self.state = {}
        self.messages = []

    def get(self, _thread_id):
        return {"user_id": "user-1", "state": self.state}

    def upsert(self, _thread_id, **fields):
        self.state.update(fields.get("state") or {})

    def append_message(self, _thread_id, **fields):
        self.messages.append(fields)


def _sse_payloads(events):
    return [
        json.loads(line.removeprefix("data: "))
        for event in events
        for line in event.splitlines()
        if line.startswith("data: ")
    ]


@pytest.mark.parametrize(
    "message",
    ["联系客服", "麻烦帮我联系人工客服", "请转人工", "我要申请人工对接"],
)
def test_explicit_contact_requests_are_recognized(message):
    assert is_explicit_contact_request(message)


@pytest.mark.parametrize("message", ["我不需要联系客服", "先不要转人工", "客服入口是什么？"])
def test_contact_mentions_without_a_request_do_not_trigger_action(message):
    assert not is_explicit_contact_request(message)


def test_external_contact_request_returns_action_and_persists_current_chat(monkeypatch):
    store = SessionStore()
    runtime = SimpleNamespace(
        deerflow_runner=object(),
        require_conversation=lambda: SimpleNamespace(session_store=store),
        require_draft_store=lambda: SimpleNamespace(),
    )

    async def unexpected_model_call(*_args, **_kwargs):
        raise AssertionError("明确联系客服时不应让模型决定客服动作或用户身份")

    monkeypatch.setattr(application, "iter_public_deerflow_events", unexpected_model_call)
    monkeypatch.setattr(
        application,
        "PostgresAuditSink",
        lambda: SimpleNamespace(record_chat_summary=lambda **_kwargs: None),
    )

    async def consume():
        events = [
            event
            async for event in deerflow_stream(
                runtime,
                ChatStreamRequest(message="麻烦帮我联系客服", thread_id="thread-1"),
                context=ToolContext(
                    user_id="user-1",
                    user_role="external",
                    thread_id="thread-1",
                    trace_id="trace-1",
                ),
                request_id="request-1",
                access_token="",
            )
        ]
        return _sse_payloads(events)

    events = asyncio.run(consume())
    result = next(item["result"] for item in events if "result" in item)

    assert result["actions"] == [
        {
            "id": "contact_wecom",
            "type": "CONTACT_WECOM",
            "label": "联系客服",
            "enabled": True,
            "query": "麻烦帮我联系客服",
            "document_ids": [],
        }
    ]
    assert "内部账号" not in result["answer"]
    assert [message["role"] for message in store.messages] == ["user", "assistant"]
    assert store.messages[-1]["response"]["actions"] == result["actions"]


def test_internal_contact_request_does_not_receive_external_contact_action(monkeypatch):
    store = SessionStore()
    model_calls = []
    runtime = SimpleNamespace(
        deerflow_runner=object(),
        require_conversation=lambda: SimpleNamespace(session_store=store),
        require_draft_store=lambda: SimpleNamespace(),
    )

    async def model_events(*_args, **_kwargs):
        model_calls.append(True)
        yield "done", {
            "result": {
                "status": "SUCCESS",
                "answer": "您可以在小程序界面点击联系客服，通过微信原生客服会话直接联系人工客服。",
            }
        }

    monkeypatch.setattr(application, "iter_public_deerflow_events", model_events)
    monkeypatch.setattr(
        application,
        "PostgresAuditSink",
        lambda: SimpleNamespace(record_chat_summary=lambda **_kwargs: None),
    )

    async def consume():
        return [
            event
            async for event in deerflow_stream(
                runtime,
                ChatStreamRequest(message="联系客服", thread_id="thread-1"),
                context=ToolContext(
                    user_id="user-1",
                    user_role="internal",
                    thread_id="thread-1",
                    trace_id="trace-1",
                ),
                request_id="request-1",
                access_token="",
            )
        ]

    events = asyncio.run(consume())
    result = next(item["result"] for item in _sse_payloads(events) if "result" in item)

    assert not model_calls
    assert not any(action.get("type") == "CONTACT_WECOM" for action in result["actions"])
    assert result["answer"] == "当前内部账号暂不提供客服转接入口，您可以直接在会话中描述需要协助的事项。"
