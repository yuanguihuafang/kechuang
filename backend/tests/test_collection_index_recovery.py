import asyncio
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from admin.service import AdminService
from runtime.workflows.task_store import _TestTaskStore


@pytest.fixture(autouse=True)
def mysql_source(monkeypatch):
    monkeypatch.setattr(
        "admin.mysql_sync.MySQLBusinessSearchService",
        lambda: SimpleNamespace(
            count_for_sync=lambda _: 0,
            list_for_sync=lambda *_args, **_kwargs: [],
            records_for_sync=lambda *_args, **_kwargs: [],
        ),
    )


def _runtime(*, missing_request=False, reset_error=False):
    calls = []
    tasks = _TestTaskStore()
    task = tasks.create(
        operation="file_ingestion",
        status="SUCCEEDED",
        document_id="doc-1",
        version_id="v1",
        knowledge_type="demand",
        scope_id="public",
        user_id="admin",
        user_role="admin",
        index_request_ref="derived/doc-1/tasks/confirm/index-request.json",
    )
    task["index_request_ref"] = f"derived/doc-1/tasks/{task['task_id']}/index-request.json"
    tasks.update(task["task_id"], index_request_ref=task["index_request_ref"])
    document = {
        "document_id": "doc-1",
        "storage_key": "uploads/doc-1.pdf",
        "scope_id": "public",
        "user_id": "admin",
        "active_version_id": "v1",
        "status": "INDEXED",
        "knowledge_type": "demand",
        "active_version": {
            "version_id": "v1",
            "storage_key": "uploads/doc-1.pdf",
            "publication_status": "PUBLISHED",
            "artifact_refs": {
                "confirmed_index_request": {
                    "storage_key": task["index_request_ref"],
                    "task_id": task["task_id"],
                    "document_id": "doc-1",
                    "version_id": "v1",
                    "identity": {"user_id": "admin", "user_role": "admin"},
                }
            },
        },
    }

    async def load(*_):
        return (
            None
            if missing_request
            else {
                "chunks": [
                    {
                        "chunk_text": "人工确认正文",
                        "source_locator": "mysql:demand:1",
                        "mysql_id": "1",
                        "document_id": "doc-1",
                    }
                ],
                "identity": {"user_id": "admin", "user_role": "admin"},
            }
        )

    def reset(*_):
        calls.append("reset")
        if reset_error:
            raise RuntimeError("checkpoint write failed")

    async def index(request):
        calls.append(("index", request.document_id, request.chunks[0].chunk_text))
        return SimpleNamespace(status="SUCCESS", indexed_count=1)

    async def ready():
        return None

    async def inspect_object(storage_key):
        return {"storage_key": storage_key, "etag": "test-etag", "size": 100}

    async def load_image_manifest(*_args, **_kwargs):
        return []

    target = SimpleNamespace(
        check_ready=ready,
        rebuild_collection=lambda: calls.append("rebuild"),
        flush=lambda: calls.append("flush"),
    )
    runtime = None

    def track_background_thread(task, stop_event):
        runtime.background_tasks.add(task)
        runtime.background_stop_events[task] = stop_event

        def discard(done):
            runtime.background_tasks.discard(done)
            runtime.background_stop_events.pop(done, None)

        task.add_done_callback(discard)

    runtime = SimpleNamespace(
        task_store=tasks,
        sync_state_store=SimpleNamespace(reset=reset),
        storage_service=SimpleNamespace(
            load_index_request=load,
            inspect_object=inspect_object,
            load_image_manifest=load_image_manifest,
        ),
        background_tasks=set(),
        background_stop_events={},
        track_background_thread=track_background_thread,
        require_draft_store=lambda: SimpleNamespace(
            list_page=lambda **_: {"items": [], "total_pages": 1}
        ),
        require_lifecycle=lambda: SimpleNamespace(
            list_documents_page=lambda **_: {"documents": [document], "total_pages": 1},
            get_document=lambda *_: document,
        ),
        embedded_retrieval=SimpleNamespace(
            agent=SimpleNamespace(public_milvus={"demand": target}, index=index)
        ),
    )
    return runtime, calls


def test_rebuild_is_read_only_preflight_and_reports_missing_sources(monkeypatch):
    runtime, calls = _runtime(missing_request=True)
    mysql_reads = []

    def mysql_preflight(knowledge_type):
        mysql_reads.append(knowledge_type)
        return 0

    monkeypatch.setattr(
        "admin.mysql_sync.MySQLBusinessSearchService",
        lambda: SimpleNamespace(count_for_sync=mysql_preflight),
    )
    service = AdminService(runtime)
    result = asyncio.run(service.rebuild_collection("demand", True))

    assert result["status"] == "PREFLIGHT_COMPLETE"
    assert calls == []
    assert mysql_reads == ["demand"]
    task = runtime.task_store.get(result["task_id"])
    assert task["status"] == "PREFLIGHT_COMPLETE"
    assert task["recovery_result"]["skipped_sources"][0]["source_id"] == "doc-1"
    assert task["recovery_result"]["recoverable_sources"] == []
    assert "object_checks" in task["recovery_result"]


