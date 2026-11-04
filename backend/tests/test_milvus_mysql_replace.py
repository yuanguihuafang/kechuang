import asyncio
import json
from types import SimpleNamespace

import pytest

from agents.retrieval_agent.application.retrieval_service import RetrievalAgent
from agents.retrieval_agent.retrieval.chunker import chunk_by_paragraph, chunk_document_blocks
from agents.retrieval_agent.schemas import IndexChunk, IndexRequest
from core.contracts.knowledge import BusinessImportRequest, CleanedKnowledgeRecord, DocumentKnowledgeRequest
from core.contracts.retrieval import ChunkPreviewBlock, ChunkPreviewRequest
from core.governance.access_policy import ManualIdentity, UserType
from services.vector.milvus.milvus_service import MilvusService


class _MemoryMilvus(MilvusService):
    """复用正式 replace_chunks，仅将数据库读写替换为内存操作。"""

    def __init__(self, rows=None, *, requires_user_id=False):
        self.rows = {row["id"]: dict(row) for row in rows or []}
        self.requires_user_id = requires_user_id
        self.expressions = []
        self.deleted_ids = set()
        self.fail_after = None

    def _existing_collection(self):
        return self

    def _query_ids(self, collection, expr):
        assert collection is self
        self.expressions.append(expr)
        owner = None
        if " && user_id == " in expr:
            expr, literal = expr.rsplit(" && user_id == ", 1)
            owner = json.loads(literal)
            expr = expr.strip("()")
        matched = set()
        for branch in expr.split(" || "):
            rows = list(self.rows.values())
            for condition in branch.strip("()").split(" && "):
                field, operator, literal = condition.strip("()").split(" ", 2)
                value = json.loads(literal)
                assert operator in {"==", "in"}
                rows = [
                    row for row in rows
                    if (row.get(field) == value if operator == "==" else row.get(field) in value)
                ]
            matched.update(row["id"] for row in rows if owner is None or row.get("user_id") == owner)
        return sorted(matched)

    def upsert_chunks(self, chunks):
        for index, chunk in enumerate(chunks):
            if index == self.fail_after:
                raise RuntimeError("Milvus upsert interrupted")
            self.rows[chunk["id"]] = dict(chunk)
        return len(chunks)

    def delete_by_ids(self, ids):
        self.deleted_ids.update(ids)
        for chunk_id in ids:
            del self.rows[chunk_id]


def _index_agent(target):
    agent = object.__new__(RetrievalAgent)
    agent.public_milvus = {"demand": target}
    agent.milvus = target
    agent.private_milvus = target

    async def encode_dense(texts):
        return [[0.0] * 1024 for _ in texts]

    agent.embedding = SimpleNamespace(encode_dense=encode_dense)
    async def check_ready():
        return None

    target.check_ready = check_ready
    return agent


def test_publish_preflight_validates_embeddings_and_collection_without_writing():
    target = _MemoryMilvus()
    agent = _index_agent(target)
    request = DocumentKnowledgeRequest(
        source_name="测试附件.pdf",
        knowledge_type="solution",
        records=[CleanedKnowledgeRecord(
            record_id="draft-1",
            mysql_id="mysql-1",
            source_id="source:doc-1",
            text="测试方案内容",
            source_locator="测试附件.pdf|type:text|attachment:doc-1",
            metadata={"chunk_role": "admin_draft"},
        )],
        identity=ManualIdentity(user_id="admin", user_role=UserType.ADMIN),
    )

    asyncio.run(agent.preflight_publish_document("doc-1", request))

    assert target.rows == {}
    assert target.expressions == []


@pytest.mark.parametrize("conflicting", [False, True])
def test_shadow_recovery_shared_source_across_requests_is_verified_once(conflicting):
    target = object.__new__(MilvusService)
    target.alias_name = None
    target.collection_name = "solution__shadow"
    target._chunk_to_row = lambda chunk: dict(chunk)
    rows = {}

    def query(*, expr, output_fields, consistency_level):
        assert consistency_level == "Strong"
        ids = json.loads(expr.removeprefix("id in "))
        return [{key: row[key] for key in output_fields} for pk, row in rows.items() if pk in ids]

    target._existing_collection = lambda: SimpleNamespace(query=query)
    target._insert_rows = lambda batch, **_: rows.update((row["id"], dict(row)) for row in batch)
    agent = _index_agent(target)

    async def scenario():
        results = []
        for index in range(2):
            request = IndexRequest(
                document_id=f"draft-request-{index}", knowledge_type="solution",
                chunks_are_final=True,
                chunks=[IndexChunk(
                    chunk_text="changed" if conflicting and index else "shared attachment text",
                    source_id="source:shared", document_id="attachment-1",
                    source_locator="attachment-1|page:1", chunk_role="admin_doc",
                )],
                identity=ManualIdentity(user_id="admin", user_role=UserType.ADMIN),
            )
            results.append(await agent.index(request, target_override=target, insert_only=True))
        return results

    first, second = asyncio.run(scenario())
    assert first.status == "SUCCESS"
    assert len(rows) == 1
    if conflicting:
        assert second.status == "FAILED"
        assert "主键内容冲突" in second.error
    else:
        assert second.status == "SUCCESS"
        assert second.indexed_count == len(second.indexed_chunks) == 1


