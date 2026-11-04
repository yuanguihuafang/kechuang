"""跨 Agent 和模型调用共用的轻量消息契约。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class Message(BaseModel):
    """一次会话或工具交互消息。"""

    role: Literal["user", "assistant", "system", "tool"]
    content: str
    message_id: str | None = Field(default=None, max_length=128)
    tool_name: str | None = Field(default=None, max_length=128)

    def to_llm_dict(self) -> dict[str, str]:
        """转换为模型客户端使用的消息格式。"""

        message = {"role": self.role, "content": self.content}
        if self.tool_name:
            message["name"] = self.tool_name
        return message
