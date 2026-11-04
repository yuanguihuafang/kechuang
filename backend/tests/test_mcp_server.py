from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from core.contracts.tools import ToolContext
from assistant.integrations.mcp_server import (
    MatchToolInput,
    RetrievalToolInput,
    _failure,
    _has_match_batch_items,
    _no_more_match_batch,
    _default_retrieval_knowledge_types,
    _mcp_allowed_hosts,
    apply_pending_match_batch,
    apply_pending_retrieval_batch,
    build_match_arguments,
    build_retrieval_arguments,
    build_mcp_app,
    filter_match_result,
    material_search_confirmation_required,
    resolve_mcp_tool_context,
    resolve_next_match_batch,
)


class _Reader:
    def get(self, user_id: str):
        return SimpleNamespace(
            is_inner_user=user_id == "internal-user",
            company="Example Co.",
        )


def _capture_mcp_tools(monkeypatch, runtime):
    fastmcp = pytest.importorskip("mcp.server.fastmcp")
    registered = {}

    class FakeFastMCP:
        def __init__(self, *_args, **_kwargs):
            self.tools = {}
            self.session_manager = object()

        def tool(self, *, name, **options):
            def register(function):
                self.tools[name] = {"function": function, **options}
                registered[name] = self.tools[name]
                return function

            return register

        def streamable_http_app(self):
            return SimpleNamespace(state=SimpleNamespace())

    monkeypatch.setattr(fastmcp, "FastMCP", FakeFastMCP)
    app = build_mcp_app(runtime)
    return app, registered


def test_mcp_tool_inputs_reject_model_supplied_identity():
    with pytest.raises(ValidationError):
        RetrievalToolInput.model_validate({"query": "芯片", "user_id": "spoofed"})

    with pytest.raises(ValidationError):
        MatchToolInput.model_validate(
            {
                "query": "芯片",
                "direction": "demand_to_supply",
                "user_role": "internal",
            }
        )

    assert MatchToolInput.model_validate(
        {"query": "芯片", "direction": "demand_to_supply"}
    ).knowledge_types == []
    assert RetrievalToolInput.model_validate({"query": "芯片"}).knowledge_types == []

    with pytest.raises(ValidationError):
        MatchToolInput.model_validate(
            {
                "query": "芯片",
                "direction": "demand_to_supply",
                "exclude_result_ids": ["model-controlled-id"],
            }
        )

    with pytest.raises(ValidationError):
        RetrievalToolInput.model_validate({"query": "芯片", "top_k": 50})

    with pytest.raises(ValidationError):
        RetrievalToolInput.model_validate({"query": "芯片", "use_rerank": False})


def test_unspecified_scope_uses_existing_defaults_and_saved_batch_scope_stays_exact():
    context = ToolContext(user_id="u-1", user_role="internal", trace_id="trace-1")
    supply_types = ["solution", "achievement", "expert", "enterprise"]

    retrieval, _ = apply_pending_retrieval_batch(
        RetrievalToolInput(query="智能制造"),
        None,
        default_knowledge_types=["demand"],
    )
    match_to_supply, _ = apply_pending_match_batch(
        MatchToolInput(query="智能制造", direction="demand_to_supply"), None
    )
    match_to_demand, _ = apply_pending_match_batch(
        MatchToolInput(query="智能制造", direction="supply_to_demand"), None
    )

    assert retrieval.knowledge_types == ["demand"]
    assert build_retrieval_arguments(retrieval, context)["knowledge_types"] == ["demand"]
    assert match_to_supply.knowledge_types == supply_types
    assert build_match_arguments(match_to_supply, context)["knowledge_types"] == supply_types
    assert match_to_demand.knowledge_types == ["demand"]
    assert build_match_arguments(match_to_demand, context)["knowledge_types"] == ["demand"]
    assert _default_retrieval_knowledge_types(
        RetrievalToolInput(query="智能制造"),
        {"request_context": {"intent_hint": "supply_demand_match", "match_direction": "demand_to_supply"}},
    ) == supply_types
    assert _default_retrieval_knowledge_types(
        RetrievalToolInput(query="智能制造"),
        {"request_context": {"intent_hint": "enterprise_profile"}},
    ) == ["enterprise"]
    assert _default_retrieval_knowledge_types(
        RetrievalToolInput(query="智能制造"), {}
    ) == []

    scoped, _ = apply_pending_retrieval_batch(
        RetrievalToolInput(query="换一批"),
        {
            "query": "上下游企业",
            "business_intent": "knowledge_search",
            "knowledge_types": ["enterprise"],
            "shown_result_ids": ["enterprise:mysql:17"],
        },
    )
    assert scoped.knowledge_types == ["enterprise"]


