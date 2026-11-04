"""平台受控动作的输入、审批和执行结果契约。"""

from typing import Literal

from pydantic import BaseModel, Field


class ExecutionRequest(BaseModel):
    action: str = Field(min_length=1)
    parameters: dict[str, object] = Field(default_factory=dict)
    requested_by: str = Field(default="", max_length=128)
    idempotency_key: str | None = Field(default=None, max_length=256)
    thread_id: str | None = None


class ExecutionDecision(BaseModel):
    approval_id: str = Field(min_length=1, max_length=128)
    decision: Literal["APPROVE", "REJECT"]
    decided_by: str = Field(min_length=1, max_length=128)
    decision_note: str | None = Field(default=None, max_length=2000)


class ExecutionDecisionRequest(BaseModel):
    """HTTP 决策请求；approval_id 从路径参数注入。"""

    decided_by: str = Field(min_length=1, max_length=128)
    decision_note: str | None = Field(default=None, max_length=2000)


class ExecutionResult(BaseModel):
    status: str
    action: str
    result: dict[str, object] = Field(default_factory=dict)
    approval_id: str | None = None
    requested_by: str | None = None
    decided_by: str | None = None
    decision: str | None = None
    thread_id: str | None = None
