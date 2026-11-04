"""MCP draft operations keep the existing route and publishing rules."""

import asyncio
from types import SimpleNamespace

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import ValidationError

from core.contracts.draft_contracts import draft_fields
from core.contracts.tools import ToolContext
from assistant.integrations.mcp_tools.draft import (
    _DRAFT_FIELD_MODELS,
    DraftCreateInput,
    DraftIdInput,
    DraftListInput,
    DraftPublishInput,
    DraftSaveInput,
    DraftUnpublishInput,
    register_draft_tools,
)
from services.persistence.postgres import InMemoryDraftStore


class _MCP:
    def __init__(self):
        self.tools = {}

    def tool(self, *, name, **_kwargs):
        def register(function):
            self.tools[name] = function
            return function

        return register


def _setup(*, role="external", retrieval=None, sink=None, session_state=None):
    store = InMemoryDraftStore()
    session_store = SimpleNamespace(
        get=lambda thread_id: session_state if thread_id == "thread-1" else None,
        upsert=lambda *_args, **_kwargs: None,
    )
    runtime = SimpleNamespace(
        require_draft_store=lambda: store,
        require_conversation=lambda: SimpleNamespace(session_store=session_store),
        require_file_workflow=lambda: SimpleNamespace(
            task_store=SimpleNamespace(list=lambda **_kwargs: [])
        ),
        require_business_record_sink=lambda: sink,
        embedded_retrieval=SimpleNamespace(agent=retrieval),
    )
    mcp = _MCP()
    annotations = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True)
    register_draft_tools(
        mcp,
        runtime,
        annotations,
        lambda request: request,
        lambda _runtime, request: request.tool_context,
        lambda context, message: {
            "status": "FAILED",
            "data": {},
            "error": message,
            "trace_id": context.trace_id,
        },
    )
    request = SimpleNamespace(
        tool_context=ToolContext(
            user_id="user-1",
            user_role=role,
            thread_id="thread-1",
            trace_id="trace-1",
        )
    )
    return mcp.tools, store, request


def _call(tools, name, params, request):
    return asyncio.run(tools[name](params, request))


def test_mcp_drafts_do_not_build_http_router_or_fake_request(monkeypatch):
    import assistant.api.v1.drafts.routes as http

    def forbidden(*args, **kwargs):
        raise AssertionError("MCP must not build HTTP routes")

    monkeypatch.setattr(http, "build_draft_router", forbidden)
    import assistant.integrations.mcp_tools.draft as mcp
    if hasattr(mcp, "build_draft_router"):
        monkeypatch.setattr(mcp, "build_draft_router", forbidden)
    tools, _, request = _setup()
    result = _call(tools, "kc_draft_create", DraftCreateInput(draft_type="demand", draft={"title": "共享服务"}), request)
    assert result["status"] == "SUCCESS"


@pytest.mark.parametrize("key", ["_visit_confirmed_version", "match_results", "unknown_field"])
def test_draft_tools_reject_undefined_top_level_content(key):
    with pytest.raises(ValidationError):
        DraftCreateInput(draft_type="demand", draft={key: "unexpected"})
    with pytest.raises(ValidationError):
        DraftSaveInput(draft_id="draft-1", expected_version=1, draft={key: "unexpected"})


def test_create_list_read_save_and_delete_use_owner_and_version():
    tools, store, request = _setup()
    created = _call(
        tools,
        "kc_draft_create",
        DraftCreateInput(
            draft_type="demand",
            draft={"title": "芯片需求", "fields": {"contect": "需国产芯片"}},
        ),
        request,
    )
    draft_id = created["data"]["draft_id"]
    assert created["data"]["user_id"] == "user-1"
    assert created["data"]["draft_type"] == "demand"
    assert created["data"]["thread_id"] == "thread-1"
    assert (
        _call(tools, "kc_draft_list", DraftListInput(), request)["data"]["items"][0]["draft_id"]
        == draft_id
    )
    assert (
        _call(tools, "kc_draft_get", DraftIdInput(draft_id=draft_id), request)["data"]["version"]
        == 1
    )
    saved = _call(
        tools,
        "kc_draft_save",
        DraftSaveInput(
            draft_id=draft_id,
            expected_version=1,
            draft={"fields": {"contect": "升级芯片"}},
        ),
        request,
    )
    assert saved["data"]["draft"]["fields"]["contect"] == "升级芯片"
    assert saved["data"]["version"] == 2
    stale = _call(
        tools,
        "kc_draft_save",
        DraftSaveInput(
            draft_id=draft_id,
            expected_version=1,
            draft={"title": "过期编辑"},
        ),
        request,
    )
    assert stale["status"] == "FAILED"
    assert store.get(draft_id, user_id="user-1")["draft"]["title"] == "芯片需求"
    other = SimpleNamespace(
        tool_context=request.tool_context.model_copy(update={"user_id": "user-2"})
    )
    assert (
        _call(tools, "kc_draft_get", DraftIdInput(draft_id=draft_id), other)["status"] == "FAILED"
    )
    deleted = _call(tools, "kc_draft_delete", DraftIdInput(draft_id=draft_id), request)
    assert deleted["data"]["status"] == "DELETED"
    assert _call(tools, "kc_draft_list", DraftListInput(), request)["data"]["items"] == []


