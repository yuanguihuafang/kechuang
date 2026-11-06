import asyncio
from copy import deepcopy
from io import BytesIO
from types import SimpleNamespace

import pytest
from fastapi import UploadFile
from sqlalchemy import create_engine

from agents.perception_agent import routes
from agents.perception_agent.schemas import PerceptionResult
from runtime.workflows.file_workflow import FileWorkflowService
from runtime.workflows.task_store import _TestTaskStore
from services.persistence.document_lifecycle import DocumentLifecycleStore
from services.persistence.sqlalchemy import metadata


@pytest.fixture(autouse=True)
def isolated_database(monkeypatch):
    # main may have loaded .env.local during collection; never acquire its DB lock.
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(FileWorkflowService, "_audit", lambda *a, **k: None)
    async def unexpected_extraction(*args, **kwargs):
        raise AssertionError("stale task must not invoke field extraction")
    from agents.perception_agent import PerceptionAgent
    monkeypatch.setattr(PerceptionAgent, "extract_fields", unexpected_extraction)


@pytest.fixture
def lifecycle():
    engine = create_engine("sqlite://")
    metadata.create_all(engine)
    yield DocumentLifecycleStore(engine=engine)
    engine.dispose()


def create(store, number=1, **fields):
    return store.create_version(**{
        "document_id": "doc-1", "source_name": "doc.txt",
        "storage_key": f"uploads/v{number}.txt", "scope_id": "public", "user_id": "admin",
        "version_number": number, "knowledge_type": "demand", **fields,
    })


def test_file_source_replacement_identity_survives_persistent_task_roundtrip(lifecycle):
    from services.persistence.repositories.runtime_repository import PostgresStateStore
    tasks = PostgresStateStore(service="supervisor", engine=lifecycle.engine)
    identity = {
        "task_type": "document_ingestion", "version_id": "doc-1:v2",
        "storage_key": "uploads/v2.txt", "previous_version_id": "doc-1:v1",
        "previous_storage_key": "uploads/v1.txt",
    }
    task_id = tasks.create(operation="file_ingestion", document_id="doc-1", status="UPLOADED", state=identity)
    reloaded = tasks.get(task_id)
    assert {field: reloaded.get(field) for field in identity} == identity


def test_field_extraction_failure_survives_persistent_task_reload(lifecycle):
    from services.persistence.repositories.runtime_repository import PostgresStateStore
    from runtime.workflows.file_workflow import _draft_failure

    tasks = PostgresStateStore(service="supervisor", engine=lifecycle.engine)
    task_id = tasks.create(operation="file_ingestion", document_id="doc-1")
    for code in ("DRAFT_INVALID_JSON", "LLM_OUTPUT_EMPTY", "LLM_OUTPUT_TRUNCATED", "LLM_REFUSAL"):
        actual_code, message = _draft_failure(ValueError(code))
        assert actual_code == code
        tasks.update(task_id, draft_status="FAILED", draft_error_code=code, draft_error=message)
        reloaded = PostgresStateStore(service="supervisor", engine=lifecycle.engine).get(task_id)
        assert reloaded["draft_error_code"] == code
        assert reloaded["draft_error"] == message
    tasks.update(task_id, draft_error_code=None, draft_error=None)
    assert tasks.get(task_id)["draft_error"] is None


def test_duplicate_version_is_idempotent_and_cannot_replace_original(lifecycle):
    original = create(lifecycle, status="INDEXED", publication_status="PUBLISHED",
                      artifact_refs={"confirmed_index_request": {"storage_key": "confirmed"}})
    assert create(lifecycle) == original
    with pytest.raises(ValueError):
        create(lifecycle, storage_key="uploads/another.txt")
    assert lifecycle.get_version("doc-1:v1") == original


def test_versions_must_increase_and_purged_identity_is_not_reused(lifecycle):
    create(lifecycle)
    create(lifecycle, 2)
    before = deepcopy(lifecycle.list_versions("doc-1"))
    with pytest.raises(ValueError):
        create(lifecycle, 1, version_id="different-id")
    assert lifecycle.list_versions("doc-1") == before
    lifecycle.update_version("doc-1:v2", status="PURGED")
    with pytest.raises(ValueError):
        create(lifecycle, 2)
    with pytest.raises(ValueError):
        lifecycle.update_version("doc-1:v2", status="PARSING")


