"""观测 PostgreSQL 仓储：保存指标统计和治理审计事件。"""

from __future__ import annotations

import math
import os
from datetime import datetime, time, timedelta, timezone
from typing import Any, ClassVar
from zoneinfo import ZoneInfo

from sqlalchemy import and_, case, func, or_, select

from ..postgres import _iso, _now
from ..sqlalchemy import get_engine, platform_audit_events, platform_metric_events

BEIJING = ZoneInfo("Asia/Shanghai")


def _beijing_date(value: Any):
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(BEIJING).date()


class PostgresMetricsStore:
    """阶段指标持久化；失败不应阻断主业务。"""

    def __init__(self, dsn: str | None = None, engine: Any = None) -> None:
        self.dsn = dsn or os.getenv("DATABASE_URL", "")
        if not self.dsn and engine is None:
            raise ValueError("DATABASE_URL 未配置")
        self.engine = engine or get_engine(self.dsn)

    def record(
        self,
        *,
        metric_name: str,
        status: str = "SUCCESS",
        duration_ms: float = 0,
        error_code: str | None = None,
        estimated_cost: float | None = None,
        trace_id: str | None = None,
    ) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                platform_metric_events.insert().values(
                    metric_name=metric_name,
                    status=status,
                    duration_ms=round(float(duration_ms), 3),
                    error_code=error_code,
                    estimated_cost=estimated_cost,
                    trace_id=trace_id,
                    created_at=_now(),
                )
            )

    def snapshot(self, *, metric_name: str | None = None, window_hours: int = 24) -> dict[str, Any]:
        window_hours = max(1, min(int(window_hours), 24 * 30))
        start = _now() - timedelta(hours=window_hours)
        filters = [platform_metric_events.c.created_at >= start]
        if metric_name:
            filters.append(platform_metric_events.c.metric_name == metric_name)
        success = platform_metric_events.c.status.in_(["SUCCESS", "COMPLETED"])
        aggregate_query = (
            select(
                platform_metric_events.c.metric_name,
                func.count().label("count"),
                func.sum(case((~success, 1), else_=0)).label("error_count"),
                func.avg(platform_metric_events.c.duration_ms).label("avg_ms"),
                func.sum(platform_metric_events.c.estimated_cost).label("estimated_cost"),
            )
            .where(*filters)
            .group_by(platform_metric_events.c.metric_name)
        )
        detail_query = select(
            platform_metric_events.c.metric_name,
            platform_metric_events.c.status,
            platform_metric_events.c.duration_ms,
            platform_metric_events.c.error_code,
            platform_metric_events.c.created_at,
            platform_metric_events.c.trace_id,
        ).where(*filters)
        with self.engine.connect() as conn:
            aggregate_rows = conn.execute(aggregate_query).mappings().all()
            rows = conn.execute(detail_query).mappings().all()
            today = datetime.now(BEIJING).date()
            yesterday = today - timedelta(days=1)
            previous_start = datetime.combine(yesterday, time.min, tzinfo=BEIJING).astimezone(
                timezone.utc
            )
            comparison_query = select(
                platform_metric_events.c.metric_name,
                platform_metric_events.c.created_at,
            ).where(platform_metric_events.c.created_at >= previous_start)
            if metric_name:
                comparison_query = comparison_query.where(
                    platform_metric_events.c.metric_name == metric_name
                )
            comparison_rows = conn.execute(comparison_query).mappings().all()
        grouped: dict[str, list[tuple[str, float, str | None, Any, str | None]]] = {}
        for row in rows:
            grouped.setdefault(row["metric_name"], []).append(
                (
                    row["status"],
                    float(row["duration_ms"]),
                    row["error_code"],
                    row["created_at"],
                    row["trace_id"],
                )
            )
        result: dict[str, Any] = {}
        for aggregate in aggregate_rows:
            name = aggregate["metric_name"]
            values = grouped.get(name, [])
            durations = sorted(item[1] for item in values)
            count = int(aggregate["count"] or 0)
            errors = int(aggregate["error_count"] or 0)
            costs = float(aggregate["estimated_cost"] or 0)
            failures = [item for item in values if item[0] not in {"SUCCESS", "COMPLETED"}]
            error_codes: dict[str, int] = {}
            for item in failures:
                code = item[2] or "UNKNOWN_ERROR"
                error_codes[code] = error_codes.get(code, 0) + 1
            result[name] = {
                "count": count,
                "error_count": len(failures),
                "error_rate": round(errors / count, 4),
                "avg_ms": round(float(aggregate["avg_ms"] or 0), 3),
                "p50_ms": round(durations[len(durations) // 2], 3),
                "p95_ms": round(
                    durations[
                        min(len(durations) - 1, max(0, math.ceil(len(durations) * 0.95) - 1))
                    ],
                    3,
                ),
                "estimated_cost": round(costs, 8),
                "error_codes": error_codes,
                "recent_failures": [
                    {
                        "status": item[0],
                        "error_code": item[2] or "UNKNOWN_ERROR",
                        "created_at": _iso(item[3]),
                        "trace_id": item[4],
                    }
                    for item in failures[-5:][::-1]
                ],
            }
        comparison: dict[str, Any] = {}
        for row in comparison_rows:
            name = row["metric_name"]
            item = comparison.setdefault(name, {"today": 0, "yesterday": 0})
            day = _beijing_date(row["created_at"])
            if day == today:
                item["today"] += 1
            elif day == yesterday:
                item["yesterday"] += 1
        for item in comparison.values():
            item["delta"] = item["today"] - item["yesterday"]
        return {
            "window_hours": window_hours,
            "timezone": "Asia/Shanghai",
            "metrics": result,
            "comparison": comparison,
        }


class PostgresAuditSink:
    """Supervisor 审计事件写入 PostgreSQL。"""

    _KEY_EVENTS = frozenset(
        {
            "user_draft_created",
            "user_draft_updated",
            "user_draft_published",
            "user_draft_unpublished",
            "user_draft_deleted",
            "user_attachment_deleted",
            "user_interaction",  # 历史事件名称
            "chat_completed",
            "knowledge_upload_completed",
            "knowledge_parse_completed",
            "knowledge_draft_extraction_completed",
            "knowledge_chunk_completed",
            "knowledge_index_completed",
            "supervisor_request_end",
            "supervisor_tool_call_rejected",
            "supervisor_dependency_blocked",
            "retention_cleanup_completed",
            "retention_cleanup_failed",
            "admin_feedback_resolved",
        }
    )

    _EVENT_LABELS: ClassVar = {
        "user_draft_created": "用户生成草稿",
        "user_draft_updated": "用户保存草稿",
        "user_draft_published": "用户发布草稿",
        "user_draft_unpublished": "用户下架草稿",
        "user_draft_deleted": "用户删除草稿",
        "user_attachment_deleted": "用户删除附件",
        "user_interaction": "用户使用智能体",
        "chat_completed": "用户聊天完成",
        "knowledge_upload_completed": "文件上传完成",
        "knowledge_parse_completed": "文件解析完成",
        "knowledge_draft_extraction_completed": "字段草稿提取完成",
        "knowledge_chunk_completed": "文件分块完成",
        "knowledge_index_completed": "知识入库完成",
        "supervisor_request_end": "用户意图流程完成",
        "chat_identity_resolved": "完成身份识别",
        "supervisor_tool_call_end": "工具调用失败",
        "supervisor_tool_call_rejected": "工具调用被拒绝",
        "supervisor_dependency_blocked": "依赖服务阻断",
        "retention_cleanup_completed": "运行状态清理完成",
        "retention_cleanup_failed": "运行状态清理失败",
        "admin_feedback_resolved": "完成检索反馈处置",
    }
    _SERVICE_LABELS: ClassVar = {
        "supervisor": "智能编排",
        "perception": "文件解析",
        "retrieval": "知识检索",
        "operations": "运行治理",
        "governance": "审核治理",
        "file_workflow": "文件流程",
    }
    _PHASE_LABELS: ClassVar = {
        "supervisor_request_end": "请求收尾",
        "supervisor_intent_selected": "意图识别",
        "supervisor_internal_intent_selected": "意图识别",
        "supervisor_plan_created": "执行计划",
        "supervisor_tool_call_end": "工具执行",
        "supervisor_tool_call_rejected": "工具校验",
        "supervisor_evaluation_completed": "流程评估",
        "supervisor_compensation_decided": "失败补偿",
        "supervisor_reasoning_end": "结果推理",
    }
    _TOOL_LABELS: ClassVar = {
        "retrieval.search_knowledge": "知识库检索",
        "retrieval.index_document": "知识库索引",
        "perception.parse_document": "文件解析",
        "perception.upload_document": "文件上传",
    }
    _ERROR_LABELS: ClassVar = {
        "TOOL_TIMEOUT": "工具调用超时",
        "TOOL_EXECUTION_FAILED": "工具执行异常",
        "OUTPUT_SCHEMA_INVALID": "工具返回结果格式不正确",
        "CIRCUIT_OPEN": "该工具近期连续失败，系统暂时停止调用",
        "MISSING_ARGUMENT": "缺少工具所需参数",
        "DEPENDENCY_BLOCKED": "前置步骤失败，当前步骤未执行",
        "EXECUTION_FAILED": "执行流程失败",
    }
    # 只把业务人员能理解、且适合展示的字段提升到响应顶层；原始 payload
    # 仍保留在 PostgreSQL，避免把内部参数和正文直接暴露到管理端列表。
    _DISPLAY_FIELDS = (
        "user_id",
        "user_name",
        "operator_id",
        "operator_name",
        "user_role",
        "company",
        "company_name",
        "enterprise_name",
        "document_id",
        "file_name",
        "draft_id",
        "draft_title",
        "knowledge_type",
        "retrieval_duration_ms",
        "draft_duration_ms",
        "attachment_names",
        "status",
        "error_code",
        "block_count",
        "chunk_count",
        "indexed_count",
        "draft_generated",
        "attachment_uploaded",
        "attachment_count",
        "match_generated",
        "match_count",
        "evidence_count",
        "duration_ms",
        "intent",
        "intent_operation",
        "intent_knowledge_type",
        "flow_state",
    )

    def __init__(self, dsn: str | None = None, engine: Any = None) -> None:
        self.dsn = dsn or os.getenv("DATABASE_URL", "")
        if not self.dsn and engine is None:
            raise ValueError("DATABASE_URL 未配置")
        self.engine = engine or get_engine(self.dsn)

    def record(self, event: str, **fields: Any) -> None:
        if event not in self._KEY_EVENTS and not (
            event == "supervisor_tool_call_end" and str(fields.get("status") or "") != "SUCCESS"
        ):
            return
        with self.engine.begin() as conn:
            conn.execute(
                platform_audit_events.insert().values(
                    event_name=event,
                    request_id=fields.pop("request_id", None),
                    session_id=fields.pop("session_id", None),
                    task_id=fields.pop("task_id", None),
                    trace_id=fields.pop("trace_id", None),
                    service=fields.pop("service", "supervisor"),
                    payload=fields,
                    created_at=_now(),
                )
            )

    @staticmethod
    def _chat_summary_payload(
        *,
        result: Any,
        draft_context: dict[str, Any] | None,
        duration_ms: float,
        user_name: str | None,
        company_name: str | None,
    ) -> dict[str, Any]:
        context = draft_context or {}
        attachments = [
            item for item in (context.get("attachments") or []) if isinstance(item, dict)
        ]
        drafts = list(getattr(result, "drafts", None) or [])
        if getattr(result, "draft", None):
            drafts.append(result.draft)
        recommendations = list(getattr(result, "recommendations", None) or [])
        related = list(getattr(result, "related_references", None) or [])
        intent = getattr(result, "intent", None) or {}
        intent_name = intent.get("name") if isinstance(intent, dict) else None
        draft_title = next(
            (
                item.get("title") or item.get("draft_title")
                for item in drafts
                if isinstance(item, dict)
            ),
            None,
        )
        return {
            key: value
            for key, value in {
                "status": getattr(result, "status", "SUCCESS"),
                "user_name": user_name,
                "company_name": company_name,
                "intent": intent_name,
                "intent_operation": intent.get("operation") if isinstance(intent, dict) else None,
                "draft_generated": bool(drafts),
                "draft_title": draft_title,
                "attachment_uploaded": bool(attachments),
                "attachment_count": len(attachments),
                "attachment_names": [
                    item.get("file_name") or item.get("name")
                    for item in attachments
                    if item.get("file_name") or item.get("name")
                ],
                "match_generated": bool(recommendations or related),
                "match_count": len(recommendations) + len(related),
                "evidence_count": len(getattr(result, "evidences", None) or []),
                "retrieval_duration_ms": getattr(result, "retrieval_elapsed_ms", None),
                "duration_ms": duration_ms,
                "flow_state": getattr(result, "flow_state", None),
                "error_code": getattr(result, "error_code", None),
                "error": getattr(result, "error", None),
            }.items()
            if value is not None
        }

    def record_chat_summary(
        self,
        *,
        user_id: str,
        session_id: str,
        request_id: str,
        result: Any,
        draft_context: dict[str, Any] | None,
        duration_ms: float,
        user_name: str | None = None,
        company_name: str | None = None,
        trace_id: str | None = None,
    ) -> None:
        self.record(
            "chat_completed",
            user_id=user_id,
            session_id=session_id,
            request_id=request_id,
            trace_id=trace_id,
            draft_id=(draft_context or {}).get("draft_id"),
            **self._chat_summary_payload(
                result=result,
                draft_context=draft_context,
                duration_ms=duration_ms,
                user_name=user_name,
                company_name=company_name,
            ),
        )

    def list(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        service: str | None = None,
        user_only: bool | None = False,
        event_name: str | None = None,
        user_id_filter: str | None = None,
        task_id: str | None = None,
        document_id: str | None = None,
        event_names: set[str] | None = None,
        trace_id_filter: str | None = None,
    ) -> list[dict[str, Any]]:
        where = [
            or_(
                platform_audit_events.c.event_name.in_(self._KEY_EVENTS),
                and_(
                    platform_audit_events.c.event_name == "supervisor_tool_call_end",
                    platform_audit_events.c.payload["status"].as_string() != "SUCCESS",
                ),
            )
        ]
        if service:
            where.append(platform_audit_events.c.service == service)
        if event_name:
            where.append(platform_audit_events.c.event_name == event_name)
        if event_names:
            where.append(platform_audit_events.c.event_name.in_(event_names))
        user_id = platform_audit_events.c.payload["user_id"].as_string()
        if user_id_filter:
            where.append(user_id == str(user_id_filter))
        elif user_only is True:
            where.append(user_id.is_not(None))
        elif user_only is False:
            where.append(user_id.is_(None))
        if task_id:
            where.append(platform_audit_events.c.task_id == task_id)
        if trace_id_filter:
            where.append(platform_audit_events.c.trace_id == trace_id_filter)
        if document_id:
            where.append(platform_audit_events.c.payload["document_id"].as_string() == document_id)
        with self.engine.connect() as conn:
            rows = (
                conn.execute(
                    platform_audit_events.select()
                    .where(*where)
                    .order_by(platform_audit_events.c.created_at.desc())
                    .limit(limit)
                    .offset(offset)
                )
                .mappings()
                .all()
            )
        return [
            self._display_event(
                {
                    "event_id": row["event_id"],
                    "event_name": row["event_name"],
                    "request_id": row["request_id"],
                    "session_id": row["session_id"],
                    "task_id": row["task_id"],
                    "trace_id": row["trace_id"],
                    "service": row["service"],
                    "payload": row["payload"] or {},
                    "created_at": _iso(row["created_at"]),
                }
            )
            for row in rows
        ]

    def get_user_event(self, event_id: int) -> dict[str, Any] | None:
        """读取单条用户审计事件，供管理员查看关联草稿和附件。"""
        user_id = platform_audit_events.c.payload["user_id"].as_string()
        query = (
            platform_audit_events.select()
            .where(
                platform_audit_events.c.event_id == int(event_id),
                user_id.is_not(None),
            )
            .limit(1)
        )
        with self.engine.connect() as conn:
            row = conn.execute(query).mappings().first()
        if row is None:
            return None
        return self._display_event(
            {
                "event_id": row["event_id"],
                "event_name": row["event_name"],
                "request_id": row["request_id"],
                "session_id": row["session_id"],
                "task_id": row["task_id"],
                "trace_id": row["trace_id"],
                "service": row["service"],
                "payload": row["payload"] or {},
                "created_at": _iso(row["created_at"]),
            }
        )

    def get_event(self, event_id: int) -> dict[str, Any] | None:
        """读取管理员可见的单条审计事件。"""
        query = (
            platform_audit_events.select()
            .where(platform_audit_events.c.event_id == int(event_id))
            .limit(1)
        )
        with self.engine.connect() as conn:
            row = conn.execute(query).mappings().first()
        if row is None:
            return None
        return self._display_event(
            {
                "event_id": row["event_id"],
                "event_name": row["event_name"],
                "request_id": row["request_id"],
                "session_id": row["session_id"],
                "task_id": row["task_id"],
                "trace_id": row["trace_id"],
                "service": row["service"],
                "payload": row["payload"] or {},
                "created_at": _iso(row["created_at"]),
            }
        )

    @classmethod
    def _display_event(cls, event: dict[str, Any]) -> dict[str, Any]:
        payload = event.get("payload") or {}
        event_name = event.get("event_name")
        label = cls._EVENT_LABELS.get(event_name, "系统操作")
        if event_name in {"user_interaction", "chat_completed"}:
            if payload.get("draft_generated"):
                label = "用户生成草稿"
            elif payload.get("match_generated"):
                label = "用户执行供需匹配"
            elif payload.get("attachment_uploaded"):
                label = "用户上传附件"
            else:
                label = "用户使用智能问答"
        service = event.get("service") or "platform"
        summary = payload.get("summary") or payload.get("reason") or payload.get("error")
        if not summary and payload.get("status"):
            summary = f"状态：{payload['status']}"
        display = {
            key: payload.get(key)
            for key in cls._DISPLAY_FIELDS
            if payload.get(key) is not None and payload.get(key) != ""
        }
        # 不同入口使用 company / enterprise_name 两种命名，管理端统一显示企业。
        display.setdefault(
            "company_name",
            payload.get("company") or payload.get("enterprise_name") or payload.get("company_name"),
        )
        return {
            **event,
            **display,
            "event_label": label,
            "service_label": cls._SERVICE_LABELS.get(service, service),
            "process_label": cls._SERVICE_LABELS.get(service, service),
            "phase_label": cls._PHASE_LABELS.get(event_name, "系统处理"),
            "result_label": {
                "SUCCESS": "成功",
                "COMPLETED": "完成",
                "PASS": "通过",
                "FAILED": "失败",
                "FAIL": "失败",
                "WARN": "需关注",
            }.get(str(payload.get("status") or "").upper(), "已记录"),
            "tool_label": cls._TOOL_LABELS.get(payload.get("tool"), payload.get("tool")),
            "error_code": payload.get("error_code"),
            "error_detail": payload.get("error")
            or payload.get("reason")
            or cls._ERROR_LABELS.get(payload.get("error_code")),
            "summary": summary or "关键节点已记录",
        }

    def count(
        self,
        *,
        service: str | None = None,
        user_only: bool | None = False,
        event_name: str | None = None,
        user_id_filter: str | None = None,
        task_id: str | None = None,
        document_id: str | None = None,
        event_names: set[str] | None = None,
        trace_id_filter: str | None = None,
    ) -> int:
        where = [
            or_(
                platform_audit_events.c.event_name.in_(self._KEY_EVENTS),
                and_(
                    platform_audit_events.c.event_name == "supervisor_tool_call_end",
                    platform_audit_events.c.payload["status"].as_string() != "SUCCESS",
                ),
            )
        ]
        if service:
            where.append(platform_audit_events.c.service == service)
        if event_name:
            where.append(platform_audit_events.c.event_name == event_name)
        if event_names:
            where.append(platform_audit_events.c.event_name.in_(event_names))
        user_id = platform_audit_events.c.payload["user_id"].as_string()
        if user_id_filter:
            where.append(user_id == str(user_id_filter))
        elif user_only is True:
            where.append(user_id.is_not(None))
        elif user_only is False:
            where.append(user_id.is_(None))
        if task_id:
            where.append(platform_audit_events.c.task_id == task_id)
        if trace_id_filter:
            where.append(platform_audit_events.c.trace_id == trace_id_filter)
        if document_id:
            where.append(platform_audit_events.c.payload["document_id"].as_string() == document_id)
        with self.engine.connect() as conn:
            value = conn.execute(
                select(func.count()).select_from(platform_audit_events).where(*where)
            ).scalar_one()
        return int(value)
