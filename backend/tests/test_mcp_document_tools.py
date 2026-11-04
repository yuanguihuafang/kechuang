"""Document MCP tools keep ownership and storage details at the backend boundary."""

import asyncio
from types import SimpleNamespace

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import ValidationError

from core.contracts.tools import ToolContext
from assistant.integrations.mcp_tools.document import (
    DocumentIdInput,
    DocumentListInput,
    register_document_tools,
)


class _MCP:
    def __init__(self):
        self.tools = {}

    def tool(self, *, name, **_kwargs):
        def register(func):
            self.tools[name] = func
            return func

        return register


class _Lifecycle:
    def __init__(self):
        self.owner_filter = None

    def list_documents(self, *, user_id, limit):
        self.owner_filter = user_id
        return [
            {
                "document_id": "doc-1",
                "source_name": "paper.pdf",
                "status": "PARSED",
                "active_version_id": "v-1",
                "storage_key": "private/source.pdf",
                "versions": [{"version_id": "v-1", "storage_key": "private/source.pdf"}],
            }
        ][:limit]

    def get_document(self, document_id, *, user_id):
        self.owner_filter = user_id
        if document_id != "doc-1" or user_id != "owner":
            return None
        return {
            "document_id": "doc-1",
            "source_name": "paper.pdf",
            "status": "PARSED",
            "active_version_id": "v-1",
            "storage_key": "private/source.pdf",
            "knowledge_type": "solution",
            "active_version": {
                "version_id": "v-1",
                "storage_key": "private/source.pdf",
                "source_name": "paper.pdf",
                "knowledge_type": "solution",
            },
            "versions": [
                {"version_id": "v-1", "status": "PARSED", "storage_key": "private/source.pdf"}
            ],
        }


class _Storage:
    def __init__(self):
        self.loaded = []
        self.result = {
            "status": "SUCCESS",
            "raw_text": "a" * 9000,
            "storage_key": "private/source.pdf",
            "metadata": {
                "parse_result_key": "private/parse.json",
                "images": [
                    {
                        "image_id": "image-1",
                        "pageNumber": 2,
                        "minio_object_key": "private/image.png",
                    },
                ],
            },
        }

    async def load_parse_result(self, document_id):
        self.loaded.append(document_id)
        return self.result


class _Adapter:
    def __init__(self):
        self.calls = []
        self.result = {"status": "SUCCESS", "storage_key": "private/source.pdf"}

    async def parse_document(self, arguments, *, context):
        self.calls.append((arguments, context))
        return self.result


def _registered():
    mcp = _MCP()
    lifecycle, storage = _Lifecycle(), _Storage()
    adapter = _Adapter()
    runtime = SimpleNamespace(
        lifecycle_service=lifecycle, storage_service=storage, tool_adapter=adapter
    )
    context = ToolContext(user_id="owner", user_role="external", trace_id="trace-1")
    register_document_tools(
        mcp,
        runtime,
        object(),
        lambda ctx: ctx.request,
        lambda _runtime, _request: context,
        lambda tool_context, message: {
            "status": "FAILED",
            "error": message,
            "trace_id": tool_context.trace_id,
        },
    )
    return mcp.tools, lifecycle, storage, adapter


def test_document_inputs_reject_identity_and_storage_key():
    with pytest.raises(ValidationError):
        DocumentIdInput.model_validate({"document_id": "doc-1", "user_id": "someone-else"})
    with pytest.raises(ValidationError):
        DocumentIdInput.model_validate({"document_id": "doc-1", "storage_key": "private/other"})
    with pytest.raises(ValidationError):
        DocumentListInput.model_validate({"limit": 10, "role": "admin"})


def test_list_documents_filters_by_resolved_owner_and_hides_storage_details():
    tools, lifecycle, _, _ = _registered()
    result = asyncio.run(
        tools["kc_document_list"](DocumentListInput(limit=10), SimpleNamespace(request=object()))
    )

    assert lifecycle.owner_filter == "owner"
    assert result["status"] == "SUCCESS"
    assert result["data"]["documents"][0]["document_id"] == "doc-1"
    assert "storage_key" not in str(result)


def test_detail_denies_other_document_before_storage_read():
    tools, lifecycle, storage, _ = _registered()
    result = asyncio.run(
        tools["kc_document_detail"](
            DocumentIdInput(document_id="doc-other"), SimpleNamespace(request=object())
        )
    )

    assert lifecycle.owner_filter == "owner"
    assert result["status"] == "FAILED"
    assert storage.loaded == []


