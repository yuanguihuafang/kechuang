import asyncio
import logging
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from admin.api.v1.admin import build_admin_router
from admin.service import AdminService


@pytest.fixture(autouse=True)
def _isolate_database_url(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)


class _TaskStore:
    def __init__(self, task):
        self.task = dict(task)

    def update(self, _task_id, **fields):
        self.task.update(fields)
        return dict(self.task)

    def get(self, _task_id):
        return dict(self.task)


class _TaskRunner:
    def __init__(self, store):
        self.store = store
        self.submissions = []

    def submit(self, task_id, operation, work, **kwargs):
        self.submissions.append((task_id, operation, work, kwargs))
        self.store.update(task_id, status="QUEUED")
        return True


@pytest.mark.parametrize("status", ["WAITING_CONFIRM", "WAITING_DRAFT_CONFIRM", "SUCCEEDED", "FAILED"])
def test_parse_again_resets_completed_parse_state_and_submits_new_run(status):
    task = {
        "task_id": "task-1",
        "status": status,
        "storage_key": "source.pdf",
        "preview_blocks": [{"text": "old"}],
        "block_count": 1,
        "draft_status": "READY",
        "draft_preview": {"title": "old draft"},
        "draft_previews": [{"title": "old draft"}],
        "draft_error_code": "LLM_TIMEOUT",
        "draft_error": "旧错误",
        "error": "old error",
    }
    store = _TaskStore(task)
    runner = _TaskRunner(store)
    workflow = SimpleNamespace(status=lambda _task_id: dict(store.task), task_store=store)
    runtime = SimpleNamespace(require_file_workflow=lambda: workflow, task_runner=runner)
    service = AdminService.__new__(AdminService)
    service.runtime = runtime

    result = asyncio.run(service.parse_task("task-1"))

    assert len(runner.submissions) == 1
    assert runner.submissions[0][1] == "parse"
    assert result["status"] == "QUEUED"
    assert store.task["operation_state"] == "STORAGE_READY"
    assert store.task["preview_blocks"] is None
    assert store.task["block_count"] is None
    assert store.task["draft_preview"] is None
    assert store.task["draft_previews"] == []
    assert store.task["draft_error_code"] is None
    assert store.task["draft_error"] is None
    assert store.task["error"] is None


def test_parse_again_is_blocked_while_field_extraction_is_running():
    task = {
        "task_id": "task-1",
        "status": "WAITING_CONFIRM",
        "storage_key": "source.pdf",
        "draft_status": "RUNNING",
        "draft_preview": None,
    }
    store = _TaskStore(task)
    runner = _TaskRunner(store)
    workflow = SimpleNamespace(status=lambda _task_id: dict(store.task), task_store=store)
    service = AdminService.__new__(AdminService)
    service.runtime = SimpleNamespace(
        require_file_workflow=lambda: workflow,
        task_runner=runner,
    )

    with pytest.raises(ValueError, match="字段提取进行中"):
        asyncio.run(service.parse_task("task-1"))

    assert runner.submissions == []
    assert store.task["status"] == "WAITING_CONFIRM"
    assert store.task["draft_status"] == "RUNNING"


def test_parse_again_returns_conflict_while_field_extraction_is_running():
    class _Admin:
        async def parse_task(self, _task_id):
            raise ValueError("字段提取进行中，暂不能重新解析")

    app = FastAPI()
    app.include_router(build_admin_router(SimpleNamespace(require_admin=lambda: _Admin())))

    response = TestClient(app).post("/api/v1/admin/tasks/task-1/parse")

    assert response.status_code == 409
    assert response.json()["detail"] == "字段提取进行中，暂不能重新解析"


