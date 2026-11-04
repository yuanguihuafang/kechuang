import asyncio
import time
from types import SimpleNamespace

import httpx
import pytest

import assistant.api.v1.execution.supervisor as module
import assistant.application.chat_stream as application
from assistant.api.v1.schemas import (
    ChatStreamRequest,
)
from assistant.application.chat_stream import (
    _next_match_batch_result,
    apply_pending_material_confirmation,
    is_existing_draft_card_request,
)
from core.contracts.tools import ToolContext


class Store:
    def __init__(self):
        self.state = {"chat_flow": {"last_status": "SUCCESS"}}
        self.messages = []

    def get(self, thread_id):
        return {"user_id": "u", "state": self.state}

    def upsert(self, thread_id, **fields):
        self.state.update(fields.get("state") or {})

    def append_message(self, thread_id, **fields):
        self.messages.append(fields)


def test_draft_card_request_does_not_capture_multi_draft_creation_command():
    assert is_existing_draft_card_request("整理草稿")
    assert is_existing_draft_card_request("显示本会话全部草稿卡片")
    assert not is_existing_draft_card_request("整理成多份草稿")


def test_affirmative_message_confirms_saved_material_intent_in_same_thread():
    payload = ChatStreamRequest(message="可以", thread_id="s")
    pending = {
        "intent_hint": "supply_demand_match",
        "query": "工业机器人柔性上下料",
        "match_direction": "demand_to_supply",
        "draft_context": {"text": "工业机器人柔性上下料需求"},
    }

    confirmed = apply_pending_material_confirmation(payload, pending)

    assert confirmed is not None
    assert confirmed.intent_hint == "supply_demand_match"
    assert confirmed.intent_confirmation == "confirmed"
    assert confirmed.draft_context["match_direction"] == "demand_to_supply"
    assert "工业机器人柔性上下料" in confirmed.message


def test_non_confirmation_does_not_apply_saved_material_intent():
    payload = ChatStreamRequest(message="先介绍一下你自己", thread_id="s")
    pending = {"intent_hint": "supply_demand_match", "draft_context": {"text": "旧资料"}}

    assert apply_pending_material_confirmation(payload, pending) is None


def test_chat_stream_confirms_pending_upload_intent_and_releases_search_gate(monkeypatch):
    store = Store()
    store.state["chat_flow"].update(
        material_intent_confirmation_required=True,
        pending_material_intent={
            "intent_hint": "supply_demand_match",
            "query": "工业机器人上下料",
            "match_direction": "demand_to_supply",
            "draft_context": {"text": "工业机器人上下料需求"},
        },
    )
    runtime = SimpleNamespace(
        deerflow_runner=object(),
        require_conversation=lambda: SimpleNamespace(session_store=store),
    )
    captured = []

    async def events(_runner, payload, **_kwargs):
        captured.append(payload)
        yield "done", {"result": {"status": "SUCCESS", "answer": "匹配完成"}}

    monkeypatch.setattr(application, "iter_public_deerflow_events", events)
    monkeypatch.setattr(
        application,
        "PostgresAuditSink",
        lambda: SimpleNamespace(record_chat_summary=lambda **_kwargs: None),
    )
    router = module.build_supervisor_router(runtime, lambda user_id: (user_id, "external", None))
    endpoint = next(route.endpoint for route in router.routes if route.path == "/api/v1/chat/stream")

    async def consume():
        response = await endpoint(
            ChatStreamRequest(message="可以", thread_id="s"),
            SimpleNamespace(
                state=SimpleNamespace(user_id="u", trace_id="current"), headers={}
            ),
        )
        return [chunk async for chunk in response.body_iterator]

    asyncio.run(consume())

    assert captured[0]["intent_hint"] == "supply_demand_match"
    assert captured[0]["intent_confirmation"] == "confirmed"
    assert captured[0]["draft_context"]["match_direction"] == "demand_to_supply"
    assert store.state["chat_flow"]["material_intent_confirmation_required"] is False
    assert "pending_material_intent" not in store.state["chat_flow"]


