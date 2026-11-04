import asyncio
import importlib
from types import SimpleNamespace

import pytest

from agents.retrieval_agent.retrieval import search_service as search_service_module
from agents.retrieval_agent.retrieval.search_service import SearchService
from agents.retrieval_agent.schemas import RetrievalHit
from core.governance.access_policy import ManualIdentity
from services.storage.minio.image_service import ImageManifestService


class _ChunkStore:
    def __init__(self, hits: list[RetrievalHit]) -> None:
        self.hits = hits

    def query_document_chunks(self, **_: object) -> list[RetrievalHit]:
        return self.hits


class _ImageManifest:
    async def find_many(
        self,
        locations: list[tuple[str, str]],
        *,
        references: list[list[str]] | None = None,
    ) -> list[list[dict[str, object]]]:
        return [[] for _ in locations]


def test_chunk_detail_reports_manifest_outage_without_losing_text(monkeypatch):
    class UnavailableManifest:
        async def find_many(self, *args, **kwargs):
            raise TimeoutError("private infrastructure details")

    hit = RetrievalHit("chunk", 1, "milvus", {
        "document_id": "doc-1", "source_id": "source-1", "chunk_role": "agent_doc",
        "chunk_index": 0, "chunk_text": "正文\n![图片](/api/v1/perception/images/doc-1/image-1)",
    })
    service = object.__new__(SearchService)
    service._resolve_search_targets = lambda *_: [(_ChunkStore([hit]), None)]
    monkeypatch.setattr(search_service_module, "ImageManifestService", UnavailableManifest)
    detail = asyncio.run(service.get_chunk_detail(
        document_id="doc-1", mysql_id=None, source_id="source-1", chunk_role="agent_doc",
        chunk_index=0, knowledge_type="solution", identity=ManualIdentity(user_id="u", user_role="internal"),
    ))
    assert "正文" in detail["text"]
    assert detail["image_error_code"] == "IMAGE_MANIFEST_UNAVAILABLE"
    assert "暂时" in detail["image_error"]
    assert "private infrastructure" not in str(detail)
    assert detail["image_urls"] == []


def test_image_manifest_matches_explicit_chunk_reference_without_page_locator():
    referenced = {
        "image_id": "image-1",
        "relativePath": "images/d923282f.jpg",
        "page_number": 2,
    }
    unrelated = {
        "image_id": "image-2",
        "relativePath": "images/other.jpg",
        "page_number": 3,
    }
    manifest = ImageManifestService.__new__(ImageManifestService)
    manifest._load_manifest = lambda _: [referenced, unrelated]

    images = asyncio.run(
        manifest.find_many(
            [("doc-1", "doc-1|chunk:5")],
            references=[["images/d923282f.jpg"]],
        )
    )

    assert images == [[referenced]]


def test_image_manifest_matches_explicit_image_id_without_page_locator():
    image = {"image_id": "image-1", "relativePath": "images/chart.jpg"}
    manifest = ImageManifestService.__new__(ImageManifestService)
    manifest._load_manifest = lambda _: [image]

    images = asyncio.run(
        manifest.find_many(
            [("doc-1", "doc-1|chunk:5")],
            references=[["image-1"]],
        )
    )

    assert images == [[image]]


def test_image_manifest_omits_missing_minio_objects():
    class Client:
        def head_object(self, *, Bucket, Key):
            if Key == "derived/doc-1/images/missing.jpg":
                from botocore.exceptions import ClientError

                raise ClientError({"Error": {"Code": "NoSuchKey"}}, "HeadObject")
            return {"ContentLength": 10}

    manifest = ImageManifestService.__new__(ImageManifestService)
    manifest.bucket = "test"
    manifest.client = Client()
    manifest._load_manifest = lambda _: [
        {"image_id": "available", "minio_object_key": "derived/doc-1/images/available.jpg",
         "page_number": 1},
        {"image_id": "missing", "minio_object_key": "derived/doc-1/images/missing.jpg",
         "page_number": 1},
    ]

    images = asyncio.run(manifest.find_many([("doc-1", "doc-1|page:1")]))

    assert [image["image_id"] for image in images[0]] == ["available"]


