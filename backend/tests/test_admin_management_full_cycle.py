"""Full management application flow with real SQLite/state stores and fake external IO."""
import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine

from admin.service import AdminService
from agents.perception_agent import PerceptionAgent
from agents.retrieval_agent.schemas import IndexChunk
from core.contracts.workflows import FileConfirmRequest, UnifiedDeleteRequest
from runtime.workflows.file_workflow import FileWorkflowService
from runtime.workflows.task_store import SupervisorTaskStore
from services.persistence.document_lifecycle import DocumentLifecycleStore
from services.persistence.postgres import InMemoryDraftStore
from services.persistence.sqlalchemy import metadata


class Result:
    def __init__(self, status="SUCCESS", *, chunks=()):
        self.status = status
        self.error = None
        self.indexed_chunks = list(chunks)
        self.indexed_count = len(self.indexed_chunks)

    def model_dump(self):
        return {"status": self.status, "indexed_count": self.indexed_count}


class Storage:
    def __init__(self):
        self.objects = set()
        self.parsed = {}

    async def check_ready(self):
        pass

    async def inspect_object(self, key):
        return {"storage_key": key, "size": 10} if key in self.objects else None

    async def load_parse_result(self, document_id):
        return self.parsed.get(document_id)

    async def load_image_manifest(self, document_id):
        return []

    async def delete_original_file(self, storage_key):
        self.objects.discard(storage_key)

    async def delete_stored_document_internal(self, *, storage_key, document_id):
        self.objects.discard(storage_key)
        self.parsed.pop(document_id, None)


class BusinessSink:
    def __init__(self):
        self.rows = {}

    def find_by_id(self, *, record_type, record_id):
        return record_id if record_id in self.rows else None

    def save(self, request):
        self.rows["business-1"] = dict(request.structured_data)
        return {"business_record_id": "business-1"}

    def delete(self, *, record_type, record_id, user_id):
        self.rows.pop(record_id, None)
        return {"status": "DELETED"}


class Retrieval:
    def __init__(self):
        self.chunks = []
        self.searchable = False
        self.public_milvus = {"solution": self}

    async def preflight_publish_document(self, document_id, request):
        assert request.records

    async def publish_document(self, document_id, request):
        self.chunks = [IndexChunk(
            chunk_text=record.text, document_id=record.document_id,
            source_locator=record.source_locator, mysql_id=record.mysql_id,
            source_id=record.source_id, chunk_role=record.metadata["chunk_role"],
            metadata=record.metadata,
        ) for record in request.records]
        self.searchable = True
        return Result(chunks=self.chunks)

    async def unpublish_document(self, document_id, request):
        selected = [chunk for chunk in self.chunks if chunk.document_id == document_id]
        self.searchable = False
        if request.remove_vectors:
            self.chunks = [chunk for chunk in self.chunks if chunk.document_id != document_id]
        return Result(chunks=selected)

    async def republish_document(self, document_id, request):
        selected = [chunk for chunk in self.chunks if chunk.document_id == document_id]
        self.searchable = bool(selected)
        return Result(chunks=selected)

    def delete_by_mysql_id(self, mysql_id, **kwargs):
        self.chunks = [chunk for chunk in self.chunks if chunk.mysql_id != mysql_id]