def test_preflight_exposes_request_sources_and_additional_object_checks(monkeypatch):
    from agents.retrieval_agent.schemas import IndexRequest

    runtime, calls = _runtime()
    request = IndexRequest(
        document_id="attachment-1", knowledge_type="demand",
        chunks=[{"chunk_text": "confirmed text", "source_locator": "attachment-1|page:1"}],
        identity={"user_id": "admin", "user_role": "admin"},
    )
    source_ref = {
        "source_id": "draft-1", "source_fingerprint": "source-hash",
        "documents": [{"document_id": "attachment-1", "version_id": "v1"}],
        "restore_mode": "saved_final_chunks",
    }
    object_ref = {"document_id": "attachment-1", "storage_key": "originals/attachment-1.pdf"}

    async def prepare(*_):
        return [request], [source_ref, object_ref], []

    monkeypatch.setattr("admin.mysql_sync.prepare_collection_recovery", prepare)
    result = asyncio.run(AdminService(runtime).rebuild_collection("demand", True))
    assert result["recoverable_sources"] == [source_ref]
    assert result["object_checks"] == [object_ref]
    persisted = runtime.task_store.get(result["task_id"])["recovery_result"]
    assert persisted["recoverable_sources"] == [source_ref]
    assert persisted["object_checks"] == [object_ref]
    assert calls == []


def test_mysql_recovery_snapshot_fingerprint_changes_when_row_content_changes():
    from admin.index_recovery import mysql_source_snapshot

    source = SimpleNamespace(
        list_for_sync=lambda _type, *, after_id, limit: (
            [{"id": 1, "name": "old"}] if after_id == 0 else []
        )
    )
    first = mysql_source_snapshot(source, "solution")
    source.list_for_sync = lambda _type, *, after_id, limit: (
        [{"id": 1, "name": "edited"}] if after_id == 0 else []
    )
    second = mysql_source_snapshot(source, "solution")

    assert first[0] == second[0] == 1
    assert first[1] != second[1]


def test_collection_preflight_never_schedules_a_background_writer():
    runtime, calls = _runtime()
    result = asyncio.run(AdminService(runtime).rebuild_collection("demand", True))
    assert result["status"] == "PREFLIGHT_COMPLETE"
    assert not runtime.background_tasks
    assert calls == []


def test_collection_preflight_does_not_reset_checkpoint_or_drop_collection():
    runtime, calls = _runtime(reset_error=True)
    result = asyncio.run(AdminService(runtime).rebuild_collection("demand", True))
    assert result["status"] == "PREFLIGHT_COMPLETE"
    assert calls == []


def test_collection_preflight_keeps_published_draft_and_source_id_unchanged():
    runtime, calls = _runtime()
    service = AdminService(runtime)
    draft_row = {
        "draft_id": "draft-1",
        "status": "PUBLISHED",
        "draft_type": "demand",
        "user_id": "admin",
        "user_role": "admin",
        "version": 1,
        "updated_at": "draft-time",
        "draft": {
            "draft_type": "demand",
            "title": "测试需求",
            "mysql_record_id": "1",
            "source_id": "source:restored",
            "fields": {"contect": "需求正文"},
            "attachments": [{"document_id": "doc-1", "version_id": "v1", "file_name": "需求.pdf"}],
            "source_blocks": [{"text": "原文内容"}],
        },
    }

    class Drafts:
        def list_page(self, **_):
            return {"items": [draft_row], "total_pages": 1}

        def get_admin(self, draft_id):
            return draft_row if draft_id == "draft-1" else None

    runtime.require_draft_store = lambda: Drafts()
    runtime.require_lifecycle = lambda: SimpleNamespace(
        list_documents_page=lambda **_: {"documents": [], "total_pages": 1}
    )
    result = asyncio.run(service.rebuild_collection("demand", True))
    assert result["status"] == "PREFLIGHT_COMPLETE"
    assert draft_row["draft"]["source_id"] == "source:restored"
    assert calls == []


def test_document_version_mismatch_does_not_block_empty_collection_rebuild():
    runtime, calls = _runtime()
    document = runtime.require_lifecycle().get_document("doc-1")
    document["active_version"]["artifact_refs"]["confirmed_index_request"]["version_id"] = "old"
    service = AdminService(runtime)

    async def scenario():
        result = await service.rebuild_collection("demand", True)
        await asyncio.gather(*runtime.background_tasks)
        return result

    result = asyncio.run(scenario())
    assert result["status"] == "PREFLIGHT_COMPLETE"
    assert result["skipped_sources"][0]["source_id"] == "doc-1"
    assert calls == []


def test_recovery_works_without_old_task():
    from admin.index_recovery import prepare_collection_recovery

    runtime, _ = _runtime()
    runtime.task_store.items.clear()
    requests, references, skipped = asyncio.run(prepare_collection_recovery(runtime, "demand"))
    assert requests[0].chunks[0].chunk_text == "人工确认正文"
    assert references[0]["version_id"] == "v1"
    assert skipped == []