def test_chunk_detail_rewrites_image_reference_using_owning_document_id(monkeypatch):
    image = {
        "image_id": "image-1",
        "relativePath": "images/d923282f.jpg",
    }

    class _ReferencedImageManifest:
        async def find_many(self, locations, *, references=None):
            assert locations == [("doc-1", "doc-1|chunk:5")]
            assert references == [["images/d923282f.jpg"]]
            return [[image]]

    hit = RetrievalHit(
        record_id="chunk-5",
        score=1.0,
        source="milvus",
        metadata={
            "document_id": "doc-1",
            "source_id": "source-1",
            "chunk_role": "agent_doc",
            "chunk_index": 5,
            "source_locator": "doc-1|chunk:5",
            "chunk_text": "方案图片：![证据图片](images/d923282f.jpg)",
        },
    )
    service = object.__new__(SearchService)
    service._resolve_search_targets = lambda *_: [(_ChunkStore([hit]), None)]
    monkeypatch.setattr(
        search_service_module,
        "ImageManifestService",
        _ReferencedImageManifest,
    )

    detail = asyncio.run(
        service.get_chunk_detail(
            document_id="doc-1",
            mysql_id=None,
            source_id="source-1",
            chunk_role="agent_doc",
            chunk_index=5,
            knowledge_type="solution",
            identity=ManualIdentity(user_id="user-1", user_role="internal"),
        )
    )

    assert detail is not None
    assert detail["source_chunks"][0]["text"] == (
        "方案图片：![证据图片](/api/v1/perception/images/doc-1/image-1)"
    )


def test_chunk_detail_omits_image_ids_missing_from_manifest(monkeypatch):
    class _SingleImageManifest:
        async def find_many(self, locations, *, references=None):
            return [[{"image_id": "available-image", "relativePath": "images/available.jpg"}]]

    hit = RetrievalHit(
        record_id="chunk-5",
        score=1.0,
        source="milvus",
        metadata={
            "document_id": "doc-1",
            "source_id": "source-1",
            "chunk_role": "agent_doc",
            "chunk_index": 5,
            "source_locator": "doc-1|chunk:5",
            "chunk_text": (
                "![有效图](/api/v1/perception/images/doc-1/available-image)\n"
                "![失效图](/api/v1/perception/images/doc-1/missing-image)"
            ),
            "image_ids": ["available-image", "missing-image"],
        },
    )
    service = object.__new__(SearchService)
    service._resolve_search_targets = lambda *_: [(_ChunkStore([hit]), None)]
    monkeypatch.setattr(search_service_module, "ImageManifestService", _SingleImageManifest)

    detail = asyncio.run(
        service.get_chunk_detail(
            document_id="doc-1",
            mysql_id=None,
            source_id="source-1",
            chunk_role="agent_doc",
            chunk_index=5,
            knowledge_type="solution",
            identity=ManualIdentity(user_id="user-1", user_role="internal"),
        )
    )

    assert detail is not None
    chunk = detail["source_chunks"][0]
    assert chunk["image_ids"] == ["available-image"]
    assert chunk["image_urls"] == ["/api/v1/perception/images/doc-1/available-image"]
    assert "available-image" in chunk["text"]
    assert "missing-image" not in chunk["text"]


def test_chunk_detail_promotes_valid_inline_image_url_without_image_ids(monkeypatch):
    class _InlineImageManifest:
        async def find_many(self, locations, *, references=None):
            assert "image-1" in references[0]
            return [[{"image_id": "image-1", "document_id": "doc-1"}]]

    hit = RetrievalHit(
        record_id="chunk-5",
        score=1.0,
        source="milvus",
        metadata={
            "document_id": "doc-1",
            "source_id": "source-1",
            "chunk_role": "agent_doc",
            "chunk_index": 5,
            "source_locator": "doc-1|chunk:5",
            "chunk_text": "![evidence](//api/v1/perception/images/doc-1/image-1)",
        },
    )
    service = object.__new__(SearchService)
    service._resolve_search_targets = lambda *_: [(_ChunkStore([hit]), None)]
    monkeypatch.setattr(search_service_module, "ImageManifestService", _InlineImageManifest)

    detail = asyncio.run(
        service.get_chunk_detail(
            document_id="doc-1",
            mysql_id=None,
            source_id="source-1",
            chunk_role="agent_doc",
            chunk_index=5,
            knowledge_type="solution",
            identity=ManualIdentity(user_id="user-1", user_role="internal"),
        )
    )

    assert detail is not None
    chunk = detail["source_chunks"][0]
    assert chunk["image_ids"] == []
    assert chunk["image_urls"] == ["/api/v1/perception/images/doc-1/image-1"]
    assert chunk["text"] == "![evidence](/api/v1/perception/images/doc-1/image-1)"


