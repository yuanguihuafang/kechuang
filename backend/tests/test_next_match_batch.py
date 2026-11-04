import asyncio
from types import SimpleNamespace

from agents.retrieval_agent.retrieval.rerank_service import (
    RerankOutcome,
    RerankUnavailableError,
)
from agents.retrieval_agent.retrieval.search_service import SearchService, _hit_business_key
from agents.retrieval_agent.schemas import RetrievalHit
from core.contracts.chat import ChatRequest, ChatResponse
from core.governance.access_policy import ManualIdentity, UserType
from services.application.chat_response import (
    build_recommendation_items,
    compose_business_response,
    match_result_identity,
    select_match_results,
)


def test_recommendation_builder_never_exposes_more_than_three_match_results():
    items = [
        {
            "record_id": f"result-{index}",
            "source_knowledge_type": "solution",
            "text": f"方案{index}",
        }
        for index in range(5)
    ]

    assert len(build_recommendation_items(items, limit=5)) == 3
    assert len(select_match_results(items, limit=5)) == 3


def test_match_selection_keeps_only_highest_score_for_shared_source_id():
    items = [
        {
            "id": "draft-chunk",
            "score": 0.91,
            "knowledge_type": "solution",
            "chunk_role": "agent_draft",
            "source_id": "source-1",
        },
        {
            "id": "source-chunk",
            "score": 0.95,
            "knowledge_type": "solution",
            "chunk_role": "agent_doc",
            "source_id": "source-1",
        },
        {"id": "source-2", "score": 0.80, "knowledge_type": "solution"},
        {"id": "source-3", "score": 0.70, "knowledge_type": "solution"},
    ]

    selected = select_match_results(items)

    assert [item["id"] for item in selected] == ["source-chunk", "source-2", "source-3"]


def test_recommendation_builder_deduplicates_draft_and_source_chunks_by_score():
    evidences = [
        {
            "record_id": "source-chunk",
            "score": 0.95,
            "source_knowledge_type": "solution",
            "source_id": "source-1",
            "chunk_role": "agent_doc",
            "text": "原文块",
        },
        {
            "record_id": "draft-chunk",
            "score": 0.91,
            "source_knowledge_type": "solution",
            "source_id": "source-1",
            "chunk_role": "agent_draft",
            "text": "草稿块",
        },
        {
            "record_id": "source-2",
            "score": 0.80,
            "source_knowledge_type": "solution",
            "text": "第二条",
        },
        {
            "record_id": "source-3",
            "score": 0.70,
            "source_knowledge_type": "solution",
            "text": "第三条",
        },
    ]

    selected = build_recommendation_items(evidences)

    assert [item["id"] for item in selected] == ["source-chunk", "source-2", "source-3"]


def test_retrieval_final_selection_keeps_highest_chunk_per_business_data():
    hits = [
        RetrievalHit(
            record_id="draft-chunk",
            score=0.91,
            source="milvus",
            metadata={
                "knowledge_type": "solution",
                "source_id": "source-1",
                "chunk_role": "agent_draft",
            },
        ),
        RetrievalHit(
            record_id="source-chunk",
            score=0.95,
            source="milvus",
            metadata={
                "knowledge_type": "solution",
                "source_id": "source-1",
                "chunk_role": "agent_doc",
            },
        ),
        RetrievalHit(
            record_id="source-2",
            score=0.80,
            source="milvus",
            metadata={"knowledge_type": "solution", "source_id": "source-2"},
        ),
        RetrievalHit(
            record_id="source-3",
            score=0.70,
            source="milvus",
            metadata={"knowledge_type": "solution", "source_id": "source-3"},
        ),
    ]

    selected = SearchService._select_final_hits(hits, top_k=3)

    assert [hit.record_id for hit in selected] == ["source-chunk", "source-2", "source-3"]


def test_search_reranks_enough_candidates_to_fill_next_batch_after_exclusions():
    class Strategy:
        def __init__(self):
            self.rerank_top_k = None

        async def recall(self, _queries, _targets, *, recall_k=None):
            return [
                [
                    RetrievalHit(record_id=f"result-{index}", score=1 - index / 20, source="milvus")
                    for index in range(12)
                ]
            ]

        def merge_chunks(self, query_hit_lists):
            return query_hit_lists[0]

        async def rerank(self, _query, hits, *, top_k):
            self.rerank_top_k = top_k
            return hits[:top_k], RerankOutcome(results=[], applied=True)

    strategy = Strategy()
    service = object.__new__(SearchService)
    service.settings = SimpleNamespace(
        rerank_top_k=3,
        rerank_enabled=True,
        rag_fusion_candidates=60,
        rag_recall_k=20,
        rag_retrieval_mode="full",
    )
    service.strategy = strategy
    service._resolve_search_targets = lambda *_args: ["collection"]

    result = asyncio.run(
        service.search(
            query="设备采购",
            top_k=8,
            use_rerank=True,
            identity=ManualIdentity(user_id="user-1", user_role=UserType.INTERNAL),
        )
    )

    assert strategy.rerank_top_k >= 8
    assert len(result) == 8


