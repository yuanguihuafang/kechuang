import pytest

from scripts.repair_retention_shared_documents import _artifact_refs, _published_version


def test_native_parser_does_not_reference_nonexistent_mineru_manifest():
    refs = _artifact_refs("doc", "derived/doc/mineru/parse-result.json", {"parser": "docx"})
    assert refs == {"parse_result": "derived/doc/mineru/parse-result.json"}
    assert _artifact_refs("doc", "parse", {"images": []})["images"] == "derived/doc/image-manifest.json"
    assert _artifact_refs("doc", "parse", {"artifact_manifest_key": "actual-manifest"})["manifest"] == "actual-manifest"


def test_restore_published_history_instead_of_failed_old_parse():
    document = {
        "document_id": "doc", "version_id": "doc:v1", "storage_key": "uploads/new.pdf",
        "status": "PARSING", "version_history": [
            {"version_id": "doc:v1", "storage_key": "uploads/old.pdf", "chunk_count": 39},
            {"version_id": "doc:v2", "storage_key": "uploads/new.pdf", "chunk_count": 19},
        ],
    }
    drafts = [{"status": "PUBLISHED", "content": {"attachments": [
        {"document_id": "doc", "version_id": "doc:v2", "storage_key": "uploads/new.pdf"}
    ]}}] * 2
    version, history = _published_version(document, drafts)
    assert version["version_id"] == "doc:v2"
    assert version["chunk_count"] == 19
    assert history == [{"version_id": "doc:v1", "storage_key": "uploads/old.pdf", "chunk_count": 39}]


def test_repair_rejects_conflicting_published_object_keys():
    document = {"document_id": "doc", "version_id": "doc:v1", "storage_key": "uploads/a.pdf"}
    drafts = [{"status": "PUBLISHED", "content": {"attachments": [
        {"document_id": "doc", "version_id": "doc:v1", "storage_key": "uploads/b.pdf"}
    ]}}]
    with pytest.raises(RuntimeError, match="对象键不一致"):
        _published_version(document, drafts)