@pytest.mark.parametrize("markdown", [False, True])
@pytest.mark.parametrize("prefix", ["/", "//", "http://example.test:8793/"])
def test_mysql_detail_validates_inline_images_using_url_document(monkeypatch, markdown, prefix):
    manifest = ImageManifestService.__new__(ImageManifestService)
    loaded_documents = []

    def load_manifest(document_id):
        loaded_documents.append(document_id)
        return [
            {"image_id": "kept-image", "relativePath": "images/kept.jpg"},
            {"image_id": "unrelated-image", "relativePath": "images/unrelated.jpg"},
        ]

    manifest._load_manifest = load_manifest
    kept = f"{prefix}api/v1/perception/images/doc-attachment/kept-image"
    removed = "/api/v1/perception/images/doc-attachment/removed-image"
    text = f"封面：![图]({kept})\n![已删]({removed})" if markdown else f"封面：{kept}\n旧图：{removed}"
    hit = RetrievalHit(
        record_id="mysql-191", score=1.0, source="milvus",
        metadata={"document_id": "", "mysql_id": "191", "chunk_role": "mysql",
                  "chunk_index": 0, "source_locator": "mysql:solution|id:191", "chunk_text": text},
    )
    service = object.__new__(SearchService)
    service._resolve_search_targets = lambda *_: [(_ChunkStore([hit]), None)]
    monkeypatch.setattr(search_service_module, "ImageManifestService", lambda: manifest)
    detail = asyncio.run(service.get_chunk_detail(
        document_id=None, mysql_id="191", source_id=None, chunk_role="mysql", chunk_index=0,
        knowledge_type="solution", identity=ManualIdentity(user_id="user-1", user_role="internal"),
    ))
    assert loaded_documents == ["doc-attachment"]
    chunk = detail["draft_chunks"][0]
    assert chunk["image_urls"] == ["/api/v1/perception/images/doc-attachment/kept-image"]
    assert "removed-image" not in chunk["text"]
    assert "//api/" not in chunk["text"]


def test_mysql_detail_does_not_merge_other_chunk_roles_for_same_record(monkeypatch):
    hits = [
        RetrievalHit(
            record_id="mysql-24",
            score=1.0,
            source="milvus",
            metadata={
                "mysql_id": "24",
                "chunk_role": "mysql",
                "source_id": "document-24",
                "chunk_index": 0,
                "source_locator": "mysql:solution|id:24",
                "chunk_text": "方案名称：工程塑料技术方案",
            },
        ),
        RetrievalHit(
            record_id="draft-24",
            score=0.9,
            source="milvus",
            metadata={
                "mysql_id": "24",
                "source_id": "document-24",
                "chunk_role": "agent_draft",
                "chunk_index": 0,
                "source_locator": "document-24|draft:0",
                "chunk_text": "方案名称：工程塑料技术方案（旧副本）",
            },
        ),
    ]
    service = object.__new__(SearchService)
    service._resolve_search_targets = lambda *_: [(_ChunkStore(hits), None)]
    monkeypatch.setattr(search_service_module, "ImageManifestService", _ImageManifest)

    detail = asyncio.run(
        service.get_chunk_detail(
            document_id=None,
            mysql_id="24",
            source_id=None,
            chunk_role="mysql",
            chunk_index=0,
            knowledge_type="solution",
            identity=ManualIdentity(user_id="user-1", user_role="internal"),
        )
    )

    assert detail is not None
    assert [chunk["chunk_role"] for chunk in detail["draft_chunks"]] == ["mysql"]
    assert detail["draft"] == "方案名称：工程塑料技术方案"


def test_final_match_selection_deduplicates_same_mysql_record_across_chunk_roles():
    hits = [
        RetrievalHit(
            record_id="mysql-24",
            score=0.95,
            source="milvus",
            metadata={
                "mysql_id": "24",
                "source_id": "document-24",
                "chunk_role": "mysql",
                "knowledge_type": "solution",
                "chunk_text": "方案名称：工程塑料技术方案",
            },
        ),
        RetrievalHit(
            record_id="draft-24",
            score=0.94,
            source="milvus",
            metadata={
                "mysql_id": "24",
                "source_id": "document-24",
                "chunk_role": "agent_draft",
                "knowledge_type": "solution",
                "chunk_text": "方案名称：工程塑料技术方案",
            },
        ),
    ]

    selected = SearchService._select_final_hits(
        hits, top_k=5, group_by_business_record=False
    )

    assert [hit.record_id for hit in selected] == ["mysql-24"]