def test_create_demand_draft_persists_explicit_session_demand_type():
    tools, store, request = _setup(
        session_state={
            "user_id": "user-1",
            "state": {
                "chat_flow": {
                    "request_context": {
                        "draft_context": {"draft_type": "demand", "demand_type": "创新研发"}
                    }
                }
            },
        }
    )

    created = _call(
        tools,
        "kc_draft_create",
        DraftCreateInput(
            draft_type="demand",
            draft={"title": "智能问答客服系统研发需求", "fields": {"contect": "研发客服系统"}},
        ),
        request,
    )

    saved = store.get(created["data"]["draft_id"], user_id="user-1")
    assert saved["draft"]["fields"]["category"] == "创新研发"


def test_create_demand_draft_does_not_replace_explicit_category_or_infer_one():
    selected_session = {
        "user_id": "user-1",
        "state": {
            "chat_flow": {
                "request_context": {
                    "draft_context": {"draft_type": "demand", "demand_type": "创新研发"}
                }
            }
        },
    }
    tools, store, request = _setup(session_state=selected_session)
    created = _call(
        tools,
        "kc_draft_create",
        DraftCreateInput(
            draft_type="demand",
            draft={"title": "已指定类型", "fields": {"category": "软件服务"}},
        ),
        request,
    )
    saved = store.get(created["data"]["draft_id"], user_id="user-1")
    assert saved["draft"]["fields"]["category"] == "软件服务"

    tools, store, request = _setup()
    created = _call(
        tools,
        "kc_draft_create",
        DraftCreateInput(draft_type="demand", draft={"title": "未选择类型"}),
        request,
    )
    saved = store.get(created["data"]["draft_id"], user_id="user-1")
    assert "category" not in saved["draft"].get("fields", {})

    selected_session["user_id"] = "user-2"
    tools, store, request = _setup(session_state=selected_session)
    created = _call(
        tools,
        "kc_draft_create",
        DraftCreateInput(draft_type="demand", draft={"title": "其他用户会话"}),
        request,
    )
    saved = store.get(created["data"]["draft_id"], user_id="user-1")
    assert "category" not in saved["draft"].get("fields", {})


def test_inputs_reject_model_identity_and_unsupported_type():
    with pytest.raises(ValidationError):
        DraftCreateInput.model_validate({"draft_type": "expert", "draft": {}})
    with pytest.raises(ValidationError):
        DraftCreateInput.model_validate(
            {"draft_type": "solution", "draft": {}, "user_id": "user-2"}
        )
    with pytest.raises(ValidationError):
        DraftCreateInput.model_validate({"draft_type": "solution", "draft": {"user_role": "admin"}})
    with pytest.raises(ValidationError):
        DraftSaveInput.model_validate(
            {
                "draft_id": "draft-1",
                "expected_version": 1,
                "draft": {"mysql_record_id": "forged"},
            }
        )
    with pytest.raises(ValidationError):
        DraftCreateInput.model_validate(
            {
                "draft_type": "demand",
                "draft": {"fields": {"user_id": "user-2"}},
            }
        )
    for fields in ({"id": "999"}, {"create_time": "2000-01-01"}, {"src_draft_id": "other"}):
        with pytest.raises(ValidationError):
            DraftCreateInput.model_validate(
                {
                    "draft_type": "solution",
                    "draft": {"fields": fields},
                }
            )
        with pytest.raises(ValidationError):
            DraftSaveInput.model_validate(
                {
                    "draft_id": "draft-1",
                    "expected_version": 1,
                    "draft": {"mysql_fields": fields},
                }
            )