def test_back_flow_confirms_return_and_preserves_pending_context(monkeypatch):
    store = Store()
    pending_context = [
        {"role": "user", "content": "需求发现"},
        {"role": "assistant", "content": "请描述具体需求"},
    ]
    store.state.update(
        pending_deerflow_context=pending_context,
        pending_deerflow_context_created_at=time.time(),
        pending_deerflow_context_failures=0,
        pending_deerflow_context_intent="demand_discovery",
    )
    store.state["chat_flow"].update(
        material_intent_confirmation_required=True,
        pending_material_intent={"intent_hint": "demand_discovery"},
    )
    runtime = SimpleNamespace(
        deerflow_runner=object(),
        require_conversation=lambda: SimpleNamespace(session_store=store),
    )
    called = []

    async def events(*_args, **_kwargs):
        called.append(True)
        yield "done", {"result": {"status": "SUCCESS", "answer": "不应调用模型"}}

    monkeypatch.setattr(application, "iter_public_deerflow_events", events)
    monkeypatch.setattr(
        application,
        "PostgresAuditSink",
        lambda: SimpleNamespace(record_chat_summary=lambda **_kwargs: None),
    )
    router = module.build_supervisor_router(runtime, lambda user_id: (user_id, "external", None))
    endpoint = next(route.endpoint for route in router.routes if route.path == "/api/v1/chat/stream")

    async def consume():
        response = await endpoint(
            ChatStreamRequest(message="返回上一步", thread_id="s", flow_action="back"),
            SimpleNamespace(state=SimpleNamespace(user_id="u", trace_id="back"), headers={}),
        )
        return "".join([chunk async for chunk in response.body_iterator])

    body = asyncio.run(consume())

    assert not called
    assert "已返回上一步" in body
    assert "接下来您想做什么" in body
    assert store.state["pending_deerflow_context"] == pending_context
    assert store.state["chat_flow"]["pending_material_intent"] == {
        "intent_hint": "demand_discovery"
    }
    assert store.state["chat_flow"]["material_intent_confirmation_required"] is True


def test_failed_confirmed_upload_restores_confirmation_for_retry(monkeypatch):
    store = Store()
    pending = {
        "intent_hint": "supply_demand_match",
        "query": "工业机器人上下料",
        "match_direction": "demand_to_supply",
        "draft_context": {"text": "工业机器人上下料需求"},
    }
    store.state["chat_flow"].update(
        material_intent_confirmation_required=True,
        pending_material_intent=pending,
    )
    runtime = SimpleNamespace(
        deerflow_runner=object(),
        require_conversation=lambda: SimpleNamespace(session_store=store),
    )

    async def failed_events(*_args, **_kwargs):
        yield "error", {"message": "上游服务失败", "stage": "upstream"}

    monkeypatch.setattr(application, "iter_public_deerflow_events", failed_events)
    router = module.build_supervisor_router(runtime, lambda user_id: (user_id, "external", None))
    endpoint = next(route.endpoint for route in router.routes if route.path == "/api/v1/chat/stream")

    async def consume():
        response = await endpoint(
            ChatStreamRequest(message="可以", thread_id="s"),
            SimpleNamespace(state=SimpleNamespace(user_id="u", trace_id="retry"), headers={}),
        )
        return [chunk async for chunk in response.body_iterator]

    asyncio.run(consume())

    assert store.state["chat_flow"]["material_intent_confirmation_required"] is True
    assert store.state["chat_flow"]["pending_material_intent"] == pending


def test_next_batch_without_saved_result_returns_actionable_state_instead_of_raising():
    result = asyncio.run(
        _next_match_batch_result(
            SimpleNamespace(),
            ChatStreamRequest(message="换一批", thread_id="s"),
            context=ToolContext(
                user_id="u", user_role="internal", thread_id="s", trace_id="trace"
            ),
            chat_flow={},
        )
    )

    assert result["status"] == "NO_EVIDENCE"
    assert "联网搜索" in result["answer"]
    assert "明确告诉我" in result["answer"]


