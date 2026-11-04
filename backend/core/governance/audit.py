"""Supervisor 的结构化审计日志与工具调用记录。"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol


class AuditSink(Protocol):
    def record(self, event: str, **fields: Any) -> None: ...


class JsonlAuditLogger:
    """以一行一个 JSON 事件写入独立审计文件。

    只记录参数键和结果摘要，不记录完整问题、完整参数或工具返回正文，避免
    把业务知识和敏感数据复制到审计日志中。
    """

    _logger = logging.getLogger("kechuang.supervisor.audit")
    _CANONICAL_EVENTS = {
        "supervisor_intent_selected": "agent.route.completed",
        "supervisor_plan_created": "agent.plan.created",
        "supervisor_tool_call_start": "agent.tool.started",
        "supervisor_tool_call_end": "agent.tool.completed",
        "supervisor_tool_call_rejected": "agent.tool.failed",
        "supervisor_replan_created": "agent.replan.started",
        "supervisor_request_end": "agent.run.completed",
    }

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        configured = path or os.getenv("SUPERVISOR_AUDIT_LOG_PATH")
        # 统一写入项目根目录 logs；即使未加载 .env，也和网关、Agent 日志保持一致。
        default_path = Path(__file__).resolve().parents[3] / "logs" / "supervisor-audit.jsonl"
        self.path = Path(configured or default_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._configure_logger()

    def _configure_logger(self) -> None:
        resolved = str(self.path.resolve())
        for handler in self._logger.handlers:
            if getattr(handler, "_kechuang_audit_path", None) == resolved:
                return
        handler = logging.FileHandler(self.path, encoding="utf-8")
        handler._kechuang_audit_path = resolved  # type: ignore[attr-defined]
        handler.setFormatter(logging.Formatter("%(message)s"))
        self._logger.addHandler(handler)
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False

    def record(self, event: str, **fields: Any) -> None:
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": event,
            "canonical_event": self._CANONICAL_EVENTS.get(event, event),
            **_json_safe(fields),
        }
        try:
            self._logger.info(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        except Exception:
            logging.getLogger(__name__).exception("Supervisor 审计日志写入失败")

    def close(self) -> None:
        """关闭当前实例创建的文件 Handler，避免临时目录无法清理。"""

        resolved = str(self.path.resolve())
        for handler in list(self._logger.handlers):
            if getattr(handler, "_kechuang_audit_path", None) != resolved:
                continue
            self._logger.removeHandler(handler)
            handler.close()


def identity_summary(identity: dict[str, Any]) -> dict[str, Any]:
    """只保留当前测试身份契约中用于追踪权限的字段。"""

    allowed = {"user_id", "user_role", "scope_id"}
    return {key: value for key, value in identity.items() if key in allowed and _is_scalar(value)}


def result_summary(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "result_status": result.get("status"),
        "result_keys": sorted(str(key) for key in result),
    }


def _is_scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def _json_safe(value: Any) -> Any:
    if _is_scalar(value):
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return str(value)
