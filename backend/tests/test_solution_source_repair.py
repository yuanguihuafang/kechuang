import asyncio
import hashlib
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import repair_solution_source_groups
from scripts.repair_solution_source_groups import resolve_source_id


def test_project_environment_uses_the_same_precedence_as_the_application(monkeypatch):
    calls = []
    monkeypatch.setitem(
        sys.modules,
        "dotenv",
        type("DotenvStub", (), {"load_dotenv": staticmethod(lambda path, override: calls.append((path, override)))}),
    )

    repair_solution_source_groups._load_project_environment()

    backend_root = Path(repair_solution_source_groups.__file__).resolve().parents[1]
    assert calls == [
        (backend_root / ".env", False),
        (backend_root / ".env.local", True),
    ]


def test_repair_keeps_source_id_saved_on_published_draft():
    assert resolve_source_id("source:pg", ["source:pg"]) == "source:pg"


def test_repair_reuses_unique_existing_milvus_source_id():
    assert resolve_source_id(None, ["source:milvus", "source:milvus"]) == "source:milvus"


def test_repair_rejects_ambiguous_existing_milvus_source_ids():
    with pytest.raises(ValueError, match="source_id 冲突"):
        resolve_source_id(None, ["source:first", "source:second"])


def test_repair_rejects_draft_and_milvus_source_id_mismatch():
    with pytest.raises(ValueError, match="source_id 冲突"):
        resolve_source_id("source:pg", ["source:milvus"])


def test_repair_uses_existing_attachment_derivation_when_no_source_is_saved():
    assert resolve_source_id(None, [], ["doc-b", "doc-a"]) == resolve_source_id(
        None, [], ["doc-a", "doc-b"]
    )


def test_repair_requires_an_existing_or_derivable_source_id():
    with pytest.raises(ValueError, match="无法确定 source_id"):
        resolve_source_id(None, [], [])


def test_overlapping_chunks_keep_each_image_reference_once():
    from agents.retrieval_agent.schemas import IndexChunk
    from scripts.repair_solution_source_groups import deduplicate_chunk_images

    url = "http://localhost:8793/api/v1/perception/images/doc-a/image-a"
    chunks = [
        IndexChunk(
            chunk_text=f"前文 ![图]({url})",
            source_locator="a|type:text",
            document_id="doc-a",
            chunk_role="admin_doc",
        ),
        IndexChunk(
            chunk_text=f"重叠 ![图]({url}) 后文",
            source_locator="a|type:text",
            document_id="doc-a",
            chunk_role="admin_doc",
        ),
    ]

    repaired, removed = deduplicate_chunk_images(chunks, f"原文 ![图]({url})")

    assert removed == 1
    assert f"![图]({url})" in repaired[0].chunk_text
    assert url not in repaired[1].chunk_text
    assert repaired[0].metadata["image_ids"] == ["image-a"]
    assert "image_ids" not in repaired[1].metadata


def test_collection_backup_does_not_query_generated_sparse_vector():
    from scripts.repair_solution_source_groups import _collection_rows

    class Target:
        def _existing_collection(self):
            return object()

        def _query_batches(self, collection, expression, fields):
            assert "sparse_vector" not in fields
            return [[{"id": "row-1", "chunk_text": "text"}]]

    assert _collection_rows(Target(), 'mysql_id == "1"') == [
        {"id": "row-1", "chunk_text": "text"}
    ]


def test_dry_run_cleanup_does_not_require_engine_on_draft_store(monkeypatch):
    disposed = []
    closed = []
    runtime = SimpleNamespace(
        agent=SimpleNamespace(close=lambda: closed.append(True)),
        lifecycle=SimpleNamespace(engine=SimpleNamespace(dispose=lambda: disposed.append(True))),
        drafts=SimpleNamespace(),
    )

    async def fail_preflight(*args):
        raise ValueError("preflight failure")

    monkeypatch.setattr(repair_solution_source_groups, "_build_runtime", lambda: runtime)
    monkeypatch.setattr(
        repair_solution_source_groups, "_prepare_one", fail_preflight
    )

    report = asyncio.run(repair_solution_source_groups.run(apply=False, backup_file=None))

    assert report["status"] == "preflight_failed"
    assert disposed == [True]
    assert closed == [True]


def test_anchor_payload_is_readable_by_confirmed_source_contract():
    from agents.retrieval_agent.schemas import IndexChunk, IndexRequest
    from core.governance.access_policy import ManualIdentity, UserType
    from scripts.repair_solution_source_anchors import build_anchor_payload

    request = IndexRequest(
        document_id="doc-a",
        version_id="doc-a:v1",
        scope_id="public",
        knowledge_type="solution",
        identity=ManualIdentity(user_id="admin-1", user_role=UserType.ADMIN),
        chunks=[
            IndexChunk(
                chunk_text="已确认分块",
                chunk_role="admin_doc",
                document_id="doc-a",
                source_locator="a.pdf|page:1",
            )
        ],
    )

    storage_key, reference, payload = build_anchor_payload(
        document_id="doc-a",
        version_id="doc-a:v1",
        request=request,
        task_id="task_repair_123",
    )

    assert storage_key == "derived/doc-a/tasks/task_repair_123/index-request.json"
    assert reference == {
        "storage_key": storage_key,
        "task_id": "task_repair_123",
        "document_id": "doc-a",
        "version_id": "doc-a:v1",
        "identity": {"user_id": "admin-1", "user_role": "admin"},
    }
    assert payload["identity"] == reference["identity"]
    assert payload["chunks"][0]["chunk_text"] == "已确认分块"