def test_empty_next_batch_is_reported_as_no_evidence_not_processing_failure():
    calls = []

    class Adapter:
        async def search_knowledge(self, arguments, *, context):
            calls.append(arguments)
            return {
                "status": "NO_EVIDENCE",
                "answer": "当前知识库没有更多匹配结果。",
                "evidences": [],
                "match_results": [],
            }

    result = asyncio.run(
        _next_match_batch_result(
            SimpleNamespace(tool_adapter=Adapter()),
            ChatStreamRequest(message="换一批", thread_id="s"),
            context=ToolContext(
                user_id="u", user_role="internal", thread_id="s", trace_id="trace"
            ),
            chat_flow={
                "match_batch": {
                    "original_query": "上下游合作企业",
                    "current_query": "上下游合作企业",
                    "business_intent": "supply_demand_match",
                    "direction": "demand_to_supply",
                    "knowledge_type": "enterprise",
                    "knowledge_types": ["enterprise"],
                    "shown_result_ids": ["enterprise:mysql:17"],
                }
            },
        )
    )

    assert result["status"] == "NO_EVIDENCE"
    assert "联网搜索" in result["answer"]
    assert calls[0]["knowledge_types"] == ["enterprise"]
    assert calls[0]["exclude_result_ids"] == ["enterprise:mysql:17"]


def test_next_batch_judges_candidates_and_preserves_original_web_fallback_policy(monkeypatch):
    calls = []

    class Adapter:
        async def search_knowledge(self, arguments, *, context):
            return {
                "status": "SUCCESS",
                "evidences": [{
                    "record_id": "candidate-1",
                    "knowledge_type": "enterprise",
                    "text": "河北利仕化学科技有限公司，精细化工企业",
                }],
            }

    async def judge(result, **kwargs):
        calls.append(kwargs)
        return result

    monkeypatch.setattr(application, "judge_and_fallback", judge)
    result = asyncio.run(
        _next_match_batch_result(
            SimpleNamespace(tool_adapter=Adapter()),
            ChatStreamRequest(message="换一批", thread_id="s"),
            context=ToolContext(
                user_id="u", user_role="internal", thread_id="s", trace_id="trace"
            ),
            chat_flow={
                "match_batch": {
                    "original_query": "河北利仕化学",
                    "current_query": "河北利仕化学",
                    "user_goal": "帮我查河北利仕化学，我一会去拜访并挖掘场景需求",
                    "web_fallback_allowed": True,
                    "business_intent": "knowledge_search",
                    "knowledge_type": "enterprise",
                    "knowledge_types": ["enterprise"],
                    "shown_result_ids": ["enterprise:mysql:17"],
                }
            },
        )
    )

    assert calls[0]["user_goal"] == "帮我查河北利仕化学，我一会去拜访并挖掘场景需求"
    assert calls[0]["allow_web_fallback"] is True
    assert result["status"] == "SUCCESS"
    assert result["match_results"]


@pytest.mark.parametrize("ending", ["done", "error", "timeout", "eof", "cancel"])
def test_terminal_state_saved_once(monkeypatch, ending):
    store = Store()
    runtime = SimpleNamespace(deerflow_runner=object(),
                              require_conversation=lambda: SimpleNamespace(session_store=store))

    async def events(*args, **kwargs):
        yield "token", {"text": "部分文本"}
        if ending == "done":
            yield "done", {"result": {"status": "SUCCESS", "answer": "完成"}}
        elif ending == "error":
            yield "error", {"message": "上游失败", "stage": "tool"}
        elif ending == "timeout":
            raise httpx.ReadTimeout("timeout")
        elif ending == "cancel":
            raise asyncio.CancelledError()

    monkeypatch.setattr(application, "iter_public_deerflow_events", events)
    monkeypatch.setattr(application, "PostgresAuditSink", lambda: SimpleNamespace(record_chat_summary=lambda **kw: None))
    router = module.build_supervisor_router(runtime, lambda user_id: (user_id, "external", None))
    endpoint = next(route.endpoint for route in router.routes if route.path == "/api/v1/chat/stream")

    async def consume():
        response = await endpoint(ChatStreamRequest(message="继续", thread_id="s"),
                                  SimpleNamespace(state=SimpleNamespace(user_id="u", trace_id="current"), headers={}))
        return [chunk async for chunk in response.body_iterator]

    if ending == "cancel":
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(consume())
    else:
        asyncio.run(consume())
    terminals = [row["response"] for row in store.messages if row["role"] == "assistant"]
    assert len(terminals) == 1
    assert terminals[0]["trace_id"] == "current"
    assert store.state["last_chat_terminal"]["trace_id"] == "current"
    if ending != "done":
        assert terminals[0]["status"] != "SUCCESS"
        assert store.state["chat_flow"]["last_status"] != "SUCCESS"
        assert terminals[0]["answer"] == "部分文本"


