import asyncio
import hashlib
from copy import deepcopy
from types import SimpleNamespace
from urllib.parse import unquote

import pytest
from fastapi import HTTPException

from agents.retrieval_agent.schemas import IndexRequest
from core.contracts.workflows import (
    DraftPublishRequest,
)
from services.application import draft_publishing
from services.application.draft_publishing import build_published_draft_index_request
from runtime.workflows.knowledge_document_draft import _source_batch_id


def test_new_and_explicit_source_ids_do_not_use_batch_namespace():
    assert _source_batch_id({"task_id": "task-1"}) == (
        "source:" + hashlib.sha256(b"task-1").hexdigest()[:32]
    )
    assert _source_batch_id({"source_id": "source:batch:legacy"}) == "source:legacy"


def published_record(role="external"):
    return {
        "draft_id": "draft-1",
        "status": "PUBLISHED",
        "draft_type": "solution",
        "user_id": "user-1",
        "user_role": role,
        "draft": {
            "draft_type": "solution",
            "title": "测试方案",
            "mysql_record_id": "mysql-1",
            "fields": {
                "contect": "方案正文",
                "link_tel": "内部电话",
                "id": "系统编号",
                "empty": "  ",
            },
            "attachments": [
                {"document_id": "doc-1", "version_id": "doc-1:v1", "file_name": "测试.pdf"},
                {"document_id": "doc-2", "version_id": "doc-2:v2", "file_name": "附件二.pdf"},
            ],
            "source_blocks": [
                {
                    "text": "解析原文",
                    "document_id": "doc-1",
                    "version_id": "doc-1:v1",
                    "metadata": {"page_number": 3, "block_type": "table", "chunks_are_final": True},
                },
                {"chunk_text": "第二块", "document_id": "doc-2", "version_id": "doc-2:v2", "page_number": 4},
                {"text": "  "},
                None,
            ],
        },
    }


def active_attachment_lifecycle(update_version=None):
    documents = {
        f"doc-{number}": {
            "document_id": f"doc-{number}",
            "user_id": "user-1",
            "scope_id": "public",
            "knowledge_type": "solution",
            "active_version": {"version_id": f"doc-{number}:v{number}"},
        }
        for number in (1, 2)
    }
    versions = {
        f"doc-{number}:v{number}": {
            "version_id": f"doc-{number}:v{number}",
            "document_id": f"doc-{number}",
            "version": number,
            "status": "ACTIVE",
            "storage_key": f"uploads/doc-{number}.pdf",
        }
        for number in (1, 2)
    }
    return SimpleNamespace(
        get_document=lambda document_id: documents.get(document_id),
        get_version=lambda version_id: versions.get(version_id),
        update_version=update_version or (lambda *_args, **_kwargs: {}),
    )


class PublishStorage:
    async def check_ready(self):
        return None

    async def inspect_object(self, _key):
        return {"size": 1}

    async def load_image_manifest(self, _document_id):
        return []


async def publish_preflight(_document_id, _request):
    return None


@pytest.mark.parametrize("role", ["admin", "internal", "external"])
def test_recovery_preserves_published_content_links_and_identity_without_mutation(role):
    record = published_record(role)
    before = deepcopy(record)
    request = build_published_draft_index_request(record)

    assert isinstance(request, IndexRequest)
    assert request.document_id == "doc-1"
    assert request.version_id == "doc-1:v1"
    assert request.scope_id == "public"
    assert request.knowledge_type == "solution"
    assert request.replace_existing is True
    assert request.chunks_are_final is False
    assert (request.chunk_size, request.chunk_overlap) == (800, 80)
    assert request.identity.model_dump() == {"user_id": "user-1", "user_role": role}
    assert len(request.chunks) == 3
    body, attachment, second = request.chunks
    prefix = "admin" if role == "admin" else "agent"
    assert body.chunk_role == f"{prefix}_draft"
    assert body.mysql_id == "mysql-1"
    assert body.document_id is None
    assert body.chunk_text.startswith("方案描述：方案正文")
    assert "方案名称：测试方案" in body.chunk_text
    assert ("联系电话：内部电话" in body.chunk_text) == (role == "admin")
    assert "系统编号" not in body.chunk_text
    assert "empty" not in body.metadata["field_names"]
    assert "id" not in body.metadata["field_names"]
    assert body.metadata["draft_id"] == "draft-1"
    assert body.source_locator.startswith("mysql:xp_pub_tech_solution|type:record|id:mysql-1|")
    assert "测试.pdf" in unquote(body.source_locator)
    assert "附件二.pdf" in unquote(body.source_locator)
    source_id = "source:" + hashlib.sha1(b"doc-1|doc-2").hexdigest()[:24]
    assert all(chunk.source_id == source_id for chunk in request.chunks)
    assert attachment.chunk_role == f"{prefix}_doc"
    assert attachment.mysql_id is None
    assert attachment.document_id == "doc-1"
    assert attachment.chunk_text == "解析原文"
    assert attachment.source_locator == "测试.pdf|type:table|attachment:doc-1|page:3"
    assert attachment.metadata == {
        "page_number": 3,
        "block_type": "table",
        "chunks_are_final": True,
        "document_id": "doc-1",
        "attachment_id": "doc-1",
        "version_id": "doc-1:v1",
        "parent_id": "draft-1",
        "draft_id": "draft-1",
        "chunk_role": f"{prefix}_doc",
    }
    assert second.chunk_text == "第二块"
    assert second.document_id == "doc-2"
    assert second.metadata["version_id"] == "doc-2:v2"
    assert second.source_locator == "附件二.pdf|type:text|attachment:doc-2|page:4"
    assert [chunk.chunk_index for chunk in request.chunks] == [0, 1, 2]
    assert all(chunk.visibility == "public" for chunk in request.chunks)
    assert build_published_draft_index_request(record) == request
    assert record == before