def test_search_returns_original_score_order_when_rerank_fails():
    class Strategy:
        async def recall(self, _queries, _targets, *, recall_k):
            return [[
                RetrievalHit(record_id="lower", score=0.2, source="milvus"),
                RetrievalHit(record_id="higher", score=0.9, source="milvus"),
                RetrievalHit(record_id="middle", score=0.5, source="milvus"),
            ]]

        def merge_chunks(self, query_hit_lists):
            return query_hit_lists[0]

        async def rerank(self, _query, _hits, *, top_k):
            raise RerankUnavailableError("reranker returned HTTP 400")

    service = object.__new__(SearchService)
    service.settings = SimpleNamespace(
        rerank_top_k=3,
        rerank_enabled=True,
        rag_fusion_candidates=60,
        rag_recall_k=3,
        rag_retrieval_mode="full",
    )
    service.strategy = Strategy()
    service._resolve_search_targets = lambda *_args: ["collection"]

    result = asyncio.run(
        service.search_with_trace(
            query="设备采购",
            top_k=2,
            identity=ManualIdentity(user_id="user-1", user_role=UserType.INTERNAL),
        )
    )

    assert [hit.record_id for hit in result.hits] == ["higher", "middle"]
    assert [hit.score for hit in result.hits] == [0.9, 0.5]
    assert result.rerank_applied is False


def test_search_falls_back_when_rerank_returns_not_applied():
    class Strategy:
        async def recall(self, _queries, _targets, *, recall_k):
            return [[
                RetrievalHit(record_id="lower", score=0.2, source="milvus"),
                RetrievalHit(record_id="higher", score=0.9, source="milvus"),
            ]]

        def merge_chunks(self, query_hit_lists):
            return query_hit_lists[0]

        async def rerank(self, _query, hits, *, top_k):
            return hits[:top_k], RerankOutcome(results=[], applied=False)

    service = object.__new__(SearchService)
    service.settings = SimpleNamespace(
        rerank_top_k=3,
        rerank_enabled=True,
        rag_fusion_candidates=60,
        rag_recall_k=3,
        rag_retrieval_mode="full",
    )
    service.strategy = Strategy()
    service._resolve_search_targets = lambda *_args: ["collection"]

    result = asyncio.run(
        service.search_with_trace(
            query="设备采购",
            top_k=2,
            identity=ManualIdentity(user_id="user-1", user_role=UserType.INTERNAL),
        )
    )

    assert [hit.record_id for hit in result.hits] == ["higher", "lower"]
    assert result.rerank_applied is False


def test_search_excludes_seen_ids_before_rerank_and_expands_recall_window():
    excluded_id = "solution:source:seen"

    class Strategy:
        def __init__(self):
            self.recall_k = None
            self.rerank_ids = []

        async def recall(self, _queries, _targets, *, recall_k):
            self.recall_k = recall_k
            return [[
                RetrievalHit(
                    record_id="seen",
                    score=1.0,
                    source="milvus",
                    metadata={
                        "source_id": "seen",
                        "knowledge_type": "solution",
                        "chunk_role": "agent_draft",
                    },
                ),
                *[
                    RetrievalHit(
                        record_id=f"new-{index}",
                        score=0.9 - index / 10,
                        source="milvus",
                        metadata={
                            "source_id": f"new-{index}",
                            "knowledge_type": "solution",
                            "chunk_role": "agent_doc",
                        },
                    )
                    for index in range(3)
                ],
            ]]

        def merge_chunks(self, query_hit_lists):
            return query_hit_lists[0]

        async def rerank(self, _query, hits, *, top_k):
            self.rerank_ids = [hit.record_id for hit in hits]
            return hits[:top_k], RerankOutcome(results=[], applied=True)

    strategy = Strategy()
    service = object.__new__(SearchService)
    service.settings = SimpleNamespace(
        rerank_top_k=3,
        rerank_enabled=True,
        rag_fusion_candidates=60,
        rag_recall_k=3,
        rag_retrieval_mode="full",
    )
    service.strategy = strategy
    service._resolve_search_targets = lambda *_args: ["collection"]

    result = asyncio.run(
        service.search_with_trace(
            query="设备采购",
            top_k=3,
            exclude_result_ids=[excluded_id],
            identity=ManualIdentity(user_id="user-1", user_role=UserType.INTERNAL),
        )
    )

    assert strategy.recall_k == 4
    assert strategy.rerank_ids == ["new-0", "new-1", "new-2"]
    assert [hit.record_id for hit in result.hits] == ["new-0", "new-1", "new-2"]

    asyncio.run(
        service.search_with_trace(
            query="设备采购",
            top_k=3,
            exclude_result_ids=[f"solution:mysql:{index}" for index in range(100)],
            identity=ManualIdentity(user_id="user-1", user_role=UserType.INTERNAL),
        )
    )
    assert strategy.recall_k == 50