def test_source_ids_by_mysql_id_reads_existing_draft_role_associations(monkeypatch):
    service = object.__new__(MilvusService)
    collection = object()
    observed = {}

    def query_batches(actual_collection, expression, output_fields):
        observed["collection"] = actual_collection
        observed["expression"] = expression
        observed["output_fields"] = output_fields
        return iter([[{"source_id": "legacy-b"}, {"source_id": "legacy-a"}, {"source_id": "legacy-a"}]])

    monkeypatch.setattr(service, "_existing_collection", lambda: collection)
    monkeypatch.setattr(service, "_query_batches", query_batches)

    source_ids = service.source_ids_by_mysql_id("mysql-1", "agent_draft")

    assert source_ids == ["legacy-a", "legacy-b"]
    assert observed == {
        "collection": collection,
        "expression": 'mysql_id == "mysql-1" && chunk_role == "agent_draft"',
        "output_fields": ["source_id"],
    }


def test_source_ids_by_mysql_ids_reads_unique_mysql_role_associations(monkeypatch):
    service = object.__new__(MilvusService)
    collection = object()
    observed = {}

    def query_batches(actual_collection, expression, output_fields):
        observed.update(collection=actual_collection, expression=expression, output_fields=output_fields)
        return iter([[
            {"mysql_id": "1", "source_id": "source:a", "chunk_role": "agent_draft"},
            {"mysql_id": "1", "source_id": "source:old-mysql", "chunk_role": "mysql"},
            {"mysql_id": "2", "source_id": "source:b"},
            {"mysql_id": "2", "source_id": "source:other"},
            {"mysql_id": "3", "source_id": ""},
        ]])

    monkeypatch.setattr(service, "_existing_collection", lambda: collection)
    monkeypatch.setattr(service, "_query_batches", query_batches)

    result = service.source_ids_by_mysql_ids(["1", "2", "2", "3"])

    assert result == {"1": ["source:a"], "2": ["source:b", "source:other"]}
    assert observed == {
        "collection": collection,
        "expression": 'chunk_role in ["agent_draft", "admin_draft", "mysql"] && mysql_id in ["1", "2", "3"] && status == "active"',
        "output_fields": ["mysql_id", "source_id", "chunk_role"],
    }


def test_published_draft_mysql_ids_uses_active_milvus_status(monkeypatch):
    service = object.__new__(MilvusService)
    collection = object()
    observed = {}

    def query_batches(actual_collection, expression, output_fields):
        observed.update(collection=actual_collection, expression=expression, output_fields=output_fields)
        return iter([[{"mysql_id": "1"}, {"mysql_id": "1"}, {"mysql_id": ""}]])

    monkeypatch.setattr(service, "_existing_collection", lambda: collection)
    monkeypatch.setattr(service, "_query_batches", query_batches)

    assert service.published_draft_mysql_ids(["1", "1", "2"]) == {"1"}
    assert observed == {
        "collection": collection,
        "expression": 'chunk_role in ["agent_draft", "admin_draft"] && mysql_id in ["1", "2"] && status == "active"',
        "output_fields": ["mysql_id"],
    }


def _mysql_import(records, *, replace_existing=True):
    return BusinessImportRequest(
        document_id="mysql:demand",
        source_name="MySQL:demand",
        knowledge_type="demand",
        publication_status="PUBLISHED",
        replace_existing=replace_existing,
        records=records,
        chunk_size=100,
        chunk_overlap=0,
        identity=ManualIdentity(user_id="admin", user_role=UserType.ADMIN),
    )


