import asyncio
from types import SimpleNamespace
from urllib.parse import unquote

import pytest
from fastapi import HTTPException

from core.contracts.workflows import (
    DraftPublishRequest,
)
from core.contracts.knowledge import CleanedKnowledgeRecord, DocumentKnowledgeRequest
from core.governance.access_policy import ManualIdentity
from services.application.draft_publishing import (
    DraftPublishingApplicationService,
    _normalize_and_preflight_images,
)
from services.persistence.postgres import InMemoryDraftStore


class DraftStore:
    def __init__(self):
        self.record = {
            "draft_id": "draft-1",
            "user_id": "user-1",
            "user_role": "admin",
            "thread_id": "file-admin",
            "status": "CONFIRMED",
            "draft_type": "solution",
            "draft": {
                "draft_type": "solution",
                "title": "测试方案",
                "fields": {"contect": "方案内容"},
                "attachments": [
                    {
                        "document_id": "doc-1",
                        "version_id": "doc-1:v1",
                        "file_name": "测试.pdf",
                    }
                ],
                "source_blocks": [{"text": "解析原文"}],
            },
        }

    def get(self, _draft_id, *, user_id):
        return self.record if user_id == self.record["user_id"] else None

    def list_for_source_id(self, _source_id):
        return []

    def update(self, _draft_id, *, user_id, draft, status=None, **_kwargs):
        assert user_id == self.record["user_id"]
        self.record["draft"].update(draft)
        if status is not None:
            self.record["status"] = status
        return self.record

    def transition(self, _draft_id, *, user_id, target_status, **_kwargs):
        assert user_id == self.record["user_id"]
        self.record["status"] = target_status
        return self.record


class Lifecycle:
    def __init__(self):
        self.updates = []

    def get_document(self, document_id):
        return {
            "document_id": document_id,
            "user_id": "user-1",
            "scope_id": "public",
            "knowledge_type": "solution",
            "active_version": {"version_id": f"{document_id}:v1"},
        }

    def get_version(self, version_id):
        return {
            "version_id": version_id, "document_id": "doc-1", "version": 1,
            "storage_key": "uploads/doc-1.pdf", "artifact_refs": {},
        }

    def update_version(self, version_id, **fields):
        self.updates.append((version_id, fields))
        return {"version_id": version_id, **fields}


class Retrieval:
    def __init__(self):
        self.request = None
        self.preflight_request = None

    async def preflight_publish_document(self, _document_id, request):
        self.preflight_request = request

    async def publish_document(self, _document_id, _request):
        from agents.retrieval_agent.schemas import IndexChunk
        self.request = _request
        return SimpleNamespace(
            status="SUCCESS",
            indexed_count=2,
            indexed_chunks=[IndexChunk(
                chunk_text=record.text, document_id=record.document_id,
                source_locator=record.source_locator, mysql_id=record.mysql_id,
                source_id=record.source_id, chunk_role=record.metadata["chunk_role"],
                metadata=record.metadata,
            ) for record in _request.records],
            model_dump=lambda: {"status": "SUCCESS", "indexed_count": 2},
        )


class Storage:
    async def check_ready(self):
        return None

    async def inspect_object(self, storage_key):
        return {"storage_key": storage_key}

    async def load_image_manifest(self, _document_id):
        return []


def test_publish_image_preflight_checks_metadata_ids_and_document_ownership():
    class ImageStorage:
        checked = []

        async def inspect_object(self, key):
            self.checked.append(key)
            return {"storage_key": key}

    request = DocumentKnowledgeRequest(
        source_name="测试.pdf",
        knowledge_type="solution",
        identity=ManualIdentity(user_id="user-1", user_role="admin"),
        records=[CleanedKnowledgeRecord(
            record_id="record-1",
            document_id="doc-1",
            text="有图片",
            metadata={"image_ids": ["image-1"]},
        )],
    )
    storage = ImageStorage()

    asyncio.run(_normalize_and_preflight_images(
        request,
        {"doc-1": [{"image_id": "image-1", "minio_object_key": "derived/doc-1/image.jpg"}]},
        storage,
        "doc-1",
    ))

    assert storage.checked == ["derived/doc-1/image.jpg"]