def test_recovery_normalizes_legacy_batch_source_id_in_draft_and_index():
    from admin.index_recovery import prepare_collection_recovery

    runtime, _ = _runtime()
    draft_row = {
        "draft_id": "draft-1",
        "status": "PUBLISHED",
        "draft_type": "solution",
        "user_id": "user-1",
        "user_role": "external",
        "version": 1,
        "updated_at": "before",
        "draft": {
            "draft_type": "solution",
            "title": "测试方案",
            "mysql_record_id": "1",
            "source_id": "source:batch:legacy",
            "fields": {"contect": "方案正文"},
            "attachments": [],
            "source_blocks": [],
        },
    }

    class Drafts:
        def list_page(self, **_):
            return {"items": [draft_row], "total_pages": 1}

        def update(self, draft_id, *, user_id, draft, expected_version=None):
            assert draft_id == "draft-1" and user_id == "user-1"
            assert expected_version == 1
            draft_row["draft"].update(draft)
            draft_row["version"] = 2
            draft_row["updated_at"] = "after"
            return draft_row

    runtime.require_draft_store = lambda: Drafts()
    runtime.require_lifecycle = lambda: SimpleNamespace(
        list_documents_page=lambda **_: {"documents": [], "total_pages": 1}
    )

    requests, references, skipped = asyncio.run(
        prepare_collection_recovery(runtime, "solution")
    )

    assert requests[0].chunks[0].source_id == "source:legacy"
    assert draft_row["draft"]["source_id"] == "source:batch:legacy"
    assert references[0]["updated_at"] == "before"
    assert skipped == []


def test_preflight_missing_image_identifies_related_draft_and_attachment():
    from admin.index_recovery import prepare_collection_recovery

    runtime, _ = _runtime()
    draft_row = {
        "draft_id": "draft-1",
        "status": "PUBLISHED",
        "draft_type": "demand",
        "user_id": "admin",
        "user_role": "admin",
        "updated_at": "now",
        "draft": {
            "title": "测试需求草稿",
            "mysql_record_id": "1",
            "source_id": "source:test",
            "fields": {"contect": "正文"},
            "attachments": [
                {"document_id": "doc-1", "version_id": "v1", "file_name": "需求附件.pdf"}
            ],
            "source_blocks": [
                {
                    "text": "原文引用 ![缺失图片](/api/v1/perception/images/doc-1/missing-image)",
                    "document_id": "doc-1",
                    "version_id": "v1",
                }
            ],
        },
    }
    runtime.require_draft_store = lambda: SimpleNamespace(
        list_page=lambda **_: {"items": [draft_row], "total_pages": 1}
    )

    _, _, skipped = asyncio.run(prepare_collection_recovery(runtime, "demand"))

    missing_image = next(item for item in skipped if item["source_type"] == "image")
    assert missing_image["source_id"] == "missing-image"
    assert missing_image["document_id"] == "doc-1"
    assert missing_image["related_sources"] == [{
        "draft_id": "draft-1",
        "draft_title": "测试需求草稿",
        "attachment_name": "需求附件.pdf",
        "document_id": "doc-1",
    }]


def test_preflight_does_not_treat_markdown_image_alt_text_as_a_missing_id():
    from admin.index_recovery import prepare_collection_recovery

    runtime, _ = _runtime()
    image_id = "8676bf791a5c4430188a188a1197b6f8cee51346"

    async def load(*_):
        image_url = f"/api/v1/perception/images/doc-1/{image_id}"
        return {
            "chunks": [{
                "chunk_text": f"![{image_url}%5D({image_url})",
                "source_locator": "doc.pdf|page:1",
                "document_id": "doc-1",
            }],
            "identity": {"user_id": "admin", "user_role": "admin"},
        }

    async def load_manifest(*_):
        return [{
            "image_id": image_id,
            "minio_object_key": f"derived/doc-1/images/{image_id}.jpg",
        }]

    runtime.storage_service.load_index_request = load
    runtime.storage_service.load_image_manifest = load_manifest

    _, _, skipped = asyncio.run(prepare_collection_recovery(runtime, "demand"))

    assert not [item for item in skipped if item["source_type"] == "image"]


def test_recovery_source_map_requires_one_draft_source_per_mysql_id():
    from admin.index_recovery import published_source_ids_by_mysql_id

    requests = [
        SimpleNamespace(
            chunks=[
                SimpleNamespace(chunk_role="agent_draft", mysql_id="1", source_id="source:a"),
                SimpleNamespace(chunk_role="admin_draft", mysql_id="2", source_id="source:b"),
                SimpleNamespace(chunk_role="agent_draft", mysql_id="2", source_id="source:c"),
                SimpleNamespace(chunk_role="mysql", mysql_id="3", source_id="source:ignored"),
                SimpleNamespace(chunk_role="agent_doc", mysql_id=None, source_id="source:doc"),
            ]
        )
    ]

    with pytest.raises(ValueError, match="source_id"):
        published_source_ids_by_mysql_id(requests)
    requests[0].chunks = requests[0].chunks[:2]
    assert published_source_ids_by_mysql_id(requests) == {"1": "source:a", "2": "source:b"}


@pytest.mark.parametrize("change", ["missing", "identity", "chunks", "version", "scope", "type"])
def test_recovery_rejects_missing_or_mismatched_version_source(change):
    from admin.index_recovery import prepare_collection_recovery

    runtime, _calls = _runtime()
    document = runtime.require_lifecycle().get_document("doc-1")
    original = runtime.storage_service.load_index_request

    async def load(*args):
        content = await original(*args)
        if change == "identity":
            content["identity"]["user_id"] = "another-user"
        elif change == "chunks":
            content["chunks"][0]["chunk_text"] = "   "
        elif change == "version":
            content["version_id"] = "old"
        elif change == "scope":
            content["scope_id"] = "private:another-user"
        elif change == "type":
            content["knowledge_type"] = "expert"
        return content

    if change == "missing":
        document["active_version"]["artifact_refs"] = {}
    runtime.storage_service.load_index_request = load
    requests, references, skipped = asyncio.run(
        prepare_collection_recovery(runtime, "demand")
    )
    assert requests == []
    assert all("object_checks" in reference for reference in references)
    assert len(skipped) == 1
    assert skipped[0]["source_id"] == "doc-1"


