import asyncio
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest

from runtime.workflows.file_workflow import FileWorkflowService
from runtime.workflows.task_store import SupervisorTaskStore


class _FailingAdapter:
    async def parse_document(self, _arguments):
        raise RuntimeError("/srv/mineru/private/input.pdf: dependency unavailable")


def test_parse_failure_does_not_persist_exception_details(tmp_path):
    store = SupervisorTaskStore(tmp_path / "tasks.json")
    task = store.create(
        operation="file_ingestion",
        task_type="document_ingestion",
        status="UPLOADED",
        document_id="doc-1",
        storage_key="uploads/input.pdf",
        file_name="input.pdf",
    )
    workflow = FileWorkflowService(_FailingAdapter(), store)

    result = asyncio.run(workflow.parse(task["task_id"]))

    assert result["error_code"] == "PARSE_FAILED"
    assert result["error"] == "文件解析失败，请稍后重试"
    assert store.get(task["task_id"])["error"] == "文件解析失败，请稍后重试"


def test_upload_failure_does_not_return_exception_details(monkeypatch):
    from starlette.datastructures import UploadFile

    import agents.perception_agent.routes as routes

    class _Storage:
        async def find_by_content_hash(self, *_args, **_kwargs):
            return []

        async def upload_path(self, *_args, **_kwargs):
            raise RuntimeError("C:\\private\\minio\\credentials.json leaked")

    async def _stage(*_args, **_kwargs):
        return SimpleNamespace(path=Path("input.txt"), size=1, content_hash="hash")

    monkeypatch.setattr(routes, "_storage", lambda: _Storage())
    monkeypatch.setattr(routes, "_stage_upload", _stage)

    result = asyncio.run(routes.upload_document(
        UploadFile(filename="input.txt", file=BytesIO(b"content")),
        scope_id="public",
        user_id="user-1",
    ))

    assert result.error == "文件上传失败，请稍后重试"


def test_document_limits_reject_files_over_50mb(tmp_path):
    import agents.perception_agent.routes as routes

    with pytest.raises(routes.DocumentLimitError, match="50MB"):
        routes.validate_document_limits(
            tmp_path / "large.pdf", 50 * 1024 * 1024 + 1,
        )


def test_document_limits_reject_pdf_over_50_pages(monkeypatch, tmp_path):
    import agents.perception_agent.routes as routes

    monkeypatch.setattr(routes, "_document_page_count", lambda *_args: 51)

    with pytest.raises(routes.DocumentLimitError, match="50页"):
        routes.validate_document_limits(tmp_path / "large.pdf", 1)