@pytest.mark.parametrize("status", ["WAITING_DRAFT_CONFIRM", "SUCCEEDED"])
def test_field_extraction_can_run_again_after_completion_but_not_concurrently(status):
    task = {
        "task_id": "task-1",
        "status": status,
        "knowledge_type": "solution",
        "draft_status": "READY",
        "draft_id": "old-id",
        "draft_ids": ["old-id"],
        "draft_preview": {"title": "old draft"},
        "draft_previews": [{"title": "old draft"}],
        "draft_error_code": "LLM_TIMEOUT",
        "draft_error": "旧错误",
    }
    store = _TaskStore(task)
    calls = []
    release_first = asyncio.Event()

    async def regenerate(_task_id):
        calls.append(_task_id)
        if len(calls) == 1:
            await release_first.wait()
        await asyncio.sleep(0)
        store.update(
            _task_id,
            draft_status="READY",
            draft_id=f"new-{len(calls)}",
            draft_ids=[f"new-{len(calls)}"],
        )

    workflow = SimpleNamespace(
        status=lambda _task_id: dict(store.task),
        validate_task_source=lambda _task: None,
        task_store=store,
        regenerate_draft=regenerate,
    )
    service = AdminService.__new__(AdminService)
    service.runtime = SimpleNamespace(require_file_workflow=lambda: workflow)

    async def run_twice():
        await service.extract_fields_task("task-1")
        await service.extract_fields_task("task-1")
        await asyncio.sleep(0)
        assert calls == ["task-1"]
        assert store.task["draft_id"] is None
        assert store.task["draft_preview"] is None
        assert store.task["draft_error_code"] is None
        assert store.task["draft_error"] is None
        release_first.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await service.extract_fields_task("task-1")
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(run_twice())

    assert calls == ["task-1", "task-1"]
    assert store.task["draft_id"] == "new-2"


def test_field_extraction_uses_selected_knowledge_type_and_clears_stale_failure():
    task = {
        "task_id": "task-1",
        "status": "WAITING_DRAFT_CONFIRM",
        "knowledge_type": None,
        "draft_status": "FAILED",
        "error_code": "DRAFT_NOT_CREATED",
        "error": "字段提取未生成可确认草稿",
    }
    store = _TaskStore(task)
    seen = []

    async def regenerate(task_id):
        seen.append(dict(store.task))

    workflow = SimpleNamespace(
        status=lambda _task_id: dict(store.task),
        validate_task_source=lambda _task: None,
        task_store=store,
        regenerate_draft=regenerate,
    )
    service = AdminService.__new__(AdminService)
    service.runtime = SimpleNamespace(require_file_workflow=lambda: workflow)

    result = asyncio.run(service.extract_fields_task("task-1", "solution"))

    assert result["draft_status"] == "RUNNING"
    assert store.task["knowledge_type"] == "solution"
    assert store.task["error_code"] is None
    assert store.task["error"] is None
    assert seen[0]["knowledge_type"] == "solution"


def test_field_extraction_without_knowledge_type_keeps_draft_empty_without_failure():
    task = {
        "task_id": "task-1",
        "status": "WAITING_DRAFT_CONFIRM",
        "knowledge_type": None,
        "draft_status": "NOT_STARTED",
        "error_code": "DRAFT_NOT_CREATED",
        "error": "字段提取未生成可确认草稿",
    }
    store = _TaskStore(task)
    calls = []
    workflow = SimpleNamespace(
        status=lambda _task_id: dict(store.task),
        validate_task_source=lambda _task: None,
        task_store=store,
        regenerate_draft=lambda _task_id: calls.append(_task_id),
    )
    service = AdminService.__new__(AdminService)
    service.runtime = SimpleNamespace(require_file_workflow=lambda: workflow)

    result = asyncio.run(service.extract_fields_task("task-1"))

    assert result["draft_status"] == "EMPTY"
    assert store.task["knowledge_type"] is None
    assert store.task["draft_status"] == "EMPTY"
    assert store.task["draft_error_code"] is None
    assert store.task["error_code"] is None
    assert store.task["error"] is None
    assert calls == []


