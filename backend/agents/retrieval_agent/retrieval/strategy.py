"""Retrieval pipeline strategy contract."""

from typing import Protocol

from agents.retrieval_agent.schemas import RetrievalHit


class RetrievalStrategy(Protocol):
    """可替换的检索阶段契约；SearchService 提供默认实现。"""

    async def recall(
        self,
        expanded_queries: list[str],
        targets: list[tuple[object, str | None]],
        *,
        recall_k: int,
    ) -> list[list[RetrievalHit]]:
        ...

    def merge(self, query_hit_lists: list[list[RetrievalHit]]) -> list[RetrievalHit]:
        ...

    def merge_chunks(self, query_hit_lists: list[list[RetrievalHit]]) -> list[RetrievalHit]:
        ...

    async def rerank(
        self,
        query: str,
        hits: list[RetrievalHit],
        top_k: int,
    ) -> tuple[list[RetrievalHit], object]:
        ...