@pytest.mark.parametrize(
    ("draft_type", "foreign_field", "field_container"),
    [
        (draft_type, foreign_field, field_container)
        for draft_type, foreign_field in (
            ("demand", "product_type"),
            ("solution", "tech_maturity"),
            ("achievement", "deliver_cycle"),
        )
        for field_container in ("fields", "mysql_fields")
    ],
)
def test_create_rejects_fields_from_other_draft_types(draft_type, foreign_field, field_container):
    with pytest.raises(ValidationError):
        DraftCreateInput.model_validate(
            {
                "draft_type": draft_type,
                "draft": {field_container: {foreign_field: "不属于该草稿类型"}},
            }
        )


@pytest.mark.parametrize(
    ("draft_type", "field"),
    [
        ("demand", "category"),
        ("solution", "product_type"),
        ("achievement", "tech_maturity"),
    ],
)
def test_create_accepts_fields_from_each_own_type(draft_type, field):
    params = DraftCreateInput.model_validate(
        {"draft_type": draft_type, "draft": {"fields": {field: "本类型字段"}}}
    )
    assert params.draft.fields.model_dump(exclude_unset=True) == {field: "本类型字段"}


def test_mcp_field_models_match_each_draft_contract():
    assert set(_DRAFT_FIELD_MODELS) == {"demand", "solution", "achievement", "customer_visit"}
    for draft_type, model in _DRAFT_FIELD_MODELS.items():
        schema = model.model_json_schema()
        assert set(model.model_fields) == set(draft_fields(draft_type))
        assert schema["additionalProperties"] is False
        assert all(prop.get("description") for prop in schema["properties"].values())


def test_visit_draft_can_be_edited_but_not_published_publicly():
    tools, store, request = _setup(role="internal")
    params = DraftCreateInput(
        draft_type="customer_visit",
        draft={
            "fields": {"enterprise_portrait": "甲公司做机器人研发"},
        },
    )
    created = _call(tools, "kc_draft_create", params, request)
    assert created["status"] == "SUCCESS"
    draft_id = created["data"]["draft_id"]
    updated = _call(
        tools,
        "kc_draft_save",
        DraftSaveInput(
            draft_id=draft_id,
            expected_version=1,
            draft={"fields": {"potential_demand_list_text": "改造生产线"}},
        ),
        request,
    )
    assert updated["status"] == "SUCCESS"
    assert updated["data"]["draft"]["fields"]["enterprise_portrait"] == "甲公司做机器人研发"
    assert updated["data"]["draft"]["fields"]["potential_demand_list_text"] == "改造生产线"
    assert (
        _call(tools, "kc_draft_publish", DraftPublishInput(draft_id=draft_id), request)["status"]
        == "FAILED"
    )
    external = SimpleNamespace(
        tool_context=request.tool_context.model_copy(update={"user_role": "external"})
    )
    denied_get = _call(tools, "kc_draft_get", DraftIdInput(draft_id=draft_id), external)
    assert denied_get["status"] == "FAILED"
    assert denied_get["error"] == "当前暂不支持处理客户拜访记录"
    denied = _call(tools, "kc_draft_create", params, external)
    assert denied["status"] == "FAILED"
    assert denied["error"] == "当前暂不支持处理客户拜访记录"


@pytest.mark.parametrize(
    ("draft_type", "field"),
    [("demand", "application"), ("solution", "tech_type"), ("achievement", "application")],
)
def test_create_rejects_mysql_fields_outside_the_draft_contract(draft_type, field):
    with pytest.raises(ValidationError):
        DraftCreateInput.model_validate(
            {"draft_type": draft_type, "draft": {"fields": {field: "管理台完整字段"}}}
        )