def test_missing_knowledge_type_does_not_clear_an_active_field_extraction():
    task = {
        "task_id": "task-1",
        "status": "WAITING_DRAFT_CONFIRM",
        "knowledge_type": None,
        "draft_status": "RUNNING",
    }
    store = _TaskStore(task)
    workflow = SimpleNamespace(
        status=lambda _task_id: dict(store.task),
        validate_task_source=lambda _task: None,
        task_store=store,
        regenerate_draft=lambda _task_id: None,
    )
    service = AdminService.__new__(AdminService)
    service.runtime = SimpleNamespace(require_file_workflow=lambda: workflow)

    result = asyncio.run(service.extract_fields_task("task-1"))

    assert result["draft_status"] == "RUNNING"
    assert store.task["draft_status"] == "RUNNING"


def test_explicit_draft_regeneration_does_not_reuse_ready_draft():
    from runtime.workflows.file_workflow import FileWorkflowService

    class _DraftBuilder:
        def __init__(self):
            self.calls = 0

        async def build_task_fields(self, *_args):
            self.calls += 1
            return {"draft_id": f"new-{self.calls}", "draft_preview": {"title": "new"}}

    store = _TaskStore({"task_id": "task-1", "status": "WAITING_DRAFT_CONFIRM"})
    builder = _DraftBuilder()
    workflow = object.__new__(FileWorkflowService)
    workflow.lifecycle_store = None
    workflow.task_store = store
    workflow.structured_draft = builder
    workflow.knowledge_draft = builder

    asyncio.run(
        workflow._build_draft_in_background(
            "task-1",
            {
                "task_id": "task-1",
                "knowledge_type": "solution",
                "draft_status": "READY",
                "draft_preview": {"title": "old"},
            },
            {"raw_text": "source"},
            "source",
        )
    )

    assert builder.calls == 1
    assert store.task["draft_id"] == "new-1"


def test_draft_provider_timeout_is_saved_as_readable_failure_and_audited(caplog):
    from runtime.workflows.file_workflow import FileWorkflowService

    class _DraftBuilder:
        async def build_task_fields(self, *_args):
            raise httpx.ReadTimeout("provider timed out")

    store = _TaskStore({"task_id": "task-1", "status": "WAITING_DRAFT_CONFIRM"})
    audit_events = []
    workflow = object.__new__(FileWorkflowService)
    workflow.lifecycle_store = None
    workflow.task_store = store
    workflow.structured_draft = _DraftBuilder()
    workflow.knowledge_draft = workflow.structured_draft
    workflow._audit = lambda event_name, **fields: audit_events.append((event_name, fields))
    workflow._audit_context = lambda _task: {}

    with caplog.at_level(logging.ERROR, logger="runtime.workflows.file_workflow"):
        asyncio.run(
            workflow._build_draft_in_background(
                "task-1", {"task_id": "task-1", "knowledge_type": "solution"}, {}, "source"
            )
        )

    assert store.task["draft_status"] == "FAILED"
    assert store.task["draft_error_code"] == "LLM_TIMEOUT"
    assert "超时" in store.task["draft_error"]
    assert "ReadTimeout: provider timed out" in audit_events[0][1]["error"]
    assert "Traceback" in audit_events[0][1]["error"]
    assert "task-1" in caplog.text


def test_empty_field_extraction_has_a_specific_failure_and_audit_event():
    from runtime.workflows.file_workflow import FileWorkflowService

    class _DraftBuilder:
        async def build_task_fields(self, *_args):
            return None

    store = _TaskStore({"task_id": "task-1", "status": "WAITING_DRAFT_CONFIRM"})
    audit_events = []
    workflow = object.__new__(FileWorkflowService)
    workflow.lifecycle_store = None
    workflow.task_store = store
    workflow.structured_draft = _DraftBuilder()
    workflow.knowledge_draft = workflow.structured_draft
    workflow._audit = lambda event_name, **fields: audit_events.append((event_name, fields))
    workflow._audit_context = lambda _task: {}

    asyncio.run(
        workflow._build_draft_in_background(
            "task-1", {"task_id": "task-1", "knowledge_type": "solution"}, {}, "source"
        )
    )

    assert store.task["draft_status"] == "FAILED"
    assert store.task["draft_error_code"] == "DRAFT_NOT_CREATED"
    assert store.task["draft_error"]
    assert audit_events[0][1]["error_code"] == "DRAFT_NOT_CREATED"