@pytest.mark.parametrize("grouped", [False, True])
def test_final_match_prefers_source_chunk_for_same_business_record(grouped):
    hits = [
        RetrievalHit(record_id="draft", score=0.99, source="milvus", metadata={
            "mysql_id": "24", "source_id": "shared-source", "knowledge_type": "solution",
            "chunk_role": "agent_draft", "chunk_index": 0, "chunk_text": "草稿正文",
        }),
        RetrievalHit(record_id="source", score=0.8, source="milvus", metadata={
            "mysql_id": None, "source_id": "shared-source", "knowledge_type": "solution",
            "document_id": "doc-1", "chunk_role": "agent_doc", "chunk_index": 2,
            "chunk_text": "命中原文",
        }),
    ]
    selected = SearchService._select_final_hits(hits, top_k=3, group_by_business_record=grouped)
    assert [item.record_id for item in selected] == ["source"]
    assert selected[0].metadata["chunk_index"] == 2


@pytest.mark.parametrize("send_source_id", [True, False])
def test_source_detail_includes_draft_by_shared_source_without_mysql_id(monkeypatch, send_source_id):
    hits = [
        RetrievalHit(record_id="source", score=0.8, source="milvus", metadata={
            "mysql_id": None, "source_id": "shared-source", "document_id": "doc-1",
            "chunk_role": "agent_doc", "chunk_index": 2, "chunk_text": "命中原文",
        }),
        RetrievalHit(record_id="draft", score=0.9, source="milvus", metadata={
            "mysql_id": "24", "source_id": "shared-source", "chunk_role": "agent_draft",
            "chunk_index": 0, "chunk_text": "对应草稿",
        }),
        RetrievalHit(record_id="other", score=0.9, source="milvus", metadata={
            "mysql_id": "24", "source_id": "other-source", "document_id": "doc-1",
            "chunk_role": "agent_draft", "chunk_index": 0, "chunk_text": "无关草稿",
        }),
    ]
    service = object.__new__(SearchService)
    class _FilteredStore:
        def query_document_chunks(self, **query):
            return [hit for hit in hits if all(
                hit.metadata.get(key) == query[key]
                for key in ("source_id", "document_id", "mysql_id") if query.get(key)
            )]

    service._resolve_search_targets = lambda *_: [(_FilteredStore(), None)]
    monkeypatch.setattr(search_service_module, "ImageManifestService", _ImageManifest)
    detail = asyncio.run(service.get_chunk_detail(
        document_id="doc-1", mysql_id=None,
        source_id="shared-source" if send_source_id else None, chunk_role="agent_doc",
        chunk_index=2, knowledge_type="solution",
        identity=ManualIdentity(user_id="u", user_role="internal"),
    ))
    assert detail["draft"] == "对应草稿"
    assert [chunk["id"] for chunk in detail["source_chunks"]] == ["source"]


def test_batch_match_carries_only_the_highest_scoring_draft_to_source_card():
    hits = [
        RetrievalHit(record_id=record_id, score=score, source="milvus", metadata={
            "knowledge_type": "solution", "source_id": "source:batch:one",
            "mysql_id": mysql_id, "chunk_role": role, "chunk_index": 0,
            "document_id": "doc-1" if role == "agent_doc" else None,
            "chunk_text": record_id,
        })
        for record_id, score, mysql_id, role in [
            ("draft-low", 0.7, "1", "agent_draft"),
            ("draft-best", 0.99, "2", "agent_draft"),
            ("original", 0.8, None, "agent_doc"),
        ]
    ]
    selected = SearchService._select_final_hits(hits, top_k=3)
    assert [hit.record_id for hit in selected] == ["original", "draft-low"]
    assert selected[0].metadata["matched_draft_record_id"] == "draft-best"
    assert selected[0].metadata["matched_draft_mysql_id"] == "2"
    assert selected[1].metadata["mysql_id"] == "1"
    from agents.retrieval_agent.application.retrieval_helpers import to_evidence
    from services.application.chat_response import build_recommendation_items
    cards = build_recommendation_items([to_evidence(hit).model_dump() for hit in selected])
    assert [card["id"] for card in cards] == ["original", "draft-low"]
    assert cards[0]["matched_draft_record_id"] == "draft-best"


