"""Embedding 服务 - 调用 Ollama bge-m3 生成向量。

支持 dense embedding（语义向量）和 sparse embedding（BM25 权重向量）。
sparse 向量计算内置实现（CJK 优化的 char-level tokenize）。
"""

import asyncio
import logging

import httpx

from runtime.config import get_settings

logger = logging.getLogger(__name__)


class EmbeddingUnavailableError(RuntimeError):
    """Embedding 后端不可用，调用方应停止索引或检索。"""


class EmbeddingService:
    """Ollama bge-m3 向量化服务。"""

    def __init__(self) -> None:
        settings = get_settings()
        self.ollama_url = settings.ollama_url.rstrip("/")
        self.model = settings.embedding_model
        self.batch_size = max(1, int(settings.embedding_batch_size))
        self.batch_concurrency = max(1, min(int(settings.embedding_batch_concurrency), 8))
        self.max_retries = 3
        self.retry_base_delay = 1.0

    # ---- 稠密向量嵌入 ----

    async def check_ready(self) -> None:
        """确认 Ollama 可访问且已加载配置的 Embedding 模型。"""

        async with httpx.AsyncClient(timeout=4.0, trust_env=False) as client:
            response = await client.get(f"{self.ollama_url}/api/tags")
            response.raise_for_status()
        models = response.json().get("models", [])
        available = {
            str(item.get("name") or item.get("model") or "")
            for item in models
            if isinstance(item, dict)
        }
        configured = self.model.split(":", maxsplit=1)[0]
        if not any(name.split(":", maxsplit=1)[0] == configured for name in available):
            raise RuntimeError(f"Embedding 模型未加载：{self.model}")

    async def encode_dense(self, texts: list[str]) -> list[list[float]]:
        """将文本列表转为 dense 向量列表，使用 Ollama 原生批量接口。"""
        if not texts:
            return []

        try:
            async with httpx.AsyncClient(timeout=60.0, trust_env=False) as client:
                batches = [texts[i : i + self.batch_size] for i in range(0, len(texts), self.batch_size)]
                semaphore = asyncio.Semaphore(self.batch_concurrency)

                async def run_batch(index: int, batch: list[str]) -> list[list[float]]:
                    async with semaphore:
                        return await self._embed_batch(client, batch, batch_index=index)

                results = await asyncio.gather(
                    *(run_batch(index, batch) for index, batch in enumerate(batches))
                )
                return [embedding for batch in results for embedding in batch]
        except Exception as exc:
            # 零向量会让检索退化成随机结果，并污染后续写入的索引。
            logger.error("Embedding 服务不可用，已中止向量化: %s", exc)
            raise EmbeddingUnavailableError("Embedding 服务不可用，请检查 OLLAMA_URL 和模型服务") from exc

    async def _embed_batch(
        self, client: httpx.AsyncClient, texts: list[str], *, batch_index: int
    ) -> list[list[float]]:
        """一次批量请求 Ollama，针对网关瞬时错误进行有限重试。"""
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = await client.post(
                    f"{self.ollama_url}/api/embed",
                    json={"model": self.model, "input": texts},
                )
                if resp.status_code in {429, 502, 503, 504}:
                    resp.raise_for_status()
                resp.raise_for_status()
                data = resp.json()
                embeddings = data.get("embeddings")
                if (
                    not isinstance(embeddings, list)
                    or len(embeddings) != len(texts)
                    or any(not isinstance(embedding, list) or not embedding for embedding in embeddings)
                ):
                    raise ValueError("Ollama 未返回完整的批量 embedding")
                return embeddings
            except (httpx.HTTPStatusError, httpx.RequestError, TimeoutError, ValueError) as exc:
                last_error = exc
                status_code = getattr(getattr(exc, "response", None), "status_code", None)
                retryable = isinstance(exc, (httpx.RequestError, TimeoutError)) or status_code in {
                    429, 502, 503, 504
                }
                if not retryable or attempt >= self.max_retries:
                    raise RuntimeError(f"Embedding 分块 {batch_index} 请求失败: {exc}") from exc
                delay = self.retry_base_delay * (2**attempt)
                logger.warning(
                    "Embedding 批次 %s 请求失败（HTTP %s），第 %s/%s 次重试，%.1fs 后继续: %s",
                    batch_index,
                    status_code or "-",
                    attempt + 1,
                    self.max_retries,
                    delay,
                    exc,
                )
                await asyncio.sleep(delay)
        raise RuntimeError(f"Embedding 批次 {batch_index} 请求失败: {last_error}") from last_error