def test_anchor_apply_only_saves_minio_request_and_version_reference():
    from agents.retrieval_agent.schemas import IndexChunk, IndexRequest
    from core.governance.access_policy import ManualIdentity, UserType
    from scripts.repair_solution_source_anchors import _save_anchor, build_anchor_payload
    from services.application.draft_publishing import draft_index_fingerprint

    draft = {"mysql_record_id": "106", "source_id": "source:test", "fields": {"title": "方案"}}
    request = IndexRequest(
        document_id="doc-a",
        version_id="doc-a:v1",
        scope_id="public",
        knowledge_type="solution",
        identity=ManualIdentity(user_id="admin-1", user_role=UserType.ADMIN),
        chunks=[
            IndexChunk(
                chunk_text="已确认分块",
                chunk_role="admin_doc",
                document_id="doc-a",
                source_locator="a.pdf|page:1",
            )
        ],
    )
    task_id = "task_repair_test"
    storage_key, reference, payload = build_anchor_payload(
        document_id="doc-a", version_id="doc-a:v1", request=request, task_id=task_id
    )
    version = {
        "document_id": "doc-a",
        "version_id": "doc-a:v1",
        "publication_status": "PUBLISHED",
        "artifact_refs": {"parse_result": "derived/doc-a/parse-result.json"},
    }
    document = {
        "document_id": "doc-a",
        "scope_id": "public",
        "knowledge_type": "solution",
        "user_id": "admin-1",
    }
    stored = {}

    class Storage:
        async def save_index_request(self, doc_id, key_task_id, body):
            stored[(doc_id, key_task_id)] = body
            return storage_key

        async def load_index_request(self, doc_id, key_task_id):
            return stored.get((doc_id, key_task_id))

        async def load_parse_result(self, doc_id):
            return {"edited": True, "raw_text": "原文"}

    class Lifecycle:
        current = version

        def get_version(self, version_id):
            return self.current

        def update_version(self, version_id, *, expected_version, artifact_refs):
            assert self.current == expected_version
            self.current = {**self.current, "artifact_refs": {**self.current["artifact_refs"], **artifact_refs}}
            return self.current

    lifecycle = Lifecycle()
    runtime = SimpleNamespace(
        storage=Storage(),
        lifecycle=lifecycle,
        drafts=SimpleNamespace(get_admin=lambda draft_id: {"status": "PUBLISHED", "draft": draft}),
    )
    group = {
        "document_id": "doc-a",
        "version_id": "doc-a:v1",
        "draft_id": "draft-a",
        "draft_fingerprint": draft_index_fingerprint(draft),
        "raw_fingerprint": hashlib.sha256("原文".encode()).hexdigest(),
        "request": request,
        "source_id": "source:test",
        "version_snapshot": version,
        "document": document,
        "anchor_task_id": task_id,
        "storage_key": storage_key,
        "reference": reference,
        "payload": payload,
    }

    asyncio.run(_save_anchor(runtime, group))

    assert lifecycle.current["artifact_refs"]["confirmed_index_request"] == reference
    assert stored[("doc-a", task_id)] == payload


def test_anchor_preflight_rejects_matching_ids_with_different_milvus_text():
    from agents.retrieval_agent.application.retrieval_service import index_record_id
    from agents.retrieval_agent.schemas import IndexChunk, IndexRequest
    from core.governance.access_policy import ManualIdentity, UserType
    from scripts.repair_solution_source_anchors import _assert_milvus_matches

    chunk = IndexChunk(
        chunk_text="expected",
        chunk_role="admin_doc",
        document_id="doc-a",
        mysql_id="1",
        source_id="source:test",
        source_locator="a.pdf|page:1",
    )
    request = IndexRequest(
        document_id="doc-a",
        scope_id="public",
        knowledge_type="solution",
        identity=ManualIdentity(user_id="admin-1", user_role=UserType.ADMIN),
        chunks=[chunk],
    )
    row = {
        "id": index_record_id("doc-a", chunk),
        "chunk_text": "different text with same stable id",
        "chunk_role": "admin_doc",
        "source_id": "source:test",
    }

    class Target:
        def _existing_collection(self):
            return object()

        def _query_batches(self, collection, expression, fields):
            return [[]] if expression.startswith('chunk_role == "mysql"') else [[row]]

    group = {
        "document_id": "doc-a",
        "mysql_id": "1",
        "source_id": "source:test",
        "request": request,
    }
    with pytest.raises(RuntimeError, match="分组与已修复内容不一致"):
        _assert_milvus_matches(group, Target())


def test_anchor_preflight_treats_encoded_markdown_bracket_as_equivalent():
    from agents.retrieval_agent.application.retrieval_service import index_record_id
    from agents.retrieval_agent.schemas import IndexChunk, IndexRequest
    from core.governance.access_policy import ManualIdentity, UserType
    from scripts.repair_solution_source_anchors import _assert_milvus_matches

    chunk = IndexChunk(
        chunk_text="![图](image-id)](next-link)",
        chunk_role="admin_doc",
        document_id="doc-a",
        mysql_id="1",
        source_id="source:test",
        source_locator="a.pdf|page:1",
    )
    request = IndexRequest(
        document_id="doc-a",
        scope_id="public",
        knowledge_type="solution",
        identity=ManualIdentity(user_id="admin-1", user_role=UserType.ADMIN),
        chunks=[chunk],
    )
    row = {
        "id": index_record_id("doc-a", chunk),
        "chunk_text": chunk.chunk_text.replace("]", "%5D"),
        "chunk_role": "admin_doc",
        "source_id": "source:test",
    }

    class Target:
        def _existing_collection(self):
            return object()

        def _query_batches(self, collection, expression, fields):
            return [[]] if expression.startswith('chunk_role == "mysql"') else [[row]]

    _assert_milvus_matches(
        {
            "document_id": "doc-a",
            "mysql_id": "1",
            "source_id": "source:test",
            "request": request,
        },
        Target(),
    )