@pytest.mark.parametrize("direct_delete", [False, True])
def test_admin_upload_parse_extract_publish_withdraw_restore_and_delete(monkeypatch, direct_delete):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr("services.application.draft_publishing.PostgresAuditSink", lambda: SimpleNamespace(record=lambda *_a, **_kw: None))
    engine = create_engine("sqlite://")
    metadata.create_all(engine)
    lifecycle = DocumentLifecycleStore(engine=engine)
    drafts, tasks, storage = InMemoryDraftStore(), SupervisorTaskStore(), Storage()
    sink, retrieval = BusinessSink(), Retrieval()

    class Adapter:
        async def upload_document(self, arguments):
            storage.objects.add("uploads/current.pdf")
            return {"status": "SUCCESS", "document_id": "doc-cycle", "version_id": "doc-cycle:v1", "version": 1, "storage_key": "uploads/current.pdf", "file_name": "测试.pdf"}

        async def parse_document(self, arguments):
            result = {"status": "SUCCESS", "document_id": "doc-cycle", "version_id": "doc-cycle:v1", "storage_key": "uploads/current.pdf", "raw_text": "测试方案的完整技术原文", "blocks": [{"text": "测试方案的完整技术原文"}], "metadata": {"images": []}}
            storage.parsed["doc-cycle"] = result
            return result

    async def extract(draft, **kwargs):
        return {**draft, "title": "人工确认的方案", "fields": {"title": "人工确认的方案", "contect": "人工确认的方案描述"}}

    monkeypatch.setattr(PerceptionAgent, "extract_fields", extract)
    workflow = FileWorkflowService(Adapter(), task_store=tasks, lifecycle_store=lifecycle, draft_store=drafts, storage_service=storage)
    monkeypatch.setattr(workflow, "_audit", lambda *_a, **_kw: None)
    runtime = SimpleNamespace(
        task_store=tasks, task_runner=None, outbox_repository=None, sync_state_store=None,
        lifecycle_service=lifecycle, draft_store_service=drafts, storage_service=storage,
        business_record_sink=sink, embedded_perception=storage,
        embedded_retrieval=SimpleNamespace(agent=retrieval),
        require_lifecycle=lambda: lifecycle, require_file_workflow=lambda: workflow,
        require_draft_store=lambda: drafts, require_business_record_sink=lambda: sink,
        require_conversation=lambda: SimpleNamespace(session_store=SimpleNamespace(get=lambda _: None, upsert=lambda *_a, **_kw: None)),
    )
    admin = AdminService(runtime)

    async def scenario():
        uploaded = await admin.upload_file(SimpleNamespace(filename="测试.pdf"), knowledge_type="solution")
        assert lifecycle.get_version(uploaded["version_id"])["publication_status"] == "UNPUBLISHED"
        assert retrieval.chunks == [] and sink.rows == {}
        parsed = await workflow.parse(uploaded["task_id"])
        assert parsed["status"] == "WAITING_CONFIRM"
        assert retrieval.chunks == [] and sink.rows == {}
        generated = await workflow.regenerate_draft(uploaded["task_id"])
        assert generated["draft_status"] == "READY"
        published = await admin.confirm_task(uploaded["task_id"], FileConfirmRequest(draft=generated["draft_preview"]))
        assert published["status"] == "SUCCEEDED"
        draft = drafts.get(generated["draft_id"], user_id="admin")
        assert draft["status"] == "PUBLISHED"
        assert sink.rows["business-1"]["contect"] == "人工确认的方案描述"
        assert any(chunk.document_id == "doc-cycle" for chunk in retrieval.chunks)
        assert retrieval.searchable
        version_id = uploaded["version_id"]
        withdrawn, _ = await admin.lifecycle_action("unpublish", version_id, "admin")
        assert withdrawn["publication_status"] == "UNPUBLISHED" and not retrieval.searchable
        assert storage.objects and sink.rows and drafts.get(generated["draft_id"], user_id="admin")
        restored, republish_task = await admin.lifecycle_action("publish", version_id, "admin")
        assert restored is not None, republish_task
        assert restored["publication_status"] == "PUBLISHED" and retrieval.searchable
        recycled, _ = await admin.lifecycle_action("recycle", version_id, "admin")
        assert recycled["status"] == "RECYCLED" and not retrieval.searchable
        restored, restore_task = await admin.lifecycle_action("restore", version_id, "admin")
        assert restored is not None, restore_task
        assert restored["publication_status"] == "PUBLISHED" and retrieval.searchable
        if direct_delete:
            await admin.delete_document("doc-cycle", UnifiedDeleteRequest(
                user_id="admin", storage_key="uploads/current.pdf", knowledge_type="solution",
            ))
        else:
            await admin.lifecycle_action("recycle", version_id, "admin")
            purged, _ = await admin.lifecycle_action("purge", version_id, "admin")
            assert purged["status"] == "PURGED"
        assert lifecycle.get_version(version_id)["status"] == "PURGED"
        assert drafts.get(generated["draft_id"], user_id="admin")["status"] == "DELETED"
        assert sink.rows == {} and retrieval.chunks == []
        assert storage.objects == set() and storage.parsed == {}

    try:
        asyncio.run(scenario())
    finally:
        engine.dispose()