def test_next_batch_identity_fills_missing_knowledge_type_for_records():
    assert (
        match_result_identity(
            {"record_id": "16", "mysql_id": "16"},
            default_knowledge_type="solution",
        )
        == "solution:mysql:16"
    )


def test_next_batch_identity_prefers_shared_source_id_over_chunk_id():
    draft_chunk = {
        "id": "draft-record",
        "source": {"source_id": "source:shared"},
        "knowledge_type": "solution",
    }
    source_chunk = {
        "id": "source-record",
        "source": {"source_id": "source:shared"},
        "knowledge_type": "solution",
    }

    assert match_result_identity(draft_chunk) == match_result_identity(source_chunk)


def test_next_batch_identity_uses_source_locator_as_last_fallback():
    assert (
        match_result_identity(
            {
                "source": {"source_locator": "demo.pdf|type:text|page:2"},
                "knowledge_type": "solution",
            }
        )
        == "solution:locator:demo.pdf|type:text|page:2"
    )


def test_business_grouping_prefers_mysql_id_over_shared_source_id():
    item = {
        "source_knowledge_type": "solution",
        "source_id": "source:shared",
        "mysql_id": "mysql-1",
        "record_id": "chunk-1",
    }
    assert (
        _hit_business_key(
            RetrievalHit(record_id="chunk-1", score=1, source="milvus", metadata=item)
        )
        == "business:solution:mysql:mysql-1"
    )
    assert match_result_identity(item) == "solution:mysql:mysql-1"


def test_business_identity_uses_linked_draft_for_source_context():
    item = {
        "knowledge_type": "solution", "chunk_role": "admin_doc",
        "document_id": "doc-1", "source_id": "shared", "mysql_id": "legacy",
        "matched_draft_mysql_id": "102",
    }
    assert match_result_identity(item) == "solution:mysql:102"
    assert _hit_business_key(RetrievalHit("original", 1, "milvus", item)) == "business:solution:mysql:102"


def test_business_identity_retains_siblings_and_knowledge_type_boundaries():
    rows = [
        {"record_id": key, "knowledge_type": kind, "mysql_id": mysql_id,
         "source_id": "shared", "chunk_role": "admin_draft", "score": score}
        for key, kind, mysql_id, score in [
            ("one", "solution", "1", 0.99),
            ("two", "solution", "2", 0.98),
            ("other-type", "achievement", "1", 0.97),
        ]
    ]
    assert len({match_result_identity(row) for row in rows}) == 3
    assert [item["record_id"] for item in select_match_results(rows)] == ["one", "two", "other-type"]
    merged = SearchService._merge_query_hits([[RetrievalHit(
        row["record_id"], row["score"], "milvus", row
    ) for row in rows]])
    assert [hit.record_id for hit in merged] == ["one", "two", "other-type"]


def test_source_explicit_draft_link_is_not_reassigned_to_higher_scoring_sibling():
    hits = [RetrievalHit(key, score, "milvus", {
        "knowledge_type": "solution", "source_id": "shared", "chunk_role": role,
        "mysql_id": mysql_id, "matched_draft_mysql_id": linked_id,
        "document_id": "doc-1" if role == "admin_doc" else None,
    }) for key, score, role, mysql_id, linked_id in [
        ("best-sibling", 0.99, "admin_draft", "101", None),
        ("linked-draft", 0.9, "admin_draft", "102", None),
        ("original", 0.95, "admin_doc", None, "102"),
    ]]
    selected = SearchService._select_final_hits(hits, top_k=3)
    assert [hit.record_id for hit in selected] == ["best-sibling", "original"]
    assert selected[1].metadata["matched_draft_mysql_id"] == "102"