def test_minio_access_failure_aborts_recovery_instead_of_skipping_source():
    from admin.index_recovery import prepare_collection_recovery

    runtime, _ = _runtime()

    async def fail(*_):
        raise RuntimeError("MinIO unavailable")

    runtime.storage_service.load_index_request = fail
    with pytest.raises(RuntimeError, match="MinIO unavailable"):
        asyncio.run(prepare_collection_recovery(runtime, "demand"))


def test_preflight_reports_invalid_document_and_keeps_valid_document_recoverable():
    runtime, calls = _runtime()
    valid_document = runtime.require_lifecycle().get_document("doc-1")
    invalid_document = {
        **valid_document,
        "document_id": "doc-2",
        "active_version_id": "v2",
        "active_version": {
            "version_id": "v2",
            "publication_status": "PUBLISHED",
            "artifact_refs": {
                "confirmed_index_request": {
                    "storage_key": "derived/doc-2/tasks/bad/index-request.json",
                    "task_id": "bad",
                    "document_id": "doc-2",
                    "version_id": "v2",
                    "identity": {"user_id": "admin", "user_role": "admin"},
                }
            },
        },
    }
    runtime.require_lifecycle = lambda: SimpleNamespace(
        list_documents_page=lambda **_: {
            "documents": [valid_document, invalid_document],
            "total_pages": 1,
        },
        get_document=lambda document_id: {
            "doc-1": valid_document,
            "doc-2": invalid_document,
        }.get(document_id),
    )
    service = AdminService(runtime)
    result = asyncio.run(service.rebuild_collection("demand", True))
    assert result["skipped_count"] == 1
    assert result["skipped_sources"][0]["source_id"] == "doc-2"
    assert result["recoverable_count"] == 1
    assert calls == []

def test_plain_confirmed_document_can_recover_without_mysql_id():
    from admin.index_recovery import prepare_collection_recovery

    runtime, _ = _runtime()

    async def load(*_):
        return {
            "chunks": [{"chunk_text": "独立文档确认正文", "source_locator": "doc.pdf|page:1"}],
            "identity": {"user_id": "admin", "user_role": "admin"},
        }

    runtime.storage_service.load_index_request = load
    requests, _, _ = asyncio.run(prepare_collection_recovery(runtime, "demand"))
    assert requests[0].chunks[0].chunk_text == "独立文档确认正文"
    assert requests[0].chunks[0].mysql_id is None


def test_restore_requires_an_existing_confirmed_preflight_without_touching_milvus():
    runtime, calls = _runtime()
    service = AdminService(runtime)
    with pytest.raises(ValueError, match="有效的 preflight_id"):
        asyncio.run(service.restore_collection("demand", True, "missing", "a" * 64))
    assert calls == []
    assert runtime.task_store.list(operation="milvus_recovery") == []


def test_restore_rejects_changed_source_snapshot_before_creating_shadow():
    runtime, calls = _runtime()
    original_loader = runtime.storage_service.load_index_request
    loads = 0

    async def changed_after_preflight(*args):
        nonlocal loads
        loads += 1
        request = await original_loader(*args)
        if loads > 1:
            request["chunks"][0]["chunk_text"] = "changed after preflight"
        return request

    runtime.storage_service.load_index_request = changed_after_preflight
    service = AdminService(runtime)

    async def scenario():
        preflight = await service.rebuild_collection("demand", True)
        with pytest.raises(ValueError, match="来源发生变化"):
            await service.restore_collection(
                "demand",
                True,
                preflight["preflight_id"],
                preflight["missing_fingerprint"],
            )

    asyncio.run(scenario())
    assert not runtime.background_tasks
    assert calls == []
    assert not any(
        item["status"] == "QUEUED"
        for item in runtime.task_store.list(operation="milvus_recovery")
    )