def test_first_mysql_batch_replaces_only_its_record_and_keeps_other_records_docs_and_drafts():
    old_rows = [
        {"id": "old-2-0", "mysql_id": "2", "chunk_role": "mysql"},
        {"id": "old-2-1", "mysql_id": "2", "chunk_role": "mysql"},
        {"id": "old-3", "mysql_id": "3", "chunk_role": "mysql"},
        {"id": "old-4", "mysql_id": "4", "chunk_role": "mysql"},
        *[
            {"id": role, "mysql_id": "2", "chunk_role": role, "document_id": "doc:2"}
            for role in ("admin_doc", "agent_doc", "admin_draft", "agent_draft")
        ],
    ]
    target = _MemoryMilvus(old_rows)
    request = _mysql_import([
        CleanedKnowledgeRecord(
            record_id="2",
            text="新的需求",
            source_locator="mysql:xp_demand|type:record|id:2",
        )
    ])

    result = asyncio.run(_index_agent(target).import_business_data(request))

    assert result.status == "SUCCESS"
    assert result.indexed_count == 1
    assert target.deleted_ids == {"old-2-0", "old-2-1"}
    assert target.expressions == ['chunk_role == "mysql" && mysql_id in ["2"]']
    for row in old_rows[2:]:
        assert target.rows[row["id"]] == row
    assert [row["chunk_text"] for row in target.rows.values() if "chunk_text" in row] == [
        "新的需求"
    ]


def test_mysql_replace_cleans_stale_chunks_when_record_gets_shorter():
    target = _MemoryMilvus()
    agent = _index_agent(target)
    first = asyncio.run(agent.import_business_data(_mysql_import([
        CleanedKnowledgeRecord(record_id="2", text="旧需求内容。" * 1000)
    ])))
    old_ids = set(target.rows)
    assert first.status == "SUCCESS"
    assert len(old_ids) > 1

    result = asyncio.run(agent.import_business_data(_mysql_import([
        CleanedKnowledgeRecord(record_id="2", text="简短的新需求")
    ])))

    assert result.status == "SUCCESS"
    assert result.indexed_count == 1
    assert len(target.rows) == 1
    assert target.deleted_ids == old_ids - set(target.rows)
    assert next(iter(target.rows.values()))["chunk_text"] == "简短的新需求"


@pytest.mark.parametrize("scope_id", ["public", "internal"])
def test_draft_republish_removes_stale_original_chunks_only_in_submitted_scope(scope_id):
    unrelated = [
        {"id": "sibling-draft", "source_id": "shared", "mysql_id": "43", "chunk_role": "admin_draft"},
        {"id": "other-document", "source_id": "shared", "document_id": "doc-other", "chunk_role": "admin_doc"},
        {"id": "other-source", "source_id": "different", "document_id": "doc-other-source", "chunk_role": "admin_doc"},
    ]
    unrelated = [{**row, "user_id": "admin"} for row in unrelated]
    if scope_id == "internal":
        unrelated.append({"id": "other-owner", "source_id": "shared", "document_id": "doc-1", "chunk_role": "admin_doc", "user_id": "other"})
    target = _MemoryMilvus(unrelated, requires_user_id=scope_id == "internal")
    agent = _index_agent(target)
    if scope_id == "internal":
        agent.public_milvus = {"demand": _MemoryMilvus()}

    def request(text):
        return IndexRequest(
            document_id="doc-1", scope_id=scope_id, knowledge_type="demand", chunk_size=100, chunk_overlap=0,
            chunks=[
                IndexChunk(chunk_text="草稿", source_locator="draft", chunk_role="admin_draft", mysql_id="42", source_id="shared"),
                IndexChunk(chunk_text=text, source_locator="original", chunk_role="admin_doc", document_id="doc-1", source_id="shared"),
            ],
            identity=ManualIdentity(user_id="admin", user_role=UserType.ADMIN),
        )

    assert asyncio.run(agent.index(request("old original. " * 200))).status == "SUCCESS"
    assert asyncio.run(agent.index(request("new original"))).status == "SUCCESS"
    assert not any("old original" in row.get("chunk_text", "") for row in target.rows.values())
    assert sum(row.get("chunk_text") == "new original" for row in target.rows.values()) == 1
    for row in unrelated:
        assert target.rows[row["id"]] == row


