"""统一 TraceContext；Agent 日志模块只负责 logger 和格式化。"""

from __future__ import annotations

import re
from contextvars import ContextVar, Token
from typing import Any
from uuid import uuid4


class TraceContext:
    def __init__(self) -> None:
        self.trace_id = ""
        # ContextVar 的值会按 asyncio 任务复制；若此处使用可变列表，
        # 从请求上下文创建的所有任务都会共享该列表。
        self._span_stack: ContextVar[tuple[str, ...]] = ContextVar(
            f"trace_span_stack_{id(self)}", default=()
        )
        self.attributes: dict[str, Any] = {}

    def generate_trace_id(self) -> str:
        self.trace_id = uuid4().hex[:16]
        return self.trace_id

    def push_span(self, span_id: str) -> None:
        self._span_stack.set((*self._span_stack.get(), span_id))

    def pop_span(self) -> str | None:
        stack = self._span_stack.get()
        if not stack:
            return None
        self._span_stack.set(stack[:-1])
        return stack[-1]

    @property
    def current_span(self) -> str | None:
        stack = self._span_stack.get()
        return stack[-1] if stack else None

    @property
    def traceparent(self) -> str | None:
        """返回 W3C traceparent；旧 trace_id 接口继续兼容。"""
        if not self.trace_id or not self.current_span:
            return None
        trace_id = self.trace_id.replace("-", "").lower().rjust(32, "0")
        span_id = (self.current_span or "0").replace("-", "").lower().rjust(16, "0")
        if not re.fullmatch(r"[0-9a-f]{32}", trace_id) or not re.fullmatch(r"[0-9a-f]{16}", span_id):
            return None
        return f"00-{trace_id}-{span_id}-01"

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value

    def clear(self) -> None:
        self.trace_id = ""
        self._span_stack.set(())
        self.attributes.clear()


_trace_context: ContextVar[TraceContext | None] = ContextVar("trace_context", default=None)


def get_trace_context() -> TraceContext:
    context = _trace_context.get()
    if context is None:
        context = TraceContext()
        _trace_context.set(context)
    return context


def record_llm_exchange(
    *, provider: str, role: str, model: str, messages: list[dict[str, Any]],
    output: str, latency_ms: int | None = None,
) -> None:
    """记录当前请求中的实际 LLM 输入和输出，供测试调试面板使用。"""
    get_trace_context().attributes.setdefault("llm_exchanges", []).append({
        "provider": provider,
        "role": role,
        "model": model,
        "messages": messages,
        "output": output,
        "latency_ms": latency_ms,
    })


def bind_trace_context(trace_id: str | None = None) -> Token:
    context = TraceContext()
    incoming = str(trace_id or "")
    match = re.fullmatch(r"00-([0-9a-fA-F]{32})-([0-9a-fA-F]{16})-([0-9a-fA-F]{2})", incoming)
    context.trace_id = (match.group(1)[:16] if match else incoming) or context.generate_trace_id()
    if match:
        context.attributes["parent_span_id"] = match.group(2)
        context.attributes["trace_flags"] = match.group(3)
    return _trace_context.set(context)


def reset_trace_context(token: Token) -> None:
    _trace_context.reset(token)


def new_trace() -> str:
    context = TraceContext()
    trace_id = context.generate_trace_id()
    _trace_context.set(context)
    return trace_id