@pytest.mark.parametrize("draft_id,expected", [("best", "best"), (None, "best"), ("foreign", None)])
def test_detail_uses_selected_draft_not_unranked_query_scores(monkeypatch, draft_id, expected):
    hits = [
        RetrievalHit(record_id=name, score=1, source="milvus", metadata={
            "source_id": "other" if name == "foreign" else "batch",
            "mysql_id": name if role == "agent_draft" else None,
            "document_id": "doc-1", "chunk_role": role, "chunk_index": 0,
            "chunk_text": name,
        })
        for name, role in [("original", "agent_doc"), ("low", "agent_draft"),
                           ("best", "agent_draft"), ("foreign", "agent_draft")]
    ]
    hits.append(RetrievalHit(record_id="best-more", score=1, source="milvus", metadata={
        "source_id": "batch", "mysql_id": "best", "chunk_role": "agent_draft",
        "chunk_index": 1, "chunk_text": "best-more",
    }))
    service = object.__new__(SearchService)
    service._resolve_search_targets = lambda *_: [(_ChunkStore(hits), None)]
    async def rerank(query, candidates, top_k):
        assert draft_id is None
        assert query == "original"
        assert {hit.record_id for hit in candidates} == {"best", "low"}
        assert next(hit.metadata["chunk_text"] for hit in candidates if hit.record_id == "best") == "best\n\nbest-more"
        assert top_k == 1
        return [next(hit for hit in candidates if hit.record_id == "best")], SimpleNamespace(applied=True)

    service.rerank = rerank
    monkeypatch.setattr(search_service_module, "ImageManifestService", _ImageManifest)
    detail = asyncio.run(service.get_chunk_detail(
        document_id="doc-1", mysql_id=None, source_id="batch", chunk_role="agent_doc",
        chunk_index=0, knowledge_type="solution", matched_draft_record_id=draft_id,
        identity=ManualIdentity(user_id="u", user_role="internal"),
    ))
    if expected is None:
        assert detail is None
    else:
        assert detail["draft"] == "best\r\n\r\nbest-more"
        assert detail["draft_selection_required"] is False
        assert detail["matched_draft_record_id"] == "best"


def test_same_original_under_different_sources_is_returned_once_with_best_draft():
    hits = []
    for source_id, draft_score in [("a", 0.9), ("b", 0.95)]:
        hits.extend([
            RetrievalHit(record_id=f"draft-{source_id}", score=draft_score, source="milvus", metadata={
                "source_id": source_id, "mysql_id": source_id, "knowledge_type": "solution",
                "chunk_role": "agent_draft", "chunk_index": 0, "chunk_text": source_id,
            }),
            RetrievalHit(record_id=f"original-{source_id}", score=0.8, source="milvus", metadata={
                "source_id": source_id, "document_id": "doc-1", "knowledge_type": "solution",
                "chunk_role": "agent_doc", "chunk_index": 0, "chunk_text": "同一个原文块",
            }),
        ])
    recalled = SearchService._merge_query_chunks([hits])
    selected = SearchService._select_final_hits(recalled, top_k=3)
    assert [hit.record_id for hit in selected] == ["original-b", "draft-a"]
    assert selected[0].metadata["matched_draft_record_id"] == "draft-b"
    assert selected[0].metadata["matched_draft_mysql_id"] == "b"
    assert selected[0].metadata["source_id"] == "b"
    assert selected[1].metadata["mysql_id"] == "a"


def test_single_associated_draft_detail_returns_directly_without_ranking(monkeypatch):
    hits = [
        RetrievalHit(record_id=name, score=0, source="milvus", metadata={
            "source_id": "batch", "document_id": "doc-1" if role == "agent_doc" else None,
            "mysql_id": "24" if role == "agent_draft" else None,
            "chunk_role": role, "chunk_index": 0, "chunk_text": name,
        })
        for name, role in [("original", "agent_doc"), ("only-draft", "agent_draft")]
    ]
    service = object.__new__(SearchService)
    service._resolve_search_targets = lambda *_: [(_ChunkStore(hits), None)]
    monkeypatch.setattr(search_service_module, "ImageManifestService", _ImageManifest)
    # No embedding, reranker or LLM service is configured for this direct lookup.
    detail = asyncio.run(service.get_chunk_detail(
        document_id="doc-1", mysql_id=None, source_id="batch", chunk_role="agent_doc",
        chunk_index=0, knowledge_type="solution",
        identity=ManualIdentity(user_id="u", user_role="internal"),
    ))
    assert detail["draft"] == "only-draft"
    assert detail["draft_selection_required"] is False


