"""统一后端基础设施日志入口。

基础设施模块不能依赖某个业务 Agent 的 Python 包，否则统一网关在加载
Agent 之前就会因导入顺序失败。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from functools import wraps
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any, ClassVar

from services.observability.trace_context import (
    TraceContext,
    bind_trace_context,
    get_trace_context,
    new_trace,
    reset_trace_context,
)

LOGGER_NAME = "kechuang"
BEIJING_TZ = timezone(timedelta(hours=8))
LOG_LEVEL = logging.INFO


class JsonFormatter(logging.Formatter):
    """统一 JSON 行格式，自动附带当前 Trace/Span。"""

    def format(self, record: logging.LogRecord) -> str:
        context = get_trace_context()
        payload: dict[str, Any] = {
            "timestamp": datetime.now(BEIJING_TZ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if context.trace_id:
            payload["trace_id"] = context.trace_id
        if context.current_span:
            payload["span_id"] = context.current_span
        structured = getattr(record, "structured", None)
        if isinstance(structured, dict):
            payload.update(structured)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


class RetrievalProgressFilter(logging.Filter):
    """同一请求的重复检索进度只在控制台输出一次；文件日志不受影响。"""

    def filter(self, record: logging.LogRecord) -> bool:
        structured = getattr(record, "structured", None)
        event = structured.get("event", "") if isinstance(structured, dict) else ""
        context = get_trace_context()
        if (
            record.levelno != logging.INFO
            or not context.trace_id
            or not event.startswith("retrieval_")
            or not event.endswith(("_started", "_finished"))
        ):
            return True
        seen = context.attributes.setdefault("_console_retrieval_progress", set())
        if event in seen:
            return False
        seen.add(event)
        return True


class ColoredConsoleFormatter(logging.Formatter):
    """开发环境控制台格式。"""

    COLORS: ClassVar[dict[str, str]] = {
        "DEBUG": "\033[36m",
        "WARNING": "\033[33m",
        "ERROR": "\033[31m",
        "CRITICAL": "\033[31m",
    }
    RESET = "\033[0m"

    def __init__(self, *, use_color: bool | None = None) -> None:
        super().__init__()
        self.use_color = (
            sys.stderr.isatty() and "NO_COLOR" not in os.environ if use_color is None else use_color
        )

    def format(self, record: logging.LogRecord) -> str:
        context = get_trace_context()
        trace = ""
        if context.trace_id:
            trace = f"[{context.trace_id[:8]}][{context.current_span or 'root'}] "
        color = self.COLORS.get(record.levelname, "")
        timestamp = datetime.fromtimestamp(record.created, BEIJING_TZ).strftime("%m/%d/%y %H:%M:%S")
        structured = getattr(record, "structured", None)
        detail = ""
        if isinstance(structured, dict):
            event = str(structured.get("event") or "")
            if record.levelno == logging.INFO and (
                event in {"span_start", "span_end"} or event.endswith(("_started", "_finished"))
            ):
                color = "\033[32m"
            duration = structured.get(
                "duration_ms", structured.get("durationMs", structured.get("latencyMs"))
            )
            if duration is not None:
                detail = f" duration_ms={duration}"
        if isinstance(structured, dict) and structured.get("event", "").startswith("span_"):
            fields = (
                ("span", structured.get("span_name")),
                ("dependency", structured.get("dependency")),
                ("duration_ms", structured.get("duration_ms")),
                ("error", structured.get("error")),
            )
            detail = " " + " ".join(
                f"{key}={value}" for key, value in fields if value not in (None, "")
            )
        source = f"{record.filename}:{record.lineno}"
        message = (
            f"[{timestamp}] {record.levelname:<8} {trace}{record.getMessage()}{detail} {source}"
        )
        if record.exc_info:
            message = f"{message}\n{self.formatException(record.exc_info)}"
        return f"{color}{message}{self.RESET}" if self.use_color and color else message


def _configure_root_console(level: int) -> None:
    """让第三方 SDK 和标准库 logger 使用与科创日志相同的控制台格式。"""

    root = logging.getLogger()
    if not any(getattr(handler, "_kechuang_console", False) for handler in root.handlers):
        handler = logging.StreamHandler(sys.stderr)
        handler.setLevel(level)
        handler.setFormatter(ColoredConsoleFormatter())
        handler.addFilter(RetrievalProgressFilter())
        handler._kechuang_console = True  # type: ignore[attr-defined]
        root.addHandler(handler)
    if root.level == logging.NOTSET or root.level > level:
        root.setLevel(level)


def setup_logger(
    service: str,
    log_dir: str | Path | None = None,
    level: int = LOG_LEVEL,
    console: bool = True,
    json_file: bool = True,
    retention_days: int | None = None,
    log_path: str | Path | None = None,
) -> logging.Logger:
    """配置统一服务日志；日志目录由调用方显式传入。"""

    if retention_days is None:
        try:
            retention_days = max(1, int(os.getenv("AUDIT_LOG_RETENTION_DAYS", "15")))
        except ValueError:
            retention_days = 15
    logger = logging.getLogger(f"{LOGGER_NAME}.{service}")
    logger.setLevel(level)
    logger.handlers.clear()
    logger.propagate = console
    if console:
        _configure_root_console(level)
    if (log_dir or log_path) and json_file:
        path = Path(log_path) if log_path else Path(log_dir) / f"{service}.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            handler = TimedRotatingFileHandler(
                path,
                when="midnight",
                backupCount=retention_days,
                encoding="utf-8",
            )
        except OSError as exc:
            # 日志目录权限异常不能阻断网关启动；保留控制台日志，部署时仍应修正目录权限。
            logger.warning("文件日志不可写，降级为控制台日志：%s", exc)
        else:
            handler.setLevel(level)
            handler.setFormatter(JsonFormatter())
            logger.addHandler(handler)
    return logger


def get_logger(service: str, component: str | None = None) -> logging.Logger:
    """返回统一命名的标准库 logger。"""

    name = f"kechuang.{service}"
    if component:
        name = f"{name}.{component}"
    return logging.getLogger(name)


def log_event(
    logger: logging.Logger,
    event: str,
    level: int = logging.INFO,
    **attributes: Any,
) -> None:
    """以结构化字段记录基础设施事件。"""

    logger.log(
        level,
        event,
        extra={"structured": {"event": event, **attributes}},
    )


@dataclass
class Span:
    name: str
    span_id: str
    start_time: datetime
    attributes: dict[str, Any] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    parent_span_id: str | None = None


@contextmanager
def trace_span(
    name: str,
    attributes: dict[str, Any] | None = None,
    log: logging.Logger | None = None,
    *,
    level: int = logging.INFO,
    error_level: int = logging.ERROR,
) -> Iterator[Span]:
    """记录一个同步或异步调用都可使用的 Span。"""

    context = get_trace_context()
    span = Span(
        name,
        uuid.uuid4().hex[:16],
        datetime.now(BEIJING_TZ),
        attributes or {},
        parent_span_id=context.current_span or context.attributes.get("parent_span_id"),
    )
    context.push_span(span.span_id)
    if log:
        log_event(
            log,
            "span_start",
            level=level,
            traceparent=context.traceparent,
            span_name=name,
            span_id=span.span_id,
            parent_span_id=span.parent_span_id,
            **span.attributes,
        )
    try:
        yield span
    except Exception as exc:
        if log:
            log_event(
                log,
                "span_error",
                error_level,
                traceparent=context.traceparent,
                span_name=name,
                span_id=span.span_id,
                parent_span_id=span.parent_span_id,
                error=str(exc),
            )
        raise
    finally:
        duration_ms = int((datetime.now(BEIJING_TZ) - span.start_time).total_seconds() * 1000)
        if log:
            log_event(
                log,
                "span_end",
                level=level,
                traceparent=context.traceparent,
                span_name=name,
                span_id=span.span_id,
                parent_span_id=span.parent_span_id,
                duration_ms=duration_ms,
                **span.attributes,
            )
        context.pop_span()


def traced(name: str | None = None, log_args: bool = False, log_result: bool = False) -> Callable:
    """为函数增加统一 Span；默认不记录参数和结果，避免泄露业务内容。"""

    def decorator(func: Callable) -> Callable:
        def attrs(args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any]:
            if not log_args:
                return {}
            return {"args": str(args)[:200], "kwargs": {k: str(v)[:100] for k, v in kwargs.items()}}

        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            logger = get_logger("trace", func.__module__)
            with trace_span(name or func.__name__, attrs(args, kwargs), logger) as span:
                result = await func(*args, **kwargs)
                if log_result:
                    span.attributes["result"] = str(result)[:200]
                return result

        @wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            logger = get_logger("trace", func.__module__)
            with trace_span(name or func.__name__, attrs(args, kwargs), logger) as span:
                result = func(*args, **kwargs)
                if log_result:
                    span.attributes["result"] = str(result)[:200]
                return result

        return async_wrapper if asyncio.iscoroutinefunction(func) else sync_wrapper

    return decorator


__all__ = [
    "BEIJING_TZ",
    "LOGGER_NAME",
    "ColoredConsoleFormatter",
    "JsonFormatter",
    "Span",
    "TraceContext",
    "bind_trace_context",
    "get_logger",
    "get_trace_context",
    "log_event",
    "new_trace",
    "reset_trace_context",
    "setup_logger",
    "trace_span",
    "traced",
]