@pytest.mark.parametrize("repeat_source", [False, True])
@pytest.mark.parametrize("verification_fails", [False, True, "zero_result", "partial_result", "source_changed"])
def test_restore_stops_for_admin_confirmation_after_shadow_validation(
    verification_fails, repeat_source, monkeypatch
):
    runtime, calls = _runtime()
    if repeat_source:
        from admin.index_recovery import prepare_collection_recovery

        async def prepare(*args):
            requests, references, skipped = await prepare_collection_recovery(*args)
            paired = references[:len(requests)]
            return requests + requests, paired + paired + references[len(requests):], skipped

        monkeypatch.setattr("admin.mysql_sync.prepare_collection_recovery", prepare)
    active = runtime.embedded_retrieval.agent.public_milvus["demand"]
    event_order = []

    class Shadow:
        collection_name = "demand__recovery_test"
        alias_name = None
        rows = 0

        def ensure_collection(self):
            event_order.append("create")

        def insert_chunks(self, *_args, **_kwargs):
            self.rows += 1
            return 1

        def flush(self):
            event_order.append("flush")

        async def check_ready(self):
            event_order.append("ready")
            if verification_fails is True:
                raise RuntimeError("shadow load failed")

        def count_entities(self):
            return self.rows

        def existing_ids(self, ids):
            return set(ids)

    shadow = Shadow()
    active._maintenance_guard = lambda: nullcontext()
    active._resolve_collection_name = lambda: "demand__g_old"
    active.new_shadow_generation = lambda: shadow

    active.activate_shadow_generation = lambda target: event_order.append("switch")
    active.new_shadow_generation = lambda collection_name=None: shadow

    async def index(request, *, target_override, insert_only):
        assert target_override is shadow and insert_only is True
        target_override.rows = 0 if verification_fails == "zero_result" else 1
        event_order.append("insert")
        if verification_fails == "source_changed":
            original_loader = runtime.storage_service.load_index_request

            async def changed_loader(*args):
                loaded = await original_loader(*args)
                loaded["chunks"][0]["chunk_text"] = "changed during recovery"
                return loaded

            runtime.storage_service.load_index_request = changed_loader
        return SimpleNamespace(
            status="SUCCESS",
            indexed_count=0 if verification_fails in {"zero_result", "partial_result"} else 1,
            indexed_chunks=[] if verification_fails == "zero_result" else request.chunks,
        )

    runtime.embedded_retrieval.agent.index = index
    service = AdminService(runtime)

    async def scenario():
        preflight = await service.rebuild_collection("demand", True)
        queued = await service.restore_collection(
            "demand",
            True,
            preflight["preflight_id"],
            preflight["missing_fingerprint"],
        )
        await asyncio.gather(*runtime.background_tasks)
        return queued

    queued = asyncio.run(scenario())
    task = runtime.task_store.get(queued["task_id"])
    if verification_fails:
        assert task["status"] == "FAILED"
        assert "switch" not in event_order
    else:
        assert task["status"] == "READY_TO_SWITCH"
        assert task["active_generation"] == "demand__g_old"
        assert task["shadow_generation"] == "demand__recovery_test"
        assert "switch" not in event_order
        assert event_order.index("insert") < event_order.index("flush") < event_order.index("ready")
    assert calls == []


@pytest.mark.parametrize("switch_fault", [None, "alias_error", "after_switch_error", "reset_error"])
def test_admin_confirmation_revalidates_then_switches_ready_shadow(switch_fault):
    runtime, calls = _runtime(reset_error=switch_fault == "reset_error")
    active = runtime.embedded_retrieval.agent.public_milvus["demand"]
    event_order = []
    current_generation = "demand__g_old"
    active.legacy_collection_name = "demand"
    active.has_physical_collection = lambda name: name.startswith("demand__backup_")

    class Shadow:
        collection_name = "demand__recovery_test"
        alias_name = None
        rows = 0

        def ensure_collection(self):
            event_order.append("create")

        def insert_chunks(self, *_args, **_kwargs):
            self.rows += 1
            return 1

        def flush(self):
            event_order.append("flush")

        async def check_ready(self):
            event_order.append("ready")

        def count_entities(self):
            return self.rows

        def existing_ids(self, ids):
            return set(ids)

    shadow = Shadow()
    active._maintenance_guard = lambda: nullcontext()
    active._resolve_collection_name = lambda: current_generation
    active.new_shadow_generation = lambda collection_name=None: shadow

    def switch(target, *, backup_collection_name, displaced_collection_name):
        nonlocal current_generation
        event_order.append("switch")
        assert target is shadow
        assert backup_collection_name.startswith("demand__backup_")
        assert displaced_collection_name.startswith("demand__backup_stale_")
        if switch_fault == "alias_error":
            raise RuntimeError("alias switch rejected")
        current_generation = "demand"
        if switch_fault == "after_switch_error":
            raise RuntimeError("alias changed but acknowledgement failed")
        return {"previous": backup_collection_name, "active": current_generation}

    active.activate_shadow_generation = switch

    async def index(request, *, target_override, insert_only):
        assert target_override is shadow and insert_only is True
        target_override.rows += 1
        event_order.append("insert")
        return SimpleNamespace(status="SUCCESS", indexed_count=1, indexed_chunks=request.chunks)

    runtime.embedded_retrieval.agent.index = index
    service = AdminService(runtime)

    async def scenario():
        preflight = await service.rebuild_collection("demand", True)
        queued = await service.restore_collection(
            "demand", True, preflight["preflight_id"], preflight["missing_fingerprint"]
        )
        await asyncio.gather(*runtime.background_tasks)
        ready = runtime.task_store.get(queued["task_id"])
        assert ready["status"] == "READY_TO_SWITCH"
        assert "switch" not in event_order

        submitted = await service.activate_collection_recovery("demand", ready["task_id"], True)
        assert submitted["status"] == "SWITCHING"
        await asyncio.gather(*runtime.background_tasks)
        return runtime.task_store.get(ready["task_id"])

    task = asyncio.run(scenario())
    if switch_fault == "alias_error":
        assert task["status"] == "FAILED"
        assert current_generation == "demand__g_old"
        assert calls == []
        return
    if switch_fault == "reset_error":
        assert task["status"] == "SWITCHING"
        assert task["error_code"] == "ACTIVATION_RECONCILIATION_FAILED"
        assert current_generation == "demand"
        assert calls == ["reset", "reset"]
        return
    assert task["status"] == "SUCCESS"
    assert task["previous_generation"].startswith("demand__backup_")
    assert task["active_generation"] == "demand"
    assert task["backup_generation"] == task["previous_generation"]
    assert event_order.count("switch") == 1
    assert calls == ["reset"]