class UploadStorage:
    def __init__(self):
        self.objects = {}

    async def find_by_content_hash(self, *_args, **_kwargs):
        return []

    async def find_by_document_id(self, document_id):
        return [dict(item) for item in self.objects.values() if item["document_id"] == document_id]

    async def upload_path(self, path, filename, content_type, **fields):
        key = f"uploads/{len(self.objects) + 1}.txt"
        self.objects[key] = {**fields, "storage_key": key, "body": path.read_bytes()}
        return key, fields["content_hash"]


def test_upload_new_version_increments_and_preserves_old_original(monkeypatch):
    storage = UploadStorage()
    monkeypatch.setattr(routes, "_storage", lambda: storage)

    async def upload(body):
        return await routes.upload_document(UploadFile(filename="doc.txt", file=BytesIO(body)),
                                            "public", "admin", True, "doc-1")

    first = asyncio.run(upload(b"first"))
    second = asyncio.run(upload(b"second"))
    assert (first.version, second.version) == (1, 2)
    assert second.version_id == "doc-1:v2"
    assert storage.objects[first.storage_key]["body"] == b"first"


def workflow_for(store):
    workflow = FileWorkflowService(SimpleNamespace(), task_store=_TestTaskStore(), lifecycle_store=store)
    task = workflow.task_store.create(operation="file_ingestion", status="WAITING_CONFIRM",
        document_id="doc-1", version_id="doc-1:v1", storage_key="uploads/v1.txt",
        scope_id="public", user_id="admin", user_role="admin", knowledge_type="demand")
    return workflow, task


@pytest.mark.parametrize("action", ["parse", "confirm", "regenerate"])
@pytest.mark.parametrize("stale", ["historical", "purged", "wrong_key"])
def test_stale_tasks_cannot_parse_index_or_mix_new_body(lifecycle, action, stale):
    create(lifecycle)
    workflow, task = workflow_for(lifecycle)
    if stale == "historical":
        create(lifecycle, 2)
    elif stale == "purged":
        lifecycle.update_version("doc-1:v1", status="PURGED")
    else:
        workflow.task_store.update(task["task_id"], storage_key="uploads/wrong.txt")
    before = deepcopy(lifecycle.list_versions("doc-1"))
    bodies_used = []

    async def load(_document_id):
        bodies_used.append("new body")
        return {"document_id": "doc-1", "storage_key": "uploads/v2.txt", "raw_text": "new body"}

    async def use(arguments):
        bodies_used.append(arguments)
        return {"status": "SUCCESS", "blocks": [], "metadata": {}}

    workflow.storage_service = SimpleNamespace(load_parse_result=load)
    workflow.adapter = SimpleNamespace(parse_document=use, index_document=use)
    if action == "parse":
        workflow.task_store.update(task["task_id"], status="UPLOADED")
        result = asyncio.run(workflow._parse_node({"task_id": task["task_id"]}))["result"]
    elif action == "confirm":
        result = asyncio.run(workflow._confirm_node({"task_id": task["task_id"],
            "chunks": [{"chunk_text": "old"}], "identity": {"user_id": "admin", "user_role": "admin"}}))["result"]
    else:
        result = asyncio.run(workflow.regenerate_draft(task["task_id"]))
    assert result.get("error_code") == "STALE_SOURCE"
    assert bodies_used == []
    assert lifecycle.list_versions("doc-1") == before


