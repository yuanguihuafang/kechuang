"""LLM 客户端路由。"""

from runtime.config import Settings
from services.llm.client import LLMClient
from services.llm.openai_client import OpenAIClient


def get_llm_client(settings: Settings) -> LLMClient:
    """创建配置的供应商客户端；失败不切换模型。"""
    provider = (settings.llm_provider or "").strip().lower()
    if provider in {"sidusai", "sidus-ai", "openai-compatible"}:
        return OpenAIClient(settings=settings)
    raise ValueError(f"不支持的 LLM provider：{settings.llm_provider}")