def test_final_selection_keeps_sibling_drafts_and_deduplicates_business_copies():
    def hit(key, score, role, mysql_id=None, source_id="shared", document_id=None):
        return RetrievalHit(key, score, "milvus", {
            "knowledge_type": "solution", "chunk_role": role, "mysql_id": mysql_id,
            "source_id": source_id, "document_id": document_id,
            "chunk_index": 0, "chunk_text": "original text" if role == "admin_doc" else key,
        })

    hits = [
        hit("draft-best", 0.99, "admin_draft", "101"),
        hit("mysql-copy", 0.98, "mysql", "101", source_id="legacy-source"),
        hit("draft-sibling", 0.90, "admin_draft", "102"),
        hit("original", 0.95, "admin_doc", document_id="doc-1"),
        hit("original-copy", 0.94, "admin_doc", document_id="doc-1"),
    ]
    selected = SearchService._select_final_hits(hits, top_k=3)
    assert [item.record_id for item in selected] == ["original", "draft-sibling"]
    assert selected[0].metadata["matched_draft_mysql_id"] == "101"
    cards = build_recommendation_items([{**item.metadata, "record_id": item.record_id, "score": item.score} for item in selected])
    assert [item["id"] for item in cards] == ["original", "draft-sibling"]


def test_same_original_under_different_sources_keeps_other_business_draft():
    hits = []
    for source, score in [("a", 0.9), ("b", 0.99)]:
        hits.extend([
            RetrievalHit(f"draft-{source}", score, "milvus", {
                "knowledge_type": "solution", "source_id": source, "mysql_id": source,
                "chunk_role": "admin_draft", "chunk_index": 0,
            }),
            RetrievalHit(f"original-{source}", 0.8, "milvus", {
                "knowledge_type": "solution", "source_id": source, "document_id": "doc-1",
                "chunk_role": "admin_doc", "chunk_index": 0, "chunk_text": "same original",
            }),
        ])
    selected = SearchService._select_final_hits(hits, top_k=3)
    assert [item.record_id for item in selected] == ["original-b", "draft-a"]


def test_mysql_chunk_role_falls_back_to_mysql_id_without_source_id():
    item = {
        "chunk_role": "mysql",
        "mysql_id": "16",
        "knowledge_type": "solution",
    }
    assert match_result_identity(item) == "solution:mysql:16"
    assert (
        _hit_business_key(
            RetrievalHit(record_id="chunk-1", score=1, source="milvus", metadata=item)
        )
        == "business:solution:mysql:16"
    )


def test_three_match_results_offer_next_batch_action():
    request = ChatRequest(
        message="工业机器人采购",
        user_id="user-1",
        intent_hint="supply_demand_match",
    )
    evidences = [
        {
            "record_id": f"result-{index}",
            "source_knowledge_type": "solution",
            "text": f"方案{index}",
            "score": index,
        }
        for index in range(5)
    ]

    blocks, actions = compose_business_response(
        request,
        answer="已找到相关匹配结果。",
        status="SUCCESS",
        evidences=evidences,
        images=[],
        documents=[],
    )

    recommendation = next(block for block in blocks if block["type"] == "recommendation_list")
    assert recommendation["data"]["count"] == 3
    assert any(action["type"] == "NEXT_MATCH_BATCH" for action in actions)