@pytest.mark.parametrize(
    ("draft_type", "field"),
    [
        ("enterprise", "description"),
        ("demand", "contect"),
        ("achievement", "analyse_contect"),
        ("expert", "research_field"),
        ("solution", "contect"),
    ],
)
def test_recovery_accepts_only_the_five_public_types(draft_type, field):
    record = published_record()
    record["draft_type"] = record["draft"]["draft_type"] = draft_type
    record["draft"]["fields"] = {field: "有效正文"}
    assert build_published_draft_index_request(record).knowledge_type == draft_type


@pytest.mark.parametrize(
    "status", ["CONFIRMED", "PENDING_REVIEW", "WITHDRAWN", "DELETED", "", None]
)
def test_recovery_rejects_unpublished_status(status):
    record = published_record()
    record["status"] = status
    with pytest.raises(ValueError, match="PUBLISHED"):
        build_published_draft_index_request(record)


@pytest.mark.parametrize("draft_type", ["customer_visit", "unknown", "", None])
def test_recovery_rejects_non_public_type(draft_type):
    record = published_record()
    record["draft_type"] = draft_type
    with pytest.raises(ValueError, match="类型"):
        build_published_draft_index_request(record)


@pytest.mark.parametrize("mysql_id", [None, "", "  "])
def test_recovery_requires_persisted_mysql_id(mysql_id):
    record = published_record()
    record["draft"]["mysql_record_id"] = mysql_id
    with pytest.raises(ValueError, match="mysql_record_id"):
        build_published_draft_index_request(record)


@pytest.mark.parametrize("role", ["admin", "external"])
def test_recovery_rejects_empty_body_even_when_attachment_text_exists(role):
    record = published_record(role)
    record["draft"].pop("title")
    record["draft"]["fields"] = {"id": "系统编号", "contect": "  "}
    with pytest.raises(ValueError, match="有效字段"):
        build_published_draft_index_request(record)


def test_recovery_rejects_body_filtered_out_of_public_fields():
    record = published_record()
    record["draft"].pop("title")
    record["draft"]["fields"] = {"link_tel": "内部电话"}
    with pytest.raises(ValueError, match="有效字段"):
        build_published_draft_index_request(record)


def test_recovery_without_attachments_uses_draft_document_and_skips_orphan_blocks():
    record = published_record()
    record["draft"]["attachments"] = []
    request = build_published_draft_index_request(record)
    assert request.document_id == "draft-1"
    assert request.version_id is None
    assert len(request.chunks) == 1
    assert request.chunks[0].document_id is None
    assert request.chunks[0].source_id


def test_recovery_prefers_persisted_source_id_and_keeps_draft_document_id_empty():
    record = published_record()
    record["draft"]["source_id"] = "source:stable-draft-id"

    request = build_published_draft_index_request(record)

    assert request.chunks[0].mysql_id == "mysql-1"
    assert request.chunks[0].source_id == "source:stable-draft-id"
    assert request.chunks[0].document_id is None
    assert all(chunk.source_id == "source:stable-draft-id" for chunk in request.chunks)


def test_source_blocks_with_unknown_document_are_rejected_instead_of_reassigned():
    record = published_record()
    block = record["draft"]["source_blocks"][0]
    block["document_id"] = "other-doc"
    block["version_id"] = "other-doc:v9"
    block["metadata"]["document_id"] = "other-doc"
    block["metadata"]["attachment_id"] = "other-doc"
    with pytest.raises(ValueError, match="附件|归属"):
        build_published_draft_index_request(record)


def test_multi_attachment_blocks_require_document_identity():
    record = published_record()
    record["draft"]["source_blocks"][0].pop("document_id")
    with pytest.raises(ValueError, match="归属"):
        build_published_draft_index_request(record)


def test_old_version_source_blocks_cannot_be_published_as_current_version():
    record = published_record()
    record["draft"]["source_blocks"][0]["version_id"] = "doc-1:v0"
    with pytest.raises(ValueError, match="版本"):
        build_published_draft_index_request(record)