@pytest.mark.parametrize("mysql_ids", [("2", "3"), ('2"quoted', r"3\path")])
def test_mysql_replace_limits_cleanup_to_all_batch_ids_and_escapes_literals(mysql_ids):
    old_rows = [
        {"id": "old-2", "mysql_id": mysql_ids[0], "chunk_role": "mysql"},
        {"id": "old-3", "mysql_id": mysql_ids[1], "chunk_role": "mysql"},
        {"id": "old-4", "mysql_id": "4", "chunk_role": "mysql"},
    ]
    target = _MemoryMilvus(old_rows)
    request = _mysql_import([
        CleanedKnowledgeRecord(record_id=mysql_id, text=f"新需求 {mysql_id}")
        for mysql_id in mysql_ids
    ])

    result = asyncio.run(_index_agent(target).import_business_data(request))

    assert result.status == "SUCCESS"
    assert result.indexed_count == 2
    assert target.deleted_ids == {"old-2", "old-3"}
    assert target.rows["old-4"] == old_rows[-1]
    assert target.expressions == [
        'chunk_role == "mysql" && mysql_id in '
        + json.dumps(sorted(mysql_ids), ensure_ascii=False)
    ]


@pytest.mark.parametrize("fail_after", [0, 1])
def test_mysql_replace_does_not_delete_old_chunks_when_batch_write_fails(fail_after):
    old_rows = [
        {"id": "old-2", "mysql_id": "2", "chunk_role": "mysql"},
        {"id": "old-3", "mysql_id": "3", "chunk_role": "mysql"},
        {"id": "old-4", "mysql_id": "4", "chunk_role": "mysql"},
    ]
    target = _MemoryMilvus(old_rows)
    target.fail_after = fail_after
    request = _mysql_import([
        CleanedKnowledgeRecord(record_id=mysql_id, text=f"新需求 {mysql_id}")
        for mysql_id in ("2", "3")
    ])

    result = asyncio.run(_index_agent(target).import_business_data(request))

    assert result.status == "FAILED"
    assert "upsert interrupted" in result.error
    assert not target.deleted_ids
    for row in old_rows:
        assert target.rows[row["id"]] == row
    assert len(target.rows) == len(old_rows) + fail_after


def test_mysql_append_does_not_cleanup_old_chunks():
    old_rows = [
        {"id": "old-2", "mysql_id": "2", "chunk_role": "mysql"},
        {"id": "old-3", "mysql_id": "3", "chunk_role": "mysql"},
    ]
    target = _MemoryMilvus(old_rows)
    request = _mysql_import([
        CleanedKnowledgeRecord(record_id="2", text="新的需求")
    ], replace_existing=False)

    result = asyncio.run(_index_agent(target).import_business_data(request))

    assert result.status == "SUCCESS"
    assert not target.expressions
    assert not target.deleted_ids
    for row in old_rows:
        assert target.rows[row["id"]] == row


@pytest.mark.parametrize("mysql_ids", [(None,), ("2", None)])
def test_mysql_replace_rejects_chunks_without_mysql_id(mysql_ids):
    old_rows = [{"id": "old-2", "mysql_id": "2", "chunk_role": "mysql"}]
    target = _MemoryMilvus(old_rows)
    request = IndexRequest(
        document_id="mysql:demand",
        knowledge_type="demand",
        chunks=[
            IndexChunk(
                chunk_text=f"新的需求 {index}",
                source_locator="mysql:xp_demand|type:record|id:2",
                mysql_id=mysql_id,
                chunk_role="mysql",
            )
            for index, mysql_id in enumerate(mysql_ids)
        ],
        identity=ManualIdentity(user_id="admin", user_role=UserType.ADMIN),
    )

    result = asyncio.run(_index_agent(target).index(request))

    assert result.status == "FAILED"
    assert "mysql_id" in result.error
    assert target.rows == {row["id"]: row for row in old_rows}
    assert not target.expressions
    assert not target.deleted_ids


