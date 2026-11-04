import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine

from runtime.workflows.file_workflow import FileWorkflowService
from runtime.workflows.task_store import _TestTaskStore
from services.persistence.document_lifecycle import DocumentLifecycleStore
from services.persistence.repositories.runtime_repository import PostgresStateStore
from services.persistence.sqlalchemy import metadata


IDENTITY = {"user_id": "admin", "user_role": "admin"}
CHUNKS = [{"chunk_text": "人工确认的最终正文", "source_locator": "doc.pdf|page:1", "document_id": "doc-1"}]


class Storage:
    def __init__(self):
        self.bodies = {}
        self.images = [{"image_id": "kept-image"}]

    async def save_index_request(self, document_id, task_id, body):
        self.bodies[(document_id, task_id)] = deepcopy(body)
        return f"derived/{document_id}/tasks/{task_id}/index-request.json"

    async def load_index_request(self, document_id, task_id):
        return deepcopy(self.bodies.get((document_id, task_id)))

    async def save_image_manifest(self, _document_id, images):
        self.images = deepcopy(images)


def _scenario(monkeypatch, *, failed=False, db_url="sqlite://"):
    engine = create_engine(db_url)
    metadata.create_all(engine)
    lifecycle = DocumentLifecycleStore(engine=engine)
    lifecycle.create_version(
        document_id="doc-1", version_id="v1", source_name="doc.pdf",
        storage_key="uploads/doc.pdf", scope_id="public", user_id="admin",
        version_number=1, knowledge_type="demand", status="READY_TO_PUBLISH",
        artifact_refs={"parse_result": "derived/doc-1/parse.json", "manifest": "derived/doc-1/manifest.json"},
    )
    tasks = _TestTaskStore()
    task = tasks.create(
        operation="file_ingestion", status="WAITING_CONFIRM", document_id="doc-1",
        version_id="v1", scope_id="public", knowledge_type="demand", **IDENTITY,
    )
    storage = Storage()

    async def index(arguments):
        assert arguments["identity"] == IDENTITY
        return {"status": "FAILED" if failed else "SUCCESS", "indexed_count": 1, "error": "index failed"}

    workflow = object.__new__(FileWorkflowService)
    workflow.task_store = tasks
    workflow.lifecycle_store = lifecycle
    workflow.storage_service = storage
    workflow.adapter = SimpleNamespace(index_document=index)
    workflow.outbox_repository = None
    monkeypatch.setattr(workflow, "_audit", lambda *_, **__: None)
    state = {"task_id": task["task_id"], "chunks": deepcopy(CHUNKS), "identity": deepcopy(IDENTITY)}
    return workflow, lifecycle, tasks, storage, task, state


def test_successful_confirmation_retains_version_source_after_task_retention(monkeypatch):
    workflow, lifecycle, tasks, storage, task, state = _scenario(monkeypatch)
    result = asyncio.run(workflow._confirm_node(state))["result"]
    assert result["status"] == "SUCCEEDED"
    tasks.items.clear()
    refs = lifecycle.get_version("v1")["artifact_refs"]
    assert refs["parse_result"] == "derived/doc-1/parse.json"
    assert refs["manifest"] == "derived/doc-1/manifest.json"
    assert refs["confirmed_index_request"] == {
        "storage_key": f"derived/doc-1/tasks/{task['task_id']}/index-request.json",
        "task_id": task["task_id"], "document_id": "doc-1", "version_id": "v1", "identity": IDENTITY,
    }
    from services.application.index_recovery_sources import load_confirmed_version_request

    request = asyncio.run(load_confirmed_version_request(storage, lifecycle.get_document("doc-1"), lifecycle.get_version("v1")))
    assert request.chunks[0].chunk_text == "人工确认的最终正文"