def test_new_parse_failure_preserves_previous_publication(lifecycle):
    original = create(lifecycle, status="INDEXED", publication_status="PUBLISHED",
                      artifact_refs={"parse_result": "old-body", "cover": "old-image"})
    create(lifecycle, 2)
    workflow, task = workflow_for(lifecycle)
    workflow.task_store.update(task["task_id"], version_id="doc-1:v2", storage_key="uploads/v2.txt", status="UPLOADED")

    async def fail(_arguments):
        return {"status": "FAILED", "error": "parse failed"}

    workflow.adapter = SimpleNamespace(parse_document=fail)
    result = asyncio.run(workflow._parse_node({"task_id": task["task_id"]}))["result"]
    assert result["status"] == "FAILED"
    old = lifecycle.get_version("doc-1:v1")
    assert {k: v for k, v in old.items() if k != "_is_current"} == {k: v for k, v in original.items() if k != "_is_current"}


def test_parse_preserves_published_assets_and_namespaces_conflicting_image(monkeypatch, tmp_path):
    output = tmp_path / "output"
    (output / "images").mkdir(parents=True)
    (output / "images" / "cover.png").write_bytes(b"new image")
    old_image = {"image_id": "cover", "minio_object_key": "derived/doc-1/mineru/old/cover.png", "content_hash": "old"}

    class Storage:
        def __init__(self):
            self.images = [deepcopy(old_image)]
            self.artifacts = [{"relative_path": "images/cover.png", "minio_object_key": old_image["minio_object_key"]}]
            self.objects = {old_image["minio_object_key"]: b"old image"}

        async def load_image_manifest(self, _doc):
            return deepcopy(self.images)

        async def load_mineru_artifact_manifest(self, _doc):
            return deepcopy(self.artifacts)

        async def upload_mineru_artifact(self, doc, relative, path, content_type, *, attempt_id):
            key = f"derived/{doc}/mineru/attempts/{attempt_id}/{relative}"
            self.objects[key] = path.read_bytes()
            return key

        async def save_mineru_artifact_manifest(self, doc, items):
            self.artifacts = deepcopy(items)
            return f"derived/{doc}/artifact-manifest.json"

        async def save_image_manifest(self, _doc, items):
            self.images = deepcopy(items)

        async def delete_derived_objects(self, _doc, keys):
            for key in keys:
                self.objects.pop(key, None)

    storage = Storage()
    monkeypatch.setattr(routes, "_storage", lambda: storage)
    monkeypatch.setattr(routes, "MINERU_OUTPUT_ROOT", tmp_path)
    result = PerceptionResult(status="SUCCESS", source_name="doc.pdf", document_id="doc-1", raw_text="![cover](images/cover.png)",
        metadata={"outputDir": str(output), "first_img_url": "/api/v1/perception/images/doc-1/cover",
                  "images": [{"image_id": "cover", "relativePath": "images/cover.png", "contentHash": "new"}]})
    parsed = asyncio.run(routes.persist_image_assets(result))
    current_image = parsed.metadata["images"][0]
    assert storage.objects[old_image["minio_object_key"]] == b"old image"
    assert next(item for item in storage.images if item["image_id"] == "cover") == old_image
    assert current_image["image_id"] != "cover"
    assert current_image["url"] in parsed.raw_text
    assert parsed.metadata["first_img_url"] == current_image["url"]
    assert any(item["minio_object_key"] == old_image["minio_object_key"] for item in storage.artifacts)


@pytest.mark.parametrize("published", [False, True])
def test_cleanup_requires_published_replacement_and_retains_idempotent_outbox(lifecycle, published):
    create(lifecycle)
    create(lifecycle, 2, publication_status="PUBLISHED" if published else "UNPUBLISHED")
    workflow, task = workflow_for(lifecycle)
    task.update(version_id="doc-1:v2", storage_key="uploads/v2.txt",
                previous_version_id="doc-1:v1", previous_storage_key="uploads/v1.txt")
    events = {}

    def append(key, kind, payload):
        events.setdefault(key, {"event_id": "event-1", "payload": deepcopy(payload)})
        return events[key]

    workflow.outbox_repository = SimpleNamespace(append=append)
    asyncio.run(workflow.cleanup_previous_version(task))
    asyncio.run(workflow.cleanup_previous_version(task))
    assert len(events) == int(published)
    if published:
        payload = next(iter(events.values()))["payload"]
        assert payload["replacement_version_id"] == "doc-1:v2"
        assert payload["replacement_storage_key"] == "uploads/v2.txt"
        assert payload["preserve_current_vectors"] is True