def test_provider_http_failure_is_classified_separately():
    from runtime.workflows.file_workflow import _draft_failure

    request = httpx.Request("POST", "https://model.example.test/chat/completions")
    response = httpx.Response(503, request=request)

    assert _draft_failure(httpx.HTTPStatusError("503 unavailable", request=request, response=response)) == (
        "LLM_PROVIDER_ERROR",
        "模型服务返回错误，请查看任务审计记录后重试。",
    )


@pytest.mark.parametrize("failure, expected_code", [
    (httpx.ReadTimeout("timeout evidence"), "LLM_TIMEOUT"),
    (ValueError("DRAFT_FIELDS_EMPTY"), "DRAFT_FIELDS_EMPTY"),
])
def test_real_admin_extraction_failure_is_audited_without_creating_blank_drafts(monkeypatch, failure, expected_code):
    from runtime.workflows.file_workflow import FileWorkflowService
    from runtime.workflows.knowledge_document_draft import KnowledgeDocumentDraftService

    class Reviewer:
        async def complete(self, _messages):
            if isinstance(failure, ValueError):
                return '{}'
            raise failure

    monkeypatch.setenv("DRAFT_LLM_REVIEW_ENABLED", "true")
    monkeypatch.setenv("DRAFT_LLM_CACHE_ENABLED", "false")
    monkeypatch.setattr("agents.perception_agent.application.field_extraction._build_draft_reviewer", lambda _: Reviewer())
    draft_store = SimpleNamespace(create_many=lambda **kw: pytest.fail("must not persist an empty draft"))
    task = {"task_id": "task-1", "status": "WAITING_CONFIRM", "knowledge_type": "achievement", "user_role": "admin", "user_id": "admin"}
    store = _TaskStore(task)
    audit = []
    workflow = object.__new__(FileWorkflowService)
    workflow.lifecycle_store = None
    workflow.task_store = store
    workflow.knowledge_draft = KnowledgeDocumentDraftService(draft_store)
    workflow.structured_draft = None
    workflow._audit = lambda event, **fields: audit.append(fields)
    workflow._audit_context = lambda _: {}
    asyncio.run(workflow._build_draft_in_background("task-1", task, {"raw_text": "科研成果资料"}, ""))
    assert store.task["draft_status"] == "FAILED"
    assert store.task["draft_error_code"] == expected_code
    assert store.task["status"] == "WAITING_CONFIRM"
    assert "Traceback" in audit[0]["error"]


def test_task_response_exposes_draft_failure_fields():
    from core.contracts.workflows import SupervisorTaskResponse

    response = SupervisorTaskResponse.model_validate(
        {
            "task_id": "task-1",
            "status": "WAITING_DRAFT_CONFIRM",
            "draft_status": "FAILED",
            "draft_error_code": "LLM_TIMEOUT",
            "draft_error": "模型服务响应超时，请稍后重试。",
        }
    )

    assert response.draft_error_code == "LLM_TIMEOUT"
    assert "超时" in response.draft_error