def test_latest_saved_fields_replace_stale_client_context(monkeypatch):
    from services.persistence.postgres import InMemoryDraftStore
    store = Store()
    drafts = InMemoryDraftStore()
    row = drafts.create(user_id="u", user_role="external", thread_id="s",
                        draft={"draft_type": "demand", "fields": {"title": "权威最新"}})
    store.state["chat_flow"]["active_draft_id"] = row["draft_id"]
    runtime = SimpleNamespace(deerflow_runner=object(), require_draft_store=lambda: drafts,
                              require_conversation=lambda: SimpleNamespace(session_store=store))
    captured = []

    async def events(runner, payload, **kwargs):
        captured.append(payload)
        yield "done", {"result": {"status": "SUCCESS", "answer": "完成"}}

    monkeypatch.setattr(application, "iter_public_deerflow_events", events)
    monkeypatch.setattr(application, "PostgresAuditSink", lambda: SimpleNamespace(record_chat_summary=lambda **kw: None))
    router = module.build_supervisor_router(runtime, lambda user_id: (user_id, "external", None))
    endpoint = next(route.endpoint for route in router.routes if route.path == "/api/v1/chat/stream")

    async def consume():
        response = await endpoint(ChatStreamRequest(message="继续", thread_id="s", draft_context={"fields": {"title": "客户端旧值"}}),
                                  SimpleNamespace(state=SimpleNamespace(user_id="u", trace_id="current"), headers={}))
        return [chunk async for chunk in response.body_iterator]

    asyncio.run(consume())
    assert captured[0]["draft_context"]["fields"]["title"] == "权威最新"
    assert "客户端旧值" not in str(captured)


def test_multi_draft_confirmation_reuses_full_message_then_clears_context(monkeypatch):
    from services.persistence.postgres import InMemoryDraftStore

    store = Store()
    drafts = InMemoryDraftStore()
    runtime = SimpleNamespace(
        deerflow_runner=object(),
        require_draft_store=lambda: drafts,
        require_conversation=lambda: SimpleNamespace(session_store=store),
    )
    captured = []

    async def events(_runner, payload, **_kwargs):
        captured.append(payload)
        status = (
            "DRAFT_MULTI_CONFIRM_REQUIRED"
            if len(captured) == 1
            else "DRAFT_GENERATED"
        )
        result = {"status": status, "answer": "请选择拆分或合并"}
        if len(captured) == 1:
            result["draft"] = {"draft_id": "must-not-display", "fields": {}}
            result["ui_blocks"] = [
                {"type": "draft_card", "data": {"draft": result["draft"]}}
            ]
        yield "done", {"result": result}

    monkeypatch.setattr(application, "iter_public_deerflow_events", events)
    monkeypatch.setattr(
        application,
        "PostgresAuditSink",
        lambda: SimpleNamespace(record_chat_summary=lambda **_kwargs: None),
    )
    endpoint = next(
        route.endpoint
        for route in module.build_supervisor_router(
            runtime, lambda user_id: (user_id, "external", None)
        ).routes
        if route.path == "/api/v1/chat/stream"
    )

    async def consume(message):
        response = await endpoint(
            ChatStreamRequest(message=message, thread_id="s"),
            SimpleNamespace(state=SimpleNamespace(user_id="u", trace_id="trace"), headers={}),
        )
        return [chunk async for chunk in response.body_iterator]

    full_text = "完整上传原文：" + "独立事项全文。" * 100
    store.state["chat_flow"]["pending_draft_material"] = {
        "text": full_text,
        "file_name": "资料.docx",
        "expires_at": time.time() + 1800,
    }
    first_turn = asyncio.run(consume("整理草稿"))
    pending = store.state["chat_flow"]["pending_draft_material"]
    assert pending["text"] == full_text
    assert "整理草稿" in captured[0]["message"]
    assert full_text in captured[0]["message"]
    assert "must-not-display" not in "".join(first_turn)
    assert "draft_card" not in "".join(first_turn)

    asyncio.run(consume("拆分生成多个草稿"))
    assert "<untrusted_material>" in captured[1]["message"]
    assert full_text in captured[1]["message"]
    assert "pending_draft_material" not in store.state["chat_flow"]