def test_publishing_attached_draft_updates_document_version_lifecycle(monkeypatch):
    monkeypatch.setattr(
        "services.application.draft_publishing.PostgresAuditSink",
        lambda: SimpleNamespace(record=lambda *_args, **_kwargs: None),
    )
    draft_store = DraftStore()
    lifecycle = Lifecycle()
    session_upserts = []
    retrieval = Retrieval()

    class SessionStore:
        def get(self, _thread_id):
            return None

        def upsert(self, thread_id, **fields):
            session_upserts.append((thread_id, fields))

    runtime = SimpleNamespace(
        require_draft_store=lambda: draft_store,
        require_business_record_sink=lambda: SimpleNamespace(
            save=lambda _payload: {"business_record_id": "mysql-1"}
        ),
        require_lifecycle=lambda: lifecycle,
        storage_service=Storage(),
        embedded_retrieval=SimpleNamespace(agent=retrieval),
        require_conversation=lambda: SimpleNamespace(session_store=SessionStore()),
    )

    asyncio.run(
        DraftPublishingApplicationService(
            runtime, lambda user_id: (user_id, "admin", None)
        ).publish("draft-1", DraftPublishRequest(user_id="user-1"))
    )

    assert len(lifecycle.updates) == 1
    version_id, fields = lifecycle.updates[0]
    assert version_id == "doc-1:v1"
    assert fields == {
        "status": "INDEXED",
        "publication_status": "PUBLISHED",
        "index_status": "INDEXED",
        "chunk_count": 1,
        "indexed_count": 1,
        "error": None,
        "unpublish_reason_code": None,
        "unpublish_reason_text": None,
        "unpublished_by": None,
        "unpublished_at": None,
        "expected_version": 1,
    }
    assert session_upserts
    assert draft_store.record["draft"]["_index_snapshot"]["indexed_count"] == 2
    assert all(fields["user_id"] == "user-1" for _, fields in session_upserts)
    assert all(fields["user_role"] == "admin" for _, fields in session_upserts)
    assert len(retrieval.request.records) == 2
    assert len(retrieval.preflight_request.records) == 2
    assert retrieval.request.records[0].source_id == retrieval.request.records[1].source_id
    assert retrieval.request.records[0].source_locator.startswith(
        "mysql:xp_pub_tech_solution|type:record|id:mysql-1|attachments:"
    )
    assert "测试.pdf" in unquote(retrieval.request.records[0].source_locator)
    assert retrieval.request.records[1].source_locator == "测试.pdf|type:text|attachment:doc-1"
    assert len(retrieval.request.records[0].source_locator.encode("utf-8")) <= 512


def test_missing_attachment_object_blocks_before_mysql_or_index_write(monkeypatch):
    monkeypatch.setattr(
        "services.application.draft_publishing.PostgresAuditSink",
        lambda: SimpleNamespace(record=lambda *_args, **_kwargs: None),
    )
    draft_store = DraftStore()
    lifecycle = Lifecycle()
    retrieval = Retrieval()
    mysql_calls = []

    class MissingStorage(Storage):
        async def inspect_object(self, _storage_key):
            return None

    runtime = SimpleNamespace(
        require_draft_store=lambda: draft_store,
        require_business_record_sink=lambda: SimpleNamespace(
            save=lambda *_args, **_kwargs: mysql_calls.append("save"),
            update=lambda **_kwargs: mysql_calls.append("update"),
        ),
        require_lifecycle=lambda: lifecycle,
        storage_service=MissingStorage(),
        embedded_retrieval=SimpleNamespace(agent=retrieval),
        require_conversation=lambda: SimpleNamespace(
            session_store=SimpleNamespace(get=lambda _thread_id: None, upsert=lambda *_a, **_k: None)
        ),
    )

    with pytest.raises(HTTPException, match="原件在 MinIO 中不可读取"):
        asyncio.run(DraftPublishingApplicationService(
            runtime, lambda user_id: (user_id, "admin", None)
        ).publish("draft-1", DraftPublishRequest(user_id="user-1")))

    assert mysql_calls == []
    assert retrieval.preflight_request is None
    assert draft_store.record["status"] == "CONFIRMED"