def test_admin_confirmation_replaces_saved_attachment_text_with_edited_blocks(monkeypatch):
    from core.contracts.workflows import FileConfirmRequest

    old_block = {"block_id": "page-1", "text": "旧原文"}
    edited_block = {"block_id": "page-1", "text": "管理员修改后的原文"}
    saved = []
    existing = {
        "draft_id": "draft-1",
        "status": "CONFIRMED",
        "draft": {"draft_type": "solution", "source_blocks": [old_block]},
    }

    class _DraftStore:
        def get(self, _draft_id, *, user_id):
            assert user_id == "admin"
            return existing

        def update(self, _draft_id, **kwargs):
            saved.append(kwargs)
            return {**existing, "draft": kwargs["draft"]}

    class _Publisher:
        def __init__(self, *_args):
            pass

        async def publish(self, _draft_id, _payload):
            return {
                "draft": {"draft": {"draft_type": "solution", "source_blocks": [edited_block]}},
                "index_result": {"indexed_count": 1},
            }

    monkeypatch.setattr("admin.documents.DraftPublishingApplicationService", _Publisher)
    task = {
        "task_id": "task-1",
        "status": "WAITING_DRAFT_CONFIRM",
        "user_id": "admin",
        "knowledge_type": "solution",
        "draft_ids": ["draft-1"],
    }
    task_store = _TaskStore(task)
    workflow = SimpleNamespace(
        status=lambda _task_id: dict(task),
        validate_task_source=lambda _task: None,
        task_store=task_store,
        cleanup_previous_version=lambda _task: asyncio.sleep(0, result={}),
    )
    service = AdminService.__new__(AdminService)
    service.runtime = SimpleNamespace(
        require_file_workflow=lambda: workflow,
        require_draft_store=lambda: _DraftStore(),
    )

    asyncio.run(
        service.confirm_task(
            "task-1",
            FileConfirmRequest(
                chunks=[edited_block],
                drafts=[{"draft_type": "solution", "fields": {"title": "方案"}}],
            ),
        )
    )

    assert saved[0]["draft"]["source_blocks"][0]["text"] == edited_block["text"]
    assert saved[0]["draft"]["source_blocks"][0]["metadata"]["chunks_are_final"] is True
    assert saved[0]["replace_source_blocks"] is True


def test_admin_confirmation_prefers_saved_edited_chunks_over_stale_draft_blocks(monkeypatch):
    from core.contracts.workflows import FileConfirmRequest

    stale_block = {"block_id": "page-1", "text": "旧附件内容"}
    edited_block = {"block_id": "chunk-1", "text": "已保存的编辑分块"}
    saved = []
    existing = {
        "draft_id": "draft-1",
        "status": "CONFIRMED",
        "draft": {"draft_type": "solution", "source_blocks": [stale_block]},
    }

    class _DraftStore:
        def get(self, _draft_id, *, user_id):
            assert user_id == "admin"
            return existing

        def update(self, _draft_id, **kwargs):
            saved.append(kwargs)
            return {**existing, "draft": kwargs["draft"]}

    class _Publisher:
        def __init__(self, *_args):
            pass

        async def publish(self, _draft_id, _payload):
            return {"draft": {"draft": existing["draft"]}, "index_result": {"indexed_count": 1}}

    async def _parse_result(_document_id):
        return {"edited": True, "raw_text": "管理员修改后的原文", "saved_chunks": [edited_block]}

    monkeypatch.setattr("admin.documents.DraftPublishingApplicationService", _Publisher)
    task = {
        "task_id": "task-1",
        "status": "WAITING_DRAFT_CONFIRM",
        "user_id": "admin",
        "knowledge_type": "solution",
        "document_id": "doc-1",
        "draft_ids": ["draft-1"],
    }
    workflow = SimpleNamespace(
        status=lambda _task_id: dict(task),
        validate_task_source=lambda _task: None,
        task_store=_TaskStore(task),
        cleanup_previous_version=lambda _task: asyncio.sleep(0, result={}),
    )
    service = AdminService.__new__(AdminService)
    service.runtime = SimpleNamespace(
        require_file_workflow=lambda: workflow,
        require_draft_store=lambda: _DraftStore(),
    )
    service.parse_result = _parse_result

    asyncio.run(
        service.confirm_task(
            "task-1",
            FileConfirmRequest(drafts=[{"draft_type": "solution", "fields": {"title": "方案"}}]),
        )
    )

    source_blocks = saved[0]["draft"]["source_blocks"]
    assert [block["text"] for block in source_blocks] == ["已保存的编辑分块"]
    assert source_blocks[0]["metadata"]["chunks_are_final"] is True
    assert saved[0]["replace_source_blocks"] is True