def test_multi_draft_confirmation_fails_closed_when_full_text_cannot_be_saved(monkeypatch):
    class UnavailableStore(Store):
        def upsert(self, thread_id, **fields):
            flow = (fields.get("state") or {}).get("chat_flow") or {}
            if "pending_draft_material" in flow:
                raise RuntimeError("session storage unavailable")
            super().upsert(thread_id, **fields)

    store = UnavailableStore()
    runtime = SimpleNamespace(
        deerflow_runner=object(),
        require_conversation=lambda: SimpleNamespace(session_store=store),
    )

    async def events(*_args, **_kwargs):
        yield "done", {
            "result": {
                "status": "DRAFT_MULTI_CONFIRM_REQUIRED",
                "answer": "请选择拆分或合并",
            }
        }

    monkeypatch.setattr(application, "iter_public_deerflow_events", events)
    monkeypatch.setattr(
        application,
        "PostgresAuditSink",
        lambda: SimpleNamespace(record_chat_summary=lambda **_kwargs: None),
    )
    endpoint = next(
        route.endpoint
        for route in module.build_supervisor_router(
            runtime, lambda user_id: (user_id, "external", None)
        ).routes
        if route.path == "/api/v1/chat/stream"
    )

    async def consume():
        response = await endpoint(
            ChatStreamRequest(message="长文全文", thread_id="s"),
            SimpleNamespace(state=SimpleNamespace(user_id="u", trace_id="trace"), headers={}),
        )
        return "".join([chunk async for chunk in response.body_iterator])

    body = asyncio.run(consume())

    assert "完整资料暂存失败" in body
    assert '"flow_state": "ERROR"' in body


def test_showing_one_draft_returns_every_draft_in_the_same_session(monkeypatch):
    from services.persistence.postgres import InMemoryDraftStore

    store = Store()
    store.state["chat_flow"]["pending_draft_material"] = {
        "text": "上轮上传资料的完整正文",
        "file_name": "资料.docx",
        "expires_at": time.time() + 60,
    }
    drafts = InMemoryDraftStore()
    created = drafts.create_many(
        user_id="u",
        user_role="external",
        thread_id="s",
        drafts=[
            {"draft_type": "solution", "title": f"方案 {index}", "fields": {}}
            for index in range(8)
        ],
    )
    runtime = SimpleNamespace(
        deerflow_runner=object(),
        require_draft_store=lambda: drafts,
        require_conversation=lambda: SimpleNamespace(session_store=store),
    )

    async def events(*_args, **_kwargs):
        raise AssertionError("现有草稿卡片应直接从会话草稿目录返回")

    monkeypatch.setattr(application, "iter_public_deerflow_events", events)
    monkeypatch.setattr(
        application,
        "PostgresAuditSink",
        lambda: SimpleNamespace(record_chat_summary=lambda **_kwargs: None),
    )
    router = module.build_supervisor_router(runtime, lambda user_id: (user_id, "external", None))
    endpoint = next(route.endpoint for route in router.routes if route.path == "/api/v1/chat/stream")

    async def consume():
        response = await endpoint(
            ChatStreamRequest(message="整理草稿", thread_id="s"),
            SimpleNamespace(state=SimpleNamespace(user_id="u", trace_id="cards"), headers={}),
        )
        return [chunk async for chunk in response.body_iterator]

    chunks = asyncio.run(consume())
    done = next(chunk for chunk in chunks if chunk.startswith("event: done"))
    payload = __import__("json").loads(done.split("data: ", 1)[1])
    result = payload["result"]

    assert len(result["drafts"]) == 8
    assert result["drafts"][0]["status"] == created[0]["status"]
    draft_card_blocks = [block for block in result["ui_blocks"] if block["type"] == "draft_card"]
    assert len(draft_card_blocks) == 1
    assert draft_card_blocks[0]["data"]["draft"]["draft_id"] == result["drafts"][0]["draft_id"]


