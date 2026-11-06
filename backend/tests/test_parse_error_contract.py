import asyncio
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

import pytest

from runtime.workflows.file_workflow import FileWorkflowService
from runtime.workflows.task_store import SupervisorTaskStore


class _FailingAdapter:
    async def parse_document(self, _arguments):
        raise RuntimeError("/srv/mineru/private/input.pdf: dependency unavailable")


@pytest.mark.parametrize("version", [
    {"storage_key": "uploads/new.pdf", "status": "UPLOADED"},
    {"storage_key": "uploads/old.pdf", "status": "PURGED"},
    None,
])
def test_stale_parse_task_does_not_modify_replaced_document(tmp_path, version):
    store = SupervisorTaskStore(tmp_path / "stale-tasks.json")
    task = store.create(operation="file_ingestion", status="UPLOADED", document_id="doc",
                        version_id="doc:v1", storage_key="uploads/old.pdf")
    changed = []
    workflow = FileWorkflowService(_FailingAdapter(), store, lifecycle_store=SimpleNamespace(
        get_version=lambda _version: version,
        update_version=lambda *args, **kwargs: changed.append((args, kwargs)),
    ))
    result = asyncio.run(workflow.parse(task["task_id"]))
    assert result["error_code"] == "STALE_SOURCE"
    assert changed == []


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


@pytest.mark.parametrize("extension", ["pdf", "pptx"])
@pytest.mark.parametrize("page_count", [72, 201])
def test_document_limits_accept_documents_over_50_pages(tmp_path, extension, page_count):
    import fitz

    from agents.perception_agent import routes

    path = tmp_path / f"long.{extension}"
    if extension == "pdf":
        with fitz.open() as document:
            for _ in range(page_count):
                document.new_page()
            document.save(path)
    else:
        with ZipFile(path, "w") as archive:
            for page in range(1, page_count + 1):
                archive.writestr(f"ppt/slides/slide{page}.xml", "<slide/>")

    routes.validate_document_limits(path, path.stat().st_size)


@pytest.mark.parametrize("extension", ["pdf", "pptx"])
def test_document_limits_still_reject_corrupt_document_containers(tmp_path, extension):
    import fitz

    from agents.perception_agent import routes

    path = tmp_path / f"corrupt.{extension}"
    path.write_bytes(b"invalid document")
    with pytest.raises((fitz.FileDataError, ValueError)):
        routes.validate_document_limits(path, path.stat().st_size)