@pytest.mark.parametrize("change", ["sources", "active_alias", "shadow_rows", "shadow_keys"])
def test_admin_confirmation_revalidates_sources_alias_and_shadow_before_switch(change):
    runtime, _ = _runtime()
    active = runtime.embedded_retrieval.agent.public_milvus["demand"]
    current_generation = "demand__g_old"
    source_changed = False
    original_loader = runtime.storage_service.load_index_request

    async def load(*args):
        request = await original_loader(*args)
        if source_changed and change == "sources":
            request["chunks"][0]["chunk_text"] = "changed after shadow restore"
        return request

    runtime.storage_service.load_index_request = load

    class Shadow:
        collection_name = "demand__recovery_test"
        alias_name = None
        rows = 0

        def ensure_collection(self):
            pass

        def insert_chunks(self, *_args, **_kwargs):
            self.rows += 1
            return 1

        def flush(self):
            pass

        async def check_ready(self):
            pass

        def count_entities(self):
            return self.rows

        def existing_ids(self, ids):
            return set() if source_changed and change == "shadow_keys" else set(ids)

    shadow = Shadow()
    active._maintenance_guard = lambda: nullcontext()
    active._resolve_collection_name = lambda: current_generation
    active.new_shadow_generation = lambda collection_name=None: shadow
    active.activate_shadow_generation = lambda target: pytest.fail("stale shadow must not be activated")

    async def index(request, *, target_override, insert_only):
        target_override.rows += 1
        return SimpleNamespace(status="SUCCESS", indexed_count=1, indexed_chunks=request.chunks)

    runtime.embedded_retrieval.agent.index = index
    service = AdminService(runtime)

    async def scenario():
        nonlocal source_changed
        nonlocal current_generation
        preflight = await service.rebuild_collection("demand", True)
        queued = await service.restore_collection(
            "demand", True, preflight["preflight_id"], preflight["missing_fingerprint"]
        )
        await asyncio.gather(*runtime.background_tasks)
        if change == "sources":
            source_changed = True
        elif change == "active_alias":
            current_generation = "demand__g_external"
        elif change == "shadow_rows":
            shadow.rows += 1
        else:
            source_changed = True
        await service.activate_collection_recovery("demand", queued["task_id"], True)
        await asyncio.gather(*runtime.background_tasks)
        return runtime.task_store.get(queued["task_id"])

    task = asyncio.run(scenario())
    assert task["status"] == "FAILED"
    expected_errors = {
        "sources": "RECOVERY_SNAPSHOT_CHANGED",
        "active_alias": "ACTIVE_ALIAS_CHANGED",
        "shadow_rows": "SHADOW_VALIDATION_FAILED",
        "shadow_keys": "SHADOW_VALIDATION_FAILED",
    }
    assert task["error_code"] == expected_errors[change]
    assert current_generation != "demand__recovery_test"


def test_preflight_report_is_retained_in_postgres_task_state():
    from sqlalchemy import create_engine

    from services.persistence.repositories.runtime_repository import PostgresStateStore
    from services.persistence.sqlalchemy import metadata

    engine = create_engine("sqlite://")
    metadata.create_all(engine)
    store = PostgresStateStore(service="supervisor", engine=engine)
    report = {"recoverable_count": 2, "skipped_sources": [{"source_id": "doc-1"}]}
    task_id = store.create(
        operation="milvus_recovery",
        status="PREFLIGHT_COMPLETE",
        state={
            "knowledge_type": "demand",
            "recovery_result": report,
            "snapshot_fingerprint": "snapshot-fingerprint",
            "accepted_missing_fingerprint": "missing-fingerprint",
            "active_generation": "demand__g_old",
            "shadow_generation": None,
            "backup_generation": "demand__backup_test",
            "displaced_generation": "demand__backup_stale_test",
        },
    )
    store.update(
        task_id,
        shadow_generation="demand__recovery_test",
        expected_ids=["chunk-1", "chunk-2"],
    )
    task = store.get(task_id)
    assert task["snapshot_fingerprint"] == "snapshot-fingerprint"
    assert task["accepted_missing_fingerprint"] == "missing-fingerprint"
    assert task["active_generation"] == "demand__g_old"
    assert task["shadow_generation"] == "demand__recovery_test"
    assert task["expected_ids"] == ["chunk-1", "chunk-2"]
    assert task["recovery_result"] == report
    assert task["backup_generation"] == "demand__backup_test"
    assert task["displaced_generation"] == "demand__backup_stale_test"
    assert store.list_page(operation="milvus_recovery", page=1, page_size=10)["items"][0][
        "recovery_result"
    ] == report
    engine.dispose()


