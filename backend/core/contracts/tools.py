from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ToolContext(BaseModel):
    """Backend-injected context; model arguments are never authoritative."""

    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(min_length=1, max_length=128)
    user_role: str = Field(min_length=1, max_length=64)
    company: str | None = Field(default=None, max_length=255)
    thread_id: str | None = Field(default=None, max_length=128)
    trace_id: str = Field(min_length=1, max_length=128)

    def identity(self) -> dict[str, str]:
        return {"user_id": self.user_id, "user_role": self.user_role}


class ToolResult(BaseModel):
    """Stable DeerFlow-facing envelope shared by every business Tool."""

    model_config = ConfigDict(extra="forbid")

    status: str = Field(min_length=1, max_length=64)
    data: Any | None = None
    error: Any | None = None
    trace_id: str = Field(min_length=1, max_length=128)