def test_mcp_context_uses_session_owner_and_thread_headers():
    request = SimpleNamespace(
        state=SimpleNamespace(trace_id="trace-1"),
        headers={"x-kc-thread-id": "thread-1"},
    )
    session_store = SimpleNamespace(get=lambda thread_id: {"user_id": "internal-user"})

    result = resolve_mcp_tool_context(
        SimpleNamespace(
            user_identity_reader=_Reader(),
            require_conversation=lambda: SimpleNamespace(session_store=session_store),
        ),
        request,
    )

    assert result == ToolContext(
        user_id="internal-user",
        user_role="internal",
        company="Example Co.",
        thread_id="thread-1",
        trace_id="trace-1",
    )


def test_mcp_context_rejects_missing_authentication():
    request = SimpleNamespace(state=SimpleNamespace(), headers={})

    with pytest.raises(PermissionError):
        resolve_mcp_tool_context(SimpleNamespace(user_identity_reader=_Reader()), request)


def test_match_arguments_map_direction_and_keep_server_context_out_of_arguments():
    result = build_match_arguments(
        MatchToolInput(
            query="芯片",
            user_goal="帮我找芯片相关方案",
            direction="demand_to_supply",
            knowledge_types=["solution", "achievement", "expert", "enterprise"],
        ),
        ToolContext(
            user_id="u-1",
            user_role="internal",
            thread_id="thread-1",
            trace_id="trace-1",
        ),
        excluded_ids={"solution:source:s-1"},
    )

    assert result == {
        "query": "芯片",
        "knowledge_types": ["solution", "achievement", "expert", "enterprise"],
        "top_k": 3,
        "exclude_result_ids": ["solution:source:s-1"],
        "use_rerank": True,
        "expand_query": False,
        "identity": {"user_id": "u-1", "user_role": "internal"},
        "thread_id": "thread-1",
        "trace_id": "trace-1",
    }


def test_match_arguments_preserve_explicit_enterprise_collection_scope():
    result = build_match_arguments(
        MatchToolInput(
            query="有没有可以合作的上下游企业",
            direction="demand_to_supply",
            knowledge_types=["enterprise"],
        ),
        ToolContext(user_id="u-1", user_role="internal", trace_id="trace-1"),
    )

    assert result["knowledge_types"] == ["enterprise"]


def test_match_arguments_use_only_the_scope_selected_by_the_model():
    result = build_match_arguments(
        MatchToolInput(
            query="芯片",
            direction="demand_to_supply",
            knowledge_types=["enterprise"],
        ),
        ToolContext(user_id="u-1", user_role="internal", trace_id="trace-1"),
    )

    assert result["knowledge_types"] == ["enterprise"]


def test_new_match_uses_current_direction_and_next_batch_uses_saved_direction():
    current = MatchToolInput(
        query="研究方向", direction="supply_to_demand", knowledge_types=["demand"]
    )
    pending = {
        "query": "工业机器人",
        "direction": "demand_to_supply",
        "knowledge_types": ["solution"],
        "shown_result_ids": ["solution:source:s-1"],
    }

    current_batch, current_excluded = apply_pending_match_batch(current, None)
    next_batch, excluded = apply_pending_match_batch(current, pending)
    arguments = build_match_arguments(
        current_batch,
        ToolContext(user_id="u-1", user_role="internal", trace_id="trace-1"),
        excluded_ids={"solution:source:s-1"},
    )

    assert current_batch.direction == "supply_to_demand"
    assert current_excluded == set()
    assert next_batch.direction == "demand_to_supply"
    assert next_batch.knowledge_types == ["solution"]
    assert excluded == {"solution:source:s-1"}
    assert arguments["top_k"] == 3
    assert arguments["exclude_result_ids"] == ["solution:source:s-1"]