def test_parse_result_checks_ownership_and_bounds_text_without_storage_keys():
    tools, _, storage, _ = _registered()
    result = asyncio.run(
        tools["kc_document_parse_result"](
            DocumentIdInput(document_id="doc-1"), SimpleNamespace(request=object())
        )
    )

    assert storage.loaded == ["doc-1"]
    assert result["status"] == "SUCCESS"
    assert len(result["data"]["raw_text"]) == 8000
    assert result["data"]["truncated"] is True
    assert "storage_key" not in str(result)


def test_parse_result_denies_other_document_before_storage_read():
    tools, _, storage, _ = _registered()
    result = asyncio.run(
        tools["kc_document_parse_result"](
            DocumentIdInput(document_id="doc-other"), SimpleNamespace(request=object())
        )
    )

    assert result["status"] == "FAILED"
    assert storage.loaded == []


def test_fastmcp_registers_document_tools_without_model_supplied_context():
    mcp = FastMCP("document-test")
    register_document_tools(
        mcp,
        SimpleNamespace(),
        ToolAnnotations(readOnlyHint=True),
        lambda ctx: ctx.request,
        lambda _runtime, _request: None,
        lambda _context, message: {"error": message},
    )

    tools = {tool.name: tool for tool in mcp._tool_manager.list_tools()}
    assert set(tools) == {
        "kc_document_list",
        "kc_document_detail",
        "kc_document_parse_result",
        "kc_document_parse",
    }
    assert "ctx" not in tools["kc_document_parse_result"].parameters["properties"]
    assert "storage_key" not in str(tools["kc_document_parse"].parameters)
    assert tools["kc_document_parse"].annotations.readOnlyHint is False


def test_parse_uses_only_owned_active_version_and_returns_bounded_saved_result():
    tools, lifecycle, storage, adapter = _registered()
    result = asyncio.run(
        tools["kc_document_parse"](
            DocumentIdInput(document_id="doc-1"), SimpleNamespace(request=object())
        )
    )

    assert lifecycle.owner_filter == "owner"
    assert adapter.calls[0][0] == {
        "document_id": "doc-1",
        "storage_key": "private/source.pdf",
        "file_name": "paper.pdf",
        "knowledge_type": "solution",
    }
    assert adapter.calls[0][1].user_id == "owner"
    assert storage.loaded == ["doc-1"]
    assert result["status"] == "SUCCESS"
    assert result["data"]["raw_text"] == "a" * 8000
    assert result["data"]["images"] == [{"image_id": "image-1", "page_number": 2}]
    assert result["data"]["image_count"] == 1
    assert "private/" not in str(result)


def test_parse_denies_other_document_before_adapter_or_storage():
    tools, _, storage, adapter = _registered()
    result = asyncio.run(
        tools["kc_document_parse"](
            DocumentIdInput(document_id="doc-other"), SimpleNamespace(request=object())
        )
    )

    assert result["status"] == "FAILED"
    assert adapter.calls == []
    assert storage.loaded == []


def test_parse_rejects_missing_active_version_storage_key():
    tools, lifecycle, storage, adapter = _registered()
    original = lifecycle.get_document

    def no_storage(document_id, *, user_id):
        document = original(document_id, user_id=user_id)
        document["active_version"].pop("storage_key")
        return document

    lifecycle.get_document = no_storage
    result = asyncio.run(
        tools["kc_document_parse"](
            DocumentIdInput(document_id="doc-1"), SimpleNamespace(request=object())
        )
    )

    assert result["status"] == "FAILED"
    assert adapter.calls == []
    assert storage.loaded == []


def test_parse_failure_does_not_expose_adapter_storage_key():
    tools, _, storage, adapter = _registered()
    adapter.result = {"status": "FAILED", "error": "private/source.pdf: parser failed"}
    result = asyncio.run(
        tools["kc_document_parse"](
            DocumentIdInput(document_id="doc-1"), SimpleNamespace(request=object())
        )
    )

    assert result["status"] == "FAILED"
    assert "private/" not in str(result)
    assert storage.loaded == []


def test_parse_redacts_active_storage_key_even_if_embedded_in_saved_text():
    tools, _, storage, _ = _registered()
    storage.result["raw_text"] = "source private/source.pdf end"

    result = asyncio.run(
        tools["kc_document_parse"](
            DocumentIdInput(document_id="doc-1"), SimpleNamespace(request=object())
        )
    )

    assert result["status"] == "SUCCESS"
    assert "private/source.pdf" not in str(result)