def test_publishing_reuses_mysql_record_from_published_source_group(monkeypatch):
    monkeypatch.setattr(
        "services.application.draft_publishing.PostgresAuditSink",
        lambda: SimpleNamespace(record=lambda *_args, **_kwargs: None),
    )
    draft_store = DraftStore()
    draft_store.record["draft"]["source_id"] = "source:doc-1"
    draft_store.record["draft"]["attachments"] = []
    draft_store.record["draft"]["source_blocks"] = []
    draft_store.list_for_source_id = lambda source_id: [
        {
            "draft_id": "previous-draft",
            "user_id": "user-1",
            "draft_type": "solution",
            "status": "PUBLISHED",
            "draft": {
                "source_id": source_id,
                "mysql_record_id": "existing-mysql-id",
            },
        }
    ]
    updates = []
    retrieval = Retrieval()
    runtime = SimpleNamespace(
        require_draft_store=lambda: draft_store,
        require_business_record_sink=lambda: SimpleNamespace(
            update=lambda **kwargs: updates.append(kwargs)
            or {"business_record_id": kwargs["record_id"]},
            save=lambda *_args, **_kwargs: pytest.fail("已有来源记录时不得新建 MySQL 行"),
        ),
        require_lifecycle=lambda: Lifecycle(),
        embedded_retrieval=SimpleNamespace(agent=retrieval),
        require_conversation=lambda: SimpleNamespace(
            session_store=SimpleNamespace(get=lambda _thread_id: None, upsert=lambda *_a, **_k: None)
        ),
    )

    asyncio.run(
        DraftPublishingApplicationService(
            runtime, lambda user_id: (user_id, "admin", None)
        ).publish("draft-1", DraftPublishRequest(user_id="user-1"))
    )

    assert len(updates) == 1
    assert updates[0]["record_id"] == "existing-mysql-id"
    assert draft_store.record["draft"]["mysql_record_id"] == "existing-mysql-id"


def test_publishing_updates_current_draft_when_source_group_has_other_mysql_records(monkeypatch):
    draft_store = DraftStore()
    draft_store.record["draft"]["source_id"] = "source:doc-1"
    draft_store.record["draft"]["mysql_record_id"] = "mysql-current"
    draft_store.record["draft"]["attachments"] = []
    draft_store.record["draft"]["source_blocks"] = []
    draft_store.list_for_source_id = lambda source_id: [
        {
            "draft_id": f"previous-{mysql_id}",
            "user_id": "user-1",
            "draft_type": "solution",
            "status": "PUBLISHED",
            "draft": {"source_id": source_id, "mysql_record_id": mysql_id},
        }
        for mysql_id in ("mysql-other",)
    ]
    persisted = []
    retrieval = Retrieval()
    runtime = SimpleNamespace(
        require_draft_store=lambda: draft_store,
        require_business_record_sink=lambda: SimpleNamespace(
            update=lambda **kwargs: persisted.append(("update", kwargs))
            or {"business_record_id": kwargs["record_id"]},
            save=lambda *_args, **_kwargs: pytest.fail("已关联草稿不得新增 MySQL 行"),
        ),
        embedded_retrieval=SimpleNamespace(agent=retrieval),
    )

    asyncio.run(
        DraftPublishingApplicationService(
            runtime, lambda user_id: (user_id, "admin", None)
        ).publish("draft-1", DraftPublishRequest(user_id="user-1"))
    )

    assert len(persisted) == 1
    assert persisted[0][0] == "update"
    assert persisted[0][1]["record_id"] == "mysql-current"
    assert draft_store.record["draft"]["mysql_record_id"] == "mysql-current"
    assert retrieval.request.replace_existing is True
    assert {
        record.mysql_id for record in retrieval.request.records
        if record.metadata["chunk_role"] == "admin_draft"
    } == {"mysql-current"}