def test_created_drafts_are_attached_when_generation_response_omits_draft_payload(monkeypatch):
    from services.persistence.postgres import InMemoryDraftStore

    store = Store()
    drafts = InMemoryDraftStore()
    created = drafts.create_many(
        user_id="u",
        user_role="external",
        thread_id="s",
        drafts=[
            {"draft_type": "solution", "title": f"方案 {index}", "fields": {}}
            for index in range(3)
        ],
    )
    runtime = SimpleNamespace(
        deerflow_runner=object(),
        require_draft_store=lambda: drafts,
        require_conversation=lambda: SimpleNamespace(session_store=store),
    )

    async def events(*_args, **_kwargs):
        yield "done", {
            "result": {
                "status": "DRAFT_GENERATED",
                "answer": "草稿已生成，请查看下方卡片。",
            }
        }

    monkeypatch.setattr(application, "iter_public_deerflow_events", events)
    monkeypatch.setattr(
        application,
        "PostgresAuditSink",
        lambda: SimpleNamespace(record_chat_summary=lambda **_kwargs: None),
    )
    endpoint = next(
        route.endpoint
        for route in module.build_supervisor_router(
            runtime, lambda user_id: (user_id, "external", None)
        ).routes
        if route.path == "/api/v1/chat/stream"
    )

    async def consume():
        response = await endpoint(
            ChatStreamRequest(message="整理成多份草稿", thread_id="s"),
            SimpleNamespace(state=SimpleNamespace(user_id="u", trace_id="generated"), headers={}),
        )
        return [chunk async for chunk in response.body_iterator]

    chunks = asyncio.run(consume())
    done = next(chunk for chunk in chunks if chunk.startswith("event: done"))
    result = __import__("json").loads(done.split("data: ", 1)[1])["result"]

    assert [draft["draft_id"] for draft in result["drafts"]] == [
        draft["draft_id"] for draft in created
    ]
    draft_card_blocks = [block for block in result["ui_blocks"] if block["type"] == "draft_card"]
    assert len(draft_card_blocks) == 1
    assert draft_card_blocks[0]["data"]["draft"]["draft_id"] == result["drafts"][0]["draft_id"]


def test_pending_quick_entry_exchange_is_sent_once_and_cleared_after_agent_done(monkeypatch):
    store = Store()
    pending_context = [
        {"role": "user", "content": "企业画像"},
        {"role": "assistant", "content": "请提供企业名称"},
    ]
    store.state["pending_deerflow_context"] = pending_context
    store.state["pending_deerflow_context_created_at"] = time.time()
    store.state["pending_deerflow_context_failures"] = 0
    store.state["pending_deerflow_context_intent"] = "enterprise_profile"
    runtime = SimpleNamespace(
        deerflow_runner=object(),
        require_conversation=lambda: SimpleNamespace(session_store=store),
    )
    captured = []

    async def events(_runner, _payload, **kwargs):
        captured.append(kwargs.get("prior_messages"))
        yield "done", {"result": {"status": "SUCCESS", "answer": "企业查询结果"}}

    monkeypatch.setattr(application, "iter_public_deerflow_events", events)
    monkeypatch.setattr(
        application,
        "PostgresAuditSink",
        lambda: SimpleNamespace(record_chat_summary=lambda **_kwargs: None),
    )
    router = module.build_supervisor_router(runtime, lambda user_id: (user_id, "internal", None))
    endpoint = next(route.endpoint for route in router.routes if route.path == "/api/v1/chat/stream")

    async def consume():
        response = await endpoint(
            ChatStreamRequest(message="青岛某某科技有限公司", thread_id="s"),
            SimpleNamespace(state=SimpleNamespace(user_id="u", trace_id="trace"), headers={}),
        )
        return [chunk async for chunk in response.body_iterator]

    asyncio.run(consume())

    assert captured == [pending_context]
    assert store.state["pending_deerflow_context"] is None


