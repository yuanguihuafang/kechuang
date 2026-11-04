"""文档、业务知识和会话摘要的跨层契约。"""

from __future__ import annotations

from typing import Any, Literal, cast

from pydantic import BaseModel, Field

from core.governance.access_policy import ManualIdentity

KnowledgeType = Literal["enterprise", "demand", "achievement", "expert", "solution"]
VALID_KNOWLEDGE_TYPES = frozenset(("enterprise", "demand", "achievement", "expert", "solution"))


def resolve_knowledge_type(*values: Any) -> KnowledgeType:
    """兼容历史版本的空类型，返回可用于生命周期操作的有效类型。"""

    for raw in values:
        value = str(raw or "").strip().lower()
        if value in VALID_KNOWLEDGE_TYPES:
            return cast(KnowledgeType, value)
    return "solution"


class CleanedKnowledgeRecord(BaseModel):
    """平台清洗并确认后的单条业务知识记录。"""

    record_id: str = Field(min_length=1, max_length=128)
    mysql_id: str | None = Field(default=None, max_length=128)
    source_id: str | None = Field(default=None, max_length=128)
    document_id: str | None = Field(default=None, max_length=128)
    # 业务记录由 Retrieval 在写入 Milvus 前自动分块；这里不限制原始 MySQL 文本长度。
    text: str | None = Field(default=None, min_length=1)
    fields: dict[str, Any] = Field(default_factory=dict)
    source_locator: str | None = Field(default=None, max_length=512)
    metadata: dict[str, Any] = Field(default_factory=dict)


class DocumentKnowledgeRequest(BaseModel):
    """文档发布或重建索引所需的完整清洗数据。"""

    source_name: str = Field(min_length=1, max_length=256)
    version_id: str | None = Field(default=None, max_length=128)
    scope_id: str = Field(default="public", min_length=1, max_length=64)
    knowledge_type: KnowledgeType
    records: list[CleanedKnowledgeRecord] = Field(min_length=1, max_length=10000)
    replace_existing: bool = True
    chunks_are_final: bool = False
    chunk_size: int = Field(default=800, ge=100, le=2000)
    chunk_overlap: int = Field(default=80, ge=0, le=500)
    quality_blockers: list[str] = Field(default_factory=list, max_length=20)
    allow_quality_override: bool = False
    identity: ManualIdentity


class DocumentUnpublishRequest(BaseModel):
    """文档下架请求；默认只改变可检索状态，不删除向量。"""

    scope_id: str = Field(default="public", min_length=1, max_length=64)
    version_id: str | None = Field(default=None, max_length=128)
    knowledge_type: KnowledgeType
    mysql_id: str | None = Field(default=None, max_length=128)
    remove_vectors: bool = Field(default=False, description="仅删除或彻底清理时使用")
    identity: ManualIdentity


class BusinessImportRequest(DocumentKnowledgeRequest):
    """正式导入主业务高可信库的请求。"""

    document_id: str = Field(min_length=1, max_length=64)
    publication_status: Literal["PUBLISHED"] = Field(
        description="仅允许平台确认发布的数据进入主业务高可信库"
    )
    replace_existing: bool = Field(
        default=True, description="仅替换本批 mysql_id 的旧分块，不清理整库"
    )


class IndexTextRequest(BaseModel):
    """纯文本索引请求（不需要文件上传）。"""

    document_id: str = Field(min_length=1, description="文档唯一标识")
    scope_id: str = Field(default="public", description="知识范围")
    knowledge_type: KnowledgeType = Field(description="公开知识库类型，必须明确指定")
    text: str = Field(min_length=1, description="要索引的文本内容")
    chunk_size: int = Field(default=800, ge=100, le=2000, description="通用分块目标大小")
    chunk_overlap: int = Field(default=80, ge=0, le=500, description="通用块间重叠")
    identity: ManualIdentity


class SessionSummaryRequest(BaseModel):
    """将一次会话产生的新摘要写入用户私有知识库。"""

    session_id: str = Field(min_length=1, max_length=128)
    summary: str = Field(min_length=1, max_length=20000)
    mysql_id: str | None = Field(default=None, max_length=128)
    source_id: str | None = Field(default=None, max_length=128)
    source_document_id: str | None = Field(default=None, max_length=128)
    source_name: str | None = Field(default=None, max_length=256)
    source_attachments: list[dict[str, str]] = Field(default_factory=list, max_length=20)
    identity: ManualIdentity
