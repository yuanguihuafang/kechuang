"""Query 扩展服务 - 使用 LLM 生成语义等价的查询扩展，提升召回质量。"""

import json
import logging

from runtime.config import get_settings
from services.llm import get_llm_client
from core.prompts.retrieval import QUERY_EXPANSION_SYSTEM, QUERY_EXPANSION_USER

logger = logging.getLogger(__name__)


class QueryService:
    """Query 扩展服务。"""

    def __init__(self) -> None:
        # Query 扩展是可选能力；Retrieval 默认只走 Milvus，不应因未配置 LLM 阻塞启动。
        self.llm_client = None

    @staticmethod
    def _limits() -> tuple[int, int]:
        settings = get_settings()
        return (max(1, int(getattr(settings, "query_expansion_max_queries", 5))),
                max(1, int(getattr(settings, "query_expansion_max_length", 500))))

    async def expand_query(self, query: str) -> list[str]:
        """判断是否需要扩展查询，如果需要则生成扩展查询列表。

        Args:
            query: 用户原始查询

        Returns:
            扩展查询列表，如果不需要扩展则返回空列表
        """
        if not query or len(query.strip()) < 3:
            return []

        try:
            if self.llm_client is None:
                self.llm_client = get_llm_client(get_settings())
            messages = [
                {"role": "system", "content": QUERY_EXPANSION_SYSTEM},
                {"role": "user", "content": QUERY_EXPANSION_USER.format(query=query)},
            ]

            response = await self.llm_client.complete(messages)

            # 解析 JSON 数组
            expanded = self._parse_expanded_queries(response)
            return expanded

        except Exception as exc:  # noqa: BLE001 - 查询扩展失败降级原查询
            logger.warning("Query 扩展失败，使用原始查询: %s", exc)
            return []

    def _parse_expanded_queries(self, response: str) -> list[str]:
        """解析 LLM 返回的扩展查询。

        Args:
            response: LLM 返回的原始文本

        Returns:
            扩展查询列表
        """
        try:
            # 尝试直接解析
            result = json.loads(response)
            max_queries, max_length = self._limits()
            if isinstance(result, list) and len(result) <= max_queries:
                return [q.strip()[:max_length] for q in result if q and isinstance(q, str) and len(q.strip()) <= max_length]
        except json.JSONDecodeError:
            pass

        # 尝试从 Markdown 代码块中提取
        try:
            # 去掉 markdown 代码块标记
            text = response.strip()
            if text.startswith("```"):
                text = text.split("```")[1] if "```" in text else text
                # 去掉可能的 json 或 json 后缀
                text = text.lstrip("json\n").lstrip("\n")
            result = json.loads(text)
            max_queries, max_length = self._limits()
            if isinstance(result, list) and len(result) <= max_queries:
                return [q.strip()[:max_length] for q in result if q and isinstance(q, str) and len(q.strip()) <= max_length]
        except json.JSONDecodeError:
            logger.warning("无法解析 Query 扩展结果: %s", response)

        return []