def test_published_draft_keeps_business_fields_when_attachment_is_withdrawn():
    from admin.index_recovery import prepare_collection_recovery

    runtime, _ = _runtime()
    draft = {
        "draft_id": "d1",
        "draft_type": "demand",
        "status": "PUBLISHED",
        "user_id": "admin",
        "user_role": "admin",
        "draft": {
            "title": "需求",
            "fields": {"contect": "正文"},
            "mysql_record_id": "1",
            "attachments": [{"document_id": "doc-1", "version_id": "v1"}],
            "source_blocks": [{"text": "附件原文"}],
        },
    }
    runtime.require_draft_store = lambda: SimpleNamespace(
        list_page=lambda **_: {"items": [draft], "total_pages": 1}
    )
    runtime.require_lifecycle = lambda: SimpleNamespace(
        list_documents_page=lambda **_: {
            "documents": [
                {
                    "document_id": "doc-1",
                    "active_version": {"version_id": "v1", "publication_status": "UNPUBLISHED"},
                }
            ],
            "total_pages": 1,
        }
    )
    requests, references, skipped = asyncio.run(prepare_collection_recovery(runtime, "demand"))
    assert len(requests) == 1
    assert [chunk.chunk_role for chunk in requests[0].chunks] == ["admin_draft"]
    assert references[0]["draft_id"] == "d1"
    assert {item["source_id"] for item in skipped} == {"doc-1"}


def test_recovery_routes_require_confirmation_and_expose_read_only_preflight():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from admin.api.v1.admin import build_admin_router

    runtime, calls = _runtime()
    service = AdminService(runtime)
    runtime.require_admin = lambda: service
    runtime.require_file_workflow = lambda: SimpleNamespace(status=runtime.task_store.get)
    app = FastAPI()
    app.include_router(build_admin_router(runtime))
    with TestClient(app) as client:
        root = "/api/v1/admin/knowledge/collections/demand"
        assert client.post(root + "/rebuild").status_code == 422
        assert client.post(root + "/restore").status_code == 422
        assert client.post(root + "/activate").status_code == 422
        assert calls == []
        result = client.post(root + "/rebuild?confirm=true")
        assert result.status_code == 200
        assert result.json()["status"] == "PREFLIGHT_COMPLETE"
        task = client.get(result.json()["status_url"])
        assert task.status_code == 200
        assert task.json()["operation"] == "milvus_recovery"
        assert task.json()["status"] == "PREFLIGHT_COMPLETE"
        assert task.json()["recovery_result"]["recoverable_count"] == 1
        assert client.post(root + "/restore?confirm=true").status_code == 422
        assert client.post(
            root + "/restore",
            json={
                "confirm": True,
                "preflight_id": "missing",
                "accepted_missing_fingerprint": "a" * 64,
            },
        ).status_code == 422
        assert calls == []


def test_activate_route_passes_explicit_admin_confirmation_to_service():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from admin.api.v1.admin import build_admin_router

    calls = []

    async def activate(knowledge_type, task_id, confirm):
        calls.append((knowledge_type, task_id, confirm))
        return {"status": "SWITCHING", "task_id": task_id}

    runtime = SimpleNamespace(
        require_admin=lambda: SimpleNamespace(activate_collection_recovery=activate)
    )
    app = FastAPI()
    app.include_router(build_admin_router(runtime))

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/admin/knowledge/collections/solution/activate",
            json={"confirm": True, "task_id": "recovery-1"},
        )

    assert response.status_code == 200
    assert response.json() == {"status": "SWITCHING", "task_id": "recovery-1"}
    assert calls == [("solution", "recovery-1", True)]


@pytest.mark.parametrize(
    ("endpoint", "service_method"),
    [("rebuild", "rebuild_collection"), ("restore", "restore_collection")],
)
def test_recovery_route_logs_preflight_failure(caplog, endpoint, service_method):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from admin.api.v1.admin import build_admin_router

    async def fail_request(*_args):
        raise RuntimeError("milvus preflight secret detail")

    service = SimpleNamespace(**{service_method: fail_request})
    runtime = SimpleNamespace(require_admin=lambda: service)
    app = FastAPI()
    app.include_router(build_admin_router(runtime))

    with TestClient(app) as client:
        response = client.post(
            f"/api/v1/admin/knowledge/collections/solution/{endpoint}?confirm=true",
            json=(
                {
                    "confirm": True,
                    "preflight_id": "preflight-1",
                    "accepted_missing_fingerprint": "a" * 64,
                }
                if endpoint == "restore"
                else None
            ),
        )

    assert response.status_code == 503
    assert "solution" in caplog.text
    assert "RuntimeError: milvus preflight secret detail" in caplog.text


def test_recovery_requests_fail_fast_when_background_writer_holds_gate():
    from admin.mysql_sync import _MYSQL_SYNC_GATE

    runtime, calls = _runtime()
    service = AdminService(runtime)
    with _MYSQL_SYNC_GATE:
        with pytest.raises(RuntimeError, match="写入任务"):
            asyncio.run(
                service.start_mysql_sync(SimpleNamespace(knowledge_type="demand", batch_size=64))
            )
        with pytest.raises(RuntimeError, match="写入任务"):
            asyncio.run(service.restore_collection("demand", True))
    assert calls == []


def test_restart_marks_recovery_interrupted_and_does_not_launch_writes():
    runtime, calls = _runtime()
    task = runtime.task_store.create(
        operation="milvus_recovery", status="RUNNING", knowledge_type="demand"
    )
    asyncio.run(AdminService(runtime).resume_mysql_syncs())
    assert runtime.task_store.get(task["task_id"])["status"] == "INTERRUPTED"
    assert calls == []


