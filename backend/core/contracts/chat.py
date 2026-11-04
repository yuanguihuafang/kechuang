"""科创问答请求与结构化响应合同，不包含模型或执行图。"""

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from core.context_window import limit_draft_context


class ChatRequest(BaseModel):
    """统一问答入口的测试身份参数。正式网站接入 JWT 后由后端填充身份。"""

    message: str = Field(min_length=1, max_length=20000)
    user_role: str | None = Field(default=None, min_length=1, max_length=64)
    user_id: str = Field(min_length=1, max_length=64)
    user_type: str | None = Field(default=None, max_length=128)
    company: str | None = Field(default=None, max_length=256)
    contact_name: str | None = Field(default=None, max_length=128)
    contact_phone: str | None = Field(default=None, max_length=32)
    scope_id: str | None = None
    knowledge_type: str | None = None
    top_k: int = Field(default=10, ge=1, le=50)
    use_rerank: bool = True
    expand_query: bool = False
    thread_id: str | None = Field(
        default=None, max_length=128, description="继续已有会话时使用的会话 ID"
    )
    intent_hint: str | None = Field(default=None, max_length=64)
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


class ChatResponse(BaseModel):
    """统一问答响应：答案和证据同时返回，避免答案脱离来源。"""

    status: str
    answer: str
    evidences: list[dict[str, Any]] = Field(default_factory=list)
    web_results: list[dict[str, Any]] = Field(default_factory=list)
    recommendations: list[dict[str, Any]] = Field(default_factory=list)
    match_results: list[dict[str, Any]] = Field(default_factory=list)
    related_references: list[dict[str, Any]] = Field(default_factory=list)
    images: list[dict[str, Any]] = Field(default_factory=list)
    documents: list[dict[str, Any]] = Field(default_factory=list)
    ui_blocks: list[dict[str, Any]] = Field(default_factory=list)
    actions: list[dict[str, Any]] = Field(default_factory=list)
    intent: dict[str, Any] = Field(default_factory=dict)
    internal_flow: dict[str, Any] = Field(default_factory=dict)
    evaluation: dict[str, Any] = Field(default_factory=dict)
    draft: dict[str, Any] | None = None
    drafts: list[dict[str, Any]] = Field(default_factory=list)
    retrieval_status: str | None = None
    retrieval_elapsed_ms: float | None = None
    processing_elapsed_ms: float | None = None
    reasoning_source: str | None = None
    error: str | None = None
    request_id: str | None = None
    trace_id: str | None = None
    thread_id: str | None = Field(default=None, description="当前会话 ID")
    flow_state: str | None = None
    debug: dict[str, Any] | None = None
