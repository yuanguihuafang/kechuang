import asyncio
from types import SimpleNamespace

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import ValidationError

from core.contracts.tools import ToolContext
from assistant.integrations.mcp_tools.customer_profile import (
    CustomerVisitSaveInput,
    CustomerVisitsInput,
    ProfileSearchInput,
    register_customer_profile_tools,
)


class _MCP:
    def __init__(self):
        self.tools = {}
        self.annotations = {}

    def tool(self, *, name, annotations=None, **_kwargs):
        def decorate(func):
            self.tools[name] = func
            self.annotations[name] = annotations
            return func

        return decorate


def _registered(runtime, *, user_id="17", role="internal", thread_id="thread-1", return_mcp=False):
    mcp = _MCP()
    context = ToolContext(user_id=user_id, user_role=role, thread_id=thread_id, trace_id="trace-1")
    register_customer_profile_tools(
        mcp,
        runtime,
        ToolAnnotations(
            readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
        ),
        lambda request: request,
        lambda _runtime, _request: context,
        lambda tool_context, message: {
            "status": "FAILED",
            "error": message,
            "trace_id": tool_context.trace_id,
        },
    )
    return mcp if return_mcp else mcp.tools


def test_fastmcp_registers_customer_profile_tools_without_model_supplied_context():
    mcp = FastMCP("customer-profile-test")
    register_customer_profile_tools(
        mcp,
        SimpleNamespace(),
        ToolAnnotations(readOnlyHint=True),
        lambda ctx: ctx.request,
        lambda _runtime, _request: None,
        lambda _context, message: {"error": message},
    )

    tools = {tool.name: tool for tool in mcp._tool_manager.list_tools()}
    assert set(tools) == {"kc_profile", "kc_customer", "kc_customer_save_visit"}
    assert "ctx" not in tools["kc_profile"].parameters["properties"]


def test_visit_confirmation_accepts_only_server_draft_and_version():
    assert set(CustomerVisitSaveInput.model_fields) == {"draft_id", "expected_version"}
    for values in (
        {"draft_id": "draft-1"},
        {"draft_id": "draft-1", "expected_version": 0},
        {"draft_id": "draft-1", "expected_version": 1, "user_id": "99"},
        {
            "draft_id": "draft-1",
            "expected_version": 1,
            "structured_data": {"enterprise_name": "甲"},
        },
    ):
        with pytest.raises(ValidationError):
            CustomerVisitSaveInput.model_validate(values)


def test_external_visit_tools_use_neutral_capability_errors():
    tools = _registered(SimpleNamespace(), role="external")

    read_result = asyncio.run(tools["kc_customer"](CustomerVisitsInput(), object()))
    save_result = asyncio.run(
        tools["kc_customer_save_visit"](
            CustomerVisitSaveInput(draft_id="draft-1", expected_version=1), object()
        )
    )

    assert read_result["error"] == "当前暂不支持处理客户拜访记录"
    assert save_result["error"] == "当前暂不支持处理客户拜访记录"
    assert "权限" not in read_result["error"]
    assert "权限" not in save_result["error"]


def _visit_setup(*, fail_first_index=False):
    from services.persistence.postgres import InMemoryDraftStore

    drafts = InMemoryDraftStore()
    record = drafts.create(
        user_id="17",
        user_role="internal",
        thread_id="thread-1",
        draft={
            "draft_type": "customer_visit",
            "fields": {
                "enterprise_portrait": "甲公司生产芯片",
                "potential_demand_list_text": "改造检测设备",
            },
        },
    )
    saved, indexed, sessions = [], [], []

    class Sink:
        def save(self, payload):
            saved.append(payload)
            return {"business_record_id": "42", "status": "SAVED"}

    class Agent:
        async def store_session_summary(self, payload):
            indexed.append(payload)
            status = "FAILED" if fail_first_index and len(indexed) == 1 else "SUCCESS"
            return SimpleNamespace(
                status=status, error="index unavailable", model_dump=lambda: {"status": status}
            )

    runtime = SimpleNamespace(
        require_draft_store=lambda: drafts,
        require_business_record_sink=lambda: Sink(),
        embedded_retrieval=SimpleNamespace(agent=Agent()),
        require_conversation=lambda: SimpleNamespace(
            session_store=SimpleNamespace(
                get=lambda _thread_id: {},
                upsert=lambda thread_id, **kwargs: sessions.append((thread_id, kwargs)),
            )
        ),
    )
    return runtime, drafts, record, saved, indexed, sessions


