"""Perception Agent 输入输出契约。"""

from typing import Any

from pydantic import BaseModel, Field

from core.contracts import DocumentBlock

__all__ = [
    "DocumentBlock",
    "PerceptionRequest",
    "PerceptionResult",
    "TaskSubmitResponse",
    "UploadResponse",
]


class PerceptionRequest(BaseModel):
    file_path: str = Field(min_length=1, description="本地文件路径")
    document_id: str | None = None
    storage_key: str | None = None
    knowledge_type: str | None = None
    extract_draft: bool = False


class UploadResponse(BaseModel):
    """Perception 独立测试入口保存原始文件后的响应。"""

    agent: str = "perception"
    status: str
    document_id: str
    storage_key: str
    file_name: str
    file_size: int
    content_hash: str | None = None
    duplicate: bool = False
    version: int = 1
    version_id: str | None = None
    error: str | None = None


class TaskSubmitResponse(BaseModel):
    """异步解析提交响应。"""

    task_id: str
    status: str = "QUEUED"
    operation: str
    document_id: str | None = None
    storage_key: str | None = None
    status_url: str


class PerceptionResult(BaseModel):
    agent: str = "perception"
    status: str
    task_id: str | None = None
    source_name: str
    document_id: str | None = None
    version_id: str | None = None
    storage_key: str | None = None
    text: str = ""
    raw_text: str = ""
    page_texts: dict[int, str] = Field(default_factory=dict)
    tables: list[dict[str, Any]] = Field(default_factory=list)
    blocks: list[DocumentBlock] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