def test_confirmation_does_not_restore_images_removed_from_saved_manifest(monkeypatch):
    workflow, _, _, storage, _, state = _scenario(monkeypatch)
    state.update(
        images=[{"image_id": "deleted-image"}, {"image_id": "kept-image"}],
        images_confirmed=True,
    )

    result = asyncio.run(workflow._confirm_node(state))["result"]

    assert result["status"] == "SUCCEEDED"
    assert storage.images == [{"image_id": "kept-image"}]


@pytest.mark.parametrize("failed", [True, False])
def test_failed_confirmation_does_not_replace_published_source(monkeypatch, failed):
    workflow, lifecycle, _, storage, task, state = _scenario(monkeypatch, failed=failed)
    old_ref = {
        "storage_key": "derived/doc-1/tasks/old/index-request.json", "task_id": "old",
        "document_id": "doc-1", "version_id": "v1", "identity": IDENTITY,
    }
    lifecycle.update_version("v1", artifact_refs={**lifecycle.get_version("v1")["artifact_refs"], "confirmed_index_request": old_ref})
    original = deepcopy(lifecycle.get_version("v1")["artifact_refs"])
    if not failed:
        lifecycle.update_version("v1", status="INDEXED", publication_status="PUBLISHED")
        workflow.task_store.update(task["task_id"], version_id="missing-version")
    result = asyncio.run(workflow._confirm_node(state))["result"]
    assert result["status"] == "FAILED"
    assert lifecycle.get_version("v1")["artifact_refs"] == original
    assert storage.bodies == {}


def test_missing_version_write_cannot_mark_task_successful(monkeypatch):
    workflow, lifecycle, _, _, _, state = _scenario(monkeypatch)
    update = lifecycle.update_version

    def fail(version_id, **fields):
        if fields.get("publication_status") == "PUBLISHED":
            return None
        return update(version_id, **fields)

    monkeypatch.setattr(lifecycle, "update_version", fail)
    result = asyncio.run(workflow._confirm_node(state))["result"]
    assert result["status"] == "FAILED"
    assert "confirmed_index_request" not in lifecycle.get_version("v1")["artifact_refs"]


def test_confirmation_source_failure_keeps_previous_version(monkeypatch):
    workflow, _, _, storage, _, state = _scenario(monkeypatch)
    cleaned = []

    async def fail_save(*args):
        raise OSError("source unavailable")

    async def cleanup(task):
        cleaned.append(task)

    storage.save_index_request = fail_save
    workflow.cleanup_previous_version = cleanup
    result = asyncio.run(workflow._confirm_node(state))["result"]
    assert result["status"] == "FAILED"
    assert cleaned == []


def test_version_artifact_updates_merge_and_reject_stale_snapshot(monkeypatch):
    _, lifecycle, _, _, _, _ = _scenario(monkeypatch)
    snapshot = lifecycle.get_version("v1")
    lifecycle.update_version("v1", artifact_refs={"new": "derived/new.json"})
    assert lifecycle.get_version("v1")["artifact_refs"]["parse_result"] == "derived/doc-1/parse.json"
    with pytest.raises(RuntimeError, match="变化"):
        lifecycle.update_version("v1", expected_version=snapshot, artifact_refs={"confirmed_index_request": {"task_id": "stale"}})
    assert "confirmed_index_request" not in lifecycle.get_version("v1")["artifact_refs"]


def _backfill_runtime(monkeypatch, *, db_url="sqlite://"):
    _, lifecycle, _, storage, _, _ = _scenario(monkeypatch, db_url=db_url)
    lifecycle.update_version("v1", status="INDEXED", publication_status="PUBLISHED", indexed_count=1)
    # 使用隔离的内存引擎测试 PostgreSQL 状态适配器；
    # 任务元数据保存在 state JSON 中，而非专用任务列。
    tasks = PostgresStateStore(service="supervisor", engine=lifecycle.engine)
    task_id = tasks.create(
        operation="file_ingestion", document_id="doc-1", status="SUCCEEDED",
        state={"version_id": "v1", "scope_id": "public", "knowledge_type": "demand", **IDENTITY},
    )
    key = asyncio.run(storage.save_index_request("doc-1", task_id, {"chunks": CHUNKS, "identity": IDENTITY}))
    tasks.update(task_id, index_request_ref=key)
    return SimpleNamespace(storage_service=storage, task_store=tasks, require_lifecycle=lambda: lifecycle), lifecycle, storage, task_id