def test_visit_confirmation_retries_and_edits_keep_same_draft():
    runtime, drafts, record, saved, indexed, sessions = _visit_setup()
    tool = _registered(runtime)["kc_customer_save_visit"]
    params = CustomerVisitSaveInput(draft_id=record["draft_id"], expected_version=1)
    result = asyncio.run(tool(params, object()))
    assert result["status"] == "SUCCESS"
    assert result["data"]["business_record"]["business_record_id"] == "42"
    assert saved[0].user_id == saved[0].confirmed_by == "17"
    assert saved[0].structured_data["fields"] == record["draft"]["fields"]
    assert saved[0].thread_id == "thread-1"
    assert len(saved) == len(indexed) == len(sessions) == 1
    assert asyncio.run(tool(params, object()))["status"] == "SUCCESS"
    assert len(saved) == 1

    current = drafts.get(record["draft_id"], user_id="17")
    edited = drafts.update(
        record["draft_id"],
        user_id="17",
        expected_version=current["version"],
        draft={"fields": {"budget_range": "50万元"}},
    )
    assert asyncio.run(tool(params, object()))["status"] == "FAILED"
    updated = asyncio.run(
        tool(
            CustomerVisitSaveInput(
                draft_id=record["draft_id"],
                expected_version=edited["version"],
            ),
            object(),
        )
    )
    assert updated["status"] == "SUCCESS"
    assert saved[-1].structured_data["fields"]["budget_range"] == "50万元"
    assert saved[-1].draft_id == saved[0].draft_id
    assert len(saved) == 2 and len(drafts.list(user_id="17")) == 1


def test_visit_confirmation_rejects_wrong_owner_thread_role_or_version():
    runtime, drafts, record, saved, _, _ = _visit_setup()
    params = CustomerVisitSaveInput(draft_id=record["draft_id"], expected_version=1)
    for identity in (
        {"user_id": "28"},
        {"role": "external"},
        {"thread_id": "other"},
        {"user_id": "bad-id"},
        {"user_id": "１７"},
        {"thread_id": None},
    ):
        result = asyncio.run(
            _registered(runtime, **identity)["kc_customer_save_visit"](params, object())
        )
        assert result["status"] == "FAILED"
    wrong_version = CustomerVisitSaveInput(draft_id=record["draft_id"], expected_version=2)
    assert (
        asyncio.run(_registered(runtime)["kc_customer_save_visit"](wrong_version, object()))[
            "status"
        ]
        == "FAILED"
    )
    assert saved == []


def test_visit_confirmation_retries_index_failure_without_new_draft():
    runtime, drafts, record, saved, indexed, _ = _visit_setup(fail_first_index=True)
    tool = _registered(runtime)["kc_customer_save_visit"]
    params = CustomerVisitSaveInput(draft_id=record["draft_id"], expected_version=1)
    assert asyncio.run(tool(params, object()))["status"] == "FAILED"
    assert drafts.get(record["draft_id"], user_id="17")["version"] == 1
    assert asyncio.run(tool(params, object()))["status"] == "SUCCESS"
    assert len(drafts.list(user_id="17")) == 1
    assert saved[0].draft_id == saved[1].draft_id
    assert len(indexed) == 2


def test_concurrent_visit_confirmation_writes_once():
    async def scenario():
        runtime, _, record, saved, _, _ = _visit_setup()
        tool = _registered(runtime)["kc_customer_save_visit"]
        params = CustomerVisitSaveInput(draft_id=record["draft_id"], expected_version=1)
        results = await asyncio.gather(tool(params, object()), tool(params, object()))
        assert all(item["status"] == "SUCCESS" for item in results)
        assert len(saved) == 1

    asyncio.run(scenario())


def test_inputs_reject_identity_and_bound_result_size():
    with pytest.raises(ValidationError):
        ProfileSearchInput.model_validate(
            {"query": "芯片", "knowledge_type": "expert", "user_id": "99"}
        )
    with pytest.raises(ValidationError):
        CustomerVisitsInput.model_validate({"user_id": "99"})
    with pytest.raises(ValidationError):
        CustomerVisitsInput(limit=201)