@pytest.mark.parametrize("failure", ["not_applied", "empty", "exception"])
def test_multiple_draft_detail_does_not_choose_arbitrarily_when_rerank_unavailable(monkeypatch, failure):
    hits = [RetrievalHit(record_id=name, score=1, source="milvus", metadata={
        "source_id": "batch", "document_id": "doc-1", "chunk_index": 0,
        "chunk_role": role, "mysql_id": name if role == "agent_draft" else None,
        "chunk_text": name,
    }) for name, role in [("original", "agent_doc"), ("a", "agent_draft"), ("b", "agent_draft")]]
    service = object.__new__(SearchService)
    service._resolve_search_targets = lambda *_: [(_ChunkStore(hits), None)]
    async def unavailable(*args, **kwargs):
        if failure == "exception":
            raise search_service_module.RerankUnavailableError("offline")
        return ([] if failure == "empty" else hits[1:2]), SimpleNamespace(applied=failure == "empty")
    service.rerank = unavailable
    monkeypatch.setattr(search_service_module, "ImageManifestService", _ImageManifest)
    detail = asyncio.run(service.get_chunk_detail(
        document_id="doc-1", mysql_id=None, source_id="batch", chunk_role="agent_doc",
        chunk_index=0, knowledge_type="solution",
        identity=ManualIdentity(user_id="u", user_role="internal"),
    ))
    assert detail["draft"] == ""
    assert detail["draft_chunks"] == []
    assert detail["text"] == "original"
    assert detail["source_chunks"][0]["text"] == "original"
    assert detail["matched_draft_record_id"] is None
    assert detail["draft_selection_error"]


def test_chunk_detail_returns_three_adjacent_source_chunks(monkeypatch):
    hits = [
        RetrievalHit(
            record_id=f"chunk-{index}",
            score=1.0,
            source="milvus",
            metadata={
                "document_id": "doc-1",
                "source_id": "source-1",
                "chunk_role": "agent_doc",
                "chunk_index": index,
                "source_locator": f"doc-1|chunk:{index}",
                "chunk_text": f"正文 {index}",
            },
        )
        for index in range(2, 9)
    ]
    store = _ChunkStore(hits)
    service = object.__new__(SearchService)
    service._resolve_search_targets = lambda *_: [(store, None)]
    monkeypatch.setattr(search_service_module, "ImageManifestService", _ImageManifest)

    detail = asyncio.run(
        service.get_chunk_detail(
            document_id="doc-1",
            mysql_id=None,
            source_id="source-1",
            chunk_role="agent_doc",
            chunk_index=5,
            knowledge_type="solution",
            identity=ManualIdentity(user_id="user-1", user_role="internal"),
        )
    )

    assert detail is not None
    assert [item["chunk_index"] for item in detail["source_chunks"]] == [4, 5, 6]


def test_chunk_detail_does_not_skip_missing_adjacent_source_chunk(monkeypatch):
    hits = [
        RetrievalHit(
            record_id=f"chunk-{index}",
            score=1.0,
            source="milvus",
            metadata={
                "document_id": "doc-1",
                "source_id": "source-1",
                "chunk_role": "agent_doc",
                "chunk_index": index,
                "source_locator": f"doc-1|chunk:{index}",
                "chunk_text": f"正文 {index}",
            },
        )
        for index in (4, 5, 7)
    ]
    store = _ChunkStore(hits)
    service = object.__new__(SearchService)
    service._resolve_search_targets = lambda *_: [(store, None)]
    monkeypatch.setattr(search_service_module, "ImageManifestService", _ImageManifest)

    detail = asyncio.run(
        service.get_chunk_detail(
            document_id="doc-1",
            mysql_id=None,
            source_id="source-1",
            chunk_role="agent_doc",
            chunk_index=5,
            knowledge_type="solution",
            identity=ManualIdentity(user_id="user-1", user_role="internal"),
        )
    )

    assert detail is not None
    assert [item["chunk_index"] for item in detail["source_chunks"]] == [4, 5]


