"""LLM 客户端抽象。"""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

from core.contracts.message import Message


class LLMClient(ABC):
    @abstractmethod
    async def complete(self, prompt: str | list[Message | dict]) -> str:
        """完成文本生成。"""
        raise NotImplementedError

    async def stream(self, prompt: str | list[Message | dict]) -> AsyncIterator[str]:
        """流式输出；不支持流式的客户端退化为一次性输出。"""
        yield await self.complete(prompt)