def test_internal_profile_search_uses_permission_checked_vector_retrieval():
    calls = []

    class Adapter:
        async def search_knowledge(self, arguments, *, context):
            calls.append((arguments, context))
            return {"status": "SUCCESS", "data": {"records": []}, "trace_id": context.trace_id}

    tools = _registered(
        SimpleNamespace(
            tool_adapter=Adapter(),
            require_conversation=lambda: SimpleNamespace(session_store=None),
        )
    )
    result = asyncio.run(
        tools["kc_profile"](
            ProfileSearchInput(query="芯片", knowledge_type="expert", allow_web_fallback=False),
            object(),
        )
    )

    assert result["status"] == "NO_EVIDENCE"
    arguments, context = calls[0]
    assert arguments["identity"] == {"user_id": "17", "user_role": "internal"}
    assert arguments["knowledge_type"] == "expert"
    assert "scope_id" not in arguments
    assert arguments["top_k"] == 3
    assert arguments["use_rerank"] is True
    assert context.user_id == "17"


def test_profile_search_logs_trace_context_without_query_text(monkeypatch, caplog):
    class Adapter:
        async def search_knowledge(self, _arguments, *, context):
            return {"status": "SUCCESS", "data": {"records": []}, "trace_id": context.trace_id}

    caplog.set_level("INFO", logger="assistant.integrations.mcp_tools.customer_profile")
    tools = _registered(
        SimpleNamespace(
            tool_adapter=Adapter(),
            require_conversation=lambda: SimpleNamespace(session_store=None),
        )
    )
    asyncio.run(
        tools["kc_profile"](
            ProfileSearchInput(
                query="隐私查询词", knowledge_type="enterprise", allow_web_fallback=False
            ),
            object(),
        )
    )

    messages = [record.getMessage() for record in caplog.records]
    assert any(
        "tool=kc_profile" in message
        and "status=NO_EVIDENCE" in message
        and "user_id=17" in message
        and "trace_id=trace-1" in message
        for message in messages
    )
    assert not any(
        record.levelname == "WARNING" and "tool=kc_profile" in record.getMessage()
        for record in caplog.records
    )
    assert all("隐私查询词" not in message for message in messages)


def test_profile_web_fallback_logs_handled_outcome_as_info(caplog):
    class Adapter:
        async def search_knowledge(self, _arguments, *, context):
            return {"status": "SUCCESS", "data": {"records": []}, "trace_id": context.trace_id}

        async def search_tavily(self, _arguments, *, context):
            return {
                "status": "SUCCESS",
                "data": {"results": [{"title": "公开企业资料", "url": "https://example.com"}]},
                "trace_id": context.trace_id,
            }

    caplog.set_level("INFO", logger="assistant.integrations.mcp_tools.customer_profile")
    tools = _registered(
        SimpleNamespace(
            tool_adapter=Adapter(),
            require_conversation=lambda: SimpleNamespace(session_store=None),
        )
    )
    result = asyncio.run(
        tools["kc_profile"](
            ProfileSearchInput(
                query="河北利仕化学", user_goal="拜访前查询河北利仕化学",
                knowledge_type="enterprise",
            ),
            object(),
        )
    )

    assert result["status"] == "WEB_FALLBACK"
    assert any(
        record.levelname == "INFO"
        and "tool=kc_profile" in record.getMessage()
        and "status=WEB_FALLBACK" in record.getMessage()
        for record in caplog.records
    )
    assert not any(
        record.levelname == "WARNING" and "tool=kc_profile" in record.getMessage()
        for record in caplog.records
    )


def test_customer_visit_failure_logs_trace_and_exception_type_without_message(monkeypatch, caplog):
    async def fail(_service, _payload):
        raise RuntimeError("database password=do-not-log")

    monkeypatch.setattr(
        "assistant.integrations.mcp_tools.customer_profile.BusinessRecordApplicationService.confirm", fail
    )
    caplog.set_level("INFO", logger="assistant.integrations.mcp_tools.customer_profile")
    runtime, _, record, _, _, _ = _visit_setup()
    params = CustomerVisitSaveInput(draft_id=record["draft_id"], expected_version=1)
    result = asyncio.run(_registered(runtime)["kc_customer_save_visit"](params, object()))

    assert result["status"] == "FAILED"
    messages = [record.getMessage() for record in caplog.records]
    assert any(
        "tool=kc_customer_save_visit" in message
        and "status=FAILED" in message
        and "user_id=17" in message
        and "thread_id=thread-1" in message
        and "trace_id=trace-1" in message
        and "error_type=RuntimeError" in message
        for message in messages
    )
    assert all("database password=do-not-log" not in message for message in messages)