@pytest.mark.parametrize("fail_current_write", [False, True])
def test_parse_snapshots_keep_published_body_immutable(monkeypatch, lifecycle, fail_current_write):
    shared_key = "derived/doc-1/mineru/parse-result.json"
    create(lifecycle, status="INDEXED", publication_status="PUBLISHED",
           artifact_refs={"parse_result": shared_key})
    create(lifecycle, 2)

    class Storage:
        def __init__(self):
            self.current = {"document_id": "doc-1", "storage_key": "uploads/v1.txt", "raw_text": "published body"}
            self.objects = {shared_key: deepcopy(self.current)}

        async def load_parse_result(self, _doc):
            return deepcopy(self.current)

        async def save_index_request(self, doc, task, body):
            key = f"derived/{doc}/tasks/{task}/index-request.json"
            self.objects[key] = deepcopy(body)
            return key

        async def save_parse_result(self, _doc, body):
            if fail_current_write:
                raise RuntimeError("current write failed")
            self.current = deepcopy(body)
            self.objects[shared_key] = deepcopy(body)
            return shared_key

    storage = Storage()
    monkeypatch.setattr(routes, "_storage", lambda: storage)
    monkeypatch.setattr(routes, "_document_lifecycle", lambda: lifecycle, raising=False)
    result = PerceptionResult(status="SUCCESS", source_name="doc.txt", document_id="doc-1",
                              storage_key="uploads/v2.txt", raw_text="new body")
    if fail_current_write:
        with pytest.raises(RuntimeError, match="current write failed"):
            asyncio.run(routes.persist_parse_result(result))
    else:
        parsed = asyncio.run(routes.persist_parse_result(result))
        assert parsed.metadata["parse_result_key"] != shared_key
        assert storage.objects[parsed.metadata["parse_result_key"]]["raw_text"] == "new body"
    retained = lifecycle.get_version("doc-1:v1")
    assert retained["artifact_refs"]["parse_result"] != shared_key
    assert storage.objects[retained["artifact_refs"]["parse_result"]]["raw_text"] == "published body"
    assert retained["publication_status"] == "PUBLISHED"


def test_current_task_cannot_extract_draft_from_another_original(lifecycle):
    create(lifecycle)
    workflow, task = workflow_for(lifecycle)
    drafted = []

    async def load(_doc):
        return {"document_id": "doc-1", "storage_key": "uploads/other.txt", "raw_text": "other body"}

    async def build(*args):
        drafted.append(args)
        return {"draft_preview": {"title": "wrong"}}

    workflow.storage_service = SimpleNamespace(load_parse_result=load)
    workflow.knowledge_draft = SimpleNamespace(build_task_fields=build)
    result = asyncio.run(workflow.regenerate_draft(task["task_id"]))
    assert result["error_code"] == "STALE_SOURCE"
    assert drafted == []


def test_storage_key_references_cover_other_documents_and_history(lifecycle):
    create(lifecycle)
    create(lifecycle, document_id="other", storage_key="uploads/shared.txt")
    assert lifecycle.is_storage_key_referenced("uploads/shared.txt")
    assert not lifecycle.is_storage_key_referenced("uploads/shared.txt", exclude_version_ids=("other:v1",))
    create(lifecycle, 2, document_id="other", storage_key="uploads/other-v2.txt")
    lifecycle.update_version("other:v2", status="PURGED")
    assert lifecycle.is_storage_key_referenced("uploads/shared.txt")
    lifecycle.update_version("other:v1", status="PURGED")
    assert not lifecycle.is_storage_key_referenced("uploads/shared.txt")


def test_version_id_cannot_belong_to_two_documents(lifecycle):
    create(lifecycle)
    with pytest.raises(ValueError):
        create(lifecycle, document_id="other", version_id="doc-1:v1")
    assert lifecycle.get_document("other") is None