@pytest.mark.parametrize("scope_id", ["public", "internal"])
@pytest.mark.parametrize(
    "chunk_fields, expected_expr",
    [
        ({"source_id": "source-2", "mysql_id": "2"}, 'chunk_role == "admin_draft" && mysql_id == "2"'),
        ({"mysql_id": "2"}, 'chunk_role == "admin_draft" && mysql_id == "2"'),
    ],
)
def test_non_mysql_publish_keeps_existing_replacement_scope(scope_id, chunk_fields, expected_expr):
    old_rows = [
        {
            "id": role,
            "chunk_role": role,
            "source_id": "source-2",
            "mysql_id": "2",
            "document_id": "doc:2",
            "user_id": "admin",
        }
        for role in ("admin_doc", "admin_draft")
    ]
    other = {
        "id": "other",
        "source_id": "source-other",
        "mysql_id": "3",
        "document_id": "doc:other",
        "user_id": "admin",
    }
    old_rows.append(other)
    if scope_id == "internal":
        old_rows.append({**old_rows[0], "id": "other-user", "user_id": "someone-else"})
    target = _MemoryMilvus(old_rows, requires_user_id=scope_id == "internal")
    agent = _index_agent(target)
    if scope_id == "internal":
        agent.public_milvus = {"demand": _MemoryMilvus()}
    request = IndexRequest(
        document_id="doc:2",
        version_id="doc:2:v2",
        scope_id=scope_id,
        knowledge_type="demand",
        chunks=[IndexChunk(
            chunk_text="新的发布内容",
            source_locator="doc:2|type:draft",
            chunk_role="admin_draft",
            **chunk_fields,
        )],
        identity=ManualIdentity(user_id="admin", user_role=UserType.ADMIN),
    )

    result = asyncio.run(agent.index(request))

    assert result.status == "SUCCESS"
    assert target.deleted_ids == {"admin_draft"}
    assert target.rows["admin_doc"] == old_rows[0]
    assert target.rows["other"] == other
    if scope_id == "internal":
        expected_expr += ' && user_id == "admin"'
        assert target.rows["other-user"] == old_rows[-1]
    assert target.expressions == [expected_expr]
    assert [row["chunk_text"] for row in target.rows.values() if "chunk_text" in row] == [
        "新的发布内容"
    ]


def test_reparse_replaces_old_attachment_chunks_even_when_source_id_changed():
    old_rows = [
        {
            "id": "old-doc",
            "chunk_role": "admin_doc",
            "source_id": "source:old-task",
            "mysql_id": "",
            "document_id": "doc:2",
            "user_id": "admin",
        },
        {
            "id": "unrelated-doc",
            "chunk_role": "admin_doc",
            "source_id": "source:other",
            "mysql_id": "",
            "document_id": "doc:other",
            "user_id": "admin",
        },
    ]
    target = _MemoryMilvus(old_rows)
    agent = _index_agent(target)
    request = IndexRequest(
        document_id="doc:2",
        version_id="doc:2:v1",
        scope_id="public",
        knowledge_type="demand",
        chunks=[
            IndexChunk(
                chunk_text="新解析原文",
                source_locator="doc:2|type:document",
                chunk_role="admin_doc",
                document_id="doc:2",
                source_id="source:new-task",
            ),
            IndexChunk(
                chunk_text="新草稿",
                source_locator="doc:2|type:draft",
                chunk_role="admin_draft",
                mysql_id="mysql-new",
                source_id="source:new-task",
            ),
        ],
        identity=ManualIdentity(user_id="admin", user_role=UserType.ADMIN),
    )

    result = asyncio.run(agent.index(request))

    assert result.status == "SUCCESS"
    assert target.deleted_ids == {"old-doc"}
    assert "unrelated-doc" in target.rows
    assert [row["chunk_text"] for row in target.rows.values() if "chunk_text" in row] == [
        "新解析原文", "新草稿"
    ]


def test_group_publish_replaces_all_submitted_draft_ids_without_deleting_other_group_rows():
    old_rows = [
        {"id": f"draft-{mysql_id}", "chunk_role": "admin_draft", "mysql_id": mysql_id,
         "source_id": "source:shared", "document_id": ""}
        for mysql_id in ("mysql-1", "mysql-2", "mysql-3")
    ]
    target = _MemoryMilvus(old_rows)
    agent = _index_agent(target)
    request = IndexRequest(
        document_id="doc:shared",
        knowledge_type="solution",
        chunks=[
            IndexChunk(
                chunk_text=f"updated {mysql_id}",
                source_locator="draft",
                chunk_role="admin_draft",
                mysql_id=mysql_id,
                source_id="source:shared",
            )
            for mysql_id in ("mysql-1", "mysql-2")
        ],
        identity=ManualIdentity(user_id="admin", user_role=UserType.ADMIN),
    )

    result = asyncio.run(agent.index(request))

    assert result.status == "SUCCESS"
    assert target.deleted_ids == {"draft-mysql-1", "draft-mysql-2"}
    assert "draft-mysql-3" in target.rows
    assert target.expressions == [
        'chunk_role == "admin_draft" && mysql_id in ["mysql-1", "mysql-2"]'
    ]


