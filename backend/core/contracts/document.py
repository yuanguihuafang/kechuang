"""文档解析结果的跨 Agent 契约。"""

from typing import Any, Literal

from pydantic import BaseModel, Field


class DocumentBlock(BaseModel):
    block_id: str
    block_type: str = "text"
    text: str = Field(min_length=1)
    page_number: int | None = None
    source_name: str
    proposed_visibility: Literal["public", "internal_supplement"] = "public"
    source_chunk_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ParsedDocument(BaseModel):
    document_id: str
    source_name: str
    status: str
    blocks: list[DocumentBlock] = Field(default_factory=list)
    images: list[dict[str, Any]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
