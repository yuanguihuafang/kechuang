import asyncio
from types import SimpleNamespace

import pytest

from agents.retrieval_agent.application.retrieval_service import RetrievalAgent
from agents.retrieval_agent.schemas import IndexChunk, IndexRequest
from core.governance.access_policy import ManualIdentity


@pytest.mark.parametrize(("inserted", "expected_status"), [(1, "SUCCESS"), (0, "SUCCESS")])
def test_shadow_indexing_reuses_index_path_without_touching_active_collection(
    monkeypatch, inserted, expected_status
):
    writes = []

    class ShadowTarget:
        def insert_chunks(self, rows, *, flush=False):
            writes.append((rows, flush))
            return inserted

    agent = object.__new__(RetrievalAgent)
    agent.image_manifest = SimpleNamespace(list_for_document=lambda _: [])
    async def encode_dense(texts):
        return [[0.0] * 1024 for _ in texts]

    agent.embedding = SimpleNamespace(encode_dense=encode_dense)
    agent._milvus_for_visibility = lambda *_: pytest.fail("active Collection must not be resolved")
    chunk = IndexChunk(
        chunk_text="恢复后的管理员确认正文",
        source_locator="source.pdf|page:1",
        chunk_role="admin_draft",
        mysql_id="27",
        source_id="source-27",
    )
    monkeypatch.setattr(
        RetrievalAgent,
        "_prepare_index_chunks",
        staticmethod(lambda *_args, **_kwargs: [chunk]),
    )
    request = IndexRequest(
        document_id="draft:27",
        knowledge_type="solution",
        chunks=[chunk],
        replace_existing=True,
        identity=ManualIdentity(user_id="admin", user_role="admin"),
    )

    result = asyncio.run(
        agent.index(request, target_override=ShadowTarget(), insert_only=True)
    )

    assert result.status == expected_status, result.error
    assert result.indexed_count == inserted
    assert len(writes) == 1
    payloads, flush = writes[0]
    assert flush is False
    assert payloads[0]["chunk_text"] == "恢复后的管理员确认正文"
    assert payloads[0]["source_id"] == "source-27"