def test_replacing_one_batch_draft_keeps_sibling_and_shared_document_chunks():
    rows = [
        {
            "id": "draft-1",
            "chunk_role": "agent_draft",
            "mysql_id": "mysql-1",
            "source_id": "batch-1",
            "document_id": "",
        },
        {
            "id": "draft-2",
            "chunk_role": "agent_draft",
            "mysql_id": "mysql-2",
            "source_id": "batch-1",
            "document_id": "",
        },
        {
            "id": "document-1",
            "chunk_role": "agent_doc",
            "mysql_id": "",
            "source_id": "batch-1",
            "document_id": "doc-1",
        },
    ]
    target = _MemoryMilvus(rows)
    request = IndexRequest(
        document_id="doc-1",
        version_id="doc-1:v2",
        scope_id="public",
        knowledge_type="demand",
        replace_existing=True,
        chunks=[
            IndexChunk(
                chunk_text="updated draft",
                source_locator="mysql:record:1",
                mysql_id="mysql-1",
                source_id="batch-1",
                chunk_role="agent_draft",
            )
        ],
        identity=ManualIdentity(user_id="admin", user_role=UserType.ADMIN),
    )

    result = asyncio.run(_index_agent(target).index(request))

    assert result.status == "SUCCESS"
    assert target.expressions == ['chunk_role == "agent_draft" && mysql_id == "mysql-1"']
    assert "draft-1" in target.deleted_ids
    assert "draft-2" not in target.deleted_ids
    assert "document-1" not in target.deleted_ids
    assert target.rows["draft-2"] == rows[1]
    assert target.rows["document-1"] == rows[2]


def test_final_oversized_draft_is_split_without_truncation():
    text = "开头标记\n" + ("长内容。" * 3000) + "\n结尾标记"
    request = IndexRequest(
        document_id="doc:1",
        knowledge_type="solution",
        chunks_are_final=True,
        chunks=[
            IndexChunk(
                chunk_text=text,
                source_locator="document:doc:1|type:text|ordinal:1",
                chunk_role="admin_draft",
            )
        ],
        identity=ManualIdentity(user_id="admin", user_role=UserType.ADMIN),
    )

    chunks = RetrievalAgent.preview_chunks(request)

    assert len(chunks) > 1
    assert max(len(chunk.chunk_text) for chunk in chunks) <= 8192
    assert "开头标记" in "\n".join(chunk.chunk_text for chunk in chunks)
    assert "结尾标记" in "\n".join(chunk.chunk_text for chunk in chunks)


def test_short_tail_text_is_merged_into_previous_chunk_when_under_hard_limit():
    blocks = [
        ChunkPreviewBlock(text="前文" * 490, source_name="manual.txt"),
        ChunkPreviewBlock(text="短尾内容。" * 8, source_name="manual.txt"),
    ]

    chunks = chunk_document_blocks(blocks, target_size=1000, hard_max_size=1300, overlap=80)

    assert len(chunks) == 1
    assert chunks[0].text.endswith("短尾内容。" * 8)
    assert len(chunks[0].text) <= 1300


def test_chunking_preserves_image_ids_from_merged_source_blocks():
    blocks = [
        ChunkPreviewBlock(
            text="正文一",
            source_name="manual.txt",
            metadata={"image_ids": ["image-1"]},
        ),
        ChunkPreviewBlock(
            text="正文二",
            source_name="manual.txt",
            metadata={"image_ids": ["image-2"]},
        ),
    ]

    chunks = chunk_document_blocks(blocks, target_size=1000, hard_max_size=1300, overlap=80)

    assert len(chunks) == 1
    assert chunks[0].metadata["image_ids"] == ["image-1", "image-2"]


def test_chunking_preserves_image_ids_for_index_chunks():
    blocks = [
        IndexChunk(
            chunk_text="正文一",
            source_locator="manual.txt|page:1",
            metadata={"image_ids": ["image-1"]},
        ),
        IndexChunk(
            chunk_text="正文二",
            source_locator="manual.txt|page:1",
            metadata={"image_ids": ["image-2"]},
        ),
    ]

    chunks = chunk_document_blocks(blocks, target_size=1000, hard_max_size=1300, overlap=80)

    assert len(chunks) == 1
    assert chunks[0].metadata["image_ids"] == ["image-1", "image-2"]