def test_complete_saved_final_chunks_are_restored_without_rebuilding():
    from services.application.draft_publishing import draft_index_fingerprint

    record = published_record("admin")
    request = build_published_draft_index_request(record)
    request.chunks[1].chunk_text = "管理员确认后的完整 chunk\n保留换行和编号"
    request.chunks[1].chunk_index = 7
    request.chunks_are_final = True
    request.chunk_size = 1234
    record["draft"]["_index_snapshot"] = {
        "source_fingerprint": draft_index_fingerprint(record["draft"]),
        "indexed_count": len(request.chunks),
        "request": request.model_dump(mode="json"),
    }
    assert build_published_draft_index_request(record) == request
    # 管理员代用户发布时，以真实发布身份保留 admin_draft，而非退回创建者角色。
    record["user_role"] = "internal"
    assert build_published_draft_index_request(record) == request
    record["draft"]["fields"]["contect"] = "管理员新修改正文"
    refreshed = build_published_draft_index_request(record)
    assert refreshed.chunks_are_final is False
    assert "管理员新修改正文" in refreshed.chunks[0].chunk_text


@pytest.mark.parametrize("role", ["admin", "external"])
def test_normal_publish_reuses_recovery_content_and_preserves_write_order(monkeypatch, role):
    record = published_record(role)
    expected = build_published_draft_index_request(record)
    record["status"] = "WITHDRAWN"
    events = []

    def transition(_draft_id, **kwargs):
        events.append(kwargs["target_status"])
        record["status"] = kwargs["target_status"]
        return record

    def update(_draft_id, *, draft, status=None, **_kwargs):
        events.append("persist_source")
        record["draft"] = draft
        if status is not None:
            record["status"] = status
        return record

    async def publish_document(document_id, request):
        events.append("index")
        assert record["status"] == "WITHDRAWN"
        assert document_id == expected.document_id
        assert request.replace_existing is False
        assert request.chunks_are_final == expected.chunks_are_final
        assert request.identity == expected.identity
        assert request.version_id == expected.version_id
        for original, recovered in zip(request.records, expected.chunks, strict=True):
            assert original.text == recovered.chunk_text
            assert original.mysql_id == recovered.mysql_id
            assert original.source_id == recovered.source_id
            assert original.source_locator == recovered.source_locator
            assert original.metadata == recovered.metadata
        return SimpleNamespace(status="SUCCESS", indexed_count=3, model_dump=dict)

    runtime = SimpleNamespace(
        require_draft_store=lambda: SimpleNamespace(
            get=lambda *_args, **_kwargs: record, update=update, transition=transition
        ),
        require_business_record_sink=lambda: SimpleNamespace(
            update=lambda **_kwargs: events.append("mysql") or {"business_record_id": "mysql-1"},
        ),
        require_lifecycle=lambda: active_attachment_lifecycle(
            update_version=lambda *_args, **_kwargs: events.append("version") or {},
        ),
        storage_service=PublishStorage(),
        embedded_retrieval=SimpleNamespace(
            agent=SimpleNamespace(
                preflight_publish_document=publish_preflight,
                publish_document=publish_document,
            )
        ),
    )
    monkeypatch.setattr(
        draft_publishing,
        "PostgresAuditSink",
        lambda: SimpleNamespace(
            record=lambda *_args, **_kwargs: events.append("audit"),
        ),
    )
    asyncio.run(
        draft_publishing.DraftPublishingApplicationService(
            runtime,
            lambda user_id: (user_id, role, None),
        ).publish("draft-1", DraftPublishRequest(user_id="user-1", replace_existing=False))
    )
    assert events == ["mysql", "persist_source", "index", "version", "version", "PUBLISHED", "audit"]


def test_publish_request_validation_still_marks_index_failure(monkeypatch):
    record = published_record()
    record["status"] = "CONFIRMED"
    record["draft"]["attachments"][0]["file_name"] = "x" * 257
    stages = []
    monkeypatch.setattr(
        draft_publishing.DraftPublishingApplicationService,
        "_mark_chat_flow_stage",
        lambda _self, _thread, _draft, stage, *_args, **_kwargs: stages.append(stage),
    )
    runtime = SimpleNamespace(
        require_draft_store=lambda: SimpleNamespace(
            get=lambda *_args, **_kwargs: record,
            update=lambda _draft_id, *, draft, status=None, **_kwargs: {
                **record, "draft": draft, "status": status or record["status"]
            },
        ),
        require_business_record_sink=lambda: SimpleNamespace(
            update=lambda **_kwargs: {"business_record_id": "mysql-1"},
        ),
        require_lifecycle=lambda: active_attachment_lifecycle(),
        storage_service=PublishStorage(),
        embedded_retrieval=SimpleNamespace(agent=SimpleNamespace(
            preflight_publish_document=publish_preflight, publish_document=None
        )),
    )
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            draft_publishing.DraftPublishingApplicationService(
                runtime,
                lambda user_id: (user_id, "external", None),
            ).publish("draft-1", DraftPublishRequest(user_id="user-1"))
        )
    assert exc_info.value.status_code == 422
    assert "未写入 MySQL 或 Milvus" in exc_info.value.detail
    assert stages == []