@pytest.mark.parametrize("alias_switched", [False, True])
def test_restart_reconciles_recovery_interrupted_during_alias_switch(alias_switched):
    runtime, calls = _runtime()
    active = runtime.embedded_retrieval.agent.public_milvus["demand"]
    active._resolve_collection_name = lambda: (
        "demand__g_new" if alias_switched else "demand__g_old"
    )
    task = runtime.task_store.create(
        operation="milvus_recovery",
        status="SWITCHING",
        knowledge_type="demand",
        active_generation="demand__g_old",
        shadow_generation="demand__g_new",
    )

    asyncio.run(AdminService(runtime).resume_mysql_syncs())

    recovered = runtime.task_store.get(task["task_id"])
    if alias_switched:
        assert recovered["status"] == "SUCCESS"
        assert recovered["active_generation"] == "demand__g_new"
        assert recovered["previous_generation"] == "demand__g_old"
    else:
        assert recovered["status"] == "INTERRUPTED"
        assert recovered["active_generation"] == "demand__g_old"
    assert calls == (["reset"] if alias_switched else [])


def test_restart_does_not_report_success_when_switched_checkpoint_reset_fails():
    runtime, calls = _runtime(reset_error=True)
    active = runtime.embedded_retrieval.agent.public_milvus["demand"]
    active._resolve_collection_name = lambda: "demand__g_new"
    task = runtime.task_store.create(
        operation="milvus_recovery", status="SWITCHING", knowledge_type="demand",
        active_generation="demand__g_old", shadow_generation="demand__g_new",
    )

    asyncio.run(AdminService(runtime).resume_mysql_syncs())

    recovered = runtime.task_store.get(task["task_id"])
    assert recovered["status"] == "SWITCHING"
    assert recovered["error_code"] == "ACTIVATION_RECONCILIATION_FAILED"
    assert "checkpoint write failed" in recovered["error"]
    assert calls == ["reset"]


def test_restart_reconciles_canonical_promotion_using_persisted_backup_name():
    runtime, calls = _runtime()
    active = runtime.embedded_retrieval.agent.public_milvus["demand"]
    active.legacy_collection_name = "demand"
    active._resolve_collection_name = lambda: "demand"
    active.has_physical_collection = lambda name: name == "demand__backup_saved"
    task = runtime.task_store.create(
        operation="milvus_recovery",
        status="SWITCHING",
        knowledge_type="demand",
        active_generation="demand__g_old",
        shadow_generation="demand__recovery_new",
        backup_generation="demand__backup_saved",
    )

    asyncio.run(AdminService(runtime).resume_mysql_syncs())

    recovered = runtime.task_store.get(task["task_id"])
    assert recovered["status"] == "SUCCESS"
    assert recovered["active_generation"] == "demand"
    assert recovered["previous_generation"] == "demand__backup_saved"
    assert calls == ["reset"]


def test_read_only_preflight_allows_normal_mysql_sync():
    runtime, calls = _runtime()
    target = runtime.embedded_retrieval.agent.public_milvus["demand"]
    target.count_by_mysql_records = lambda _: 0
    runtime.sync_state_store.get = lambda *_: None
    service = AdminService(runtime)
    service._launch_mysql_sync = lambda *_: None

    async def scenario():
        await service.rebuild_collection("demand", True)
        return await service.start_mysql_sync(SimpleNamespace(knowledge_type="demand", batch_size=64))

    result = asyncio.run(scenario())
    assert result["status"] == "QUEUED"
    assert calls == []


def test_new_preflight_does_not_hide_running_recovery_from_write_guard():
    runtime, _ = _runtime()
    recovery = runtime.task_store.create(
        operation="milvus_recovery", status="RUNNING", knowledge_type="demand",
    )
    runtime.task_store.create(
        operation="milvus_recovery", status="PREFLIGHT_COMPLETE", knowledge_type="demand",
    )
    service = AdminService(runtime)
    assert service._collection_recovery_task("demand")["task_id"] == recovery["task_id"]
    with pytest.raises(RuntimeError, match="尚未完成索引恢复"):
        service._start_mysql_sync(SimpleNamespace(knowledge_type="demand", batch_size=64))


@pytest.mark.parametrize("change", ["status", "content"])
def test_draft_source_is_revalidated_without_business_writes(change):
    from admin.index_recovery import validate_recovery_source
    from services.application.draft_publishing import build_published_draft_index_request

    runtime, _ = _runtime()
    draft = {
        "draft_id": "d1",
        "draft_type": "demand",
        "status": "PUBLISHED",
        "user_id": "admin",
        "user_role": "admin",
        "updated_at": "t1",
        "draft": {"title": "需求", "fields": {"contect": "确认正文"}, "mysql_record_id": "1"},
    }
    runtime.require_draft_store = lambda: SimpleNamespace(get_admin=lambda _: draft)
    request = build_published_draft_index_request(draft)
    reference = {"draft_id": "d1", "updated_at": "t1"}
    validate_recovery_source(runtime, request, reference)
    if change == "status":
        draft["status"] = "WITHDRAWN"
    else:
        draft["draft"]["fields"]["contect"] = "已修改正文"
    with pytest.raises(RuntimeError, match="来源已变化"):
        validate_recovery_source(runtime, request, reference)