@pytest.mark.parametrize("field_container", ("fields", "mysql_fields"))
def test_save_rejects_fields_from_another_type_of_owned_draft(field_container):
    tools, store, request = _setup()
    created = _call(
        tools,
        "kc_draft_create",
        DraftCreateInput(
            draft_type="demand",
            draft={"title": "需求", "fields": {"contect": "采购一批机器人"}},
        ),
        request,
    )
    draft_id = created["data"]["draft_id"]

    result = _call(
        tools,
        "kc_draft_save",
        DraftSaveInput(
            draft_id=draft_id,
            expected_version=1,
            draft={field_container: {"product_type": "技术方案专用字段"}},
        ),
        request,
    )

    assert result["status"] == "FAILED"
    assert store.get(draft_id, user_id="user-1")["draft"]["fields"]["contect"] == "采购一批机器人"


def test_publish_uses_existing_service_and_requires_owner():
    class Retrieval:
        async def publish_document(self, _draft_id, _payload):
            return SimpleNamespace(
                status="SUCCESS",
                indexed_count=1,
                model_dump=lambda: {"status": "SUCCESS", "indexed_count": 1},
            )

    records = []
    sink = SimpleNamespace(
        save=lambda payload: records.append(payload) or {"business_record_id": "mysql-1"}
    )
    tools, store, request = _setup(retrieval=Retrieval(), sink=sink)
    draft_id = store.create(
        user_id="user-1",
        user_role="external",
        thread_id=None,
        draft={"draft_type": "solution", "title": "方案", "fields": {"contect": "工业方案"}},
    )["draft_id"]
    other = SimpleNamespace(
        tool_context=request.tool_context.model_copy(update={"user_id": "user-2"})
    )
    assert (
        _call(tools, "kc_draft_publish", DraftPublishInput(draft_id=draft_id), other)["status"]
        == "FAILED"
    )
    assert not records
    result = _call(tools, "kc_draft_publish", DraftPublishInput(draft_id=draft_id), request)
    assert result["status"] == "SUCCESS"
    assert store.get(draft_id, user_id="user-1")["status"] == "PUBLISHED"
    assert len(records) == 1
    assert (
        _call(tools, "kc_draft_delete", DraftIdInput(draft_id=draft_id), request)["status"]
        == "FAILED"
    )


def test_unpublish_removes_index_before_status_change():
    events = []

    class Retrieval:
        async def unpublish_document(self, draft_id, _payload):
            events.append(("unpublish", draft_id))
            return SimpleNamespace(status="SUCCESS", indexed_count=1)

    tools, store, request = _setup(retrieval=Retrieval())
    draft_id = store.create(
        user_id="user-1",
        user_role="external",
        thread_id=None,
        draft={"draft_type": "achievement", "title": "成果", "fields": {"contect": "技术成果"}},
    )["draft_id"]
    store.transition(
        draft_id,
        user_id="user-1",
        target_status="PUBLISHED",
        operator_id="user-1",
        operator_role="external",
    )
    result = _call(
        tools,
        "kc_draft_unpublish",
        DraftUnpublishInput(
            draft_id=draft_id,
            reason_code="OUTDATED",
        ),
        request,
    )
    assert result["status"] == "SUCCESS"
    assert events == [("unpublish", draft_id)]
    assert store.get(draft_id, user_id="user-1")["status"] == "WITHDRAWN"


def test_repeating_unpublish_returns_completed_state_without_removing_index_again():
    events = []

    class Retrieval:
        async def unpublish_document(self, draft_id, _payload):
            events.append(draft_id)
            return SimpleNamespace(status="SUCCESS", indexed_count=2)

    tools, store, request = _setup(retrieval=Retrieval())
    draft_id = store.create(
        user_id="user-1",
        user_role="external",
        thread_id=None,
        draft={"draft_type": "achievement", "title": "Result"},
    )["draft_id"]
    store.transition(
        draft_id,
        user_id="user-1",
        target_status="PUBLISHED",
        operator_id="user-1",
        operator_role="external",
    )
    params = DraftUnpublishInput(draft_id=draft_id, reason_code="OUTDATED")

    first = _call(tools, "kc_draft_unpublish", params, request)
    version_after_withdrawal = first["data"]["draft"]["version"]
    repeated = _call(tools, "kc_draft_unpublish", params, request)

    assert repeated["status"] == "SUCCESS"
    assert repeated["data"]["draft"]["status"] == "WITHDRAWN"
    assert repeated["data"]["draft"]["version"] == version_after_withdrawal
    assert repeated["data"]["index_result"] == {
        "status": "UNPUBLISHED",
        "document_id": draft_id,
        "updated_count": 0,
    }
    assert events == [draft_id]