def test_backfill_ignores_versions_that_were_never_confirmed(monkeypatch):
    from services.application.index_recovery_sources import backfill_confirmed_sources
    workflow, lifecycle, tasks, storage, _, _ = _scenario(monkeypatch)
    runtime = SimpleNamespace(require_lifecycle=lambda: lifecycle, task_store=tasks,
                              storage_service=storage)
    report = asyncio.run(backfill_confirmed_sources(runtime))
    assert report == {"scanned": 0, "eligible": 0, "updated": 0, "rejected": 0}


def test_backfill_keeps_live_history_under_purged_current_version(monkeypatch):
    from services.application.index_recovery_sources import backfill_confirmed_sources
    runtime, lifecycle, _, _ = _backfill_runtime(monkeypatch)
    lifecycle.create_version(document_id="doc-1", version_id="v2", source_name="doc.pdf",
        storage_key="uploads/v2.pdf", scope_id="public", user_id="admin", version_number=2,
        knowledge_type="demand")
    lifecycle.update_version("v2", status="PURGED")
    report = asyncio.run(backfill_confirmed_sources(runtime))
    assert report["eligible"] == 1
    assert report["rejected"] == 0


def test_new_upload_retains_previously_confirmed_version_source(monkeypatch):
    runtime, lifecycle, _, _ = _backfill_runtime(monkeypatch)
    from services.application.index_recovery_sources import backfill_confirmed_sources
    asyncio.run(backfill_confirmed_sources(runtime, apply=True))
    original = deepcopy(lifecycle.get_version("v1"))
    lifecycle.create_version(document_id="doc-1", version_id="v2", source_name="new.pdf",
        storage_key="uploads/v2.pdf", scope_id="public", user_id="admin", version_number=2,
        knowledge_type="demand", status="UPLOADED")
    retained = lifecycle.get_version("v1")
    assert retained is not None
    assert retained["artifact_refs"] == original["artifact_refs"]
    assert retained["publication_status"] == "PUBLISHED"
    assert len(lifecycle.list_versions("doc-1")) == 2


def test_backfill_dry_run_and_apply_are_explicit(monkeypatch):
    from services.application.index_recovery_sources import backfill_confirmed_sources

    runtime, lifecycle, storage, task_id = _backfill_runtime(monkeypatch)
    original = deepcopy(lifecycle.get_version("v1"))
    body = deepcopy(storage.bodies)
    report = asyncio.run(backfill_confirmed_sources(runtime))
    assert report["eligible"] == 1 and report["updated"] == 0
    assert lifecycle.get_version("v1") == original
    report = asyncio.run(backfill_confirmed_sources(runtime, apply=True))
    assert report["updated"] == 1
    version = lifecycle.get_version("v1")
    assert version["status"] == original["status"]
    assert version["publication_status"] == original["publication_status"]
    assert version["artifact_refs"]["confirmed_index_request"]["task_id"] == task_id
    assert storage.bodies == body
    assert asyncio.run(backfill_confirmed_sources(runtime, apply=True))["updated"] == 0


@pytest.mark.parametrize("change", ["identity", "version", "empty", "ref"])
def test_backfill_rejects_bad_confirmed_task_body(monkeypatch, change):
    from services.application.index_recovery_sources import backfill_confirmed_sources

    runtime, lifecycle, storage, task_id = _backfill_runtime(monkeypatch)
    body = storage.bodies[("doc-1", task_id)]
    if change == "identity":
        body["identity"] = {"user_id": "another", "user_role": "admin"}
    elif change == "version":
        body["version_id"] = "old"
    elif change == "empty":
        body["chunks"] = []
    else:
        runtime.task_store.update(task_id, index_request_ref="derived/another/tasks/bad/index-request.json")
    report = asyncio.run(backfill_confirmed_sources(runtime, apply=True))
    assert report["updated"] == 0 and report["rejected"] == 1
    assert "confirmed_index_request" not in lifecycle.get_version("v1")["artifact_refs"]