def test_page_images_are_assigned_to_only_one_chunk_when_page_is_split():
    image_ids = ["image-1", "image-2"]
    image_refs = [
        {"image_id": image_id, "sources": [f"images/{image_id}.png"]}
        for image_id in image_ids
    ]
    blocks = [
        ChunkPreviewBlock(
            text="这是用于触发页面拆分的正文内容。" * 250,
            source_name="report.pdf",
            page_number=7,
            metadata={
                "page_block": True,
                "image_ids": image_ids,
                "image_refs": image_refs,
            },
        )
    ]

    chunks = chunk_document_blocks(blocks, target_size=1000, hard_max_size=1300, overlap=80)

    assert len(chunks) > 1

    indexed_chunks = RetrievalAgent._prepare_index_chunks(IndexRequest(
        document_id="doc-image-page",
        knowledge_type="solution",
        chunks_are_final=True,
        chunks=[
            IndexChunk(
                chunk_text=chunk.text,
                chunk_index=index,
                source_locator=f"report.pdf|page:7|ordinal:{index + 1}",
                document_id="doc-image-page",
                metadata=chunk.metadata,
            )
            for index, chunk in enumerate(chunks)
        ],
        identity=ManualIdentity(user_id="admin", user_role=UserType.ADMIN),
    ))
    indexed_text = "\n".join(chunk.chunk_text for chunk in indexed_chunks)
    assert all(
        sum(image_id in chunk.metadata.get("image_ids", []) for chunk in indexed_chunks) == 1
        for image_id in image_ids
    )
    assert all(
        indexed_text.count(f"/api/v1/perception/images/doc-image-page/{image_id})") == 1
        for image_id in image_ids
    )


def test_chunking_keeps_markdown_image_reference_whole():
    image = "![企业介绍/office/images/image-1.jpg](企业介绍/office/images/image-1.jpg)"
    chunks = list(chunk_by_paragraph("前文。" * 80 + image + "后文。" * 80, max_chunk_size=100))

    assert any(image in chunk for chunk in chunks)
    assert all(chunk.count("![") == chunk.count("](") for chunk in chunks)


def test_admin_source_blocks_are_chunked_even_when_marked_final():
    request = IndexRequest(
        document_id="doc:source",
        knowledge_type="solution",
        chunks_are_final=True,
        chunk_size=800,
        chunk_overlap=0,
        chunks=[
            IndexChunk(
                chunk_text="方案名称：测试方案",
                source_locator="doc:source|type:draft",
                chunk_role="admin_draft",
            ),
            IndexChunk(
                chunk_text="原文内容。" * 1500,
                source_locator="doc:source|type:text|page:1",
                chunk_role="admin_doc",
            ),
        ],
        identity=ManualIdentity(user_id="admin", user_role=UserType.ADMIN),
    )

    chunks = RetrievalAgent._prepare_index_chunks(request)
    source_chunks = [chunk for chunk in chunks if chunk.chunk_role == "admin_doc"]

    assert len(source_chunks) > 1
    assert max(len(chunk.chunk_text) for chunk in source_chunks) <= 1500


def test_solution_chunk_preview_uses_chunker_for_edited_source():
    image = "![企业介绍/office/images/image-1.jpg](企业介绍/office/images/image-1.jpg)"
    request = ChunkPreviewRequest(
        document_id="doc:solution-preview",
        knowledge_type="solution",
        chunk_size=800,
        chunk_overlap=0,
        blocks=[
            ChunkPreviewBlock(text=("方案正文。" * 300) + image * 4, source_name="sample.pptx")
        ],
        identity={"user_id": "admin", "user_role": "admin"},
    )

    chunks = RetrievalAgent.preview_chunks_from_blocks(request)

    assert len(chunks) > 1
    assert all(chunk.chunk_text.count("![") == chunk.chunk_text.count("](") for chunk in chunks)


def test_solution_chunk_preview_merges_short_adjacent_ppt_slides_and_keeps_range():
    request = ChunkPreviewRequest(
        document_id="doc:ppt-range",
        knowledge_type="solution",
        chunk_size=1000,
        chunk_overlap=0,
        blocks=[
            ChunkPreviewBlock(
                text=f"第{page}页的方案内容",
                page_number=page,
                source_name="sample.pptx",
                metadata={"slide_start": page, "slide_end": page},
            )
            for page in range(1, 4)
        ],
        identity={"user_id": "admin", "user_role": "admin"},
    )

    chunks = RetrievalAgent.preview_chunks_from_blocks(request)

    assert len(chunks) == 1
    assert chunks[0].metadata["page_start"] == 1
    assert chunks[0].metadata["page_end"] == 3
    assert "|slides:1-3|" in chunks[0].source_locator


def test_chunk_preview_restores_accessible_image_urls_from_image_ids():
    request = ChunkPreviewRequest(
        document_id="doc:image-preview",
        knowledge_type="solution",
        blocks=[
            ChunkPreviewBlock(
                text="方案正文",
                source_name="sample.pdf",
                metadata={"image_ids": ["image-1", "image-2"]},
            )
        ],
        identity={"user_id": "admin", "user_role": "admin"},
    )

    chunks = RetrievalAgent.preview_chunks_from_blocks(request)

    assert len(chunks) == 1
    assert chunks[0].document_id == "doc:image-preview"
    assert "/api/v1/perception/images/doc%3Aimage-preview/image-1" in chunks[0].chunk_text
    assert "/api/v1/perception/images/doc%3Aimage-preview/image-2" in chunks[0].chunk_text


