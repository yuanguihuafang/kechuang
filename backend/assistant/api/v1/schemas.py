"""统一网关 API 层专用请求模型。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from core.context_window import limit_draft_context


class AuthLoginRequest(BaseModel):
    """登录请求；身份类型由用户表自动确定。"""

    user_id: str = Field(min_length=1, max_length=64)

class AuthIdentityResponse(BaseModel):
    """统一身份接口响应。"""

    authenticated: bool
    mode: str
    user: dict[str, object] | None = None
    access_token: str | None = None
    expires_in: int | None = None

class ChatRequest(BaseModel):
    """前端问答请求；身份、企业资料和检索参数由后端补齐。"""

    message: str = Field(min_length=1, max_length=20000)
    user_id: str = Field(min_length=1, max_length=64)
    thread_id: str | None = Field(
        default=None, max_length=128, description="继续已有会话时传会话 ID"
    )
    intent_hint: str | None = Field(default=None, max_length=64)
    knowledge_type: str | None = Field(default=None, max_length=64)
    intent_confirmation: Literal["confirmed", "rejected"] | None = None
    flow_action: (
        Literal["interrupt", "back", "switch", "immediate_match", "next_match_batch"] | None
    ) = None
    draft_context: dict[str, Any] | None = None
    debug: bool = False

    @field_validator("draft_context")
    @classmethod
    def bound_draft_context(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        return limit_draft_context(value) if value is not None else None

class ChatStreamRequest(ChatRequest):
    """流式问答请求；身份由 Bearer Token 提供，不信任请求体 user_id。"""

    user_id: str | None = Field(default=None, max_length=64)

class ChatSessionRenameRequest(BaseModel):
    """用户修改历史会话标题。"""

    user_id: str | None = Field(default=None, max_length=64)
    title: str = Field(min_length=1, max_length=80)

class ChatTaskSubmitResponse(BaseModel):
    """智能问答异步提交结果；前端拿 task_id 继续轮询。"""

    task_id: str = Field(description="问答任务 ID")
    status: str = Field(description="任务状态：QUEUED、RUNNING、SUCCEEDED、FAILED")
    stage: str | None = Field(default=None, description="当前处理阶段")
    progress: int | None = Field(default=None, ge=0, le=100, description="处理进度百分比")
    thread_id: str = Field(description="会话 ID")
    trace_id: str | None = Field(default=None, description="链路追踪 ID")

class ChatTaskResponse(BaseModel):
    """智能问答异步任务状态和最终结果。"""

    task_id: str = Field(description="问答任务 ID")
    status: str = Field(description="任务状态：QUEUED、RUNNING、SUCCEEDED、FAILED")
    stage: str | None = Field(default=None, description="当前处理阶段")
    progress: int | None = Field(default=None, ge=0, le=100, description="处理进度百分比")
    thread_id: str | None = Field(default=None, description="会话 ID")
    result: dict[str, Any] | None = Field(default=None, description="任务成功后的问答结果")
    error_code: str | None = Field(default=None, description="失败错误码")
    error: str | None = Field(default=None, description="失败原因")

class SupervisorSearchRequest(BaseModel):
    """Supervisor 检索入口。"""

    query: str = Field(min_length=1, max_length=20000)
    scope_id: str | None = None
    knowledge_type: str | None = None
    top_k: int = Field(default=3, ge=1, le=50)
    use_rerank: bool = True
    expand_query: bool = False
    identity: dict[str, Any] = Field(default_factory=dict)
    thread_id: str | None = Field(default=None, max_length=128)

class RetrievalChunkDetailRequest(BaseModel):
    """分步检索结果的详情回查参数。"""

    user_id: str | None = Field(default=None, max_length=64)
    document_id: str | None = Field(default=None, max_length=128)
    mysql_id: str | None = Field(default=None, max_length=128)
    source_id: str | None = Field(default=None, max_length=128)
    matched_draft_record_id: str | None = Field(default=None, min_length=1, max_length=256)
    chunk_role: str = Field(min_length=1, max_length=32)
    chunk_index: int = Field(ge=0)
    include_summary: bool = True
    knowledge_type: Literal["enterprise", "demand", "achievement", "expert", "solution"] | None = (
        None
    )

    @model_validator(mode="after")
    def require_record_key(self) -> "RetrievalChunkDetailRequest":
        if not self.document_id and not self.mysql_id and not self.source_id:
            raise ValueError("document_id、mysql_id 或 source_id 至少提供一个")
        return self

class WeComContactRequest(BaseModel):
    """企业微信联系请求。"""

    user_id: str | None = Field(default=None, max_length=64)
    query: str = Field(min_length=1, max_length=20000)
    document_ids: list[str] = Field(default_factory=list, max_length=20)

class DraftCreateRequest(BaseModel):
    user_id: str | None = Field(default=None, max_length=64)
    draft: dict[str, Any]
    thread_id: str | None = Field(default=None, max_length=128)

class DraftSimilarityRequest(BaseModel):
    user_id: str | None = Field(default=None, max_length=64)
    draft: dict[str, Any]
    draft_id: str | None = Field(default=None, max_length=128)
    threshold: float = Field(default=0.72, ge=0.5, le=1.0)

class DraftUpdateRequest(BaseModel):
    user_id: str | None = Field(default=None, max_length=64)
    draft: dict[str, Any]
    thread_id: str | None = Field(default=None, max_length=128)
    status: str | None = Field(default=None, max_length=32)

class DraftAttachmentRemovalRequest(BaseModel):
    document_id: str | None = Field(default=None, max_length=128)
    storage_key: str | None = Field(default=None, max_length=2048)

class DraftTransitionRequest(BaseModel):
    user_id: str | None = Field(default=None, max_length=64)
    operator_id: str | None = Field(default=None, max_length=64)
    target_status: Literal["PENDING_REVIEW", "PUBLISHED", "WITHDRAWN"]
    note: str | None = Field(default=None, max_length=2000)

class MemoryCreateRequest(BaseModel):
    """写入长期用户记忆。"""

    user_id: str = Field(min_length=1, max_length=64)
    memory_type: str = Field(min_length=1, max_length=64)
    content: str = Field(min_length=1, max_length=10000)
    source: str = Field(default="user_declared", min_length=1, max_length=64)
    importance: float = Field(default=0.5, ge=0, le=1)
    confirmed: bool = True
    status: Literal["ACTIVE", "ARCHIVED"] = "ACTIVE"
    expires_at: datetime | None = None

class MemoryUpdateRequest(BaseModel):
    """长期记忆允许修改的字段。"""

    content: str | None = Field(default=None, min_length=1, max_length=10000)
    importance: float | None = Field(default=None, ge=0, le=1)
    confirmed: bool | None = None
    status: Literal["PENDING", "ACTIVE", "ARCHIVED"] | None = None
    expires_at: datetime | None = None

class MemoryResponse(BaseModel):
    memory_id: str
    user_id: str
    memory_type: str
    content: str
    source: str
    importance: float
    confirmed: bool
    status: str
    expires_at: str | None = None
    created_at: str
    updated_at: str

class MemoryCandidateRequest(BaseModel):
    """从消息中提取长期记忆。"""

    user_id: str = Field(min_length=1, max_length=64)
    message: str = Field(min_length=1, max_length=20000)
