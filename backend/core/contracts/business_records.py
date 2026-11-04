"""已确认业务记录的存储契约。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ConfirmedBusinessRecordRequest(BaseModel):
    """确认后的业务数据，供关系库和向量库写入服务共同使用。"""

    user_id: str = Field(min_length=1, max_length=128)
    record_type: Literal[
        "enterprise", "demand", "achievement", "expert", "solution", "customer_visit"
    ]
    confirmed: bool = False
    confirmed_by: str = Field(min_length=1, max_length=128)
    raw_text: str | None = Field(default=None, max_length=100000)
    structured_data: dict[str, Any] = Field(default_factory=dict)
    draft_id: str | None = Field(default=None, max_length=128)
    expected_draft_version: int | None = Field(default=None, ge=1)
    thread_id: str | None = Field(default=None, max_length=128)
    document_id: str | None = Field(default=None, max_length=128)
    version_id: str | None = Field(default=None, max_length=128)
    source_locator: str | None = Field(default=None, max_length=1000)
    confirmation_note: str | None = Field(default=None, max_length=2000)
    idempotency_key: str | None = Field(default=None, max_length=256)