def test_external_profile_search_uses_permission_checked_retrieval():
    calls = []

    class Adapter:
        async def search_knowledge(self, arguments, *, context):
            calls.append((arguments, context))
            return {"status": "SUCCESS", "data": {"records": []}, "trace_id": context.trace_id}

    tools = _registered(
        SimpleNamespace(
            tool_adapter=Adapter(),
            require_conversation=lambda: SimpleNamespace(session_store=None),
        ),
        user_id="28",
        role="external",
    )
    result = asyncio.run(
        tools["kc_profile"](
            ProfileSearchInput(
                query="芯片", knowledge_type="enterprise", allow_web_fallback=False
            ),
            object(),
        )
    )

    assert result["status"] == "NO_EVIDENCE"
    assert calls[0][0]["identity"] == {"user_id": "28", "user_role": "external"}
    assert calls[0][0]["knowledge_type"] == "enterprise"
    assert calls[0][0]["scope_id"] == "public"
    assert calls[0][0]["top_k"] == 3
    assert calls[0][0]["use_rerank"] is True


def test_customer_visits_are_limited_to_authenticated_internal_owner():
    calls = []

    class Sink:
        def list(self, *, user_id, record_type):
            calls.append((user_id, record_type))
            return [
                {
                    "business_record_id": "1",
                    "structured_data": {
                        "user_card_id": 17,
                        "enterprise_name": "甲公司",
                        "inquiry_status": 1,
                        "is_deleted": 0,
                    },
                },
                {
                    "business_record_id": "2",
                    "structured_data": {
                        "user_card_id": 28,
                        "enterprise_name": "乙公司",
                        "inquiry_status": 1,
                        "is_deleted": 0,
                    },
                },
            ]

    tools = _registered(SimpleNamespace(require_business_record_sink=lambda: Sink()))
    result = asyncio.run(tools["kc_customer"](CustomerVisitsInput(limit=5), object()))

    assert calls == [("17", "customer_visit")]
    assert [item["business_record_id"] for item in result["data"]["records"]] == ["1"]


def test_customer_visits_filter_by_query_without_changing_unfiltered_reads():
    records = [
        {
            "business_record_id": "1",
            "structured_data": {
                "user_card_id": 17,
                "enterprise_name": "甲公司",
                "inquiry_status": 1,
                "is_deleted": 0,
                "potential_demand_list_text": "需要工业机器人进行焊接自动化",
            },
            "raw_text": "客户希望提升焊接效率。",
        },
        {
            "business_record_id": "2",
            "structured_data": {
                "user_card_id": 17,
                "enterprise_name": "乙公司",
                "inquiry_status": 1,
                "is_deleted": 0,
                "potential_demand_list_text": "寻找办公楼清洁服务",
            },
            "raw_text": "暂无机器人相关需求。",
        },
    ]

    class Sink:
        def list(self, *, user_id, record_type):
            assert (user_id, record_type) == ("17", "customer_visit")
            return records

    tools = _registered(SimpleNamespace(require_business_record_sink=lambda: Sink()))

    matching = asyncio.run(tools["kc_customer"](CustomerVisitsInput(query="焊接自动化"), object()))
    unrelated = asyncio.run(
        tools["kc_customer"](CustomerVisitsInput(query="智能问答客服系统"), object())
    )
    unfiltered = asyncio.run(tools["kc_customer"](CustomerVisitsInput(limit=5), object()))

    assert [item["business_record_id"] for item in matching["data"]["records"]] == ["1"]
    assert unrelated["data"]["records"] == []
    assert [item["business_record_id"] for item in unfiltered["data"]["records"]] == ["1", "2"]


def test_customer_visits_reject_external_and_nonnumeric_identity_before_query():
    sink = SimpleNamespace(list=lambda **_kwargs: pytest.fail("private records queried"))
    runtime = SimpleNamespace(require_business_record_sink=lambda: sink)
    for user_id, role in (("17", "external"), ("bad-id", "internal")):
        tools = _registered(runtime, user_id=user_id, role=role)
        result = asyncio.run(tools["kc_customer"](CustomerVisitsInput(), object()))
        assert result["status"] == "FAILED"