def test_next_batch_hides_seen_document_and_linked_draft_but_returns_sibling_draft():
    from agents.retrieval_agent.application.retrieval_helpers import to_evidence
    from assistant.integrations.mcp_server import (
        RetrievalToolInput,
        apply_pending_retrieval_batch,
        filter_match_result,
        resolve_next_match_batch,
    )
    from services.application.conversation_state import ConversationStateService

    def hit(record_id, score, role, mysql_id=None, document_id=None, source_id="source-a", index=0):
        return RetrievalHit(record_id, score, "milvus", {
            "knowledge_type": "solution", "source_knowledge_type": "solution",
            "source_id": source_id, "mysql_id": mysql_id, "document_id": document_id,
            "version_id": "v1", "chunk_role": role, "chunk_index": index,
            "chunk_text": "shared original" if role == "agent_doc" else record_id,
        })

    hits = [
        hit("draft-best", 0.99, "agent_draft", mysql_id="101"),
        hit("draft-best-tail", 0.98, "agent_draft", mysql_id="101", index=1),
        hit("mysql-best", 0.97, "mysql", mysql_id="101"),
        hit("original", 0.95, "agent_doc", document_id="doc-1"),
        hit("duplicate-original", 0.94, "agent_doc", document_id="doc-1", source_id="source-b"),
        hit("original-other-chunk", 0.93, "agent_doc", document_id="doc-1", index=1),
        hit("draft-sibling", 0.9, "agent_draft", mysql_id="102"),
        hit("other-document", 0.8, "agent_doc", document_id="doc-2", source_id="source-c"),
    ]
    rerank_inputs = []

    class Strategy:
        async def recall(self, _queries, _targets, *, recall_k):
            return [hits]

        def merge_chunks(self, lists):
            return SearchService._merge_query_chunks(lists)

        async def rerank(self, _query, candidates, *, top_k):
            rerank_inputs.append([item.record_id for item in candidates])
            return sorted(candidates, key=lambda item: item.score, reverse=True)[:top_k], RerankOutcome(
                results=[], applied=True
            )

    service = object.__new__(SearchService)
    service.settings = SimpleNamespace(
        rerank_top_k=20, rerank_enabled=True, rag_fusion_candidates=60,
        rag_recall_k=15, rag_retrieval_mode="full",
    )
    service.strategy = Strategy()
    service._resolve_search_targets = lambda *_: ["collection"]
    identity = ManualIdentity(user_id="u", user_role=UserType.INTERNAL)
    batch = {}
    excluded = set()
    returned = []
    for batch_index in range(4):
        found = asyncio.run(service.search_with_trace(
            query="robot", top_k=1, knowledge_type="solution", identity=identity,
            exclude_result_ids=sorted(excluded),
        ))
        evidences = [to_evidence(item).model_dump() for item in found.hits]
        filtered = filter_match_result({"evidences": evidences}, excluded, top_k=1)
        cards = build_recommendation_items(filtered["evidences"])
        returned.append([item["id"] for item in cards])
        result = ChatResponse(
            status="SUCCESS" if cards else "NO_EVIDENCE", answer="result", match_results=cards,
            intent={"operation": "knowledge_search", "query": "robot", "entities": {
                "knowledge_types": ["solution"], "next_match_batch": batch_index > 0,
                "web_fallback_allowed": batch_index == 0,
            }},
        )
        batch = ConversationStateService._next_match_batch_state(
            {"match_batch": batch}, result,
            request_message=(
                "帮我查河北利仕化学，我一会要走访并挖掘场景需求"
                if batch_index == 0 else "换一批"
            ),
        )
        if batch_index == 0:
            assert cards[0]["matched_draft_record_id"] == "draft-best"
            assert set(batch["shown_result_ids"]) == {"solution:document:doc-1", "solution:mysql:101"}
        pending = resolve_next_match_batch(True, {"chat_flow": {"match_batch": batch}})
        assert pending["user_goal"] == "帮我查河北利仕化学，我一会要走访并挖掘场景需求"
        assert pending["web_fallback_allowed"] is True
        params, excluded = apply_pending_retrieval_batch(RetrievalToolInput(query="ignored"), pending)
        assert params.query == "robot"

    assert returned == [["original"], ["draft-sibling"], ["other-document"], []]
    assert set(rerank_inputs[1]) == {"draft-sibling", "other-document"}
    assert set(batch["shown_result_ids"]) == {
        "solution:document:doc-1", "solution:mysql:101",
        "solution:mysql:102", "solution:document:doc-2",
    }
    # 新问题重新建立展示集；先前批次不影响新检索。
    reset = ConversationStateService._next_match_batch_state(
        {"match_batch": batch}, ChatResponse(
            status="SUCCESS", answer="new query", match_results=[{
                "id": "original", "knowledge_type": "solution", "chunk_role": "agent_doc",
                "document_id": "doc-1", "source_id": "source-a",
            }], intent={"operation": "knowledge_search", "query": "new query"},
        ),
    )
    assert reset["shown_result_ids"] == ["solution:document:doc-1"]


def test_next_batch_tool_filter_keeps_other_drafts_under_the_same_source():
    from assistant.integrations.mcp_server import filter_match_result

    rows = [
        {"record_id": "original", "chunk_role": "admin_doc", "document_id": "doc-1"},
        {"record_id": "seen-draft", "chunk_role": "admin_draft", "mysql_id": "101"},
        {"record_id": "other-draft", "chunk_role": "admin_draft", "mysql_id": "102"},
    ]
    rows = [{**row, "knowledge_type": "solution", "source_id": "shared"} for row in rows]
    filtered = filter_match_result({"data": {
        "evidences": rows, "records": [{"evidences": [row]} for row in rows],
    }}, {"solution:document:doc-1", "solution:mysql:101"}, top_k=3)

    assert [row["record_id"] for row in filtered["data"]["evidences"]] == ["other-draft"]
    assert [row["evidences"][0]["record_id"] for row in filtered["data"]["records"]] == ["other-draft"]