def test_external_enterprise_chunk_detail_hides_internal_fields():
    hit = RetrievalHit(
        record_id="mysql:enterprise:7",
        score=0.9,
        source="mysql",
        metadata={
            "storage": "mysql",
            "chunk_role": "mysql",
            "chunk_index": 0,
            "mysql_id": "7",
            "field_names": ["enterprise_name", "created_by", "business_scope"],
            "fields": {
                "enterprise_name": "甲公司",
                "created_by": "内部员工甲",
                "business_scope": "仅内部经营范围",
            },
            "chunk_text": "企业名称：甲公司\n创建人：内部员工甲\n经营范围：仅内部经营范围",
        },
    )
    store = _ChunkStore([hit])
    service = object.__new__(SearchService)
    service._resolve_search_targets = lambda *_: [(store, None)]

    detail = asyncio.run(
        service.get_chunk_detail(
            document_id=None,
            mysql_id="7",
            source_id=None,
            chunk_role="mysql",
            chunk_index=0,
            knowledge_type="enterprise",
            identity=ManualIdentity(user_id="external-1", user_role="external"),
        )
    )

    assert detail is not None
    assert "甲公司" in detail["text"]
    assert "内部员工甲" not in detail["draft"]
    assert "仅内部经营范围" not in detail["draft"]
    assert "创建人" not in detail["draft"]
    assert "经营范围" not in detail["draft"]


def test_chunk_detail_summary_returns_short_grounded_summary(monkeypatch):
    expected = "设备用于自动化产线，可减少人工搬运。"
    captured = {}

    class _LLM:
        async def complete(self, messages):
            captured["call_count"] = captured.get("call_count", 0) + 1
            captured["messages"] = messages
            return expected

    settings = SimpleNamespace(
        llm_provider="test",
        llm_model="test-model",
        llm_base_url="http://localhost",
        llm_api_key="test-key",
    )
    runtime_config = importlib.import_module("runtime.config")
    llm_module = importlib.import_module("services.llm")
    monkeypatch.setattr(runtime_config, "get_settings", lambda: settings)
    monkeypatch.setattr(runtime_config, "settings_for_role", lambda *_: settings)
    monkeypatch.setattr(llm_module, "get_llm_client", lambda *_: _LLM())

    detail = asyncio.run(
        SearchService.summarize_chunk_detail(
            object.__new__(SearchService),
            {
                "draft_chunks": [{"text": "草稿：自动化搬运设备"}],
                "source_chunks": [{"text": "原文：设备用于自动化产线，降低人工搬运。"}],
            },
            "solution",
        )
    )

    assert detail["formatted_text"] == expected
    assert captured["call_count"] == 1
    assert detail["cleaned_source_text"] == "原文：设备用于自动化产线，降低人工搬运。"
    system_prompt = captured["messages"][0]["content"]
    assert "简短总结" in system_prompt
    assert "不得补充资料外事实" in system_prompt
    assert "不要输出标题" in system_prompt
    user_prompt = captured["messages"][1]["content"]
    assert "草稿：自动化搬运设备" in user_prompt
    assert "原文：设备用于自动化产线，降低人工搬运。" in user_prompt


def test_chunk_detail_summary_accepts_grounded_paraphrase(monkeypatch):
    settings = SimpleNamespace(
        llm_provider="test",
        llm_model="test-model",
        llm_base_url="http://localhost",
        llm_api_key="test-key",
    )

    class _LLM:
        async def complete(self, _messages):
            return "该设备可用于自动化产线，并减少人工搬运。"

    runtime_config = importlib.import_module("runtime.config")
    llm_module = importlib.import_module("services.llm")
    monkeypatch.setattr(runtime_config, "get_settings", lambda: settings)
    monkeypatch.setattr(runtime_config, "settings_for_role", lambda *_: settings)
    monkeypatch.setattr(llm_module, "get_llm_client", lambda *_: _LLM())

    detail = asyncio.run(
        SearchService.summarize_chunk_detail(
            object.__new__(SearchService),
            {
                "draft_chunks": [{"text": "草稿：自动化搬运设备"}],
                "source_chunks": [{"text": "原文：设备用于自动化产线，降低人工搬运。"}],
            },
            "solution",
        )
    )

    assert detail["formatted_text"] == "该设备可用于自动化产线，并减少人工搬运。"
