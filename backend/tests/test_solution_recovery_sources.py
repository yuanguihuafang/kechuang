import asyncio
from copy import deepcopy
from types import SimpleNamespace

from admin.index_recovery import prepare_collection_recovery, validate_recovery_source


def scenario(*, withdrawn=False, missing_file=False):
    docs = {doc: {
        "document_id": doc, "scope_id": "public", "knowledge_type": "solution", "user_id": "admin",
        "storage_key": f"uploads/{doc}.pdf",
        "active_version": {"version_id": f"{doc}:v2", "publication_status": "PUBLISHED", "storage_key": f"uploads/{doc}.pdf"},
    } for doc in ("doc-1", "doc-2")}
    if withdrawn:
        docs["doc-2"]["active_version"]["publication_status"] = "UNPUBLISHED"
    drafts = [{
        "draft_id": f"draft-{mysql}", "status": "PUBLISHED", "draft_type": "solution",
        "user_id": "admin", "user_role": "admin", "updated_at": "before",
        "draft": {
            "title": f"方案{mysql}", "fields": {"contect": f"业务正文{mysql}"},
            "mysql_record_id": mysql, "source_id": "same-source",
            "attachments": [{"document_id": doc, "version_id": f"{doc}:v2", "storage_key": f"uploads/{doc}.pdf"} for doc in docs],
            "source_blocks": [{"text": f"{doc}的正文", "document_id": doc, "version_id": f"{doc}:v2"} for doc in docs],
        },
    } for mysql in ("1", "2")]

    async def inspect(key):
        return None if missing_file and key == "uploads/doc-2.pdf" else {"storage_key": key, "etag": "e1", "size": 42}

    async def manifest(_):
        return []

    runtime = SimpleNamespace(
        storage_service=SimpleNamespace(inspect_object=inspect, load_image_manifest=manifest),
        require_draft_store=lambda: SimpleNamespace(
            list_page=lambda **_: {"items": deepcopy(drafts), "total_pages": 1},
            get_admin=lambda key: next((row for row in drafts if row["draft_id"] == key), None),
        ),
        require_lifecycle=lambda: SimpleNamespace(
            list_documents_page=lambda **_: {"documents": list(docs.values()), "total_pages": 1},
            get_document=lambda key: docs.get(key),
        ),
    )
    return runtime, drafts


def test_multiple_business_drafts_share_each_attachment_once():
    runtime, drafts = scenario()
    requests, references, skipped = asyncio.run(prepare_collection_recovery(runtime, "solution"))
    assert skipped == []
    chunks = [chunk for request in requests for chunk in request.chunks]
    assert sorted(chunk.mysql_id for chunk in chunks if chunk.chunk_role == "admin_draft") == ["1", "2"]
    assert sorted(chunk.document_id for chunk in chunks if chunk.chunk_role == "admin_doc") == ["doc-1", "doc-2"]
    for request, reference in zip(requests, references):
        validate_recovery_source(runtime, request, reference)
    assert all(len(row["draft"]["source_blocks"]) == 2 for row in drafts)


def test_bad_attachment_preserves_business_fields_and_other_attachment():
    runtime, _ = scenario(withdrawn=True)
    requests, _, skipped = asyncio.run(prepare_collection_recovery(runtime, "solution"))
    chunks = [chunk for request in requests for chunk in request.chunks]
    assert sorted(chunk.mysql_id for chunk in chunks if chunk.chunk_role == "admin_draft") == ["1", "2"]
    assert [chunk.document_id for chunk in chunks if chunk.chunk_role == "admin_doc"] == ["doc-1"]
    assert any(item["source_type"] == "attachment" and item["source_id"] == "doc-2" for item in skipped)


def test_missing_original_is_reported_separately_from_recoverable_text():
    runtime, _ = scenario(missing_file=True)
    requests, refs, skipped = asyncio.run(prepare_collection_recovery(runtime, "solution"))
    assert any(chunk.document_id == "doc-2" for request in requests for chunk in request.chunks)
    assert any(item.get("storage_key") == "uploads/doc-2.pdf" and "下载" in item["reason"] for item in skipped)
    assert any(item.get("object_checks") for item in refs)


def test_duplicate_published_drafts_for_one_mysql_id_are_reported_not_restored():
    runtime, drafts = scenario()
    drafts[1]["draft"]["mysql_record_id"] = "1"

    requests, _, skipped = asyncio.run(prepare_collection_recovery(runtime, "solution"))

    assert not any(
        chunk.mysql_id == "1" and chunk.chunk_role == "admin_draft"
        for request in requests
        for chunk in request.chunks
    )
    conflict = next(item for item in skipped if item["source_type"] == "draft_conflict")
    assert conflict["mysql_id"] == "1"
    assert conflict["draft_ids"] == ["draft-1", "draft-2"]
    assert conflict["source_type"] == "draft_conflict"


def test_duplicate_mysql_id_restores_only_the_latest_unambiguous_draft():
    runtime, drafts = scenario()
    drafts[1]["draft"]["mysql_record_id"] = "1"
    drafts[1]["updated_at"] = "later"
    drafts[1]["draft"]["fields"]["contect"] = "最新草稿内容"

    requests, references, skipped = asyncio.run(prepare_collection_recovery(runtime, "solution"))

    draft_chunks = [
        chunk for request in requests for chunk in request.chunks
        if chunk.chunk_role == "admin_draft" and chunk.mysql_id == "1"
    ]
    assert len(draft_chunks) == 1
    assert "最新草稿内容" in draft_chunks[0].chunk_text
    assert not any(item["source_type"] == "draft_conflict" for item in skipped)
    assert any(reference.get("superseded_draft_ids") == ["draft-1"] for reference in references)


def test_conflicting_shared_attachment_chunks_are_skipped_without_losing_drafts():
    runtime, drafts = scenario()
    drafts[1]["draft"]["source_blocks"][0]["text"] = "不同版本的解析原文"

    requests, references, skipped = asyncio.run(prepare_collection_recovery(runtime, "solution"))

    chunks = [chunk for request in requests for chunk in request.chunks]
    assert sorted(chunk.mysql_id for chunk in chunks if chunk.chunk_role == "admin_draft") == ["1", "2"]
    assert [chunk.document_id for chunk in chunks if chunk.chunk_role == "admin_doc"] == ["doc-2"]
    conflict = next(item for item in skipped if item["source_type"] == "attachment_source")
    assert conflict["document_id"] == "doc-1"
    assert all(
        [item["document_id"] for item in reference["documents"]] == ["doc-2"]
        for reference in references[:len(requests)]
    )


def test_persisted_draft_source_blocks_are_not_model_context_truncated():
    from services.application.draft_builder import build_draft
    blocks = [{"text": "完整原文" * 1500, "document_id": "doc-1", "version_id": "doc-1:v2"} for _ in range(70)]
    draft = build_draft("solution", "测试方案", {"source_blocks": blocks, "document_id": "doc-1"})
    assert draft["source_blocks"] == blocks