def test_publishing_matches_unlinked_draft_to_its_source_group_record_by_title(monkeypatch):
    draft_store = DraftStore()
    draft_store.record["draft"]["source_id"] = "source:doc-1"
    draft_store.record["draft"]["mysql_record_id"] = None
    draft_store.record["draft"]["title"] = "matched-title"
    draft_store.record["draft"]["attachments"] = []
    draft_store.record["draft"]["source_blocks"] = []
    draft_store.list_for_source_id = lambda source_id: [
        {
            "draft_id": "previous-other",
            "user_id": "user-1",
            "draft_type": "solution",
            "status": "PUBLISHED",
            "draft": {"source_id": source_id, "mysql_record_id": "mysql-other", "title": "其他方案"},
        },
        {
            "draft_id": "previous-current",
            "user_id": "user-1",
            "draft_type": "solution",
            "status": "PUBLISHED",
            "draft": {"source_id": source_id, "mysql_record_id": "mysql-current", "title": "æµ‹è¯•æ–¹æ¡ˆ"},
        },
    ]
    previous_lookup = draft_store.list_for_source_id
    draft_store.list_for_source_id = lambda source_id: [
        *previous_lookup(source_id),
        {
            "draft_id": "previous-current-title",
            "user_id": "user-1",
            "draft_type": "solution",
            "status": "PUBLISHED",
            "draft": {"source_id": source_id, "mysql_record_id": "mysql-current", "title": "matched-title"},
        },
    ]
    updates = []
    runtime = SimpleNamespace(
        require_draft_store=lambda: draft_store,
        require_business_record_sink=lambda: SimpleNamespace(
            update=lambda **kwargs: updates.append(kwargs)
            or {"business_record_id": kwargs["record_id"]},
            save=lambda *_args, **_kwargs: pytest.fail("å·²æœ‰å¯¹åº”æ–¹æ¡ˆæ—¶ä¸å¾—æ–°å»º MySQL è¡Œ"),
        ),
        require_lifecycle=lambda: Lifecycle(),
        embedded_retrieval=SimpleNamespace(agent=Retrieval()),
    )

    asyncio.run(
        DraftPublishingApplicationService(
            runtime, lambda user_id: (user_id, "admin", None)
        ).publish("draft-1", DraftPublishRequest(user_id="user-1"))
    )

    assert len(updates) == 1
    assert updates[0]["record_id"] == "mysql-current"
    assert draft_store.record["draft"]["mysql_record_id"] == "mysql-current"


def test_publishing_rejects_manually_edited_overlong_field_before_persistence():
    draft_store = DraftStore()
    draft_store.record["draft"]["fields"]["contect"] = "x" * 3061
    persisted = []

    runtime = SimpleNamespace(
        require_draft_store=lambda: draft_store,
        require_business_record_sink=lambda: SimpleNamespace(
            save=lambda payload: persisted.append(payload) or {"business_record_id": "mysql-1"}
        ),
    )

    with pytest.raises(HTTPException) as error:
        asyncio.run(
            DraftPublishingApplicationService(
                runtime, lambda user_id: (user_id, "admin", None)
            ).publish("draft-1", DraftPublishRequest(user_id="user-1"))
        )

    assert error.value.status_code == 422
    assert "方案描述" in str(error.value.detail)
    assert "3060" in str(error.value.detail)
    assert not persisted


def test_draft_update_merges_non_empty_fields_and_checks_version():
    store = InMemoryDraftStore()
    created = store.create(
        user_id="user-1",
        user_role="external",
        draft={
            "draft_type": "demand",
            "fields": {"company": "旧企业", "contect": "已有需求"},
        },
    )

    updated = store.update(
        created["draft_id"],
        user_id="user-1",
        draft={"draft_type": "demand", "fields": {"company": "新企业", "contect": ""}},
        expected_version=created["version"],
    )

    assert updated["draft"]["fields"] == {"company": "新企业", "contect": "已有需求"}
    assert updated["version"] == created["version"] + 1
    with pytest.raises(ValueError, match="version"):
        store.update(
            created["draft_id"],
            user_id="user-1",
            draft={"fields": {"company": "并发修改"}},
            expected_version=created["version"],
        )


def test_published_draft_must_be_withdrawn_before_delete():
    store = InMemoryDraftStore()
    created = store.create(
        user_id="user-1",
        user_role="external",
        draft={"draft_type": "solution", "fields": {"contect": "方案"}},
    )
    store.transition(
        created["draft_id"],
        user_id="user-1",
        target_status="PUBLISHED",
        operator_id="user-1",
        operator_role="external",
    )

    with pytest.raises(ValueError, match="下架"):
        store.logical_delete(created["draft_id"], user_id="user-1")
