"""共享文档工作流与草稿发布契约。"""

from __future__ import annotations
from datetime import datetime
from typing import Any, Literal
from pydantic import BaseModel, Field, model_validator


class FileConfirmRequest(BaseModel):
    """人工确认后的最终检索块。"""

    chunks: list[dict[str, Any]] = Field(default_factory=list)
    images: list[dict[str, Any]] = Field(default_factory=list, max_length=1000)
    images_confirmed: bool = False
    identity: dict[str, Any] = Field(default_factory=dict)
    draft: dict[str, Any] | None = None
    drafts: list[dict[str, Any]] = Field(default_factory=list, max_length=100)
    intent_confirmation: Literal["confirmed", "rejected"] | None = None
    idempotency_key: str | None = Field(default=None, max_length=256)


class SupervisorTaskResponse(BaseModel):
    """Supervisor 文件流程状态。"""

    task_id: str
    thread_id: str | None = None
    operation: str = "file_ingestion"
    task_type: str | None = None
    lifecycle_action: str | None = None
    status: str
    operation_state: str | None = None
    stage: str | None = None
    progress: int | None = None
    document_id: str | None = None
    version_id: str | None = None
    storage_key: str | None = None
    file_name: str | None = None
    parsed_result: dict[str, Any] | None = None
    upload_result: dict[str, Any] | None = None
    indexed_count: int | None = None
    index_result: dict[str, Any] | None = None
    result_status: str | None = None
    total: int | None = None
    processed: int | None = None
    skipped_count: int | None = None
    last_id: int | None = None
    batch_size: int | None = None
    error_code: str | None = None
    error: str | None = None
    attempt: int | None = None
    max_attempts: int | None = None
    timeout_seconds: float | None = None
    idempotency_key: str | None = None
    selected_tool: str | None = None
    evaluation: dict[str, Any] | None = None
    compensation: dict[str, Any] | None = None
    recovery_attempts: int | None = None
    recovery_result: dict[str, Any] | None = None
    recovery_error: str | None = None
    request_payload: dict[str, Any] | None = None
    user_id: str | None = None
    user_role: str | None = None
    company: str | None = None
    knowledge_type: str | None = None
    attachments: list[dict[str, Any]] = Field(default_factory=list)
    draft_id: str | None = None
    draft_preview: dict[str, Any] | None = None
    draft_ids: list[str] = Field(default_factory=list)
    draft_previews: list[dict[str, Any]] = Field(default_factory=list)
    draft_status: str | None = None
    draft_error_code: str | None = None
    draft_error: str | None = None
    flow_state: str | None = None
    intent_candidates: list[str] = Field(default_factory=list)
    intent_knowledge_type: str | None = None
    current_stage: str | None = None
    error_message: str | None = None
    retry_count: int | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @model_validator(mode="before")
    @classmethod
    def normalize_admin_fields(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        normalized.setdefault("current_stage", normalized.get("stage"))
        normalized.setdefault("error_message", normalized.get("error"))
        normalized.setdefault("retry_count", normalized.get("attempt"))
        return normalized


class DraftPublishRequest(BaseModel):
    """确认发布草稿并建立向量索引。"""

    user_id: str | None = Field(default=None, max_length=64)
    thread_id: str | None = Field(default=None, max_length=128)
    scope_id: str = Field(default="public", min_length=1, max_length=64)
    replace_existing: bool = True
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=256)


class UnifiedDeleteRequest(BaseModel):
    """统一删除所需的最小资源定位和权限上下文。"""

    storage_key: str = Field(min_length=1, max_length=512)
    scope_id: str = Field(default="public", min_length=1, max_length=64)
    knowledge_type: str = Field(min_length=1, max_length=32, description="必须明确指定知识库类型")
    user_id: str | None = Field(default=None, max_length=64)


class LifecycleDecisionRequest(BaseModel):
    user_id: str | None = Field(default=None, max_length=64)
    # 管理员代操作时，user_id 仍用于定位草稿，operator_id 用于权限校验。
    operator_id: str | None = Field(default=None, max_length=64)
    note: str | None = Field(default=None, max_length=2000)
    reason_code: str | None = Field(default=None, max_length=64)
    reason_text: str | None = Field(default=None, max_length=500)


UNPUBLISH_REASON_CODES = {
    "NEEDS_REVISION": "内容需要修改",
    "OUTDATED": "内容已过期",
    "DUPLICATE": "内容重复",
    "PROJECT_ENDED": "项目已结束",
    "NO_LONGER_PUBLIC": "不希望继续公开",
    "UPLOAD_ERROR": "上传错误",
    "OTHER": "其他",
}
