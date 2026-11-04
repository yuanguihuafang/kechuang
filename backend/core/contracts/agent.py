"""所有进程内 Agent 共用的调用上下文和结果契约。"""

from __future__ import annotations

from time import perf_counter
from typing import Any, Generic, Literal, Protocol, TypeVar
from uuid import uuid4

from pydantic import BaseModel, Field

T = TypeVar("T")
AgentStatus = Literal["SUCCESS", "PARTIAL_SUCCESS", "FAILED", "NO_EVIDENCE"]


class AgentContext(BaseModel):
    """一次 Agent 调用的链路上下文。

    Supervisor 传给子 Agent 时只替换 ``agent``，request/trace/session/task
    标识保持不变，因此一次用户请求可以在结果和日志中完整串联。
    """

    request_id: str = Field(min_length=1, max_length=128)
    trace_id: str = Field(default_factory=lambda: f"trace-{uuid4().hex}", min_length=1, max_length=128)
    agent: str = Field(default="supervisor", min_length=1, max_length=64)
    session_id: str | None = Field(default=None, max_length=128)
    task_id: str | None = Field(default=None, max_length=128)
    thread_id: str | None = Field(default=None, max_length=128)
    identity: dict[str, Any] = Field(default_factory=dict)
    run_id: str | None = Field(default=None, max_length=128)


class ResultMeta(BaseModel):
    """平台可观测信息；业务结果不要重复嵌入这些字段。"""

    request_id: str = Field(min_length=1, max_length=128)
    trace_id: str = Field(min_length=1, max_length=128)
    session_id: str | None = Field(default=None, max_length=128)
    task_id: str | None = Field(default=None, max_length=128)
    agent: str = Field(min_length=1, max_length=64)
    operation: str = Field(min_length=1, max_length=128)
    duration_ms: float | None = Field(default=None, ge=0)
    warnings: list[str] = Field(default_factory=list)


class AgentResult(BaseModel, Generic[T]):
    """统一 Agent 结果外壳，业务负载由调用方提供强类型 ``data``。

    ``data`` 只放业务结果；耗时、Token、告警和链路标识统一放在 ``meta``，
    避免各 Agent 自己定义一套不可互换的返回格式。
    """

    status: AgentStatus
    data: T | None = None
    error_code: str | None = Field(default=None, max_length=128)
    error_message: str | None = Field(default=None, max_length=2000)
    meta: ResultMeta


class Agent(Protocol):
    """进程内业务 Agent 的最小公共接口。"""

    name: str

    async def invoke(self, context: AgentContext, request: Any) -> AgentResult[Any]:
        """执行一次带上下文的 Agent 调用。"""


def child_context(context: AgentContext, *, agent: str) -> AgentContext:
    """为 Supervisor 调用子 Agent 生成同一请求链上的上下文。"""

    return context.model_copy(update={"agent": agent})


def result_meta(context: AgentContext, *, operation: str, started_at: float) -> ResultMeta:
    """集中构造结果元数据，避免每个 Agent 重复拼装。"""

    return ResultMeta(
        request_id=context.request_id,
        trace_id=context.trace_id,
        session_id=context.session_id,
        task_id=context.task_id,
        agent=context.agent,
        operation=operation,
        duration_ms=round((perf_counter() - started_at) * 1000, 3),
    )


def success(
    context: AgentContext,
    data: T,
    *,
    operation: str,
    started_at: float,
    warnings: list[str] | None = None,
) -> AgentResult[T]:
    meta = result_meta(context, operation=operation, started_at=started_at)
    if warnings:
        meta = meta.model_copy(update={"warnings": warnings})
    return AgentResult(status="SUCCESS", data=data, meta=meta)


def failure(
    context: AgentContext,
    *,
    operation: str,
    started_at: float,
    error_code: str,
    error_message: str,
) -> AgentResult[None]:
    return AgentResult(
        status="FAILED",
        error_code=error_code,
        error_message=error_message,
        meta=result_meta(context, operation=operation, started_at=started_at),
    )