def test_editing_new_parse_does_not_remove_prior_attempt_assets():
    old_key, new_key = "derived/doc-1/old.png", "derived/doc-1/new.png"
    current = {"storage_key": "uploads/v2.txt", "metadata": {"images": [
        {"image_id": "new", "url": "/api/v1/perception/images/doc-1/new", "minio_object_key": new_key}
    ]}}
    old = {"image_id": "old", "minio_object_key": old_key}
    manifest = [old, {"image_id": "new", "minio_object_key": new_key}]
    _, retained, artifacts, removed = routes._prune_removed_parse_images(
        "![new](/api/v1/perception/images/doc-1/new)", current, manifest,
        [{"relative_path": "images/cover.png", "minio_object_key": key} for key in (old_key, new_key)]
    )
    assert old in retained
    assert old_key not in removed
    assert any(item["minio_object_key"] == old_key for item in artifacts)


def test_selecting_draft_type_does_not_change_source_identity(lifecycle, monkeypatch):
    from agents.perception_agent import PerceptionAgent
    from services.persistence.postgres import InMemoryDraftStore
    create(lifecycle, knowledge_type="")
    workflow, task = workflow_for(lifecycle)
    workflow.knowledge_draft.draft_store = InMemoryDraftStore()
    seen_bodies = []

    async def extract(draft, *, source_text, **kwargs):
        seen_bodies.append(source_text)
        return {**draft, "title": "从原件提取的需求"}

    async def load(_doc):
        return {"document_id": "doc-1", "storage_key": "uploads/v1.txt", "raw_text": "原件正文", "blocks": []}

    monkeypatch.setattr(PerceptionAgent, "extract_fields", extract)
    workflow.storage_service = SimpleNamespace(load_parse_result=load)
    assert workflow.validate_task_source(task)["storage_key"] == task["storage_key"]
    result = asyncio.run(workflow.regenerate_draft(task["task_id"]))
    assert result["draft_status"] == "READY"
    assert seen_bodies == ["原件正文"]
    assert result["draft_preview"]["title"] == "从原件提取的需求"
    assert lifecycle.get_version("doc-1:v1")["knowledge_type"] == ""


def test_upload_route_adapter_and_workflow_keep_published_version(monkeypatch, lifecycle):
    from runtime.tool_adapters.adapters import InProcessToolAdapter
    original = create(lifecycle, status="INDEXED", publication_status="PUBLISHED",
                      artifact_refs={"cover": "old-cover", "confirmed_index_request": {"storage_key": "confirmed"}})
    storage = UploadStorage()
    storage.objects["uploads/v1.txt"] = {"document_id": "doc-1", "version": 1, "body": b"old", "storage_key": "uploads/v1.txt"}
    monkeypatch.setattr(routes, "_storage", lambda: storage)
    monkeypatch.setattr(routes, "_document_lifecycle", lambda: lifecycle)
    adapter = InProcessToolAdapter(SimpleNamespace(upload_document=routes.upload_document), SimpleNamespace())
    workflow = FileWorkflowService(adapter, task_store=_TestTaskStore(), lifecycle_store=lifecycle)
    task = asyncio.run(workflow.upload(UploadFile(filename="doc.txt", file=BytesIO(b"new")),
        scope_id="public", user_id="admin", user_role="admin", knowledge_type="demand", existing_document_id="doc-1"))
    assert task["status"] == "UPLOADED"
    assert task["version_id"] == "doc-1:v2"
    assert task["previous_version_id"] == "doc-1:v1"
    assert storage.objects["uploads/v1.txt"]["body"] == b"old"
    retained = lifecycle.get_version("doc-1:v1")
    assert retained == {**original, "_is_current": False}


def test_upload_version_uses_persisted_watermark_after_original_cleanup(monkeypatch, lifecycle):
    create(lifecycle, 8)
    lifecycle.update_version("doc-1:v8", status="PURGED")
    storage = UploadStorage()
    monkeypatch.setattr(routes, "_storage", lambda: storage)
    monkeypatch.setattr(routes, "_document_lifecycle", lambda: lifecycle)
    result = asyncio.run(routes.upload_document(
        UploadFile(filename="doc.txt", file=BytesIO(b"new")), "public", "admin", True, "doc-1"))
    assert result.version == 9
    assert result.version_id == "doc-1:v9"
    assert lifecycle.get_version("doc-1:v8")["status"] == "PURGED"