def test_retrieval_arguments_force_three_reranked_results():
    result = build_retrieval_arguments(
        RetrievalToolInput(
            query="采购机器人",
            user_goal="我想找适合生产线的采购机器人",
            knowledge_type="demand",
            knowledge_types=["demand"],
        ),
        ToolContext(user_id="u-1", user_role="internal", trace_id="trace-1"),
    )

    assert result["top_k"] == 3
    assert result["use_rerank"] is True
    assert "user_goal" not in result
    assert "allow_web_fallback" not in result


def test_external_retrieval_is_restricted_to_public_scope_even_if_model_requests_private():
    context = ToolContext(user_id="u-1", user_role="external", trace_id="trace-1")

    retrieval = build_retrieval_arguments(
        RetrievalToolInput(
            query="专家检索",
            scope_id="private/company-1",
            knowledge_types=["expert"],
        ),
        context,
    )
    match = build_match_arguments(
        MatchToolInput(
            query="专家检索",
            direction="demand_to_supply",
            scope_id="private/company-1",
            knowledge_types=["expert"],
        ),
        context,
    )

    assert retrieval["scope_id"] == "public"
    assert retrieval["knowledge_types"] == ["expert"]
    assert match["scope_id"] == "public"
    assert match["knowledge_types"] == ["expert"]

    internal = build_retrieval_arguments(
        RetrievalToolInput(query="专家检索", scope_id="private/company-1"),
        ToolContext(user_id="u-1", user_role="internal", trace_id="trace-1"),
    )
    assert internal["scope_id"] == "private/company-1"


def test_match_tool_does_not_accept_result_count_or_rerank_controls():
    with pytest.raises(ValidationError):
        MatchToolInput(
            query="扫地机器人", direction="demand_to_supply", knowledge_types=["enterprise"], top_k=4
        )
    with pytest.raises(ValidationError):
        MatchToolInput(
            query="扫地机器人",
            direction="demand_to_supply",
            knowledge_types=["enterprise"],
            use_rerank=False,
        )


def test_filter_match_result_applies_exclusions_to_public_and_nested_payloads():
    result = {
        "status": "SUCCESS",
        "trace_id": "trace-1",
        "evidences": [
            {"source_id": "keep", "source_knowledge_type": "solution"},
            {"source_id": "drop", "source_knowledge_type": "solution"},
        ],
        "data": {
            "evidences": [
                {"source_id": "keep", "source_knowledge_type": "solution"},
                {"source_id": "drop", "source_knowledge_type": "solution"},
            ]
        },
    }

    filtered = filter_match_result(result, {"solution:source:drop"}, top_k=1)

    assert [item["source_id"] for item in filtered["evidences"]] == ["keep"]
    assert [item["source_id"] for item in filtered["data"]["evidences"]] == ["keep"]


def test_filter_match_result_never_returns_more_than_three_items():
    result = {
        "evidences": [
            {"source_id": f"result-{index}", "source_knowledge_type": "solution"}
            for index in range(5)
        ]
    }

    filtered = filter_match_result(result, set(), top_k=50)

    assert len(filtered["evidences"]) == 3


def test_pending_match_batch_overrides_model_query_and_exclusions_from_thread_state():
    params = MatchToolInput(
        query="模型生成的泛化查询",
        direction="supply_to_demand",
        knowledge_types=["demand"],
    )
    pending = {
        "query": "扫地机器人采购",
        "direction": "demand_to_supply",
        "knowledge_types": ["enterprise"],
        "shown_result_ids": ["id-1", "id-2"],
    }

    scoped, excluded_ids = apply_pending_match_batch(params, pending)
    assert scoped.query == "扫地机器人采购"
    assert scoped.direction == "demand_to_supply"
    assert scoped.knowledge_types == ["enterprise"]
    assert excluded_ids == {"id-1", "id-2"}


