"""Rerank 服务 - 调用远程重排模型对候选文档进行重排。"""

import math
from dataclasses import dataclass

import httpx

from runtime.config import get_settings
from services.observability.logging import get_logger

logger = get_logger("retrieval", "retrieval.rerank")


class RerankUnavailableError(RuntimeError):
    """重排服务不可用。"""


class RerankResult:
    """单条重排结果。"""

    def __init__(self, index: int, score: float, document: str) -> None:
        self.index = index
        self.score = score
        self.document = document


@dataclass(frozen=True)
class RerankOutcome:
    """重排结果及其是否由远程重排模型实际生成。"""

    results: list[RerankResult]
    applied: bool


class RerankService:
    """重排服务，调用远程 rerank API 对候选文档重排。

    典型的 rerank API 为 OpenAI Compatible 格式：
    POST {reranker_url}
    {
        "model": "bge-reranker-large",
        "query": "用户问题",
        "documents": ["文档1", "文档2", ...]
    }

    返回：
    [
        {"index": 0, "relevance_score": 0.95},
        {"index": 1, "relevance_score": 0.88},
        ...
    ]
    """

    def __init__(self) -> None:
        settings = get_settings()
        self.reranker_url = settings.reranker_url.rstrip("/")
        self.model = settings.reranker_model

    async def rerank(
        self,
        query: str,
        documents: list[str],
        top_k: int = 8,
    ) -> list[RerankResult]:
        """对文档列表进行重排，返回 top_k 条结果。

        Args:
            query: 用户查询
            documents: 待重排的文档列表
            top_k: 返回的重排结果数量

        Returns:
            按相关性分数排序的 RerankResult 列表
        """
        return (await self.rerank_with_status(query, documents, top_k)).results

    async def rerank_with_status(
        self,
        query: str,
        documents: list[str],
        top_k: int = 8,
    ) -> RerankOutcome:
        """重排并返回是否真正使用远程重排模型的状态。"""
        if not documents or top_k <= 0:
            return RerankOutcome(results=[], applied=False)

        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(
                    self.reranker_url,
                    json={
                        "model": self.model,
                        "query": query,
                        "documents": documents,
                    },
                )
                resp.raise_for_status()
                raw = resp.json()

            result_items = self._result_items(raw)

            final_results = self._normalize_results(result_items, documents, top_k)
            if not final_results:
                raise ValueError("rerank 未返回有效候选")
            logger.info(
                "远程重排完成",
                extra={
                    "structured": {
                        "event": "rerank_finished",
                        "model": self.model,
                        "candidateCount": len(documents),
                        "resultCount": len(final_results),
                    }
                },
            )
            return RerankOutcome(results=final_results, applied=True)

        except Exception as exc:
            logger.error(
                "重排服务调用失败",
                extra={
                    "structured": {
                        "event": "rerank_failed",
                        "model": self.model,
                        "candidateCount": len(documents),
                        "errorType": type(exc).__name__,
                    }
                },
            )
            raise RerankUnavailableError("重排服务不可用，请稍后重试") from exc

    @staticmethod
    def _normalize_results(
        result_items: list[object],
        documents: list[str],
        top_k: int,
    ) -> list[RerankResult]:
        """校验索引、分数并去除重复结果，防止异常响应污染排序。"""

        normalized: list[RerankResult] = []
        seen_indices: set[int] = set()
        for item in result_items:
            if not isinstance(item, dict):
                continue
            raw_index = item.get("index")
            raw_score = item.get("relevance_score")
            if isinstance(raw_index, bool) or not isinstance(raw_index, int):
                continue
            if raw_index < 0 or raw_index >= len(documents) or raw_index in seen_indices:
                continue
            try:
                score = float(raw_score)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(score):
                continue
            seen_indices.add(raw_index)
            normalized.append(RerankResult(raw_index, score, documents[raw_index]))

        normalized.sort(key=lambda item: item.score, reverse=True)
        return normalized[:top_k]

    @staticmethod
    def _result_items(raw: object) -> list[object]:
        """兼容常见重排服务的裸列表、``data``、``results`` 响应包装。"""
        if isinstance(raw, list):
            return raw
        if isinstance(raw, dict):
            for key in ("data", "results"):
                if isinstance(raw.get(key), list):
                    return raw[key]
            raise ValueError(f"rerank 返回 dict 中缺少 data/results 列表，keys={list(raw)}")
        raise ValueError(f"rerank 返回格式错误，期望 list 或 dict，实际 {type(raw).__name__}")
