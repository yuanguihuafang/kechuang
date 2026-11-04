from agents.retrieval_agent.application.retrieval_service import RetrievalAgent, index_record_id
from agents.retrieval_agent.schemas import IndexChunk, IndexRequest


def test_saved_final_chunks_keep_content_order_and_indexes():
    chunks = [IndexChunk(
        chunk_text="人工编辑\n" + "长正文。" * 400,
        chunk_index=7,
        document_id="doc-2",
        source_id="source-group",
        chunk_role="admin_doc",
        source_locator="附件二.pdf|page:9",
    ), IndexChunk(
        chunk_text="相同正文仍属于不同附件",
        chunk_index=2,
        document_id="doc-1",
        source_id="source-group",
        chunk_role="admin_doc",
        source_locator="附件一.pdf|page:3",
    )]
    request = IndexRequest(
        document_id="doc-1", knowledge_type="solution", chunks=chunks,
        chunks_are_final=True, identity={"user_id": "admin", "user_role": "admin"},
    )
    restored = RetrievalAgent.preview_chunks(request)
    assert [(c.chunk_text, c.chunk_index, c.document_id, c.source_locator) for c in restored] == [
        (c.chunk_text, c.chunk_index, c.document_id, c.source_locator) for c in chunks
    ]


def test_attachment_primary_key_does_not_depend_on_enclosing_draft_document():
    chunk = IndexChunk(
        chunk_text="附件原文", source_locator="附件.pdf|page:1", document_id="doc-2",
        source_id="source-group", chunk_role="admin_doc",
    )
    assert index_record_id("doc-1", chunk) == index_record_id("doc-2", chunk)


def test_rechunking_never_merges_different_attachments():
    request = IndexRequest(
        document_id="doc-1", knowledge_type="solution",
        chunks=[IndexChunk(chunk_text=text, document_id=doc, source_id="group",
            chunk_role="admin_doc", source_locator=f"{doc}.pdf|page:1")
            for text, doc in [("附件一的独立正文", "doc-1"), ("附件二的独立正文", "doc-2")]],
        identity={"user_id": "admin", "user_role": "admin"},
    )
    chunks = RetrievalAgent._expand_chunks(request)
    assert [(c.document_id, c.chunk_text) for c in chunks] == [
        ("doc-1", "附件一的独立正文"), ("doc-2", "附件二的独立正文")
    ]


def test_same_text_in_distinct_attachments_is_not_dropped():
    from agents.retrieval_agent.application.retrieval_helpers import deduplicate_source_chunks
    chunks = [IndexChunk(chunk_text="同样的正文", source_locator=f"{doc}.pdf|page:1", document_id=doc)
              for doc in ("doc-1", "doc-2")]
    assert deduplicate_source_chunks(chunks) == chunks