def test_failed_quick_entry_response_keeps_context_for_one_retry(monkeypatch):
    store = Store()
    pending_context = [
        {"role": "user", "content": "企业画像"},
        {"role": "assistant", "content": "请提供企业名称"},
    ]
    store.state.update(
        pending_deerflow_context=pending_context,
        pending_deerflow_context_created_at=time.time(),
        pending_deerflow_context_failures=0,
        pending_deerflow_context_intent="enterprise_profile",
    )
    runtime = SimpleNamespace(
        deerflow_runner=object(),
        require_conversation=lambda: SimpleNamespace(session_store=store),
    )
    captured = []

    async def failed_events(_runner, _payload, **kwargs):
        captured.append(kwargs.get("prior_messages"))
        yield "done", {"result": {"status": "FAILED", "answer": "暂时失败"}}

    monkeypatch.setattr(application, "iter_public_deerflow_events", failed_events)
    monkeypatch.setattr(
        application,
        "PostgresAuditSink",
        lambda: SimpleNamespace(record_chat_summary=lambda **_kwargs: None),
    )
    router = module.build_supervisor_router(runtime, lambda user_id: (user_id, "internal", None))
    endpoint = next(route.endpoint for route in router.routes if route.path == "/api/v1/chat/stream")

    async def send():
        response = await endpoint(
            ChatStreamRequest(message="青岛某某科技有限公司", thread_id="s"),
            SimpleNamespace(state=SimpleNamespace(user_id="u", trace_id="trace"), headers={}),
        )
        return [chunk async for chunk in response.body_iterator]

    asyncio.run(send())
    assert store.state["pending_deerflow_context"] == pending_context
    assert store.state["pending_deerflow_context_failures"] == 1

    asyncio.run(send())

    assert captured == [pending_context, pending_context]
    assert store.state["pending_deerflow_context"] is None


def test_fixed_quick_entry_exchange_flows_from_first_reply_to_next_agent_call(monkeypatch):
    store = Store()
    runtime = SimpleNamespace(
        deerflow_runner=object(),
        require_conversation=lambda: SimpleNamespace(session_store=store),
    )
    first_exchange = [
        {"role": "user", "content": "企业画像"},
        {"role": "assistant", "content": "请提供企业名称"},
    ]
    agent_contexts = []
    call_count = 0

    async def events(_runner, _payload, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            kwargs["on_quick_entry_context"](first_exchange)
            yield "done", {"result": {"status": "SUCCESS", "answer": "请提供企业名称"}}
            return
        agent_contexts.append(kwargs["prior_messages"])
        yield "done", {"result": {"status": "SUCCESS", "answer": "已查询企业"}}

    monkeypatch.setattr(application, "iter_public_deerflow_events", events)
    monkeypatch.setattr(
        application,
        "PostgresAuditSink",
        lambda: SimpleNamespace(record_chat_summary=lambda **_kwargs: None),
    )
    router = module.build_supervisor_router(runtime, lambda user_id: (user_id, "internal", None))
    endpoint = next(route.endpoint for route in router.routes if route.path == "/api/v1/chat/stream")

    async def send(payload):
        response = await endpoint(
            payload,
            SimpleNamespace(state=SimpleNamespace(user_id="u", trace_id="trace"), headers={}),
        )
        return [chunk async for chunk in response.body_iterator]

    asyncio.run(
        send(
            ChatStreamRequest(
                message="企业画像",
                thread_id="s",
                intent_hint="enterprise_profile",
                intent_confirmation="confirmed",
            )
        )
    )
    assert store.state["pending_deerflow_context"] == first_exchange

    asyncio.run(send(ChatStreamRequest(message="青岛某某科技有限公司", thread_id="s")))

    assert agent_contexts == [first_exchange]
    assert store.state["pending_deerflow_context"] is None