def test_pending_demand_discovery_batch_pins_query_and_demand_collection():
    params = RetrievalToolInput(
        query="模型生成的新查询", knowledge_type="enterprise", knowledge_types=["enterprise"]
    )
    pending = {
        "query": "钢板采购需求",
        "business_intent": "demand_discovery",
        "knowledge_type": "demand",
        "shown_result_ids": ["demand:mysql:17"],
    }

    scoped, excluded_ids = apply_pending_retrieval_batch(params, pending)

    assert scoped.query == "钢板采购需求"
    assert scoped.knowledge_type == "demand"
    assert scoped.knowledge_types == ["demand"]
    assert excluded_ids == {"demand:mysql:17"}


def test_pending_supply_match_retrieval_preserves_enterprise_only_scope():
    params = RetrievalToolInput(query="换一批", knowledge_types=["enterprise"], next_batch=True)
    pending = {
        "query": "有没有可以合作的上下游企业",
        "business_intent": "supply_demand_match",
        "direction": "demand_to_supply",
        "knowledge_type": "enterprise",
        "knowledge_types": ["enterprise"],
        "shown_result_ids": ["enterprise:mysql:17"],
    }

    scoped, excluded_ids = apply_pending_retrieval_batch(params, pending)

    assert scoped.knowledge_type == "enterprise"
    assert scoped.knowledge_types == ["enterprise"]
    assert excluded_ids == {"enterprise:mysql:17"}


def test_pending_supply_match_retrieval_requires_saved_scope_instead_of_inferencing():
    params = RetrievalToolInput(query="换一批", knowledge_types=["enterprise"], next_batch=True)
    pending = {
        "query": "有没有可以合作的上下游企业",
        "business_intent": "supply_demand_match",
        "direction": "demand_to_supply",
        "shown_result_ids": ["enterprise:mysql:17"],
    }

    with pytest.raises(ValueError, match="知识库范围"):
        apply_pending_retrieval_batch(params, pending)


def test_next_batch_is_per_tool_call_and_never_reads_stale_pending_marker():
    state = {
        "pending_match_batch": {
            "query": "旧查询",
            "shown_result_ids": ["stale-id"],
            "business_intent": "demand_discovery",
        },
        "chat_flow": {
            "match_batch": {
                "original_query": "钢筋采购",
                "current_query": "钢筋采购",
                "shown_result_ids": ["demand:mysql:17"],
                "business_intent": "demand_discovery",
            },
        },
    }

    assert resolve_next_match_batch(False, state) is None
    assert resolve_next_match_batch(True, state) == {
        "query": "钢筋采购",
        "user_goal": "钢筋采购",
        "web_fallback_allowed": False,
        "business_intent": "demand_discovery",
        "direction": "",
        "knowledge_type": None,
        "knowledge_types": [],
        "shown_result_ids": ["demand:mysql:17"],
    }


def test_material_search_is_blocked_until_same_thread_intent_confirmation():
    assert material_search_confirmation_required(
        {"chat_flow": {"material_intent_confirmation_required": True}}
    )
    assert not material_search_confirmation_required(
        {"chat_flow": {"material_intent_confirmation_required": False}}
    )
    assert not material_search_confirmation_required({"chat_flow": {}})


def test_next_batch_uses_explicit_current_request_action():
    state = {
        "request_flow_action": "next_match_batch",
        "chat_flow": {
            "match_batch": {
                "original_query": "钢筋采购",
                "current_query": "钢筋采购",
                "shown_result_ids": ["demand:mysql:17"],
                "business_intent": "demand_discovery",
            },
        },
    }

    assert resolve_next_match_batch(False, state) == {
        "query": "钢筋采购",
        "user_goal": "钢筋采购",
        "web_fallback_allowed": False,
        "business_intent": "demand_discovery",
        "direction": "",
        "knowledge_type": None,
        "knowledge_types": [],
        "shown_result_ids": ["demand:mysql:17"],
    }
    state["request_flow_action"] = None
    assert resolve_next_match_batch(False, state) is None


def test_next_batch_failure_is_marked_as_applied_for_state_cleanup():
    context = ToolContext(
        user_id="u-1",
        user_role="internal",
        thread_id="thread-1",
        trace_id="trace-1",
    )

    assert _failure(context, "检索失败", next_batch=True)["_next_match_batch"] is True
    assert "_next_match_batch" not in _failure(context, "普通检索失败")


