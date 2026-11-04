"""Retrieval 检索智能体的稳定输入输出契约。"""

from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field

import core.contracts
from core.governance.access_policy import ManualIdentity


@dataclass
class RetrievalHit:
    """单条召回结果。"""

    record_id: str
    score: float
    source: str
    metadata: dict[str, Any] = field(default_factory=dict)

Evidence = core.contracts.Evidence
IndexChunk = core.contracts.IndexChunk
RelatedImage = core.contracts.RelatedImage
RelatedDocument = core.contracts.RelatedDocument
RetrievedRecord = core.contracts.RetrievedRecord
RetrievalResult = core.contracts.RetrievalResult

KnowledgeType = Literal["enterprise", "demand", "achievement", "expert", "solution"]


class RetrievalRequest(BaseModel):
    query: str = Field(min_length=1)
    scope_id: str | None = None
    knowledge_type: KnowledgeType | None = None
    knowledge_types: list[KnowledgeType] = Field(default_factory=list, max_length=5)
    document_id: str | None = None
    top_k: int = Field(default=3, ge=1, le=50)
    exclude_result_ids: list[str] = Field(default_factory=list)
    use_rerank: bool = True
    expand_query: bool = False
    identity: ManualIdentity


class IndexRequest(BaseModel):
    document_id: str = Field(min_length=1)
    version_id: str | None = Field(default=None, max_length=128)
    scope_id: str = "public"
    knowledge_type: KnowledgeType
    chunks: list[IndexChunk] = Field(min_length=1)
    replace_existing: bool = True
    # Supervisor 确认阶段传入的块已经由 chunk-preview 生成，不再二次切分。
    chunks_are_final: bool = False
    chunk_size: int = Field(default=1000, ge=100, le=2000)
    chunk_overlap: int = Field(default=80, ge=0, le=500)
    quality_blockers: list[str] = Field(default_factory=list, max_length=20)
    allow_quality_override: bool = False
    identity: ManualIdentity


class IndexResult(BaseModel):
    agent: str = "knowledge_retrieval"
    status: str
    task_id: str | None = None
    document_id: str
    version_id: str | None = None
    indexed_count: int = 0
    # 技术方案库返回 Milvus 实际接受的文本块，前端以此作为最终显示源。
    indexed_chunks: list[IndexChunk] = Field(default_factory=list)
    error: str | None = None


class TaskSubmitResponse(BaseModel):
    """异步操作提交响应；真正结果通过任务状态接口查询。"""

    task_id: str
    status: Literal["QUEUED"] = "QUEUED"
    operation: str
    document_id: str | None = None
    status_url: str