def test_admin_confirmation_uses_edited_parse_text_when_no_chunks_were_generated(monkeypatch):
    from core.contracts.workflows import FileConfirmRequest

    stale_block = {"block_id": "page-1", "text": "旧解析内容"}
    saved = []
    existing = {"draft_id": "draft-1", "status": "CONFIRMED", "draft": {"draft_type": "solution"}}

    class _DraftStore:
        def get(self, _draft_id, *, user_id):
            return existing

        def update(self, _draft_id, **kwargs):
            saved.append(kwargs)
            return {**existing, "draft": kwargs["draft"]}

    class _Publisher:
        def __init__(self, *_args):
            pass

        async def publish(self, _draft_id, _payload):
            return {"draft": {"draft": existing["draft"]}, "index_result": {"indexed_count": 1}}

    async def _parse_result(_document_id):
        return {"edited": True, "raw_text": "只用管理员改过的原文", "blocks": [stale_block]}

    monkeypatch.setattr("admin.documents.DraftPublishingApplicationService", _Publisher)
    task = {
        "task_id": "task-1",
        "status": "WAITING_DRAFT_CONFIRM",
        "user_id": "admin",
        "knowledge_type": "solution",
        "document_id": "doc-1",
        "draft_ids": ["draft-1"],
    }
    workflow = SimpleNamespace(
        status=lambda _task_id: dict(task),
        validate_task_source=lambda _task: None,
        task_store=_TaskStore(task),
        cleanup_previous_version=lambda _task: asyncio.sleep(0, result={}),
    )
    service = AdminService.__new__(AdminService)
    service.runtime = SimpleNamespace(
        require_file_workflow=lambda: workflow,
        require_draft_store=lambda: _DraftStore(),
    )
    service.parse_result = _parse_result

    asyncio.run(
        service.confirm_task(
            "task-1",
            FileConfirmRequest(drafts=[{"draft_type": "solution", "fields": {"title": "方案"}}]),
        )
    )

    source_blocks = saved[0]["draft"]["source_blocks"]
    assert [block["text"] for block in source_blocks] == ["只用管理员改过的原文"]
    assert not source_blocks[0]["metadata"].get("chunks_are_final")


def test_republishing_multiple_admin_drafts_replaces_each_old_vector(monkeypatch):
    from core.contracts.workflows import FileConfirmRequest

    published = []
    records = {
        draft_id: {
            "draft_id": draft_id,
            "draft_type": "enterprise",
            "status": "CONFIRMED",
            "user_id": "admin",
            "draft": {"draft_type": "enterprise", "fields": {"title": "old"}},
        }
        for draft_id in ("draft-1", "draft-2")
    }

    class _DraftStore:
        def get(self, draft_id, *, user_id):
            assert user_id == "admin"
            return records[draft_id]

        def update(self, draft_id, *, draft, **_kwargs):
            records[draft_id]["draft"] = draft
            records[draft_id]["status"] = "CONFIRMED"
            return records[draft_id]

    class _Publisher:
        def __init__(self, *_args):
            pass

        async def publish(self, draft_id, payload):
            published.append((draft_id, payload.replace_existing))
            return {
                "draft": {"draft": records[draft_id]["draft"]},
                "index_result": {"indexed_count": 1},
            }

    monkeypatch.setattr("admin.documents.DraftPublishingApplicationService", _Publisher)
    task = {
        "task_id": "task-1",
        "status": "WAITING_DRAFT_CONFIRM",
        "user_id": "admin",
        "knowledge_type": "enterprise",
        "document_id": "doc-1",
        "draft_ids": ["draft-1", "draft-2"],
    }
    task_store = _TaskStore(task)
    workflow = SimpleNamespace(
        status=lambda _task_id: dict(task),
        validate_task_source=lambda _task: None,
        task_store=task_store,
        cleanup_previous_version=lambda _task: asyncio.sleep(0, result={}),
    )
    service = AdminService.__new__(AdminService)
    service.runtime = SimpleNamespace(
        require_file_workflow=lambda: workflow,
        require_draft_store=lambda: _DraftStore(),
    )
    async def _parse_result(_document_id):
        return {"blocks": [{"block_id": "page-1", "text": "附件原文"}]}

    service.parse_result = _parse_result

    asyncio.run(
        service.confirm_task(
            "task-1",
            FileConfirmRequest(
                drafts=[
                    {"draft_type": "enterprise", "fields": {"title": "new-1"}},
                    {"draft_type": "enterprise", "fields": {"title": "new-2"}},
                ],
            ),
        )
    )

    assert published == [("draft-1", True), ("draft-2", True)]
    assert [block["text"] for block in records["draft-1"]["draft"]["source_blocks"]] == ["附件原文"]
    assert records["draft-2"]["draft"]["source_blocks"] == []