def test_missing_next_batch_state_returns_guidance_instead_of_tool_failure():
    context = ToolContext(
        user_id="u-1",
        user_role="internal",
        thread_id="thread-1",
        trace_id="trace-1",
    )

    result = _no_more_match_batch(context)

    assert result["status"] == "NO_EVIDENCE"
    assert result["match_results"] == []
    assert result["evidences"] == []
    assert "联网搜索" in result["answer"]
    assert "明确告诉我" in result["answer"]
    assert result["_next_match_batch"] is True


def test_empty_next_batch_result_is_detected_in_retrieval_envelope():
    assert not _has_match_batch_items({"data": {"evidences": []}})
    assert _has_match_batch_items({"data": {"evidences": [{"source_id": "new"}]}})


def test_pending_knowledge_search_batch_preserves_collection_and_excludes_seen_records():
    params = RetrievalToolInput(
        query="再找其他候选",
        knowledge_types=["expert"],
        next_batch=True,
    )
    pending = {
        "query": "语音识别专家",
        "business_intent": "knowledge_search",
        "knowledge_types": ["expert", "achievement"],
        "shown_result_ids": ["expert:mysql:1", "achievement:mysql:2"],
    }

    scoped, excluded_ids = apply_pending_retrieval_batch(params, pending)

    assert scoped.query == "语音识别专家"
    assert scoped.knowledge_types == ["expert", "achievement"]
    assert scoped.knowledge_type is None
    assert excluded_ids == {"expert:mysql:1", "achievement:mysql:2"}
    arguments = build_retrieval_arguments(
        scoped,
        ToolContext(user_id="u-1", user_role="internal", thread_id="t-1", trace_id="trace-1"),
        excluded_ids=excluded_ids,
    )
    assert "next_batch" not in arguments
    assert arguments["top_k"] == 3
    assert arguments["exclude_result_ids"] == sorted(excluded_ids)


def test_next_batch_passes_excluded_result_ids_to_retrieval():
    params = MatchToolInput(
        query="扫地机器人", direction="demand_to_supply", knowledge_types=["solution"]
    )
    arguments = build_match_arguments(
        params,
        ToolContext(user_id="u-1", user_role="internal", thread_id="thread-1", trace_id="trace-1"),
        excluded_ids={f"solution:mysql:{item}" for item in range(8)},
    )

    assert arguments["top_k"] == 3
    assert set(arguments["exclude_result_ids"]) == {
        f"solution:mysql:{item}" for item in range(8)
    }
    assert arguments["use_rerank"] is True


def test_mcp_allowed_hosts_include_local_docker_callback(monkeypatch):
    monkeypatch.delenv("KC_MCP_ALLOWED_HOSTS", raising=False)

    assert "host.docker.internal:*" in _mcp_allowed_hosts()


def test_mcp_allowed_hosts_can_be_overridden_for_server_deployment(monkeypatch):
    monkeypatch.setenv("KC_MCP_ALLOWED_HOSTS", "deerflow.example.com:8000,10.0.0.8:8000")

    assert _mcp_allowed_hosts() == ["deerflow.example.com:8000", "10.0.0.8:8000"]


def test_build_mcp_app_registers_all_current_tool_groups(monkeypatch):
    app, registered = _capture_mcp_tools(monkeypatch, SimpleNamespace())
    assert app.state.kc_mcp_session_manager is not None
    assert set(registered) >= {
        "kc_retrieval",
        "kc_match",
        "kc_document_list",
        "kc_document_detail",
        "kc_document_parse_result",
        "kc_document_parse",
        "kc_draft_create",
        "kc_draft_get",
        "kc_draft_list",
        "kc_draft_save",
        "kc_draft_publish",
        "kc_draft_unpublish",
        "kc_draft_delete",
        "kc_profile",
        "kc_customer",
        "kc_customer_save_visit",
    }
    assert "kc_contact" not in registered
    assert registered["kc_draft_delete"]["annotations"].destructiveHint is True
    assert registered["kc_document_parse"]["annotations"].readOnlyHint is False
    assert registered["kc_customer_save_visit"]["annotations"].readOnlyHint is False