def test_published_index_drops_image_ids_removed_from_saved_manifest():
    target = _MemoryMilvus()
    agent = _index_agent(target)

    class ImageManifest:
        async def list_for_document(self, document_id):
            assert document_id == "doc:image-publish"
            return [{"image_id": "kept-image"}, {"image_id": "unannotated-image"}]

    agent.image_manifest = ImageManifest()
    request = IndexRequest(
        document_id="doc:image-publish",
        knowledge_type="solution",
        chunks=[
            IndexChunk(
                chunk_text=(
                    "发布后的正文\n"
                    "![图片1](/api/v1/perception/images/doc%3Aimage-publish/deleted-image)\n"
                    "![图片2](/api/v1/perception/images/doc%3Aimage-publish/kept-image)"
                ),
                source_locator="doc:image-publish|page:1",
                chunk_role="admin_doc",
                document_id="doc:image-publish",
                metadata={"image_ids": ["deleted-image", "kept-image"]},
            ),
            IndexChunk(
                chunk_text="![图片](/api/v1/perception/images/doc%3Aimage-publish/unannotated-image)",
                source_locator="doc:image-publish|page:2",
                chunk_role="admin_doc",
                document_id="doc:image-publish",
            ),
        ],
        identity=ManualIdentity(user_id="admin", user_role=UserType.ADMIN),
    )

    result = asyncio.run(agent.index(request))

    assert result.status == "SUCCESS"
    stored_text = "\n".join(row["chunk_text"] for row in target.rows.values())
    assert "deleted-image" not in stored_text
    assert "/api/v1/perception/images/doc%3Aimage-publish/kept-image" in stored_text
    assert "/api/v1/perception/images/doc%3Aimage-publish/unannotated-image" in stored_text


def test_chunk_preview_replaces_image_url_in_original_position_without_copying_sibling_images():
    request = ChunkPreviewRequest(
        document_id="doc:image-position",
        knowledge_type="solution",
        chunk_size=1000,
        blocks=[
            ChunkPreviewBlock(
                text=("前文\n![图一](/office/images/image-1.jpg)\n中间内容\n后文"),
                source_name="sample.pptx",
                metadata={
                    "image_ids": ["image-1", "image-2"],
                    "image_refs": [
                        {"image_id": "image-1", "sources": ["/office/images/image-1.jpg"]},
                        {"image_id": "image-2", "sources": ["/office/images/image-2.jpg"]},
                    ],
                },
            )
        ],
        identity={"user_id": "admin", "user_role": "admin"},
    )

    chunks = RetrievalAgent.preview_chunks_from_blocks(request)

    text = chunks[0].chunk_text
    image_url = "/api/v1/perception/images/doc%3Aimage-position/image-1"
    assert text.count(image_url) == 1
    assert text.index(image_url) < text.index("中间内容")
    assert "image-2" not in text


def test_unmatched_image_reference_is_in_preview_and_stored_chunk_text():
    request = IndexRequest(
        document_id="doc:image-unmatched-ref",
        knowledge_type="solution",
        replace_existing=False,
        chunks=[
            IndexChunk(
                chunk_text="正文没有包含解析器提供的图片路径",
                source_locator="doc:image-unmatched-ref|page:1",
                chunk_role="admin_doc",
                metadata={
                    "image_ids": ["image-1"],
                    "image_refs": [
                        {"image_id": "image-1", "sources": ["/office/images/renamed.jpg"]}
                    ],
                },
            )
        ],
        identity=ManualIdentity(user_id="admin", user_role=UserType.ADMIN),
    )
    image_url = "/api/v1/perception/images/doc%3Aimage-unmatched-ref/image-1"
    preview = RetrievalAgent.preview_chunks(request)
    target = _MemoryMilvus()
    agent = _index_agent(target)

    class ImageManifest:
        async def list_for_document(self, document_id):
            assert document_id == "doc:image-unmatched-ref"
            return [{"image_id": "image-1"}]

    agent.image_manifest = ImageManifest()
    result = asyncio.run(agent.index(request))

    assert result.status == "SUCCESS"
    assert image_url in preview[0].chunk_text
    stored_text = next(iter(target.rows.values()))["chunk_text"]
    assert stored_text == preview[0].chunk_text