def test_published_confirmation_retry_resumes_cleanup_without_republishing(monkeypatch):
    from core.contracts.workflows import FileConfirmRequest

    existing = {
        "draft_id": "draft-1",
        "draft_type": "solution",
        "status": "PUBLISHED",
        "user_id": "admin",
        "draft": {
            "draft_type": "solution",
            "source_id": "source-1",
            "mysql_record_id": 27,
            "fields": {"title": "已发布方案"},
            "source_blocks": [
                {
                    "chunk_text": "已发布附件分块",
                    "source_chunk_id": "doc-1:chunk:0",
                    "visibility": "public",
                }
            ],
        },
    }
    cleanup_calls = 0
    publish_calls = 0

    class _DraftStore:
        def get(self, _draft_id, *, user_id):
            assert user_id == "admin"
            return existing

    class _Publisher:
        def __init__(self, *_args):
            pass

        async def publish(self, *_args):
            nonlocal publish_calls
            publish_calls += 1
            raise AssertionError("published draft must not be written again")

    def _cleanup(*_args, **_kwargs):
        nonlocal cleanup_calls
        cleanup_calls += 1
        if cleanup_calls == 1:
            raise RuntimeError("simulated cleanup failure")
        return {"drafts": 1, "mysql_records": 1, "milvus_records": 1}

    monkeypatch.setattr("admin.documents.DraftPublishingApplicationService", _Publisher)
    monkeypatch.setattr("admin.documents._cleanup_superseded_source_drafts", _cleanup)
    task = {
        "task_id": "task-1",
        "status": "WAITING_DRAFT_CONFIRM",
        "user_id": "admin",
        "knowledge_type": "solution",
        "draft_ids": ["draft-1"],
    }
    task_store = _TaskStore(task)
    workflow = SimpleNamespace(
        status=lambda _task_id: dict(task),
        validate_task_source=lambda _task: None,
        task_store=task_store,
        cleanup_previous_version=lambda _task: asyncio.sleep(0, result={}),
    )
    service = AdminService.__new__(AdminService)
    service.runtime = SimpleNamespace(
        require_file_workflow=lambda: workflow,
        require_draft_store=lambda: _DraftStore(),
    )

    with pytest.raises(RuntimeError, match="simulated cleanup failure"):
        asyncio.run(
            service.confirm_task(
                "task-1",
                FileConfirmRequest(
                    chunks=[
                        {
                            "chunk_text": "已发布附件分块",
                            "source_chunk_id": "doc-1:chunk:0",
                            "visibility": "public",
                            "metadata": {"preview_confirmed": True},
                        }
                    ]
                ),
            )
        )

    result = asyncio.run(
        service.confirm_task(
            "task-1",
            FileConfirmRequest(
                chunks=[
                    {
                        "chunk_text": "已发布附件分块",
                        "source_chunk_id": "doc-1:chunk:0",
                        "visibility": "public",
                        "metadata": {"preview_confirmed": True},
                    }
                ]
            ),
        )
    )

    assert result["status"] == "SUCCEEDED"
    assert result["indexed_count"] == 0
    assert cleanup_calls == 2
    assert publish_calls == 0