def test_mcp_descriptions_select_tools_from_the_user_goal(monkeypatch):
    _app, registered = _capture_mcp_tools(monkeypatch, SimpleNamespace())

    assert "not solely from an entity mention" in registered["kc_retrieval"]["description"]
    assert "goal is to find compatible supply-demand candidates" in registered["kc_match"]["description"]
    assert "goal calls for public web information" in registered["kc_web_search"]["description"]
    assert "company name alone" in registered["kc_profile"]["description"]
    assert "拜访前准备属于资料检索与总结" in registered["kc_draft_create"]["description"]


@pytest.mark.parametrize("tool_name", ["kc_retrieval", "kc_match"])
@pytest.mark.parametrize("status", ["FAILED", "DEGRADED", "PARTIAL_SUCCESS"])
def test_failed_next_batch_search_is_not_reported_as_no_evidence(monkeypatch, tool_name, status):
    class _SessionStore:
        def get(self, _thread_id):
            return {
                "user_id": "internal-user",
                "state": {
                    "chat_flow": {
                        "match_batch": {
                            "original_query": "原始需求",
                            "current_query": "原始需求",
                            "business_intent": "supply_demand_match",
                            "direction": "supply_to_demand",
                            "knowledge_type": "demand",
                            "knowledge_types": ["demand"],
                            "shown_result_ids": ["demand:mysql:1"],
                        }
                    }
                },
            }

    class _Adapter:
        async def search_knowledge(self, *_args, **_kwargs):
            return {"status": status, "error": "Milvus unavailable", "data": {}}

    runtime = SimpleNamespace(
        require_conversation=lambda: SimpleNamespace(session_store=_SessionStore()),
        user_identity_reader=_Reader(),
        tool_adapter=_Adapter(),
    )
    _app, registered = _capture_mcp_tools(monkeypatch, runtime)
    request = SimpleNamespace(
        headers={"x-kc-thread-id": "thread-1", "x-trace-id": "trace-1"},
        state=SimpleNamespace(trace_id="trace-1"),
    )
    context = SimpleNamespace(request_context=SimpleNamespace(request=request))
    if tool_name == "kc_retrieval":
        params = RetrievalToolInput(
            query="换一批", knowledge_type="demand", knowledge_types=["demand"], next_batch=True
        )
    else:
        params = MatchToolInput(
            query="换一批",
            direction="supply_to_demand",
            knowledge_types=["demand"],
            next_batch=True,
        )

    import asyncio

    result = asyncio.run(registered[tool_name]["function"](params, context))

    assert result["status"] == status
    assert result["error"] == "Milvus unavailable"
    assert result["_next_match_batch"] is True


@pytest.mark.parametrize("status, expected", [("DEGRADED", "FAILED"), ("PARTIAL_SUCCESS", "PARTIAL_SUCCESS")])
def test_retrieval_agent_status_survives_adapter_and_next_batch(status, expected):
    import asyncio
    from agents.retrieval_agent.application.retrieval_service import RetrievalAgent
    from agents.retrieval_agent.schemas import RetrievalRequest, RetrievalResult
    from assistant.integrations.mcp_server import _finish_match_batch
    from core.contracts.agent import AgentContext
    from runtime.tool_adapters.adapters import InProcessToolAdapter

    agent = object.__new__(RetrievalAgent)

    async def retrieve(_request):
        return RetrievalResult(status=status, query="test", error="Milvus unavailable")

    agent.retrieve = retrieve
    context = ToolContext(user_id="u", user_role="internal", trace_id="test")
    result = asyncio.run(agent.invoke(
        AgentContext(request_id="test", trace_id="test"),
        RetrievalRequest(query="test", identity=context.identity()),
    ))
    envelope = InProcessToolAdapter._envelope(result, context)
    batch = _finish_match_batch(envelope, context)
    assert envelope["data"]["status"] == status
    assert envelope["status"] == batch["status"] == expected
    assert batch["error"] == "Milvus unavailable"