def test_concurrent_uploads_allocate_distinct_increasing_versions(monkeypatch):
    class SlowStorage(UploadStorage):
        async def upload_path(self, *args, **kwargs):
            await asyncio.sleep(0)
            return await super().upload_path(*args, **kwargs)

    storage = SlowStorage()
    monkeypatch.setattr(routes, "_storage", lambda: storage)

    async def upload_all():
        return await asyncio.gather(*(routes.upload_document(
            UploadFile(filename="doc.txt", file=BytesIO(str(i).encode())),
            "public", "admin", True, "doc-1") for i in range(5)))

    results = asyncio.run(upload_all())
    assert sorted(item.version for item in results) == [1, 2, 3, 4, 5]
    assert len({item.version_id for item in results}) == 5
    assert len(storage.objects) == 5


def test_replayed_historical_upload_cannot_move_active_version_backwards(lifecycle):
    original = create(lifecycle, status="INDEXED", publication_status="PUBLISHED", artifact_refs={"cover": "old"})
    current = create(lifecycle, 2)
    replay = create(lifecycle)
    assert replay == {**original, "_is_current": False}
    assert lifecycle.get_document("doc-1")["active_version_id"] == "doc-1:v2"
    assert lifecycle.get_version("doc-1:v2") == current


def test_real_routes_adapter_and_langgraph_parse_finish_without_nested_task_deadlock(monkeypatch, lifecycle):
    from agents.perception_agent import PerceptionAgent
    from runtime.tool_adapters.adapters import InProcessToolAdapter
    from runtime.workflows.task_store import SupervisorTaskStore
    from services.document_parsing.text_parser import PlainTextDocumentParser

    class Storage(UploadStorage):
        def __init__(self):
            super().__init__()
            self.parse_result = None
            self.snapshots = {}

        async def download_file_to_directory(self, key, directory, _limit):
            path = directory / "doc.txt"
            path.write_bytes(self.objects[key]["body"])
            return path, "text/plain", "doc.txt", path.stat().st_size

        async def load_image_manifest(self, _doc):
            return []

        async def save_image_manifest(self, _doc, _images):
            return "derived/doc/images.json"

        async def save_index_request(self, doc, task, body):
            key = f"derived/{doc}/tasks/{task}/index-request.json"
            self.snapshots[key] = deepcopy(body)
            return key

        async def save_parse_result(self, doc, body):
            self.parse_result = deepcopy(body)
            return f"derived/{doc}/mineru/parse-result.json"

    storage = Storage()
    route_store = SupervisorTaskStore()
    route_context = routes._RouteContext(
        PerceptionAgent(parser=PlainTextDocumentParser()), route_store)
    monkeypatch.setattr(routes, "task_store", route_store)
    monkeypatch.setattr(routes, "_context", lambda: route_context)
    monkeypatch.setattr(routes, "_storage", lambda: storage)
    monkeypatch.setattr(routes, "_document_lifecycle", lambda: lifecycle)
    workflow = FileWorkflowService(InProcessToolAdapter(routes, SimpleNamespace()),
                                   task_store=_TestTaskStore(), lifecycle_store=lifecycle)

    async def upload_and_parse():
        task = await workflow.upload(UploadFile(filename="doc.txt", file=BytesIO(b"actual source")),
            scope_id="public", user_id="admin", user_role="admin", knowledge_type="demand")
        result = await workflow.parse(task["task_id"])
        return task, result

    async def bounded_flow():
        return await asyncio.wait_for(upload_and_parse(), timeout=5)

    task, parsed = asyncio.run(bounded_flow())
    assert parsed["status"] == "WAITING_CONFIRM"
    assert parsed["preview_text"] == "actual source"
    version = lifecycle.get_version(task["version_id"])
    assert storage.snapshots[version["artifact_refs"]["parse_result"]]["raw_text"] == "actual source"
    assert storage.parse_result["storage_key"] == task["storage_key"]