def test_backfill_refuses_version_changed_during_body_read(monkeypatch):
    from services.application.index_recovery_sources import backfill_confirmed_sources

    runtime, lifecycle, storage, _ = _backfill_runtime(monkeypatch)
    load = storage.load_index_request

    async def changed(*args):
        lifecycle.update_version("v1", status="PURGED")
        return await load(*args)

    storage.load_index_request = changed
    report = asyncio.run(backfill_confirmed_sources(runtime, apply=True))
    assert report["updated"] == 0 and report["rejected"] == 1
    assert lifecycle.get_version("v1")["status"] == "PURGED"
    assert "confirmed_index_request" not in lifecycle.get_version("v1")["artifact_refs"]


@pytest.mark.parametrize("apply", [False, True])
def test_backfill_cli_defaults_to_dry_run_without_runtime_start(monkeypatch, capsys, tmp_path, apply):
    from scripts import backfill_confirmed_index_sources as script

    runtime, lifecycle, _, _ = _backfill_runtime(monkeypatch, db_url=f"sqlite:///{tmp_path / 'cli.sqlite'}")
    monkeypatch.setattr(script, "_build_runtime", lambda: runtime)
    assert script.main(["--apply"] if apply else []) == 0
    import json

    output = json.loads(capsys.readouterr().out)
    assert output["mode"] == ("apply" if apply else "dry-run")
    assert output["updated"] == int(apply)
    assert ("confirmed_index_request" in lifecycle.get_version("v1")["artifact_refs"]) == apply


def test_backfill_scans_all_pages_before_updates_reorder_documents(monkeypatch):
    from services.application.index_recovery_sources import backfill_confirmed_sources

    runtime, lifecycle, _, _ = _backfill_runtime(monkeypatch)
    original = lifecycle.get_document("doc-1")
    # 应用更新后，每份文档都会排到实际目录的最前面。
    documents = [deepcopy(original) for _ in range(101)]
    for i, document in enumerate(documents):
        document["document_id"] = f"doc-{i}"
    reads = []
    updates = []

    def page(*, page, page_size, include_purged):
        assert include_purged is True
        reads.append(page)
        start = (page - 1) * page_size
        return {"documents": documents[start:start + page_size], "total_pages": 2}

    version = lifecycle.get_version("v1")

    def versions(document_id):
        return [{**version, "document_id": document_id, "version_id": document_id}]

    def update(version_id, **fields):
        assert reads == [1, 2]
        updates.append(version_id)
        target = next(item for item in documents if item["document_id"] == version_id)
        documents.remove(target)
        documents.insert(0, target)
        return fields

    class Tasks:
        def get(self, task_id):
            return {
                "task_id": task_id, "document_id": task_id, "version_id": task_id,
                "status": "SUCCEEDED", "operation": "file_ingestion", "scope_id": "public",
                "knowledge_type": "demand", **IDENTITY,
                "index_request_ref": f"derived/{task_id}/tasks/{task_id}/index-request.json",
            }

        def list_page(self, **fields):
            return {"items": [self.get(fields["document_id"])], "total_pages": 1}

    async def load(document_id, task_id):
        return {"chunks": [{"chunk_text": "正文", "source_locator": "doc.pdf|page:1", "document_id": document_id}], "identity": IDENTITY}

    runtime.require_lifecycle = lambda: SimpleNamespace(
        list_documents_page=page, list_versions=versions,
        get_version=lambda version_id: versions(version_id)[0], update_version=update,
    )
    runtime.task_store = Tasks()
    runtime.storage_service.load_index_request = load
    report = asyncio.run(backfill_confirmed_sources(runtime, apply=True))
    assert report["updated"] == 101
    assert len(set(updates)) == 101