def test_unpublish_failure_keeps_draft_published():
    class Retrieval:
        async def unpublish_document(self, _draft_id, _payload):
            return SimpleNamespace(status="FAILED", error="index down", indexed_count=0)

    tools, store, request = _setup(retrieval=Retrieval())
    draft_id = store.create(
        user_id="user-1",
        user_role="external",
        thread_id=None,
        draft={"draft_type": "demand", "fields": {"contect": "需求"}},
    )["draft_id"]
    store.transition(
        draft_id,
        user_id="user-1",
        target_status="PUBLISHED",
        operator_id="user-1",
        operator_role="external",
    )
    result = _call(
        tools,
        "kc_draft_unpublish",
        DraftUnpublishInput(
            draft_id=draft_id,
            reason_code="OUTDATED",
        ),
        request,
    )
    assert result["status"] == "FAILED"
    assert result["data"] == {"stage": "unpublish", "status_code": 502}
    assert store.get(draft_id, user_id="user-1")["status"] == "PUBLISHED"


def test_mcp_schema_exposes_draft_inputs_without_identity_or_context():
    mcp = FastMCP("draft-test")
    store = InMemoryDraftStore()
    runtime = SimpleNamespace(require_draft_store=lambda: store)
    register_draft_tools(
        mcp,
        runtime,
        ToolAnnotations(readOnlyHint=True),
        lambda context: context,
        lambda _runtime, _request: ToolContext(
            user_id="user-1",
            user_role="external",
            trace_id="trace-1",
        ),
        lambda context, message: {
            "status": "FAILED",
            "data": {},
            "error": message,
            "trace_id": context.trace_id,
        },
    )
    schemas = {tool.name: tool.inputSchema for tool in asyncio.run(mcp.list_tools())}
    assert set(schemas) == {
        "kc_draft_create",
        "kc_draft_get",
        "kc_draft_list",
        "kc_draft_save",
        "kc_draft_publish",
        "kc_draft_unpublish",
        "kc_draft_delete",
    }
    for schema in schemas.values():
        assert "ctx" not in schema.get("properties", {})
        assert "user_id" not in schema.get("properties", {})
        assert "user_role" not in schema.get("properties", {})

    create_schema = schemas["kc_draft_create"]
    save_schema = schemas["kc_draft_save"]
    for schema in (create_schema, save_schema):
        definitions = schema["$defs"]
        properties = definitions["DraftContent"]["properties"]
        for field_name in ("fields", "mysql_fields"):
            field_refs = properties[field_name]["anyOf"]
            field_definitions = {
                ref["$ref"].rsplit("/", 1)[-1]: definitions[ref["$ref"].rsplit("/", 1)[-1]]
                for ref in field_refs
                if "$ref" in ref
            }
            for draft_type, model in _DRAFT_FIELD_MODELS.items():
                field_schema = field_definitions[model.__name__]
                assert set(field_schema["properties"]) == set(draft_fields(draft_type))
                assert field_schema["additionalProperties"] is False


def test_other_draft_types_cannot_be_read_through_draft_tools():
    tools, store, request = _setup()
    draft_id = store.create(
        user_id="user-1",
        user_role="external",
        thread_id=None,
        draft={"draft_type": "customer_visit", "fields": {"company": "企业"}},
    )["draft_id"]
    assert (
        _call(tools, "kc_draft_get", DraftIdInput(draft_id=draft_id), request)["status"] == "FAILED"
    )


def test_repeating_delete_does_not_write_the_tombstone_again():
    tools, store, request = _setup()
    draft_id = store.create(
        user_id="user-1",
        user_role="external",
        thread_id="thread-1",
        draft={"draft_type": "demand", "title": "Need"},
    )["draft_id"]

    first = _call(tools, "kc_draft_delete", DraftIdInput(draft_id=draft_id), request)
    version_after_delete = first["data"]["draft"]["version"]
    repeated = _call(tools, "kc_draft_delete", DraftIdInput(draft_id=draft_id), request)

    assert repeated["status"] == "SUCCESS"
    assert repeated["data"]["status"] == "DELETED"
    assert repeated["data"]["draft"]["version"] == version_after_delete
    assert store.get(draft_id, user_id="user-1")["version"] == version_after_delete
