import asyncio
from io import BytesIO
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.datastructures import UploadFile

from agents.perception_agent import routes
from agents.perception_agent.schemas import PerceptionRequest, PerceptionResult
from runtime.workflows.file_workflow import FileWorkflowService
from runtime.workflows.task_store import SupervisorTaskStore


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(FileWorkflowService, "_audit", lambda *args, **kwargs: None)


@pytest.fixture
def tasks(tmp_path, monkeypatch):
    store = SupervisorTaskStore(tmp_path / "format-tasks.json")
    monkeypatch.setattr(routes, "_context", lambda: SimpleNamespace(task_store=store))
    monkeypatch.setattr(routes, "task_store", store)
    return store


@pytest.mark.parametrize(
    "extension, target",
    [
        ("doc", ".docx"),
        ("ppt", ".pptx"),
        ("xls", ".xlsx"),
        ("DOC", ".docx"),
        ("zip", "解压"),
        ("unknown", "PDF"),
        ("", "PDF"),
    ],
)
def test_upload_rejects_unsupported_types_before_storing(extension, target, monkeypatch):
    def unexpected_storage():
        pytest.fail("unsupported uploads must not reach MinIO")

    monkeypatch.setattr(routes, "_storage", unexpected_storage)
    name = f"input.{extension}" if extension else "input"
    result = asyncio.run(
        routes.upload_document(
            UploadFile(filename=name, file=BytesIO(b"content")),
            scope_id="public",
            user_id="admin",
        )
    )
    assert result.status == "FAILED"
    assert result.error_code == "UNSUPPORTED_FILE_TYPE"
    assert target in result.error
    assert not result.storage_key


def test_invalid_filename_is_not_misreported_as_unsupported_format():
    result = asyncio.run(
        routes.upload_document(
            UploadFile(filename="../input.docx", file=BytesIO(b"content")),
            scope_id="public",
            user_id="admin",
        )
    )
    assert result.status == "FAILED"
    assert result.error_code == "VALIDATION_ERROR"
    assert "文件名" in result.error


@pytest.mark.parametrize("entry", ["path", "upload", "stored"])
def test_parse_entries_and_polling_preserve_format_error(entry, tmp_path, tasks, monkeypatch):
    class Storage:
        async def download_file_to_directory(self, _key, directory, _limit):
            path = directory / "input.doc"
            path.write_bytes(b"old document")
            return path, "application/msword", path.name, path.stat().st_size

    monkeypatch.setattr(routes, "_storage", lambda: Storage())
    path = tmp_path / "input.doc"
    path.write_bytes(b"old document")
    if entry == "path":
        result = asyncio.run(routes.parse_document(PerceptionRequest(file_path=str(path))))
    elif entry == "upload":
        result = asyncio.run(
            routes.parse_uploaded_document(
                UploadFile(filename=path.name, file=BytesIO(b"old document")),
                "doc-1",
                None,
                "",
            )
        )
    else:
        result = asyncio.run(
            routes.parse_stored_document("doc-1", "uploads/input.doc", "input.doc")
        )
    assert result.error_code == "UNSUPPORTED_FILE_TYPE"
    assert ".docx" in result.error
    persisted = tasks.get(result.task_id)
    assert persisted["status"] == "FAILED"
    assert persisted["error_code"] == "UNSUPPORTED_FILE_TYPE"


def test_async_parse_preserves_format_error(tasks):
    task = tasks.create(operation="parse_stored", status="QUEUED")

    async def action():
        return PerceptionResult(
            status="FAILED",
            source_name="input.doc",
            error="请转换为 .docx",
            error_code="UNSUPPORTED_FILE_TYPE",
        )

    result = asyncio.run(routes._run_async_parse(task["task_id"], "parse_stored", action()))
    assert result["status"] == "FAILED"
    assert result["error_code"] == "UNSUPPORTED_FILE_TYPE"


def test_admin_upload_returns_failed_task_with_error_code(tasks):
    from admin.api.v1.admin import build_admin_router
    from admin.service import AdminService

    class Adapter:
        async def upload_document(self, arguments):
            return (
                await routes.upload_document(
                    arguments["file"],
                    scope_id=arguments["scope_id"],
                    user_id=arguments["user_id"],
                )
            ).model_dump(mode="json")

    workflow = FileWorkflowService(Adapter(), tasks)
    service = AdminService.__new__(AdminService)
    service.runtime = SimpleNamespace(require_file_workflow=lambda: workflow)
    app = FastAPI()
    app.include_router(build_admin_router(SimpleNamespace(require_admin=lambda: service)))
    response = TestClient(app).post(
        "/api/v1/admin/files/upload", files={"file": ("input.xls", b"old workbook")}
    )
    assert response.status_code == 202
    result = response.json()
    assert result["status"] == "FAILED"
    assert result["error_code"] == "UNSUPPORTED_FILE_TYPE"
    assert ".xlsx" in result["error"]
    assert tasks.get(result["task_id"])["error_code"] == "UNSUPPORTED_FILE_TYPE"


def test_workflow_does_not_replace_specific_parse_error_with_parse_failed(tasks):
    class Adapter:
        async def parse_document(self, _arguments):
            return {
                "status": "FAILED",
                "error_code": "UNSUPPORTED_FILE_TYPE",
                "error": "请转换为 .docx",
            }

    workflow = FileWorkflowService(Adapter(), tasks)
    task = tasks.create(
        operation="file_ingestion",
        status="UPLOADED",
        document_id="doc-1",
        storage_key="uploads/input.doc",
        file_name="input.doc",
    )
    result = asyncio.run(workflow.parse(task["task_id"]))
    assert result["error_code"] == "UNSUPPORTED_FILE_TYPE"


@pytest.mark.parametrize("streaming", [False, True])
def test_chat_rest_and_sse_report_the_same_format_error(streaming, monkeypatch):
    from assistant.api.v1.chat.routes import build_chat_router
    from main import register_error_handlers

    monkeypatch.delenv("DATABASE_URL", raising=False)

    class Perception:
        async def upload_document(self, **kwargs):
            return await routes.upload_document(**kwargs)

    runtime = SimpleNamespace(
        embedded_perception=Perception(),
        deerflow_runner=object(),
        require_conversation=lambda: SimpleNamespace(session_store=None),
    )
    app = FastAPI()
    register_error_handlers(app)

    @app.middleware("http")
    async def identity(request: Request, call_next):
        request.state.user_id = "user-1"
        return await call_next(request)

    app.include_router(build_chat_router(runtime, lambda user: (user, "external", None)))
    path = "/api/v1/chat/materials/parse" + ("/stream" if streaming else "")
    response = TestClient(app).post(
        path, data={"intent_hint": "solution"}, files={"file": ("input.doc", b"old document")}
    )
    if streaming:
        import json

        errors = [
            json.loads(part.split("data: ", 1)[1])
            for part in response.text.split("\n\n")
            if part.startswith("event: error\n")
        ]
        assert response.status_code == 200
        assert len(errors) == 1
        assert errors[0]["code"] == 415
        assert errors[0]["error_code"] == "UNSUPPORTED_FILE_TYPE"
        assert errors[0]["stage"] == "upload"
        assert ".docx" in errors[0]["message"]
        assert "event: done" not in response.text
    else:
        assert response.status_code == 415
        assert response.json()["error"]["code"] == "UNSUPPORTED_FILE_TYPE"
        assert ".docx" in response.json()["error"]["message"]
